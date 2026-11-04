"""The brain worker (SPEC §3): accepted commands -> Tier 0 -> plan -> executor -> island + log.

One thread ("nova-brain") takes accepted commands from the gate in order. For each: the
context snapshot, Tier 0, then the executor with the island as the asker (Confirm / Clarify
answered by voice, a click, Enter / 1-4 from the hook, or Esc / No / timeout). Every command
writes one trace line with its hop timings (SPEC §16); ``nova latency`` reads them.
Anything Tier 0 doesn't understand is said plainly in the island (Tier 1 arrives in Stage 05).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..actions import native
from ..actions.catalog import Catalog, CatalogStore
from ..actions.context import Context, snapshot
from ..actions.hands import NovaControls, WindowsHands
from ..bus import Bus
from ..events import (
    AnswerHeard,
    Asking,
    BrainProgress,
    CommandFinished,
    Gated,
    IslandAction,
    SessionCancelled,
    SleepRequested,
    SpeechEnded,
    StepsChanged,
)
from ..log import TraceLog
from ..state import CLARIFY_S, CONFIRM_S
from . import tier0
from .answers import pick, yes_no
from .executor import Executor, RunResult
from .plan import Plan

_log = logging.getLogger(__name__)

_KEEP = 16
_STOP = object()


class IslandAsker:
    """Confirm / Clarify in the island. Answers arrive on ``answers`` from the bus."""

    def __init__(self, bus: Bus, answers: queue.SimpleQueue) -> None:
        self._bus = bus
        self._answers = answers

    def _drain(self) -> None:
        while True:
            try:
                self._answers.get_nowait()
            except queue.Empty:
                return

    def _wait(self, seconds: float, judge: Callable[[object], object]) -> object:
        deadline = time.perf_counter() + seconds
        while True:
            left = deadline - time.perf_counter()
            if left <= 0:
                return None
            try:
                item = self._answers.get(timeout=left)
            except queue.Empty:
                return None
            verdict = judge(item)
            if verdict is not _KEEP_WAITING:
                return verdict

    def confirm(self, question: str) -> bool | None:
        self._drain()
        self._bus.publish(Asking("confirm", question))
        self._bus.publish(BrainProgress("confirm"))

        def judge(item: object) -> object:
            if isinstance(item, IslandAction):
                return True if item.what == "yes" else (None if item.what == "no" else _KEEP_WAITING)
            if isinstance(item, AnswerHeard):
                said = yes_no(item.text)
                if said is None:
                    return _KEEP_WAITING
                if not said:
                    self._bus.publish(BrainProgress("idle", "Cancelled"))
                return said
            if isinstance(item, SessionCancelled):
                return None
            return _KEEP_WAITING

        answer = self._wait(CONFIRM_S + 1.0, judge)
        if answer:
            self._bus.publish(BrainProgress("acting"))
        return bool(answer) if answer is not None else None

    def choose(self, question: str, choices: Sequence[str]) -> int | None:
        self._drain()
        self._bus.publish(Asking("clarify", question, tuple(choices)))
        self._bus.publish(BrainProgress("clarify"))

        def judge(item: object) -> object:
            if isinstance(item, IslandAction):
                if item.what == "choice" and 0 <= item.index < len(choices):
                    return item.index
                if item.what == "yes" and len(choices) == 1:
                    return 0
                return None if item.what == "no" else _KEEP_WAITING
            if isinstance(item, AnswerHeard):
                index = pick(item.text, choices)
                if index is not None:
                    return index
                if yes_no(item.text) is False:
                    self._bus.publish(BrainProgress("idle", "Cancelled"))
                    return None
                return _KEEP_WAITING
            if isinstance(item, SessionCancelled):
                return None
            return _KEEP_WAITING

        index = self._wait(CLARIFY_S + 1.0, judge)
        if isinstance(index, int):
            self._bus.publish(BrainProgress("acting"))
            return index
        return None


_KEEP_WAITING = object()


@dataclass(frozen=True)
class Command:
    """What the brain was asked: from the gate, or typed into `nova say`."""

    text: str
    utterance: int | None = None
    via: str = "say"
    strict: bool = False  # always-listening: whole names only, safe actions only
    at: float = 0.0  # perf_counter when it arrived
    speech_end_at: float | None = None


def command_type(plan: Plan | None) -> str:
    """For latency stats: "open_app", "window:maximize", "tab:new", ... (first step)."""
    if plan is None or not plan.steps:
        return "none" if plan is None else plan.kind
    first = plan.steps[0]
    op = first.get("op")
    return f"{first.action}:{op}" if op and first.action in ("window", "tab", "nav", "media") else first.action


def handle(
    command: Command,
    catalog: Catalog,
    executor: Executor,
    *,
    publish: Callable[[object], None] = lambda event: None,
    trace: TraceLog | None = None,
    snap: Callable[[Catalog], Context] = snapshot,
) -> tuple[RunResult | None, dict]:
    """One command, start to finish. Returns the result and the trace record."""
    began = time.perf_counter()
    ctx: Context = snap(catalog)  # first: which apps are open helps Tier 0 with misheard names
    running = frozenset(w.app for w in ctx.windows if w.app)
    snapped = time.perf_counter()
    plan = tier0.parse(command.text, catalog, strict=command.strict, running=running)
    parsed = time.perf_counter()
    if plan is not None and command.strict and not tier0.safe_plan(plan, command.text):
        plan = None
    result: RunResult | None = None
    if plan is None:
        detail = f"Didn't get “{command.text.strip()}”" if command.text.strip() else "Didn't catch that"
        publish(CommandFinished(False, detail))
    else:
        if plan.kind == "act":
            publish(BrainProgress("acting"))
        result = executor.run(plan, ctx)
        detail = result.detail
        if not result.cancelled:
            publish(CommandFinished(result.ok, detail))
    done = time.perf_counter()
    origin = command.speech_end_at if command.speech_end_at is not None else command.at or began
    record = {
        "kind": "command",
        "utterance": command.utterance,
        "via": command.via,
        "text": command.text,
        "tier": "0" if plan is not None else "none",
        "type": command_type(result.plan if result and result.plan else plan),
        "plan": [s.to_dict() for s in (result.plan if result and result.plan else plan).steps] if plan else [],
        "ok": bool(result and result.ok),
        "cancelled": bool(result and result.cancelled),
        "asked": result.asked if result else "",
        "detail": detail,
        "parse_ms": round((parsed - snapped) * 1000, 2),
        "ctx_ms": round(ctx.ms, 1),
        "wait_ms": round((began - origin) * 1000, 1),
        "start_ms": round((result.started_at - origin) * 1000, 1) if result and result.started_at else None,
        "exec_ms": round(sum(s.ms for s in result.steps), 1) if result else None,
        "total_ms": round((done - origin) * 1000, 1),
        "steps": [{"action": s.action, "ok": s.ok, "ms": round(s.ms, 1), "method": s.method, "detail": s.detail}
                  for s in (result.steps if result else ())],
    }
    if trace is not None:
        trace.write(**record)
    return result, record


class Brain:
    def __init__(self, bus: Bus, catalogs: CatalogStore, *, trace: TraceLog | None = None) -> None:
        self._bus = bus
        self._catalogs = catalogs
        self._trace = trace
        self._commands: queue.SimpleQueue[object] = queue.SimpleQueue()
        self._answers: queue.SimpleQueue[object] = queue.SimpleQueue()
        self._ends: dict[int, float] = {}
        self._hands = WindowsHands(catalogs.current, nova=NovaControls(self._dictation, self._listening))
        self._executor = Executor(self._hands, IslandAsker(bus, self._answers), progress=self._progress)
        self._busy = False
        self._unsubscribe = [
            bus.subscribe(Gated, self._on_gated),
            bus.subscribe(SpeechEnded, self._on_speech_end),
            bus.subscribe(IslandAction, self._answers.put),
            bus.subscribe(AnswerHeard, self._answers.put),
            bus.subscribe(SessionCancelled, self._on_cancelled),
        ]
        self._thread = threading.Thread(target=self._run, name="nova-brain", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self, timeout: float = 3.0) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._executor.cancel()
        self._hands.stop.set()
        self._commands.put(_STOP)
        if self._thread.is_alive():
            self._thread.join(timeout)

    # --- bus handlers (other threads) ---------------------------------------------------------------------------

    def _on_gated(self, event: Gated) -> None:
        if not event.accepted or event.wake_only:
            return
        end = self._ends.get(event.utterance)
        self._commands.put(Command(event.text, event.utterance, event.via, event.safe_only, event.at, end))

    def _on_speech_end(self, event: SpeechEnded) -> None:
        if event.reason != "cancel":
            self._ends[event.utterance] = event.speech_end_at
            while len(self._ends) > _KEEP:
                self._ends.pop(next(iter(self._ends)))

    def _on_cancelled(self, event: SessionCancelled) -> None:
        self._answers.put(event)
        if self._busy:  # Esc / superseded while acting: stop before the next step
            self._executor.cancel()
            self._hands.stop.set()

    # --- the brain thread ---------------------------------------------------------------------------------------

    def _run(self) -> None:
        native.com_init()
        while True:
            item = self._commands.get()
            if item is _STOP:
                return
            if not isinstance(item, Command):
                continue
            self._busy = True
            self._hands.stop.clear()
            try:
                _, record = handle(item, self._catalogs.current(), self._executor, publish=self._bus.publish,
                                   trace=self._trace)
                _log.info("command %r -> %s (%s) start %s ms, total %s ms", item.text, record["detail"], record["type"],
                          record["start_ms"], record["total_ms"])
            except Exception:  # noqa: BLE001 - one bad command must not stop the brain
                _log.exception("command %r failed", item.text)
                self._bus.publish(CommandFinished(False, "Something went wrong — details in the log."))
            finally:
                self._busy = False

    def _progress(self, labels: tuple[str, ...], active: int) -> None:
        self._bus.publish(StepsChanged(labels, active))

    def _dictation(self, op: str) -> str:
        if op == "start":
            self._bus.publish(BrainProgress("dictating", "voice"))
            return "Dictation on"
        return "Dictation isn't on"

    def _listening(self, op: str) -> str:
        asleep = op != "wake"
        self._bus.publish(SleepRequested(asleep))
        return "Sleeping — say Nova or press Copilot to wake me" if asleep else "Listening again"
