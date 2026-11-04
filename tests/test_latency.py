"""`nova latency`: p50 / p95 per command type from the trace log (SPEC §16)."""

import json

from nova.latency import read_records, render, summarize


def rec(kind="command", type_="open_app", start=100.0, total=300.0, ok=True, via="key"):
    return {"kind": kind, "type": type_, "start_ms": start, "total_ms": total, "ok": ok, "via": via}


def test_percentiles_per_command_type():
    records = [rec(start=s, total=s + 200) for s in (100, 200, 300, 400, 500)] + [rec(type_="volume", start=50)]
    rows = {row.name: row for row in summarize(records)}
    assert rows["open_app"].count == 5 and rows["open_app"].p50 == 300 and rows["open_app"].p95 == 480
    assert rows["volume"].count == 1 and rows["volume"].p50 == 50
    assert rows["all commands"].count == 6


def test_speech_to_text_row_and_skips():
    records = [{"kind": "utterance", "final_ms": 180.0}, {"kind": "utterance", "final_ms": 220.0},
               rec(start=None, ok=False, type_="none"), {"kind": "gate"}]
    rows = {row.name: row for row in summarize(records)}
    assert rows["speech → text"].p50 == 200
    assert "none" not in rows  # nothing ran: no start time


def test_say_commands_are_kept_apart():
    rows = {row.name: row for row in summarize([rec(via="say"), rec()])}
    assert rows["open_app"].count == 1 and rows["(typed) open_app"].count == 1


def test_commands_that_asked_first_are_listed_apart():
    asked = {**rec(start=3000), "asked": "clarify"}
    rows = {row.name: row for row in summarize([asked, rec(start=100)])}
    assert rows["open_app (asked first)"].p50 == 3000 and rows["all commands"].count == 1


def test_read_and_render(tmp_path):
    path = tmp_path / "traces.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in [rec(), rec(type_="layout", start=900)]) + "\nnot json\n",
                    encoding="utf-8")
    (tmp_path / "traces.jsonl.1").write_text(json.dumps(rec(start=50)) + "\n", encoding="utf-8")
    records = read_records(path)
    assert len(records) == 3
    text = render(summarize(records))
    assert "open_app" in text and "layout" in text and "p95" in text


def test_paths_per_tier_and_device():
    records = [{**rec(start=40), "tier": "0"}, {**rec(start=900), "tier": "1", "device": "gpu"},
               {**rec(start=1100), "tier": "1", "device": "gpu"}, {**rec(start=2100), "tier": "1", "device": "cpu"},
               {**rec(start=950, via="say"), "tier": "1", "device": "gpu"}]
    rows = {row.name: row for row in summarize(records)}
    assert rows["tier 0"].count == 1 and rows["tier 1 GPU"].count == 2 and rows["tier 1 GPU"].p50 == 1000
    assert rows["tier 1 CPU"].p50 == 2100 and rows["(typed) tier 1 GPU"].count == 1
