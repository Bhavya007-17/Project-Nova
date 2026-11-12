"""nova record-set: the 30 commands (pure), the manifest, and the recorder with a fake mic and fake keys."""

import json
import wave

import numpy as np
import pytest

from nova import recordset as rs
from nova.actions.catalog import key_of, parse_aliases

ALIASES = parse_aliases("""\
canvas: https://canvas.example.edu
fusion: Autodesk Fusion
chrome: Google Chrome
claude: Claude
chatgpt: ChatGPT
docs: https://docs.google.com
google docs: https://docs.google.com
youtube: https://www.youtube.com
vs code: Visual Studio Code
vscode: Visual Studio Code
explorer: File Explorer
files: File Explorer
""")
TERMS = ["CroLabs", "ROS2", "MuJoCo", "Canvas", "Claude", "ChatGPT", "Virginia Tech", "Notion"]
APPS = ["Google Chrome", "Claude", "ChatGPT", "Autodesk Fusion", "Visual Studio Code", "File Explorer", "Notepad",
        "Calculator", "Spotify", "Microsoft Word", "Paint", "Python 3.12 (64-bit)", "Uninstall Foo", "Snipping Tool",
        "Notion"]


def _set(**kw):
    return rs.build_set(ALIASES, TERMS, APPS, **kw)


def test_thirty_lines_numbered_and_unique():
    lines = _set()
    assert len(lines) == 30
    assert [line.id for line in lines] == [f"{i:02d}" for i in range(1, 31)]
    assert len({line.text for line in lines}) == 30


def test_same_config_same_lines_and_another_seed_differs():
    assert _set() == _set()
    assert _set() != _set(seed=rs.SEED + 1)


def test_half_start_with_the_wake_name():
    lines = _set()
    woke = [line for line in lines if line.has_wake]
    assert len(woke) == 15
    assert all(line.text.startswith("Nova, ") for line in woke)
    assert not any(line.text.lower().startswith("nova") for line in lines if not line.has_wake)
    assert all(line.text[0].isupper() for line in lines)


def test_other_wake_name_from_settings():
    lines = _set(wake="Jarvis")
    assert sum(line.text.startswith("Jarvis, ") for line in lines) == 15


def test_every_alias_is_said_at_least_once():
    entities = {key_of(e) for line in _set() for e in line.entities}
    for phrase in ALIASES:
        assert key_of(phrase) in entities, phrase  # "vs code" and "vscode" sound the same: one covers both


def test_every_vocab_term_is_said():
    entities = {key_of(e) for line in _set() for e in line.entities}
    for term in TERMS:
        assert key_of(term) in entities, term


def test_entities_appear_in_the_text_in_their_display_spelling():
    for line in _set():
        assert line.entities
        for entity in line.entities:
            assert entity in line.text, (entity, line.text)


def test_names_are_spelled_the_way_he_writes_them():
    entities = {e for line in _set() for e in line.entities}
    assert "ChatGPT" in entities and "MuJoCo" in entities and "ROS2" in entities
    assert "VS Code" in entities  # "vs code": no vowels -> capitals
    assert "Chrome" in entities  # short name of Google Chrome
    assert "Google Docs" in entities


def test_verbs_vary():
    verbs = {line.text.removeprefix("Nova, ").split()[0].lower() for line in _set()}
    assert {"open", "switch", "close", "search", "go"} <= verbs


def test_sites_are_not_closed_or_switched_to():
    sites = {"Canvas", "Docs", "Google Docs", "Youtube"}
    for line in _set():
        body = line.text.removeprefix("Nova, ").lower()
        if body.startswith(("close ", "switch to ")):
            assert line.entities[0] not in sites, line.text


def test_extra_installed_apps_fill_the_set_but_never_uninstallers_or_versions():
    texts = " | ".join(line.text for line in _set())
    assert "Uninstall" not in texts and "Python" not in texts
    extras = {"Notepad", "Calculator", "Spotify", "Microsoft Word", "Paint", "Snipping Tool"}
    assert sum(name in texts for name in extras) >= 3


def test_the_apps_his_aliases_point_at_are_said_by_full_name_before_other_apps():
    texts = " | ".join(line.text for line in _set())
    for name in ("Google Chrome", "Visual Studio Code", "File Explorer", "Autodesk Fusion"):
        assert name in texts
    few = " | ".join(line.text for line in rs.build_set(ALIASES, TERMS, APPS, count=20))
    assert "Google Chrome" in few and "Notepad" not in few and "Spotify" not in few


def test_without_an_app_list_it_repeats_names_with_other_verbs():
    lines = rs.build_set(ALIASES, TERMS, [])
    assert len(lines) == 30 and len({line.text for line in lines}) == 30


def test_too_many_names_keeps_every_alias_first():
    many = {f"site{i}": f"https://s{i}.example" for i in range(28)}
    lines = rs.build_set(parse_aliases("\n".join(f"{k}: {v}" for k, v in many.items())), TERMS, [])
    entities = {key_of(e) for line in lines for e in line.entities}
    assert len(lines) == 30
    assert all(key_of(k) in entities for k in many)


def test_manifest_round_trip(tmp_path):
    lines = _set()[:3]
    data = rs.manifest(lines, {"01": "01.wav", "03": "03.wav"})
    assert data["sample_rate"] == 16000
    assert [c["wav"] for c in data["commands"]] == ["01.wav", None, "03.wav"]
    first = data["commands"][0]
    assert set(first) == {"id", "wav", "expected_text", "entities", "has_wake"}
    assert first["expected_text"] == lines[0].text and first["entities"] == list(lines[0].entities)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    loaded, recorded = rs.read_manifest(path)
    assert loaded == lines and recorded == {"01": "01.wav", "03": "03.wav"}


def test_read_manifest_tolerates_junk(tmp_path):
    path = tmp_path / "manifest.json"
    assert rs.read_manifest(path) == ((), {})
    path.write_text("{not json", encoding="utf-8")
    assert rs.read_manifest(path) == ((), {})
    path.write_text(json.dumps({"commands": [{"id": 1}, "x", {"id": "02", "expected_text": "Open Paint",
                                                             "entities": ["Paint"], "has_wake": False,
                                                             "wav": "../evil.wav"}]}), encoding="utf-8")
    lines, recorded = rs.read_manifest(path)
    assert [line.text for line in lines] == ["Open Paint"]
    assert recorded == {}  # a wav name that isn't NN.wav is ignored


# --- the recorder (fake mic, fake keys) -----------------------------------------------------------------------


class FakeMic:
    def __init__(self, level: float = 0.2) -> None:
        self.calls = 0
        self.level = level

    def __call__(self, seconds: float) -> np.ndarray:
        self.calls += 1
        t = np.arange(int(seconds * 16000)) / 16000
        return (self.level * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def _keys(*keys):
    it = iter(keys)
    return lambda: next(it)


def _run(tmp_path, lines, keys, mic=None, out=None):
    printed = out if out is not None else []
    return rs.record(lines, tmp_path, mic or FakeMic(), _keys(*keys), seconds=1.0, tick=lambda s: None,
                     out=lambda *a, **k: printed.append(" ".join(str(x) for x in a)))


def test_enter_keeps_r_records_again_s_skips(tmp_path):
    lines = _set()[:3]
    mic = FakeMic()
    done = _run(tmp_path, lines, ["\r", "r", "x", "\r", "s"], mic)
    assert done == {"01": "01.wav", "02": "02.wav"}
    assert mic.calls == 4  # 01 once, 02 twice, 03 once
    assert (tmp_path / "01.wav").is_file() and (tmp_path / "02.wav").is_file()
    assert not (tmp_path / "03.wav").exists()
    _, recorded = rs.read_manifest(tmp_path / "manifest.json")
    assert recorded == {"01": "01.wav", "02": "02.wav"}


def test_wav_is_16k_mono_16_bit(tmp_path):
    _run(tmp_path, _set()[:1], ["\r"])
    with wave.open(str(tmp_path / "01.wav"), "rb") as w:
        assert (w.getnchannels(), w.getframerate(), w.getsampwidth()) == (1, 16000, 2)
        assert w.getnframes() == 16000
    from nova.audio import read_wav

    audio = read_wav(tmp_path / "01.wav")
    assert 0.19 < float(np.max(np.abs(audio))) < 0.21


def test_q_quits_and_keeps_what_is_there(tmp_path):
    lines = _set()[:4]
    done = _run(tmp_path, lines, ["\r", "q"])
    assert done == {"01": "01.wav"}
    _, recorded = rs.read_manifest(tmp_path / "manifest.json")
    assert recorded == {"01": "01.wav"}
    assert not (tmp_path / "02.wav").exists()


def test_a_second_run_records_only_what_is_missing(tmp_path):
    lines = _set()[:3]
    _run(tmp_path, lines, ["\r", "s", "q"])
    mic = FakeMic()
    printed: list[str] = []
    done = _run(tmp_path, lines, ["\r", "\r"], mic, printed)
    assert done == {"01": "01.wav", "02": "02.wav", "03": "03.wav"}
    assert mic.calls == 2
    assert any("1 of 3 already recorded" in line for line in printed)


def test_a_changed_set_starts_over(tmp_path):
    _run(tmp_path, _set()[:2], ["\r", "\r"])
    other = rs.build_set(ALIASES, TERMS, APPS, seed=99)[:2]
    mic = FakeMic()
    done = _run(tmp_path, other, ["\r", "s"], mic)
    assert mic.calls == 2 and done == {"01": "01.wav"}


def test_quiet_and_clipped_takes_are_flagged(tmp_path):
    printed: list[str] = []
    _run(tmp_path, _set()[:1], ["\r"], FakeMic(level=0.001), printed)
    assert any("quiet" in line for line in printed)
    printed.clear()
    _run(tmp_path / "b", _set()[:1], ["\r"], FakeMic(level=1.0), printed)
    assert any("clipped" in line for line in printed)


def test_the_screen_shows_the_line_and_the_keys(tmp_path):
    printed: list[str] = []
    lines = _set()[:1]
    _run(tmp_path, lines, ["\r"], out=printed)
    screen = "\n".join(printed)
    assert lines[0].text in screen and "[1/1]" in screen
    assert "Enter" in screen and "R" in screen and "S" in screen and "Q" in screen
    assert "bench-stt --set" in screen


@pytest.mark.parametrize("raw, key", [("\r", "keep"), ("\n", "keep"), ("R", "again"), ("s", "skip"), ("Q", "quit"),
                                      ("x", None)])
def test_key_meanings(raw, key):
    assert rs.key_action(raw) == key
