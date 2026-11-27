"""Lets the pure-logic tests run off Windows (macOS / Linux dev machines).

Nova itself is Windows-only; many modules bind Win32 DLLs at import time. Off Windows this file swaps in
inert stand-ins so the logic tests can be collected. On Windows it does nothing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Keep the Windows modules (with the stubs below) even on a Mac: see nova/platform.py.
os.environ.setdefault("NOVA_WIN_STUBS", "1")

sys.path.insert(0, str(Path(__file__).parent))

if sys.platform != "win32":
    import ctypes
    from unittest.mock import MagicMock

    class _Func:
        """A DLL function: takes argtypes / restype, returns 0."""

        def __call__(self, *_args, **_kwargs):
            return 0

    class _Dll:
        def __getattr__(self, name):
            func = _Func()
            self.__dict__[name] = func
            return func

        __getitem__ = __getattr__

    def _dll(*_args, **_kwargs):
        return _Dll()

    ctypes.WinDLL = _dll  # type: ignore[attr-defined]
    ctypes.OleDLL = _dll  # type: ignore[attr-defined]
    ctypes.windll = _Dll()  # type: ignore[attr-defined]
    ctypes.oledll = _Dll()  # type: ignore[attr-defined]
    ctypes.WINFUNCTYPE = ctypes.CFUNCTYPE  # type: ignore[attr-defined]
    ctypes.HRESULT = ctypes.c_long  # type: ignore[attr-defined]
    ctypes.WinError = lambda *a, **k: OSError(*a)  # type: ignore[attr-defined]
    ctypes.get_last_error = lambda: 0  # type: ignore[attr-defined]
    ctypes.set_last_error = lambda _v: 0  # type: ignore[attr-defined]
    ctypes.FormatError = lambda *_a: "error"  # type: ignore[attr-defined]

    for name in ("winreg", "winsound"):
        if name not in sys.modules:
            sys.modules[name] = MagicMock(name=name)

    # Windows is LLP64 (long = 32 bits); macOS / Linux are LP64. Keep the struct layouts Windows-sized.
    from ctypes import wintypes

    for _name in ("DWORD", "ULONG"):
        setattr(wintypes, _name, ctypes.c_uint32)
    for _name in ("LONG", "BOOL", "HRESULT"):
        setattr(wintypes, _name, ctypes.c_int32)
    wintypes.LPDWORD = wintypes.PDWORD = ctypes.POINTER(ctypes.c_uint32)

    class _Point(ctypes.Structure):
        _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]

    class _Rect(ctypes.Structure):
        _fields_ = [("left", ctypes.c_int32), ("top", ctypes.c_int32),
                    ("right", ctypes.c_int32), ("bottom", ctypes.c_int32)]

    wintypes.POINT = _Point
    wintypes.RECT = _Rect
    ctypes.c_ulong = ctypes.c_uint32  # type: ignore[misc]
    ctypes.c_long = ctypes.c_int32  # type: ignore[misc]

    # Opens a real Qt Quick window through Win32 calls; it aborts the interpreter off Windows.
    collect_ignore = ["tests/test_island_qt.py"]
