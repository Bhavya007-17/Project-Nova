"""Typing and keys (SPEC §8): Unicode key events for short text, a borrowed clipboard for longer text, key chords.

- Up to 20 characters (and no line breaks): SendInput Unicode events.
- Longer: clipboard borrow — save every format, put the text there (kept out of Win+V
  history), Ctrl+V, and put his clipboard back ~300 ms later unless something was copied since.
- Every key Nova sends carries NOVA_MARK, so its own keyboard hook lets it through.
- Password fields are refused before this runs (``nova.brain.risk``, via UI Automation).
- Nothing is sent while he still holds a modifier (it would turn "t" into Ctrl+T), or if the
  window that should get the text isn't in front any more.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import byref, wintypes

from . import native
from .native import ActionError, Win

_log = logging.getLogger(__name__)

UNICODE_MAX = 20
RESTORE_AFTER_S = 0.3

_VK = {
    "ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "enter": 0x0D, "esc": 0x1B, "tab": 0x09, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "insert": 0x2D, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "=": 0xBB, "-": 0xBD, ",": 0xBC, ".": 0xBE,
    "/": 0xBF, ";": 0xBA, "'": 0xDE, "[": 0xDB, "]": 0xDD, "\\": 0xDC, "`": 0xC0,
    **{f"f{n}": 0x6F + n for n in range(1, 25)},
}
_MODIFIERS = frozenset({"ctrl", "shift", "alt", "win"})

u32, k32 = native.user32, native.kernel32
native._bind(u32, "OpenClipboard", wintypes.BOOL, wintypes.HWND)
native._bind(u32, "CloseClipboard", wintypes.BOOL)
native._bind(u32, "EmptyClipboard", wintypes.BOOL)
native._bind(u32, "EnumClipboardFormats", wintypes.UINT, wintypes.UINT)
native._bind(u32, "GetClipboardData", wintypes.HANDLE, wintypes.UINT)
native._bind(u32, "SetClipboardData", wintypes.HANDLE, wintypes.UINT, wintypes.HANDLE)
native._bind(u32, "GetClipboardSequenceNumber", wintypes.DWORD)
native._bind(u32, "RegisterClipboardFormatW", wintypes.UINT, wintypes.LPCWSTR)
native._bind(u32, "CreateWindowExW", wintypes.HWND, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
             ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
             wintypes.HINSTANCE, wintypes.LPVOID)
native._bind(u32, "DestroyWindow", wintypes.BOOL, wintypes.HWND)
native._bind(u32, "PeekMessageW", wintypes.BOOL, ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
             wintypes.UINT)
native._bind(u32, "TranslateMessage", wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
native._bind(u32, "DispatchMessageW", ctypes.c_ssize_t, ctypes.POINTER(wintypes.MSG))
native._bind(u32, "SendMessageTimeoutW", wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
             wintypes.LPARAM, wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t))


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hwndActive", wintypes.HWND),
                ("hwndFocus", wintypes.HWND), ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND), ("rcCaret", wintypes.RECT)]


native._bind(u32, "GetGUIThreadInfo", wintypes.BOOL, wintypes.DWORD, ctypes.POINTER(GUITHREADINFO))
native._bind(k32, "GlobalAlloc", wintypes.HGLOBAL, wintypes.UINT, ctypes.c_size_t)
native._bind(k32, "GlobalLock", wintypes.LPVOID, wintypes.HGLOBAL)
native._bind(k32, "GlobalUnlock", wintypes.BOOL, wintypes.HGLOBAL)
native._bind(k32, "GlobalSize", ctypes.c_size_t, wintypes.HGLOBAL)
native._bind(k32, "GlobalFree", wintypes.HGLOBAL, wintypes.HGLOBAL)

_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002
_GDI_FORMATS = frozenset({2, 3, 9, 14, 0x80, 0x82, 0x83, 0x8E})  # bitmaps / metafiles / palettes: handles, not memory
_HWND_MESSAGE = wintypes.HWND(-3)
_WM_GETTEXTLENGTH = 0x000E
_SMTO_ABORTIFHUNG = 0x0002
_clipboard_lock = threading.Lock()  # one borrow at a time


def keys_to_vks(keys: str) -> list[int]:
    vks = []
    for part in keys.lower().split("+"):
        if part in _VK:
            vks.append(_VK[part])
        elif len(part) == 1 and part.isalnum():
            vks.append(ord(part.upper()))
        else:
            raise ActionError(f"I don't know the key {part!r}.")
    return vks


def chord(keys: str) -> list[tuple[int, bool]]:
    """ "ctrl+shift+t" -> modifiers down, key down, key up, modifiers up (reverse)."""
    vks = keys_to_vks(keys)
    return [(vk, True) for vk in vks] + [(vk, False) for vk in reversed(vks)]


def _wait_for_hands_off() -> None:
    if native.modifiers_down() and not native.wait_for(lambda: not native.modifiers_down(), 0.6):
        raise ActionError("Let go of Ctrl / Shift / Alt / Win first — I'd press them with you.")


def _guard(target: Win | None) -> None:
    if target is None:
        return
    if not native.is_window(target.hwnd):
        raise ActionError("The window I was typing into is gone.")
    fg = native.foreground()
    if fg != target.hwnd and native.root(fg) != target.hwnd:
        raise ActionError("Another window came to the front — I stopped.")
    if native.elevated(target.pid):
        raise ActionError("That window runs as administrator — Windows won't let Nova type into it.")


def press(keys: str, target: Win | None) -> str:
    _wait_for_hands_off()
    _guard(target)
    strokes = chord(keys)
    if native.send_keys(strokes) != len(strokes):
        raise ActionError("Windows blocked the keys (another app may be grabbing the keyboard).")
    return f"Pressed {keys.replace('+', ' + ')}"


def _focus_hwnd(target: Win) -> int:
    info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    if u32.GetGUIThreadInfo(target.tid, byref(info)) and info.hwndFocus:
        return int(info.hwndFocus)
    return 0


def _text_length(hwnd: int) -> int | None:
    """Length of an Edit / RichEdit control's text (None for other controls: web pages, Electron, ...)."""
    if not hwnd:
        return None
    cls = native._class(hwnd).lower()
    if cls != "edit" and not cls.startswith("richedit"):
        return None
    result = ctypes.c_size_t()
    if not u32.SendMessageTimeoutW(hwnd, _WM_GETTEXTLENGTH, 0, 0, _SMTO_ABORTIFHUNG, 200, byref(result)):
        return None
    return int(result.value)


def type_text(text: str, submit: bool, target: Win | None) -> str:
    if not text or "\0" in text:
        raise ActionError("Nothing to type.")
    _wait_for_hands_off()
    _guard(target)
    focus = _focus_hwnd(target) if target is not None else 0
    before = _text_length(focus)
    if len(text) <= UNICODE_MAX and "\n" not in text:
        sent = native.send_unicode(text)
        if sent == 0:
            raise ActionError("Windows blocked the typing.")
        how = "typed"
    else:
        _paste(text, target)
        how = "pasted"
    landed = ""
    if before is not None:
        grew = native.wait_for(lambda: (_text_length(focus) or 0) >= before + len(text.replace("\r", "")), 0.5)
        landed = "" if grew else " (couldn't confirm it landed)"
    if submit:
        _guard(target)
        native.send_keys(chord("enter"))
    words = len(text.split())
    noun = "word" if words == 1 else "words"
    return f"{how.capitalize()} {words} {noun}{' + Enter' if submit else ''}{landed}"


# --- clipboard borrow ------------------------------------------------------------------------------------------------


def _open_clipboard(owner: int) -> None:
    if not native.wait_for(lambda: bool(u32.OpenClipboard(owner)), 0.3, interval=0.01):
        raise ActionError("Another app is holding the clipboard — try again.")


def _snapshot() -> list[tuple[int, bytes]]:
    saved = []
    fmt = 0
    while True:
        fmt = int(u32.EnumClipboardFormats(fmt))
        if not fmt:
            return saved
        if fmt in _GDI_FORMATS or 0x200 <= fmt <= 0x3FF:
            continue
        handle = u32.GetClipboardData(fmt)
        if not handle:
            continue
        size = k32.GlobalSize(handle)
        pointer = k32.GlobalLock(handle)
        if not pointer:
            continue
        try:
            saved.append((fmt, ctypes.string_at(pointer, size)))
        finally:
            k32.GlobalUnlock(handle)


def _put(fmt: int, data: bytes) -> bool:
    memory = k32.GlobalAlloc(_GMEM_MOVEABLE, max(1, len(data)))
    if not memory:
        return False
    pointer = k32.GlobalLock(memory)
    if not pointer:
        k32.GlobalFree(memory)
        return False
    ctypes.memmove(pointer, data, len(data))
    k32.GlobalUnlock(memory)
    if not u32.SetClipboardData(fmt, memory):
        k32.GlobalFree(memory)
        return False
    return True


def _private_formats() -> list[tuple[int, bytes]]:
    """Keep Nova's paste out of Win+V history and the cloud clipboard."""
    zero = (0).to_bytes(4, "little")
    return [
        (u32.RegisterClipboardFormatW("ExcludeClipboardContentFromMonitorProcessing"), b"\0"),
        (u32.RegisterClipboardFormatW("CanIncludeInClipboardHistory"), zero),
        (u32.RegisterClipboardFormatW("CanUploadToCloudClipboard"), zero),
    ]


def _pump(seconds: float) -> None:
    """Wait while handling this thread's window messages (the clipboard owner must answer WM_DESTROYCLIPBOARD)."""
    message = wintypes.MSG()
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        while u32.PeekMessageW(byref(message), None, 0, 0, 1):  # PM_REMOVE
            u32.TranslateMessage(byref(message))
            u32.DispatchMessageW(byref(message))
        time.sleep(0.01)


def _paste(text: str, target: Win | None) -> None:
    """Clipboard borrow, start to finish on this thread (~0.3 s): his clipboard is back before the next step."""
    with _clipboard_lock:
        owner = u32.CreateWindowExW(0, "STATIC", "Nova clipboard", 0, 0, 0, 0, 0, _HWND_MESSAGE, None, None, None)
        if not owner:
            raise ActionError("Couldn't borrow the clipboard.")
        try:
            _open_clipboard(owner)
            try:
                saved = _snapshot()
                if not u32.EmptyClipboard() or not _put(_CF_UNICODETEXT, (text + "\0").encode("utf-16-le")):
                    raise ActionError("Couldn't put the text on the clipboard.")
                for fmt, data in _private_formats():
                    _put(fmt, data)
            finally:
                u32.CloseClipboard()
            sequence = int(u32.GetClipboardSequenceNumber())
            _guard(target)
            native.send_keys(chord("ctrl+v"))
            _pump(RESTORE_AFTER_S)
            if int(u32.GetClipboardSequenceNumber()) != sequence:
                return  # he (or an app) copied something new: that wins
            _open_clipboard(owner)
            try:
                if int(u32.GetClipboardSequenceNumber()) == sequence:
                    u32.EmptyClipboard()
                    for fmt, data in saved:
                        _put(fmt, data)
            finally:
                u32.CloseClipboard()
        finally:
            u32.DestroyWindow(owner)


def scroll(direction: str, amount: str, target: Win | None) -> str:
    """Wheel when the mouse is over the window, else keys (PageDown / arrows / Ctrl+Home)."""
    _wait_for_hands_off()
    _guard(target)
    if direction in ("top", "bottom"):
        native.send_keys(chord("ctrl+home" if direction == "top" else "ctrl+end"))
        return f"Scrolled to the {direction}"
    notches = {"small": 2, "page": 5, "large": 15}.get(amount or "page", 5)
    if direction in ("up", "down") and target is not None and native.cursor_over(target.hwnd):
        native.send_wheel(notches if direction == "up" else -notches)
    else:
        key = {"up": "up", "down": "down", "left": "left", "right": "right"}[direction]
        if amount in ("page", "large") and direction in ("up", "down"):
            page = "pageup" if direction == "up" else "pagedown"
            for _ in range(1 if amount == "page" else 3):
                native.send_keys(chord(page))
        else:
            for _ in range(3):
                native.send_keys(chord(key))
    return f"Scrolled {direction}"

