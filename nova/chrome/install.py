"""`nova chrome --install`: register Nova's native-messaging host with Chrome (SPEC §9).

Writes ``data/chrome-bridge.json`` (pipe name + key, made once), the host manifest
``extension/host/com.nova.bridge.json`` (only Nova's extension id may start the host) and
``HKCU\\Software\\Google\\Chrome\\NativeMessagingHosts\\com.nova.bridge`` -> that manifest.
The one step left is Bhavya's: load ``extension/`` unpacked in chrome://extensions.
The extension id is fixed by the manifest's ``key`` (its private key is git-ignored).
"""

from __future__ import annotations

import base64
import hashlib
import json
import winreg
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import paths
from .bridge import BridgeInfo

HOST_NAME = "com.nova.bridge"
EXTENSION = paths.ROOT / "extension"
HOST_DIR = EXTENSION / "host"
HOST_BAT = HOST_DIR / "nova_host.bat"
HOST_MANIFEST = HOST_DIR / f"{HOST_NAME}.json"
VENV_PYTHON = paths.ROOT / ".venv" / "Scripts" / "python.exe"
# Where each Chromium build looks for native-messaging hosts (per user). Google Chrome is the real one; the
# others are for testing with Chrome for Testing / Chromium (they still honour --load-extension).
REGISTRY = {
    "chrome": r"Software\Google\Chrome\NativeMessagingHosts",
    "testing": r"Software\Google\Chrome for Testing\NativeMessagingHosts",
    "chromium": r"Software\Chromium\NativeMessagingHosts",
}
MANUAL_STEP = (
    "One manual step: open chrome://extensions > turn on Developer mode (top right) > Load unpacked > "
    f"pick {EXTENSION}"
)


def extension_id(key_b64: str) -> str:
    """Chrome's id for an extension with this manifest ``key``: SHA-256 of the public key, first 32 hex
    digits written with the letters a-p."""
    digest = hashlib.sha256(base64.b64decode(key_b64)).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def manifest_key(manifest: Path = EXTENSION / "manifest.json") -> str:
    return str(json.loads(manifest.read_text(encoding="utf-8"))["key"])


def host_manifest(bat: Path, ext_id: str) -> dict:
    return {
        "name": HOST_NAME,
        "description": "Nova: relays the Chrome extension to Nova on this laptop",
        "path": str(bat),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{ext_id}/"],
    }


def _registered(browser: str) -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{REGISTRY[browser]}\{HOST_NAME}") as key:
            return str(winreg.QueryValueEx(key, "")[0])
    except OSError:
        return None


def install(browsers: Sequence[str] = ("chrome",)) -> list[str]:
    """Do it; return what was done, in plain lines."""
    lines = []
    info = BridgeInfo.ensure(paths.CHROME_BRIDGE)
    lines.append(f"Bridge: {paths.CHROME_BRIDGE} (pipe {info.pipe})")
    ext_id = extension_id(manifest_key())
    HOST_MANIFEST.write_text(json.dumps(host_manifest(HOST_BAT, ext_id), indent=2) + "\n", encoding="utf-8")
    lines.append(f"Host manifest: {HOST_MANIFEST} (extension id {ext_id})")
    for browser in browsers:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"{REGISTRY[browser]}\{HOST_NAME}") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(HOST_MANIFEST))
        lines.append(rf"Registry: HKCU\{REGISTRY[browser]}\{HOST_NAME}")
    if not VENV_PYTHON.is_file():
        lines.append(f"Warning: {VENV_PYTHON} is missing — run `uv sync` first; the host runs on it.")
    return lines


def uninstall(browsers: Sequence[str] = ("chrome",)) -> list[str]:
    lines = []
    for browser in browsers:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"{REGISTRY[browser]}\{HOST_NAME}")
            lines.append(rf"Removed HKCU\{REGISTRY[browser]}\{HOST_NAME}")
        except OSError:
            lines.append(rf"Not registered for {browser}")
    return lines


@dataclass(frozen=True)
class Status:
    ok: bool
    detail: str
    fix: str = ""


def status(profiles_root: Path | None = None) -> Status:
    """For `nova doctor`: registered, files in place, and loaded in a Chrome profile."""
    ext_id = extension_id(manifest_key())
    registered = _registered("chrome")
    if registered is None or Path(registered) != HOST_MANIFEST or not HOST_MANIFEST.is_file():
        return Status(False, "native host not registered", "Run: uv run nova chrome --install")
    if not VENV_PYTHON.is_file() or not HOST_BAT.is_file():
        return Status(False, f"host files missing ({HOST_BAT}, {VENV_PYTHON})", "Run `uv sync`, then nova chrome --install.")
    if BridgeInfo.load(paths.CHROME_BRIDGE) is None:
        return Status(False, f"{paths.CHROME_BRIDGE} is missing", "Run: uv run nova chrome --install")
    profiles = loaded_in(ext_id, profiles_root)
    if not profiles:
        return Status(False, f"host ready; extension {ext_id} not loaded in Chrome yet", MANUAL_STEP)
    return Status(True, f"{ext_id} loaded ({', '.join(profiles)})")


def loaded_in(ext_id: str, profiles_root: Path | None = None) -> list[str]:
    """Chrome profiles whose preferences list the extension (read-only; Chrome may be running)."""
    import os

    root = profiles_root or Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
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
