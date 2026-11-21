"""Screenshots and photos (prompt 07c §7).

"Take a screenshot": the screen the front window is on (or just that window) -> a PNG in Pictures\\Screenshots
named like the Snipping Tool's, and the picture on the clipboard (a DIB for every app, PNG for the ones that
read it). The island and the numbers overlay are excluded from capture, so they never show up in it.

"Take a picture": the Camera app is opened if need be, its shutter is invoked through UI Automation once the
preview is up (the Space key if it can't be found), and a new file in Camera Roll is the receipt.
The byte shuffling (``to_rgb``, ``to_dib``, ``screenshot_name``) is pure.
"""

from __future__ import annotations

import logging
import os
import re
import struct
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from . import apps, native, places, screen
from . import input as keyboard
from .apps import Outcome
from .catalog import App, Catalog
from .native import ActionError, Win
from .uia import FocusReader

_log = logging.getLogger(__name__)

_CF_DIB = 8
CAMERA_AUMID = "Microsoft.WindowsCamera_8wekyb3d8bbwe!App"
_SHUTTER = re.compile(r"^(take (a )?photo|capture( photo)?|shutter|take picture)\b", re.IGNORECASE)
_PHOTO_MODE = re.compile(r"^(photo( mode)?|switch to photo( mode)?)$", re.IGNORECASE)
PREVIEW_WAIT_S = 8.0  # the camera warming up
PHOTO_WAIT_S = 5.0  # the file landing in Camera Roll
SHOT_HOLD_S = 4.0


# --- pure ------------------------------------------------------------------------------------------------------------


def to_rgb(bgra: bytes, width: int, height: int) -> bytes:
    """Top-down BGRA -> top-down RGB (what a PNG wants)."""
    rgb = bytearray(width * height * 3)
    rgb[0::3] = bgra[2::4]
    rgb[1::3] = bgra[1::4]
    rgb[2::3] = bgra[0::4]
    return bytes(rgb)


def to_dib(bgra: bytes, width: int, height: int) -> bytes:
    """Top-down BGRA -> a packed 24-bit bottom-up DIB (CF_DIB): every app that pastes images reads this one."""
    stride = (width * 3 + 3) & ~3
    header = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, stride * height, 0, 0, 0, 0)
    pixels = bytearray(stride * height)
    for y in range(height):
        row = bgra[y * width * 4:(y + 1) * width * 4]
        start = (height - 1 - y) * stride
        out = bytearray(width * 3)
        out[0::3] = row[0::4]
        out[1::3] = row[1::4]
        out[2::3] = row[2::4]
        pixels[start:start + width * 3] = out
    return header + bytes(pixels)


def screenshot_name(now: datetime, taken: Callable[[str], bool]) -> str:
    """ "Screenshot 2026-11-21 143015.png", with " (2)" … when that name is taken."""
    base = f"Screenshot {now:%Y-%m-%d %H%M%S}"
    name, n = f"{base}.png", 2
    while taken(name):
        name, n = f"{base} ({n}).png", n + 1
    return name


# --- screenshot ------------------------------------------------------------------------------------------------------


def _area(target: str, front: Win | None) -> native.Rect:
    if target == "window":
        if front is None:
            raise ActionError("No window is in front to take.")
        return native.frame_rect(front.hwnd)
    handle = native.monitor_of(front.hwnd) if front is not None else native.cursor_monitor()
    monitors = native.monitors()
    monitor = next((m for m in monitors if m.handle == handle), None) or next((m for m in monitors if m.primary), None)
    if monitor is None:
        raise ActionError("Couldn't find the screen.")
    return monitor.rect


def screenshot(target: str, front: Win | None) -> Outcome:
    import mss.tools  # noqa: PLC0415 - mss is loaded lazily (screen.py)

    shot = screen.grab(_area(target, front))
    folder = places.known_folder("screenshots", create=True)
    name = screenshot_name(datetime.now(), lambda candidate: (folder / candidate).exists())
    png = mss.tools.to_png(to_rgb(shot.bgra, shot.width, shot.height), (shot.width, shot.height), level=1)
    try:
        (folder / name).write_bytes(png)
    except OSError as error:
        raise ActionError("Couldn't save the screenshot.") from error
    copied = " · copied"
    try:
        keyboard.put_clipboard([(_CF_DIB, to_dib(shot.bgra, shot.width, shot.height)),
                                (keyboard.clipboard_format("PNG"), png)])
    except ActionError as error:
        _log.info("screenshot saved, clipboard: %s", error)
        copied = ""
    what = "Window" if target == "window" else "Screen"
    return Outcome(f"{what} saved to Screenshots{copied}", "screenshot", hold=SHOT_HOLD_S)


# --- camera ----------------------------------------------------------------------------------------------------------


def _camera(catalog: Catalog) -> App:
    app = next((a for a in catalog.apps if a.aumid == CAMERA_AUMID), None) or catalog.app("Camera")
    if app is None:
        raise ActionError("The Camera app isn't installed.")
    return app


def _newest(folder: Path) -> float:
    try:
        return max((entry.stat().st_mtime for entry in os.scandir(folder) if entry.is_file()), default=0.0)
    except OSError:
        return 0.0


def _shutter(focus: FocusReader, hwnd: int, stop: Callable[[], bool]) -> str:
    """Invoke the shutter once the preview is up. The method used ("invoke", "space")."""
    deadline = time.monotonic() + PREVIEW_WAIT_S
    switched = False
    while time.monotonic() < deadline and not stop():
        elements = focus.snapshot(hwnd, timeout=1.5)
        shutter = next((e for e in elements if e.clickable and _SHUTTER.match(e.name.strip())), None)
        if shutter is not None:
            method = focus.act(shutter.index, shutter.patterns)
            if method:
                return method
        elif not switched:
            mode = next((e for e in elements if e.clickable and _PHOTO_MODE.match(e.name.strip())), None)
            if mode is not None:  # video mode: switch to photos first
                focus.act(mode.index, mode.patterns)
                switched = True
        time.sleep(0.3)
    if stop():
        raise ActionError("Stopped.")
    keyboard.press("space", native.describe(hwnd))
    return "space"


def photo(catalog: Catalog, focus: FocusReader, stop: Callable[[], bool] = lambda: False) -> Outcome:
    roll = places.known_folder("camera roll", create=True)
    before = _newest(roll)
    opened = apps.open_app(catalog, _camera(catalog))
    win = opened.win or native.describe(native.foreground())
    if win is None:
        raise ActionError("The Camera app didn't open.")
    method = _shutter(focus, win.hwnd, stop)
    if not native.wait_for(lambda: _newest(roll) > before, PHOTO_WAIT_S, interval=0.1):
        raise ActionError("I pressed the shutter, but no photo showed up in Camera Roll.")
    return Outcome("Photo saved to Camera Roll", method, win, hold=SHOT_HOLD_S)
