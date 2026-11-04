"""The executor (SPEC §8, §14): steps in order, risk check first, confirm / clarify through an asker, stop on failure."""

from nova.actions.apps import Outcome
from nova.actions.native import ActionError
from nova.brain.executor import Executor
from nova.brain.plan import Plan, act, step
from nova.brain.risk import Scene


class FakeHands:
    def __init__(self, scene=None, fail=None):
        self.ran = []
        self._scene = scene or Scene()
        self._fail = fail

    def scene(self, s, ctx):
        return self._scene

    def label(self, s):
        return s.action

    def run(self, s, ctx):
        if s.action == self._fail:
            raise ActionError(f"{s.action} broke")
        self.ran.append(s)
        return Outcome(f"did {s.action}", "plain")


class FakeAsker:
    def __init__(self, confirm=True, choose=0):
        self.questions = []
        self._confirm = confirm
        self._choose = choose

    def confirm(self, question):
        self.questions.append(question)
        return self._confirm

    def choose(self, question, choices):
        self.questions.append((question, tuple(choices)))
        return self._choose


def run(plan, hands=None, asker=None, progress=None):
    hands = hands or FakeHands()
    asker = asker or FakeAsker()
    seen = []
    executor = Executor(hands, asker, progress=lambda labels, i: seen.append((labels, i)))
    result = executor.run(plan, ctx=None)
    if progress is not None:
        progress.extend(seen)
    return result, hands, asker


def test_steps_run_in_order_and_report_progress():
    seen = []
    result, hands, _ = run(act(step("open_app", app="Notepad"), step("type_text", text="hi", submit=True)), progress=seen)
    assert result.ok and [s.action for s in hands.ran] == ["open_app", "type_text"]
    assert result.detail == "did open_app · did type_text"
    assert seen == [(("open_app", "type_text"), 0), (("open_app", "type_text"), 1)]
    assert all(r.ok and r.ms >= 0 for r in result.steps) and result.started_at is not None


def test_refused_steps_never_run():
    hands = FakeHands(scene=Scene(password=True))
    result, hands, asker = run(act(step("type_text", text="hunter2")), hands=hands)
    assert not result.ok and "password" in result.detail and hands.ran == [] and asker.questions == []


def test_risky_steps_wait_for_yes():
    teams = Scene(app="Microsoft Teams", exe="ms-teams.exe")
    result, hands, asker = run(act(step("type_text", text="late", submit=True)), hands=FakeHands(teams))
    assert result.ok and asker.questions == ["Send “late”?"] and len(hands.ran) == 1
    for answer in (False, None):
        result, hands, _ = run(act(step("type_text", text="late", submit=True)), hands=FakeHands(teams),
                               asker=FakeAsker(confirm=answer))
        assert not result.ok and result.cancelled and hands.ran == []


def test_a_failing_step_stops_the_rest():
    hands = FakeHands(fail="window")
    plan = act(step("open_app", app="Claude"), step("window", op="maximize", target="active"), step("tab", op="new"))
    result, hands, _ = run(plan, hands=hands)
    assert not result.ok and result.detail == "window broke" and [s.action for s in hands.ran] == ["open_app"]
    assert [r.ok for r in result.steps] == [True, False]


def test_clarify_asks_then_runs_the_chosen_option():
    options = (act(step("open_app", app="Google Chrome")), act(step("open_app", app="Calculator")))
    plan = Plan("clarify", question="Open which one?", choices=("Google Chrome", "Calculator"), options=options)
    result, hands, asker = run(plan, asker=FakeAsker(choose=1))
    assert result.ok and hands.ran == [step("open_app", app="Calculator")]
    assert asker.questions == [("Open which one?", ("Google Chrome", "Calculator"))]
    result, hands, _ = run(plan, asker=FakeAsker(choose=None))
    assert result.cancelled and hands.ran == []


def test_cancel_stops_before_the_next_step():
    class Cancelling(FakeHands):
        def run(self, s, ctx):
            outcome = super().run(s, ctx)
            executor.cancel()
            return outcome

    hands = Cancelling()
    executor = Executor(hands, FakeAsker())
    result = executor.run(act(step("open_app", app="Notepad"), step("tab", op="new")), ctx=None)
    assert result.cancelled and [s.action for s in hands.ran] == ["open_app"]


def test_other_plan_kinds_wait_for_later_stages():
    result, hands, _ = run(Plan("handoff", handoff={"to": "claude"}))
    assert not result.ok and hands.ran == []


def test_a_single_choice_is_a_yes_no_question():
    plan = Plan("clarify", question="Close Notepad?", choices=("Notepad",), options=(act(step("close_app", app="Notepad")),))
    result, hands, asker = run(plan, asker=FakeAsker(confirm=True))
    assert result.ok and asker.questions == ["Close Notepad?"] and hands.ran == [step("close_app", app="Notepad")]
    result, hands, _ = run(plan, asker=FakeAsker(confirm=False))
    assert result.cancelled and hands.ran == []
