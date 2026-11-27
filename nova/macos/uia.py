"""macOS stand-in for ``nova.actions.uia``: the focused control and the text before its caret, via Accessibility.

AX roles are mapped onto the UI Automation control-type ids the shared code checks
(50004 edit, 50030 document, 50033 pane / unknown container).
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import native

_EDIT, _DOCUMENT, _PANE, _BUTTON, _LINK, _COMBO = 50004, 50030, 50033, 50000, 50005, 50003
_ROLES = {"AXTextField": _EDIT, "AXTextArea": _EDIT, "AXSearchField": _EDIT, "AXComboBox": _COMBO,
          "AXWebArea": _DOCUMENT, "AXButton": _BUTTON, "AXLink": _LINK, "AXGroup": _PANE, "AXWindow": _PANE,
          "AXScrollArea": _PANE}


@dataclass(frozen=True)
class Focus:
    name: str
    control_type: int
    password: bool
    hwnd: int
    pid: int

    @property
    def role(self) -> str:
        return {50004: "edit", 50000: "button", 50005: "link", 50030: "document", 50003: "combobox"}.get(
            self.control_type, str(self.control_type))


@dataclass(frozen=True)
class Caret:
    focus: Focus
    before: str | None


_opened: set[int] = set()


def _focus() -> tuple[Focus, Any] | None:
    pid, app = native.front_app()
    if app is None:
        return None
    if pid not in _opened:
        # Chromium / Electron build their accessibility tree only for assistive apps that ask for it.
        native.ax_set(app, "AXManualAccessibility", True)
        _opened.add(pid)
    element = native.ax_get(app, "AXFocusedUIElement")
    if element is None:
        return Focus("", _PANE, False, 0, pid), None
    role = str(native.ax_get(element, "AXRole") or "")
    subrole = str(native.ax_get(element, "AXSubrole") or "")
    password = role == "AXSecureTextField" or subrole == "AXSecureTextField"
    control = _EDIT if password else _ROLES.get(role, _PANE)
    if control == _PANE and native.ax_get(element, "AXSelectedTextRange") is not None:
        control = _EDIT  # a custom text view (Electron, editors) that still exposes a caret
    name = str(native.ax_get(element, "AXTitle") or native.ax_get(element, "AXDescription") or "")
    return Focus(name, control, password, 0, native.ax_pid(element)), element


def _before(element, limit: int) -> str | None:
    selection = native.ax_get(element, "AXSelectedTextRange")
    value = native.ax_get(element, "AXValue")
    if selection is None or not isinstance(value, str):
        return None
    from ApplicationServices import AXValueGetValue, kAXValueCFRangeType

    ok, rng = AXValueGetValue(selection, kAXValueCFRangeType, None)
    if not ok:
        return None
    start = int(rng[0] if isinstance(rng, tuple) else rng.location)  # PyObjC gives CFRange as (location, length)
    return value[max(0, start - limit):start]


class FocusReader:
    """Same shape as the Windows reader; AX calls run on one worker thread with a deadline."""

    def __init__(self) -> None:
        self._requests: queue.SimpleQueue = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _ask(self, fn: Callable[[], Any], timeout: float) -> Any:
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="nova-ax", daemon=True)
                self._thread.start()
        reply: queue.SimpleQueue = queue.SimpleQueue()
        self._requests.put((fn, reply))
        try:
            return reply.get(timeout=timeout)
        except queue.Empty:
            return None

    def focused(self, timeout: float = 0.25) -> Focus | None:
        value = self._ask(lambda: (_focus() or (None,))[0], timeout)
        return value if isinstance(value, Focus) else None

    def caret(self, timeout: float = 0.35, limit: int = 300) -> Caret | None:
        def read():
            found = _focus()
            if found is None:
                return None
            focus, element = found
            if element is None:
                return Caret(focus, None)
            return Caret(focus, None if focus.password else _before(element, limit))

        value = self._ask(read, timeout)
        return value if isinstance(value, Caret) else None

    def close(self) -> None:
        if self._thread is not None:
            self._requests.put(None)

    def _run(self) -> None:
        while True:
            item = self._requests.get()
            if item is None:
                return
            fn, reply = item
            try:
                reply.put(fn())
            except Exception:  # noqa: BLE001
                reply.put(None)
