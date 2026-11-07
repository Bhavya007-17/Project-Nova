"""UI Automation: the focused control (name, control type, password flag), SPEC §7.1 / §8, and the text just before
its caret (TextPattern) so dictation can fit its words to it (SPEC §11).

Asked on its own thread (multithreaded apartment) with a deadline, so a slow or hung app
can't stall the brain. Only asked when it matters (before typing), because asking Chrome
turns on its accessibility tree, which costs it some CPU.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import threading
from collections.abc import Callable
from ctypes import POINTER, byref, c_void_p, wintypes
from dataclasses import dataclass
from typing import Any

from . import native

_log = logging.getLogger(__name__)

_CLSID_CUIAutomation = native.guid("{FF48DBA4-60EF-4201-AA87-54103EEF594E}")
_IID_IUIAutomation = native.guid("{30CBE57D-D9D0-452A-AB13-7AC5AC4825EE}")
_IID_IUIAutomationTextPattern = native.guid("{32EBA289-3583-42C9-9C59-3B6D9A1E9B6A}")
_oleaut32 = ctypes.WinDLL("oleaut32")
_oleaut32.SysFreeString.argtypes = [c_void_p]
_oleaut32.SysFreeString.restype = None
# vtable slots (UIAutomationClient.h)
_GET_FOCUSED_ELEMENT = 8
_GET_CURRENT_PATTERN_AS = 14
_CURRENT_PROCESS_ID, _CURRENT_CONTROL_TYPE, _CURRENT_NAME = 20, 21, 23
_CURRENT_IS_PASSWORD, _CURRENT_NATIVE_WINDOW_HANDLE = 35, 36
_TEXT_GET_SELECTION = 5  # IUIAutomationTextPattern
_ARRAY_LENGTH, _ARRAY_ELEMENT = 3, 4  # IUIAutomationTextRangeArray
_RANGE_CLONE, _RANGE_GET_TEXT, _RANGE_MOVE_BY_UNIT, _RANGE_MOVE_BY_RANGE = 3, 12, 14, 15  # IUIAutomationTextRange
_UIA_TEXT_PATTERN_ID = 10014
_START, _END = 0, 1  # TextPatternRangeEndpoint
_CHARACTER = 0  # TextUnit
_EDIT_CONTROL = 50004


@dataclass(frozen=True)
class Focus:
    name: str
    control_type: int  # UIA control type id (50004 = edit)
    password: bool
    hwnd: int  # the native window behind it (0 for most web content)
    pid: int

    @property
    def role(self) -> str:
        return {50004: "edit", 50000: "button", 50005: "link", 50030: "document", 50003: "combobox"}.get(
            self.control_type, str(self.control_type))


@dataclass(frozen=True)
class Caret:
    focus: Focus
    before: str | None  # the text just before the caret (None: the control doesn't share its text)


class FocusReader:
    def __init__(self) -> None:
        self._requests: queue.SimpleQueue[tuple[Callable[[c_void_p], Any], queue.SimpleQueue] | None] = \
            queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _ask(self, fn: Callable[[c_void_p], Any], timeout: float) -> Any:
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="nova-uia", daemon=True)
                self._thread.start()
        reply: queue.SimpleQueue = queue.SimpleQueue()
        self._requests.put((fn, reply))
        try:
            return reply.get(timeout=timeout)
        except queue.Empty:
            _log.info("UI Automation didn't answer within %.0f ms", timeout * 1000)
            return None

    def focused(self, timeout: float = 0.25) -> Focus | None:
        """The focused control, or None if UI Automation didn't answer in time."""
        value = self._ask(self._read, timeout)
        return value if isinstance(value, Focus) else None

    def caret(self, timeout: float = 0.35, limit: int = 300) -> Caret | None:
        """The focused control and up to ``limit`` characters before its caret, or None if UI Automation didn't
        answer in time."""
        value = self._ask(lambda automation: self._caret(automation, limit), timeout)
        return value if isinstance(value, Caret) else None

    def close(self) -> None:
        if self._thread is not None:
            self._requests.put(None)

    def _run(self) -> None:
        native.com_init()
        automation = c_void_p()
        hr = native.ole32.CoCreateInstance(byref(_CLSID_CUIAutomation), None, 1, byref(_IID_IUIAutomation),
                                           byref(automation))
        if hr < 0:
            _log.warning("UI Automation unavailable (0x%08X)", hr & 0xFFFFFFFF)
        while True:
            item = self._requests.get()
            if item is None:
                break
            fn, reply = item
            if hr < 0:
                reply.put(None)
                continue
            try:
                reply.put(fn(automation))
            except Exception:  # noqa: BLE001 - one odd app must not kill the UIA thread
                _log.exception("UI Automation request failed")
                reply.put(None)
        native.com_release(automation)

    @staticmethod
    def _focused_element(automation: c_void_p) -> c_void_p | None:
        element = c_void_p()
        if native.com_call(automation, _GET_FOCUSED_ELEMENT, byref(element), types=(POINTER(c_void_p),)) < 0:
            return None
        return element if element else None

    @staticmethod
    def _describe(element: c_void_p) -> Focus:
        password, control, pid = wintypes.BOOL(), ctypes.c_int(), ctypes.c_int()
        hwnd, name = c_void_p(), c_void_p()
        native.com_call(element, _CURRENT_IS_PASSWORD, byref(password), types=(POINTER(wintypes.BOOL),))
        native.com_call(element, _CURRENT_CONTROL_TYPE, byref(control), types=(POINTER(ctypes.c_int),))
        native.com_call(element, _CURRENT_PROCESS_ID, byref(pid), types=(POINTER(ctypes.c_int),))
        native.com_call(element, _CURRENT_NATIVE_WINDOW_HANDLE, byref(hwnd), types=(POINTER(c_void_p),))
        native.com_call(element, _CURRENT_NAME, byref(name), types=(POINTER(c_void_p),))
        text = ctypes.wstring_at(name.value) if name.value else ""
        if name.value:
            _oleaut32.SysFreeString(name)
        return Focus(text[:200], control.value, bool(password.value), int(hwnd.value or 0), pid.value)

    @classmethod
    def _read(cls, automation: c_void_p) -> Focus | None:
        element = cls._focused_element(automation)
        if element is None:
            return None
        try:
            return cls._describe(element)
        except OSError:
            return None
        finally:
            native.com_release(element)

    @classmethod
    def _caret(cls, automation: c_void_p, limit: int) -> Caret | None:
        element = cls._focused_element(automation)
        if element is None:
            return None
        try:
            focus = cls._describe(element)
            return Caret(focus, None if focus.password else cls._before(element, limit))
        except OSError:
            return None
        finally:
            native.com_release(element)

    @staticmethod
    def _before(element: c_void_p, limit: int) -> str | None:
        """Text from ``limit`` characters before the start of the selection up to it (TextPattern)."""
        pattern, ranges, selection, before, text = c_void_p(), c_void_p(), c_void_p(), c_void_p(), c_void_p()
        try:
            if native.com_call(element, _GET_CURRENT_PATTERN_AS, _UIA_TEXT_PATTERN_ID,
                               byref(_IID_IUIAutomationTextPattern), byref(pattern),
                               types=(ctypes.c_int, POINTER(native.GUID), POINTER(c_void_p))) < 0 or not pattern:
                return None
            if native.com_call(pattern, _TEXT_GET_SELECTION, byref(ranges), types=(POINTER(c_void_p),)) < 0 \
                    or not ranges:
                return None
            count = ctypes.c_int()
            native.com_call(ranges, _ARRAY_LENGTH, byref(count), types=(POINTER(ctypes.c_int),))
            if count.value < 1:
                return None
            if native.com_call(ranges, _ARRAY_ELEMENT, 0, byref(selection),
                               types=(ctypes.c_int, POINTER(c_void_p))) < 0 or not selection:
                return None
            if native.com_call(selection, _RANGE_CLONE, byref(before), types=(POINTER(c_void_p),)) < 0 or not before:
                return None
            # collapse the copy onto the start of the selection, then stretch its start back ``limit`` characters
            native.com_call(before, _RANGE_MOVE_BY_RANGE, _END, selection, _START,
                            types=(ctypes.c_int, c_void_p, ctypes.c_int))
            moved = ctypes.c_int()
            native.com_call(before, _RANGE_MOVE_BY_UNIT, _START, _CHARACTER, -limit, byref(moved),
                            types=(ctypes.c_int, ctypes.c_int, ctypes.c_int, POINTER(ctypes.c_int)))
            if native.com_call(before, _RANGE_GET_TEXT, limit + 16, byref(text),
                               types=(ctypes.c_int, POINTER(c_void_p))) < 0:
                return None
            value = ctypes.wstring_at(text.value) if text.value else ""
            return value[-limit:]
        finally:
            if text.value:
                _oleaut32.SysFreeString(text)
            for pointer in (before, selection, ranges, pattern):
                native.com_release(pointer)
