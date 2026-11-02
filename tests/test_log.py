import json
import logging
from pathlib import Path

from nova.log import TRACE_FILE, TraceLog, setup_app_log


def _lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_trace_writes_one_json_line_per_utterance(tmp_path):
    log = TraceLog(tmp_path)
    record = log.write(stt_ms=180, tier=0, text="open chrome")
    log.close()
    rows = _lines(tmp_path / TRACE_FILE)
    assert rows == [record]
    assert rows[0]["stt_ms"] == 180 and rows[0]["text"] == "open chrome"
    assert "ts" in rows[0]


def test_transcripts_can_be_turned_off(tmp_path):
    log = TraceLog(tmp_path, transcripts=False)
    log.write(stt_ms=5, text="secret words", transcript="secret", prompt="secret", tier=1)
    log.close()
    (row,) = _lines(tmp_path / TRACE_FILE)
    assert row["stt_ms"] == 5 and row["tier"] == 1
    assert not {"text", "transcript", "prompt"} & row.keys()


def test_transcript_toggle_applies_to_later_writes(tmp_path):
    log = TraceLog(tmp_path)
    log.transcripts = False
    log.write(text="hidden")
    log.close()
    (row,) = _lines(tmp_path / TRACE_FILE)
    assert "text" not in row


def test_unicode_and_odd_values(tmp_path):
    log = TraceLog(tmp_path)
    log.write(text="café ✓", where=Path("C:/x"))
    log.close()
    raw = (tmp_path / TRACE_FILE).read_text(encoding="utf-8")
    assert "café ✓" in raw
    assert _lines(tmp_path / TRACE_FILE)[0]["where"] == str(Path("C:/x"))


def test_traces_rotate_and_cap_backups(tmp_path):
    log = TraceLog(tmp_path, max_bytes=300, backups=2)
    for i in range(60):
        log.write(n=i, pad="x" * 40)
    log.close()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == [TRACE_FILE, f"{TRACE_FILE}.1", f"{TRACE_FILE}.2"]
    assert _lines(tmp_path / TRACE_FILE)[-1]["n"] == 59


def test_rollover_while_file_is_held_open_loses_nothing(tmp_path):
    # Windows can't rename a file another handle has open (a log viewer, `Get-Content -Wait`).
    log = TraceLog(tmp_path, max_bytes=300, backups=3)
    for i in range(40):
        log.write(n=i, pad="x" * 40)
    before = sorted(p.name for p in tmp_path.iterdir())
    held = open(tmp_path / TRACE_FILE, encoding="utf-8")  # noqa: SIM115
    try:
        for i in range(40, 60):
            log.write(n=i, pad="x" * 40)
        assert sorted(p.name for p in tmp_path.iterdir()) == before  # backups untouched
    finally:
        held.close()
    log.write(n=60, pad="x" * 40)  # rolls over now that the file is free
    log.close()
    numbers = set()
    for path in tmp_path.iterdir():
        numbers |= {row["n"] for row in _lines(path)}
    assert set(range(40, 61)) <= numbers
    assert sorted(p.name for p in tmp_path.iterdir()) == [TRACE_FILE, f"{TRACE_FILE}.1", f"{TRACE_FILE}.2", f"{TRACE_FILE}.3"]


def test_no_file_until_first_write(tmp_path):
    TraceLog(tmp_path).close()
    assert not (tmp_path / TRACE_FILE).exists()


def test_app_log_writes_plain_lines_and_is_replaceable(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    close = setup_app_log(first, console=False)
    logging.getLogger("nova.test").info("hello %s", "there")
    close = setup_app_log(second, console=False)  # replaces the first setup
    logging.getLogger("nova.test").warning("second")
    close()
    assert "INFO nova.test: hello there" in (first / "nova.log").read_text(encoding="utf-8")
    second_text = (second / "nova.log").read_text(encoding="utf-8")
    assert "second" in second_text and "hello" not in second_text
    assert "second" not in (first / "nova.log").read_text(encoding="utf-8")
