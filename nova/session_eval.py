"""`nova eval --sessions` (Stage 07b, SPEC §5 / §18): talk sessions from docs/eval/sessions.yaml, clause by clause.

Text run: each line goes through the real clause module, state machine, gate, router (Tier 0 -> Tier 1) and
validation via ``brain.handle``; speech is the line's text with made-up times, the screen is ``evals.fake_context``
moved on by each wanted ``open``, the hands are fake (plans are scored, never run). A line is right per command when
the k-th thing it planned matches its k-th want; a ``want: []`` line is right when nothing ran and nothing was asked.

Replay (``--replay N``): session N spoken by SAPI into a WAV file, played through ``WavFeed`` -> the real
``SpeechWorker`` + ``Triggers`` (a synthetic Copilot tap opens the talk; no hook, no mic, no detectors), the real
router / Tier 1 through ``brain.handle`` with fake hands that only note when the action would start. Per clause:
end of speech -> action start, its queue wait apart, and where the endpoint cut a line or merged two.
"""

from __future__ import annotations

import difflib
import json
import math
import queue
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import paths
from .actions.catalog import Catalog, words_of
from .brain.brain import Command as BrainCommand
from .brain.brain import handle
from .brain.executor import RunResult, StepResult
from .brain.plan import Plan
from .events import BrainProgress, CommandFinished, TalkEndRequested
from .evals import Case, Want, fake_context, parse_want, percentile, score, show
from .gate import Env, NoResolver, Resolver, WakeName, strip_wake
from .keys import Press, Release
from .state import Command as StateCommand
from .state import (
    Done,
    EndTalk,
    Heard,
    Ignored,
    Machine,
    Progress,
    Skipped,
    SpeechEnd,
    SpeechStart,
    State,
    TalkEvent,
    Tick,
    WakeHeard,
    Warming,
    step,
)

SESSIONS_FILE = paths.ROOT / "docs" / "eval" / "sessions.yaml"
REPLAY_DIR = paths.ROOT / ".foreman" / "scratch" / "replay"  # WAVs and logs of `--replay` (never fixtures/ or logs/)
TARGET_RIGHT = 0.9
TARGET_START_MS = {"0": 800.0, "1": 1500.0}  # end of clause -> action start, nothing queued ahead (SPEC §16)
_WAKE = WakeName.of("Nova")
_STATES = {s.value: s for s in State}


# --- the file -------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Screen:
    front: str | None
    open_apps: tuple[str, ...] = ()
    tab: str | None = None


@dataclass(frozen=True)
class Line:
    say: str
    want: tuple[str, ...]  # as written: one want string per command, in order
    wants: tuple[tuple[Want, ...], ...]
    fail: bool = False

    @classmethod
    def of(cls, say: str, want: Sequence[str], catalog: Catalog, *, fail: bool = False) -> Line:
        try:
            wants = tuple(parse_want(w, catalog) for w in want)
        except ValueError as error:
            raise ValueError(f"{say!r}: {error}") from error
        return cls(say, tuple(want), wants, fail)


@dataclass(frozen=True)
class Session:
    name: str
    note: str
    screen: Screen
    pause_ms: int
    opener: str  # tap | name
    lines: tuple[Line, ...]


def _screen(raw: Mapping[str, Any] | None, default: Screen) -> Screen:
    if not raw:
        return default
    return Screen(raw.get("front", default.front), tuple(raw.get("open", default.open_apps)), raw.get("tab"))


def load_sessions(catalog: Catalog, path: Path = SESSIONS_FILE) -> list[Session]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    default = _screen(data.get("screen"), Screen(None))
    sessions = []
    for raw in data.get("sessions") or []:
        opener = raw.get("open", "tap")
        if opener not in ("tap", "name"):
            raise ValueError(f"{raw.get('name')!r}: open is tap or name, not {opener!r}")
        lines = tuple(Line.of(str(row["say"]), row.get("want") or (), catalog, fail=bool(row.get("fail")))
                      for row in raw.get("lines") or [])
        sessions.append(Session(raw["name"], raw.get("note", ""), _screen(raw.get("screen"), default),
                                int(raw.get("pause_ms", 900)), opener, lines))
    return sessions


def advance(screen: Screen, wants: Sequence[Want]) -> Screen:
    """The screen after a wanted command ran: an opened app (or site, in Chrome) comes to the front, a closed one
    goes. Only the first alternative counts; it moves the screen whether or not Nova got the command right, so one
    miss doesn't make every later line wrong."""
    if not wants or wants[0].kind != "act":
        return screen
    for s in wants[0].steps:
        if s[0] == "open":
            site = "." in str(s[1])
            app = "Google Chrome" if site else s[1]
            apps = screen.open_apps if app in screen.open_apps else (*screen.open_apps, app)
            screen = Screen(app, apps, s[1] if site else screen.tab)
        elif s[0] == "close":
            apps = tuple(a for a in screen.open_apps if a != s[1])
            front = screen.front if screen.front != s[1] else (apps[0] if apps else None)
            screen = Screen(front, apps, screen.tab)
    return screen


def _followed(screen: Screen, plan: Plan | None) -> Screen:
    """The replay's screen after a plan "ran" (the fake hands open nothing, so the screen follows the plan)."""
    if plan is None or plan.kind != "act":
        return screen
    for s in plan.steps:
        if s.action in ("open_app", "switch_app") and s.get("app"):
            app = str(s.get("app"))
            screen = Screen(app, screen.open_apps if app in screen.open_apps else (*screen.open_apps, app), screen.tab)
        elif s.action == "open_url":
            apps = screen.open_apps if "Google Chrome" in screen.open_apps else (*screen.open_apps, "Google Chrome")
            screen = Screen("Google Chrome", apps, str(s.get("url")))
    return screen


def context(screen: Screen) -> Any:
    return fake_context(Case("", "positive", front=screen.front, open_apps=screen.open_apps, tab=screen.tab))


# --- the brain with fake hands ------------------------------------------------------------------------------------


class _Watched:
    """The session's one router, noting what it routed and settled (``handle`` only returns the trace record)."""

    def __init__(self, router: Any) -> None:
        self._router = router
        self.routed: Any = None
        self.settled: Any = None

    def route(self, *args: Any, **kwargs: Any) -> Any:
        self.routed = self._router.route(*args, **kwargs)
        return self.routed

    def settle(self, *args: Any, **kwargs: Any) -> Any:
        self.settled = self._router.settle(*args, **kwargs)
        return self.settled

    def __getattr__(self, name: str) -> Any:
        return getattr(self._router, name)


class _Asker:
    """Nobody answers: every question is declined, as the island's No would (Nova moves back to idle)."""

    def __init__(self, publish: Callable[[object], None]) -> None:
        self._publish = publish
        self.asked = ""

    def _declined(self, question: str, state: str) -> None:
        self.asked = question
        self._publish(BrainProgress(state))
        self._publish(BrainProgress("idle", "Cancelled"))

    def confirm(self, question: str) -> bool:
        self._declined(question, "confirm")
        return False

    def choose(self, question: str, choices: Sequence[str]) -> int | None:
        self._declined(question, "clarify")
        return None

    def answer(self, question: str) -> str | None:
        self._declined(question, "clarify")
        return None


class _Hands:
    """An executor that touches nothing: notes when the first action would begin, says it went fine (or failed)."""

    def __init__(self, asker: _Asker, publish: Callable[[object], None], *, fail: bool, clock: Callable[[], float]):
        self.asker = asker
        self._publish = publish
        self._fail = fail
        self._clock = clock

    def run(self, plan: Plan, ctx: Any) -> RunResult:
        began = self._clock()
        if any(s.action == "listening" and s.get("op") == "end_session" for s in plan.steps):
            self._publish(TalkEndRequested("phrase"))  # what NovaControls does for the closing phrase
        detail = _describe(plan)
        ok = not self._fail
        steps = tuple(StepResult(s.action, ok, "", 0.0, "eval") for s in plan.steps)
        return RunResult(ok, detail if ok else f"Couldn't: {detail}", steps, started_at=began, plan=plan)


def _describe(plan: Plan) -> str:
    from .brain.tier0 import describe

    try:
        return describe(plan)
    except Exception:  # noqa: BLE001 - only a label for the session context
        return plan.steps[0].action if plan.steps else plan.kind


@dataclass(frozen=True)
class Ran:
    """One command through ``brain.handle``: what was published, the plan to score, and the trace record."""

    events: tuple[object, ...]
    plan: Plan | None  # what would run, or the question that was asked
    tier: str
    kind: str
    note: str
    quiet: bool
    record: Mapping[str, Any]


class HandleRunner:
    """``brain.handle`` with fake hands and one router for the whole eval (its session context is cleared when a
    session opens, as the brain does on TalkChanged(opened))."""

    def __init__(self, router: Any, catalog: Catalog, *, clock: Callable[[], float] = time.perf_counter) -> None:
        self._router = router
        self._catalog = catalog
        self._clock = clock

    def opened(self) -> None:
        self._router.forget_talk()

    def run(self, command: BrainCommand, ctx: Any, *, fail: bool = False,
            publish: Callable[[object], None] = lambda event: None) -> Ran:
        events: list[object] = []

        def out(event: object) -> None:
            events.append(event)
            publish(event)

        watched = _Watched(self._router)
        asker = _Asker(out)
        hands = _Hands(asker, out, fail=fail, clock=self._clock)
        _, record = handle(command, self._catalog, hands, router=watched, asker=asker, publish=out,
                           snap=lambda catalog: ctx)
        routed, settled = watched.routed, watched.settled
        plan = settled.plan if settled is not None else None
        if plan is None and asker.asked:
            plan = next((r.plan for r in reversed(settled.rounds) if r.plan is not None and r.plan.kind == "clarify"),
                        None)
        kind = plan.kind if plan is not None else (routed.kind or "none")
        quiet = bool(settled is not None and settled.quiet)
        return Ran(tuple(events), plan, routed.tier, "quiet" if quiet else kind, settled.note if settled else "",
                   quiet, record)


def handle_runner(router: Any, catalog: Catalog) -> HandleRunner:
    return HandleRunner(router, catalog)


def _machine_input(event: object, at: float) -> object | None:
    """What the controller (``nova.triggers``) feeds the machine for an event the brain published."""
    if isinstance(event, BrainProgress):
        if event.state == "warming":
            return Warming(at)
        state = _STATES.get(event.state)
        return Progress(state, at, event.why) if state is not None else None
    if isinstance(event, CommandFinished):
        return Done(event.ok, at, event.detail, event.hold)
    if isinstance(event, TalkEndRequested):
        return EndTalk(at, event.why)
    return None


# --- the text run -------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Got:
    text: str  # the clause the brain was given
    via: str
    tier: str
    kind: str  # act | handoff | clarify | quiet | none
    shown: str
    plan: Plan | None
    front: str | None  # the app in front when it was routed
    waited: float | None
    prompt_tokens: int | None = None


@dataclass(frozen=True)
class LineResult:
    session: str
    index: int
    say: str
    want: tuple[str, ...]
    got: tuple[Got, ...]
    notes: tuple[str, ...]  # clauses dropped as small talk, refused by the gate, or skipped
    right: int
    units: int
    quiet_actions: int  # actions run on a `want: []` line


def score_line(line: Line, got: Sequence[Got], catalog: Catalog) -> tuple[int, int, int]:
    """(right, units, actions on a quiet line). One unit per wanted command or per extra planned one; a quiet line
    is one unit, right when nothing was planned (acting or asking)."""
    planned = [g for g in got if g.plan is not None]
    units = max(len(line.wants), len(planned), 1)
    if not line.wants:
        acted = sum(g.plan.kind in ("act", "handoff") for g in planned)
        return (0 if planned else 1), units, acted
    right = sum(score(g.plan, wants, catalog, kind=g.kind, front=g.front)
                for g, wants in zip(planned, line.wants, strict=False))
    return right, units, 0


def run_session(session: Session, runner: HandleRunner, catalog: Catalog, *, wake: WakeName = _WAKE,
                resolver: Resolver | None = None) -> list[LineResult]:
    """One session through the machine, line by line, on a made-up clock. Commands run one after the other as
    the machine hands them out (each waits for the one before it, as on the brain thread)."""
    env = Env(wake, False, resolver or NoResolver())
    machine = Machine()
    now = 0.0
    screen = session.screen
    pending: list[StateCommand] = []
    notes: list[str] = []

    def feed(event: object) -> None:
        nonlocal machine
        result = step(machine, event)
        machine = result.machine
        for effect in result.effects:
            if isinstance(effect, TalkEvent) and effect.what == "opened":
                runner.opened()
            elif isinstance(effect, StateCommand):
                pending.append(effect)
            elif isinstance(effect, Ignored) and not effect.decision.wake_only:
                notes.append(f"{effect.decision.why}: {effect.decision.text}")
            elif isinstance(effect, Skipped):
                notes.append(f"skipped ({effect.why}): {', '.join(effect.texts)}")

    if session.opener == "tap":
        feed(Press("copilot", now))
        feed(Release("copilot", now + 0.15, 0.15, False))
        now += 0.15
    results = []
    for index, line in enumerate(session.lines):
        notes.clear()
        now += session.pause_ms / 1000
        feed(Tick(now))
        utterance = index + 1
        feed(SpeechStart(utterance, now))
        if strip_wake(line.say, wake) is not None:
            feed(WakeHeard(utterance, now))
        now += 0.25 + 0.3 * len(line.say.split())
        ended = now
        feed(SpeechEnd(utterance, "silence", now))
        now += 0.3
        feed(Heard(utterance, line.say, now, env))
        got: list[Got] = []
        planned = 0
        while pending:
            effect = pending.pop(0)
            d = effect.decision
            command = BrainCommand(d.text, effect.utterance, d.via, d.safe_only, now, ended, talk=d.talk,
                                   request=d.request, waited=effect.waited)
            ran = runner.run(command, context(screen), fail=line.fail)
            got.append(Got(d.text, d.via, ran.tier, ran.kind, show(ran.plan, ran.kind, ran.note), ran.plan,
                           screen.front, effect.waited, ran.record.get("prompt_tokens")))
            for event in ran.events:
                now += 0.1
                fed = _machine_input(event, now)
                if fed is not None:
                    feed(fed)
            if ran.plan is not None:
                if planned < len(line.wants):
                    screen = advance(screen, line.wants[planned])
                planned += 1
        for wants in line.wants[planned:]:
            screen = advance(screen, wants)
        right, units, quiet = score_line(line, got, catalog)
        results.append(LineResult(session.name, index, line.say, line.want, tuple(got), tuple(notes), right, units,
                                  quiet))
    return results


@dataclass(frozen=True)
class Summary:
    right: int
    total: int
    quiet_actions: int  # actions on `want: []` lines
    quiet_lines: int
    tiers: dict[str, int] = field(default_factory=dict)  # clauses per tier

    @property
    def misses(self) -> int:
        return self.total - self.right

    @property
    def rate(self) -> float:
        return self.right / self.total if self.total else math.nan


def summarize(results: Sequence[LineResult]) -> Summary:
    tiers: dict[str, int] = {}
    for result in results:
        for g in result.got:
            tiers[g.tier] = tiers.get(g.tier, 0) + 1
    return Summary(sum(r.right for r in results), sum(r.units for r in results),
                   sum(r.quiet_actions for r in results), sum(not r.want for r in results), tiers)


def report(summary: Summary, results: Sequence[LineResult], *, title: str) -> str:
    s = summary
    lines = [title,
             f"clauses right: {s.right}/{s.total} = {s.rate:.1%}   (target ≥ {TARGET_RIGHT:.0%})",
             f"actions on small-talk lines (want: []): {s.quiet_actions} over {s.quiet_lines} lines   (target 0)",
             "clauses per tier: " + ", ".join(f"tier {t}: {n}" for t, n in sorted(s.tiers.items()))]
    wrong = [r for r in results if r.right < r.units]
    if wrong:
        lines.append("misses:")
    for r in wrong:
        lines.append(f"  ✗ [{r.session} #{r.index + 1}] {r.say!r}   want: {list(r.want) or 'nothing'}")
        for g in r.got:
            lines.append(f"      [{g.tier}] {g.text!r} ({g.via}) -> {g.shown}")
        for note in r.notes:
            lines.append(f"      · {note}")
    return "\n".join(lines)


# --- timings and the endpoint (replay) ------------------------------------------------------------------------


@dataclass(frozen=True)
class TimingRow:
    text: str
    tier: str
    start_ms: float | None  # end of his speech -> the action began (queue wait included)
    queue_ms: float | None  # waited behind earlier clauses (None: nothing queued ahead)
    after_turn_ms: float | None  # start_ms without the queue wait
    prompt_tokens: int | None = None


@dataclass(frozen=True)
class Timings:
    rows: tuple[TimingRow, ...]
    start_p50: dict[str, float]  # per tier, clauses with nothing queued ahead
    queue_p50: float
    queue_p95: float
    prompt_tokens: tuple[int, ...]


def timings(records: Sequence[Mapping[str, Any]]) -> Timings:
    rows = []
    for r in records:
        start, waited = r.get("start_ms"), r.get("queue_ms")
        after = None if start is None else round(start - (waited or 0.0), 1)
        rows.append(TimingRow(str(r.get("text", "")), str(r.get("tier", "")), start, waited, after,
                              r.get("prompt_tokens")))
    starts: dict[str, list[float]] = {}
    for row in rows:
        if row.start_ms is not None and row.queue_ms is None:
            starts.setdefault(row.tier, []).append(row.start_ms)
    waits = [row.queue_ms for row in rows if row.queue_ms is not None]
    return Timings(tuple(rows), {t: percentile(v, 50) for t, v in starts.items()}, percentile(waits, 50),
                   percentile(waits, 95), tuple(row.prompt_tokens for row in rows if row.prompt_tokens is not None))


@dataclass(frozen=True)
class Segmentation:
    merged: tuple[tuple[int, tuple[int, ...]], ...] = ()  # (final, the script lines it holds) when it holds several
    cut: tuple[tuple[int, tuple[int, ...]], ...] = ()  # (line, the finals it came out as) when it came out as several


def align(lines: Sequence[str], finals: Sequence[str]) -> Segmentation:
    """Which script lines each final holds, by matching words (speech-to-text may mishear a few)."""
    script = [(i, w) for i, text in enumerate(lines) for w in words_of(text)]
    heard = [(j, w) for j, text in enumerate(finals) for w in words_of(text)]
    matcher = difflib.SequenceMatcher(a=[w for _, w in script], b=[w for _, w in heard], autojunk=False)
    holds: dict[int, set[int]] = {}
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            holds.setdefault(heard[block.b + k][0], set()).add(script[block.a + k][0])
    became: dict[int, set[int]] = {}
    for final, held in holds.items():
        for line in held:
            became.setdefault(line, set()).add(final)
    merged = tuple((f, tuple(sorted(h))) for f, h in sorted(holds.items()) if len(h) > 1)
    cut = tuple((ln, tuple(sorted(f))) for ln, f in sorted(became.items()) if len(f) > 1)
    return Segmentation(merged, cut)


# --- running it -------------------------------------------------------------------------------------------------


def _parts(model: str | None, device: str | None):
    from .governor import Choice
    from .say import _catalog_store, brain_parts

    store = _catalog_store()
    router, tier1 = brain_parts(store)
    choice = None
    if model or device:
        llm = tier1.llm
        choice = Choice(model or (llm.cpu_model if device == "cpu" else llm.gpu_model), device or "gpu",
                        "nova eval --model/--device")
        tier1.pin(choice)
    picked = choice or tier1.governor.choose(fresh=True)
    print(f"Warming {picked.model} on the {picked.device.upper()}…", flush=True)
    warm_ms = tier1.warm(picked)
    print(f"  ready in {warm_ms:.0f} ms.", flush=True)
    return store, router, picked


def _env_parts(catalog: Catalog) -> tuple[WakeName, Resolver]:
    from .brain.tier0 import Tier0Resolver
    from .config import ConfigStore

    config = ConfigStore(paths.SETTINGS)
    config.load()
    s = config.settings
    return WakeName.of(s.wake_name, s.wake_spellings, enabled=s.triggers.wake_name), Tier0Resolver(catalog)


def main(*, replay: int = 0, model: str | None = None, device: str | None = None, misses: bool = True) -> int:
    try:
        store, router, picked = _parts(model, device)
    except Exception as error:  # noqa: BLE001
        print(f"Can't use the local model: {error} — NOT MEASURED")
        return 1
    catalog = store.current()
    sessions = load_sessions(catalog)
    if replay:
        if not 1 <= replay <= len(sessions):
            print(f"--replay takes 1..{len(sessions)}")
            return 2
        return run_replay(sessions[replay - 1], replay, router, catalog)
    wake, resolver = _env_parts(catalog)
    runner = handle_runner(router, catalog)
    results: list[LineResult] = []
    for session in sessions:
        print(f"  session {session.name!r} ({len(session.lines)} lines)…", flush=True)
        results.extend(run_session(session, runner, catalog, wake=wake, resolver=resolver))
    summary = summarize(results)
    text = report(summary, results if misses else [], title=f"Session eval — {picked.model} on the "
                                                            f"{picked.device.upper()} (clauses -> machine -> gate -> "
                                                            f"Tier 0 -> Tier 1)")
    print(text)
    tokens = [g.prompt_tokens for r in results for g in r.got if g.prompt_tokens]
    if tokens:
        print(f"Tier 1 prompt_tokens: p50 {percentile(tokens, 50):.0f}, max {max(tokens)} ({len(tokens)} calls)")
    _save(paths.LOGS / "eval-sessions.jsonl", [_line_json(r) for r in results])
    met = summary.rate >= TARGET_RIGHT and summary.quiet_actions == 0
    print(f"target ≥ 90 % right and 0 actions on small talk: {'met' if met else 'NOT met'}")
    sys.stdout.flush()
    return 0 if met else 1


def _line_json(r: LineResult) -> dict[str, Any]:
    return {"session": r.session, "line": r.index + 1, "say": r.say, "want": list(r.want), "right": r.right,
            "units": r.units, "quiet_actions": r.quiet_actions, "notes": list(r.notes),
            "got": [{"text": g.text, "via": g.via, "tier": g.tier, "kind": g.kind, "shown": g.shown,
                     "waited": g.waited, "prompt_tokens": g.prompt_tokens} for g in r.got]}


def _save(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as out:
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def run_replay(session: Session, number: int, router: Any, catalog: Catalog, *, out_dir: Path = REPLAY_DIR) -> int:
    """Session ``number`` spoken by SAPI and played through the live speech path; per-clause timings."""
    from . import tts
    from .audio import WavFeed
    from .brain.tier0 import Tier0Resolver
    from .bus import Bus
    from .config import ConfigStore
    from .events import FinalTranscript, Gated, MicChanged, SpeechEnded, TalkChanged
    from .keys import TriggerEvent
    from .log import TraceLog
    from .speech import standard_worker
    from .triggers import Triggers
    from .vocab import VocabFile

    out_dir.mkdir(parents=True, exist_ok=True)
    wav = out_dir / f"session-{number}.wav"
    said = [line.say for line in session.lines]
    try:
        audio_s = tts.speak(said, wav, pause_ms=session.pause_ms)
    except Exception as error:  # noqa: BLE001
        print(f"SAPI couldn't write the WAV: {error} — NOT MEASURED")
        return 1
    print(f"SAPI: {wav} ({audio_s:.1f} s, {len(said)} lines, {session.pause_ms} ms after each)", flush=True)
    config = ConfigStore(paths.SETTINGS)
    config.load()
    settings = config.settings
    vocab = VocabFile(paths.VOCAB)
    vocab.current()
    trace = TraceLog(out_dir, transcripts=True)
    bus = Bus()
    feeds: list[WavFeed] = []

    def wav_source(ring, wake):
        feed = WavFeed(ring, wake, [wav], lead_s=1.5, gap_s=1.0)
        feeds.append(feed)
        return feed

    worker = standard_worker(bus, vocab=vocab, trace=trace, device="wav", source=wav_source)
    screen = [session.screen]
    triggers = Triggers(bus, worker, settings, keys=None, detector=None, trace=trace,
                        running=lambda: frozenset(screen[0].open_apps))
    triggers.set_resolver(Tier0Resolver(catalog))
    runner = handle_runner(router, catalog)
    work: queue.SimpleQueue[object] = queue.SimpleQueue()
    ends: dict[int, float] = {}
    finals: list[FinalTranscript] = []
    gated: list[Gated] = []
    rows: list[dict[str, Any]] = []
    busy = threading.Event()
    opened = object()
    tapped = threading.Event()

    def on_mic(event: MicChanged) -> None:
        if event.open and not tapped.is_set():  # the synthetic tap: the talk is open before the first word
            tapped.set()
            now = time.perf_counter()
            triggers.key_event(TriggerEvent("copilot", True, now))
            triggers.key_event(TriggerEvent("copilot", False, now + 0.15))

    def on_gated(event: Gated) -> None:
        gated.append(event)
        if event.accepted and not event.wake_only:
            busy.set()
            work.put(event)

    def on_talk(event: TalkChanged) -> None:
        if event.what == "opened":
            work.put(opened)

    bus.subscribe(MicChanged, on_mic)
    bus.subscribe(SpeechEnded, lambda e: ends.__setitem__(e.utterance, e.speech_end_at))
    bus.subscribe(FinalTranscript, lambda e: finals.append(e) if e.text else None)
    bus.subscribe(Gated, on_gated)
    bus.subscribe(TalkChanged, on_talk)

    def brain() -> None:
        while True:
            item = work.get()
            if item is None:
                return
            if item is opened:
                runner.opened()
                continue
            g = item
            command = BrainCommand(g.text, g.utterance, g.via, g.safe_only, g.at, ends.get(g.utterance),
                                   talk=g.talk, request=g.request, waited=g.waited)
            ran = runner.run(command, context(screen[0]), publish=bus.publish)
            screen[0] = _followed(screen[0], ran.plan)
            rows.append({**ran.record, "heard": g.heard, "clause": g.clause, "shown": show(ran.plan, ran.kind, ran.note),
                         "kind": ran.kind})
            if work.empty():
                busy.clear()

    thread = threading.Thread(target=brain, name="nova-eval-brain", daemon=True)
    thread.start()
    triggers.start()
    worker.start()
    try:
        while not worker.ready.wait(0.1):
            pass
        if worker.failed:
            print("The speech models didn't load — NOT MEASURED")
            return 1
        deadline = time.monotonic() + audio_s + 90
        quiet_since = None
        while time.monotonic() < deadline:
            time.sleep(0.25)
            settled = feeds and feeds[0].done.is_set() and worker.idle and not busy.is_set()
            quiet_since = (quiet_since or time.monotonic()) if settled else None
            if quiet_since is not None and time.monotonic() - quiet_since > 3.0:
                break
    finally:
        triggers.close()
        worker.close()
        work.put(None)
        thread.join(30)
        trace.close()
    _save(out_dir / f"replay-{number}.jsonl", rows)
    print(replay_report(session, finals, gated, rows))
    return 0


def replay_report(session: Session, finals: Sequence[Any], gated: Sequence[Any], rows: Sequence[Mapping]) -> str:
    t = timings(rows)
    out = [f"Replay of session {session.name!r} — SAPI voice → WavFeed → SpeechWorker + Triggers → brain.handle "
           f"(fake hands)", "finals (speech → text):"]
    out += [f"  u{f.utterance}: {f.text!r}  ({f.final_ms:.0f} ms after the speech ended)" for f in finals]
    out.append("clauses:")
    out.append(f"  {'heard (clause)':<44} {'tier':>4} {'start':>7} {'queue':>7} {'after turn':>10} {'tokens':>6}  plan")
    for row, rec in zip(t.rows, rows, strict=True):
        def ms(v: float | None) -> str:
            return f"{v:.0f}" if v is not None else "-"

        out.append(f"  {row.text[:44]:<44} {row.tier:>4} {ms(row.start_ms):>7} {ms(row.queue_ms):>7} "
                   f"{ms(row.after_turn_ms):>10} {row.prompt_tokens or '-':>6}  {rec.get('shown', '')}")
    dropped = [g for g in gated if not g.accepted]
    if dropped:
        out.append("dropped: " + "; ".join(f"{g.via}: {g.text!r}" for g in dropped))
    for tier, target in TARGET_START_MS.items():
        got = t.start_p50.get(tier)
        if got is None:
            out.append(f"tier {tier}: no clause with nothing queued ahead")
        else:
            n = sum(1 for r in t.rows if r.tier == tier and r.queue_ms is None and r.start_ms is not None)
            out.append(f"tier {tier}: end of clause → action start p50 {got:.0f} ms over {n} (target ≤ {target:.0f}): "
                       f"{'met' if got <= target else 'NOT met'}")
    if not math.isnan(t.queue_p50):
        out.append(f"queue wait (clauses behind another): p50 {t.queue_p50:.0f} ms, p95 {t.queue_p95:.0f} ms "
                   "(fake hands take ~0 ms, so this is routing time only)")
    if t.prompt_tokens:
        out.append(f"Tier 1 prompt_tokens: {list(t.prompt_tokens)}")
    seg = align([line.say for line in session.lines], [f.text for f in finals])
    quiet = {i for i, line in enumerate(session.lines) if not line.want}
    for final, held in seg.merged:
        small = " (merged with small talk)" if quiet & set(held) and set(held) - quiet else ""
        out.append(f"endpoint merged lines {[h + 1 for h in held]} into one final: {finals[final].text!r}{small}")
    for line, became in seg.cut:
        out.append(f"endpoint cut line {line + 1} {session.lines[line].say!r} into "
                   f"{[finals[f].text for f in became]}")
    if not seg.merged and not seg.cut:
        out.append("endpoint: every line came out as exactly one final (no cut, no merge)")
    return "\n".join(out)
