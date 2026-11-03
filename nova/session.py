"""Workstation lock / unlock via WTS session notifications (SPEC §4: mic closed while locked).

A hidden message-only window on its own thread receives WM_WTSSESSION_CHANGE.
``on_change(True)`` = locked, ``on_change(False)`` = unlocked; it runs on that
thread, so keep it quick (SpeechWorker.set_locked just flips a flag).
"""

from __future__ import annotations

import ctypes
import logging
import threading
from collections.abc import Callable
from ctypes import wintypes

_log = logging.getLogger(__name__)

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_WTSSESSION_CHANGE = 0x02B1
WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8
_NOTIFY_FOR_THIS_SESSION = 0
_HWND_MESSAGE = -3

LRESULT = ctypes.c_ssize_t
_WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE
_user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.DefWindowProcW.restype = LRESULT
_user32.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
_user32.RegisterClassExW.restype = wintypes.ATOM
_user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
_user32.UnregisterClassW.restype = wintypes.BOOL
_user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.DestroyWindow.argtypes = [wintypes.HWND]
_user32.DestroyWindow.restype = wintypes.BOOL
_user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
_user32.GetMessageW.restype = wintypes.BOOL
_user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.DispatchMessageW.restype = LRESULT
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.PostMessageW.restype = wintypes.BOOL
_user32.PostQuitMessage.argtypes = [ctypes.c_int]
_wts.WTSRegisterSessionNotification.argtypes = [wintypes.HWND, wintypes.DWORD]
_wts.WTSRegisterSessionNotification.restype = wintypes.BOOL
_wts.WTSUnRegisterSessionNotification.argtypes = [wintypes.HWND]
_wts.WTSUnRegisterSessionNotification.restype = wintypes.BOOL


class LockWatcher:
    def __init__(self, on_change: Callable[[bool], None]) -> None:
        self._on_change = on_change
        self._hwnd: int | None = None
        self._ready = threading.Event()
        self._proc = _WNDPROC(self._wndproc)  # must outlive the window
        self._class = f"NovaLockWatcher{id(self):x}"
        self._thread = threading.Thread(target=self._run, name="nova-lock-watch", daemon=True)
        self.error: str | None = None

    def start(self) -> bool:
        """Start watching. False (with ``error``) if Windows refused; Nova then just can't see locks."""
        self._thread.start()
        self._ready.wait(5)
        return self._hwnd is not None

    def close(self) -> None:
        if self._hwnd is not None:
            _user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
            self._thread.join(2)

    def simulate(self, locked: bool) -> None:
        """Deliver a fake lock/unlock through the real window (for checks without locking the PC)."""
        if self._hwnd is not None:
            _user32.PostMessageW(self._hwnd, WM_WTSSESSION_CHANGE, WTS_SESSION_LOCK if locked else WTS_SESSION_UNLOCK, 0)

    def _wndproc(self, hwnd, message, wparam, lparam):
        if message == WM_WTSSESSION_CHANGE and wparam in (WTS_SESSION_LOCK, WTS_SESSION_UNLOCK):
            locked = wparam == WTS_SESSION_LOCK
            _log.info("screen %s", "locked" if locked else "unlocked")
            try:
                self._on_change(locked)
            except Exception:  # noqa: BLE001 - never let a handler break the message loop
                _log.exception("lock handler failed")
            return 0
        if message == WM_CLOSE:
            _wts.WTSUnRegisterSessionNotification(hwnd)
            _user32.DestroyWindow(hwnd)
            return 0
        if message == WM_DESTROY:
            _user32.PostQuitMessage(0)
            return 0
        return _user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _run(self) -> None:
        instance = _kernel32.GetModuleHandleW(None)
        wc = _WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
        wc.lpfnWndProc = self._proc
        wc.hInstance = instance
        wc.lpszClassName = self._class
        try:
            if not _user32.RegisterClassExW(ctypes.byref(wc)):
                raise ctypes.WinError(ctypes.get_last_error())
            hwnd = _user32.CreateWindowExW(0, self._class, "Nova lock watcher", 0, 0, 0, 0, 0, _HWND_MESSAGE, None, instance, None)
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            if not _wts.WTSRegisterSessionNotification(hwnd, _NOTIFY_FOR_THIS_SESSION):
                error = ctypes.WinError(ctypes.get_last_error())
                _user32.DestroyWindow(hwnd)
                raise error
        except OSError as error:
            self.error = f"can't watch for screen lock ({error})"
            _log.warning("%s", self.error)
            self._ready.set()
            return
        self._hwnd = hwnd
        self._ready.set()
        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))
        self._hwnd = None
        _user32.UnregisterClassW(self._class, instance)
