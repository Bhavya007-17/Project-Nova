"""`nova doctor`: one table of what this laptop has for Nova.

Read-only: never downloads, installs or changes anything. Probes run in
parallel; each failing row says what to do.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import winreg
from collections.abc import Callable, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import paths
from .brain.llm import is_cloud
from .config import ConfigStore
from .console import enable_vt
from .keys import MOD_NOREPEAT, Chord, parse_chord


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""
    optional: bool = False  # missing is fine: Nova works without it (shown as OPT, never fails the doctor)


Probe = Callable[[], "Check | Sequence[Check]"]


# --- runner and table ------------------------------------------------------------


def run_checks(probes: Sequence[tuple[str, Probe]], timeout: float = 30.0) -> list[Check]:
    """Run probes in parallel on daemon threads (a hung probe can't hang the doctor)."""
    pending = object()
    results: list[Any] = [pending] * len(probes)

    def work(index: int, probe: Probe) -> None:
        try:
            results[index] = probe()
        except Exception as error:  # noqa: BLE001 - every failure becomes a table row
            results[index] = error

    threads = [
        threading.Thread(target=work, args=(i, probe), name=f"doctor-{name}", daemon=True)
        for i, (name, probe) in enumerate(probes)
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))

    checks: list[Check] = []
    for (name, _probe), result in zip(probes, results, strict=True):
        if result is pending:
            checks.append(Check(name, False, f"no answer within {timeout:.0f} s", "Run nova doctor again."))
        elif isinstance(result, Exception):
            checks.append(
                Check(name, False, f"check crashed: {type(result).__name__}: {result}",
                      "Run `nova doctor` again; if this row stays red, paste it to Claude Code.")
            )
        elif isinstance(result, Check):
            checks.append(result)
        else:
            checks.extend(result)
    return checks


_GREEN, _RED, _YELLOW, _RESET = "\x1b[32m", "\x1b[31m", "\x1b[33m", "\x1b[0m"


def render(checks: Sequence[Check], *, color: bool) -> str:
    width = max((len(check.name) for check in checks), default=0)
    lines = []
    for check in checks:
        status, tint = ("OK ", _GREEN) if check.ok else ("OPT", _YELLOW) if check.optional else ("FIX", _RED)
        if color:
            status = f"{tint}{status}{_RESET}"
        lines.append(f"{check.name:<{width}}  {status}  {check.detail}")
        if not check.ok and check.fix:
            lines.append(f"{'':<{width}}       fix: {check.fix}")
    return "\n".join(lines)


def exit_code(checks: Sequence[Check]) -> int:
    return 0 if all(check.ok or check.optional for check in checks) else 1


def summary(checks: Sequence[Check]) -> str:
    problems = sum(not check.ok and not check.optional for check in checks)
    optional = sum(not check.ok and check.optional for check in checks)
    text = "All good." if not problems else f"{problems} to fix (see the fix: lines)."
    if optional:
        one = optional == 1
        text += f" {optional} optional {'item' if one else 'items'} missing (Nova works without {'it' if one else 'them'})."
    return text


# --- probes ----------------------------------------------------------------------


def check_python(version: Sequence[int] = tuple(sys.version_info[:3]), executable: str = sys.executable) -> Check:
    text = ".".join(str(part) for part in version[:3])
    if tuple(version[:2]) == (3, 12):
        return Check("Python", True, f"{text} ({executable})")
    return Check("Python", False, f"{text}; Nova needs 3.12", "Run Nova through uv (`uv run nova ...`); it uses the pinned 3.12.")


def check_uv() -> Check:
    exe = shutil.which("uv")
    if exe is None:
        return Check("uv", False, "not on PATH", "Install uv: winget install --id=astral-sh.uv -e")
    result = subprocess.run(
        [exe, "--version"], capture_output=True, text=True, timeout=10, check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return Check("uv", True, result.stdout.strip())


def pe_subsystem(exe: Path) -> int | None:
    """PE header Subsystem field: 2 = windowed (GUI), 3 = console. None if not a readable PE file."""
    try:
        with exe.open("rb") as file:
            head = file.read(4096)
    except OSError:
        return None
    if head[:2] != b"MZ" or len(head) < 0x40:
        return None
    pe = int.from_bytes(head[0x3C:0x40], "little")
    field = pe + 24 + 68  # after "PE\0\0" + COFF header; same offset in PE32 and PE32+
    if head[pe:pe + 4] != b"PE\0\0" or len(head) < field + 2:
        return None
    return int.from_bytes(head[field:field + 2], "little")


def check_windowed_launcher(pythonw: Path = Path(sys.executable).with_name("pythonw.exe")) -> Check:
    """The tray runs under pythonw; uv 0.11 made venvs whose pythonw.exe is a console exe (uv bug #19226)."""
    subsystem = pe_subsystem(pythonw)
    if subsystem == 2:
        return Check("No-console run", True, f"{pythonw.name} is windowed")
    fix = (
        "Update uv (fixed in 0.12): `py -3.11 -m pip install -U uv` for the pip copy, `uv self update` for a "
        "standalone one; `Get-Command uv -All` shows which you have. Then delete .venv and run `uv sync`."
    )
    if subsystem is None:
        return Check("No-console run", False, f"{pythonw} is missing or unreadable", fix)
    return Check("No-console run", False, f"{pythonw} is a console exe, so `novaw` would open a console window", fix)


def check_settings(store: ConfigStore) -> Check:
    store.load()
    if store.error:
        return Check(
            "Settings", False, store.error,
            f"Edit {store.path}; until it's fixed Nova uses the last good settings (defaults on first start).",
        )
    return Check("Settings", True, str(store.path))


_CONSENT = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"


def mic_consent() -> dict[str, str | None]:
    """Windows microphone privacy switches: Allow / Deny / None (not set)."""
    places = {
        "device": (winreg.HKEY_LOCAL_MACHINE, _CONSENT),
        "user": (winreg.HKEY_CURRENT_USER, _CONSENT),
        "desktop apps": (winreg.HKEY_CURRENT_USER, _CONSENT + r"\NonPackaged"),
    }
    values: dict[str, str | None] = {}
    for label, (hive, key) in places.items():
        try:
            with winreg.OpenKey(hive, key) as handle:
                values[label] = str(winreg.QueryValueEx(handle, "Value")[0])
        except OSError:
            values[label] = None
    return values


def check_mic(sd: Any = None, consent: dict[str, str | None] | None = None) -> Check:
    """Lists devices only; never opens the mic."""
    if sd is None:
        import sounddevice as sd
    if consent is None:
        consent = mic_consent()
    devices = list(sd.query_devices())
    hostapis = list(sd.query_hostapis())
    # WASAPI has the full device names (MME cuts them at 31 characters).
    api = next((i for i, host in enumerate(hostapis) if host["name"] == "Windows WASAPI"), 0 if hostapis else None)
    default = hostapis[api]["default_input_device"] if api is not None else -1
    inputs = {d["name"] for d in devices if d["max_input_channels"] > 0 and d["hostapi"] == api}
    if not inputs or not 0 <= default < len(devices):
        return Check("Microphone", False, "no input device", "Plug in or enable a mic: Settings > System > Sound > Input.")
    name = devices[default]["name"]
    denied = [place for place, value in consent.items() if value == "Deny"]
    if denied:
        return Check(
            "Microphone", False, f"{name}, but Windows privacy blocks it ({', '.join(denied)})",
            "Settings > Privacy & security > Microphone: turn on Microphone access and "
            "Let desktop apps access your microphone.",
        )
    return Check("Microphone", True, f"default: {name}; {len(inputs)} inputs")


def check_gpu(nvml: Any = None) -> Check:
    if nvml is None:
        import pynvml as nvml
    try:
        nvml.nvmlInit()
    except nvml.NVMLError as error:
        return Check(
            "GPU", False, f"NVIDIA driver not answering ({error})",
            "Update the NVIDIA driver. Nova still works; the LLM will run on CPU.",
        )
    try:
        handle = nvml.nvmlDeviceGetHandleByIndex(0)
        name = nvml.nvmlDeviceGetName(handle)
        memory = nvml.nvmlDeviceGetMemoryInfo(handle)
    finally:
        nvml.nvmlShutdown()
    if isinstance(name, bytes):
        name = name.decode()
    gb = 1024**3
    return Check("GPU", True, f"{name}: {memory.free / gb:.1f} of {memory.total / gb:.1f} GB free")


_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _fetch_json(url: str, timeout: float) -> Any:
    # Never route the local Ollama call through a system proxy.
    with _NO_PROXY.open(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def check_ollama(url: str, fetch: Callable[[str, float], Any] = _fetch_json) -> Check:
    base = url.rstrip("/")
    try:
        version = fetch(base + "/api/version", 2.0).get("version", "?")
        models = sorted(model.get("name", "?") for model in fetch(base + "/api/tags", 2.0).get("models", []))
    except (OSError, ValueError) as error:
        # Windows takes ~2 s to refuse a loopback connect, so a stopped Ollama often shows up as a timeout.
        reason = getattr(error, "reason", error)
        return Check(
            "Ollama", False, f"nothing answering at {base}; probably not running ({reason})",
            "Start Ollama (Start menu > Ollama, or `ollama serve`), then run nova doctor again.",
        )
    # "-cloud" tags run on Ollama's servers; local-first means Nova must never pick them.
    cloud = [name for name in models if is_cloud(name)]
    local = [name for name in models if not is_cloud(name)]
    detail = f"{version} at {base}; models: {', '.join(local) or 'none pulled yet'}"
    if cloud:
        detail += f"; cloud (Nova never uses): {', '.join(cloud)}"
    return Check("Ollama", True, detail)


def check_llm(url: str, gpu_model: str, cpu_model: str, fetch: Callable[[str, float], Any] = _fetch_json) -> Check:
    """Tier 1's models (settings.yaml llm:) are pulled."""
    try:
        pulled = {model.get("name") for model in fetch(url.rstrip("/") + "/api/tags", 2.0).get("models", [])}
    except (OSError, ValueError):
        return Check("Local LLM", False, "can't ask Ollama which models are pulled", "Start Ollama first.")
    wanted = dict.fromkeys((gpu_model, cpu_model))
    missing = [m for m in wanted if m not in pulled]
    if missing:
        return Check("Local LLM", False, "not pulled: " + ", ".join(missing),
                     "Run: " + " ; ".join(f"ollama pull {m}" for m in missing))
    same = gpu_model == cpu_model
    detail = f"{gpu_model} (GPU and CPU)" if same else f"GPU {gpu_model}, CPU {cpu_model}"
    return Check("Local LLM", True, detail + " — loads on demand, unloads when idle")


_APP_PATHS = r"Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
_VERSION_DIR = re.compile(r"^\d+\.\d+\.\d+\.\d+$")


def find_chrome() -> Path | None:
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, _APP_PATHS) as handle:
                value = str(winreg.QueryValueEx(handle, "")[0])
        except OSError:
            continue
        exe = Path(value.strip().strip('"'))
        if exe.is_file():
            return exe
    return None


def chrome_version(app_dir: Path) -> str | None:
    """Chrome keeps its files in a folder named after the version, next to chrome.exe."""
    try:
        names = [entry.name for entry in app_dir.iterdir() if entry.is_dir() and _VERSION_DIR.match(entry.name)]
    except OSError:
        return None
    return max(names, key=lambda name: tuple(int(part) for part in name.split(".")), default=None)


def check_chrome(find: Callable[[], Path | None] = find_chrome) -> Check:
    exe = find()
    if exe is None:
        return Check("Chrome", False, "not found", "Install Google Chrome; Nova's page actions go through a Chrome extension.")
    return Check("Chrome", True, f"{chrome_version(exe.parent) or 'version unknown'} ({exe})")


def check_extension(state: Callable[[], Any] | None = None) -> Check:
    """Nova's Chrome extension: native host registered, files in place, loaded in a Chrome profile."""
    if state is None:
        from .chrome import install

        state = install.status
    found = state()
    return Check("Chrome extension", found.ok, found.detail, found.fix)


@dataclass(frozen=True)
class WantedApp:
    label: str
    start_name: str
    aumid: str
    needed_for: str


WANTED_APPS = (
    WantedApp("Claude app", "Claude", "Claude_pzs8sxrjxfjjc!Claude", "handoff"),
    WantedApp("ChatGPT app", "ChatGPT", "OpenAI.Codex_2p2nqsd0c76g0!App", "handoff"),
    WantedApp("Fusion", "Autodesk Fusion", "AutodeskInc.Fusion360", "opening and arranging Fusion"),
)

_START_APPS = (
    "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
    "ConvertTo-Json -InputObject @(Get-StartApps | Select-Object Name, AppID) -Compress"
)


def start_apps() -> Any:
    """Start-menu apps as [{Name, AppID}] via a fixed PowerShell command."""
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-Command", _START_APPS],
        capture_output=True, encoding="utf-8-sig", timeout=30, check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return json.loads(result.stdout or "[]")


def match_apps(rows: Any, wanted: Sequence[WantedApp] = WANTED_APPS) -> list[Check]:
    if isinstance(rows, dict):  # ConvertTo-Json gives an object, not a list, for one app
        rows = [rows]
    rows = [row for row in rows if isinstance(row, dict)]
    ids = {row.get("AppID") for row in rows}
    checks = []
    for app in wanted:
        if app.aumid in ids:
            checks.append(Check(app.label, True, app.aumid))
            continue
        named = next((row for row in rows if str(row.get("Name", "")).casefold() == app.start_name.casefold()), None)
        if named is not None:
            checks.append(Check(app.label, True, f"{named.get('AppID')} (AGENTS.md says {app.aumid}; update it)"))
        else:
            checks.append(
                Check(app.label, False, f"not in the Start menu (looked for {app.aumid})",
                      f"Install {app.start_name} if you want {app.needed_for}.", optional=True)
            )
    return checks


def check_apps(fetch: Callable[[], Any] = start_apps) -> list[Check]:
    return match_apps(fetch())


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
_user32.RegisterHotKey.restype = wintypes.BOOL
_user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.UnregisterHotKey.restype = wintypes.BOOL
_PROBE_ID = 0x4E56


def hotkey_free(chord: Chord) -> bool:
    """Try to register the chord for this thread, then release it at once."""
    if not _user32.RegisterHotKey(None, _PROBE_ID, chord.modifiers | MOD_NOREPEAT, chord.vk):
        return False
    _user32.UnregisterHotKey(None, _PROBE_ID)
    return True


def check_chord(text: str, is_free: Callable[[Chord], bool] = hotkey_free) -> Check:
    try:
        chord = parse_chord(text)
    except ValueError as error:
        return Check("Fallback chord", False, str(error), "Set `chord:` in config/settings.yaml, e.g. ctrl+alt+n.")
    if is_free(chord):
        return Check("Fallback chord", True, f"{chord.label} is free")
    return Check(
        "Fallback chord", False, f"{chord.label} is already taken by another app",
        "Quit the app that owns it, or pick another `chord:` in config/settings.yaml (e.g. ctrl+alt+space).",
    )


def try_keyboard_hook() -> str | None:
    """Install the low-level keyboard hook for a moment. None = fine, else what Windows said."""
    from .hook import KeyHook

    hook = KeyHook(lambda key: False, name="doctor-keys")
    try:
        return None if hook.start(timeout=3.0) else (hook.error or "no answer from the hook thread")
    finally:
        hook.close()


def check_keyboard_hook(attempt: Callable[[], str | None] = try_keyboard_hook) -> Check:
    error = attempt()
    if error is None:
        return Check("Keyboard hook", True, "Copilot key and chord can be caught (WH_KEYBOARD_LL)")
    return Check(
        "Keyboard hook", False, error,
        'Something blocks low-level keyboard hooks (security software?). "Nova ..." still works without it.',
    )


REQUIRED_MODELS = (
    "parakeet/encoder-model.int8.onnx",
    "parakeet/decoder_joint-model.int8.onnx",
    "parakeet/vocab.txt",
    "parakeet/config.json",
    "silero/silero_vad.onnx",
)


def _size(n: int) -> str:
    if n >= 100 * 1024**2:
        return f"{n / 1024**2:.0f} MB"
    if n >= 1024**2:
        return f"{n / 1024**2:.1f} MB"
    return f"{n / 1024:.0f} KB"


def _nonempty(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def check_models(models_dir: Path) -> Check:
    missing = [rel for rel in REQUIRED_MODELS if not _nonempty(models_dir / rel)]
    if missing:
        return Check(
            "Models", False, f"missing in {models_dir}: {', '.join(missing)}",
            "Run scripts/setup.ps1, or just `uv run python scripts/get_models.py` (~670 MB, checked by SHA-256). "
            "Doctor itself never downloads.",
        )
    folders = sorted(entry for entry in models_dir.iterdir() if entry.is_dir())
    sizes = [f"{folder.name} {_size(sum(f.stat().st_size for f in folder.rglob('*') if f.is_file()))}" for folder in folders]
    return Check("Models", True, ", ".join(sizes))


_AUDIO = frozenset({".wav", ".flac", ".mp3", ".ogg", ".m4a"})


def check_recordings(recordings_dir: Path) -> Check:
    fix = (f"Only `nova bench-stt` (the speech-to-text WER eval) uses them. They're private voice recordings, "
           f"kept off GitHub: put session folders (audio + manifest.json) in {recordings_dir} to run it.")
    if not recordings_dir.is_dir():
        return Check("Recordings", False, f"{recordings_dir} is missing", fix, optional=True)
    sessions = sum(1 for _ in recordings_dir.rglob("manifest.json"))
    audio = sum(1 for f in recordings_dir.rglob("*") if f.suffix.lower() in _AUDIO)
    if not sessions:
        return Check("Recordings", False, f"no sessions (manifest.json) in {recordings_dir}", fix, optional=True)
    return Check("Recordings", True, f"{sessions} sessions, {audio} audio files")


def check_vocab(path: Path) -> Check:
    """Read-only: parses config/vocab.yaml without creating it."""
    from .vocab import VocabError, parse_vocab

    if not path.is_file():
        return Check("Vocab", True, f"{path.name} not created yet (Nova writes the starter list on first use)")
    try:
        terms = parse_vocab(path.read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, VocabError) as error:
        return Check("Vocab", False, f"{path.name}: {error}", f"Fix {path}; until then Nova uses the last good names.")
    return Check("Vocab", True, f"{len(terms)} names in {path.name}")


# --- entry point -------------------------------------------------------------------


def main() -> int:
    # Problems show up as table rows; keep library warnings from also printing raw to stderr.
    logging.getLogger("nova").addHandler(logging.NullHandler())
    store = ConfigStore(paths.SETTINGS)
    settings_check = check_settings(store)  # first, so the other probes see the real settings
    settings = store.settings
    checks = run_checks(
        [
            ("Python", check_python),
            ("uv", check_uv),
            ("No-console run", check_windowed_launcher),
            ("Settings", lambda: settings_check),
            ("Microphone", check_mic),
            ("GPU", check_gpu),
            ("Ollama", lambda: check_ollama(settings.ollama.url)),
            ("Local LLM", lambda: check_llm(settings.ollama.url, settings.llm.gpu_model, settings.llm.cpu_model)),
            ("Chrome", check_chrome),
            ("Chrome extension", check_extension),
            ("Desktop apps", check_apps),
            ("Fallback chord", lambda: check_chord(settings.chord)),
            ("Keyboard hook", check_keyboard_hook),
            ("Models", lambda: check_models(paths.MODELS)),
            ("Recordings", lambda: check_recordings(paths.RECORDINGS)),
            ("Vocab", lambda: check_vocab(paths.VOCAB)),
        ]
    )
    print(render(checks, color=enable_vt()))
    print("\n" + summary(checks))
    return exit_code(checks)
