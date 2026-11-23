"""The numbers layer (SPEC §8.1): numbered tags or a 3 × 3 grid over one monitor; and guide mode's ring (Stage 07d)
around the one control he should click, on a layer of its own (it can be on another monitor).

A plain QWidget (no Qt Quick: it paints a few dozen rectangles once per showing), click-through, always on top,
never focused, excluded from screen capture (so OCR and the click receipts never see it) unless NOVA_CAPTURABLE=1.
It only draws what ``MarksShown`` / ``GuideRing`` say and hides on ``MarksHidden`` / ``GuideHidden``;
``nova.actions.pointing`` and ``nova.actions.taskworld`` own the state.
Rectangles arrive in physical pixels; the QScreen with the same device name maps them to logical ones.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

from PySide6.QtCore import QObject, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen, QScreen
from PySide6.QtWidgets import QWidget

from ..bus import Bus
from ..events import GuideHidden, GuideRing, MarksHidden, MarksShown
from .window import capturable

_log = logging.getLogger(__name__)

_WDA_EXCLUDEFROMCAPTURE = 0x11
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
_user32.SetWindowDisplayAffinity.restype = wintypes.BOOL

Rect = tuple[int, int, int, int]
_TAG_BG = QColor(255, 214, 10)
_TAG_FG = QColor(20, 20, 20)
_GRID_LINE = QColor(255, 214, 10, 200)
_GRID_SHADE = QColor(0, 0, 0, 40)
_RING = QColor(100, 210, 255)  # the island's guide accent (#64D2FF)
_RING_GLOW = QColor(100, 210, 255, 70)
_RING_PAD = 6.0  # logical px between the control and the ring


def to_logical(rect: Rect, monitor: Rect, origin: tuple[int, int], dpr: float) -> tuple[float, float, float, float]:
    """A physical-pixel rect on ``monitor`` -> (x, y, w, h) in the overlay's own logical coordinates (its top-left is
    the monitor's top-left; ``origin`` is unused there but kept for the screen's logical position)."""
    del origin
    left, top = (rect[0] - monitor[0]) / dpr, (rect[1] - monitor[1]) / dpr
    return left, top, (rect[2] - rect[0]) / dpr, (rect[3] - rect[1]) / dpr


def tag_box(x: float, y: float, text: str, size: float, bounds: tuple[float, float]) -> tuple[float, float, float, float]:
    """Where a tag's label goes: on the target's top-left corner, kept on screen."""
    width = size * (0.62 * len(text) + 0.7)
    height = size * 1.35
    x = min(max(0.0, x - width * 0.35), bounds[0] - width)
    y = min(max(0.0, y - height * 0.35), bounds[1] - height)
    return x, y, width, height


def ring_rect(box: Rect, monitor: Rect, dpr: float, bounds: tuple[float, float],
              pad: float = _RING_PAD) -> tuple[float, float, float, float]:
    """The guide ring around a physical-pixel ``box``: (x, y, w, h) in the layer's logical px, ``pad`` out, on screen."""
    x, y, w, h = to_logical(box, monitor, (0, 0), dpr)
    left, top = max(0.0, x - pad), max(0.0, y - pad)
    right, bottom = min(bounds[0], x + w + pad), min(bounds[1], y + h + pad)
    return left, top, max(0.0, right - left), max(0.0, bottom - top)


class _Layer(QWidget):
    def __init__(self, title: str = "Nova numbers") -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus
                         | Qt.WindowType.NoDropShadowWindowHint | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowTitle(title)
        self._shown: MarksShown | None = None
        self._ring: GuideRing | None = None
        self._dpr = 1.0
        self._excluded = False

    def present(self, shown: MarksShown) -> None:
        if self._place(shown.device):
            self._shown = shown
            self.update()

    def ring(self, ring: GuideRing) -> None:
        if self._place(ring.device):
            self._ring = ring
            self.update()

    def _place(self, device: str) -> bool:
        screen = _screen_named(device)
        if screen is None:
            _log.warning("overlay: no screen called %s", device)
            return False
        self._dpr = screen.devicePixelRatio() or 1.0
        if self.screen() is not screen:
            self.setScreen(screen)
        self.setGeometry(screen.geometry())
        if not self.isVisible():
            self.show()
            self._exclude()
        self.raise_()
        return True

    def clear(self) -> None:
        self._shown = None
        self._ring = None
        self.hide()

    def _exclude(self) -> None:
        if self._excluded or capturable():
            return
        if _user32.SetWindowDisplayAffinity(int(self.winId()), _WDA_EXCLUDEFROMCAPTURE):
            self._excluded = True
        else:
            _log.warning("numbers: couldn't exclude them from screen capture (error %d)", ctypes.get_last_error())

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt's name
        shown = self._shown
        if shown is None and self._ring is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds = (float(self.width()), float(self.height()))
        if self._ring is not None:
            self._paint_ring(painter, self._ring, bounds)
        if shown is None:
            painter.end()
            return
        if shown.cells:
            self._paint_grid(painter, shown)
        size = 11.0
        font = QFont("Segoe UI", 1)
        font.setPixelSize(int(size))
        font.setBold(True)
        painter.setFont(font)
        for number, rect in shown.tags:
            x, y, w, h = to_logical(rect, shown.monitor, (0, 0), self._dpr)
            painter.setPen(QPen(_TAG_BG, 1.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(x, y, w, h), 3, 3)
            text = str(number)
            lx, ly, lw, lh = tag_box(x, y, text, size, bounds)
            painter.setPen(QPen(_TAG_FG, 1.0))
            painter.setBrush(_TAG_BG)
            painter.drawRoundedRect(QRectF(lx, ly, lw, lh), 4, 4)
            painter.drawText(QRectF(lx, ly, lw, lh), Qt.AlignmentFlag.AlignCenter, text)
        painter.end()

    def _paint_ring(self, painter: QPainter, ring: GuideRing, bounds: tuple[float, float]) -> None:
        box = QRectF(*ring_rect(ring.box, ring.monitor, self._dpr, bounds))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(_RING_GLOW, 8.0))
        painter.drawRoundedRect(box, 8, 8)
        painter.setPen(QPen(_RING, 2.5))
        painter.drawRoundedRect(box, 8, 8)

    def _paint_grid(self, painter: QPainter, shown: MarksShown) -> None:
        for number, rect in shown.cells:
            x, y, w, h = to_logical(rect, shown.monitor, (0, 0), self._dpr)
            box = QRectF(x, y, w, h)
            painter.setBrush(_GRID_SHADE)
            painter.setPen(QPen(_GRID_LINE, 2.0))
            painter.drawRect(box)
            font = QFont("Segoe UI", 1)
            font.setPixelSize(max(14, int(min(w, h) / 4)))
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QPen(QColor(0, 0, 0, 180), 1.0))
            painter.drawText(box.translated(2, 2), Qt.AlignmentFlag.AlignCenter, str(number))
            painter.setPen(QPen(_TAG_BG, 1.0))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, str(number))


def _screen_named(device: str) -> QScreen | None:
    screens = QGuiApplication.screens()
    for screen in screens:
        if screen.name().lower() == device.lower():
            return screen
    return QGuiApplication.primaryScreen() if len(screens) == 1 else None


class Overlay(QObject):
    """Owns the layer on the GUI thread; bus events hop over through a queued signal."""

    _inbox = Signal(object)

    def __init__(self, bus: Bus) -> None:
        super().__init__()
        self._layer = _Layer()
        self._ringer = _Layer("Nova guide")
        self._inbox.connect(self._on_event)
        self._unsubscribe = [bus.subscribe(kind, self._inbox.emit)
                             for kind in (MarksShown, MarksHidden, GuideRing, GuideHidden)]

    def _on_event(self, event: object) -> None:
        if isinstance(event, MarksShown):
            self._layer.present(event)
        elif isinstance(event, MarksHidden):
            self._layer.clear()
        elif isinstance(event, GuideRing):
            self._ringer.ring(event)
        else:
            self._ringer.clear()

    def geometry(self) -> QRect:
        return self._layer.geometry()

    def close(self) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._layer.close()
        self._ringer.close()
