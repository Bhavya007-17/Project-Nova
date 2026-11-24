"""Prefetch (Stage 07d): the early snapshot's trigger match and the once-per-utterance gate — pure logic."""

import pytest

from nova.brain.prefetch import Prefetcher, wants_snapshot


@pytest.mark.parametrize("text", [
    "show me how to",
    "Show me",
    "how do I turn on",
    "how do you change",
    "how can I",
    "click",
    "click the blue",
    "right-click on",
    "double click it",
    "where's the",
    "Where is",
    "where are my",
    "turn on night",
    "turn off bluetooth",
    "Nova, show me",
    "hey nova click save",
    "um, where's",
    "can you click",
    "please turn on",
])
def test_guide_and_click_starts_want_the_snapshot(text):
    assert wants_snapshot(text)


@pytest.mark.parametrize("text", [
    "",
    "show",  # not yet: "show" alone could be "show desktop"
    "how",
    "open chrome",
    "type hello",
    "what time is it",
    "the numbers on this report don't add up",
    "I clicked it already",  # not a request
    "clicker",  # a word that only starts with "click"
    "somewhere is",  # "where" inside another word
    "so the first law says",
    "tell me where's the",  # a match must be at the start
    "nova",
])
def test_everything_else_does_not(text):
    assert not wants_snapshot(text)


class Spawn:
    def __init__(self):
        self.started: list = []

    def __call__(self, work):
        self.started.append(work)


def make(idle=True):
    taken: list = []
    spawn = Spawn()
    flag = {"idle": idle}
    prefetcher = Prefetcher(lambda: taken.append(1), lambda: flag["idle"], spawn=spawn)
    return prefetcher, spawn, flag


def test_one_snapshot_per_utterance():
    prefetcher, spawn, _ = make()
    assert prefetcher.heard(1, "show") is False
    assert prefetcher.heard(1, "show me") is True
    assert prefetcher.heard(1, "show me how") is False
    assert prefetcher.heard(1, "show me how to turn on") is False
    assert len(spawn.started) == 1
    assert prefetcher.heard(2, "click save") is True
    assert len(spawn.started) == 2


def test_never_while_a_command_runs_or_dictation_types():
    prefetcher, spawn, flag = make(idle=False)
    assert prefetcher.heard(1, "click save") is False
    assert spawn.started == []
    flag["idle"] = True  # the command finished while he was still talking: a later partial may still take it
    assert prefetcher.heard(1, "click save as") is True
    assert len(spawn.started) == 1


def test_it_remembers_only_recent_utterances():
    prefetcher, spawn, _ = make()
    for utterance in range(40):
        prefetcher.heard(utterance, "click")
    assert len(spawn.started) == 40
    assert prefetcher.heard(39, "click it") is False
    assert len(prefetcher._seen) <= 16  # bounded: a long session doesn't grow it
