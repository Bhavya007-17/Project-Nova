"""macOS stand-in for ``nova.chrome.install``: the native-messaging host manifest goes in Chrome's per-user
NativeMessagingHosts folder (no registry on a Mac); the host is ``extension/host/nova_host.sh``."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import paths
from ..chrome.bridge import BridgeInfo

HOST_NAME = "com.nova.bridge"
EXTENSION = paths.ROOT / "extension"
HOST_DIR = EXTENSION / "host"
HOST_BAT = HOST_DIR / "nova_host.sh"
VENV_PYTHON = paths.ROOT / ".venv" / "bin" / "python"
_SUPPORT = Path.home() / "Library" / "Application Support"
FOLDERS = {
    "chrome": _SUPPORT / "Google" / "Chrome" / "NativeMessagingHosts",
    "testing": _SUPPORT / "Google" / "Chrome for Testing" / "NativeMessagingHosts",
    "chromium": _SUPPORT / "Chromium" / "NativeMessagingHosts",
}
PROFILES = _SUPPORT / "Google" / "Chrome"
HOST_MANIFEST = FOLDERS["chrome"] / f"{HOST_NAME}.json"
MANUAL_STEP = (
    "One manual step: open chrome://extensions > turn on Developer mode (top right) > Load unpacked > "
    f"pick {EXTENSION}"
)


def extension_id(key_b64: str) -> str:
    import base64
    import hashlib

    digest = hashlib.sha256(base64.b64decode(key_b64)).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def manifest_key(manifest: Path = EXTENSION / "manifest.json") -> str:
    return str(json.loads(manifest.read_text(encoding="utf-8"))["key"])


def host_manifest(script: Path, ext_id: str) -> dict:
    return {
        "name": HOST_NAME,
        "description": "Nova: relays the Chrome extension to Nova on this Mac",
        "path": str(script),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{ext_id}/"],
    }


def install(browsers: Sequence[str] = ("chrome",)) -> list[str]:
    lines = []
    info = BridgeInfo.ensure(paths.CHROME_BRIDGE)
    lines.append(f"Bridge: {paths.CHROME_BRIDGE} (socket {info.pipe})")
    ext_id = extension_id(manifest_key())
    os.chmod(HOST_BAT, 0o755)
    for browser in browsers:
        folder = FOLDERS[browser]
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{HOST_NAME}.json"
        target.write_text(json.dumps(host_manifest(HOST_BAT, ext_id), indent=2) + "\n", encoding="utf-8")
        lines.append(f"Host manifest: {target} (extension id {ext_id})")
    if not VENV_PYTHON.is_file():
        lines.append(f"Warning: {VENV_PYTHON} is missing — run `uv sync` first; the host runs on it.")
    return lines


def uninstall(browsers: Sequence[str] = ("chrome",)) -> list[str]:
    lines = []
    for browser in browsers:
        target = FOLDERS[browser] / f"{HOST_NAME}.json"
        if target.exists():
            target.unlink()
            lines.append(f"Removed {target}")
        else:
            lines.append(f"Not registered for {browser}")
    return lines


@dataclass(frozen=True)
class Status:
    ok: bool
    detail: str
    fix: str = ""


def status(profiles_root: Path | None = None) -> Status:
    ext_id = extension_id(manifest_key())
    try:
        registered = json.loads(HOST_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Status(False, "native host not registered", "Run: uv run nova chrome --install")
    if Path(registered.get("path", "")) != HOST_BAT:
        return Status(False, "native host points elsewhere", "Run: uv run nova chrome --install")
    if not VENV_PYTHON.is_file() or not HOST_BAT.is_file():
        return Status(False, f"host files missing ({HOST_BAT}, {VENV_PYTHON})", "Run `uv sync`, then nova chrome --install.")
    if BridgeInfo.load(paths.CHROME_BRIDGE) is None:
        return Status(False, f"{paths.CHROME_BRIDGE} is missing", "Run: uv run nova chrome --install")
    profiles = loaded_in(ext_id, profiles_root)
    if not profiles:
        return Status(False, f"host ready; extension {ext_id} not loaded in Chrome yet", MANUAL_STEP)
    return Status(True, f"{ext_id} loaded ({', '.join(profiles)})")


def loaded_in(ext_id: str, profiles_root: Path | None = None) -> list[str]:
    root = profiles_root or PROFILES
    names = []
    try:
        folders = [p for p in root.iterdir() if p.is_dir() and (p.name == "Default" or p.name.startswith("Profile "))]
    except OSError:
        return []
    for folder in folders:
        for file in ("Secure Preferences", "Preferences"):
            try:
                data = json.loads((folder / file).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            settings = ((data.get("extensions") or {}).get("settings") or {}) if isinstance(data, dict) else {}
            if ext_id in settings:
                names.append(folder.name)
                break
    return names
