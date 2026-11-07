"""Dictation cleanup (SPEC §11 steps 3-4): the model lists misheard words; the guard decides whether to use them."""

import json
import threading

import pytest

from nova.brain.llm import LLMError, Reply
from nova.config import Llm
from nova.dictation.cleanup import Cleaned, Cleaner, apply_fixes, parse_fixes, prefix, request
from nova.governor import Choice

TERMS = ("CroLabs", "ROS2", "MuJoCo", "Claude", "Canvas")


def fixed(text, fixes, before=""):
    return apply_fixes(text, fixes, terms=TERMS, before=before)


# --- the guard -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text, fixes, want", [
    ("I think we should meat at the library.", [("meat", "meet")], "I think we should meet at the library."),
    ("Let me know weather you can make it.", [("weather", "whether")], "Let me know whether you can make it."),
    ("I'll bring the peace of paper.", [("peace", "piece")], "I'll bring the piece of paper."),
    ("Claire said it looks fine.", [("Claire", "Claude")], "Claude said it looks fine."),
    ("We port it to ross two next.", [("ross two", "ROS2")], "We port it to ROS2 next."),
    ("Then mew joco for the sim.", [("mew joco", "MuJoCo")], "Then MuJoCo for the sim."),
    ("It is due friday.", [("friday", "Friday")], "It is due Friday."),  # case only
    ("Their is a problem.", [("Their", "there")], "There is a problem."),  # keeps the capital
    ("Nothing wrong here.", [], "Nothing wrong here."),
])
def test_sound_alike_fixes_are_applied(text, fixes, want):
    assert fixed(text, fixes).text == want


def test_a_fix_for_part_of_a_name_takes_the_whole_name():
    assert fixed("Sensors from crow labs arrived.", [("crow", "CroLabs")]).text == "Sensors from CroLabs arrived."


def test_context_words_count_like_names():
    assert fixed("Klod said yes.", [("Klod", "Claude")], before="I asked Claude.").text == "Claude said yes."


@pytest.mark.parametrize("fixes, why", [
    ([("dog", "cat")], "not a sound-alike"),  # a different word: new content
    ([("The dog barked", "A cat purred loudly at")], "adds words"),
    ([("dog", "")], "empty"),
])
def test_new_words_throw_the_whole_answer_out(fixes, why):
    cleaned = fixed("The dog barked at night.", fixes)
    assert cleaned.text == "The dog barked at night."
    assert cleaned.fixes == () and why in cleaned.rejected


def test_more_than_a_quarter_changed_is_rejected():
    cleaned = fixed("for to meat their", [("for", "four"), ("to", "two"), ("meat", "meet")])
    assert cleaned.text == "for to meat their" and "too much" in cleaned.rejected


def test_a_heard_phrase_that_isnt_there_is_skipped():
    cleaned = fixed("We meet at noon.", [("meat", "meet")])
    assert cleaned == Cleaned("We meet at noon.")


def test_ambiguous_heard_word_is_skipped():
    assert fixed("Their cat and their dog.", [("their", "there")]).text == "Their cat and their dog."


def test_parse_fixes_is_forgiving():
    assert parse_fixes('{"fixes": [{"heard": "meat", "meant": "meet"}]}') == [("meat", "meet")]
    assert parse_fixes('{"fixes": []}') == []
    assert parse_fixes("not json") is None
    assert parse_fixes('{"fixes": [{"heard": 3}]}') == []


# --- the prompt --------------------------------------------------------------------------------------------------------


def test_prefix_is_stable_and_names_his_terms():
    one, two = prefix(TERMS), prefix(TERMS)
    assert one == two and "CroLabs" in one[0]["content"]
    assert all(json.loads(m["content"]) for m in one[1:])  # examples are JSON both ways


def test_request_keeps_only_the_end_of_the_text_before():
    data = request("next words", "x" * 500)
    assert data["text"] == "next words" and len(data["before"]) == 200


# --- the LLM job ------------------------------------------------------------------------------------------------------


class FakeClient:
    url = "http://127.0.0.1:11434"

    def __init__(self, answer='{"fixes": []}', fail=None, gate=None):
        self.answer, self.fail, self.gate = answer, fail, gate
        self.calls = []

    def show(self, model, timeout=5.0):
        return {"details": {"family": "qwen35"}, "capabilities": ["completion", "thinking"]}

    def generate(self, model, prompt, **kw):
        self.calls.append((model, prompt, kw))
        if self.gate is not None and kw.get("num_predict") != 1:  # warm-ups go straight through
            self.gate.wait(5)
        if self.fail:
            raise LLMError(self.fail)
        return Reply(self.answer, 10.0)

    def abort(self):
        pass


class FakeGovernor:
    def __init__(self, device="gpu"):
        self.choice = Choice("qwen3.5:4b", device)

    def choose(self, *, fresh=False):
        return self.choice

    def used(self):
        pass


def make(client, device="gpu", enabled=True):
    return Cleaner(FakeGovernor(device), Llm, lambda: TERMS, client=lambda: client, enabled=lambda: enabled)


def test_warm_reads_the_prefix_once_and_makes_it_ready():
    client = FakeClient()
    cleaner = make(client)
    assert not cleaner.ready
    cleaner.warm(block=True)
    assert cleaner.ready
    model, prompt, kw = client.calls[0]
    assert kw["num_predict"] == 1 and prompt.endswith("<|im_start|>user\n")


def test_cpu_means_rules_only():
    cleaner = make(FakeClient(), device="cpu")
    cleaner.warm(block=True)
    assert not cleaner.ready and cleaner.start("text", "") is None


def test_turned_off_in_settings():
    cleaner = make(FakeClient(), enabled=False)
    cleaner.warm(block=True)
    assert not cleaner.ready


def test_a_job_returns_the_checked_text():
    client = FakeClient('{"fixes": [{"heard": "meat", "meant": "meet"}]}')
    cleaner = make(client)
    cleaner.warm(block=True)
    job = cleaner.start("We should meat at noon.", "Hi.")
    assert job.wait(2).text == "We should meet at noon."
    sent = client.calls[-1][1]
    assert '"text":"We should meat at noon."' in sent and sent.endswith("<think>\n\n</think>\n\n")


def test_a_job_that_fails_gives_nothing():
    client = FakeClient()
    cleaner = make(client)
    cleaner.warm(block=True)
    client.fail = "Ollama isn't answering"
    job = cleaner.start("a b c", "")
    assert job.wait(2) is None and "answering" in job.error


def test_a_late_job_gives_nothing_in_time_but_can_still_be_used():
    gate = threading.Event()
    cleaner = make(FakeClient(gate=gate))
    cleaner.warm(block=True)
    job = cleaner.start("a b c", "")
    assert job.wait(0.05) is None
    gate.set()
    assert job.wait(2) == Cleaned("a b c")


def test_cancel_stops_waiting():
    gate = threading.Event()
    cleaner = make(FakeClient(gate=gate))
    cleaner.warm(block=True)
    job = cleaner.start("a b c", "")
    job.cancel()
    assert job.wait(0.5) is None
    gate.set()
