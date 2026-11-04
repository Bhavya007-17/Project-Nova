"""Window actions (SPEC §8): maximize / minimize / restore / full screen / snap / center / move to a monitor,
and layouts of 2-4 windows ("Fusion on the left, Claude on the right").

Rectangles are physical pixels (per-monitor DPI aware) in a monitor's work area (taskbar
excluded); ``native.place`` compensates the invisible DWM borders, so snapped windows sit
edge to edge like Windows Snap. Every move is checked by reading the frame back.
The geometry helpers at the top are pure and unit-tested.
"""

from __future__ import annotations

from collections.abc import Sequence

from . import native
from .native import ActionError, Monitor, Rect, Win

_TOLERANCE = 12  # px: apps round sizes to their own steps (terminal cells, min widths)


# --- geometry (pure) ------------------------------------------------------------------------------------------------


def area_rect(work: Rect, area: str) -> Rect:
    left, top, right, bottom = work
    width, height = right - left, bottom - top
    mid_x, mid_y = left + width // 2, top + height // 2
    third = width // 3
    areas = {
        "left": (left, top, mid_x, bottom),
        "right": (mid_x, top, right, bottom),
        "top": (left, top, right, mid_y),
        "bottom": (left, mid_y, right, bottom),
        "top_left": (left, top, mid_x, mid_y),
        "top_right": (mid_x, top, right, mid_y),
        "bottom_left": (left, mid_y, mid_x, bottom),
        "bottom_right": (mid_x, mid_y, right, bottom),
        "left_third": (left, top, left + third, bottom),
        "middle_third": (left + third, top, left + 2 * third, bottom),
        "right_third": (left + 2 * third, top, right, bottom),
        "full": work,
    }
    return areas[area]


def centered(work: Rect, size: tuple[int, int]) -> Rect:
    left, top, right, bottom = work
    width = min(size[0], round((right - left) * 0.9))
    height = min(size[1], round((bottom - top) * 0.9))
    x = left + (right - left - width) // 2
    y = top + (bottom - top - height) // 2
    return (x, y, x + width, y + height)


def pick_monitor(monitors: Sequence[Monitor], current: int, which: str) -> Monitor | None:
    """The monitor to move to: other / next / prev (wrapping, left-to-right order), left / right of the current, primary."""
    ordered = sorted(monitors, key=lambda m: (m.rect[0], m.rect[1]))
    if len(ordered) < 2:
        return None
    index = next((i for i, m in enumerate(ordered) if m.handle == current), 0)
    here = ordered[index]
    if which == "primary":
        found = next((m for m in ordered if m.primary), None)
        return None if found is None or found.handle == current else found
    if which in ("other", "next"):
        return ordered[(index + 1) % len(ordered)]
    if which == "prev":
        return ordered[(index - 1) % len(ordered)]
    if which == "left":
        lefts = [m for m in ordered if m.rect[0] < here.rect[0]]
        return lefts[-1] if lefts else None
    if which == "right":
        rights = [m for m in ordered if m.rect[0] > here.rect[0]]
        return rights[0] if rights else None
    return None


def translated(rect: Rect, source: Monitor, target: Monitor) -> Rect:
    """Same logical size (DPI-scaled) and the same relative place in the target's work area; never bigger than it."""
    sl, st, sr, sb = source.work
    tl, tt, tr, tb = target.work
    scale = target.dpi / source.dpi if source.dpi else 1.0
    width = min(round((rect[2] - rect[0]) * scale), tr - tl)
    height = min(round((rect[3] - rect[1]) * scale), tb - tt)
    room_x = max(1, (sr - sl) - (rect[2] - rect[0]))
    room_y = max(1, (sb - st) - (rect[3] - rect[1]))
    fx = min(1.0, max(0.0, (rect[0] - sl) / room_x))
    fy = min(1.0, max(0.0, (rect[1] - st) / room_y))
    x = tl + round(fx * (tr - tl - width))
    y = tt + round(fy * (tb - tt - height))
    return (x, y, x + width, y + height)


def close_enough(a: Rect, b: Rect, tolerance: int = _TOLERANCE) -> bool:
    return all(abs(p - q) <= tolerance for p, q in zip(a, b, strict=True))


# --- doing it --------------------------------------------------------------------------------------------------------


def _monitor(handle: int) -> Monitor:
    found = next((m for m in native.monitors() if m.handle == handle), None)
    if found is None:
        raise ActionError("Couldn't tell which monitor that window is on.")
    return found


def _normal(hwnd: int) -> None:
    """Out of maximized / minimized, so it can be moved and sized."""
    if native.user32.IsIconic(hwnd) or native.user32.IsZoomed(hwnd):
        native.show(hwnd, native.SW_RESTORE)
        native.wait_for(lambda: not native.user32.IsIconic(hwnd) and not native.user32.IsZoomed(hwnd), 0.6)
        if native.user32.IsZoomed(hwnd):  # a minimized window can come back maximized
            native.show(hwnd, native.SW_RESTORE)
            native.wait_for(lambda: not native.user32.IsZoomed(hwnd), 0.6)


def put(hwnd: int, rect: Rect) -> bool:
    """Restore, move the visible frame onto ``rect``, and check it got there."""
    _normal(hwnd)
    native.place(hwnd, rect)
    landed = native.wait_for(lambda: close_enough(native.frame_rect(hwnd), rect), 0.8)
    if not landed:  # some apps re-layout after the first move (DPI change between monitors): once more
        native.place(hwnd, rect)
        landed = native.wait_for(lambda: close_enough(native.frame_rect(hwnd), rect), 0.6)
    return bool(landed)


def _check_drivable(win: Win, name: str) -> None:
    if native.elevated(win.pid):
        raise ActionError(f"{name} is running as administrator — Windows won't let Nova move it.")
    if native.is_hung(win.hwnd):
        raise ActionError(f"{name} isn't responding.")


def window_op(win: Win, op: str, name: str, monitor: str | None = None) -> str:
    """One window operation on ``win``; returns the island's result text."""
    _check_drivable(win, name)
    hwnd = win.hwnd
    if op == "maximize":
        native.show(hwnd, native.SW_SHOWMAXIMIZED)
        if not native.wait_for(lambda: native.user32.IsZoomed(hwnd), 1.0):
            raise ActionError(f"{name} didn't maximize.")
        return f"Maximized {name}"
    if op == "minimize":
        native.show(hwnd, native.SW_MINIMIZE)
        if not native.wait_for(lambda: native.user32.IsIconic(hwnd), 1.0):
            raise ActionError(f"{name} didn't minimize.")
        return f"Minimized {name}"
    if op == "restore":
        _normal(hwnd)
        if native.user32.IsIconic(hwnd) or native.user32.IsZoomed(hwnd):
            raise ActionError(f"{name} didn't restore.")
        return f"Restored {name}"
    if op == "fullscreen":
        return _fullscreen(win, name)
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


def _fullscreen(win: Win, name: str) -> str:
    hwnd = win.hwnd
    native.activate(hwnd)
    screen = _monitor(native.monitor_of(hwnd)).rect
    native.send_keys([(0x7A, True), (0x7A, False)])  # F11: the app's own full screen
    if native.wait_for(lambda: close_enough(native.window_rect(hwnd), screen, 16), 1.2):
        return f"{name} full screen"
    native.show(hwnd, native.SW_SHOWMAXIMIZED)  # no full screen in this app: the next best thing
    native.wait_for(lambda: native.user32.IsZoomed(hwnd), 0.8)
    return f"Maximized {name} (it has no full screen)"


def _move_monitor(win: Win, name: str, which: str) -> str:
    hwnd = win.hwnd
    all_monitors = native.monitors()
    current = native.monitor_of(hwnd)
    target = pick_monitor(all_monitors, current, which)
    if target is None:
        if len(all_monitors) < 2:
            raise ActionError("There's only one monitor connected.")
        raise ActionError(f"There's no monitor to the {which} of that one.")
    source = next(m for m in all_monitors if m.handle == current)
    was_max = bool(native.user32.IsZoomed(hwnd))
    _normal(hwnd)
    rect = translated(native.frame_rect(hwnd), source, target)
    native.place(hwnd, rect)
    native.wait_for(lambda: native.monitor_of(hwnd) == target.handle, 0.8)
    native.place(hwnd, rect)  # once more at the new DPI
    if not native.wait_for(lambda: native.monitor_of(hwnd) == target.handle, 0.6):
        raise ActionError(f"{name} didn't move to the other monitor.")
    if was_max:
        native.show(hwnd, native.SW_SHOWMAXIMIZED)
    return f"{name} moved to the {'other' if which in ('other', 'next', 'prev') else which} monitor"


def arrange(placed: Sequence[tuple[Win, str, str]], monitor: Monitor) -> str:
    """Layout: (window, area, name) for each slot on ``monitor``; then all of them to the front, the first slot last
    so the window he named first ends up with the focus... the last one named keeps it."""
    for win, _area, name in placed:
        _check_drivable(win, name)
    missed = []
    for win, area, name in placed:
        if not put(win.hwnd, area_rect(monitor.work, area)):
            missed.append(name)
    for win, _area, _name in placed:  # bring each to the front so the layout is visible, last named on top
        try:
            native.activate(win.hwnd)
        except ActionError:
            pass
    last = placed[-1][0].hwnd
    in_front = lambda: native.foreground() == last or native.root(native.foreground()) == last  # noqa: E731
    if not native.wait_for(in_front, 0.15):  # an app restored from maximized can grab the front back late
        try:
            native.activate(last)
        except ActionError:
            pass
    if missed:
        raise ActionError(f"{' and '.join(missed)} didn't move into place.")
    return " · ".join(f"{name} {_AREA_LABEL.get(area, area.replace('_', ' '))}" for _w, area, name in placed)


_AREA_LABEL = {"left": "left", "right": "right", "top": "top", "bottom": "bottom", "left_third": "left",
               "middle_third": "middle", "right_third": "right", "full": "full screen"}

