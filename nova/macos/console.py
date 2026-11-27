"""macOS stand-in for ``nova.console``: terminals already speak ANSI."""

from __future__ import annotations

import os
import sys


def enable_vt() -> bool:
    return "NO_COLOR" not in os.environ and sys.stdout is not None and sys.stdout.isatty()
