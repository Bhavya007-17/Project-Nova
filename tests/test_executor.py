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


# --- Stage 06: steps resolved against the page / Canvas before they run ----------------------------------------------

from nova.brain.executor import Pick  # noqa: E402


class ResolvingHands(FakeHands):
    """page_click "the blue button" resolves to an element; canvas_open_item may need a choice; canvas_due answers."""

    def __init__(self, resolutions, scene=None):
        super().__init__(scene=scene)
        self.resolutions = resolutions
        self.previews = []

    def resolve(self, s, ctx):
        found = self.resolutions.get(s.action)
        return found(s) if callable(found) else (found if found is not None else s)

    def preview(self, s, on):
        self.previews.append((s.get("label"), on))


def test_a_resolved_click_is_risk_checked_on_its_real_label():
    resolved = step("page_click", description="the blue button", element="0:4", label="Submit Assignment")
    hands = ResolvingHands({"page_click": resolved})
    result, hands, asker = run(act(step("page_click", description="the blue button")), hands=hands)
    assert result.ok and asker.questions == ["Click “Submit Assignment”?"] and hands.ran == [resolved]
    assert hands.previews == [("Submit Assignment", True)]  # highlighted while asking; the click itself flashes it
    result, hands, _ = run(act(step("page_click", description="the blue button")),
                           hands=ResolvingHands({"page_click": resolved}), asker=FakeAsker(confirm=False))
    assert result.cancelled and hands.ran == [] and hands.previews == [("Submit Assignment", True), ("Submit Assignment", False)]


def test_several_matches_are_a_numbered_choice():
    options = (step("canvas_open", url="https://canvas.vt.edu/a/1", title="HW 5"),
               step("canvas_open", url="https://canvas.vt.edu/a/2", title="Pre-lab"))
    pick = Pick("2 due today — which one?", ("HW 5 · today", "Pre-lab · today"), options)
    hands = ResolvingHands({"canvas_open_item": pick})
    result, hands, asker = run(act(step("canvas_open_item", item="my assignment for today")), hands=hands,
                               asker=FakeAsker(choose=1))
    assert result.ok and hands.ran == [options[1]] and result.asked == "clarify"
    assert asker.questions == [("2 due today — which one?", ("HW 5 · today", "Pre-lab · today"))]
    result, hands, _ = run(act(step("canvas_open_item", item="x")), hands=ResolvingHands({"canvas_open_item": pick}),
                           asker=FakeAsker(choose=None))
    assert result.cancelled and hands.ran == []


def test_one_match_in_a_pick_is_yes_no():
    pick = Pick("Open HW 5?", ("HW 5",), (step("canvas_open", url="https://canvas.vt.edu/a/1", title="HW 5"),))
    result, hands, asker = run(act(step("canvas_open_item", item="x")), hands=ResolvingHands({"canvas_open_item": pick}))
    assert result.ok and asker.questions == ["Open HW 5?"] and len(hands.ran) == 1


def test_an_optional_list_needs_no_answer():
    pick = Pick("3 due this week", ("HW 5 · today", "Lab 3 · Fri", "Quiz 2 · Nov 10"),
                tuple(step("canvas_open", url=f"https://canvas.vt.edu/a/{i}", title=str(i)) for i in range(3)),
                optional=True, detail="3 due this week", hold=6.0)
    result, hands, _ = run(act(step("canvas_due", range="week")), hands=ResolvingHands({"canvas_due": pick}),
                           asker=FakeAsker(choose=None))
    assert result.ok and not result.cancelled and result.detail == "3 due this week" and hands.ran == []
    result, hands, _ = run(act(step("canvas_due", range="week")), hands=ResolvingHands({"canvas_due": pick}),
                           asker=FakeAsker(choose=2))
    assert result.ok and hands.ran == [pick.steps[2]]


def test_a_resolution_can_be_the_answer_itself():
    answer = Outcome("Nothing due today — next: HW 6 (Thermodynamics), Nov 12", hold=6.0)
    result, hands, _ = run(act(step("canvas_open_item", item="today")), hands=ResolvingHands({"canvas_open_item": answer}))
    assert result.ok and result.detail.startswith("Nothing due today") and result.hold == 6.0 and hands.ran == []


def test_resolution_failures_stop_the_plan():
    def broken(s):
        raise ActionError("Couldn't find “purple” on this page.")

    result, hands, _ = run(act(step("page_click", description="purple"), step("tab", op="new")),
                           hands=ResolvingHands({"page_click": broken}))
    assert not result.ok and result.detail == "Couldn't find “purple” on this page." and hands.ran == []


def test_a_list_or_an_answer_counts_as_the_start():
    pick = Pick("3 due this week", ("a", "b", "c"),
                tuple(step("canvas_open", url=f"https://canvas.vt.edu/a/{i}", title=str(i)) for i in range(3)),
                optional=True, detail="3 due this week")
    result, _, _ = run(act(step("canvas_due", range="week")), hands=ResolvingHands({"canvas_due": pick}),
                       asker=FakeAsker(choose=None))
    assert result.started_at is not None
    result, _, _ = run(act(step("canvas_due", range="today")),
                       hands=ResolvingHands({"canvas_due": Outcome("Nothing due today", hold=6.0)}))
    assert result.started_at is not None
