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
from dataclasses import dataclass, replace

from .. import paths
from ..actions import native
from ..actions.catalog import Catalog, CatalogStore
from ..actions.chrome import Chrome, ChromeHands
from ..actions.context import Context, snapshot
from ..actions.hands import NovaControls, WindowsHands
from ..bus import Bus
from ..chrome.bridge import Bridge
from ..config import Llm, Settings
from ..dictation.cleanup import Cleaner
from ..dictation.history import History
from ..dictation.session import Dictation
from ..dictation.target import Target
from ..events import (
    WARMING,
    AnswerHeard,
    Asking,
    BrainProgress,
    CommandFinished,
    ConfigChanged,
    MarksHide,
    Gated,
    IslandAction,
    PreloadHint,
    SessionCancelled,
    SleepRequested,
    SpeechEnded,
    StepsChanged,
    TalkChanged,
    TalkEndRequested,
)
from ..governor import Governor
from ..log import TraceLog
from ..state import CLARIFY_S, CONFIRM_S
from . import meaning, task
from .answers import pick, yes_no
from .executor import Executor, RunResult
from .llama_server import connect
from .plan import Plan
from .router import Router, Settled, describe_handoff, running_apps
from .tier1 import Tier1

_log = logging.getLogger(__name__)

_KEEP = 16
_STOP = object()
_FORGET_TALK = object()  # on the command queue: a talk session opened (FIFO: it runs before that session's commands)
# keep_alive while a talk session is open: resident for the whole session. Not "-1": Ollama 0.30.10 answers a string
# "-1" with HTTP 400 "time: missing unit in duration" (only the number -1 means forever there), and llm.keep_alive is
# a string. "24h" reads the same on both runtimes (llama-server: an idle timer of a day). Closing re-arms the normal one.
TALK_KEEP_ALIVE = "24h"
_READ_OUT = "read it out"  # what the session context says for an answer read from a site (SPEC §14: never its text)


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
    talk: bool = False  # a command of a talk session (07b): Tier 1 sees the session's last steps
    request: bool = True  # worded as a request; a session clause that isn't and that Nova isn't sure of is dropped
    waited: float | None = None  # seconds it waited in the session queue behind earlier clauses


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
    prepare: Callable[[Plan], None] | None = None,
) -> tuple[RunResult | None, dict]:
    """One command, start to finish. Returns the result and the trace record. ``prepare``: called with the plan
    right before it runs (a plan that starts dictation opens it first, so what he says meanwhile is kept)."""
    began = time.perf_counter()
    ctx: Context = snap(catalog)  # first: which apps are open helps Tier 0 with misheard names
    running = running_apps(ctx)
    router = router or Router()  # Tier 0 only
    routed = router.route(command.text, ctx, catalog, strict=command.strict, via=command.via, running=running,
                          talk=command.talk, request=command.request)
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
        if prepare is not None:
            prepare(plan)
        result = executor.run(plan, ctx)
        detail = result.detail
        if not result.cancelled:
            publish(CommandFinished(result.ok, detail, hold=result.hold))
    if command.talk and plan is not None and not (result and result.cancelled):
        # The next clauses may point at this step ("there", "it"). An answer read from a site stays out (SPEC §14).
        ok = result.ok if result is not None else True
        router.remember(command.text, ok, _READ_OUT if result is not None and result.hold else detail)
    done = time.perf_counter()
    ran = result.plan if result and result.plan else plan
    thought = next((r.thought for r in settled.rounds if r.thought is not None), None)
    origin = command.speech_end_at if command.speech_end_at is not None else command.at or began
    record = {
        "kind": "command",
        "utterance": command.utterance,
        "via": command.via,
        "talk": command.talk,
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
        "page_ms": routed.page_ms,  # the page map shown to Tier 1 (None: not fetched)
        **(thought.trace() if thought is not None else {}),
        "rounds": len(settled.rounds),
        "wait_ms": round((began - origin) * 1000, 1),
        "queue_ms": round(command.waited * 1000, 1) if command.waited is not None else None,  # behind earlier clauses
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
                 trace: TraceLog | None = None, bridge: Bridge | None = None,
                 terms: Callable[[], Sequence[str]] = tuple) -> None:
        self._bus = bus
        self._catalogs = catalogs
        self._trace = trace
        self._settings = settings
        self._talk_open = False  # a talk session is open: the model stays resident until it closes (07b)
        self._commands: queue.SimpleQueue[object] = queue.SimpleQueue()
        self._answers: queue.SimpleQueue[object] = queue.SimpleQueue()
        self._ends: dict[int, float] = {}
        browser = Chrome(bridge, catalogs.current) if bridge is not None else None
        chrome = ChromeHands(browser) if browser is not None else None
        self._snap: Callable[[Catalog], Context] = lambda catalog: snapshot(catalog, bridge)
        self._hands = WindowsHands(catalogs.current, nova=NovaControls(self._dictation, self._listening), chrome=chrome,
                                   publish=bus.publish)
        self._executor = Executor(self._hands, IslandAsker(bus, self._answers), progress=self._progress)
        client = connect(settings().ollama.url, lambda: settings().llm.runtime)
        self._governor = Governor(client, lambda: settings().llm)
        self._tier1 = Tier1(client, self._governor, self._llm, catalogs.current,
                            warming=lambda: bus.publish(BrainProgress("warming", WARMING)),
                            warmed=lambda: bus.publish(BrainProgress("thinking")))
        self._hands.pointer.meaning = meaning.asker(self._tier1)  # click by meaning (Stage 07d)
        self._hands.picker = task.model_picker(self._tier1)  # a task's next step (Stage 07d)
        self._router = Router(self._tier1, page=chrome.page_rows if chrome is not None else None,
                              numbers=lambda: self._hands.pointer.showing or self._hands.pointer.grid_on,
                              scrolling=lambda: self._hands.scroller.scrolling, buttons=self._hands.button_named)
        # Dictation (Stage 07): the same model as Tier 1 cleans it up, through the same governor and loader.
        self._cleaner = Cleaner(self._governor, self._llm, terms,
                                client=client.sibling,
                                enabled=lambda: settings().dictation.cleanup, loader=self._tier1.loader)
        self.dictation = Dictation(bus, self._cleaner, Target(catalogs.current, browser),
                                   History(paths.DICTATION_HISTORY), trace=trace)
        self._prepared = False  # a plan opened dictation before running (its "start" step releases it)
        self._opened = False  # ...and the plan opened an app first
        self._busy = False
        self._unsubscribe = [
            bus.subscribe(Gated, self._on_gated),
            bus.subscribe(SpeechEnded, self._on_speech_end),
            bus.subscribe(IslandAction, self._answers.put),
            bus.subscribe(AnswerHeard, self._answers.put),
            bus.subscribe(SessionCancelled, self._on_cancelled),
            bus.subscribe(PreloadHint, lambda event: self._tier1.preload(event.reason)),
            bus.subscribe(ConfigChanged, self._on_config),
            bus.subscribe(TalkChanged, self._on_talk),
            bus.subscribe(MarksHide, lambda event: self._hands.pointer.hide(event.why)),
        ]
        self._thread = threading.Thread(target=self._run, name="nova-brain", daemon=True)

    def start(self) -> None:
        self._thread.start()
        self.dictation.start()

    def close(self, timeout: float = 3.0) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._executor.cancel()
        self._router.abort()
        self._hands.stop.set()
        self._commands.put(_STOP)
        if self._thread.is_alive():
            self._thread.join(timeout)
        self.dictation.close()
        self._tier1.close()  # unload the model now rather than after keep_alive

    # --- bus handlers (other threads) ---------------------------------------------------------------------------

    def _on_gated(self, event: Gated) -> None:
        if not event.accepted or event.wake_only:
            return
        end = self._ends.get(event.utterance)
        self._commands.put(Command(event.text, event.utterance, event.via, event.safe_only, event.at, end,
                                   talk=event.talk, request=event.request, waited=event.waited))

    def _llm(self) -> Llm:
        """The model settings Tier 1 and dictation cleanup use: while a talk session is open, the model stays."""
        llm = self._settings().llm
        return replace(llm, keep_alive=TALK_KEEP_ALIVE) if self._talk_open else llm

    def _on_talk(self, event: TalkChanged) -> None:
        """Bus thread: only flags and background work here; the session context is the brain thread's."""
        if event.what == "opened":
            self._talk_open = True
            # A new session starts with nothing to point at. Cleared at the opening, not the close: clauses still
            # running after a tap-close keep their context, and "opened" comes before the command that opens it.
            self._commands.put(_FORGET_TALK)
            self._tier1.keep_warm()  # load it now, while he says the first thing (no PreloadHint inside a session)
        elif event.what == "closed":
            self._talk_open = False
            self._tier1.keep_warm(only_if_resident=True)  # the normal keep_alive again; never loads for it

    def _on_speech_end(self, event: SpeechEnded) -> None:
        if event.reason != "cancel":
            self._ends[event.utterance] = event.speech_end_at
            while len(self._ends) > _KEEP:
                self._ends.pop(next(iter(self._ends)))

    def _on_config(self, event: ConfigChanged) -> None:
        self._governor.forget()
        self._cleaner.forget()

    def _on_cancelled(self, event: SessionCancelled) -> None:
        self._answers.put(event)
        self._hands.scroller.stop()  # Esc / "stop" ends steady scrolling too (it runs on its own thread)
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
            if item is _FORGET_TALK:
                self._router.forget_talk()
                continue
            if not isinstance(item, Command):
                continue
            self._busy = True
            self._hands.stop.clear()
            self._prepared = self._opened = False
            try:
                _, record = handle(item, self._catalogs.current(), self._executor, router=self._router,
                                   publish=self._bus.publish, trace=self._trace, snap=self._snap,
                                   prepare=lambda plan, item=item: self._prepare(plan, item))
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

    def _prepare(self, plan: Plan, command: Command) -> None:
        """A plan that starts dictation opens it before its first step (a new Google Doc takes a few seconds):
        his first words and whatever he says meanwhile wait for the dictation step."""
        start = next((s for s in plan.steps if s.action == "dictation" and s.get("op") == "start"), None)
        if start is None:
            return
        new_doc = any(s.action == "docs_new" for s in plan.steps)
        why = "Google Doc" if new_doc else "voice"
        self.dictation.begin(why, start.get("text"), utterance=command.utterance, new_doc=new_doc)
        self._prepared = True
        self._opened = any(s.action in ("open_app", "switch_app") for s in plan.steps)
        self._bus.publish(BrainProgress("dictating", why))

    def _dictation(self, op: str, text: str | None = None) -> str:
        if op == "start":
            if not self._prepared:
                self.dictation.begin("voice", text)
                self._bus.publish(BrainProgress("dictating", "voice"))
            self._prepared = False
            self.dictation.release(wait_for_field=self._opened)  # "open Word and type this": Word's page first
            return "Dictating — tap Copilot or say “stop dictation” to end"
        if op == "paste":
            return self.dictation.paste_last()
        return "Dictation isn't on"

    def _listening(self, op: str) -> str:
        if op == "end_session":  # "that's all": the controller closes the talk session; Nova stays awake
            self._bus.publish(TalkEndRequested("phrase"))
            return "Talk session ended"
        asleep = op != "wake"
        self._bus.publish(SleepRequested(asleep))
        return "Sleeping — say Nova or press Copilot to wake me" if asleep else "Listening again"
