import time
import urllib.error

import pytest

from nova import doctor
from nova.doctor import Check, WantedApp
from nova.keys import parse_chord

# --- table and runner --------------------------------------------------------


def test_render_plain_table_aligns_and_shows_fixes():
    checks = [
        Check("Python", True, "3.12.13"),
        Check("Ollama", False, "not reachable", fix="Start Ollama."),
    ]
    out = doctor.render(checks, color=False)
    lines = out.splitlines()
    assert lines[0].split() == ["Python", "OK", "3.12.13"]
    assert lines[1].split()[:3] == ["Ollama", "FIX", "not"]
    assert lines[2].strip() == "fix: Start Ollama."
    # Detail column starts at the same place on every row.
    assert lines[0].index("3.12") == lines[1].index("not")
    assert "\x1b[" not in out


def test_render_color_marks_status():
    out = doctor.render([Check("A", True, "x"), Check("B", False, "y", "do z")], color=True)
    assert "\x1b[32mOK" in out and "\x1b[31mFIX" in out


def test_run_checks_keeps_order_flattens_and_catches(monkeypatch):
    def slow():
        time.sleep(0.05)
        return Check("slow", True, "done")

    def many():
        return [Check("a", True, "1"), Check("b", False, "2", "fix b")]

    def broken():
        raise RuntimeError("probe exploded")

    checks = doctor.run_checks([("slow", slow), ("many", many), ("broken", broken)])
    assert [c.name for c in checks] == ["slow", "a", "b", "broken"]
    assert checks[-1].ok is False
    assert "probe exploded" in checks[-1].detail
    assert "nova doctor" in checks[-1].fix  # says what to do, not just that others ran


def test_exit_code():
    assert doctor.exit_code([Check("a", True, "")]) == 0
    assert doctor.exit_code([Check("a", True, ""), Check("b", False, "")]) == 1


# --- individual probes (pure parts) -------------------------------------------


def _pe(subsystem, e_lfanew=0x80):
    data = bytearray(e_lfanew + 24 + 96)
    data[:2] = b"MZ"
    data[0x3C:0x40] = e_lfanew.to_bytes(4, "little")
    data[e_lfanew:e_lfanew + 4] = b"PE\0\0"
    pos = e_lfanew + 24 + 68
    data[pos:pos + 2] = subsystem.to_bytes(2, "little")
    return bytes(data)


def test_pe_subsystem(tmp_path):
    gui, console, junk = tmp_path / "gui.exe", tmp_path / "con.exe", tmp_path / "junk.exe"
    gui.write_bytes(_pe(2))
    console.write_bytes(_pe(3, e_lfanew=0xF0))
    junk.write_bytes(b"not an exe")
    assert doctor.pe_subsystem(gui) == 2
    assert doctor.pe_subsystem(console) == 3
    assert doctor.pe_subsystem(junk) is None
    assert doctor.pe_subsystem(tmp_path / "missing.exe") is None


def test_windowed_launcher_check(tmp_path):
    (tmp_path / "pythonw.exe").write_bytes(_pe(2))
    assert doctor.check_windowed_launcher(tmp_path / "pythonw.exe").ok
    (tmp_path / "pythonw.exe").write_bytes(_pe(3))
    bad = doctor.check_windowed_launcher(tmp_path / "pythonw.exe")
    assert not bad.ok and "console" in bad.detail
    # The first uv on this laptop's PATH is a pip install, where `uv self update` refuses to run.
    assert "pip install -U uv" in bad.fix and "uv self update" in bad.fix and "uv sync" in bad.fix
    assert not doctor.check_windowed_launcher(tmp_path / "nope.exe").ok


def test_python_check():
    assert doctor.check_python((3, 12, 13), "py.exe").ok
    bad = doctor.check_python((3, 14, 2), "py.exe")
    assert not bad.ok and "uv run" in bad.fix


def _touch(path, size=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _models(root):
    for rel in doctor.REQUIRED_MODELS:
        _touch(root / rel, 2048)


def test_models_present(tmp_path):
    _models(tmp_path)
    check = doctor.check_models(tmp_path)
    assert check.ok
    assert "parakeet" in check.detail and "silero" in check.detail


def test_models_missing_file_named(tmp_path):
    _models(tmp_path)
    (tmp_path / "silero" / "silero_vad.onnx").unlink()
    check = doctor.check_models(tmp_path)
    assert not check.ok
    assert "silero_vad.onnx" in check.detail
    assert check.fix


def test_models_empty_file_counts_as_missing(tmp_path):
    _models(tmp_path)
    (tmp_path / "parakeet" / "vocab.txt").write_bytes(b"")
    assert not doctor.check_models(tmp_path).ok


def test_recordings(tmp_path):
    for session in ("s1", "s2"):
        _touch(tmp_path / session / "manifest.json")
        _touch(tmp_path / session / "a.wav")
        _touch(tmp_path / session / "b.flac")
    _touch(tmp_path / "loose" / "c.wav")
    check = doctor.check_recordings(tmp_path)
    assert check.ok and "2 sessions" in check.detail and "5 audio files" in check.detail


def test_recordings_missing(tmp_path):
    assert not doctor.check_recordings(tmp_path / "nope").ok
    (tmp_path / "empty").mkdir()
    assert not doctor.check_recordings(tmp_path / "empty").ok


WANTED = (
    WantedApp("Claude app", "Claude", "Claude_pzs8sxrjxfjjc!Claude", "handoff"),
    WantedApp("Fusion", "Autodesk Fusion", "AutodeskInc.Fusion360", "open/arrange"),
)


def test_apps_found_by_aumid():
    rows = [{"Name": "Claude", "AppID": "Claude_pzs8sxrjxfjjc!Claude"},
            {"Name": "Autodesk Fusion", "AppID": "AutodeskInc.Fusion360"}]
    checks = doctor.match_apps(rows, WANTED)
    assert [c.ok for c in checks] == [True, True]
    assert "Claude_pzs8sxrjxfjjc!Claude" in checks[0].detail


def test_apps_found_by_name_when_aumid_changed():
    rows = [{"Name": "claude", "AppID": "Claude_newpublisher!Claude"}]
    (claude, fusion) = doctor.match_apps(rows, WANTED)
    assert claude.ok and "Claude_newpublisher!Claude" in claude.detail
    assert not fusion.ok and "Start menu" in fusion.detail and fusion.fix


def test_apps_tolerate_single_row_object():
    # ConvertTo-Json emits an object, not a list, when there is exactly one app.
    checks = doctor.match_apps({"Name": "Claude", "AppID": "Claude_pzs8sxrjxfjjc!Claude"}, WANTED)
    assert checks[0].ok


def test_chrome_version_picks_highest_version_folder(tmp_path):
    for name in ("155.0.1.2", "156.0.8078.74", "SetupMetrics", "99.9.9.9"):
        (tmp_path / name).mkdir()
    assert doctor.chrome_version(tmp_path) == "156.0.8078.74"
    assert doctor.chrome_version(tmp_path / "missing") is None


def test_chrome_check_missing():
    check = doctor.check_chrome(find=lambda: None)
    assert not check.ok and check.fix


def test_chrome_check_found(tmp_path):
    exe = tmp_path / "chrome.exe"
    _touch(exe)
    (tmp_path / "156.0.8078.74").mkdir()
    check = doctor.check_chrome(find=lambda: exe)
    assert check.ok and "156.0.8078.74" in check.detail


def test_chord_free_and_taken():
    assert doctor.check_chord("ctrl+alt+n", is_free=lambda chord: True).ok
    taken = doctor.check_chord("ctrl+alt+n", is_free=lambda chord: False)
    assert not taken.ok and "Ctrl+Alt+N" in taken.detail and "chord" in taken.fix


def test_chord_probe_receives_parsed_chord():
    seen = []
    doctor.check_chord("ctrl+alt+n", is_free=lambda chord: seen.append(chord) or True)
    assert seen == [parse_chord("ctrl+alt+n")]


def test_ollama_reachable_lists_models():
    replies = {
        "/api/version": {"version": "0.30.10"},
        "/api/tags": {"models": [{"name": "qwen2.5:7b"}, {"name": "gemma4:e4b"}]},
    }

    def fetch(url, timeout):
        return replies[url.removeprefix("http://127.0.0.1:11434")]

    check = doctor.check_ollama("http://127.0.0.1:11434", fetch=fetch)
    assert check.ok
    assert "0.30.10" in check.detail and "qwen2.5:7b" in check.detail and "gemma4:e4b" in check.detail


def test_ollama_flags_cloud_models_apart():
    replies = {
        "/api/version": {"version": "0.30.10"},
        "/api/tags": {"models": [{"name": "qwen2.5:7b"}, {"name": "gemma4:31b-cloud"}, {"name": "x:cloud"}]},
    }
    check = doctor.check_ollama("http://127.0.0.1:11434", fetch=lambda url, t: replies[url[22:]])
    assert check.ok
    local, cloud = check.detail.split("; cloud")
    assert "qwen2.5:7b" in local and "cloud" not in local.split("models:")[1]
    assert "gemma4:31b-cloud" in cloud and "x:cloud" in cloud and "never" in cloud


def test_ollama_down_says_what_to_do():
    def fetch(url, timeout):
        raise urllib.error.URLError("refused")

    check = doctor.check_ollama("http://127.0.0.1:11434", fetch=fetch)
    assert not check.ok and "Ollama" in check.fix
    # Windows takes ~2 s to refuse a loopback connect, so "timed out" usually just means "not running".
    assert "not running" in check.detail


class FakeNvml:
    class NVMLError(Exception):
        pass

    def __init__(self, fail=False):
        self.fail = fail
        self.shutdown = False

    def nvmlInit(self):
        if self.fail:
            raise self.NVMLError("Driver Not Loaded")

    def nvmlDeviceGetHandleByIndex(self, index):
        return index

    def nvmlDeviceGetName(self, handle):
        return "NVIDIA GeForce RTX 5070 Laptop GPU"

    def nvmlDeviceGetMemoryInfo(self, handle):
        class Mem:
            free = 7 * 1024**3
            total = 8 * 1024**3

        return Mem()

    def nvmlShutdown(self):
        self.shutdown = True


def test_gpu_reports_free_and_total():
    nvml = FakeNvml()
    check = doctor.check_gpu(nvml)
    assert check.ok and "7.0" in check.detail and "8.0 GB" in check.detail
    assert nvml.shutdown


def test_gpu_failure_has_fix():
    check = doctor.check_gpu(FakeNvml(fail=True))
    assert not check.ok and check.fix


class FakeSD:
    def __init__(self, devices, hostapis):
        self._devices = devices
        self._hostapis = hostapis

    def query_devices(self):
        return self._devices

    def query_hostapis(self):
        return self._hostapis


DEVICES = [
    {"name": "Microphone (USB PnP Audio Devic", "hostapi": 0, "max_input_channels": 1},
    {"name": "Speakers", "hostapi": 0, "max_input_channels": 0},
    {"name": "Microphone (USB PnP Audio Device)", "hostapi": 1, "max_input_channels": 1},
    {"name": "Microphone Array (Intel® Smart Sound Technology)", "hostapi": 1, "max_input_channels": 2},
]
HOSTAPIS = [
    {"name": "MME", "default_input_device": 0},
    {"name": "Windows WASAPI", "default_input_device": 2},
]
ALLOW = {"user": "Allow", "desktop apps": "Allow", "device": "Allow"}


def test_mic_uses_full_wasapi_name():
    check = doctor.check_mic(FakeSD(DEVICES, HOSTAPIS), ALLOW)
    assert check.ok
    assert "Microphone (USB PnP Audio Device)" in check.detail
    assert "2 inputs" in check.detail


def test_mic_none():
    check = doctor.check_mic(FakeSD([], [{"name": "Windows WASAPI", "default_input_device": -1}]), ALLOW)
    assert not check.ok and check.fix


def test_mic_privacy_denied():
    check = doctor.check_mic(FakeSD(DEVICES, HOSTAPIS), {**ALLOW, "desktop apps": "Deny"})
    assert not check.ok
    assert "desktop apps" in check.fix.lower()


@pytest.mark.parametrize("missing", [{}, {"user": None}])
def test_mic_privacy_unknown_is_not_a_failure(missing):
    assert doctor.check_mic(FakeSD(DEVICES, HOSTAPIS), missing).ok


def test_settings_check(tmp_path):
    from nova.config import ConfigStore

    good = ConfigStore(tmp_path / "ok.yaml")
    assert doctor.check_settings(good).ok
    bad_path = tmp_path / "bad.yaml"
    bad_path.write_text("wake_nme: x\n", encoding="utf-8")
    bad = doctor.check_settings(ConfigStore(bad_path))
    assert not bad.ok and "wake_nme" in bad.detail and bad.fix
