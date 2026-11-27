"""Learned skills (SPEC §7.3, §12): what's worth keeping, which words match, teaching and forgetting by voice."""

from catalog_fixture import CATALOG
from test_executor import FakeAsker, FakeHands
from test_router import FakeTier1

from nova.actions.context import Context
from nova.brain.brain import Command, handle
from nova.brain.executor import Executor
from nova.brain.plan import act, step
from nova.brain.router import Router
from nova.events import CommandFinished
from nova.skills.book import Book, learnable, same, stable_steps, teaching, words

EMPTY = Context(None, (), (), None, False, 1.0)
CANVAS = act(step("open_url", url="https://canvas.vt.edu/courses/7"))


def test_the_words_that_carry_meaning():
    assert words("Could you open my thermo course on Canvas, please?") == ("open", "thermo", "course", "canvas")
    assert words("open the thermo course in canvas") == ("open", "thermo", "course", "canvas")


def test_paraphrases_match_and_other_intents_dont():
    key = words("open my thermo course on canvas")
    assert same(words("hey Nova, open the thermo course in Canvas"), key)
    assert same(words("open my thermos course on canvas"), key)  # a long word misheard by a letter
    assert not same(words("open my physics course on canvas"), key)
    assert not same(words("close my thermo course on canvas"), key)
    assert not same(words("open my thermo course"), key)
    assert not same(words("mute chrome tab 2"), words("unmute chrome tab 2"))
    assert not same(words("open chapter 12 notes"), words("open chapter 13 notes"))  # numbers are never "close"
    assert not same((), ())


def test_only_plans_that_mean_the_same_tomorrow_are_kept():
    assert learnable("open my thermo course on canvas", CANVAS) == [
        {"action": "open_url", "url": "https://canvas.vt.edu/courses/7"}]
    assert learnable("open that course", CANVAS) is None  # "that" points at the moment
    assert learnable("click the third one", act(step("page_click", description="third"))) is None
    assert learnable("canvas", CANVAS) is None  # one word is too little to match on
    # acting on whatever is in front isn't repeatable...
    assert stable_steps(act(step("press", keys="ctrl+s"))) is None
    assert stable_steps(act(step("page_click", description="Grades", element="e12"))) is None
    # ...unless the plan put it there first; page element ids never survive
    kept = stable_steps(act(step("open_url", url="https://canvas.vt.edu"),
                            step("page_click", description="Grades", element="e12", label="Grades")))
    assert kept == [{"action": "open_url", "url": "https://canvas.vt.edu"},
                    {"action": "page_click", "description": "Grades"}]
    assert stable_steps(act(step("window", op="maximize", target="Notepad"))) is not None
    assert stable_steps(act(step("window", op="maximize", target="active"))) is None
    assert stable_steps(act(step("dictation", op="start"))) is None


def test_a_book_learns_matches_and_survives_a_restart(tmp_path):
    book = Book(tmp_path)
    skill = book.learn("open my thermo course on canvas", CANVAS)
    assert skill is not None and (tmp_path / "open-thermo-course-canvas.json").exists()
    assert book.match("could you open the thermo course in canvas") == CANVAS
    assert book.match("open my physics course on canvas") is None
    again = Book(tmp_path)
    assert again.match("open thermo course on canvas") == CANVAS
    # the same steps under new words: one skill, two triggers
    assert again.learn("get thermodynamics up please", CANVAS).id == skill.id
    assert len(again.all()) == 1 and len(again.all()[0].triggers) == 2


def test_a_broken_skill_goes_stale_until_relearned(tmp_path):
    book = Book(tmp_path)
    book.learn("open my thermo course on canvas", CANVAS)
    book.failed("open my thermo course on canvas")
    assert book.match("open my thermo course on canvas") is None
    newer = act(step("open_url", url="https://canvas.vt.edu/courses/9"))
    book.learn("open my thermo course on canvas", newer)
    assert book.match("open my thermo course on canvas") == newer and len(book.all()) == 1


def test_a_bad_file_is_skipped(tmp_path):
    (tmp_path / "bad.json").write_text('{"name": "x", "triggers": ["do x now"], "steps": [{"action": "rm_rf"}]}')
    (tmp_path / "junk.json").write_text("not json")
    assert Book(tmp_path).all() == []


def test_teaching_phrases():
    assert teaching("When I say study mode, open Canvas and Notion") == ("study mode", "open Canvas and Notion")
    assert teaching("Nova, whenever I say 'movie time' then you should open Netflix.") == ("movie time", "open Netflix")
    assert teaching("when I say hello she laughs") is None
    assert teaching("open chrome") is None


def test_forget_and_list_by_voice(tmp_path):
    book = Book(tmp_path)
    assert "Nothing learned" in book.manage("what have you learned?")
    assert book.manage("forget that") == "Nothing learned just now to forget"
    book.learn("study mode", act(step("open_app", app="Notepad")), taught=True)
    assert book.manage("what have you learned") == "Learned: study mode"
    assert book.manage("forget the milk") is None  # not about a skill
    assert book.manage("forget study mode") == "Forgot “study mode”"
    assert book.all() == [] and list(tmp_path.glob("*.json")) == []
    book.learn("study mode", act(step("open_app", app="Notepad")), taught=True)
    assert book.manage("Forget that.") == "Forgot “study mode”"


# --- through the brain ------------------------------------------------------------------------------------------

LLM_CANVAS = {"kind": "act", "steps": [{"action": "open_url", "url": "https://canvas.vt.edu/courses/7"}],
              "confidence": 0.95}


def say(text, book, tier1, hands=None, strict=False):
    events = []
    hands = hands or FakeHands()
    result, record = handle(Command(text, via="key", strict=strict), CATALOG, Executor(hands, FakeAsker()),
                            router=Router(tier1, book), publish=events.append, snap=lambda c: EMPTY, skills=book)
    return result, record, events, hands


def test_the_llms_plan_replays_without_the_llm(tmp_path):
    book = Book(tmp_path)
    tier1 = FakeTier1(LLM_CANVAS)
    _, record, _, _ = say("pull up my thermo course page", book, tier1)
    assert record["tier"] == "1" and record["learned"] and len(tier1.calls) == 1
    result, record, events, hands = say("Could you pull up the thermo course page?", book, tier1)
    assert record["tier"] == "skill" and result.ok and len(tier1.calls) == 1  # no second LLM call
    assert hands.ran[-1] == step("open_url", url="https://canvas.vt.edu/courses/7")
    assert any(isinstance(e, CommandFinished) and e.skill for e in events)
    assert book.all()[0].uses == 1


def test_always_listening_never_runs_a_skill(tmp_path):
    book = Book(tmp_path)
    book.learn("pull up my thermo course page", CANVAS)
    result, record, _, hands = say("pull up my thermo course page", book, FakeTier1(), strict=True)
    assert result is None and hands.ran == [] and record["tier"] == "none"


def test_teach_by_voice_saves_without_running(tmp_path):
    book = Book(tmp_path)
    result, record, events, hands = say("When I say writing time, open Notepad", book, FakeTier1())
    assert result is None and hands.ran == [] and record["ok"]
    assert events == [CommandFinished(True, "Learned “writing time” — say it any time", skill=True, hold=3.0)]
    result, record, _, hands = say("writing time", book, FakeTier1())
    assert record["tier"] == "skill" and hands.ran == [step("open_app", app="Notepad")]
