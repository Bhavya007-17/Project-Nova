"""UI Automation: the focused control (name, control type, password flag), SPEC §7.1 / §8.

Asked on its own thread (multithreaded apartment) with a deadline, so a slow or hung app
can't stall the brain. Only asked when it matters (before typing), because asking Chrome
turns on its accessibility tree, which costs it some CPU.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import threading
from ctypes import POINTER, byref, c_void_p, wintypes
from dataclasses import dataclass

from . import native

_log = logging.getLogger(__name__)

_CLSID_CUIAutomation = native.guid("{FF48DBA4-60EF-4201-AA87-54103EEF594E}")
_IID_IUIAutomation = native.guid("{30CBE57D-D9D0-452A-AB13-7AC5AC4825EE}")
_oleaut32 = ctypes.WinDLL("oleaut32")
_oleaut32.SysFreeString.argtypes = [c_void_p]
_oleaut32.SysFreeString.restype = None
# vtable slots (UIAutomationClient.h)
_GET_FOCUSED_ELEMENT = 8
_CURRENT_PROCESS_ID, _CURRENT_CONTROL_TYPE, _CURRENT_NAME = 20, 21, 23
_CURRENT_IS_PASSWORD, _CURRENT_NATIVE_WINDOW_HANDLE = 35, 36
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


class FocusReader:
    def __init__(self) -> None:
        self._requests: queue.SimpleQueue[queue.SimpleQueue | None] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def focused(self, timeout: float = 0.25) -> Focus | None:
        """The focused control, or None if UI Automation didn't answer in time."""
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="nova-uia", daemon=True)
                self._thread.start()
        reply: queue.SimpleQueue = queue.SimpleQueue()
        self._requests.put(reply)
        try:
            value = reply.get(timeout=timeout)
        except queue.Empty:
            _log.info("UI Automation didn't answer within %.0f ms", timeout * 1000)
            return None
        return value if isinstance(value, Focus) else None

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
            reply = self._requests.get()
            if reply is None:
                break
            if hr < 0:
                reply.put(None)
                continue
            reply.put(self._read(automation))
        native.com_release(automation)

    @staticmethod
    def _read(automation: c_void_p) -> Focus | None:
        element = c_void_p()
        try:
            if native.com_call(automation, _GET_FOCUSED_ELEMENT, byref(element), types=(POINTER(c_void_p),)) < 0:
                return None
            if not element:
                return None
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
        except OSError:
            return None
        finally:
            native.com_release(element)
