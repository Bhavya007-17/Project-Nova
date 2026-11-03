"""Console helpers shared by the CLI commands."""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes


def enable_vt() -> bool:
    """Turn on ANSI escapes (colors, in-place lines) on a real console. False if not a console or NO_COLOR."""
    if "NO_COLOR" in os.environ or sys.stdout is None or not sys.stdout.isatty():
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
    mode = wintypes.DWORD()
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return False
    return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
