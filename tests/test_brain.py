"""The brain: one command start to finish (handle), and asking in the island (IslandAsker). No windows opened."""

import queue
import threading

from catalog_fixture import CATALOG
from test_executor import FakeAsker, FakeHands

from nova.actions.context import Context
from nova.brain.brain import Command, IslandAsker, command_type, handle
from nova.brain.executor import Executor
from nova.brain.plan import act, step
from nova.bus import Bus
from nova.events import AnswerHeard, Asking, BrainProgress, CommandFinished, IslandAction, SessionCancelled

EMPTY = Context(None, (), (), None, False, 1.5)


def run(text, strict=False, hands=None):
    events = []
    hands = hands or FakeHands()
    executor = Executor(hands, FakeAsker())
    result, record = handle(Command(text, utterance=3, via="key", strict=strict, at=0.0), CATALOG, executor,
                            publish=events.append, snap=lambda catalog: EMPTY)
    return result, record, events, hands


def test_a_command_runs_and_reports():
    result, record, events, hands = run("open chrome")
    assert result.ok and hands.ran == [step("open_app", app="Google Chrome")]
    assert BrainProgress("acting") in events and CommandFinished(True, "did open_app") in events
    assert record["type"] == "open_app" and record["tier"] == "0" and record["ctx_ms"] == 1.5
    assert record["plan"] == [{"action": "open_app", "app": "Google Chrome"}]


def test_what_tier_0_doesnt_get_is_said_plainly():
    result, record, events, hands = run("make me a sandwich")
    assert result is None and hands.ran == [] and record["tier"] == "none"
    assert events == [CommandFinished(False, "Didn't get “make me a sandwich”")]


def test_always_listening_runs_safe_plans_only():
    _, record, events, hands = run("close notepad", strict=True)
    assert hands.ran == [] and record["tier"] == "none"
    result, *_ = run("open notepad", strict=True)
    assert result.ok


def test_command_types_for_latency():
    assert command_type(act(step("window", op="maximize", target="active"))) == "window:maximize"
    assert command_type(act(step("open_url", url="https://canvas.vt.edu"))) == "open_url"
    assert command_type(None) == "none"


def ask(method, *args, answers=()):
    """Run an asker call on a thread, feed it ``answers`` once it has asked, return (result, bus events)."""
    bus = Bus()
    seen = []
    bus.subscribe(object, seen.append)
    inbox = queue.SimpleQueue()
    asker = IslandAsker(bus, inbox)
    out = []
    thread = threading.Thread(target=lambda: out.append(getattr(asker, method)(*args)))
    thread.start()
    for answer in answers:
        inbox.put(answer)
    thread.join(5)
    return out[0], seen


def test_confirm_yes_by_click_or_key():
    answer, seen = ask("confirm", "Send “late”?", answers=[IslandAction("yes", 1.0, via="key")])
    assert answer is True
    assert seen[:2] == [Asking("confirm", "Send “late”?"), BrainProgress("confirm")]
    assert seen[-1] == BrainProgress("acting")


def test_confirm_by_voice():
    answer, _ = ask("confirm", "Close all 3?", answers=[AnswerHeard(4, "uh what", 1.0), AnswerHeard(5, "yes", 1.1)])
    assert answer is True
    answer, seen = ask("confirm", "Close all 3?", answers=[AnswerHeard(4, "no", 1.0)])
    assert answer is False and seen[-1] == BrainProgress("idle", "Cancelled")


def test_confirm_cancelled():
    answer, _ = ask("confirm", "Send?", answers=[SessionCancelled("Esc", 1.0)])
    assert answer is None
    answer, _ = ask("confirm", "Send?", answers=[IslandAction("no", 1.0)])
    assert answer is None


def test_clarify_answers():
    choices = ("Google Chrome", "Calculator")
    assert ask("choose", "Open which one?", choices, answers=[AnswerHeard(1, "the second one", 1.0)])[0] == 1
    assert ask("choose", "Open which one?", choices, answers=[IslandAction("choice", 1.0, index=0)])[0] == 0
    index, seen = ask("choose", "Open which one?", choices, answers=[AnswerHeard(1, "nope", 1.0)])
    assert index is None and seen[-1] == BrainProgress("idle", "Cancelled")
    assert Asking("clarify", "Open which one?", choices) in seen
