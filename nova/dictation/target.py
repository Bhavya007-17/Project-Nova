"""Where dictated text goes (SPEC §11): the focused field of the window in front.

``look`` finds it and reads the text just before its caret — through Nova's Chrome extension in Chrome (any
frame; Google Docs draws text on a canvas, so there only its cursor position), through UI Automation TextPattern
elsewhere (Notepad, Word, Electron apps). ``insert`` pastes with the clipboard borrow and checks the text landed:
the Win32 control's length grew, the text before the caret now ends with it, or Docs' cursor moved. ``erase``
takes characters back ("scratch that"). Runs on the dictation thread; every call is time-boxed.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..actions import input as keyboard
from ..actions import native
from ..actions.catalog import Catalog
from ..actions.native import ActionError, Win
from ..actions.uia import FocusReader
from ..brain.risk import Scene
from .rules import landed

_log = logging.getLogger(__name__)

CHROME_EXE = "chrome.exe"
_NOT_WINDOWS = frozenset({"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"})
_CONTAINERS = frozenset({50033, 50032})  # UIA pane / window: Chrome / Electron still building their tree
_TEXT_FIELDS = frozenset({50004, 50030})  # UIA edit / document
# Usually ~40-60 ms; Notepad sometimes stalls (seen live: two sentences landed after the old 0.6 s limit). His own
# clipboard only goes back once the text is there, so a late Ctrl+V still pastes the dictated words, never his.
LANDED_WAIT_S = 3.0
READY_S = 3.0  # an app the plan just opened: wait this long for a text field to have focus


@dataclass(frozen=True)
class Spot:
    """The place the next words would land, as seen just now."""

    win: Win | None  # the window in front (None: nothing to type into)
    app: str = ""
    kind: str = "none"  # native | chrome | docs | none
    before: str | None = None  # text before the caret (None: can't tell)
    password: bool = False
    url: str = ""
    edit_hwnd: int = 0  # a Win32 Edit / RichEdit control with focus (its length is a landing check)
    length: int | None = None
    cursor: tuple[int, ...] | None = None  # Google Docs' cursor (it moves when text lands)
    ms: float = 0.0

    @property
    def hwnd(self) -> int:
        return self.win.hwnd if self.win is not None else 0

    def scene(self) -> Scene:
        win = self.win
        return Scene(app=self.app, exe=win.exe_name if win else "", window_class=win.cls if win else "",
                     title=win.title if win else "", url=self.url, password=self.password,
                     locked=native.workstation_locked())


@dataclass(frozen=True)
class Landed:
    ok: bool | None  # True landed / False didn't show up / None no way to tell
    at: float  # perf_counter: when it landed (or was pasted, when there's no telling)
    ms: float  # paste + check


class Target:
    def __init__(self, catalog: Callable[[], Catalog], chrome: Any = None, focus: FocusReader | None = None) -> None:
        self._catalog = catalog
        self._chrome = chrome  # nova.actions.chrome.Chrome (None: no extension)
        self._focus = focus or FocusReader()

    def _chrome_caret(self) -> dict[str, Any] | None:
        chrome = self._chrome
        if chrome is None or not chrome.connected:
            return None
        try:
            return chrome.caret()
        except ActionError as error:
            _log.info("no caret from the extension: %s", error)
            return None

    def look(self) -> Spot:
        began = time.perf_counter()
        win = native.describe(native.foreground())
        if win is None or win.cls in _NOT_WINDOWS:
            return Spot(None, ms=(time.perf_counter() - began) * 1000)
        app = self._catalog().app_for(win.exe, win.aumids)
        name = app.name if app else win.exe_name.removesuffix(".exe")
        edit = keyboard._focus_hwnd(win)
        length = keyboard._text_length(edit)
        if win.exe_name.lower() == CHROME_EXE:
            data = self._chrome_caret()
            if data is not None:
                url = str((data.get("tab") or {}).get("url") or "")
                ms = (time.perf_counter() - began) * 1000
                if data.get("docs"):
                    cursor = data.get("cursor")
                    return Spot(win, "Google Docs", "docs", None, False, url, cursor=tuple(cursor) if cursor else None,
                                ms=ms)
                before = data.get("before")
                return Spot(win, name, "chrome", before if isinstance(before, str) else None, bool(data.get("secret")),
                            url, ms=ms)
        caret = self._focus.caret()
        if caret is not None and caret.focus.control_type in _CONTAINERS:
            time.sleep(0.15)  # Chrome / Electron answer "pane" to their first question (a password field too)
            caret = self._focus.caret()
        before = caret.before if caret is not None else None
        password = bool(caret and caret.focus.password)
        return Spot(win, name, "native", before, password, edit_hwnd=edit if length is not None else 0, length=length,
                    ms=(time.perf_counter() - began) * 1000)

    def _check(self, spot: Spot, text: str) -> Callable[[], bool] | None:
        """How to tell the text arrived, best first; None when there's no way."""
        flat = text.replace("\r", "")
        if spot.length is not None and spot.edit_hwnd:
            return lambda: (keyboard._text_length(spot.edit_hwnd) or 0) >= spot.length + len(flat)
        if not flat.strip():
            return None  # only line breaks: nothing to compare
        if spot.kind == "docs":
            if spot.cursor is None:
                return None

            def moved() -> bool:
                data = self._chrome_caret() or {}
                cursor = data.get("cursor")
                return bool(cursor) and tuple(cursor) != spot.cursor

            return moved
        if spot.kind == "chrome" and spot.before is not None:
            return lambda: landed((self._chrome_caret() or {}).get("before"), text)
        if spot.kind == "native" and spot.before is not None:
            def arrived() -> bool:
                caret = self._focus.caret(0.2)
                return caret is not None and landed(caret.before, text)

            return arrived
        return None

    def insert(self, spot: Spot, text: str) -> Landed:
        """Paste ``text`` at ``spot`` (raises ActionError if it can't be done)."""
        began = time.perf_counter()
        clip = text.replace("\r\n", "\n").replace("\n", "\r\n")
        ok, at = keyboard.paste(clip, spot.win, self._check(spot, text), LANDED_WAIT_S)
        return Landed(ok, at, (time.perf_counter() - began) * 1000)

    def erase(self, spot: Spot, count: int) -> None:
        keyboard.erase(count, spot.win)

    def ready(self, timeout: float = READY_S) -> bool:
        """An app the plan just opened ("open Word and type this"): wait until a text field has focus — a paste
        before its document is up goes nowhere (seen live with Word)."""
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            win = native.describe(native.foreground())
            if win is not None and win.cls not in _NOT_WINDOWS:
                if win.exe_name.lower() == CHROME_EXE:
                    data = self._chrome_caret() or {}
                    if data.get("editable") or data.get("docs"):
                        return True
                else:
                    focus = self._focus.focused(0.3)
                    if focus is not None and focus.control_type in _TEXT_FIELDS:
                        return True
            time.sleep(0.15)
        return False
