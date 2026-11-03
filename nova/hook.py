"""The low-level keyboard hook (WH_KEYBOARD_LL) on its own thread with a message loop.

SPEC §3/§5/§20. ``on_key`` runs inside the hook callback on this thread and must
return fast (budget < 10 ms; typical ~20 µs): no I/O, no waiting. Returning True
swallows the key.

Windows silently removes a hook whose callback misses LowLevelHooksTimeout, and
says nothing. Two checks catch that:
- the same thread registers for raw keyboard input (RIDEV_INPUTSINK), which no
  timeout can remove. Raw input sees every key the hook lets through (swallowed
  keys never reach it); when it has counted 3+ keys more than the hook passed,
  and still has 300 ms later (the two can arrive in either order), the hook is
  reinstalled;
- a callback that runs late (event older than ~150 ms) reinstalls it too.
"""

from __future__ import annotations

import collections
import ctypes
import logging
import threading
import time
from collections.abc import Callable, Sequence
from ctypes import wintypes

from .keys import NOVA_MARK, Key

_log = logging.getLogger(__name__)

WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_INPUT = 0x00FF
WM_TIMER = 0x0113
_CHECK_TIMER = 1
_WM_REHOOK = 0x8000 + 1  # WM_APP + 1, posted to the thread
_WM_SEND = 0x8000 + 2  # WM_APP + 2: inject queued keys, outside the callback
LLKHF_UP = 0x80
RIDEV_REMOVE = 0x00000001
RIDEV_INPUTSINK = 0x00000100
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
_HWND_MESSAGE = -3

LATE_MS = 150  # a callback this late may already have cost us the hook (default timeout is ~300 ms)
MISSED_KEYS = 3  # raw input counted this many more keys than the hook passed...
CHECK_MS = 300  # ...and still does this much later: the hook is gone
REHOOK_GAP_S = 2.0  # never reinstall more often than this

LRESULT = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t
_HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
_WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wintypes.USHORT),
        ("usUsage", wintypes.USHORT),
        ("dwFlags", wintypes.DWORD),
        ("hwndTarget", wintypes.HWND),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


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
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
_user32.SetWindowsHookExW.restype = wintypes.HHOOK
_user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
_user32.UnhookWindowsHookEx.restype = wintypes.BOOL
_user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
_user32.CallNextHookEx.restype = LRESULT
_user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
_user32.SendInput.restype = wintypes.UINT
_user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE), wintypes.UINT, wintypes.UINT]
_user32.RegisterRawInputDevices.restype = wintypes.BOOL
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
_user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.DispatchMessageW.restype = LRESULT
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.PostMessageW.restype = wintypes.BOOL
_user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.PostThreadMessageW.restype = wintypes.BOOL
_user32.PostQuitMessage.argtypes = [ctypes.c_int]
_user32.SetTimer.argtypes = [wintypes.HWND, ULONG_PTR, wintypes.UINT, ctypes.c_void_p]
_user32.SetTimer.restype = ULONG_PTR
_user32.KillTimer.argtypes = [wintypes.HWND, ULONG_PTR]
_user32.KillTimer.restype = wintypes.BOOL
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD
_kernel32.GetTickCount.restype = wintypes.DWORD

assert ctypes.sizeof(INPUT) == 40, "SendInput needs the x64 INPUT layout"


def send_keys(strokes: Sequence[tuple[int, bool]]) -> int:
    """Inject (vk, down) strokes marked as Nova's own. Returns how many Windows accepted."""
    events = (INPUT * len(strokes))()
    for item, (vk, down) in zip(events, strokes, strict=True):
        item.type = INPUT_KEYBOARD
        item.u.ki = KEYBDINPUT(vk, 0, 0 if down else KEYEVENTF_KEYUP, 0, NOVA_MARK)
    return int(_user32.SendInput(len(strokes), events, ctypes.sizeof(INPUT)))


class HookStats:
    """Counters, written on the hook thread only."""

    def __init__(self) -> None:
        self.calls = 0
        self.total_ms = 0.0
        self.max_ms = 0.0
        self.over_5ms = 0
        self.late = 0  # callbacks that ran > LATE_MS after the key event
        self.raw = 0  # keys seen by raw input
        self.reinstalls = 0
        self.errors = 0

    @property
    def mean_ms(self) -> float:
        return self.total_ms / self.calls if self.calls else 0.0


class KeyHook:
    """Installs the hook on a "nova-keys" thread. ``on_key(Key) -> bool`` (True = swallow) runs on it.

    ``on_reinstall`` also runs on that thread, after the hook was put back
    (keys may have been missed while it was gone).
    """

    def __init__(
        self,
        on_key: Callable[[Key], bool],
        *,
        on_reinstall: Callable[[], None] | None = None,
        name: str = "nova-keys",
    ) -> None:
        self._on_key = on_key
        self._on_reinstall = on_reinstall
        self._proc = _HOOKPROC(self._callback)  # must outlive the hook
        self._wndproc = _WNDPROC(self._window_proc)
        self._class = f"NovaKeyHook{id(self):x}"
        self._hook: int | None = None
        self._hwnd: int | None = None
        self._tid = 0
        self._passed = 0  # keys the hook let through: raw input should count the same
        self._checking = False
        self._last_rehook = 0.0
        self._outbox: collections.deque[Sequence[tuple[int, bool]]] = collections.deque()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self.stats = HookStats()
        self.error: str | None = None

    def start(self, timeout: float = 5.0) -> bool:
        """Install the hook. False (with ``error``) if Windows refused."""
        self._thread.start()
        self._ready.wait(timeout)
        return self._hook is not None

    def close(self) -> None:
        if self._hwnd is not None:
            _user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(2)

    @property
    def alive(self) -> bool:
        return self._hook is not None and self._thread.is_alive()

    def post(self, strokes: Sequence[tuple[int, bool]]) -> None:
        """Inject keys (marked as Nova's) on the hook thread right after the current callback.

        Strokes posted from callbacks go out in order, before the hook sees any later key.
        """
        self._outbox.append(tuple(strokes))
        _user32.PostThreadMessageW(self._tid, _WM_SEND, 0, 0)

    # --- hook thread -----------------------------------------------------------------------------

    def _callback(self, code: int, wparam: int, lparam: int) -> int:
        if code == HC_ACTION:
            began = time.perf_counter()
            stats = self.stats
            swallow = False
            try:
                raw = KBDLLHOOKSTRUCT.from_address(lparam)
                key = Key(raw.vkCode, not raw.flags & LLKHF_UP, began, raw.scanCode, raw.flags, raw.time, raw.dwExtraInfo)
                swallow = bool(self._on_key(key))
                if not swallow:
                    self._passed += 1
                late = (_kernel32.GetTickCount() - raw.time) & 0xFFFFFFFF
                if LATE_MS < late < 60_000:
                    stats.late += 1
                    _user32.PostThreadMessageW(self._tid, _WM_REHOOK, 0, 0)
            except Exception:  # noqa: BLE001 - never let an error escape into Windows
                stats.errors += 1
                if stats.errors == 1:
                    _log.exception("key hook callback failed")
            ms = (time.perf_counter() - began) * 1000
            stats.calls += 1
            stats.total_ms += ms
            if ms > stats.max_ms:
                stats.max_ms = ms
            if ms > 5:
                stats.over_5ms += 1
            if swallow:
                return 1
        return _user32.CallNextHookEx(None, code, wparam, lparam)

    def _window_proc(self, hwnd, message, wparam, lparam):
        if message == WM_INPUT:
            self.stats.raw += 1
            if self.stats.raw - self._passed >= MISSED_KEYS and not self._checking:
                self._checking = True
                _user32.SetTimer(hwnd, _CHECK_TIMER, CHECK_MS, None)
            return _user32.DefWindowProcW(hwnd, message, wparam, lparam)  # lets Windows free the raw data
        if message == WM_TIMER and wparam == _CHECK_TIMER:
            _user32.KillTimer(hwnd, _CHECK_TIMER)
            self._checking = False
            if self.stats.raw - self._passed >= MISSED_KEYS:
                self._rehook("raw input saw keys the hook missed")
            return 0
        if message == WM_CLOSE:
            self._shutdown(hwnd)
            return 0
        if message == WM_DESTROY:
            _user32.PostQuitMessage(0)
            return 0
        return _user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _install(self) -> int | None:
        hook = _user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, _kernel32.GetModuleHandleW(None), 0)
        return hook or None

    def _rehook(self, why: str) -> None:
        now = time.monotonic()
        if now - self._last_rehook < REHOOK_GAP_S:
            return
        self._last_rehook = now
        new = self._install()
        if new is None:
            _log.error("keyboard hook: reinstall failed (%s)", ctypes.WinError(ctypes.get_last_error()))
            return
        old, self._hook = self._hook, new
        if old and old != new:  # a hook Windows removed may hand its handle value to the new one
            _user32.UnhookWindowsHookEx(old)
        self._passed = self.stats.raw
        self.stats.reinstalls += 1
        _log.warning("keyboard hook reinstalled: %s", why)
        if self._on_reinstall is not None:
            try:
                self._on_reinstall()
            except Exception:  # noqa: BLE001
                _log.exception("key hook reinstall handler failed")

    def _shutdown(self, hwnd: int) -> None:
        if self._hook:
            _user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
        device = RAWINPUTDEVICE(1, 6, RIDEV_REMOVE, None)
        _user32.RegisterRawInputDevices(ctypes.byref(device), 1, ctypes.sizeof(RAWINPUTDEVICE))
        _user32.DestroyWindow(hwnd)

    def _run(self) -> None:
        self._tid = _kernel32.GetCurrentThreadId()
        instance = _kernel32.GetModuleHandleW(None)
        wc = _WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = instance
        wc.lpszClassName = self._class
        try:
            if not _user32.RegisterClassExW(ctypes.byref(wc)):
                raise ctypes.WinError(ctypes.get_last_error())
            hwnd = _user32.CreateWindowExW(0, self._class, "Nova keys", 0, 0, 0, 0, 0, _HWND_MESSAGE, None, instance, None)
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self._hwnd = hwnd
            device = RAWINPUTDEVICE(1, 6, RIDEV_INPUTSINK, hwnd)  # generic desktop / keyboard
            if not _user32.RegisterRawInputDevices(ctypes.byref(device), 1, ctypes.sizeof(RAWINPUTDEVICE)):
                _log.warning("keyboard hook: no raw-input watchdog (%s)", ctypes.WinError(ctypes.get_last_error()))
            hook = self._install()
            if hook is None:
                raise ctypes.WinError(ctypes.get_last_error())
            self._hook = hook
        except OSError as error:
            self.error = f"can't watch the keyboard ({error})"
            _log.error("%s", self.error)
            if self._hwnd is not None:
                _user32.DestroyWindow(self._hwnd)
                self._hwnd = None
            _user32.UnregisterClassW(self._class, instance)
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.hWnd is None and msg.message == _WM_SEND:
                while self._outbox:
                    send_keys(self._outbox.popleft())
                continue
            if msg.hWnd is None and msg.message == _WM_REHOOK:
                self._rehook("a key event reached the hook late")
                continue
            _user32.DispatchMessageW(ctypes.byref(msg))
        self._hwnd = None
        _user32.UnregisterClassW(self._class, instance)


class TriggerHook:
    """``KeyHook`` + ``TriggerKeys``: swallows the triggers, injects the mask key, forwards trigger events.

    ``sink`` runs inside the hook callback: it must only hand the event over (e.g. queue.put).
    """

    def __init__(self, keys, sink: Callable[[object], None]) -> None:
        self.keys = keys
        self._sink = sink
        self.hook = KeyHook(self._on_key, on_reinstall=self._reinstalled)

    def start(self) -> bool:
        return self.hook.start()

    def close(self) -> None:
        self.hook.close()

    @property
    def error(self) -> str | None:
        return self.hook.error

    def _on_key(self, key: Key) -> bool:
        verdict = self.keys.on_key(key)
        if verdict.replay:
            self.hook.post(verdict.replay)  # mask key + the swallowed Win / Alt release
        if verdict.event is not None:
            self._sink(verdict.event)
        return verdict.swallow

    def _reinstalled(self) -> None:
        for event in self.keys.reset(time.perf_counter()):
            self._sink(event)
