import pytest

from nova.stt.wer import corpus_wer, normalize, word_errors


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ("Open Google Chrome.", ["open", "google", "chrome"]),
        ("Close Claude, open Google Chrome!", ["close", "claude", "open", "google", "chrome"]),
        ("meeting at three p.m.", ["meeting", "at", "3", "pm"]),
        ("meeting at 3 PM", ["meeting", "at", "3", "pm"]),
        ("twenty-five minutes", ["25", "minutes"]),
        ("twenty five minutes", ["25", "minutes"]),
        ("thirty", ["30"]),
        ("ECE 2534", ["ece", "2534"]),
        ("Lo-Fi hip hop", ["lo", "fi", "hip", "hop"]),
        ("don't stop", ["don't", "stop"]),
        ("Claude’s reply", ["claude's", "reply"]),
        ("the U.S. map", ["the", "us", "map"]),
        ("rock & roll", ["rock", "and", "roll"]),
        ("", []),
        ("...", []),
    ],
)
def test_normalize(text, words):
    assert normalize(text) == words


def test_same_after_normalizing_is_zero_errors():
    assert word_errors("open Notepad and type meeting at 3 pm", "Open notepad and type meeting at three p.m.") == (0, 8)


def test_counts_substitutions_insertions_deletions():
    assert word_errors("open the door", "open a door now") == (2, 3)
    assert word_errors("go back", "back") == (1, 2)


def test_empty_reference_counts_every_word_as_inserted():
    assert word_errors("", "uh hi") == (2, 0)


def test_corpus_wer_weights_by_reference_length():
    pairs = [("a b c d", "a b c d"), ("e f", "x f")]
    assert corpus_wer(pairs) == pytest.approx(1 / 6)
    assert corpus_wer([]) == 0.0
