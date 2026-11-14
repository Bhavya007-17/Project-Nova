import os
import time
from dataclasses import dataclass, field

import pytest

from nova.bus import Bus
from nova.config import (
    DEFAULT_YAML,
    ConfigError,
    ConfigStore,
    Settings,
    build,
    load_text,
    with_value,
)
from nova.events import ConfigChanged, ConfigInvalid


# --- parsing and validation -------------------------------------------------


def test_defaults_follow_spec():
    s = Settings()
    assert s.wake_name == "Nova"
    assert s.chord == "ctrl+alt+n"
    assert (s.triggers.copilot_key, s.triggers.wake_name, s.triggers.always_listening, s.triggers.chord) == (
        True,
        True,
        True,
        True,
    )
    assert s.ollama.url == "http://127.0.0.1:11434"
    assert s.logs.transcripts is True


def test_default_yaml_parses_to_defaults():
    assert load_text(DEFAULT_YAML) == Settings()


def test_partial_override_keeps_other_defaults():
    s = load_text("wake_name: Nora\ntriggers:\n  always_listening: false\n")
    assert s.wake_name == "Nora"
    assert s.triggers.always_listening is False
    assert s.triggers.copilot_key is True
    assert s.chord == "ctrl+alt+n"


@pytest.mark.parametrize("text", ["", "# just a comment\n", "\n\n"])
def test_empty_file_means_defaults(text):
    assert load_text(text) == Settings()


def test_settings_are_immutable():
    s = Settings()
    with pytest.raises(AttributeError):
        s.wake_name = "x"  # type: ignore[misc]


def test_unknown_key_rejected_with_suggestion():
    with pytest.raises(ConfigError, match=r"wake_nme.*wake_name"):
        load_text("wake_nme: Nova\n")


def test_unknown_nested_key_names_full_path():
    with pytest.raises(ConfigError, match=r"triggers\.copilot.*copilot_key"):
        load_text("triggers:\n  copilot: false\n")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("triggers:\n  wake_name: sometimes\n", r"triggers\.wake_name must be true or false"),
        ("wake_name: 5\n", r"wake_name must be text"),
        ("triggers: true\n", r"triggers must be a section"),
        ("- a\n- b\n", r"settings\.yaml must be a mapping"),
        ("wake_name: '   '\n", r"wake_name must not be empty"),
        ("chord: ctrl+alt\n", r"chord"),
        ("ollama:\n  url: https://api.example.com\n", r"ollama\.url must point at this laptop"),
        ("ollama:\n  url: not a url\n", r"ollama\.url"),
        # localhost tries ::1 first; Ollama listens on 127.0.0.1 only, so each connection waits ~2 s.
        ("ollama:\n  url: http://localhost:11434\n", r"ollama\.url.*127\.0\.0\.1"),
        # PyYAML turns unquoted dates into date objects; an impossible one raised a bare ValueError.
        ("wake_name: 2026-02-30\n", r"not valid YAML"),
        ("wake_name: 2026-02-28\n", r"wake_name must be text"),
    ],
)
def test_bad_values(text, message):
    with pytest.raises(ConfigError, match=message):
        load_text(text)


@pytest.mark.parametrize("url", ["http://127.0.0.1:11434", "http://[::1]:11434", "http://127.0.0.1:9999/"])
def test_local_ollama_urls_ok(url):
    assert load_text(f"ollama:\n  url: '{url}'\n").ollama.url == url


def test_section_with_everything_commented_out_means_defaults():
    text = DEFAULT_YAML.replace("  url: http://127.0.0.1:11434", "  # url: http://127.0.0.1:11434").replace(
        "  transcripts: true", "  # transcripts: true"
    )
    assert "# url" in text and "# transcripts" in text
    assert load_text(text) == Settings()


def test_yaml_syntax_error_mentions_line():
    with pytest.raises(ConfigError, match=r"line 2"):
        load_text("wake_name: Nova\ntriggers: [unclosed\n")


def test_duplicate_keys_rejected():
    with pytest.raises(ConfigError, match=r"wake_name.*twice"):
        load_text("wake_name: A\nwake_name: B\n")


# --- the generic builder ----------------------------------------------------


@dataclass(frozen=True)
class _Inner:
    ratio: float = 0.5


@dataclass(frozen=True)
class _Outer:
    count: int = 1
    ratio: float = 1.0
    inner: _Inner = field(default_factory=_Inner)


def test_build_numbers():
    assert build(_Outer, {"count": 3, "ratio": 2, "inner": {"ratio": 0.25}}) == _Outer(3, 2.0, _Inner(0.25))


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"count": True}, r"count must be a whole number"),
        ({"count": 1.5}, r"count must be a whole number"),
        ({"ratio": "fast"}, r"ratio must be a number"),
        ({"ratio": False}, r"ratio must be a number"),
    ],
)
def test_build_number_errors(data, message):
    with pytest.raises(ConfigError, match=message):
        build(_Outer, data)


# --- the store: file I/O, hot reload, last-good fallback ---------------------


def _write(path, text, encoding="utf-8"):
    path.write_text(text, encoding=encoding)
    # Make sure the change is visible even on coarse mtime clocks.
    stamp = time.time_ns() + 10_000_000
    os.utime(path, ns=(stamp, stamp))


def _recorder(bus):
    got = []
    bus.subscribe(ConfigChanged, got.append)
    bus.subscribe(ConfigInvalid, got.append)
    return got


def test_load_creates_default_file(tmp_path):
    path = tmp_path / "config" / "settings.yaml"
    store = ConfigStore(path)
    assert store.load() == Settings()
    assert path.read_text(encoding="utf-8") == DEFAULT_YAML
    assert store.error is None


def test_load_reads_existing_file_and_publishes_change(tmp_path):
    path = tmp_path / "settings.yaml"
    _write(path, "wake_name: Nora\n")
    bus = Bus()
    got = _recorder(bus)
    store = ConfigStore(path, bus)
    store.load()
    assert store.settings.wake_name == "Nora"
    assert got == [ConfigChanged(store.settings)]


def test_broken_file_keeps_last_good_and_reports(tmp_path):
    path = tmp_path / "settings.yaml"
    _write(path, "wake_name: Nora\n")
    bus = Bus()
    store = ConfigStore(path, bus)
    store.load()
    got = _recorder(bus)

    _write(path, "wake_name: [broken\n")
    assert store.reload_if_changed() is True
    assert store.settings.wake_name == "Nora"
    assert store.error and "line" in store.error
    assert got == [ConfigInvalid(str(path), store.error, using_defaults=False)]

    _write(path, "wake_name: Nyx\n")
    assert store.reload_if_changed() is True
    assert store.settings.wake_name == "Nyx"
    assert store.error is None
    assert got[-1] == ConfigChanged(store.settings)


def test_fix_that_restores_same_values_still_announces(tmp_path):
    path = tmp_path / "settings.yaml"
    _write(path, "wake_name: Nora\n")
    bus = Bus()
    store = ConfigStore(path, bus)
    store.load()
    got = _recorder(bus)
    _write(path, "wake_name: [broken\n")
    store.reload_if_changed()
    _write(path, "wake_name: Nora\n")
    store.reload_if_changed()
    assert [type(e) for e in got] == [ConfigInvalid, ConfigChanged]
    assert store.error is None


def test_non_utf8_file_is_reported_not_crashed(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_bytes("wake_name: Nóva\n".encode("cp1252"))
    store = ConfigStore(path)
    assert store.load() == Settings()
    assert "UTF-8" in store.error


def test_first_load_of_broken_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "settings.yaml"
    _write(path, "nonsense_key: 1\n")
    bus = Bus()
    got = _recorder(bus)
    store = ConfigStore(path, bus)
    assert store.load() == Settings()
    assert "nonsense_key" in store.error
    # Nothing good was ever loaded, so the UI must say "defaults", not "last good".
    assert got == [ConfigInvalid(str(path), store.error, using_defaults=True)]


def test_reload_if_changed_ignores_untouched_file(tmp_path):
    path = tmp_path / "settings.yaml"
    _write(path, "wake_name: Nora\n")
    bus = Bus()
    store = ConfigStore(path, bus)
    store.load()
    got = _recorder(bus)
    assert store.reload_if_changed() is False
    assert got == []


def test_resave_with_same_content_does_not_publish(tmp_path):
    path = tmp_path / "settings.yaml"
    _write(path, "wake_name: Nora\n")
    bus = Bus()
    store = ConfigStore(path, bus)
    store.load()
    got = _recorder(bus)
    _write(path, "wake_name: Nora   # same value, new comment\n")
    assert store.reload_if_changed() is True
    assert got == []


def test_utf8_bom_from_notepad_is_fine(tmp_path):
    path = tmp_path / "settings.yaml"
    _write(path, "wake_name: Nóva\n", encoding="utf-8-sig")
    store = ConfigStore(path)
    assert store.load().wake_name == "Nóva"


def test_deleted_file_keeps_settings_and_is_not_recreated(tmp_path):
    path = tmp_path / "settings.yaml"
    _write(path, "wake_name: Nora\n")
    store = ConfigStore(path)
    store.load()
    path.unlink()
    assert store.reload_if_changed() is False
    assert store.settings.wake_name == "Nora"
    assert not path.exists()


def test_watch_picks_up_saves_in_background(tmp_path):
    path = tmp_path / "settings.yaml"
    _write(path, "wake_name: Nora\n")
    bus = Bus()
    store = ConfigStore(path, bus)
    store.load()
    got = _recorder(bus)
    stop = store.watch(interval=0.02)
    try:
        _write(path, "wake_name: Nyx\n")
        deadline = time.monotonic() + 3
        while not got and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        stop()
    assert store.settings.wake_name == "Nyx"
    assert got == [ConfigChanged(store.settings)]
    stop()  # idempotent


# --- audio (Stage 01) ----------------------------------------------------------------


def test_audio_device_defaults_to_windows_default():
    assert Settings().audio.device == "default"


def test_audio_device_can_name_a_mic():
    assert load_text("audio:\n  device: Jabra\n").audio.device == "Jabra"


@pytest.mark.parametrize("text", ["audio:\n  device: ''\n", "audio:\n  device: '  '\n"])
def test_audio_device_must_not_be_empty(text):
    with pytest.raises(ConfigError, match="audio.device"):
        load_text(text)


def test_audio_device_must_be_text():
    with pytest.raises(ConfigError, match="audio.device must be text"):
        load_text("audio:\n  device: 3\n")


# --- Stage 02: wake spellings, saving one value from the tray ---------------------------------------


def test_wake_spellings_default_to_the_logged_mishearings_and_parse_a_list():
    assert Settings().wake_spellings == ("no va", "noba", "noma")
    assert load_text(DEFAULT_YAML).wake_spellings == Settings().wake_spellings  # the written file says the same
    assert load_text("wake_spellings: [Novah, no va]\n").wake_spellings == ("Novah", "no va")
    assert load_text("wake_spellings:\n  - Novah\n").wake_spellings == ("Novah",)
    assert load_text("wake_spellings: []\n").wake_spellings == ()  # he can still turn them off


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("wake_spellings: Novah\n", "list"),
        ("wake_spellings: [Novah, 3]\n", "text"),
        ("wake_spellings: ['  ']\n", "empty"),
    ],
)
def test_bad_wake_spellings(text, message):
    with pytest.raises(ConfigError, match=message):
        load_text(text)


def test_with_value_changes_only_that_value_and_keeps_comments():
    text = DEFAULT_YAML
    changed = with_value(text, "triggers.always_listening", False)
    assert load_text(changed).triggers.always_listening is False
    assert changed.replace("always_listening: false", "always_listening: true") == text
    assert "# plain commands like" in changed


def test_with_value_handles_flow_style_and_rejects_missing_keys():
    flow = "triggers: {always_listening: true, chord: false}\n"
    assert load_text(with_value(flow, "triggers.always_listening", False)).triggers.always_listening is False
    with pytest.raises(ConfigError, match="always_listening"):
        with_value("wake_name: Nova\n", "triggers.always_listening", False)


def test_save_value_keeps_bom_and_line_endings(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_bytes(b"\xef\xbb\xbfwake_name: Nova\r\ntriggers:\r\n  always_listening: true\r\n")
    bus = Bus()
    seen = []
    bus.subscribe(ConfigChanged, seen.append)
    store = ConfigStore(path, bus)
    store.load()
    store.save_value("triggers.always_listening", False)
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and b"always_listening: false\r\n" in raw
    assert store.settings.triggers.always_listening is False and seen[-1].settings == store.settings


def test_save_value_refuses_a_broken_file(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("triggers: [oops\n", encoding="utf-8")
    store = ConfigStore(path)
    store.load()
    with pytest.raises(ConfigError):
        store.save_value("triggers.always_listening", False)
    assert path.read_text(encoding="utf-8") == "triggers: [oops\n"
