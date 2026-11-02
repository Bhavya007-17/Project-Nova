"""Tray icon + menu (PySide6). Lives on the Qt GUI thread and never blocks it."""

from __future__ import annotations

import ctypes
import logging
import os
import signal
import subprocess
import sys
import threading
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import paths
from .bus import Bus
from .config import ConfigStore
from .events import ConfigChanged, ConfigInvalid, PowerChanged

_log = logging.getLogger(__name__)

_ICON_SIZES = (16, 20, 24, 32, 40, 48, 64)  # crisp at 100-200 % scaling


def make_icon(on: bool) -> QIcon:
    """A small black island pill with a status dot: blue when on, grey when off."""
    icon = QIcon()
    for size in _ICON_SIZES:
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        height = size * 0.56
        pill = QRectF(size * 0.04, (size - height) / 2, size * 0.92, height)
        painter.setPen(QPen(QColor(255, 255, 255, 210 if on else 120), max(1.0, size / 16)))
        painter.setBrush(QColor("#000000"))
        painter.drawRoundedRect(pill, height / 2, height / 2)
        dot = height * 0.42
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#0A84FF" if on else "#8E8E93"))
        painter.drawEllipse(QRectF(pill.right() - height / 2 - dot / 2, pill.center().y() - dot / 2, dot, dot))
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def _open_in_background(path: Path) -> None:
    """Open a file or folder with its default app; .yaml has no default on a fresh Windows, so fall back to Notepad."""

    def work() -> None:
        try:
            os.startfile(path)  # noqa: S606 - fixed local path
        except OSError:
            try:
                subprocess.Popen(["notepad.exe", str(path)])  # noqa: S603, S607
            except OSError:
                _log.exception("cannot open %s", path)

    threading.Thread(target=work, name="nova-open", daemon=True).start()


class _Bridge(QObject):
    """Bus handlers and console handlers run on other threads; these signals hop onto the GUI thread."""

    event = Signal(object)
    quit = Signal()


_HandlerRoutine = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
_CTRL_CLOSE_EVENT = 2


def _quit_on_console_close(request_quit, closed: threading.Event):
    """Closing the console window raises no Python signal: Windows calls this on its own thread
    and ends the process ~5 s later. Ask Qt to quit and wait until the tray icon is gone."""

    def handler(kind: int) -> bool:
        if kind != _CTRL_CLOSE_EVENT:
            return False  # Ctrl+C / Ctrl+Break go to the Python signal handlers
        request_quit()
        closed.wait(4)
        return True

    callback = _HandlerRoutine(handler)
    ctypes.windll.kernel32.SetConsoleCtrlHandler(callback, True)
    return callback


_console_handler = None  # stays registered with Windows for the life of the process; must not be freed


class Tray(QObject):
    def __init__(self, app: QApplication, bus: Bus, store: ConfigStore) -> None:
        super().__init__()
        self._bus = bus
        self._store = store
        self._icons = {True: make_icon(True), False: make_icon(False)}
        self._config_broken = False

        self._menu = QMenu()
        self._power = QAction("Nova is on", self._menu, checkable=True, checked=True)
        self._power.toggled.connect(self._set_power)
        self._menu.addAction(self._power)
        self._menu.addSeparator()
        self._add("Open settings", lambda: _open_in_background(self._store.path))
        self._add("Open logs", self._open_logs)
        self._menu.addSeparator()
        self._add("Quit Nova", app.quit)

        self._icon = QSystemTrayIcon(self._icons[True])
        self._icon.setToolTip("Nova - on")
        self._icon.setContextMenu(self._menu)

        self._bridge = _Bridge()
        self._bridge.event.connect(self._on_event)
        self._unsubscribe = [
            bus.subscribe(ConfigChanged, self._bridge.event.emit),
            bus.subscribe(ConfigInvalid, self._bridge.event.emit),
        ]
        self._icon.show()
        if store.error:  # a broken file at startup was reported before we subscribed
            self._on_event(ConfigInvalid(str(store.path), store.error, using_defaults=not store.loaded_once))

    def _add(self, text: str, slot) -> None:
        action = QAction(text, self._menu)
        action.triggered.connect(slot)
        self._menu.addAction(action)

    def _set_power(self, on: bool) -> None:
        self._icon.setIcon(self._icons[on])
        self._icon.setToolTip(f"Nova - {'on' if on else 'off'}")
        self._power.setText(f"Nova is {'on' if on else 'off'}")
        _log.info("power %s", "on" if on else "off")
        self._bus.publish(PowerChanged(on))

    def _open_logs(self) -> None:
        paths.LOGS.mkdir(parents=True, exist_ok=True)
        _open_in_background(paths.LOGS)

    def _on_event(self, event: object) -> None:
        if isinstance(event, ConfigInvalid):
            self._config_broken = True
            using = "the default settings until it's fixed" if event.using_defaults else "the last good settings"
            self._icon.showMessage(
                "Nova settings", f"{event.error}\nUsing {using}.",
                QSystemTrayIcon.MessageIcon.Warning, 8000,
            )
        elif isinstance(event, ConfigChanged):
            if self._config_broken:
                self._config_broken = False
                self._icon.showMessage("Nova settings", "Settings fixed and applied.",
                                       QSystemTrayIcon.MessageIcon.Information, 3000)
            _log.info("settings applied")

    def close(self) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._icon.hide()  # otherwise a ghost icon stays until the mouse passes over it


def run_tray(bus: Bus, store: ConfigStore) -> int:
    app = QApplication.instance() or QApplication(["nova"])
    app.setApplicationName("Nova")
    app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        _log.error("no system tray available")
        return 1
    global _console_handler
    tray = Tray(app, bus, store)
    keepalive = None
    closed = threading.Event()
    stopper = _Bridge()
    stopper.quit.connect(app.quit, Qt.ConnectionType.QueuedConnection)
    if sys.stderr is not None:
        # Console run: Ctrl+C / Ctrl+Break / closing the window quit cleanly (no ghost icon).
        # Python only sees signals when it gets to run, so a light timer hands it control 4x a second.
        for sig in (signal.SIGINT, signal.SIGBREAK):
            signal.signal(sig, lambda *_: app.quit())
        _console_handler = _quit_on_console_close(stopper.quit.emit, closed)
        keepalive = QTimer()
        keepalive.timeout.connect(lambda: None)
        keepalive.start(250)
    _log.info("tray ready")
    try:
        return app.exec()
    finally:
        if keepalive is not None:
            keepalive.stop()
        tray.close()
        closed.set()
