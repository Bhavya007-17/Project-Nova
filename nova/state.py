"""Nova's state machine (SPEC §5). Pure: ``step(machine, input) -> Step(new machine, effects)``.

The controller (``nova.triggers``) feeds it key gestures (``nova.keys``), speech events,
finals with the gate's environment, timer ticks and results from the brain; it
publishes every ``Transition`` on the bus and carries out the other effects (end or
drop the utterance, preload the LLM, hand a command over...). Level outputs - key
held, a session waiting for speech, Esc armed - are read with ``outputs()``.

States: Off · Idle · Listening · Thinking · Acting · Confirm · Clarify · Teaching ·
Dictating · Passthrough · Done · Error.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, replace

from .dictation.rules import split_stop
from .gate import Addressing, Decision, Env, decide, is_stop
from .keys import Cancel, DoubleTap, Press, Release


class State(enum.Enum):
    OFF = "off"
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    ACTING = "acting"
    CONFIRM = "confirm"
    CLARIFY = "clarify"
    TEACHING = "teaching"
    DICTATING = "dictating"
    PASSTHROUGH = "passthrough"
    DONE = "done"
    ERROR = "error"


FOLLOW_UP_S = 8.0  # after a command ran, commands don't need the name (SPEC §5)
DONE_S = 1.5  # Done shows this long (SPEC §6)
MAX_HOLD_S = 10.0  # an answer to read ("Nothing due today — next: …") stays at most this long
ERROR_S = 5.0
TAP_WAIT_S = 6.0  # tapped, then said nothing
WAKE_WAIT_S = 8.0  # "Nova" alone: wait this long for the command
HEARD_WAIT_S = 10.0  # speech ended but no final (or no answer from the brain)
CONFIRM_S = 15.0  # SPEC §6: 15 s without an answer = cancel
CLARIFY_S = 20.0
CLAIM_S = 1.0  # speech that started at most this long before a key press belongs to the key session
DICTATION_SILENCE_S = 10.0  # SPEC §11: dictation ends after this long without speech
ANSWER_MIN_S = 0.4  # a spoken answer is at least this long (speech-to-text invents "yeah" on tiny clips)
QUEUE_MAX_AGE_S = 10.0  # a command that waited longer than this for its turn is dropped: the screen has moved on
QUEUE_MAX = 4  # commands waiting behind the running one
NOTE_S = 3.0  # Done stays up this long when it also says a waiting command was skipped
_KEEP_SPOKEN = 8  # utterances whose start / end are remembered until their finals arrive

_BUSY = frozenset({State.THINKING, State.ACTING})
_ASKING = frozenset({State.CONFIRM, State.CLARIFY})
_RUNNING = _BUSY | _ASKING  # a command is with the brain (when no session is waiting for its own words)
_CANCELLABLE = frozenset({State.LISTENING, State.THINKING, State.ACTING, State.CONFIRM, State.CLARIFY, State.TEACHING})
_ESC = _CANCELLABLE | {State.DICTATING}
_KEY_STARTS = frozenset({State.IDLE, State.DONE, State.ERROR}) | _BUSY | _ASKING
_PROGRESS = {  # brain-driven moves (Stage 04+)
    # -> Idle: the brain decided a follow-up wasn't a command after all (nothing to show)
    State.THINKING: {State.ACTING, State.CONFIRM, State.CLARIFY, State.PASSTHROUGH, State.TEACHING, State.DICTATING,
                     State.IDLE},
    State.ACTING: {State.THINKING, State.CONFIRM, State.CLARIFY, State.PASSTHROUGH, State.DICTATING},
    State.CONFIRM: {State.ACTING, State.THINKING, State.IDLE},
    State.CLARIFY: {State.ACTING, State.THINKING, State.IDLE},
    State.PASSTHROUGH: {State.IDLE},
    State.TEACHING: {State.IDLE, State.THINKING},
    State.DICTATING: {State.IDLE},
}
_PROGRESS_DEADLINE = {State.CONFIRM: CONFIRM_S, State.CLARIFY: CLARIFY_S, State.DICTATING: DICTATION_SILENCE_S}


# --- inputs (Press / Release / DoubleTap / Cancel come from nova.keys) --------------------------------


@dataclass(frozen=True)
class Power:
    on: bool
    at: float


@dataclass(frozen=True)
class SpeechStart:
    utterance: int
    at: float


@dataclass(frozen=True)
class SpeechEnd:
    utterance: int
    reason: str  # silence | key | cap | cancel
    at: float


@dataclass(frozen=True)
class WakeHeard:
    """The live words of this utterance start with the wake name (display + preload only)."""

    utterance: int
    at: float


@dataclass(frozen=True)
class Heard:
    """A final transcript, with what the gate needs to judge it."""

    utterance: int
    text: str
    at: float
    env: Env


@dataclass(frozen=True)
class Tick:
    at: float


@dataclass(frozen=True)
class Done:
    """The command ran (ok) or failed; detail = short island text; hold = show an answer this long (s)."""

    ok: bool
    at: float
    detail: str = ""
    hold: float = 0.0


@dataclass(frozen=True)
class Progress:
    """The brain moved on: Acting / Confirm / Clarify / Passthrough / Teaching / Dictating / back to Idle."""

    state: State
    at: float
    why: str = ""


@dataclass(frozen=True)
class Sleep:
    on: bool
    at: float


# --- effects -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Transition:
    old: State
    new: State
    why: str
    trigger: str | None = None  # copilot | chord | wake | named (the session that caused it)


@dataclass(frozen=True)
class EndUtterance:
    """Finish the utterance now (key release / tap to finish)."""


@dataclass(frozen=True)
class DropUtterance:
    """Throw the utterance away: no text follows."""


@dataclass(frozen=True)
class Preload:
    reason: str  # key | wake | speech


@dataclass(frozen=True)
class Command:
    decision: Decision
    utterance: int
    waited: float | None = None  # it was queued behind another command for this long (s)


@dataclass(frozen=True)
class Queued:
    """Accepted while Nova was busy: it runs when the command before it is done (a ``Command`` follows then)."""

    decision: Decision
    utterance: int


@dataclass(frozen=True)
class Dropped:
    """A queued command that will not run. stale: it waited too long (the island says so)."""

    decision: Decision
    utterance: int
    why: str
    stale: bool = False


@dataclass(frozen=True)
class Ignored:
    decision: Decision
    utterance: int


@dataclass(frozen=True)
class Woke:
    """He said only the name: listening for the command."""

    decision: Decision
    utterance: int


@dataclass(frozen=True)
class Answer:
    """A final while Nova asks (Confirm / Clarify): for the brain to read as the answer."""

    text: str
    utterance: int


@dataclass(frozen=True)
class Dictated:
    """A final while dictating (Stage 07 types it)."""

    text: str
    utterance: int


@dataclass(frozen=True)
class Dictation:
    on: bool
    why: str


@dataclass(frozen=True)
class Cancelled:
    why: str  # Esc | No (island button) | superseded | no answer | timed out


@dataclass(frozen=True)
class PassthroughEnded:
    why: str


Effect = (
    Transition | EndUtterance | DropUtterance | Preload | Command | Queued | Dropped | Ignored | Woke | Answer
    | Dictated | Dictation | Cancelled | PassthroughEnded
)


# --- the machine ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Session:
    """A listening session: who opened it and which utterance it claimed."""

    trigger: str  # copilot | chord (keys) · wake ("Nova" alone: the next utterance) · named (live words said Nova)
    #             · dictation (a tap while he talks: that utterance is typed, then dictation ends)
    utterance: int | None = None
    held: bool = False  # its key is down
    finishing: bool = False  # a tap asked to finish: that press's release does nothing


@dataclass(frozen=True)
class Spoken:
    """When an utterance started and ended (its final arrives later and is judged by these)."""

    utterance: int
    started: float
    ended: float | None = None  # None: still talking


@dataclass(frozen=True)
class Waiting:
    """A command accepted while another one ran, waiting for its turn."""

    decision: Decision
    utterance: int
    since: float  # when he finished saying it


@dataclass(frozen=True)
class Machine:
    state: State = State.IDLE
    session: Session | None = None
    speaking: tuple[int, float] | None = None  # VAD utterance in progress: (id, started at)
    deadline: float | None = None  # when the current state times out
    follow_up_until: float = 0.0
    asleep: bool = False
    spoken: tuple[Spoken, ...] = ()  # the last few utterances, oldest first
    asked_at: float | None = None  # Confirm / Clarify: when this question appeared
    queue: tuple[Waiting, ...] = ()  # commands behind the running one, in order (only while one runs)


@dataclass(frozen=True)
class Step:
    machine: Machine
    effects: tuple[Effect, ...] = ()


@dataclass(frozen=True)
class Outputs:
    held: bool  # push-to-talk key down: speech must bridge pauses
    attentive: bool  # a session waits for speech: run the VAD on every frame
    esc: bool  # Esc cancels: the hook swallows it
    asking: str | None = None  # confirm | clarify: the hook reads Enter / 1-4 as answers
    dictating: bool = False  # segment speech on dictation pauses (SPEC §11), not command endpoints


def outputs(m: Machine) -> Outputs:
    held = m.state is State.LISTENING and m.session is not None and m.session.held
    asking = m.state.value if m.state in _ASKING else None
    dictating = m.state is State.DICTATING
    return Outputs(held, m.state in (State.LISTENING, State.DICTATING), m.state in _ESC, asking, dictating)


def _finishing(m: Machine) -> bool:
    """Dictating, and a tap asked to stop once the sentence he's saying is typed."""
    return m.state is State.DICTATING and m.session is not None and m.session.finishing


def next_deadline(m: Machine) -> float | None:
    times = [t for t in (m.deadline, m.follow_up_until or None) if t is not None]
    return min(times) if times else None


def _go(m: Machine, state: State, why: str, *effects: Effect, **changes) -> Step:
    new = replace(m, state=state, **changes)
    if state is m.state:
        return Step(new, effects)
    trigger = (changes["session"] if "session" in changes else m.session)
    transition = Transition(m.state, state, why, trigger.trigger if trigger is not None else None)
    return Step(new, (transition, *effects))


def _idle(m: Machine, why: str, *effects: Effect) -> Step:
    return _go(m, State.IDLE, why, *effects, session=None, deadline=None)


def _mine(m: Machine, utterance: int) -> bool:
    return m.session is not None and m.session.utterance == utterance


def _speaking_mine(m: Machine) -> bool:
    return m.session is not None and m.speaking is not None and m.speaking[0] == m.session.utterance


def spoken(m: Machine, utterance: int) -> Spoken | None:
    return next((s for s in m.spoken if s.utterance == utterance), None)


def _begun_after(m: Machine, utterance: int, now: float) -> Spoken | None:
    """The first utterance he began after ``utterance`` (the name alone) whose final hasn't arrived yet."""
    name = spoken(m, utterance)
    later = [s for s in m.spoken if s.utterance > utterance and now - s.started <= WAKE_WAIT_S
             and (name is None or s.started >= name.started)]
    return min(later, key=lambda s: s.utterance) if later else None


def not_an_answer(m: Machine, utterance: int) -> tuple[str, str] | None:
    """Why this utterance can't answer the question on screen (SPEC §5, §14), as (reason, plain words); None = it can.

    An answer began after the question appeared and lasted at least ``ANSWER_MIN_S``."""
    said = spoken(m, utterance)
    if said is None or m.asked_at is None or said.started < m.asked_at:
        return "answer-early", "began before the question appeared"
    if said.ended is None or said.ended - said.started < ANSWER_MIN_S:
        return "answer-short", f"shorter than {round(ANSWER_MIN_S * 1000)} ms"
    return None


def addressing(m: Machine, utterance: int, now: float) -> Addressing:
    """What the gate should know about this utterance."""
    session = m.session if _mine(m, utterance) else None
    named = session is not None and session.trigger == "named"  # the text itself must carry the name
    working = _working(m)
    return Addressing(
        session=None if session is None or named else session.trigger,
        follow_up=now < m.follow_up_until,
        passthrough=m.state is State.PASSTHROUGH,
        asleep=m.asleep,
        busy=(m.state in _BUSY or m.state is State.LISTENING) and session is None and not working,
        working=working,
    )


def _working(m: Machine) -> bool:
    """Thinking / acting on a command (not: waiting for the final of a session's own words)."""
    return m.state in _BUSY and m.session is None


def step(m: Machine, event: object) -> Step:
    if isinstance(event, Power):
        return _settled(_power(m, event))
    if m.state is State.OFF:
        return Step(m)
    handler = _HANDLERS.get(type(event))
    if handler is None:
        raise TypeError(f"not a state-machine input: {event!r}")
    return _settled(handler(m, event))


def _settled(result: Step) -> Step:
    """Commands wait only behind a running one: once nothing runs (stopped, failed, replaced by a key press, Off)
    whatever is still waiting is dropped, never run later against a screen that has moved on."""
    m = result.machine
    if not m.queue or (m.state in _RUNNING and m.session is None):
        return result
    dropped = tuple(Dropped(w.decision, w.utterance, "the command before it didn't finish") for w in m.queue)
    return Step(replace(m, queue=()), (*result.effects, *dropped))


def _next_up(m: Machine, at: float) -> tuple[Waiting | None, tuple[Waiting, ...], tuple[Dropped, ...]]:
    """The running command is done: (who runs now, who still waits, who waited too long)."""
    fresh = tuple(w for w in m.queue if at - w.since <= QUEUE_MAX_AGE_S)
    stale = tuple(Dropped(w.decision, w.utterance, f"waited over {QUEUE_MAX_AGE_S:.0f} s", stale=True)
                  for w in m.queue if at - w.since > QUEUE_MAX_AGE_S)
    return (fresh[0] if fresh else None), fresh[1:], stale


def _start(m: Machine, nxt: Waiting, rest: tuple[Waiting, ...], stale: tuple[Dropped, ...], at: float, why: str,
           **changes) -> Step:
    return _go(m, State.THINKING, why, *stale, Command(nxt.decision, nxt.utterance, waited=at - nxt.since),
               session=None, queue=rest, deadline=at + HEARD_WAIT_S, **changes)


def _power(m: Machine, e: Power) -> Step:
    if e.on and m.state is State.OFF:
        return _go(Machine(state=State.OFF, asleep=m.asleep), State.IDLE, "Nova is on")
    if not e.on and m.state is not State.OFF:
        return _go(m, State.OFF, "Nova is off", session=None, speaking=None, deadline=None, follow_up_until=0.0,
                   spoken=(), asked_at=None)
    return Step(m)


def _press(m: Machine, e: Press) -> Step:
    state, session = m.state, m.session
    if state is State.DICTATING:
        if m.speaking is not None and not _finishing(m):  # mid-sentence: type it, then stop
            return Step(replace(m, session=Session("dictation", m.speaking[0], finishing=True),
                                deadline=e.at + HEARD_WAIT_S), (EndUtterance(),))
        return _idle(m, "dictation stopped", Dictation(False, "tap"))
    if state is State.PASSTHROUGH:
        return _idle(m, "voice mode ended", PassthroughEnded("tap"))
    if state is State.TEACHING:
        return Step(m)  # Stage 08 decides what a tap means while teaching
    if state is State.LISTENING and session is not None:
        if _speaking_mine(m):
            return Step(replace(m, session=replace(session, held=False, finishing=True)), (EndUtterance(),))
        if session.trigger in ("copilot", "chord"):
            return _idle(m, "finished: nothing heard")
        # A wake session: the key takes over.
    if state is State.LISTENING or state in _KEY_STARTS:
        effects: list[Effect] = [Preload("key")]
        if state in _BUSY or state in _ASKING:
            effects.append(Cancelled("superseded"))
        claim = None
        if m.speaking is not None:
            utterance, started = m.speaking
            if e.at - started <= CLAIM_S:
                claim = utterance  # he started talking a beat before pressing
            else:
                effects.append(DropUtterance())  # someone was already talking: start clean
        why = "Copilot key" if e.source == "copilot" else "chord"
        return _go(m, State.LISTENING, why, *effects, session=Session(e.source, claim, held=True), deadline=None)
    return Step(m)


def _release(m: Machine, e: Release) -> Step:
    session = m.session
    if m.state is not State.LISTENING or session is None or session.trigger != e.source or not session.held:
        return Step(m)
    released = replace(session, held=False)
    if e.hold:  # push-to-talk: the release is the end of speech
        if _speaking_mine(m):
            return Step(replace(m, session=released), (EndUtterance(),))
        return _idle(m, "released: nothing heard")
    # A tap: listen until he stops.
    deadline = None if session.utterance is not None else e.at + TAP_WAIT_S
    return Step(replace(m, session=released, deadline=deadline))


def _double(m: Machine, e: DoubleTap) -> Step:
    return _go(m, State.DICTATING, "double-tap: dictation", DropUtterance(), Dictation(True, "double-tap"),
               session=None, deadline=e.at + DICTATION_SILENCE_S)


def _cancel(m: Machine, e: Cancel) -> Step:
    drop = (DropUtterance(),) if _speaking_mine(m) else ()
    if m.state is State.DICTATING:
        drop = (DropUtterance(),) if m.speaking is not None else ()
        return _idle(m, "dictation stopped", *drop, Dictation(False, e.why))
    if m.state in _CANCELLABLE:
        return _idle(m, "cancelled", *drop, Cancelled(e.why))
    return Step(m)


def _speech_start(m: Machine, e: SpeechStart) -> Step:
    others = tuple(s for s in m.spoken if s.utterance != e.utterance)
    m = replace(m, speaking=(e.utterance, e.at), spoken=(*others, Spoken(e.utterance, e.at))[-_KEEP_SPOKEN:])
    if m.state is State.DICTATING and not _finishing(m):
        return Step(replace(m, deadline=None))  # talking: the silence clock stops
    session = m.session
    if m.state is State.LISTENING and session is not None and session.utterance is None:
        return Step(replace(m, session=replace(session, utterance=e.utterance), deadline=None), (Preload("speech"),))
    return Step(m)


def _speech_end(m: Machine, e: SpeechEnd) -> Step:
    if m.speaking is not None and m.speaking[0] == e.utterance:
        m = replace(m, speaking=None)
    if e.reason == "cancel":  # no final follows
        m = replace(m, spoken=tuple(s for s in m.spoken if s.utterance != e.utterance))
    else:
        m = replace(m, spoken=tuple(replace(s, ended=e.at) if s.utterance == e.utterance else s for s in m.spoken))
    if m.state is State.DICTATING:
        if not _finishing(m):
            return Step(replace(m, deadline=e.at + DICTATION_SILENCE_S))
        if e.reason == "cancel" and _mine(m, e.utterance):
            return _idle(m, "dictation stopped", Dictation(False, "tap"))
        return Step(m)
    if m.state is not State.LISTENING or not _mine(m, e.utterance):
        return Step(m)
    if e.reason == "cancel":
        return _idle(m, "speech dropped")
    return _go(m, State.THINKING, "heard you", deadline=e.at + HEARD_WAIT_S,
               session=replace(m.session, held=False))


def _wake_heard(m: Machine, e: WakeHeard) -> Step:
    if m.state in (State.IDLE, State.DONE, State.ERROR) and m.session is None:
        return _go(m, State.LISTENING, "heard the name", Preload("wake"), session=Session("named", e.utterance),
                   deadline=None)
    if _mine(m, e.utterance):
        return Step(m)
    return Step(m, (Preload("wake"),))


def _heard(m: Machine, e: Heard) -> Step:
    if m.state is State.DICTATING:
        text, stop = split_stop(e.text)
        typed = (Dictated(text, e.utterance),) if text.strip() else ()
        if stop:
            return _idle(m, "dictation stopped", *typed, Dictation(False, "stop dictation"))
        if _finishing(m) and _mine(m, e.utterance):
            return _idle(m, "dictation stopped", *typed, Dictation(False, "tap"))
        return Step(m, typed)
    if m.state in _ASKING:
        refused = not_an_answer(m, e.utterance)
        if refused is None:
            return Step(m, (Answer(e.text, e.utterance),))
        heard = e.text.strip()  # Enter, the buttons and a real answer still count; this one changes nothing
        return Step(m, (Ignored(Decision(False, refused[0], heard, heard, f"not an answer: {refused[1]}"), e.utterance),))
    mine = _mine(m, e.utterance)
    working = _working(m)
    if working and is_stop(e.text, e.env.wake):  # "stop" / "cancel": abort what runs, forget what waits
        heard = e.text.strip()
        return _idle(m, "stopped", Cancelled("stop"),
                     Ignored(Decision(False, "stop", heard, heard, "stopped the running command"), e.utterance))
    decision = decide(e.text, addressing(m, e.utterance, e.at), e.env)
    if not decision.accepted:
        if mine:
            return _idle(m, decision.why, Ignored(decision, e.utterance))
        return Step(m, (Ignored(decision, e.utterance),))
    if working and decision.via in ("follow-up", "always"):  # no name, no key: it waits its turn (SPEC §5)
        if len(m.queue) >= QUEUE_MAX:
            full = Decision(False, "busy", decision.text, decision.heard, "too many commands already waiting")
            return Step(m, (Ignored(full, e.utterance),))
        said = spoken(m, e.utterance)
        since = said.ended if said is not None and said.ended is not None else e.at
        return Step(replace(m, queue=(*m.queue, Waiting(decision, e.utterance, since))), (Queued(decision, e.utterance),))
    if decision.wake_only:
        effects = (Preload("wake"), Woke(decision, e.utterance))
        begun = _begun_after(m, e.utterance, e.at)
        if begun is None:
            return _go(m, State.LISTENING, "listening for the command", *effects, session=Session("wake"),
                       deadline=e.at + WAKE_WAIT_S)
        # He went on to the command before this final arrived: that utterance is the command.
        session = Session("wake", begun.utterance)
        if begun.ended is None:
            return _go(m, State.LISTENING, "listening for the command", *effects, session=session, deadline=None)
        return _go(m, State.THINKING, "heard you", *effects, session=session, deadline=e.at + HEARD_WAIT_S)
    command = Command(decision, e.utterance)
    if m.state is State.PASSTHROUGH:
        return Step(m, (command,))  # "Nova, ..." during voice mode runs; voice mode goes on
    superseded = (Cancelled("superseded"),) if m.state in _BUSY and not mine else ()
    replaced = tuple(Dropped(w.decision, w.utterance, "replaced by a new command") for w in m.queue)
    return _go(m, State.THINKING, decision.why, *superseded, *replaced, command, session=None, queue=(),
               deadline=e.at + HEARD_WAIT_S)


def _tick(m: Machine, e: Tick) -> Step:
    if m.follow_up_until and e.at >= m.follow_up_until:
        m = replace(m, follow_up_until=0.0)
    if m.deadline is None or e.at < m.deadline:
        return Step(m)
    if m.state is State.DICTATING:
        if m.speaking is not None and not _finishing(m):
            return Step(replace(m, deadline=None))  # he's mid-sentence: the clock starts when he stops
        why = "tap" if _finishing(m) else "silence"
        return _idle(m, "dictation stopped" if why == "tap" else "10 s of silence", Dictation(False, why))
    if m.state in _ASKING:
        return _idle(m, "no answer", Cancelled("no answer"))
    if m.state in _BUSY:
        return _idle(m, "timed out", Cancelled("timed out"))
    reasons = {State.DONE: "done", State.ERROR: "error shown", State.LISTENING: "nothing heard"}
    return _idle(m, reasons.get(m.state, "timed out"))


def _done(m: Machine, e: Done) -> Step:
    if m.state is State.DICTATING and not e.ok:  # the plan that started dictation failed (no Google Doc...)
        return _go(m, State.ERROR, e.detail or "failed", Dictation(False, "failed"), session=None,
                   deadline=e.at + ERROR_S)
    if m.state not in _BUSY and m.state not in _ASKING:
        return Step(m)
    why = e.detail or ("done" if e.ok else "failed")
    if not e.ok:  # what waited was said for a screen this command didn't produce: it is dropped (see _settled)
        return _go(m, State.ERROR, why, session=None, deadline=e.at + ERROR_S, follow_up_until=e.at + FOLLOW_UP_S)
    nxt, rest, stale = _next_up(m, e.at)
    if nxt is not None:
        return _start(m, nxt, rest, stale, e.at, why, follow_up_until=e.at + FOLLOW_UP_S)
    wait = max(DONE_S, min(e.hold, MAX_HOLD_S), NOTE_S if stale else 0.0)
    return _go(m, State.DONE, why, *stale, session=None, queue=(), deadline=e.at + wait,
               follow_up_until=e.at + FOLLOW_UP_S)


def _progress(m: Machine, e: Progress) -> Step:
    if e.state not in _PROGRESS.get(m.state, ()):
        return Step(m)
    if e.state is State.IDLE:
        if m.state is State.THINKING and m.queue:  # not a command after all: the next one's turn
            nxt, rest, stale = _next_up(m, e.at)
            if nxt is not None:
                return _start(m, nxt, rest, stale, e.at, e.why or "done")
            return _idle(replace(m, queue=()), e.why or "done", *stale)
        effects: tuple[Effect, ...] = ()
        if m.state is State.PASSTHROUGH:
            effects = (PassthroughEnded(e.why or "voice mode closed"),)
        elif m.state is State.DICTATING:
            effects = (Dictation(False, e.why or "stopped"),)
        return _idle(m, e.why or "done", *effects)
    wait = _PROGRESS_DEADLINE.get(e.state)
    effects = (Dictation(True, e.why or "plan"),) if e.state is State.DICTATING else ()
    return _go(m, e.state, e.why or e.state.value, *effects, session=None,
               deadline=e.at + wait if wait is not None else None,
               asked_at=e.at if e.state in _ASKING else None)


def _sleep(m: Machine, e: Sleep) -> Step:
    return Step(replace(m, asleep=e.on))


_HANDLERS = {
    Press: _press,
    Release: _release,
    DoubleTap: _double,
    Cancel: _cancel,
    SpeechStart: _speech_start,
    SpeechEnd: _speech_end,
    WakeHeard: _wake_heard,
    Heard: _heard,
    Tick: _tick,
    Done: _done,
    Progress: _progress,
    Sleep: _sleep,
}
