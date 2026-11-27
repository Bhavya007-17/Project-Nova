"""Start with Windows (SPEC §6 tray): one value under the per-user Run key. No admin, no service, no shortcut file.

The command is this checkout's own ``pythonw.exe -m nova`` (no console window), so moving the folder turns it off
until it's toggled again: ``enabled`` is true only when the stored command is the current one.
"""

from __future__ import annotations

import sys
from pathlib import Path

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_NAME = "Nova"


def command(python: Path | str | None = None) -> str:
    """The line Windows runs at sign-in: the windowless interpreter next to ``python`` when there is one."""
    exe = Path(python or sys.executable)
    windowless = exe.with_name("pythonw.exe")
    if exe.name.lower() == "python.exe" and windowless.exists():
        exe = windowless
    return f'"{exe}" -m nova'


def enabled() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, _NAME)
    except OSError:
        return False
    return str(value) == command()


def set_enabled(on: bool) -> None:
    """Raises OSError when the registry says no."""
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, _NAME, 0, winreg.REG_SZ, command())
        else:
            try:
                winreg.DeleteValue(key, _NAME)
            except FileNotFoundError:
                pass
