"""macOS stand-in for ``nova.startup``: "Start at login" as a per-user LaunchAgent (no admin)."""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "com.nova.voicelayer"
AGENT = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def command(python: Path | str | None = None) -> list[str]:
    return [str(python or sys.executable), "-m", "nova"]


def _plist() -> dict:
    root = Path(__file__).resolve().parent.parent.parent
    return {"Label": LABEL, "ProgramArguments": command(), "RunAtLoad": True, "WorkingDirectory": str(root),
            "ProcessType": "Interactive"}


def enabled() -> bool:
    try:
        data = plistlib.loads(AGENT.read_bytes())
    except (OSError, plistlib.InvalidFileException):
        return False
    return data.get("ProgramArguments") == command()


def set_enabled(on: bool) -> None:
    if on:
        AGENT.parent.mkdir(parents=True, exist_ok=True)
        AGENT.write_bytes(plistlib.dumps(_plist()))
    else:
        if AGENT.exists():
            subprocess.run(["/bin/launchctl", "bootout", f"gui/{_uid()}", str(AGENT)], capture_output=True, check=False)
            AGENT.unlink()


def _uid() -> int:
    import os

    return os.getuid()
