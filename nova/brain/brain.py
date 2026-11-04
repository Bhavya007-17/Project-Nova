"""The brain worker (SPEC §3): accepted commands -> router -> plan -> executor -> island + log.

One thread ("nova-brain") takes accepted commands from the gate in order. For each: the
context snapshot, the router (Tier 0 -> skills -> Tier 1, ``nova.brain.router``), the questions
the plan carries, then the executor with the island as the asker (Confirm / Clarify answered by
voice, a click, Enter / 1-4 from the hook, or Esc / No / timeout). Handoff plans show what they
would do (Stage 09 hands off). Every command writes one trace line with its hop timings
(SPEC §16); ``nova latency`` reads them. Preload hints warm the LLM while he's still talking.
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
from ..config import Settings
from ..events import (
    AnswerHeard,
    Asking,
    BrainProgress,
    CommandFinished,
    ConfigChanged,
    Gated,
    IslandAction,
    PreloadHint,
    SessionCancelled,
    SleepRequested,
    SpeechEnded,
    StepsChanged,
)
from ..governor import Governor
from ..log import TraceLog
from ..state import CLARIFY_S, CONFIRM_S
from .answers import pick, yes_no
from .executor import Executor, RunResult
from .llm import Ollama
from .plan import Plan
from .router import Router, Settled, describe_handoff, running_apps
from .tier1 import Tier1

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

    def answer(self, question: str) -> str | None:
        """An open question ("What should I open?"): whatever he says next, or None (no / Esc / timeout)."""
        self._drain()
        self._bus.publish(Asking("clarify", question))
        self._bus.publish(BrainProgress("clarify"))

        def judge(item: object) -> object:
            if isinstance(item, IslandAction):
                return None if item.what == "no" else _KEEP_WAITING
            if isinstance(item, AnswerHeard):
                text = item.text.strip()
                if not text:
                    return _KEEP_WAITING
                if yes_no(text) is False:
                    self._bus.publish(BrainProgress("idle", "Cancelled"))
                    return None
                return text
            if isinstance(item, SessionCancelled):
                return None
            return _KEEP_WAITING

        said = self._wait(CLARIFY_S + 1.0, judge)
        return said if isinstance(said, str) else None


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
    router: Router | None = None,
    asker: object | None = None,
    publish: Callable[[object], None] = lambda event: None,
    trace: TraceLog | None = None,
    snap: Callable[[Catalog], Context] = snapshot,
) -> tuple[RunResult | None, dict]:
    """One command, start to finish. Returns the result and the trace record."""
    began = time.perf_counter()
    ctx: Context = snap(catalog)  # first: which apps are open helps Tier 0 with misheard names
    running = running_apps(ctx)
    router = router or Router()  # Tier 0 only
    routed = router.route(command.text, ctx, catalog, strict=command.strict, via=command.via, running=running)
    settled: Settled = router.settle(routed, command.text, ctx, catalog, asker or executor.asker, via=command.via,
                                     running=running, progress=lambda state: publish(BrainProgress(state)))
    result: RunResult | None = None
    plan = settled.plan
    if plan is None:
        detail = settled.note
        if settled.quiet and not settled.cancelled:
            publish(BrainProgress("idle", "not a command"))
        elif not settled.cancelled:
            publish(CommandFinished(False, detail))
    elif plan.kind == "handoff":
        detail = describe_handoff(plan)  # Stage 09 hands off for real
        publish(CommandFinished(True, detail))
    else:
        publish(BrainProgress("acting"))
        result = executor.run(plan, ctx)
        detail = result.detail
        if not result.cancelled:
            publish(CommandFinished(result.ok, detail))
    done = time.perf_counter()
    ran = result.plan if result and result.plan else plan
    thought = next((r.thought for r in settled.rounds if r.thought is not None), None)
    origin = command.speech_end_at if command.speech_end_at is not None else command.at or began
    record = {
        "kind": "command",
        "utterance": command.utterance,
        "via": command.via,
        "text": command.text,
        "tier": routed.tier,
        "plan_kind": ran.kind if ran else (routed.kind or "none"),
        "type": command_type(ran),
        "plan": [s.to_dict() for s in ran.steps] if ran else [],
        "ok": bool(result and result.ok) or bool(plan is not None and plan.kind == "handoff"),
        "cancelled": bool(settled.cancelled or (result and result.cancelled)),
        "asked": settled.asked or (result.asked if result else ""),
        "detail": detail,
        "parse_ms": routed.parse_ms,
        "ctx_ms": round(ctx.ms, 1),
        **(thought.trace() if thought is not None else {}),
        "rounds": len(settled.rounds),
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
    def __init__(self, bus: Bus, catalogs: CatalogStore, *, settings: Callable[[], Settings] = Settings,
                 trace: TraceLog | None = None) -> None:
        self._bus = bus
        self._catalogs = catalogs
        self._trace = trace
        self._commands: queue.SimpleQueue[object] = queue.SimpleQueue()
        self._answers: queue.SimpleQueue[object] = queue.SimpleQueue()
        self._ends: dict[int, float] = {}
        self._hands = WindowsHands(catalogs.current, nova=NovaControls(self._dictation, self._listening))
        self._executor = Executor(self._hands, IslandAsker(bus, self._answers), progress=self._progress)
        client = Ollama(settings().ollama.url)
        self._governor = Governor(client, lambda: settings().llm)
        self._tier1 = Tier1(client, self._governor, lambda: settings().llm, catalogs.current)
        self._router = Router(self._tier1)
        self._busy = False
        self._unsubscribe = [
            bus.subscribe(Gated, self._on_gated),
            bus.subscribe(SpeechEnded, self._on_speech_end),
            bus.subscribe(IslandAction, self._answers.put),
            bus.subscribe(AnswerHeard, self._answers.put),
            bus.subscribe(SessionCancelled, self._on_cancelled),
            bus.subscribe(PreloadHint, lambda event: self._tier1.preload(event.reason)),
            bus.subscribe(ConfigChanged, lambda event: self._governor.forget()),
        ]
        self._thread = threading.Thread(target=self._run, name="nova-brain", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self, timeout: float = 3.0) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._executor.cancel()
        self._router.abort()
        self._hands.stop.set()
        self._commands.put(_STOP)
        if self._thread.is_alive():
            self._thread.join(timeout)
        self._tier1.close()  # unload the model now rather than after keep_alive

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
        if self._busy:  # Esc / superseded while thinking or acting: drop the LLM call, stop before the next step
            self._executor.cancel()
            self._router.abort()
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
                _, record = handle(item, self._catalogs.current(), self._executor, router=self._router,
                                   publish=self._bus.publish, trace=self._trace)
                _log.info("command %r -> %s (tier %s, %s) llm %s ms, start %s ms, total %s ms", item.text,
                          record["detail"], record["tier"], record["type"], record.get("llm_ms"), record["start_ms"],
                          record["total_ms"])
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
