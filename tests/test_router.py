"""The router (SPEC §7): Tier 0 -> skills (stub) -> Tier 1 -> checked plan; questions settled before acting."""

import pytest

from catalog_fixture import CATALOG

from nova.actions.context import Context
from nova.brain.plan import Plan, step
from nova.brain.router import Router, describe_handoff
from nova.brain.tier0 import Tier0Resolver
from nova.brain.tier1 import Thought
from nova.gate import Addressing, Env, WakeName, decide

EMPTY = Context(None, (), (), None, False, 0.0)


class FakeTier1:
    """Answers from a script: each call pops the next plan JSON (or an error)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []
        self.aborted = False

    def think(self, text, seen, catalog=None, *, asked=None, answer=None, choice=None):
        self.calls.append((text, asked, answer))
        answer_ = self.answers.pop(0)
        if isinstance(answer_, Thought):
            return answer_
        return Thought(answer_, ms=500.0)

    def abort(self):
        self.aborted = True


class Asker:
    def __init__(self, confirm=True, choose=0, answer="Notepad"):
        self._confirm, self._choose, self._answer = confirm, choose, answer
        self.asked = []

    def confirm(self, question):
        self.asked.append(("confirm", question))
        return self._confirm

    def choose(self, question, choices):
        self.asked.append(("choose", question, tuple(choices)))
        return self._choose

    def answer(self, question):
        self.asked.append(("answer", question))
        return self._answer


def route(text, tier1=None, **kw):
    return Router(tier1).route(text, EMPTY, CATALOG, **kw)


def test_tier_0_first_and_no_llm_call():
    tier1 = FakeTier1()
    routed = route("open chrome", tier1)
    assert routed.tier == "0" and routed.plan.steps == (step("open_app", app="Google Chrome"),)
    assert tier1.calls == []


def test_what_tier_0_misses_goes_to_tier_1_and_is_checked():
    tier1 = FakeTier1({"kind": "act", "steps": [{"action": "open_app", "app": "Canvas"}], "confidence": 0.95})
    routed = route("can you get canvas up for me", tier1)
    assert routed.tier == "1" and routed.plan.steps == (step("open_url", url="https://canvas.vt.edu"),)
    assert routed.thought.ms == 500.0


def test_always_listening_never_asks_the_llm():
    tier1 = FakeTier1()
    routed = route("can you get canvas up for me", tier1, strict=True)
    assert routed.plan is None and routed.tier == "none" and tier1.calls == []


def test_tier_1_problems_and_kinds_become_island_words():
    assert route("x", FakeTier1(Thought(error="Ollama isn't answering"))).note == "Ollama isn't answering"
    teach = route("turn on dark mode in fusion", FakeTier1({"kind": "teach", "steps": [], "confidence": 0.7}))
    assert teach.plan is None and "show me" in teach.note
    ignored = route("my roommate keeps opening the window", FakeTier1({"kind": "ignore", "steps": [], "confidence": 1}),
                    via="follow-up")
    assert ignored.quiet and ignored.plan is None
    addressed = route("my roommate keeps opening the window", FakeTier1({"kind": "ignore", "steps": [],
                                                                         "confidence": 1}), via="key")
    assert not addressed.quiet


def test_without_tier_1_its_said_plainly():
    assert route("make me a sandwich").note == "Didn't get “make me a sandwich”"


def test_settle_runs_the_option_he_picks():
    router = Router(FakeTier1())
    routed = router.route("open the microsoft one", EMPTY, CATALOG)  # Tier 0: Teams / Edge / Store?
    asker = Asker(choose=1)
    settled = router.settle(routed, "open the microsoft one", EMPTY, CATALOG, asker)
    assert settled.asked == "clarify" and settled.plan.kind == "act"
    assert asker.asked[0][0] == "choose"


def test_settle_yes_no_and_no_means_cancelled():
    plan = {"kind": "act", "steps": [{"action": "open_app", "app": "Notepad"}], "confidence": 0.4}
    router = Router(FakeTier1(plan))
    routed = router.route("I kinda want notepad up", EMPTY, CATALOG)
    assert routed.tier == "1" and routed.plan.kind == "clarify"
    yes = router.settle(routed, "I kinda want notepad up", EMPTY, CATALOG, Asker(confirm=True))
    assert yes.plan.steps == (step("open_app", app="Notepad"),)
    no = router.settle(routed, "I kinda want notepad up", EMPTY, CATALOG, Asker(confirm=False))
    assert no.plan is None and no.cancelled


def test_the_models_question_goes_back_to_it_with_his_answer():
    tier1 = FakeTier1(
        {"kind": "clarify", "steps": [], "confidence": 0.3, "question": "Open which one?",
         "choices": ["Google Chrome", "Notepad"]},
        {"kind": "act", "steps": [{"action": "open_app", "app": "Notepad"}], "confidence": 0.9},
    )
    router = Router(tier1)
    routed = router.route("open the thing", EMPTY, CATALOG)
    moves = []
    settled = router.settle(routed, "open the thing", EMPTY, CATALOG, Asker(choose=1), progress=moves.append)
    assert settled.plan.steps == (step("open_app", app="Notepad"),) and len(settled.rounds) == 2
    assert tier1.calls[1] == ("open the thing", "Open which one?", "Notepad")
    assert moves == ["thinking"]


def test_an_open_question_takes_whatever_he_says():
    tier1 = FakeTier1(
        {"kind": "clarify", "steps": [], "confidence": 0.3, "question": "What should I open?"},
        {"kind": "act", "steps": [{"action": "open_app", "app": "Calculator"}], "confidence": 0.9},
    )
    router = Router(tier1)
    routed = router.route("open it", EMPTY, CATALOG)
    asker = Asker(answer="the calculator")
    settled = router.settle(routed, "open it", EMPTY, CATALOG, asker)
    assert asker.asked == [("answer", "What should I open?")]
    assert settled.plan.steps == (step("open_app", app="Calculator"),)


def test_questions_stop_after_two_replans():
    question = {"kind": "clarify", "steps": [], "confidence": 0.3, "question": "Which?", "choices": ["A", "B"]}
    router = Router(FakeTier1(question, question, question))
    routed = router.route("do the thing", EMPTY, CATALOG)
    settled = router.settle(routed, "do the thing", EMPTY, CATALOG, Asker())
    assert settled.plan is None and "another way" in settled.note and len(settled.rounds) == 3


def test_handoffs_say_what_they_would_do():
    plan = Plan("handoff", handoff={"to": "claude", "mode": "text", "prompt": "write a cover letter for bosch"})
    assert describe_handoff(plan) == "Would hand off to Claude: “write a cover letter for bosch”"
    voice = Plan("handoff", handoff={"to": "chatgpt", "mode": "voice", "prompt": "x" * 80})
    assert describe_handoff(voice).startswith("Would hand off to ChatGPT (voice): “xxx") and "…" in describe_handoff(voice)


def test_abort_reaches_tier_1():
    tier1 = FakeTier1()
    Router(tier1).abort()
    assert tier1.aborted


def test_tier_0_guesses_let_tier_1_try_first():
    tier1 = FakeTier1({"kind": "act", "steps": [{"action": "tab", "op": "new"}], "confidence": 0.9})
    routed = route("I need a new dab", tier1)
    assert routed.tier == "1" and routed.plan.steps == (step("tab", op="new"),)
    unsure = FakeTier1({"kind": "clarify", "steps": [], "confidence": 0.3, "question": "Open what?"})
    routed = route("I need a new dab", unsure)
    assert routed.tier == "0" and routed.plan.kind == "clarify" and routed.plan.choices == ("Youtube",)
    assert route("I need a new dab", FakeTier1(Thought(error="Ollama isn't answering"))).plan.kind == "clarify"
    sure = FakeTier1()
    assert route("open chrome", sure).tier == "0" and sure.calls == []  # a sure Tier 0 plan never asks the model


# --- Stage 06: the page in front -------------------------------------------------------------------------------------

from nova.actions.chrome import PageRows  # noqa: E402
from nova.chrome.pagemap import Element, Scored  # noqa: E402


class PageTier1(FakeTier1):
    def think(self, text, seen, catalog=None, *, asked=None, answer=None, choice=None, page=None):
        self.pages = getattr(self, "pages", []) + [page]
        return super().think(text, seen, catalog, asked=asked, answer=answer, choice=choice)


SIGN_IN = Element("0:3", "button", "Sign in")


def test_a_plain_click_on_the_page_needs_no_llm():
    direct = step("page_click", description="hit that sign in thing", element="0:3", label="Sign in")
    pages = []

    def page(text, ctx):
        pages.append(text)
        return PageRows((Scored(SIGN_IN, 100.0, 0),), 12.0, direct)

    tier1 = PageTier1()
    routed = Router(tier1, page=page).route("hit that sign in thing", EMPTY, CATALOG)
    assert routed.tier == "page" and routed.plan.steps == (direct,) and routed.page_ms == 12.0
    assert tier1.calls == [] and pages == ["hit that sign in thing"]


def test_otherwise_tier_1_sees_the_same_page_once():
    rows = PageRows((Scored(SIGN_IN, 60.0, 0), Scored(Element("0:4", "link", "Help"), 0.0, 1)), 9.0)
    fetched = []

    def page(text, ctx):
        fetched.append(text)
        return rows

    tier1 = PageTier1({"kind": "act", "steps": [{"action": "page_click", "description": "the blue one", "element": 2}],
                       "confidence": 0.9})
    routed = Router(tier1, page=page).route("press the blue one", EMPTY, CATALOG)
    assert routed.tier == "1" and routed.plan.steps == (
        step("page_click", description="the blue one", element="0:4", label="Help"),)
    assert fetched == ["press the blue one"] and tier1.pages == [["1 button: Sign in", "2 link: Help"]]
    assert routed.page_ms == 9.0


def test_always_listening_never_asks_the_llm_about_a_guess_or_an_unsafe_plan():
    for text in ("open foam", "close notepad", "type hello world", "click the first link"):
        tier1 = FakeTier1()
        routed = route(text, tier1, strict=True, via="always")
        assert tier1.calls == [] and routed.plan is None and routed.tier == "none", text


@pytest.mark.parametrize("speakers", [False, True])
@pytest.mark.parametrize(
    "heard",
    ["open spotify", "pause", "next", "stop", "new tab", "volume up", "open foam", "close notepad",
     "can you get canvas up for me", "open the zorblax", "type hello world", "um next please"],
)
def test_speech_nobody_addressed_to_nova_never_reaches_tier_1(heard, speakers):
    """SPEC §5: what the gate lets through un-addressed is safe-only, and safe-only routes stay on Tier 0."""
    env = Env(WakeName.of("Nova"), True, Tier0Resolver(CATALOG), speakers=speakers)
    decision = decide(heard, Addressing(), env)
    if not decision.accepted:
        return  # nothing reaches the brain at all
    assert decision.via == "always" and decision.safe_only
    tier1 = FakeTier1()
    routed = route(decision.text, tier1, strict=decision.safe_only, via=decision.via)
    assert tier1.calls == [] and routed.tier == "0" and routed.plan is not None and routed.plan.kind == "act"


def test_always_listening_never_reads_the_page():
    fetched = []
    Router(PageTier1(), page=lambda text, ctx: fetched.append(text)).route("press the blue one", EMPTY, CATALOG,
                                                                          strict=True)
    assert fetched == []
