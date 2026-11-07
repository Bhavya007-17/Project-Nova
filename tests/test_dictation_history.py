"""Dictation history (SPEC §11): the last 50 dictations, kept in data/dictation_history.jsonl."""

from nova.dictation.history import Entry, History


def test_keeps_the_last_ones_and_survives_a_restart(tmp_path):
    path = tmp_path / "dictation_history.jsonl"
    history = History(path, limit=3)
    for n in range(5):
        history.add(Entry(f"2026-11-04T10:0{n}", f"text {n}", "Notepad"))
    assert [e.text for e in history.entries()] == ["text 2", "text 3", "text 4"]
    again = History(path, limit=3)
    assert again.last() == Entry("2026-11-04T10:04", "text 4", "Notepad")


def test_the_running_dictation_grows_in_place(tmp_path):
    history = History(tmp_path / "h.jsonl")
    history.add(Entry("t", "Hello.", "Notepad"))
    history.update_last(Entry("t", "Hello. How are you?", "Notepad"))
    assert [e.text for e in History(tmp_path / "h.jsonl").entries()] == ["Hello. How are you?"]


def test_text_that_did_not_land_is_marked(tmp_path):
    history = History(tmp_path / "h.jsonl")
    history.add(Entry("t", "lost words", "Chrome", landed=False))
    assert History(tmp_path / "h.jsonl").last().landed is False


def test_a_broken_file_is_not_fatal(tmp_path):
    path = tmp_path / "h.jsonl"
    path.write_text('{"at": "t", "text": "ok"}\nnot json\n{"text": 3}\n', encoding="utf-8")
    assert [e.text for e in History(path).entries()] == ["ok"]
    assert History(tmp_path / "missing.jsonl").last() is None
