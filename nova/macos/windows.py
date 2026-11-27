"""macOS stand-in for ``nova.actions.windows``: maximize (fill the screen), minimize, restore, full screen, snap,
center, move to another display, and layouts. The pure geometry comes from the Windows module itself."""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path

from . import native
from .native import ActionError, Monitor, Rect, Win

_spec = importlib.util.spec_from_file_location(
    "nova.actions._windows_geometry", Path(__file__).resolve().parent.parent / "actions" / "windows.py")
_geometry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_geometry)
area_rect = _geometry.area_rect
centered = _geometry.centered
pick_monitor = _geometry.pick_monitor
translated = _geometry.translated
close_enough = _geometry.close_enough
_AREA_LABEL = _geometry._AREA_LABEL

_TOLERANCE = 12


def _monitor(handle: int) -> Monitor:
    found = next((m for m in native.monitors() if m.handle == handle), None)
    if found is None:
        raise ActionError("Couldn't tell which display that window is on.")
    return found


def _minimized(hwnd: int) -> bool:
    return native.show_state(hwnd) == native.SW_SHOWMINIMIZED


def _fullscreen_on(hwnd: int) -> bool:
    return native.show_state(hwnd) == native.SW_SHOWMAXIMIZED


def _normal(hwnd: int) -> None:
    if _minimized(hwnd) or _fullscreen_on(hwnd):
        native.show(hwnd, native.SW_RESTORE)
        native.wait_for(lambda: not _minimized(hwnd) and not _fullscreen_on(hwnd), 1.5, 0.05)


def put(hwnd: int, rect: Rect) -> bool:
    _normal(hwnd)
    native.place(hwnd, rect)
    landed = native.wait_for(lambda: close_enough(native.frame_rect(hwnd), rect, 24), 0.8)
    if not landed:
        native.place(hwnd, rect)
        landed = native.wait_for(lambda: close_enough(native.frame_rect(hwnd), rect, 24), 0.6)
    if not landed:
        return _fit_minimum(hwnd, rect)
    return True


def _fit_minimum(hwnd: int, rect: Rect) -> bool:
    """An app with a minimum size bigger than the slot (WhatsApp: 800 px wide): keep its size, but pull it back
    inside the slot's outer edges (a right-half snap stays flush with the screen's right edge)."""
    left, top, right, bottom = native.frame_rect(hwnd)
    width, height = right - left, bottom - top
    if width < rect[2] - rect[0] - 24 or height < rect[3] - rect[1] - 24:
        return False  # smaller than asked: it really didn't move / resize
    work = next((m.work for m in native.monitors() if m.handle == native.monitor_of(hwnd)), rect)
    x = rect[0] if rect[2] < work[2] else max(work[0], work[2] - width)
    y = rect[1] if rect[3] < work[3] else max(work[1], work[3] - height)
    native.place(hwnd, (x, y, x + width, y + height))
    return bool(native.wait_for(lambda: abs(native.frame_rect(hwnd)[0] - x) <= 24, 0.6))


def window_op(win: Win, op: str, name: str, monitor: str | None = None) -> str:
    hwnd = win.hwnd
    if op == "maximize":
        work = _monitor(native.monitor_of(hwnd)).work
        if not put(hwnd, work):
            raise ActionError(f"{name} didn't fill the screen (it may have a fixed size).")
        return f"Maximized {name}"
    if op == "minimize":
        native.show(hwnd, native.SW_MINIMIZE)
        if not native.wait_for(lambda: _minimized(hwnd), 1.5, 0.05):
            raise ActionError(f"{name} didn't minimize.")
        return f"Minimized {name}"
    if op == "restore":
        _normal(hwnd)
        if _minimized(hwnd):
            raise ActionError(f"{name} didn't restore.")
        return f"Restored {name}"
    if op == "fullscreen":
        if native.set_fullscreen(hwnd, True) and native.wait_for(lambda: _fullscreen_on(hwnd), 1.5, 0.05):
            return f"{name} full screen"
        put(hwnd, _monitor(native.monitor_of(hwnd)).work)
        return f"Maximized {name} (it has no full screen)"
    if op in ("snap_left", "snap_right", "snap_top", "snap_bottom"):
        area = op.removeprefix("snap_")
        work = _monitor(native.monitor_of(hwnd)).work
        if not put(hwnd, area_rect(work, area)):
            raise ActionError(f"{name} didn't move to the {area}.")
        return f"{name} on the {area}"
    if op == "center":
        work = _monitor(native.monitor_of(hwnd)).work
        _normal(hwnd)
        frame = native.frame_rect(hwnd)
        if not put(hwnd, centered(work, (frame[2] - frame[0], frame[3] - frame[1]))):
            raise ActionError(f"{name} didn't move to the center.")
        return f"Centered {name}"
    if op == "move_monitor":
        return _move_monitor(win, name, monitor or "other")
    raise ActionError(f"I can't {op.replace('_', ' ')} a window yet.")


def _move_monitor(win: Win, name: str, which: str) -> str:
    hwnd = win.hwnd
    all_monitors = native.monitors()
    current = native.monitor_of(hwnd)
    target = pick_monitor(all_monitors, current, which)
    if target is None:
        if len(all_monitors) < 2:
            raise ActionError("There's only one display connected.")
        raise ActionError(f"There's no display to the {which} of that one.")
    source = next(m for m in all_monitors if m.handle == current)
    _normal(hwnd)
    rect = translated(native.frame_rect(hwnd), source, target)
    native.place(hwnd, rect)
    if not native.wait_for(lambda: native.monitor_of(hwnd) == target.handle, 1.0):
        raise ActionError(f"{name} didn't move to the other display.")
    return f"{name} moved to the {'other' if which in ('other', 'next', 'prev') else which} display"


def arrange(placed: Sequence[tuple[Win, str, str]], monitor: Monitor) -> str:
    missed = []
    for win, area, name in placed:
        if not put(win.hwnd, area_rect(monitor.work, area)):
            missed.append(name)
    for win, _area, _name in placed:
        try:
            native.activate(win.hwnd)
        except ActionError:
            pass
    if missed:
        raise ActionError(f"{' and '.join(missed)} didn't move into place.")
    return " · ".join(f"{name} {_AREA_LABEL.get(area, area.replace('_', ' '))}" for _w, area, name in placed)
