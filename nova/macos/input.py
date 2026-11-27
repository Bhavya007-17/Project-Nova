"""macOS stand-in for ``nova.actions.input``: keys, typing, the clipboard borrow, erase, scroll.

Plans name keys the Windows way ("ctrl+t", "alt+left", "f5"); ``mac_keys`` turns them into the Mac shortcut
(Cmd+T, Cmd+[, Cmd+R). Ctrl stays Ctrl for Ctrl+Tab (tab switching works the same in Chrome on a Mac).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from AppKit import NSPasteboard, NSPasteboardTypeString

from . import native
from .native import ActionError, Win

_log = logging.getLogger(__name__)

UNICODE_MAX = 20
RESTORE_AFTER_S = 0.3

_VK = {
    "ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "cmd": 0x5B, "enter": 0x0D, "esc": 0x1B, "tab": 0x09,
    "space": 0x20, "backspace": 0x08, "delete": 0x2E, "insert": 0x2D, "up": 0x26, "down": 0x28, "left": 0x25,
    "right": 0x27, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "=": 0xBB, "-": 0xBD, ",": 0xBC,
    ".": 0xBE, "/": 0xBF, ";": 0xBA, "'": 0xDE, "[": 0xDB, "]": 0xDD, "\\": 0xDC, "`": 0xC0,
    **{f"f{n}": 0x6F + n for n in range(1, 25)},
}
_SPECIAL = {
    "alt+left": "cmd+[", "alt+right": "cmd+]", "f5": "cmd+r", "ctrl+f5": "cmd+shift+r", "alt+f4": "cmd+w",
    "ctrl+home": "cmd+up", "ctrl+end": "cmd+down", "home": "cmd+up", "end": "cmd+down",
    "ctrl+left": "alt+left", "ctrl+right": "alt+right", "ctrl+backspace": "alt+backspace", "f11": "ctrl+cmd+f",
    "ctrl+y": "cmd+shift+z", "win": "cmd+space", "win+d": "f11", "win+e": "cmd+shift+a",
}
_KEEP_CTRL = frozenset({"tab"})
_clipboard_lock = threading.Lock()


def mac_keys(keys: str) -> str:
    """A Windows key chord -> the Mac one."""
    keys = keys.lower().replace(" ", "")
    if keys in _SPECIAL:
        return _SPECIAL[keys]
    parts = keys.split("+")
    if "ctrl" in parts and not (set(parts) & _KEEP_CTRL):
        parts = ["cmd" if p == "ctrl" else p for p in parts]
    parts = ["cmd" if p == "win" else p for p in parts]
    return "+".join(dict.fromkeys(parts))


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
    vks = keys_to_vks(keys)
    return [(vk, True) for vk in vks] + [(vk, False) for vk in reversed(vks)]


def _wait_for_hands_off() -> None:
    if native.modifiers_down() and not native.wait_for(lambda: not native.modifiers_down(), 0.6):
        raise ActionError("Let go of Control / Shift / Option / Command first — I'd press them with you.")


def _guard(target: Win | None) -> None:
    if target is None:
        return
    if not native.is_window(target.hwnd):
        raise ActionError("The window I was typing into is gone.")
    fg = native.foreground()
    if fg != target.hwnd:
        front = native.describe(fg)
        if front is None or front.pid != target.pid:
            raise ActionError("Another window came to the front — I stopped.")


def press(keys: str, target: Win | None) -> str:
    _wait_for_hands_off()
    _guard(target)
    strokes = chord(mac_keys(keys))
    if native.send_keys(strokes) != len(strokes):
        raise ActionError("macOS blocked the keys (Nova needs Accessibility permission).")
    return f"Pressed {keys.replace('+', ' + ')}"


def _focus_hwnd(target: Win) -> int:
    return 0  # no per-control handles on a Mac: dictation checks through Accessibility instead


def _text_length(hwnd: int) -> int | None:
    return None


def type_text(text: str, submit: bool, target: Win | None) -> str:
    if not text or "\0" in text:
        raise ActionError("Nothing to type.")
    _wait_for_hands_off()
    _guard(target)
    if len(text) <= UNICODE_MAX and "\n" not in text:
        if native.send_unicode(text) == 0:
            raise ActionError("macOS blocked the typing.")
        how = "typed"
    else:
        paste(text, target)
        how = "pasted"
    if submit:
        _guard(target)
        native.send_keys(chord("enter"))
    words = len(text.split())
    noun = "word" if words == 1 else "words"
    return f"{how.capitalize()} {words} {noun}{' + Enter' if submit else ''}"


def _snapshot(board) -> list[dict]:
    saved = []
    for item in board.pasteboardItems() or []:
        data = {}
        for kind in item.types() or []:
            value = item.dataForType_(kind)
            if value is not None:
                data[str(kind)] = value
        if data:
            saved.append(data)
    return saved


def _restore(board, saved: list[dict]) -> None:
    from AppKit import NSPasteboardItem

    board.clearContents()
    items = []
    for data in saved:
        item = NSPasteboardItem.alloc().init()
        for kind, value in data.items():
            item.setData_forType_(value, kind)
        items.append(item)
    if items:
        board.writeObjects_(items)


def paste(text: str, target: Win | None, landed: Callable[[], bool] | None = None,
          wait_s: float = 0.6) -> tuple[bool | None, float]:
    """Borrow the clipboard: put ``text`` on it, Cmd+V, give the user's clipboard back once it landed."""
    if not text or "\0" in text:
        raise ActionError("Nothing to type.")
    _wait_for_hands_off()
    _guard(target)
    text = text.replace("\r\n", "\n")
    with _clipboard_lock:
        board = NSPasteboard.generalPasteboard()
        saved = _snapshot(board)
        board.clearContents()
        if not board.setString_forType_(text, NSPasteboardTypeString):
            raise ActionError("Couldn't put the text on the clipboard.")
        board.setString_forType_("", "org.nspasteboard.TransientType")  # clipboard managers: don't keep this
        change = board.changeCount()
        _guard(target)
        native.send_keys(chord("cmd+v"))
        sent = time.perf_counter()
        ok: bool | None = None
        at = sent
        if landed is None:
            time.sleep(RESTORE_AFTER_S)
        else:
            while True:
                time.sleep(0.02)
                try:
                    if landed():
                        ok, at = True, time.perf_counter()
                        break
                except Exception:  # noqa: BLE001
                    _log.debug("landing check failed", exc_info=True)
                if time.perf_counter() - sent >= wait_s:
                    ok = False
                    break
            if not ok:
                time.sleep(max(0.0, RESTORE_AFTER_S - (time.perf_counter() - sent)))
        if board.changeCount() == change:
            _restore(board, saved)
        return ok, at


def erase(count: int, target: Win | None) -> None:
    if count <= 0:
        return
    _wait_for_hands_off()
    _guard(target)
    strokes = chord("backspace") * count
    if native.send_keys(strokes) != len(strokes):
        raise ActionError("macOS blocked the keys.")


def scroll(direction: str, amount: str, target: Win | None) -> str:
    _wait_for_hands_off()
    _guard(target)
    if direction in ("top", "bottom"):
        native.send_keys(chord("cmd+up" if direction == "top" else "cmd+down"))
        return f"Scrolled to the {direction}"
    notches = {"small": 2, "page": 5, "large": 15}.get(amount or "page", 5)
    if direction in ("up", "down") and target is not None and native.cursor_over(target.hwnd):
        native.send_wheel(notches if direction == "up" else -notches)
    else:
        if amount in ("page", "large") and direction in ("up", "down"):
            page = "pageup" if direction == "up" else "pagedown"
            for _ in range(1 if amount == "page" else 3):
                native.send_keys(chord(page))
        else:
            for _ in range(3):
                native.send_keys(chord(direction))
    return f"Scrolled {direction}"
