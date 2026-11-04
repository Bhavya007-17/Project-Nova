"""The executor (SPEC §8, §14): runs a plan's steps in order.

For each step: risk check (``nova.brain.risk``) -> refuse, or confirm through the asker
(the island) -> do it (the hands time-box and verify their own work) -> result
{ok, detail, ms}. The first failure stops the plan. A clarify plan asks which option first.
Pure orchestration: the hands (Windows) and the asker (island / console) are passed in,
so tests run it with fakes.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..actions.native import ActionError
from .plan import Plan, Step
from .risk import Scene, assess

_log = logging.getLogger(__name__)


class Hands(Protocol):
    def scene(self, s: Step, ctx: Any) -> Scene: ...
    def label(self, s: Step) -> str: ...
    def run(self, s: Step, ctx: Any) -> Any: ...  # an Outcome (detail, method); raises ActionError


class Asker(Protocol):
    def confirm(self, question: str) -> bool | None: ...  # None = no answer / cancelled
    def choose(self, question: str, choices: Sequence[str]) -> int | None: ...


@dataclass(frozen=True)
class StepResult:
    action: str
    ok: bool
    detail: str
    ms: float
    method: str = ""


@dataclass(frozen=True)
class RunResult:
    ok: bool
    detail: str
    steps: tuple[StepResult, ...] = ()
    cancelled: bool = False
    asked: str = ""  # confirm | clarify (he was asked something)
    started_at: float | None = None  # perf_counter when the first action began
    plan: Plan | None = None  # what ran (the chosen option for a clarify)


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

    def cancel(self) -> None:
        """Any thread: stop before the next step."""
        self._cancel.set()

    def run(self, plan: Plan, ctx: Any) -> RunResult:
        self._cancel.clear()
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
                             chosen)
        if plan.kind != "act":
            return RunResult(False, f"I can't do “{plan.kind}” yet.", plan=plan)
        return self._act(plan, ctx)

    def _act(self, plan: Plan, ctx: Any) -> RunResult:
        labels = tuple(self._hands.label(s) for s in plan.steps)
        results: list[StepResult] = []
        details: list[str] = []
        asked = ""
        started_at: float | None = None
        for index, s in enumerate(plan.steps):
            if self._cancel.is_set():
                return RunResult(False, "Cancelled", tuple(results), True, asked, started_at, plan)
            risk = assess(s, self._hands.scene(s, ctx))
            if risk is not None and risk.refuse:
                results.append(StepResult(s.action, False, risk.reason, 0.0))
                return RunResult(False, risk.reason, tuple(results), False, asked, started_at, plan)
            if risk is not None and risk.confirm:
                asked = "confirm"
                answer = self._asker.confirm(risk.question)
                if not answer:
                    return RunResult(False, "Cancelled", tuple(results), True, asked, started_at, plan)
            self._progress(labels, index)
            began = self._clock()
            if started_at is None:
                started_at = began
            try:
                outcome = self._hands.run(s, ctx)
            except ActionError as error:
                results.append(StepResult(s.action, False, str(error), (self._clock() - began) * 1000))
                return RunResult(False, str(error), tuple(results), False, asked, started_at, plan)
            except Exception as error:  # noqa: BLE001 - a bug in one action must not take the brain down
                _log.exception("%s failed", s.action)
                results.append(StepResult(s.action, False, f"{s.action} failed: {error}", (self._clock() - began) * 1000))
                return RunResult(False, "Something went wrong — details in the log.", tuple(results), False, asked,
                                 started_at, plan)
            detail = getattr(outcome, "detail", str(outcome))
            results.append(StepResult(s.action, True, detail, (self._clock() - began) * 1000,
                                      getattr(outcome, "method", "")))
            details.append(detail)
        return RunResult(True, " · ".join(d for d in details if d), tuple(results), False, asked, started_at, plan)
