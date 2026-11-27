"""macOS stand-in for ``nova.actions.native``: windows, focus, monitors and synthetic input.

A window's "hwnd" is its CGWindowID. Window details and moves go through the Accessibility API (AXUIElement), z-order
through CGWindowList. Coordinates are global points, origin top-left of the main display (what AX and CG use).
Needs Accessibility permission for the process running Nova; without it ``app_windows`` sees nothing and moves fail.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TypeVar

import objc
import Quartz
from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication, NSScreen, NSWorkspace
from ApplicationServices import (
    AXIsProcessTrusted,
    AXIsProcessTrustedWithOptions,
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    AXUIElementCreateSystemWide,
    AXUIElementGetPid,
    AXUIElementPerformAction,
    AXUIElementSetAttributeValue,
    AXUIElementSetMessagingTimeout,
    AXValueCreate,
    AXValueGetValue,
    kAXValueCGPointType,
    kAXValueCGSizeType,
)
from Foundation import NSBundle

from ..keys import NOVA_MARK
from . import keymap

T = TypeVar("T")
Rect = tuple[int, int, int, int]  # left, top, right, bottom

SW_SHOWNORMAL, SW_SHOWMINIMIZED, SW_SHOWMAXIMIZED, SW_MINIMIZE, SW_RESTORE = 1, 2, 3, 6, 9
WM_CLOSE = 0x0010

_ax_fns: dict = {}
objc.loadBundleFunctions(NSBundle.bundleWithPath_("/System/Library/Frameworks/ApplicationServices.framework"),
                         _ax_fns, [("_AXUIElementGetWindow", b"i@o^I")])
_ax_window_id = _ax_fns.get("_AXUIElementGetWindow")

# Names the shared code compares against (it was written for Windows process names).
_EXE_NAMES = {"com.google.Chrome": "chrome.exe", "com.apple.finder": "explorer.exe"}


class ActionError(Exception):
    """An action couldn't be done; the message is plain words for the island."""


def wait_for(predicate: Callable[[], T], timeout: float, interval: float = 0.01) -> T | None:
    deadline = time.perf_counter() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.perf_counter() >= deadline:
            return None
        time.sleep(interval)


@contextmanager
def dpi_aware() -> Iterator[None]:
    yield


def com_init() -> None:
    """No COM on a Mac."""


def trusted(prompt: bool = False) -> bool:
    """Accessibility permission for this process (``prompt`` opens the system dialog once)."""
    if prompt:
        return bool(AXIsProcessTrustedWithOptions({"AXTrustedCheckOptionPrompt": True}))
    return bool(AXIsProcessTrusted())


# --- AX helpers -------------------------------------------------------------------------------------------------


def ax_get(element, attribute: str):
    if element is None:
        return None
    try:
        err, value = AXUIElementCopyAttributeValue(element, attribute, None)
    except Exception:  # noqa: BLE001 - a dead element
        return None
    return value if err == 0 else None


def ax_set(element, attribute: str, value) -> bool:
    try:
        return AXUIElementSetAttributeValue(element, attribute, value) == 0
    except Exception:  # noqa: BLE001
        return False


def ax_do(element, action: str) -> bool:
    try:
        return AXUIElementPerformAction(element, action) == 0
    except Exception:  # noqa: BLE001
        return False


def ax_pid(element) -> int:
    try:
        err, pid = AXUIElementGetPid(element, None)
        return int(pid) if err == 0 else 0
    except Exception:  # noqa: BLE001
        return 0


def ax_window_id(element) -> int:
    if _ax_window_id is None or element is None:
        return 0
    try:
        err, wid = _ax_window_id(element, None)
        return int(wid) if err == 0 else 0
    except Exception:  # noqa: BLE001
        return 0


def _point(value) -> tuple[float, float] | None:
    if value is None:
        return None
    ok, p = AXValueGetValue(value, kAXValueCGPointType, None)
    return (p.x, p.y) if ok else None


def _size(value) -> tuple[float, float] | None:
    if value is None:
        return None
    ok, s = AXValueGetValue(value, kAXValueCGSizeType, None)
    return (s.width, s.height) if ok else None


def app_element(pid: int):
    element = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(element, 0.4)
    return element


_system = None


def system_element():
    global _system
    if _system is None:
        _system = AXUIElementCreateSystemWide()
        AXUIElementSetMessagingTimeout(_system, 0.4)
    return _system


# --- windows ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Win:
    hwnd: int  # CGWindowID
    pid: int
    tid: int
    title: str
    cls: str  # AX subrole: AXStandardWindow / AXDialog / ...
    exe: str  # the app bundle's path
    aumids: tuple[str, ...]  # (bundle id,)
    minimized: bool = False
    maximized: bool = False  # full screen
    owner: int = 0  # dialogs / sheets: non-zero

    @property
    def exe_name(self) -> str:
        bundle = self.aumids[0] if self.aumids else ""
        if bundle in _EXE_NAMES:
            return _EXE_NAMES[bundle]
        return os.path.basename(self.exe.rstrip("/")).removesuffix(".app")


_lock = threading.Lock()
_elements: dict[int, tuple[int, object]] = {}  # wid -> (pid, AXUIElement)
_identities: dict[int, tuple[str, str]] = {}  # pid -> (bundle path, bundle id)


def _identity(pid: int) -> tuple[str, str]:
    known = _identities.get(pid)
    if known is not None:
        return known
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is None:
        return "", ""
    url = app.bundleURL()
    found = (str(url.path()) if url is not None else "", str(app.bundleIdentifier() or ""))
    _identities[pid] = found
    return found


def _app_windows_ax(pid: int) -> list[tuple[int, object]]:
    windows = ax_get(app_element(pid), "AXWindows") or []
    out = []
    for element in windows:
        wid = ax_window_id(element)
        if wid:
            out.append((wid, element))
    with _lock:
        for wid, element in out:
            _elements[wid] = (pid, element)
    return out


def _element(wid: int):
    with _lock:
        hit = _elements.get(wid)
    if hit is not None:
        return hit
    info = _cg_info(wid)
    if info is None:
        return None
    pid = int(info.get("kCGWindowOwnerPID", 0))
    for found, element in _app_windows_ax(pid):
        if found == wid:
            return pid, element
    return None


def _cg_info(wid: int) -> dict | None:
    rows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionIncludingWindow, wid) or []
    for row in rows:
        if int(row.get("kCGWindowNumber", 0)) == wid:
            return row
    return None


def _describe(wid: int, pid: int, element) -> Win | None:
    role = ax_get(element, "AXRole")
    if role is not None and str(role) != "AXWindow":
        return None
    exe, bundle = _identity(pid)
    subrole = str(ax_get(element, "AXSubrole") or "AXWindow")
    title = str(ax_get(element, "AXTitle") or "")
    owned = subrole in ("AXSystemDialog", "AXSheet", "AXFloatingWindow", "AXSystemFloatingWindow")
    if subrole == "AXDialog":  # Catalyst / Electron apps (WhatsApp...) call their main window a dialog
        owned = ax_get(element, "AXMinimizeButton") is None
    return Win(wid, pid, 0, title, subrole, exe, (bundle,) if bundle else (),
               bool(ax_get(element, "AXMinimized")), bool(ax_get(element, "AXFullScreen")), 1 if owned else 0)


def describe(hwnd: int) -> Win | None:
    if not hwnd:
        return None
    hit = _element(hwnd)
    if hit is None:
        return None
    return _describe(hwnd, hit[0], hit[1])


def window_title(hwnd: int) -> str:
    hit = _element(hwnd)
    return str(ax_get(hit[1], "AXTitle") or "") if hit else ""


def _regular_pids() -> list[int]:
    """Apps with a Dock icon. Owners of window-server windows are added to NSWorkspace's list, which only refreshes
    while a run loop spins (an app launched a second ago is missing from it in CLI runs)."""
    own = os.getpid()
    pids = {int(a.processIdentifier()) for a in NSWorkspace.sharedWorkspace().runningApplications()
            if a.activationPolicy() == 0}
    rows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
    for row in rows:
        pid = int(row.get("kCGWindowOwnerPID", 0))
        if pid in pids or int(row.get("kCGWindowLayer", 1)) != 0:
            continue
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is not None and app.activationPolicy() == 0:
            pids.add(pid)
    pids.discard(own)
    return sorted(pids)


def app_windows(*, include_owned: bool = False, limit: int | None = None) -> list[Win]:
    """Real app windows, frontmost first (on-screen z-order; minimized / other Spaces after)."""
    order = {}
    rows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID) or []
    for i, row in enumerate(rows):
        if int(row.get("kCGWindowLayer", 1)) == 0:
            order[int(row["kCGWindowNumber"])] = i
    found: list[tuple[int, Win]] = []
    for pid in _regular_pids():
        for wid, element in _app_windows_ax(pid):
            win = _describe(wid, pid, element)
            if win is None or (win.owner and not include_owned):
                continue
            if not win.title and wid not in order and not win.minimized:
                continue
            found.append((order.get(wid, 10_000 + len(found)), win))
    found.sort(key=lambda item: item[0])
    wins = [w for _, w in found]
    return wins[:limit] if limit is not None else wins


def front_app():
    """(pid, AX element) of the frontmost app. The system-wide element's AXFocusedApplication fails with
    kAXErrorCannotComplete on macOS 26 for processes started from a terminal, so ask the app itself."""
    pid = _front_pid()
    return (pid, app_element(pid)) if pid else (0, None)


def foreground() -> int:
    """The focused window of the frontmost app (0 if none / no permission)."""
    pid, app = front_app()
    window = ax_get(app, "AXFocusedWindow") or ax_get(app, "AXMainWindow")
    if window is not None:
        wid = ax_window_id(window)
        if wid:
            with _lock:
                _elements[wid] = (pid, window)
            return wid
    rows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID) or []
    for row in rows:
        if int(row.get("kCGWindowLayer", 1)) == 0 and int(row.get("kCGWindowOwnerPID", 0)) == pid:
            return int(row["kCGWindowNumber"])
    return 0


def root(hwnd: int) -> int:
    return hwnd


def is_window(hwnd: int) -> bool:
    return bool(hwnd) and _cg_info(hwnd) is not None


def is_hung(hwnd: int) -> bool:
    return False


def elevated(pid: int) -> bool:
    return False


def workstation_locked() -> bool:
    session = Quartz.CGSessionCopyCurrentDictionary() or {}
    return bool(session.get("CGSSessionScreenIsLocked", False))


def grant_foreground() -> None:
    """Nothing to grant on a Mac."""


def _front_pid() -> int:
    """The frontmost app, from the window server's z-order. NSWorkspace.frontmostApplication only updates while a
    run loop spins on the main thread, so in `nova say` (and other CLI runs) it reports a stale app."""
    own = os.getpid()
    rows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID) or []
    for row in rows:
        pid = int(row.get("kCGWindowOwnerPID", 0))
        if int(row.get("kCGWindowLayer", 1)) == 0 and pid != own:
            return pid
    front = NSWorkspace.sharedWorkspace().frontmostApplication()
    return int(front.processIdentifier()) if front is not None else 0


def activate_pid(pid: int) -> bool:
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is None:
        return False
    ax_set(app_element(pid), "AXFrontmost", True)
    if _front_pid() != pid:
        app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
    return bool(wait_for(lambda: _front_pid() == pid, 0.4, 0.02))


def activate(hwnd: int, timeout: float = 0.4) -> str:
    hit = _element(hwnd)
    if hit is None:
        raise ActionError("That window is gone.")
    pid, element = hit
    if ax_get(element, "AXMinimized"):
        ax_set(element, "AXMinimized", False)
    if foreground() == hwnd:
        return "already"
    ax_do(element, "AXRaise")
    ax_set(element, "AXMain", True)
    if activate_pid(pid) and wait_for(lambda: foreground() == hwnd, timeout, 0.02):
        return "plain"
    if _front_pid() == pid:
        return "app"
    raise ActionError("macOS wouldn't let me bring that window to the front.")


# --- geometry -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Monitor:
    handle: int
    rect: Rect
    work: Rect
    primary: bool
    device: str
    dpi: int


def _flip(frame, main_height: float) -> Rect:
    left = int(frame.origin.x)
    top = int(main_height - frame.origin.y - frame.size.height)
    return (left, top, left + int(frame.size.width), top + int(frame.size.height))


def monitors() -> list[Monitor]:
    screens = list(NSScreen.screens() or [])
    if not screens:
        return []
    main_height = screens[0].frame().size.height
    out = []
    for i, screen in enumerate(screens):
        number = int(screen.deviceDescription().get("NSScreenNumber", i + 1))
        out.append(Monitor(number, _flip(screen.frame(), main_height), _flip(screen.visibleFrame(), main_height),
                           i == 0, str(screen.localizedName()), int(72 * screen.backingScaleFactor())))
    return out


def _monitor_at(x: float, y: float) -> int:
    mons = monitors()
    for m in mons:
        if m.rect[0] <= x < m.rect[2] and m.rect[1] <= y < m.rect[3]:
            return m.handle
    return mons[0].handle if mons else 0


def monitor_of(hwnd: int) -> int:
    left, top, right, bottom = frame_rect(hwnd)
    return _monitor_at((left + right) / 2, (top + bottom) / 2)


def cursor_position() -> tuple[float, float]:
    point = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return point.x, point.y


def cursor_monitor() -> int:
    return _monitor_at(*cursor_position())


def cursor_over(hwnd: int) -> bool:
    x, y = cursor_position()
    left, top, right, bottom = frame_rect(hwnd)
    return left <= x < right and top <= y < bottom


def show_state(hwnd: int) -> int:
    hit = _element(hwnd)
    if hit and ax_get(hit[1], "AXMinimized"):
        return SW_SHOWMINIMIZED
    if hit and ax_get(hit[1], "AXFullScreen"):
        return SW_SHOWMAXIMIZED
    return SW_SHOWNORMAL


def show(hwnd: int, command: int) -> None:
    hit = _element(hwnd)
    if hit is None:
        return
    element = hit[1]
    if command in (SW_MINIMIZE, SW_SHOWMINIMIZED):
        ax_set(element, "AXMinimized", True)
    elif command == SW_RESTORE:
        ax_set(element, "AXMinimized", False)
        if ax_get(element, "AXFullScreen"):
            ax_set(element, "AXFullScreen", False)
    elif command == SW_SHOWMAXIMIZED:
        work = next((m.work for m in monitors() if m.handle == monitor_of(hwnd)), None)
        if work is not None:
            place(hwnd, work)


def frame_rect(hwnd: int) -> Rect:
    hit = _element(hwnd)
    if hit is None:
        return (0, 0, 0, 0)
    pos, size = _point(ax_get(hit[1], "AXPosition")), _size(ax_get(hit[1], "AXSize"))
    if pos is None or size is None:
        return (0, 0, 0, 0)
    return (int(pos[0]), int(pos[1]), int(pos[0] + size[0]), int(pos[1] + size[1]))


window_rect = frame_rect


def place(hwnd: int, visible: Rect) -> None:
    hit = _element(hwnd)
    if hit is None:
        raise ActionError("That window is gone.")
    element = hit[1]
    left, top, right, bottom = visible
    position = AXValueCreate(kAXValueCGPointType, Quartz.CGPoint(left, top))
    size = AXValueCreate(kAXValueCGSizeType, Quartz.CGSize(right - left, bottom - top))
    # Position, size, position again: a size change near a screen edge can push the window back.
    ax_set(element, "AXPosition", position)
    ax_set(element, "AXSize", size)
    ax_set(element, "AXPosition", position)


def post_close(hwnd: int) -> bool:
    hit = _element(hwnd)
    if hit is None:
        return False
    button = ax_get(hit[1], "AXCloseButton")
    return button is not None and ax_do(button, "AXPress")


def set_fullscreen(hwnd: int, on: bool) -> bool:
    hit = _element(hwnd)
    return hit is not None and ax_set(hit[1], "AXFullScreen", on)


# --- synthetic input --------------------------------------------------------------------------------------------

_source = None


def _event_source():
    global _source
    if _source is None:
        _source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStatePrivate)
    return _source


def _post(event) -> None:
    Quartz.CGEventSetIntegerValueField(event, Quartz.kCGEventSourceUserData, NOVA_MARK)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def send_codes(strokes: Sequence[tuple[int, bool]], flags: int = 0) -> int:
    """(mac key code, down) strokes with explicit modifier flags held through them."""
    sent = 0
    for code, down in strokes:
        event = Quartz.CGEventCreateKeyboardEvent(_event_source(), code, down)
        Quartz.CGEventSetFlags(event, flags)
        _post(event)
        sent += 1
    return sent


def send_keys(strokes: Sequence[tuple[int, bool]]) -> int:
    """(Windows VK, down) strokes, like the Windows version; modifier strokes become event flags."""
    flags = 0
    sent = 0
    for vk, down in strokes:
        code = keymap.VK_TO_MAC.get(vk)
        if code is None:
            sent += 1  # the Windows mask key etc.: nothing to do here
            continue
        flag = keymap.FLAG_FOR_VK.get(vk, 0)
        if flag:
            flags = flags | flag if down else flags & ~flag
            event = Quartz.CGEventCreateKeyboardEvent(_event_source(), code, down)
            Quartz.CGEventSetType(event, Quartz.kCGEventFlagsChanged)
        else:
            event = Quartz.CGEventCreateKeyboardEvent(_event_source(), code, down)
        Quartz.CGEventSetFlags(event, flags)
        _post(event)
        sent += 1
    return sent


def send_unicode(text: str) -> int:
    """Type text as Unicode keyboard events (up to 20 UTF-16 units each); newlines as Return."""
    count = 0
    for line_no, line in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n")):
        if line_no:
            count += send_codes([(36, True), (36, False)])
        units = line.encode("utf-16-le")
        for i in range(0, len(units), 40):
            chunk = units[i:i + 40].decode("utf-16-le", errors="ignore")
            for down in (True, False):
                event = Quartz.CGEventCreateKeyboardEvent(_event_source(), 0, down)
                Quartz.CGEventSetFlags(event, 0)
                Quartz.CGEventKeyboardSetUnicodeString(event, len(chunk.encode("utf-16-le")) // 2, chunk)
                _post(event)
                count += 1
    return count


def send_wheel(notches: int) -> int:
    event = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 1, notches * 3)
    _post(event)
    return 1


_MOD_MASK = keymap.FLAG_SHIFT | keymap.FLAG_CONTROL | keymap.FLAG_OPTION | keymap.FLAG_COMMAND


def modifiers_down() -> bool:
    return bool(Quartz.CGEventSourceFlagsState(Quartz.kCGEventSourceStateHIDSystemState) & _MOD_MASK)
