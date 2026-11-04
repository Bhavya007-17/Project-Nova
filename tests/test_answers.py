"""Spoken answers while the island asks (SPEC §6): yes / no, and "the second one"."""

import pytest

from nova.brain.answers import pick, yes_no


@pytest.mark.parametrize("text", ["yes", "Yes.", "yeah", "yep", "sure", "do it", "go ahead", "confirm", "okay", "yes please",
                                  "send it", "yeah do it"])
def test_yes(text):
    assert yes_no(text) is True


@pytest.mark.parametrize("text", ["no", "No.", "nope", "nah", "cancel", "stop", "don't", "never mind", "no thanks", "wait"])
def test_no(text):
    assert yes_no(text) is False


@pytest.mark.parametrize("text", ["", "what", "maybe later", "open chrome"])
def test_neither(text):
    assert yes_no(text) is None


CHOICES = ("Google Chrome", "Calculator", "Claude")


@pytest.mark.parametrize(
    ("text", "index"),
    [("the second one", 1), ("two", 1), ("2", 1), ("number three", 2), ("first", 0), ("the last one", 2),
     ("calculator", 1), ("Claude.", 2), ("chrome", 0), ("the third", 2)],
)
def test_pick(text, index):
    assert pick(text, CHOICES) == index


def test_pick_yes_takes_a_single_choice():
    assert pick("yes", ("Visual Studio Code",)) == 0
    assert pick("yes", CHOICES) is None
    assert pick("no", ("Visual Studio Code",)) is None
    assert pick("the fifth one", CHOICES) is None
    assert pick("banana", CHOICES) is None
