"""Dictation cleanup (SPEC §11 steps 3-4): the model lists misheard words; the guard decides whether to use them."""

import json
import threading

import pytest

from nova.brain.llm import LLMError, Loaded, Loader, Reply, load_options
from nova.config import Llm
from nova.dictation.cleanup import Cleaned, Cleaner, apply_fixes, guard, parse_fixes, prefix, request
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


# --- the output guards (Stage 07a item 9): any one -> the rules-only text ---------------------------------------------


def test_guard_more_words_than_heard_plus_an_eighth():
    heard = "one two three four five six seven eight"
    assert guard(heard, heard + " nine") == ""  # 8 + 1/8 * 8 = 9 words allowed
    assert "more words" in guard(heard, heard + " nine ten")


def test_guard_changed_negations():
    assert "negation" in guard("I can make it on Friday.", "I can't make it on Friday.")
    assert "negation" in guard("We did not ship it.", "We did ship it.")
    assert guard("I don't think so.", "I doesn't think so.") == ""  # still one negation


def test_guard_fewer_than_a_third_of_the_words_kept():
    assert "kept" in guard("alpha beta gamma delta epsilon zeta", "alpha theta iota kappa lambda mu")
    assert guard("alpha beta gamma delta epsilon zeta", "alpha beta gamma theta iota kappa") == ""


def test_guard_more_than_three_unfamiliar_new_words():
    heard = "we will meet the team at the lab after class on monday morning to test it"
    four = "we will meet the crew at the shop after lunch on tuesday morning to test it"
    assert "new words" in guard(heard, four)
    three = "we will meet the crew at the shop after lunch on monday morning to test it"
    assert guard(heard, three) == ""
    assert guard(heard, four, terms=("crew", "shop", "lunch", "tuesday")) == ""  # his names aren't unfamiliar
    assert guard(heard, four, before="The crew, the shop, lunch, Tuesday.") == ""  # nor is what's before the cursor


def test_a_sound_alike_that_flips_the_meaning_is_thrown_out():
    # "want" -> "won't" sounds alike (same metaphone) and passed the old guard
    cleaned = fixed("I want to go to the lab today.", [("want", "won't")])
    assert cleaned.text == "I want to go to the lab today." and "negation" in cleaned.rejected
    cleaned = fixed("I can make it on Friday.", [("can", "can't")])
    assert cleaned.text == "I can make it on Friday." and cleaned.fixes == ()


def test_fixes_that_each_add_a_word_add_too_many():
    cleaned = fixed("We need alot of time and alot of help.", [("alot of time", "a lot of time"),
                                                              ("alot of help", "a lot of help")])
    assert "more words" in cleaned.rejected and cleaned.text == "We need alot of time and alot of help."


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
    """A fake Ollama: a warm-up (num_predict 1) loads the model; ``resident`` = what /api/ps lists."""

    url = "http://127.0.0.1:11434"

    def __init__(self, answer='{"fixes": []}', fail=None, gate=None):
        self.answer, self.fail, self.gate = answer, fail, gate
        self.calls = []
        self.resident = set()

    def show(self, model, timeout=5.0):
        return {"details": {"family": "qwen35"}, "capabilities": ["completion", "thinking"]}

    def loaded(self, timeout=2.0):
        return tuple(Loaded(name, 1, 1) for name in self.resident)

    def generate(self, model, prompt, **kw):
        self.calls.append((model, prompt, kw))
        if kw.get("num_predict") == 1:  # warm-ups go straight through
            self.resident.add(model)
            return Reply("", 10.0)
        if self.gate is not None:
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
    return Cleaner(FakeGovernor(device), Llm, lambda: TERMS, client=lambda: client, enabled=lambda: enabled,
                   loader=Loader(client))


def answers(client):
    return [c for c in client.calls if c[2].get("num_predict") != 1]


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


# --- one model, no thrash (Stage 07a item 1) ---------------------------------------------------------------------------


def test_cleanup_and_the_planner_send_the_same_load_options():
    client = FakeClient()
    cleaner = make(client)
    cleaner.warm(block=True)
    cleaner.start("We should meat at noon.", "").wait(2)
    want = load_options(Choice("qwen3.5:4b", "gpu").options)
    assert [c[2]["options"] for c in client.calls] == [want, want]  # warm-up and job: what Tier 1 sends too


def test_a_job_on_a_model_ollama_unloaded_is_skipped_at_once_and_loads_it_again():
    client = FakeClient('{"fixes": [{"heard": "meat", "meant": "meet"}]}')
    cleaner = make(client)
    cleaner.warm(block=True)
    client.resident.clear()  # keep_alive ran out, or another model took the GPU
    assert cleaner.ready  # it still says ready: the job finds out
    job = cleaner.start("We should meat at noon.", "")
    assert job.wait(1) is None and job.done and job.cold  # no 5 s wait for a model that isn't there
    assert answers(client) == []
    for _ in range(100):  # ...and it's loading again for the next segment
        if "qwen3.5:4b" in client.resident:
            break
        threading.Event().wait(0.01)
    assert "qwen3.5:4b" in client.resident
    assert cleaner.start("We should meat at noon.", "").wait(2).text == "We should meet at noon."


def test_a_job_while_the_model_loads_for_tier1_doesnt_load_it_twice():
    gate = threading.Event()

    class Slow(FakeClient):
        def generate(self, model, prompt, **kw):
            if kw.get("num_predict") == 1:
                gate.wait(5)
            return super().generate(model, prompt, **kw)

    client = Slow()
    loader = Loader(client)
    cleaner = Cleaner(FakeGovernor(), Llm, lambda: TERMS, client=lambda: client, loader=loader)
    cleaner._ready = Choice("qwen3.5:4b", "gpu")  # warmed earlier; Ollama dropped it since
    tier1_load = loader.load("qwen3.5:4b", options=load_options({}), keep_alive="10m", prompt="planner prefix")
    job = cleaner.start("a b c d", "")
    assert job.wait(1) is None and job.cold
    gate.set()
    assert tier1_load.wait(2)
    assert len([c for c in client.calls if c[2].get("num_predict") == 1]) == 1
