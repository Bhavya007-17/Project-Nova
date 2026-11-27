"""Which OS backend Nova runs on.

Nova was built on Windows (Win32 hands, low-level keyboard hook, UI Automation). On macOS the same public APIs
come from ``nova.macos`` (Quartz event taps, Accessibility, NSWorkspace), swapped in at import time by
``nova.macos.install_redirects``. ``NOVA_WIN_STUBS=1`` (set by ``conftest.py``) keeps the Windows modules even on a
Mac, so the unit tests exercise the Windows code with stubbed DLLs exactly as before.
"""

from __future__ import annotations

import os
import sys

WINDOWS = sys.platform == "win32"
MAC = sys.platform == "darwin" and not os.environ.get("NOVA_WIN_STUBS")
