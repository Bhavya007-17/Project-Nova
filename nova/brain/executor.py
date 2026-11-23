"""The executor (SPEC §8, §14): runs a plan's steps in order.

For each step: resolve it against the world first (hands that know pages / Canvas turn "the blue button" into
an element, "my assignment for today" into the item's address; several candidates come back as a ``Pick`` he
chooses from in the island; a resolution can also be the answer itself, "Nothing due today — next: …") ->
risk check on the resolved step (``nova.brain.risk``: so a vague "the blue button" that is "Submit Assignment"
still asks) -> refuse, or confirm through the asker (the element highlighted meanwhile) -> do it (the hands
time-box and verify their own work) -> result {ok, detail, ms}. The first failure stops the plan. A clarify plan
asks which option first. Pure orchestration: the hands (Windows / Chrome) and the asker (island / console) are
passed in, so tests run it with fakes.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..actions.native import ActionError
from .plan import Plan, Step, act
from .risk import Scene, assess

_log = logging.getLogger(__name__)

MAX_RESOLVES = 3  # resolve -> pick -> resolve the chosen one... never more than this per step


class Hands(Protocol):
    def scene(self, s: Step, ctx: Any) -> Scene: ...
    def label(self, s: Step) -> str: ...
    def run(self, s: Step, ctx: Any) -> Any: ...  # an Outcome (detail, method, hold); raises ActionError
    # optional: resolve(s, ctx) -> Step | Pick | Outcome; preview(s, on) highlights what a confirm is about


class Asker(Protocol):
    def confirm(self, question: str) -> bool | None: ...  # None = no answer / cancelled
    def choose(self, question: str, choices: Sequence[str]) -> int | None: ...


@dataclass(frozen=True)
class Pick:
    """A step that needs him to choose: each choice is a step of its own (resolved further if need be)."""

    question: str
    choices: tuple[str, ...]
    steps: tuple[Step, ...]
    optional: bool = False  # a list to read ("what's due this week"): no answer is fine...
    detail: str = ""  # ...and this is the result then
    hold: float = 0.0


@dataclass(frozen=True)
class StepResult:
    action: str
    ok: bool
    detail: str
    ms: float
    method: str = ""
    # a task's hops (``nova.brain.task.Result``), for `nova latency`
    pick_ms: tuple[float, ...] = ()
    step_ms: tuple[float, ...] = ()
    ring_ms: tuple[float, ...] = ()


@dataclass(frozen=True)
class RunResult:
    ok: bool
    detail: str
    steps: tuple[StepResult, ...] = ()
    cancelled: bool = False
    asked: str = ""  # confirm | clarify (he was asked something)
    started_at: float | None = None  # perf_counter when the first action began
    plan: Plan | None = None  # what ran (the chosen option for a clarify)
    hold: float = 0.0  # the result is an answer to read: keep it on screen this long


def repeats(plan: Plan) -> bool:
    """ "Do that again": a plan that is only that."""
    return (plan.kind == "act" and len(plan.steps) == 1 and plan.steps[0].action == "nova"
            and plan.steps[0].get("op") == "again")


def _about_nova(plan: Plan) -> bool:
    """Help and "again" are never what "again" repeats."""
    return all(s.action == "nova" and s.get("op") in ("help", "again") for s in plan.steps)


def _replay(plan: Plan, kept: dict[int, tuple[Step, ...]]) -> Plan:
    """What "do that again" runs: a task's clicks instead of the task (it repeats what was done, not the search
    for it). A task that did nothing is asked again."""
    if not any(s.action == "task" for s in plan.steps):
        return plan
    steps: list[Step] = []
    for i, s in enumerate(plan.steps):
        if s.action != "task":
            steps.append(s)
        elif kept.get(i):
            steps.extend(kept[i])
        else:
            return plan
    return act(*steps)


class _Stop(Exception):
    """The step can't go on: cancelled (no / Esc / no answer), refused or failed. Carries the run's outcome."""

    def __init__(self, detail: str, *, cancelled: bool = False, failed: StepResult | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.cancelled = cancelled
        self.failed = failed


class Executor:
    def __init__(
        self,
        hands: Hands,
        asker: Asker,
        *,
        progress: Callable[[tuple[str, ...], int], None] = lambda labels, active: None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._hands = hands
        self._asker = asker
        self._progress = progress
        self._clock = clock
        self._cancel = threading.Event()
        self._last: Plan | None = None  # what "do that again" runs
        self._task_last: tuple[str, tuple[Step, ...]] | None = None  # the last task's goal + what it did
        self._kept: dict[int, tuple[Step, ...]] = {}  # this plan's tasks: step index -> the steps they did

    @property
    def asker(self) -> Asker:
        return self._asker

    def cancel(self) -> None:
        """Any thread: stop before the next step."""
        self._cancel.set()

    def run(self, plan: Plan, ctx: Any) -> RunResult:
        self._cancel.clear()
        if repeats(plan):
            if self._last is None:
                return RunResult(False, "Nothing to repeat yet.", plan=plan)
            plan = self._last  # asks again whatever it asked the first time (risk is checked afresh)
        result = self._run_plan(plan, ctx)
        if result.ok and result.plan is not None and result.plan.kind == "act" and not _about_nova(result.plan):
            self._last = _replay(result.plan, self._kept)
        return result

    def _run_plan(self, plan: Plan, ctx: Any) -> RunResult:
        if plan.kind == "clarify":
            if len(plan.choices) == 1:  # "Close Notepad?": a yes / no question (Enter = yes)
                index = 0 if self._asker.confirm(plan.question) else None
            else:
                index = self._asker.choose(plan.question, plan.choices)
            chosen = plan.option(index) if index is not None else None
            if chosen is None:
                return RunResult(False, "Cancelled", cancelled=True, asked="clarify")
            result = self._act(chosen, ctx)
            return RunResult(result.ok, result.detail, result.steps, result.cancelled, "clarify", result.started_at,
                             chosen, result.hold)
        if plan.kind != "act":
            return RunResult(False, f"I can't do “{plan.kind}” yet.", plan=plan)
        return self._act(plan, ctx)

    # --- one plan ---------------------------------------------------------------------------------------------

    def _act(self, plan: Plan, ctx: Any) -> RunResult:
        labels = [self._hands.label(s) for s in plan.steps]
        results: list[StepResult] = []
        details: list[str] = []
        self._asked = ""
        self._started: float | None = None
        self._kept = {}
        hold = 0.0
        for index, s in enumerate(plan.steps):
            if self._cancel.is_set():
                return RunResult(False, "Cancelled", tuple(results), True, self._asked, self._started, plan, hold)
            try:
                result, extra = self._step(s, ctx, labels, index)
            except _Stop as stop:
                if stop.failed is not None:
                    results.append(stop.failed)
                return RunResult(False, stop.detail, tuple(results), stop.cancelled, self._asked, self._started, plan)
            results.append(result)
            details.append(result.detail)
            hold = max(hold, extra)
        return RunResult(True, " · ".join(d for d in details if d), tuple(results), False, self._asked, self._started,
                         plan, hold)

    def _step(self, s: Step, ctx: Any, labels: list[str], index: int) -> tuple[StepResult, float]:
        """Resolve, ask what needs asking, check the risk, run. Returns the result and how long to show it."""
        if s.action == "task":
            return self._task(s, ctx, labels, index), 0.0
        current: Any = s
        for _ in range(MAX_RESOLVES):
            began = self._clock()
            try:
                current = self._resolve(current, ctx)
            except ActionError as error:
                raise _Stop(str(error), failed=StepResult(s.action, False, str(error), (self._clock() - began) * 1000)) from None
            except Exception as error:  # noqa: BLE001 - a bug in one resolver must not take the brain down
                _log.exception("%s failed to resolve", s.action)
                raise _Stop("Something went wrong — details in the log.",
                            failed=StepResult(s.action, False, f"{s.action} failed: {error}", 0.0)) from None
            if isinstance(current, Pick):
                if self._started is None:
                    self._started = self._clock()  # the list in the island is the first thing he sees
                chosen = self._pick(current)
                if chosen is None:
                    if current.optional:
                        return StepResult(s.action, True, current.detail, (self._clock() - began) * 1000), current.hold
                    raise _Stop("Cancelled", cancelled=True)
                current = chosen
                continue
            if not isinstance(current, Step):  # the resolution is the answer ("Nothing due today — next: …")
                if self._started is None:
                    self._started = self._clock()
                detail = getattr(current, "detail", str(current))
                return (StepResult(s.action, True, detail, (self._clock() - began) * 1000, getattr(current, "method", "")),
                        float(getattr(current, "hold", 0.0) or 0.0))
            break
        if not isinstance(current, Step):
            raise _Stop("I couldn't settle what to do.", failed=StepResult(s.action, False, "too many questions", 0.0))
        labels[index] = self._hands.label(current)
        return self._run(current, ctx, labels, index)

    def _task(self, s: Step, ctx: Any, labels: list[str], index: int) -> StepResult:
        """A job of several clicks (``nova.brain.task``): the hands look, pick, check the risk and act until it's
        done; each click that needs a yes asks through the same asker. No goal: the last task ("just do it",
        "let me do it", "show me again")."""
        task = getattr(self._hands, "task", None)
        if task is None:
            raise _Stop("I can't do tasks here.", failed=StepResult("task", False, "no task hands", 0.0))
        goal = s.get("goal")
        kept: tuple[Step, ...] = ()
        if not goal:
            if self._task_last is None:
                detail = "No task yet — say what you want done."
                raise _Stop(detail, failed=StepResult("task", False, detail, 0.0))
            goal, kept = self._task_last

        def confirm(question: str) -> bool | None:
            self._asked = "confirm"
            return self._asker.confirm(question)

        def progress(label: str, n: int) -> None:
            labels[index] = label
            self._progress(tuple(labels), index)

        began = self._clock()
        if self._started is None:
            self._started = began
        self._progress(tuple(labels), index)
        try:
            result = task(goal, s.get("mode") or "do", kept=kept, point=bool(s.get("point")), confirm=confirm,
                          stopped=self._cancel.is_set, progress=progress, ctx=ctx)
        except ActionError as error:
            raise _Stop(str(error), failed=StepResult("task", False, str(error), (self._clock() - began) * 1000)) from None
        except Exception as error:  # noqa: BLE001 - a bug in the loop must not take the brain down
            _log.exception("task failed")
            raise _Stop("Something went wrong — details in the log.",
                        failed=StepResult("task", False, f"task failed: {error}", (self._clock() - began) * 1000)) from None
        ms = (self._clock() - began) * 1000
        self._task_last = (goal, tuple(result.kept) or kept)
        self._kept[index] = tuple(result.kept)
        hops = (tuple(result.pick_ms), tuple(result.step_ms), tuple(result.ring_ms))
        if result.exit == "done":
            return StepResult("task", True, result.detail, ms, "task", *hops)
        if result.exit in ("stopped", "declined"):
            raise _Stop("Cancelled" if result.exit == "declined" else result.detail, cancelled=True)
        raise _Stop(result.detail, failed=StepResult("task", False, result.detail, ms, result.exit, *hops))

    def _resolve(self, s: Step, ctx: Any) -> Any:
        resolve = getattr(self._hands, "resolve", None)
        return resolve(s, ctx) if resolve is not None else s

    def _pick(self, pick: Pick) -> Step | None:
        self._asked = "clarify"
        if len(pick.steps) == 1 and not pick.optional:
            return pick.steps[0] if self._asker.confirm(pick.question) else None
        index = self._asker.choose(pick.question, pick.choices)
        return pick.steps[index] if index is not None and 0 <= index < len(pick.steps) else None

    def _preview(self, s: Step, on: bool) -> None:
        preview = getattr(self._hands, "preview", None)
        if preview is None:
            return
        try:
            preview(s, on)
        except Exception:  # noqa: BLE001 - a highlight that didn't show is no reason to stop
            _log.debug("preview failed", exc_info=True)

    def _run(self, s: Step, ctx: Any, labels: list[str], index: int) -> tuple[StepResult, float]:
        risk = assess(s, self._hands.scene(s, ctx))
        if risk is not None and risk.refuse:
            raise _Stop(risk.reason, failed=StepResult(s.action, False, risk.reason, 0.0))
        if risk is not None and risk.confirm:
            self._asked = "confirm"
            self._preview(s, True)
            answer = self._asker.confirm(risk.question)
            if not answer:
                self._preview(s, False)
                raise _Stop("Cancelled", cancelled=True)
        if self._cancel.is_set():
            raise _Stop("Cancelled", cancelled=True)
        self._progress(tuple(labels), index)
        began = self._clock()
        if self._started is None:
            self._started = began
        try:
            outcome = self._hands.run(s, ctx)
        except ActionError as error:
            raise _Stop(str(error), failed=StepResult(s.action, False, str(error), (self._clock() - began) * 1000)) from None
        except Exception as error:  # noqa: BLE001 - a bug in one action must not take the brain down
            _log.exception("%s failed", s.action)
            raise _Stop("Something went wrong — details in the log.",
                        failed=StepResult(s.action, False, f"{s.action} failed: {error}", (self._clock() - began) * 1000)) from None
        detail = getattr(outcome, "detail", str(outcome))
        return (StepResult(s.action, True, detail, (self._clock() - began) * 1000, getattr(outcome, "method", "")),
                float(getattr(outcome, "hold", 0.0) or 0.0))
