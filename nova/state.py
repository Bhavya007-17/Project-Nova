"""Nova's state machine (SPEC §5). Pure: ``step(machine, input) -> Step(new machine, effects)``.

The controller (``nova.triggers``) feeds it key gestures (``nova.keys``), speech events,
finals with the gate's environment, timer ticks and results from the brain; it
publishes every ``Transition`` on the bus and carries out the other effects (end or
drop the utterance, preload the LLM, hand a command over...). Level outputs - key
held, a session waiting for speech, Esc armed - are read with ``outputs()``.

States: Off · Idle · Listening · Thinking · Acting · Confirm · Clarify · Teaching ·
Dictating · Passthrough · Done · Error.

A talk session (``Talk``, Stage 07b) is not a state: it runs beside them. While one is open the mic stays attentive,
every final is split into clauses (``nova.clauses``) that run in order through the one queue, and an idle clock
closes it after ``idle_s`` without a command. Each of its moves leaves as a ``TalkEvent``.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, replace

from .clauses import split as split_clauses
from .dictation.rules import split_stop
from .gate import Addressing, Decision, Env, decide, is_stop, strip_wake
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
# The local model is cold and loads first: longer than the loader's own 180 s timeout, which is what really ends a
# load that never finishes. Once loaded (Progress(Thinking)) the answer gets HEARD_WAIT_S again.
WARM_WAIT_S = 200.0
CONFIRM_S = 15.0  # SPEC §6: 15 s without an answer = cancel
CLARIFY_S = 20.0
CLAIM_S = 1.0  # speech that started at most this long before a key press belongs to the key session
DICTATION_SILENCE_S = 10.0  # SPEC §11: dictation ends after this long without speech
ANSWER_MIN_S = 0.4  # a spoken answer is at least this long (speech-to-text invents "yeah" on tiny clips)
QUEUE_MAX_AGE_S = 10.0  # a command that waited longer than this for its turn is dropped: the screen has moved on
QUEUE_MAX = 4  # commands waiting behind the running one
NOTE_S = 3.0  # Done stays up this long when it also says a waiting command was skipped
_KEEP_SPOKEN = 8  # utterances whose start / end are remembered until their finals arrive
# Talk sessions (SPEC §5). The settings value (session.idle_s) replaces TALK_IDLE_S on the machine.
TALK_IDLE_S = 30.0  # a talk closes after this long without a command (SPEC §5)
TALK_WARN_S = 5.0  # the pill shrinks over the last 5 s before an idle close
TALK_QUEUE_MAX = 8  # three clauses in one breath is normal; two breaths behind a slow launch still fit
TALK_QUEUE_MAX_AGE_S = 30.0  # one 20 s app launch (Fusion) + two local-model plans + margin

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
class Warming:
    """The brain found the local model cold: the running command waits for it to load (not a state)."""

    at: float


@dataclass(frozen=True)
class Sleep:
    on: bool
    at: float


@dataclass(frozen=True)
class EndTalk:
    """The brain ran the closing phrase ("that's all"): close the talk session."""

    at: float
    why: str = "phrase"


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


@dataclass(frozen=True)
class TalkEvent:
    """A talk session opened, closed, or its idle clock moved (the bus's TalkChanged; see nova.events)."""

    what: str  # opened | closed | paused | resumed | closing
    why: str
    closes_at: float | None  # None: the clock is paused, or the talk closed


@dataclass(frozen=True)
class Skipped:
    """Clauses of a talk that won't run now (the bus's ClausesSkipped): the island says which."""

    texts: tuple[str, ...]
    why: str


Effect = (
    Transition | EndUtterance | DropUtterance | Preload | Command | Queued | Dropped | Ignored | Woke | Answer
    | Dictated | Dictation | Cancelled | PassthroughEnded | TalkEvent | Skipped
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
    follow_up: bool = False  # he began inside the follow-up window (or while Nova worked on his command)
    talk: bool = False  # he began it while a talk session was open: its final is split into clauses
    heard: bool = False  # its final arrived (or was given up on)


@dataclass(frozen=True)
class Waiting:
    """A command accepted while another one ran, waiting for its turn."""

    decision: Decision
    utterance: int
    since: float  # when he finished saying it
    talk: bool = False  # a clause of a talk session: the session's limits and Skipped apply


@dataclass(frozen=True)
class Talk:
    """An open talk session (SPEC §5): the mic stays open across commands until he closes it or goes quiet."""

    via: str  # copilot | chord (a tap) · wake ("Nova" alone) · named ("Nova, <command>")
    opened: float
    left: float  # idle seconds still on the clock
    since: float | None = None  # the clock runs from here; None = paused (something runs, waits, asks or dictates)
    warned: bool = False  # the closing warning went out for this run of the clock
    press: str | None = None  # a trigger key is down inside the talk (tap or hold: decided on release)
    press_at: float = 0.0

    def closes_at(self) -> float | None:
        return None if self.since is None else self.since + self.left


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
    talk: Talk | None = None  # an open talk session
    idle_s: float = TALK_IDLE_S  # a talk closes after this long without a command (the controller sets it)
    talk_command: bool = False  # the running command came from a talk or opened one: no follow-up window after it


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
    talking: bool = False  # a talk session listens: end pieces of speech on the talk endpoint


def outputs(m: Machine) -> Outputs:
    talk = m.talk
    held = (m.state is State.LISTENING and m.session is not None and m.session.held) or (
        talk is not None and talk.press is not None)
    asking = m.state.value if m.state in _ASKING else None
    dictating = m.state is State.DICTATING
    attentive = m.state in (State.LISTENING, State.DICTATING) or talk is not None
    esc = m.state in _ESC or talk is not None  # Esc closes a talk (SPEC §5)
    return Outputs(held, attentive, esc, asking, dictating, talk is not None and not dictating)


def _finishing(m: Machine) -> bool:
    """Dictating, and a tap asked to stop once the sentence he's saying is typed."""
    return m.state is State.DICTATING and m.session is not None and m.session.finishing


def next_deadline(m: Machine) -> float | None:
    times = [t for t in (m.deadline, m.follow_up_until or None) if t is not None]
    talk = m.talk
    if talk is not None and talk.since is not None:
        closes = talk.since + talk.left
        if not talk.warned:
            times.append(closes - TALK_WARN_S)
        awaited = _awaited(m)
        if m.speaking is None and not awaited:
            times.append(closes)
        times.extend(ended + HEARD_WAIT_S for ended in awaited)  # stop waiting for a final that never comes
    return min(times) if times else None


def _awaited(m: Machine) -> tuple[float, ...]:
    """When each talk utterance whose final is still on its way ended (an idle close waits for those)."""
    return tuple(s.ended for s in m.spoken if s.talk and s.ended is not None and not s.heard)


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
    said = spoken(m, utterance)  # judged by when he began to speak: the text arrives 0.3-2 s after the window shut
    return Addressing(
        session=None if session is None or named else session.trigger,
        follow_up=said.follow_up if said is not None else now < m.follow_up_until,
        passthrough=m.state is State.PASSTHROUGH,
        asleep=m.asleep,
        busy=(m.state in _BUSY or m.state is State.LISTENING) and session is None and not working,
        working=working,
        talk=said.talk if said is not None else m.talk is not None,  # judged by when he began, like follow_up
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
    return _clocked(m, _settled(handler(m, event)), event)


def _settled(result: Step) -> Step:
    """Commands wait only behind a running one: once nothing runs (stopped, failed, replaced by a key press, Off)
    whatever is still waiting is dropped, never run later against a screen that has moved on."""
    m = result.machine
    if not m.queue or (m.state in _RUNNING and m.session is None):
        return result
    dropped = tuple(Dropped(w.decision, w.utterance, "the command before it didn't finish") for w in m.queue)
    stopped = any(isinstance(e, Cancelled) and e.why == "stop" for e in result.effects)
    skipped = _skipped(m.queue, "stopped" if stopped else "the step before it didn't finish")
    return Step(replace(m, queue=()), (*result.effects, *dropped, *skipped))


def _skipped(queue: tuple[Waiting, ...], why: str) -> tuple[Skipped, ...]:
    """What the island says about talk clauses that won't run (nothing for 07a follow-ups: they drop quietly)."""
    texts = tuple(w.decision.text for w in queue if w.talk)
    return (Skipped(texts, why),) if texts else ()


def _drop(queue: tuple[Waiting, ...], why: str) -> tuple[Effect, ...]:
    return (*(Dropped(w.decision, w.utterance, why) for w in queue), *_skipped(queue, why))


def _next_up(m: Machine, at: float) -> tuple[Waiting | None, tuple[Waiting, ...], tuple[Effect, ...]]:
    """The running command is done: (who runs now, who still waits, Dropped / Skipped for who waited too long).

    Outside a talk each waiting command has its own 10 s. In a talk the clauses build on each other: when the next
    one has waited too long for its turn, the whole talk queue goes."""
    queue, gone = m.queue, ()
    if queue and queue[0].talk and at - queue[0].since > TALK_QUEUE_MAX_AGE_S:
        talk = tuple(w for w in queue if w.talk)
        why = f"waited over {TALK_QUEUE_MAX_AGE_S:.0f} s"
        gone = (*(Dropped(w.decision, w.utterance, why, stale=True) for w in talk), *_skipped(talk, why))
        queue = tuple(w for w in queue if not w.talk)
    fresh = tuple(w for w in queue if w.talk or at - w.since <= QUEUE_MAX_AGE_S)
    stale = tuple(Dropped(w.decision, w.utterance, f"waited over {QUEUE_MAX_AGE_S:.0f} s", stale=True)
                  for w in queue if not w.talk and at - w.since > QUEUE_MAX_AGE_S)
    return (fresh[0] if fresh else None), fresh[1:], (*gone, *stale)


def _start(m: Machine, nxt: Waiting, rest: tuple[Waiting, ...], stale: tuple[Effect, ...], at: float, why: str,
           **changes) -> Step:
    return _go(m, State.THINKING, why, *stale, Command(nxt.decision, nxt.utterance, waited=at - nxt.since),
               session=None, queue=rest, deadline=at + HEARD_WAIT_S, talk_command=nxt.talk, **changes)


def _power(m: Machine, e: Power) -> Step:
    if e.on and m.state is State.OFF:
        return _go(Machine(state=State.OFF, asleep=m.asleep, idle_s=m.idle_s), State.IDLE, "Nova is on")
    if not e.on and m.state is not State.OFF:
        closed = (TalkEvent("closed", "off", None),) if m.talk is not None else ()
        off = _go(m, State.OFF, "Nova is off", session=None, speaking=None, deadline=None, follow_up_until=0.0,
                  spoken=(), asked_at=None, talk=None, talk_command=False)
        return Step(off.machine, (*closed, *off.effects))
    return Step(m)


# --- talk sessions ----------------------------------------------------------------------------------------


def _talk_busy(m: Machine) -> bool:
    """Something runs, waits, asks or dictates: the talk's idle clock doesn't count down."""
    return (m.state in _RUNNING or m.state in (State.DICTATING, State.TEACHING, State.PASSTHROUGH)
            or bool(m.queue))


def _open_talk(m: Machine, at: float, via: str, *, running: bool, marks_from: float) -> tuple[Machine, TalkEvent]:
    """Open a talk. Every utterance he began from ``marks_from`` on is one of its clauses; no follow-up window."""
    talk = Talk(via, at, m.idle_s, since=None if running else at)
    spoken = tuple(replace(s, talk=True) if s.started >= marks_from else s for s in m.spoken)
    return (replace(m, talk=talk, spoken=spoken, follow_up_until=0.0),
            TalkEvent("opened", via, talk.closes_at()))


def _opening(result: Step, at: float, via: str, *, marks_from: float) -> Step:
    """``result`` plus an open talk, announced before anything else so the pill is up before the state moves."""
    m, opened = _open_talk(result.machine, at, via, running=_talk_busy(result.machine), marks_from=marks_from)
    return Step(m, (opened, *result.effects))


def _close_talk(m: Machine, at: float, why: str, *, forget_pending: bool) -> Step:
    """Close the talk. A tap keeps what he already said (the running step, waiting clauses and finals still on
    their way all go on); every other close drops waiting clauses, and ``forget_pending`` makes finals still on
    their way count as said outside a session."""
    if m.talk is None:
        return Step(m)
    effects: tuple[Effect, ...] = (TalkEvent("closed", why, None),)
    queue = m.queue
    if why != "tap":
        waiting = tuple(w for w in queue if w.talk)
        effects = (*effects, *_drop(waiting, "the session closed"))
        queue = tuple(w for w in queue if not w.talk)
    spoken = tuple(replace(s, talk=False) for s in m.spoken) if forget_pending else m.spoken
    return Step(replace(m, talk=None, queue=queue, spoken=spoken), effects)


def _then(first: Step, then) -> Step:
    """``first``, then ``then(first.machine)``: one step with both sets of effects."""
    second = then(first.machine)
    return Step(second.machine, (*first.effects, *second.effects))


def _clocked(before: Machine, result: Step, event) -> Step:
    """The talk's idle clock after any input: paused while something runs, waits, asks or dictates; restarted
    with ``idle_s`` when that ends - except a clause the brain found was no command after all, which gives back
    the seconds the clock had left (ignored speech doesn't reset it, SPEC §5)."""
    m = result.machine
    talk = m.talk
    if talk is None:
        return result
    at = event.at
    if _talk_busy(m):
        dictating = m.state is State.DICTATING
        if talk.since is not None:
            left = max(0.0, talk.since + talk.left - at)
            paused = replace(m, talk=replace(talk, left=left, since=None, warned=False))
            event_why = "dictating" if dictating else "running"
            return Step(paused, (TalkEvent("paused", event_why, None), *result.effects))
        if dictating and before.state is not State.DICTATING:
            return Step(m, (*result.effects, TalkEvent("paused", "dictating", None)))
        return result
    if talk.since is not None:
        return result
    if isinstance(event, Progress) and event.state is State.IDLE and before.state is State.THINKING:
        moved, why = replace(talk, since=at, warned=False), "ignored"
    else:
        moved, why = replace(talk, left=m.idle_s, since=at, warned=False), "done"
    return Step(replace(m, talk=moved), (*result.effects, TalkEvent("resumed", why, moved.closes_at())))


def _end_talk(m: Machine, e: EndTalk) -> Step:
    return _close_talk(m, e.at, e.why, forget_pending=True)


def _talk_press(m: Machine, e: Press) -> Step:
    """Inside a talk a press only notes the key: a tap closes the talk, a hold interrupts - known at release."""
    return Step(replace(m, talk=replace(m.talk, press=e.source, press_at=e.at)), (Preload("key"),))


def _talk_release(m: Machine, e: Release) -> Step:
    talk = m.talk
    m = replace(m, talk=replace(talk, press=None))
    if not e.hold:  # a tap closes the talk; what he is saying is finished and still counts
        finish = (EndUtterance(),) if m.speaking is not None else ()
        closed = _close_talk(m, e.at, "tap", forget_pending=False)
        return Step(closed.machine, (*finish, *closed.effects))
    # A hold: push-to-talk inside the talk. It interrupts now, and what he held the key for is one keyed command.
    claimed = [s for s in m.spoken if s.started >= talk.press_at - CLAIM_S and not s.heard]
    if not claimed:
        return Step(m)
    said = min(claimed, key=lambda s: s.utterance)
    effects: list[Effect] = []
    if m.state in _RUNNING:
        effects.append(Cancelled("superseded"))
    effects.extend(_drop(m.queue, "replaced by a new command"))
    session = Session(e.source, said.utterance)
    if said.ended is None:
        why = "Copilot key" if e.source == "copilot" else "chord"
        return _go(m, State.LISTENING, why, *effects, EndUtterance(), session=session, deadline=None, queue=())
    return _go(m, State.THINKING, "heard you", *effects, session=session, deadline=e.at + HEARD_WAIT_S, queue=())


def _talk_tick(result: Step, at: float) -> Step:
    """The idle close: a warning 5 s before; the close itself waits for a sentence under way and its final."""
    m = result.machine
    talk = m.talk
    if talk is None or talk.since is None:
        return result
    closes = talk.since + talk.left
    effects = result.effects
    if not talk.warned and at >= closes - TALK_WARN_S:
        m = replace(m, talk=replace(talk, warned=True))
        effects = (*effects, TalkEvent("closing", "idle", closes))
    if at >= closes and m.speaking is None and not _awaited(m):
        closed = _close_talk(m, at, "idle", forget_pending=True)  # a final still marked can't run without the name
        return Step(closed.machine, (*effects, *closed.effects))
    return Step(m, effects)


def _given_up(m: Machine, at: float) -> Machine:
    """Talk finals that never came (HEARD_WAIT_S after his speech ended) no longer hold the idle close, and are no
    longer talk clauses: one that turns up later needs the name or the key like any other speech (SPEC §5)."""
    late = [s for s in m.spoken if s.talk and s.ended is not None and not s.heard and at - s.ended >= HEARD_WAIT_S]
    if not late:
        return m
    return replace(m, spoken=tuple(replace(s, heard=True, talk=False) if s in late else s for s in m.spoken))


def _talk_final(m: Machine, e: Heard) -> bool:
    """Is this final a talk clause? Judged by when he began it (a final can land after a tap closed the talk)."""
    said = spoken(m, e.utterance)
    if not (said.talk if said is not None else m.talk is not None):
        return False
    if m.session is not None or m.state is State.PASSTHROUGH:
        return False  # a key session waits for (or owns) its own words: as today
    named = e.env.wake.enabled and strip_wake(e.text, e.env.wake) is not None
    return m.talk is not None or not named  # the name after a tap-close opens a new talk


def _talk_heard(m: Machine, e: Heard) -> Step:
    """A talk final: clauses in order. The first runs if nothing does, the rest wait their turn; small talk drops."""
    heard = e.text.strip()
    at, utterance = e.at, e.utterance
    rest = strip_wake(heard, e.env.wake) if e.env.wake.enabled else None
    running = m.state in _RUNNING
    effects: list[Effect] = []
    if rest is not None and not rest:  # the name alone inside a talk
        if running:
            return Step(m, (Preload("wake"),))
        talk = replace(m.talk, left=m.idle_s, since=at, warned=False)
        ignored = Decision(False, "wake-only", heard, heard, "already listening: a talk session is open", talk=True)
        return Step(replace(m, talk=talk), (Ignored(ignored, utterance), TalkEvent("resumed", "name", talk.closes_at())))
    interrupted = rest is not None and running
    if interrupted:  # the name still interrupts (SPEC §5)
        effects.extend((Cancelled("superseded"), *_drop(m.queue, "replaced by a new command")))
        m = replace(m, queue=())
        running = False
    said = spoken(m, utterance)
    since = said.ended if said is not None and said.ended is not None else at
    addressed = addressing(m, utterance, at)
    queue = m.queue
    first: Command | None = None
    for i, clause in enumerate(split_clauses(rest if rest is not None else heard)):
        if clause.kind != "command":
            small = Decision(False, "small-talk", clause.said, heard, "small talk", talk=True, request=clause.request,
                             clause=i)
            effects.append(Ignored(small, utterance))
            continue
        if rest is not None:  # named: every clause has the name's authority, whatever the speakers or mic do
            decision = Decision(True, "wake", clause.text, heard, f"starts with {e.env.wake.name}", talk=True,
                                request=clause.request, clause=i)
        else:
            decision = replace(decide(clause.text, addressed, e.env), heard=heard, talk=True, request=clause.request,
                               clause=i)
        if not decision.accepted:
            effects.append(Ignored(decision, utterance))
        elif first is None and not running:
            first = Command(decision, utterance)
            effects.append(first)
        elif sum(w.talk for w in queue) >= TALK_QUEUE_MAX:
            full = Decision(False, "busy", decision.text, heard, "too many commands already waiting", talk=True,
                            request=clause.request, clause=i)
            effects.append(Ignored(full, utterance))
        else:
            queue = (*queue, Waiting(decision, utterance, since, talk=True))
            effects.append(Queued(decision, utterance))
    if first is not None:
        return _go(m, State.THINKING, first.decision.why, *effects, session=None, queue=queue,
                   deadline=at + HEARD_WAIT_S, talk_command=True)
    if interrupted:
        return _idle(m, "cancelled", *effects)
    return Step(replace(m, queue=queue), tuple(effects))


# --- inputs ----------------------------------------------------------------------------------------------


def _press(m: Machine, e: Press) -> Step:
    if m.talk is not None and m.state not in (State.DICTATING, State.LISTENING, State.PASSTHROUGH, State.TEACHING):
        return _talk_press(m, e)
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
    if m.talk is not None and m.talk.press == e.source:
        return _talk_release(m, e)
    session = m.session
    if m.state is not State.LISTENING or session is None or session.trigger != e.source or not session.held:
        return Step(m)
    released = replace(session, held=False)
    if e.hold:  # push-to-talk: the release is the end of speech
        if _speaking_mine(m):
            return Step(replace(m, session=released), (EndUtterance(),))
        return _idle(m, "released: nothing heard")
    # A tap opens a talk session (SPEC §5): what he is already saying is its first clause.
    idle = _idle(m, "talk session")
    m, opened = _open_talk(idle.machine, e.at, session.trigger, running=False, marks_from=e.at - e.held - CLAIM_S)
    return Step(m, (opened, *idle.effects))


def _double(m: Machine, e: DoubleTap) -> Step:
    # The first tap opened (or closed) a talk; a double-tap means dictation, so the talk closes first.
    closed = _close_talk(m, e.at, "tap", forget_pending=False)
    return _then(closed, lambda m: _go(m, State.DICTATING, "double-tap: dictation", DropUtterance(),
                                       Dictation(True, "double-tap"), session=None,
                                       deadline=e.at + DICTATION_SILENCE_S))


def _cancel(m: Machine, e: Cancel) -> Step:
    if m.talk is None or e.why == "No":  # the island's No answers its question; the talk goes on
        return _cancel_now(m, e)
    # Esc / the stop button close the talk and cancel whatever runs; a sentence under way is thrown away.
    speaking = m.speaking is not None
    result = _then(_close_talk(m, e.at, e.why, forget_pending=True), lambda m: _cancel_now(m, e))
    if speaking and not any(isinstance(x, DropUtterance) for x in result.effects):
        return Step(result.machine, (*result.effects, DropUtterance()))
    return result


def _cancel_now(m: Machine, e: Cancel) -> Step:
    drop = (DropUtterance(),) if _speaking_mine(m) else ()
    if m.state is State.DICTATING:
        drop = (DropUtterance(),) if m.speaking is not None else ()
        return _idle(m, "dictation stopped", *drop, Dictation(False, e.why))
    if m.state in _CANCELLABLE:
        return _idle(m, "cancelled", *drop, Cancelled(e.why))
    return Step(m)


def _speech_start(m: Machine, e: SpeechStart) -> Step:
    others = tuple(s for s in m.spoken if s.utterance != e.utterance)
    said = Spoken(e.utterance, e.at, follow_up=e.at < m.follow_up_until or _working(m), talk=m.talk is not None)
    m = replace(m, speaking=(e.utterance, e.at), spoken=(*others, said)[-_KEEP_SPOKEN:])
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
    if m.state in (State.IDLE, State.DONE, State.ERROR) and m.session is None and m.talk is None:
        return _go(m, State.LISTENING, "heard the name", Preload("wake"), session=Session("named", e.utterance),
                   deadline=None)
    if _mine(m, e.utterance):
        return Step(m)
    return Step(m, (Preload("wake"),))


def _heard(m: Machine, e: Heard) -> Step:
    m = replace(m, spoken=tuple(replace(s, heard=True) if s.utterance == e.utterance else s for s in m.spoken))
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
    if _talk_final(m, e):
        return _talk_heard(m, e)
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
    said = spoken(m, e.utterance)
    began = said.started if said is not None else e.at
    opens = decision.via == "wake" and m.talk is None and m.state is not State.PASSTHROUGH  # SPEC §5
    if decision.wake_only and opens:  # the name alone opens a talk: what he says next is its first clause
        effects = (Preload("wake"), Woke(decision, e.utterance))
        result = _idle(m, "talk session", *effects) if mine else Step(m, effects)
        return _opening(result, e.at, "wake", marks_from=began)
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
    # The command that opens a talk is its first clause: the brain remembers it for the clauses after it.
    command = Command(replace(decision, talk=True) if opens else decision, e.utterance)
    if m.state is State.PASSTHROUGH:
        return Step(m, (command,))  # "Nova, ..." during voice mode runs; voice mode goes on
    superseded = (Cancelled("superseded"),) if m.state in _BUSY and not mine else ()
    replaced = _drop(m.queue, "replaced by a new command")
    result = _go(m, State.THINKING, decision.why, *superseded, *replaced, command, session=None, queue=(),
                 deadline=e.at + HEARD_WAIT_S, talk_command=decision.talk or opens)
    # "Nova, <command>" runs as one command (not split) and opens a talk, paused until it is done.
    return _opening(result, e.at, "named", marks_from=began) if opens else result


def _tick(m: Machine, e: Tick) -> Step:
    m = _given_up(m, e.at)
    return _talk_tick(_tick_state(m, e), e.at)


def _tick_state(m: Machine, e: Tick) -> Step:
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
    # The 8 s follow-up window is for commands outside a talk; a talk keeps listening by itself (SPEC §5).
    follow_up = m.follow_up_until if m.talk_command else e.at + FOLLOW_UP_S
    if not e.ok:  # what waited was said for a screen this command didn't produce: it is dropped (see _settled)
        return _go(m, State.ERROR, why, session=None, deadline=e.at + ERROR_S, follow_up_until=follow_up)
    nxt, rest, stale = _next_up(m, e.at)
    if nxt is not None:
        return _start(m, nxt, rest, stale, e.at, why, follow_up_until=follow_up)
    wait = max(DONE_S, min(e.hold, MAX_HOLD_S), NOTE_S if stale else 0.0)
    return _go(m, State.DONE, why, *stale, session=None, queue=(), deadline=e.at + wait, follow_up_until=follow_up)


def _waiting_for_the_model(m: Machine) -> bool:
    """A command is with the brain under a deadline (not a session waiting for its own words, not his answer going
    back to the model: those have no thinking deadline to move)."""
    return m.state is State.THINKING and m.session is None and m.deadline is not None


def _warming(m: Machine, e: Warming) -> Step:
    if not _waiting_for_the_model(m):
        return Step(m)
    return Step(replace(m, deadline=e.at + WARM_WAIT_S))


def _progress(m: Machine, e: Progress) -> Step:
    if e.state is State.THINKING and _waiting_for_the_model(m):  # loaded: the answer has the usual time from now
        return Step(replace(m, deadline=e.at + HEARD_WAIT_S))
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
    result = _go(m, e.state, e.why or e.state.value, *effects, session=None,
                 deadline=e.at + wait if wait is not None else None,
                 asked_at=e.at if e.state in _ASKING else None)
    closes = {State.PASSTHROUGH: "voice mode", State.TEACHING: "teaching"}.get(e.state)
    if closes is not None:  # voice mode and teaching take the mic over: the talk ends
        return _then(result, lambda m: _close_talk(m, e.at, closes, forget_pending=True))
    return result


def _sleep(m: Machine, e: Sleep) -> Step:
    if e.on:  # "stop listening" closes a talk too
        return _then(_close_talk(m, e.at, "asleep", forget_pending=True), lambda m: Step(replace(m, asleep=True)))
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
    Warming: _warming,
    Sleep: _sleep,
    EndTalk: _end_talk,
}
