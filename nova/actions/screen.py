"""Screen pixels: one grab of a rectangle (``mss``) and a cheap row signature, so an action can tell whether the
content under it changed (the receipt for keys and wheel scrolls, SPEC §8).

``mss`` sets the process DPI awareness the first time one is built, so it is built lazily: in the tray app that is
long after Qt set its own (the call then fails harmlessly), and grabs use physical pixels either way.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .native import ActionError, Rect

_local = threading.local()
CHANGED_SHARE = 0.06  # rows that must differ to count as "the content moved" (a blinking caret is ~3 %)


@dataclass(frozen=True)
class Shot:
    left: int
    top: int
    width: int
    height: int
    bgra: bytes


def _grabber():
    grabber = getattr(_local, "mss", None)
    if grabber is None:
        import mss  # noqa: PLC0415 - lazy on purpose (DPI awareness, above)

        grabber = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
        _local.mss = grabber
    return grabber


def grab(rect: Rect) -> Shot:
    """The pixels of ``rect`` (physical, left/top/right/bottom), BGRA."""
    left, top, right, bottom = rect
    width, height = max(1, right - left), max(1, bottom - top)
    try:
        shot = _grabber().grab({"left": left, "top": top, "width": width, "height": height})
    except Exception as error:  # noqa: BLE001 - mss raises its own ScreenShotError and OSErrors
        raise ActionError("I couldn't read the screen.") from error
    return Shot(left, top, shot.width, shot.height, bytes(shot.bgra))


def inset(rect: Rect, left: float = 0.2, top: float = 0.25, right: float = 0.2, bottom: float = 0.1) -> Rect:
    """The middle of a window: away from toolbars, scroll bars and the clock, where scrolled content shows."""
    x0, y0, x1, y1 = rect
    w, h = x1 - x0, y1 - y0
    return (x0 + int(w * left), y0 + int(h * top), x1 - int(w * right), y1 - int(h * bottom))


def rows(shot: Shot, step: int = 2) -> tuple[int, ...]:
    """One hash per ``step``-th pixel row."""
    stride = shot.width * 4
    data = shot.bgra
    return tuple(hash(data[i * stride:(i + 1) * stride]) for i in range(0, shot.height, step))


def changed(before: tuple[int, ...], after: tuple[int, ...], share: float = CHANGED_SHARE) -> bool:
    """Did enough rows change to call it movement (not a caret blink)?"""
    if len(before) != len(after):
        return True
    if not before:
        return False
    diff = sum(a != b for a, b in zip(before, after, strict=True))
    return diff >= max(2, share * len(before))


def signature(rect: Rect) -> tuple[int, ...]:
    return rows(grab(rect))
