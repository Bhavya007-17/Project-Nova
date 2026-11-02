"""The Nova process: single instance, logs, settings watcher, bus, tray."""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
from ctypes import wintypes

from . import __version__, paths
from .bus import Bus
from .config import ConfigStore
from .log import setup_app_log

MUTEX_NAME = "Local\\Nova.VoiceLayer"
_ERROR_ACCESS_DENIED = 5
_ERROR_ALREADY_EXISTS = 183
_MB_ICONINFORMATION = 0x40

_log = logging.getLogger(__name__)

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


def _claim_single_instance() -> int | None:
    """Named mutex: returns its handle, or None if another Nova already holds it."""
    handle = _kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        error = ctypes.get_last_error()
        if error == _ERROR_ACCESS_DENIED:  # held by a Nova started as administrator
            return None
        raise ctypes.WinError(error)
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(handle)
        return None
    return handle


def _log_uncaught() -> None:
    # Under pythonw there is no console, so anything uncaught must land in logs/nova.log.
    sys.excepthook = lambda kind, error, tb: _log.critical("uncaught error", exc_info=(kind, error, tb))
    threading.excepthook = lambda args: _log.critical(
        "uncaught error in thread %s", args.thread.name if args.thread else "?",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


def run() -> int:
    mutex = _claim_single_instance()
    if mutex is None:
        message = "Nova is already running (look for its tray icon)."
        if sys.stdout is None:  # pythonw / novaw: no console to print to
            ctypes.windll.user32.MessageBoxW(None, message, "Nova", _MB_ICONINFORMATION)
        else:
            print(message)
        return 0
    close_log = setup_app_log(paths.LOGS, console=sys.stderr is not None)
    _log_uncaught()
    _log.info("Nova %s starting (pid %d)", __version__, os.getpid())
    bus = Bus()
    store = ConfigStore(paths.SETTINGS, bus)
    store.load()
    stop_watching = store.watch()
    try:
        from .tray import run_tray

        return run_tray(bus, store)
    except Exception:
        _log.exception("Nova crashed")
        raise
    finally:
        stop_watching()
        _log.info("Nova stopped")
        close_log()
        _kernel32.CloseHandle(mutex)
