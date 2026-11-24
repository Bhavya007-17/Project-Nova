"""OCR lines split into targets at wide gaps (``nova.actions.ocr.segments``) — pure, no winrt."""

from nova.actions.marks import Box
from nova.actions.ocr import Word, segments


def word(text: str, left: int, right: int, top: int = 100, bottom: int = 120) -> Word:
    return Word(text, Box(left, top, right, bottom))


def test_close_words_make_one_target():
    found = segments([word("Fit", 10, 30), word("to", 36, 50), word("width", 56, 100)])
    assert [t.name for t in found] == ["Fit to width"]
    assert found[0].box == Box(10, 100, 100, 120)
    assert (found[0].role, found[0].source) == ("text", "ocr")


def test_a_wide_gap_splits_a_toolbar_row():
    # height 20: a gap over 30 px (1.5 line heights) starts a new target
    found = segments([word("File", 10, 40), word("Edit", 80, 110), word("View", 115, 150)])
    assert [t.name for t in found] == ["File", "Edit View"]


def test_words_come_in_any_order_and_blanks_are_skipped():
    found = segments([word("width", 56, 100), word(" ", 31, 35), word("Fit", 10, 30), word("to", 36, 50)])
    assert [t.name for t in found] == ["Fit to width"]


def test_nothing_in_nothing_out():
    assert segments([]) == []
    assert segments([word("  ", 0, 10)]) == []
