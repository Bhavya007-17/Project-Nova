"""`nova bench-stt --set`: the command-set metric on made-up pairs (no models)."""

import json

import pytest

from nova.actions.catalog import parse_aliases
from nova.gate import WakeName
from nova.recordset import Line
from nova.stt import bench

ALIASES = parse_aliases("""\
chrome: Google Chrome
claude: Claude
docs: https://docs.google.com
google docs: https://docs.google.com
vs code: Visual Studio Code
vscode: Visual Studio Code
""")
WAKE = WakeName.of("Nova")


def _result(text, heard, entities=(), wake=False, segments=None, ms=(100.0,)):
    line = Line("01", text, tuple(entities), wake)
    return bench.SetResult(line, heard, tuple(segments if segments is not None else [heard]), tuple(ms))


# --- which spellings count as the same name ---------------------------------------------------------------------


def test_entity_keys_join_aliases_app_names_and_short_names():
    assert bench.entity_keys("Chrome", ALIASES) == {"chrome", "googlechrome"}
    assert bench.entity_keys("Google Chrome", ALIASES) == {"chrome", "googlechrome"}
    assert bench.entity_keys("VS Code", ALIASES) == {"vscode", "visualstudiocode"}
    assert bench.entity_keys("Docs", ALIASES) == {"docs", "googledocs"}  # same site
    assert bench.entity_keys("MuJoCo", ALIASES) == {"mujoco"}  # a vocab term: itself only


@pytest.mark.parametrize("heard, ok", [
    ("Open Chrome.", True),
    ("open google chrome", True),
    ("Close Chrome's window", True),
    ("Open home.", False),  # the mishearing this set exists to count
    ("Open chromebook", False),  # whole words only
    ("", False),
])
def test_heard_entity(heard, ok):
    assert bench.heard_entity(bench.entity_keys("Chrome", ALIASES), heard) is ok


def test_heard_entity_squashes_spacing_and_case():
    assert bench.heard_entity(bench.entity_keys("VS Code", ALIASES), "switch to V.S. code")
    assert bench.heard_entity(bench.entity_keys("VS Code", ALIASES), "switch to vscode")
    assert bench.heard_entity({"chatgpt"}, "close chat GPT")
    assert bench.heard_entity({"ros2"}, "search for ROS 2")


# --- scoring --------------------------------------------------------------------------------------------------


def test_score_counts_entity_errors_wake_misses_and_empties():
    results = [
        _result("Nova, open Chrome", "Nova, open Chrome.", ["Chrome"], wake=True),
        _result("Nova, close Claude", "Nova, close Claire.", ["Claude"], wake=True),  # entity error
        _result("Nova, switch to VS Code", "No, but switch to VS Code.", ["VS Code"], wake=True),  # wake miss
        _result("Open Docs", "Open Google Docs.", ["Docs"]),  # same site: fine
        _result("Close Chrome", "", ["Chrome"], segments=[""], ms=(90.0,)),  # empty: an entity error too
        _result("Search Docs for MuJoCo", "Search docs for Mujoco.", ["Docs", "MuJoCo"],
                segments=["Search docs", "for Mujoco."], ms=(120.0, 80.0)),
    ]
    score = bench.score_set(results, ALIASES, WAKE)
    assert score.items == 6
    assert score.entities == 7 and score.entity_errors == 2
    assert [m.line.text for m in score.entity_misses] == ["Nova, close Claude", "Close Chrome"]
    assert score.wake_items == 3 and score.wake_misses == 1
    assert score.empty == 1 and score.decodes == 7 and score.silent_items == 1
    assert score.p50 == pytest.approx(100.0)
    assert 0 < score.wer < 1


def test_score_wake_spellings_from_settings_count():
    results = [_result("Nova, open Chrome", "Noba, open Chrome.", ["Chrome"], wake=True)]
    assert bench.score_set(results, ALIASES, WAKE).wake_misses == 1
    assert bench.score_set(results, ALIASES, WakeName.of("Nova", ["noba"])).wake_misses == 0


def test_score_without_speech_found():
    results = [_result("Open Chrome", "", ["Chrome"], segments=[], ms=())]
    score = bench.score_set(results, ALIASES, WAKE)
    assert score.decodes == 0 and score.empty == 0 and score.silent_items == 1 and score.entity_errors == 1


# --- the adoption rule (fixed in the stage prompt) -------------------------------------------------------------


def _score(errors, p95, empty):
    return bench.SetScore(items=30, entities=36, entity_errors=errors, wake_items=15, wake_misses=0, empty=empty,
                          decodes=31, silent_items=0, wer=0.1, p50=200.0, p95=p95, entity_misses=())


@pytest.mark.parametrize("base, cand, adopt", [
    ((8, 300, 1), (4, 390, 1), True),  # halved, by 4, +90 ms, same empties
    ((8, 300, 1), (5, 300, 0), False),  # not halved
    ((4, 300, 1), (2, 300, 1), False),  # halved but only by 2
    ((8, 300, 1), (4, 401, 1), False),  # p95 +101 ms
    ((8, 300, 1), (2, 300, 2), False),  # more empty outputs
    ((0, 300, 0), (0, 250, 0), False),  # nothing to fix
])
def test_adoption_rule(base, cand, adopt):
    ok, why = bench.adopt(_score(*base), _score(*cand))
    assert ok is adopt
    assert why


def test_wake_verdict_says_more_than_one_in_fifteen():
    ok = bench.SetScore(30, 30, 0, 15, 1, 0, 30, 0, 0.0, 1.0, 1.0, ())
    bad = bench.SetScore(30, 30, 0, 15, 2, 0, 30, 0, 0.0, 1.0, 1.0, ())
    assert "revisit" not in bench.wake_verdict(ok)
    assert "revisit" in bench.wake_verdict(bad)


# --- reading the folder --------------------------------------------------------------------------------------


def test_find_set_keeps_recorded_lines_only(tmp_path):
    data = {"commands": [
        {"id": "01", "wav": "01.wav", "expected_text": "Open Chrome", "entities": ["Chrome"], "has_wake": False},
        {"id": "02", "wav": None, "expected_text": "Close Claude", "entities": ["Claude"], "has_wake": False},
        {"id": "03", "wav": "03.wav", "expected_text": "Nova, open Docs", "entities": ["Docs"], "has_wake": True},
    ]}
    (tmp_path / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    (tmp_path / "01.wav").write_bytes(b"RIFF")
    items = bench.find_set(tmp_path)
    assert [(line.id, wav.name) for line, wav in items] == [("01", "01.wav")]  # 03.wav isn't on disk


def test_run_set_without_recordings_says_how_to_make_them(tmp_path):
    said = []
    assert bench.run_set(tmp_path, out=said.append) == 1
    assert "nova record-set" in said[0]


def test_unknown_backend():
    with pytest.raises(ValueError, match="parakeet or sherpa"):
        bench.load_backend("whisper")
