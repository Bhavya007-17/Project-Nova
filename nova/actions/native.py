"""Win32 plumbing for Nova's hands (ctypes only, no new packages).

Queries never change anything; the few calls that do (activate, show, move, close,
send keys) are separate so the actions can check their target right before. Geometry runs
per-monitor-DPI-aware (``dpi_aware``), so rectangles are physical pixels on every monitor.
Process identities are cached for a minute (one OpenProcess per new process), so a
window list with identities takes a few milliseconds.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from ctypes import POINTER, byref, c_void_p, wintypes
from dataclasses import dataclass
from typing import TypeVar

from ..hook import INPUT, KEYBDINPUT, MOUSEINPUT
from ..keys import NOVA_MARK

T = TypeVar("T")
Rect = tuple[int, int, int, int]  # left, top, right, bottom

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
shell32 = ctypes.WinDLL("shell32")
ole32 = ctypes.WinDLL("ole32")
shcore = ctypes.WinDLL("shcore")
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, POINTER(wintypes.RECT),
                                     wintypes.LPARAM)


def _bind(dll, name: str, restype, *argtypes) -> None:
    fn = getattr(dll, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", wintypes.UINT), ("flags", wintypes.UINT), ("showCmd", wintypes.UINT),
                ("ptMinPosition", wintypes.POINT), ("ptMaxPosition", wintypes.POINT),
                ("rcNormalPosition", wintypes.RECT)]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD), ("szDevice", ctypes.c_wchar * 32)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", wintypes.WORD), ("r1", wintypes.WORD), ("r2", wintypes.WORD), ("r3", wintypes.WORD),
                ("value", c_void_p), ("value2", c_void_p)]


_bind(user32, "EnumWindows", wintypes.BOOL, WNDENUMPROC, wintypes.LPARAM)
_bind(user32, "IsWindow", wintypes.BOOL, wintypes.HWND)
_bind(user32, "IsWindowVisible", wintypes.BOOL, wintypes.HWND)
_bind(user32, "IsIconic", wintypes.BOOL, wintypes.HWND)
_bind(user32, "IsZoomed", wintypes.BOOL, wintypes.HWND)
_bind(user32, "IsHungAppWindow", wintypes.BOOL, wintypes.HWND)
_bind(user32, "GetWindowLongPtrW", ctypes.c_ssize_t, wintypes.HWND, ctypes.c_int)
_bind(user32, "GetWindow", wintypes.HWND, wintypes.HWND, wintypes.UINT)
_bind(user32, "GetAncestor", wintypes.HWND, wintypes.HWND, wintypes.UINT)
_bind(user32, "GetWindowTextLengthW", ctypes.c_int, wintypes.HWND)
_bind(user32, "GetWindowTextW", ctypes.c_int, wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
_bind(user32, "GetClassNameW", ctypes.c_int, wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
_bind(user32, "GetWindowThreadProcessId", wintypes.DWORD, wintypes.HWND, POINTER(wintypes.DWORD))
_bind(user32, "GetForegroundWindow", wintypes.HWND)
_bind(user32, "SetForegroundWindow", wintypes.BOOL, wintypes.HWND)
_bind(user32, "BringWindowToTop", wintypes.BOOL, wintypes.HWND)
_bind(user32, "AttachThreadInput", wintypes.BOOL, wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
_bind(user32, "AllowSetForegroundWindow", wintypes.BOOL, wintypes.DWORD)
_bind(user32, "ShowWindow", wintypes.BOOL, wintypes.HWND, ctypes.c_int)
_bind(user32, "ShowWindowAsync", wintypes.BOOL, wintypes.HWND, ctypes.c_int)
_bind(user32, "GetWindowPlacement", wintypes.BOOL, wintypes.HWND, POINTER(WINDOWPLACEMENT))
_bind(user32, "SetWindowPlacement", wintypes.BOOL, wintypes.HWND, POINTER(WINDOWPLACEMENT))
_bind(user32, "GetWindowRect", wintypes.BOOL, wintypes.HWND, POINTER(wintypes.RECT))
_bind(user32, "SetWindowPos", wintypes.BOOL, wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
      ctypes.c_int, wintypes.UINT)
_bind(user32, "PostMessageW", wintypes.BOOL, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_bind(user32, "GetCursorPos", wintypes.BOOL, POINTER(wintypes.POINT))
_bind(user32, "WindowFromPoint", wintypes.HWND, wintypes.POINT)
_bind(user32, "MonitorFromWindow", wintypes.HMONITOR, wintypes.HWND, wintypes.DWORD)
_bind(user32, "MonitorFromPoint", wintypes.HMONITOR, wintypes.POINT, wintypes.DWORD)
_bind(user32, "EnumDisplayMonitors", wintypes.BOOL, wintypes.HDC, POINTER(wintypes.RECT), MONITORENUMPROC,
      wintypes.LPARAM)
_bind(user32, "GetMonitorInfoW", wintypes.BOOL, wintypes.HMONITOR, POINTER(MONITORINFOEXW))
_bind(user32, "SetThreadDpiAwarenessContext", c_void_p, c_void_p)
_bind(user32, "SendInput", wintypes.UINT, wintypes.UINT, POINTER(INPUT), ctypes.c_int)
_bind(user32, "GetAsyncKeyState", ctypes.c_short, ctypes.c_int)
_bind(user32, "OpenInputDesktop", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_bind(user32, "CloseDesktop", wintypes.BOOL, wintypes.HANDLE)
_bind(kernel32, "OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_bind(kernel32, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
_bind(kernel32, "QueryFullProcessImageNameW", wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
      POINTER(wintypes.DWORD))
_bind(kernel32, "GetApplicationUserModelId", ctypes.c_long, wintypes.HANDLE, POINTER(wintypes.UINT), wintypes.LPWSTR)
_bind(kernel32, "GetCurrentThreadId", wintypes.DWORD)
_bind(kernel32, "GetCurrentProcess", wintypes.HANDLE)
_bind(dwmapi, "DwmGetWindowAttribute", ctypes.c_long, wintypes.HWND, wintypes.DWORD, c_void_p, wintypes.DWORD)
_bind(shell32, "SHGetPropertyStoreForWindow", ctypes.c_long, wintypes.HWND, POINTER(GUID), POINTER(c_void_p))
_bind(ole32, "CoInitializeEx", ctypes.c_long, c_void_p, wintypes.DWORD)
_bind(ole32, "CoCreateInstance", ctypes.c_long, POINTER(GUID), c_void_p, wintypes.DWORD, POINTER(GUID),
      POINTER(c_void_p))
_bind(ole32, "CLSIDFromString", ctypes.c_long, wintypes.LPCWSTR, POINTER(GUID))
_bind(ole32, "PropVariantClear", ctypes.c_long, POINTER(PROPVARIANT))
_bind(shcore, "GetDpiForMonitor", ctypes.c_long, wintypes.HMONITOR, ctypes.c_int, POINTER(wintypes.UINT),
      POINTER(wintypes.UINT))
_bind(advapi32, "OpenProcessToken", wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD, POINTER(wintypes.HANDLE))
_bind(advapi32, "GetTokenInformation", wintypes.BOOL, wintypes.HANDLE, ctypes.c_int, c_void_p, wintypes.DWORD,
      POINTER(wintypes.DWORD))
_bind(advapi32, "GetSidSubAuthorityCount", POINTER(ctypes.c_ubyte), c_void_p)
_bind(advapi32, "GetSidSubAuthority", POINTER(wintypes.DWORD), c_void_p, wintypes.DWORD)

SW_SHOWNORMAL, SW_SHOWMINIMIZED, SW_SHOWMAXIMIZED, SW_MINIMIZE, SW_RESTORE = 1, 2, 3, 6, 9
WM_CLOSE = 0x0010
GW_OWNER = 4
GA_ROOT = 2
_WS_EX_TOOLWINDOW = 0x80
_WS_EX_NOACTIVATE = 0x08000000
_GWL_EXSTYLE = -20
_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_DWMWA_CLOAKED = 14
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PER_MONITOR_AWARE_V2 = c_void_p(-4)
_SWP_NOZORDER, _SWP_NOACTIVATE, _SWP_ASYNCWINDOWPOS = 0x0004, 0x0010, 0x4000
_VK_MASK = 0xE8  # unassigned key: sent so Windows counts Nova as the last input source (foreground rights)
_ASFW_ANY = 0xFFFFFFFF
_KEYEVENTF_EXTENDEDKEY, _KEYEVENTF_KEYUP, _KEYEVENTF_UNICODE = 0x1, 0x2, 0x4
_INPUT_MOUSE, _INPUT_KEYBOARD = 0, 1
_MOUSEEVENTF_WHEEL = 0x0800
_EXTENDED_VKS = frozenset({0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C, 0x6F, 0x90, 0xA3,
                           0xA5, 0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3})


class ActionError(Exception):
    """An action couldn't be done; the message is plain words for the island."""


def wait_for(predicate: Callable[[], T], timeout: float, interval: float = 0.01) -> T | None:
    """Poll ``predicate`` until it returns something truthy or ``timeout`` seconds pass."""
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
    """Physical pixels on every monitor for the calls inside (this thread only)."""
    previous = user32.SetThreadDpiAwarenessContext(_PER_MONITOR_AWARE_V2)
    try:
        yield
    finally:
        if previous:
            user32.SetThreadDpiAwarenessContext(previous)


# --- COM --------------------------------------------------------------------------------------------------------

_com = threading.local()


def com_init() -> None:
    """COM on this thread (multithreaded apartment), once."""
    if not getattr(_com, "ready", False):
        ole32.CoInitializeEx(None, 0x0)  # COINIT_MULTITHREADED; RPC_E_CHANGED_MODE is fine too
        _com.ready = True


def guid(text: str) -> GUID:
    value = GUID()
    if ole32.CLSIDFromString(text, byref(value)) < 0:
        raise ValueError(f"bad GUID {text}")
    return value


def com_call(pointer: c_void_p, index: int, *args: object, types: Sequence[object] = ()) -> int:
    """Call vtable slot ``index``; returns the HRESULT (negative = failed)."""
    vtable = ctypes.cast(pointer, POINTER(POINTER(c_void_p)))[0]
    method = ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, *types)(vtable[index])
    return int(method(pointer, *args))


def com_release(pointer: c_void_p | None) -> None:
    if pointer:
        vtable = ctypes.cast(pointer, POINTER(POINTER(c_void_p)))[0]
        ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(vtable[2])(pointer)


_IID_IPropertyStore = guid("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")
_PKEY_AppUserModel_ID = PROPERTYKEY(guid("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"), 5)


# --- windows ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Win:
    hwnd: int
    pid: int
    tid: int
    title: str
    cls: str
    exe: str  # full path of the process image ("" if Windows wouldn't say)
    aumids: tuple[str, ...]  # process and window AppUserModelIDs
    minimized: bool = False
    maximized: bool = False
    owner: int = 0  # the window that owns it (dialogs), 0 for app windows

    @property
    def exe_name(self) -> str:
        return os.path.basename(self.exe)


_IDENTITY_TTL = 60.0
_identities: dict[int, tuple[float, str, str]] = {}
_window_ids: dict[int, tuple[float, str]] = {}
_identity_lock = threading.Lock()


def _process_identity(pid: int) -> tuple[str, str]:
    """(image path, AUMID) of a process; cached for a minute."""
    now = time.monotonic()
    with _identity_lock:
        cached = _identities.get(pid)
        if cached and now - cached[0] < _IDENTITY_TTL:
            return cached[1], cached[2]
    exe, aumid = "", ""
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        try:
            size = wintypes.DWORD(1024)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, byref(size)):
                exe = buffer.value
            count = wintypes.UINT(0)
            if kernel32.GetApplicationUserModelId(handle, byref(count), None) == 122 and 0 < count.value < 1024:
                name = ctypes.create_unicode_buffer(count.value)
                if kernel32.GetApplicationUserModelId(handle, byref(count), name) == 0:
                    aumid = name.value
        finally:
            kernel32.CloseHandle(handle)
    with _identity_lock:
        _identities[pid] = (now, exe, aumid)
        if len(_identities) > 512:
            for key in sorted(_identities, key=lambda k: _identities[k][0])[:256]:
                _identities.pop(key, None)
    return exe, aumid


def _window_aumid(hwnd: int) -> str:
    now = time.monotonic()
    with _identity_lock:
        cached = _window_ids.get(hwnd)
        if cached and now - cached[0] < _IDENTITY_TTL:
            return cached[1]
    com_init()
    value = ""
    store = c_void_p()
    if shell32.SHGetPropertyStoreForWindow(hwnd, byref(_IID_IPropertyStore), byref(store)) >= 0 and store:
        variant = PROPVARIANT()
        try:
            hr = com_call(store, 5, byref(_PKEY_AppUserModel_ID), byref(variant),
                          types=(POINTER(PROPERTYKEY), POINTER(PROPVARIANT)))
            if hr >= 0 and variant.vt in (31, 8) and variant.value:
                value = ctypes.wstring_at(variant.value)
        finally:
            ole32.PropVariantClear(byref(variant))
            com_release(store)
    with _identity_lock:
        _window_ids[hwnd] = (now, value)
        if len(_window_ids) > 1024:
            _window_ids.clear()
    return value


def _text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(min(length + 1, 2048))
    user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value


def window_title(hwnd: int) -> str:
    return _text(hwnd)


def _class(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def _cloaked(hwnd: int) -> bool:
    value = wintypes.DWORD()
    return dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_CLOAKED, byref(value), ctypes.sizeof(value)) == 0 and bool(value.value)


def describe(hwnd: int) -> Win | None:
    """Everything Nova knows about one window, or None if it's gone."""
    if not hwnd or not user32.IsWindow(hwnd):
        return None
    pid = wintypes.DWORD()
    tid = user32.GetWindowThreadProcessId(hwnd, byref(pid))
    exe, process_aumid = _process_identity(pid.value)
    window_aumid = _window_aumid(hwnd)
    aumids = tuple(dict.fromkeys(a for a in (window_aumid, process_aumid) if a))
    return Win(
        int(hwnd), int(pid.value), int(tid), _text(hwnd), _class(hwnd), exe, aumids,
        bool(user32.IsIconic(hwnd)), bool(user32.IsZoomed(hwnd)), int(user32.GetWindow(hwnd, GW_OWNER) or 0),
    )


def _app_window(hwnd: int, include_owned: bool) -> bool:
    if not user32.IsWindowVisible(hwnd):
        return False
    style = user32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
    if style & _WS_EX_TOOLWINDOW or style & _WS_EX_NOACTIVATE:
        return False
    if not include_owned and user32.GetWindow(hwnd, GW_OWNER):
        return False
    if user32.GetWindowTextLengthW(hwnd) <= 0 or _cloaked(hwnd):
        return False
    return True


def app_windows(*, include_owned: bool = False, limit: int | None = None) -> list[Win]:
    """Visible, titled, uncloaked top-level windows, frontmost first (z-order)."""
    handles: list[int] = []

    @WNDENUMPROC
    def collect(hwnd, _):
        if _app_window(hwnd, include_owned):
            handles.append(int(hwnd))
        return limit is None or len(handles) < limit

    user32.EnumWindows(collect, 0)
    own = os.getpid()
    out = []
    for hwnd in handles:
        win = describe(hwnd)
        if win is not None and win.pid != own:
            out.append(win)
    return out


def foreground() -> int:
    return int(user32.GetForegroundWindow() or 0)


def root(hwnd: int) -> int:
    return int(user32.GetAncestor(hwnd, GA_ROOT) or 0)


def is_window(hwnd: int) -> bool:
    return bool(hwnd) and bool(user32.IsWindow(hwnd))


def is_hung(hwnd: int) -> bool:
    return bool(user32.IsHungAppWindow(hwnd))


def workstation_locked() -> bool:
    """True while the lock screen (or a secure desktop) has the input."""
    desktop = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if not desktop:
        return True
    user32.CloseDesktop(desktop)
    return False


# --- foreground ----------------------------------------------------------------------------------------------------


def _is_foreground(hwnd: int) -> bool:
    fg = foreground()
    return fg == hwnd or (fg != 0 and root(fg) == hwnd)


def grant_foreground() -> None:
    """Make Nova the last input source (an unassigned key), then pass the foreground right on to whatever starts next."""
    send_keys([(_VK_MASK, True), (_VK_MASK, False)])
    user32.AllowSetForegroundWindow(_ASFW_ANY)


def activate(hwnd: int, timeout: float = 0.4) -> str:
    """Bring a window to the front, restoring it if minimized. Returns how ("plain" / "input" / "attach")."""
    if not is_window(hwnd):
        raise ActionError("That window is gone.")
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    if _is_foreground(hwnd):
        return "already"
    user32.SetForegroundWindow(hwnd)
    if wait_for(lambda: _is_foreground(hwnd), 0.05):
        return "plain"
    send_keys([(_VK_MASK, True), (_VK_MASK, False)])  # Windows lets the last input source change the foreground
    user32.SetForegroundWindow(hwnd)
    if wait_for(lambda: _is_foreground(hwnd), 0.1):
        return "input"
    ours = kernel32.GetCurrentThreadId()
    fg = foreground()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = bool(fg_tid) and fg_tid != ours and bool(user32.AttachThreadInput(ours, fg_tid, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(ours, fg_tid, False)
    if wait_for(lambda: _is_foreground(hwnd), timeout):
        return "attach"
    raise ActionError("Windows wouldn't let me bring that window to the front.")


# --- geometry --------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Monitor:
    handle: int
    rect: Rect
    work: Rect
    primary: bool
    device: str
    dpi: int


def _rect(r: wintypes.RECT) -> Rect:
    return (r.left, r.top, r.right, r.bottom)


def monitors() -> list[Monitor]:
    found: list[Monitor] = []

    @MONITORENUMPROC
    def collect(handle, _hdc, _rect_ptr, _):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if user32.GetMonitorInfoW(handle, byref(info)):
            dpi_x, dpi_y = wintypes.UINT(96), wintypes.UINT(96)
            shcore.GetDpiForMonitor(handle, 0, byref(dpi_x), byref(dpi_y))
            found.append(Monitor(int(handle), _rect(info.rcMonitor), _rect(info.rcWork), bool(info.dwFlags & 1),
                                 info.szDevice, int(dpi_x.value)))
        return True

    with dpi_aware():
        user32.EnumDisplayMonitors(None, None, collect, 0)
    return sorted(found, key=lambda m: (m.rect[0], m.rect[1]))


def monitor_of(hwnd: int) -> int:
    with dpi_aware():
        return int(user32.MonitorFromWindow(hwnd, 2) or 0)  # nearest


def cursor_monitor() -> int:
    point = wintypes.POINT()
    with dpi_aware():
        user32.GetCursorPos(byref(point))
        return int(user32.MonitorFromPoint(point, 2) or 0)


def cursor_over(hwnd: int) -> bool:
    point = wintypes.POINT()
    with dpi_aware():
        user32.GetCursorPos(byref(point))
        under = user32.WindowFromPoint(point)
    return bool(under) and root(int(under)) == hwnd


def show_state(hwnd: int) -> int:
    placement = WINDOWPLACEMENT()
    placement.length = ctypes.sizeof(placement)
    user32.GetWindowPlacement(hwnd, byref(placement))
    return int(placement.showCmd)


def show(hwnd: int, command: int) -> None:
    user32.ShowWindowAsync(hwnd, command)


def window_rect(hwnd: int) -> Rect:
    r = wintypes.RECT()
    with dpi_aware():
        user32.GetWindowRect(hwnd, byref(r))
    return _rect(r)


def frame_rect(hwnd: int) -> Rect:
    """The visible frame (without Windows 11's invisible resize borders)."""
    r = wintypes.RECT()
    with dpi_aware():
        if dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_EXTENDED_FRAME_BOUNDS, byref(r), ctypes.sizeof(r)) != 0:
            user32.GetWindowRect(hwnd, byref(r))
    return _rect(r)


def place(hwnd: int, visible: Rect) -> None:
    """Move/size so the *visible* frame lands on ``visible`` (DWM border compensation)."""
    outer, frame = window_rect(hwnd), frame_rect(hwnd)
    left = visible[0] - (frame[0] - outer[0])
    top = visible[1] - (frame[1] - outer[1])
    right = visible[2] + (outer[2] - frame[2])
    bottom = visible[3] + (outer[3] - frame[3])
    with dpi_aware():
        user32.SetWindowPos(hwnd, None, left, top, right - left, bottom - top,
                            _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_ASYNCWINDOWPOS)


def post_close(hwnd: int) -> bool:
    return bool(user32.PostMessageW(hwnd, WM_CLOSE, 0, 0))


# --- process integrity ---------------------------------------------------------------------------------------------


def _integrity(process: int) -> int:
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(process, 0x0008, byref(token)):  # TOKEN_QUERY
        return -1
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 25, None, 0, byref(size))  # TokenIntegrityLevel
        if not 0 < size.value < 4096:
            return -1
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, 25, buffer, size, byref(size)):
            return -1
        sid = ctypes.cast(buffer, POINTER(c_void_p))[0]
        count = advapi32.GetSidSubAuthorityCount(sid)
        if not count or not count[0]:
            return -1
        return int(advapi32.GetSidSubAuthority(sid, count[0] - 1)[0])
    finally:
        kernel32.CloseHandle(token)


_own_integrity: int | None = None


def elevated(pid: int) -> bool:
    """Runs above Nova's integrity (as administrator): Windows blocks Nova's input to it."""
    global _own_integrity
    if _own_integrity is None:
        _own_integrity = _integrity(kernel32.GetCurrentProcess())
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        level = _integrity(handle)
    finally:
        kernel32.CloseHandle(handle)
    return level > _own_integrity >= 0


# --- input ---------------------------------------------------------------------------------------------------------


def send_keys(strokes: Sequence[tuple[int, bool]]) -> int:
    """(vk, down) strokes, marked as Nova's (the keyboard hook ignores them). Returns how many Windows took."""
    events = (INPUT * len(strokes))()
    for item, (vk, down) in zip(events, strokes, strict=True):
        flags = (_KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED_VKS else 0) | (0 if down else _KEYEVENTF_KEYUP)
        item.type = _INPUT_KEYBOARD
        item.u.ki = KEYBDINPUT(vk, 0, flags, 0, NOVA_MARK)
    return int(user32.SendInput(len(strokes), events, ctypes.sizeof(INPUT)))


def send_unicode(text: str) -> int:
    """Type text as Unicode key events (UTF-16 units; newlines as Enter)."""
    events: list[INPUT] = []
    for ch in text.replace("\r\n", "\n").replace("\r", "\n"):
        if ch == "\n":
            for down in (True, False):
                item = INPUT(type=_INPUT_KEYBOARD)
                item.u.ki = KEYBDINPUT(0x0D, 0, 0 if down else _KEYEVENTF_KEYUP, 0, NOVA_MARK)
                events.append(item)
            continue
        raw = ch.encode("utf-16-le")
        for i in range(0, len(raw), 2):
            unit = int.from_bytes(raw[i : i + 2], "little")
            for down in (True, False):
                item = INPUT(type=_INPUT_KEYBOARD)
                item.u.ki = KEYBDINPUT(0, unit, _KEYEVENTF_UNICODE | (0 if down else _KEYEVENTF_KEYUP), 0, NOVA_MARK)
                events.append(item)
    if not events:
        return 0
    array = (INPUT * len(events))(*events)
    return int(user32.SendInput(len(events), array, ctypes.sizeof(INPUT)))


def send_wheel(notches: int) -> int:
    item = INPUT(type=_INPUT_MOUSE)
    item.u.mi = MOUSEINPUT(0, 0, ctypes.c_uint32(120 * notches).value, _MOUSEEVENTF_WHEEL, 0, NOVA_MARK)
    return int(user32.SendInput(1, byref(item), ctypes.sizeof(INPUT)))


_PHYSICAL_MODIFIERS = (0x10, 0x11, 0x12, 0x5B, 0x5C)  # shift, ctrl, alt, left / right win


def modifiers_down() -> bool:
    return any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in _PHYSICAL_MODIFIERS)
