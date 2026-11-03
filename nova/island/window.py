"""The island window (SPEC §6): a frameless, transparent, always-on-top tool window on the Qt GUI thread.

- Never takes focus (WS_EX_NOACTIVATE via WindowDoesNotAcceptFocus), not even for
  Confirm / Clarify: Windows' foreground lock refused it here even with
  AttachThreadInput, and the confirmed action must land in his app anyway. Enter /
  1-4 answer through the keyboard hook instead (``nova.keys``).
- Click-through (WS_EX_LAYERED | WS_EX_TRANSPARENT via WindowTransparentForInput)
  unless something is clickable; then an input mask (SetWindowRgn) limits clicks to
  the pill and chips. The mask also clips drawing, so it is only set once nothing
  moves; during a morph the whole window takes clicks for ~0.3 s.
- Excluded from screen capture (WDA_EXCLUDEFROMCAPTURE) unless NOVA_CAPTURABLE=1.
- Hidden = the window is hidden: no frames, no animations, no timers. The first show
  creates the D3D11 device and swap chain (~165 ms measured); a warm-up show at start
  pays that once, so a key press later shows the island in about one frame.
- Qt Quick's "basic" render loop (render on the GUI thread) unless QSG_RENDER_LOOP says
  otherwise. Measured here, key press -> first frame: basic 10-19 ms, threaded
  37-79 ms (it waits ~2-3 vsyncs on DirectComposition), threaded without vsync 15-34 ms;
  CPU while listening about the same. SPEC §16 wants <= 50 ms. Animators then run on
  the GUI thread like other animations, which has nothing else to do.
- Shown top-centre of the monitor under the mouse; re-placed when monitors or DPI change.
- Ctrl + Copilot pins it (a small status pill between commands). Dragging the pill moves
  its home: later drops appear there too, until a double-click (back to top-centre) or a
  restart. The window keeps its full height on screen below the home, so expansions fit.
- No Python ever runs on Qt's render thread (no direct connections to frameSwapped and
  friends): the GUI thread waits on that thread in show / hide / grab while holding the
  GIL, so a Python slot there would deadlock.
"""

from __future__ import annotations

import ctypes
import logging
import os
import time
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QRect, QRectF, QUrl, Qt, Signal
from PySide6.QtGui import QCursor, QGuiApplication, QRegion, QScreen
from PySide6.QtQuick import QQuickView

from ..bus import Bus
from ..events import (
    AlwaysListeningChanged,
    Asking,
    CommandFinished,
    ConfigChanged,
    DictatedText,
    FinalTranscript,
    Gated,
    IslandAction,
    JobChip,
    MicChanged,
    MicLevel,
    PartialTranscript,
    PinToggled,
    SpeechReady,
    StateChanged,
    StepsChanged,
)
from .bridge import IslandBridge
from .view import View, reduce

_log = logging.getLogger(__name__)

QML = Path(__file__).with_name("qml") / "Island.qml"
WIDTH, HEIGHT = 1040, 220  # logical px: the expanded pill (≤ 560 × 160) plus chips beside it
MASK_MARGIN = 6  # logical px around the clickable content
REACH = 60  # logical px: a dragged pill's centre stays at least this far inside its screen
_EVENTS = (
    StateChanged, PartialTranscript, FinalTranscript, DictatedText, Gated, CommandFinished, StepsChanged,
    Asking, JobChip, MicChanged, SpeechReady, PinToggled, AlwaysListeningChanged, ConfigChanged,
)
_WDA_EXCLUDEFROMCAPTURE = 0x11
_DWMWA_TRANSITIONS_FORCEDISABLED = 3

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
_user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
_dwmapi = ctypes.WinDLL("dwmapi")
_dwmapi.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
_dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long


def top_center(area: tuple[int, int, int, int], size: tuple[int, int]) -> tuple[int, int, int, int]:
    """(x, y, w, h) of a window of ``size`` at the top centre of ``area`` (x, y, w, h); never wider than it."""
    ax, ay, aw, _ah = area
    width = min(size[0], aw)
    return ax + (aw - width) // 2, ay, width, size[1]


def clamp_home(position: tuple[int, int], area: tuple[int, int, int, int], size: tuple[int, int]) -> tuple[int, int]:
    """Where a dragged window may rest in ``area``: the pill (top centre) stays reachable, and the
    whole window height fits below it so expanded states never run off the bottom."""
    x, y = position
    ax, ay, aw, ah = area
    centre = min(max(x + size[0] // 2, ax + REACH), ax + aw - REACH)
    return centre - size[0] // 2, min(max(y, ay), ay + ah - size[1])


def capturable() -> bool:
    """NOVA_CAPTURABLE=1: let screenshots see the island (for checking layouts)."""
    return os.environ.get("NOVA_CAPTURABLE", "").strip() == "1"


class Island(QObject):
    """Owns the window and the bridge; fed from the bus. Create and use on the Qt GUI thread."""

    _inbox = Signal(object)  # bus events, hopped onto the GUI thread
    _levels = Signal(float)

    def __init__(self, bus: Bus, *, always: bool = True, warm: bool = True) -> None:
        super().__init__()
        self._bus = bus
        self._model = View(always=always, file_always=always)
        self._home: QPoint | None = None  # where he dragged it (this run only); None = top centre
        self._shown = False
        self._interactive = False
        self._hit: tuple[list[QRectF], bool] = ([], True)
        self._wants_levels = False  # read on the speech thread: a plain bool
        self.shown_at: float | None = None  # perf_counter of the last show (for `nova island --demo` timings)
        self._bridge = IslandBridge(self._act, self._collapsed, self._hit_area, self._dropped, self._reset_home)

        os.environ.setdefault("QSG_RENDER_LOOP", "basic")  # read when the first Qt Quick window is made
        view = QQuickView()
        self._view = view  # QML reports its hit area while it loads
        view.setTitle("Nova")
        view.setColor(Qt.GlobalColor.transparent)
        view.setFlags(
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint
            | Qt.WindowType.WindowTransparentForInput
        )
        view.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
        self._bridge.apply(self._model)
        view.setInitialProperties({"island": self._bridge})
        view.setSource(QUrl.fromLocalFile(str(QML)))
        if view.status() != QQuickView.Status.Ready:
            errors = "; ".join(error.toString() for error in view.errors())
            raise RuntimeError(f"the island's QML didn't load: {errors}")
        self._place(self._screen_under_mouse())
        view.create()
        self._hwnd = int(view.winId())
        off = wintypes.BOOL(True)  # Windows' own show / hide fade would fight the island's drop-down
        _dwmapi.DwmSetWindowAttribute(self._hwnd, _DWMWA_TRANSITIONS_FORCEDISABLED, ctypes.byref(off), ctypes.sizeof(off))
        if capturable():
            _log.info("island: NOVA_CAPTURABLE=1, screenshots will see it")
        elif not _user32.SetWindowDisplayAffinity(self._hwnd, _WDA_EXCLUDEFROMCAPTURE):
            _log.warning("island: couldn't exclude it from screen capture (error %d)", ctypes.get_last_error())

        self._inbox.connect(self._on_event)
        self._levels.connect(self._bridge.set_level)
        self._unsubscribe = [bus.subscribe(kind, self._inbox.emit) for kind in _EVENTS]
        self._unsubscribe.append(bus.subscribe(MicLevel, self._on_level))
        app = QGuiApplication.instance()
        app.screenAdded.connect(self._watch_screen)
        app.screenAdded.connect(self._screens_changed)
        app.screenRemoved.connect(self._screens_changed)
        app.primaryScreenChanged.connect(self._screens_changed)
        for screen in app.screens():
            self._watch_screen(screen)
        if warm:
            self._warm()

    # --- any thread ----------------------------------------------------------------------------

    def _on_level(self, event: MicLevel) -> None:
        if self._wants_levels:
            self._levels.emit(event.level)

    # --- GUI thread ----------------------------------------------------------------------------

    @property
    def model(self) -> View:
        return self._model

    @property
    def window(self) -> QQuickView:
        return self._view

    @property
    def bridge(self) -> IslandBridge:
        return self._bridge

    def close(self) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe = []
        self._view.hide()
        self._view.close()

    def _on_event(self, event: object) -> None:
        model = reduce(self._model, event)
        if model != self._model:
            self._set(model)

    def _set(self, model: View) -> None:
        self._model = model
        self._wants_levels = model.state == "listening"
        if not self._wants_levels:
            self._bridge.set_level(0.0)  # the next session's waveform starts flat
        if model.visible and not self._shown:
            self._show()
        self._bridge.apply(model)
        self._apply_input()

    def _show(self) -> None:
        self._go_home()
        self._view.show()  # a tool window that doesn't accept focus: shown without activation
        self._shown = True
        self.shown_at = time.perf_counter()

    def _collapsed(self) -> None:
        if not self._model.visible and self._shown:
            self._view.hide()
            self._shown = False

    def _warm(self) -> None:
        def first_frame() -> None:
            if not self._model.visible:
                self._view.hide()
                self._shown = False
            _log.info("island ready (graphics warmed up)")

        once = Qt.ConnectionType(Qt.ConnectionType.QueuedConnection.value | Qt.ConnectionType.SingleShotConnection.value)
        self._view.frameSwapped.connect(first_frame, once)  # emitted on the render thread: run it on this one, once
        self._view.show()  # nothing visible yet: the pill waits above the top edge, clicks pass through
        self._shown = True

    # input: click-through, or clicks only on the pill and chips --------------------------------

    def _hit_area(self, rects: list[QRectF], settled: bool) -> None:
        self._hit = (rects, settled)
        self._apply_input()

    def _apply_input(self) -> None:
        interactive = self._model.interactive and self._shown
        if interactive != self._interactive:
            self._interactive = interactive
            flags = self._view.flags()
            transparent = Qt.WindowType.WindowTransparentForInput
            self._view.setFlags(flags & ~transparent if interactive else flags | transparent)
        rects, settled = self._hit
        region = QRegion()
        if interactive and settled:
            for rect in rects:
                region = region.united(rect.toAlignedRect().adjusted(-MASK_MARGIN, -MASK_MARGIN, MASK_MARGIN, MASK_MARGIN))
        if region != self._view.mask():
            self._view.setMask(region)  # empty = no mask: the whole window (only while something moves)

    def _act(self, what: str, index: int, chip: str) -> None:
        self._bus.publish(IslandAction(what, time.perf_counter(), index=index, chip=chip, via="click"))

    # placement ----------------------------------------------------------------------------------

    @staticmethod
    def _screen_under_mouse() -> QScreen:
        return QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()

    def _place(self, screen: QScreen) -> None:
        area = screen.availableGeometry()
        x, y, width, height = top_center((area.x(), area.y(), area.width(), area.height()), (WIDTH, HEIGHT))
        if self._view.screen() is not screen:
            self._view.setScreen(screen)
        self._view.setGeometry(QRect(x, y, width, height))

    def _go_home(self) -> None:
        """To where he dragged it (kept on a screen that still exists), else top centre under the mouse."""
        home = self._home
        screen = QGuiApplication.screenAt(home + QPoint(WIDTH // 2, 20)) if home is not None else None
        if home is None or screen is None:
            self._home = None
            self._place(self._screen_under_mouse())
            return
        area = screen.availableGeometry()
        x, y = clamp_home((home.x(), home.y()), (area.x(), area.y(), area.width(), area.height()), (WIDTH, HEIGHT))
        if self._view.screen() is not screen:
            self._view.setScreen(screen)
        self._view.setGeometry(QRect(x, y, self._view.width(), HEIGHT))

    def _dropped(self) -> None:
        """QML moved the window while he dragged the pill; where he let go is home (kept in reach)."""
        self._home = self._view.position()
        self._go_home()
        self._home = self._view.position()
        _log.info("island moved to (%d, %d)", self._home.x(), self._home.y())

    def _reset_home(self) -> None:
        self._home = None
        if self._shown:
            screen = self._view.screen()
            self._place(screen if screen in QGuiApplication.screens() else self._screen_under_mouse())

    def _watch_screen(self, screen: QScreen) -> None:
        for signal in (screen.geometryChanged, screen.availableGeometryChanged, screen.logicalDotsPerInchChanged):
            signal.connect(self._screens_changed)

    def _screens_changed(self, *_args: object) -> None:
        if not self._shown:  # hidden: placed again on the next show anyway
            return
        if self._home is not None:
            self._go_home()
            return
        screen = self._view.screen()
        self._place(screen if screen in QGuiApplication.screens() else self._screen_under_mouse())
