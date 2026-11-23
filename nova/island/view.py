"""What the island shows (SPEC §6), as a pure reducer: ``reduce(view, event) -> view``.

The bridge (``nova.island.bridge``) runs it on the Qt thread for every bus event it
gets and hands the result to QML. Each kind of content has its own fields (live
words, steps, result, question, note), so a face that is fading out keeps showing
what it had while the next one fades in, and the last content stays on the pill
while it collapses.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from ..events import (
    WARMING,
    AlwaysListeningChanged,
    Asking,
    BrainProgress,
    ClausesSkipped,
    CommandFinished,
    ConfigChanged,
    DictatedText,
    DictationTyped,
    FinalTranscript,
    Gated,
    Guiding,
    JobChip,
    MicChanged,
    PartialTranscript,
    PinToggled,
    SpeechReady,
    StateChanged,
    StepsChanged,
    TalkChanged,
)

# Machine state -> island state. Idle hides it, or shows the pinned status pill ("idle"); Off hides it.
_SHOWN = {
    "listening", "thinking", "acting", "confirm", "clarify", "teaching", "dictating", "passthrough", "done", "error",
}
ACCENTS = {  # one accent per state (iOS dark-mode system colours)
    "hidden": "#0A84FF",
    "idle": "#0A84FF",  # the pinned status pill (its dot: blue = always-listening on, grey = off)
    "listening": "#0A84FF",  # blue
    "thinking": "#0A84FF",
    "acting": "#0A84FF",
    "clarify": "#0A84FF",  # listening for the answer
    "confirm": "#FFB340",  # amber
    "error": "#FF453A",  # red
    "teaching": "#FF453A",  # the recording dot
    "dictating": "#FF9F0A",  # orange
    "done": "#30D158",  # green
    "passthrough": "#BF5AF2",  # another app has the mic
    "session": "#0A84FF",  # a talk session is open: the mic stays open between commands
    "guide": "#64D2FF",  # guide mode: a ring on screen waits for his click (the overlay's ring colour)
}
EXPANDED = frozenset({"confirm", "clarify", "error"})
BUTTONS = {"confirm": ("No", "Yes"), "error": ("Try again", "Show me"), "guide": ("Stop", "Do it")}
TEACHING_NOTE = "Recording — do it, narrate if you like, say “done”"
PASSTHROUGH_END = "tap Copilot to end"
MAX_CHOICES = 4
# Talk sessions (Stage 07b). The machine has no "session" state: while a talk is open, these machine states
# show as the one session pill (so it never collapses between clauses); confirm, clarify and dictating keep
# their own faces and the pill comes back after them.
IN_SESSION = frozenset({"idle", "listening", "thinking", "acting", "done", "error"})
BENEATH = frozenset({"thinking", "acting", "done", "error"})  # what the pill's second row shows
MAX_SKIPPED = 44  # chars of "Skipped: …" (the pill is ~420 px wide and the failed step's result sits beside it)
# Guide mode (Stage 07d) runs inside the machine's acting: the step he should click replaces the step line.
GUIDED = frozenset({"acting", "confirm", "clarify"})  # machine states a guide lives through (a Nova step may ask)


@dataclass(frozen=True)
class Chip:
    id: str
    text: str
    status: str  # working | done | failed


@dataclass(frozen=True)
class View:
    state: str = "hidden"
    # live words (listening / thinking / dictating)
    words: str = ""
    final: bool = False  # settled: a final transcript, not live words
    fresh: int = 0  # words[fresh:] just arrived (they fade in)
    windowed: bool = False  # only the tail of a long utterance was decoded ("…" in front)
    utterance: int | None = None  # whose words are shown; a newer utterance takes over
    note: str = ""  # teaching / passthrough line
    steps: tuple[str, ...] = ()  # acting
    step: int = 0
    result: str = ""  # done / error
    icon: str = ""  # ok | fail | skill (⚡)
    question: str = ""  # confirm / clarify
    choices: tuple[str, ...] = ()
    chips: tuple[Chip, ...] = ()
    mic: str = ""  # why the mic is closed ("" = open)
    stt: str = "loading"  # speech-to-text: loading | ok | why it failed
    pinned: bool = False  # Ctrl + Copilot: stays on screen as a small status pill between commands
    always: bool = True  # always-listening is on (the status pill's dot)
    file_always: bool = True  # what settings.yaml says: a tray toggle lasts until the file changes
    off: bool = False  # Nova is off: hidden even when pinned
    warming: bool = False  # thinking, but the local model is cold and loading first: "Warming up…" instead of words
    under: str = ""  # while warming: his command, shown again once the model is loaded
    machine: str = "idle"  # the state machine's own state (the island's differs while a talk is open)
    talk: bool = False  # a talk session is open (SPEC §5)
    closing: bool = False  # its idle close is under 5 s away: the pill shrinks
    beneath: str = ""  # the session pill's second row: thinking | acting | done | error | "" (none)
    skipped: str = ""  # "Skipped: a, b…": what a stopped session queue won't run (until the next command)
    guide: str = ""  # guide mode: the ringed step ("2 · Click “Word wrap”")
    hint: str = ""  # guide mode: a nudge or what a wrong click did (the step stays)

    @property
    def accent(self) -> str:
        return ACCENTS[self.state]

    @property
    def open(self) -> bool:
        """The pill is out (the window may still show chips without it)."""
        return self.state != "hidden"

    @property
    def visible(self) -> bool:
        return self.open or bool(self.chips)

    @property
    def expanded(self) -> bool:
        return self.state in EXPANDED

    @property
    def buttons(self) -> tuple[str, ...]:
        return BUTTONS.get(self.state, ())

    @property
    def interactive(self) -> bool:
        """Something to click or drag: buttons, choices, chips, the pinned pill (otherwise clicks go through)."""
        return (
            bool(self.buttons) or (self.state == "clarify" and bool(self.choices)) or bool(self.chips)
            or (self.pinned and self.open) or self.state == "session"  # the session pill's stop button
        )

    @property
    def placeholder(self) -> str:
        """Shown instead of empty live words: a problem first, else what Nova is doing."""
        if self.stt == "loading":
            return "Loading speech…"
        if self.stt != "ok":
            return "Speech-to-text isn't working"
        if self.mic:
            return f"No mic — {self.mic}"
        return "Dictating…" if self.state == "dictating" else "Listening…"


def fresh_index(old: str, new: str) -> int:
    """Where the words of ``new`` stop matching ``old`` (a char index into ``new``, at the space before)."""
    old_words, new_words = old.split(), new.split()
    same = 0
    while same < len(old_words) and same < len(new_words) and old_words[same] == new_words[same]:
        same += 1
    if same == 0:
        return 0
    position = 0
    for word in new_words[:same]:
        position = new.index(word, position) + len(word)
    return position


def _words(view: View, text: str, utterance: int, final: bool, windowed: bool = False) -> View:
    same = utterance == view.utterance and not windowed and not view.windowed
    fresh = fresh_index(view.words, text) if same else 0
    if windowed:
        fresh = len(text)  # the decoded window slides: nothing lines up, so nothing fades
    return replace(view, words=text, final=final, fresh=fresh, windowed=windowed, utterance=utterance)


def _session(view: View) -> View:
    """A new listening session: forget the last one's content."""
    return replace(
        view, words="", final=False, fresh=0, windowed=False, utterance=None, note="", steps=(), step=0,
        result="", icon="", question="", choices=(), under="",
    )


def _warming(view: View) -> View:
    under = view.under if view.words == WARMING else view.words
    return replace(view, words=WARMING, under=under, final=True, fresh=0, windowed=False)


def _warmed(view: View) -> View:
    """Loaded: the model answers now — his words again (the thinking face)."""
    if view.state != "thinking":
        return replace(view, warming=False, under="")
    return replace(view, warming=False, under="", words=view.under, final=True, fresh=0, windowed=False)


def _resting(view: View) -> str:
    """What shows while nothing is going on."""
    return "idle" if view.pinned and not view.off else "hidden"


def _state(view: View, event: StateChanged) -> View:
    if (event.new == "off") != view.off:
        view = replace(view, off=event.new == "off")
    before = view.machine
    if event.new != before:
        view = replace(view, machine=event.new)
    if view.guide and event.new not in GUIDED:
        view = replace(view, guide="", hint="")  # the command ended: so did its guide
    if view.talk and event.new in IN_SESSION:
        return _in_session(view, event.new, event.why, before)
    new = event.new if event.new in _SHOWN else _resting(view)
    if new == "acting" and view.guide:
        new = "guide"
    if new == view.state:
        return view
    if new != "thinking" and view.warming:
        view = replace(view, warming=False, under="")  # loaded (or gone): the next command starts clean
    if new == "listening":
        view = _session(view)
    elif new == "dictating":
        view = replace(_session(view), final=True)
    elif new == "teaching":
        view = replace(view, note=TEACHING_NOTE)
    elif new == "passthrough":
        view = replace(view, note=f"{event.why or 'Voice mode'} · {PASSTHROUGH_END}")
    elif new == "done":
        view = replace(view, result=view.result or event.why, icon=view.icon if view.icon in ("ok", "skill") else "ok")
    elif new == "error":
        view = replace(view, result=view.result or event.why, icon="fail")
    elif new in ("confirm", "clarify"):
        view = replace(view, question=view.question or event.why)
    elif new == "thinking" and view.state in ("hidden", "idle", "done", "error"):
        view = replace(_session(view), warming=view.warming)  # a command without a session: it arrives with Gated
    if new == "thinking" and view.warming:
        view = _warming(view)
    return replace(view, state=new)


# --- talk sessions ------------------------------------------------------------------------------------------


def _in_session(view: View, new: str, why: str, before: str) -> View:
    """The machine moved while a talk is open: the same pill stays, only its second row changes.

    Row 1 (his words) is never cleared here: they keep rolling, and the final that started a clause
    arrives before the move to thinking."""
    if new != "thinking" and view.warming:
        view = replace(view, warming=False, under="")
    if new == "thinking" and before not in ("thinking", "confirm", "clarify"):
        # the next clause: its own steps and result (an answer to a question goes back to the same command)
        view = replace(view, steps=(), step=0, result="", icon="", question="", choices=())
    elif new == "done":
        view = replace(view, result=view.result or why, icon=view.icon if view.icon in ("ok", "skill") else "ok")
    elif new == "error":
        view = replace(view, result=view.result or why, icon="fail")
    if view.state == "dictating":  # back from dictation: what was typed isn't something he said to Nova
        view = replace(view, words="", final=False, fresh=0, windowed=False)
    return replace(view, state="session", beneath=new if new in BENEATH else "")


def _talk(view: View, event: TalkChanged) -> View:
    if event.what == "opened":
        return _opened(view)
    if not view.talk:
        return view
    if event.what == "closing":
        return replace(view, closing=True)
    if event.what in ("paused", "resumed"):  # something happened: the clock stopped or starts again from full
        return replace(view, closing=False)
    if event.what == "closed":
        return _closed(view)
    return view


def _opened(view: View) -> View:
    view = replace(view, talk=True, closing=False, skipped="", beneath="")
    if view.machine not in ("thinking", "acting"):  # "Nova, <command>": keep the command that opened it
        view = _session(view)  # fresh words for the session
    if view.off or view.machine not in IN_SESSION:
        return view  # confirm / clarify / dictating keep their face; the pill comes after
    return replace(view, state="session", beneath=view.machine if view.machine in BENEATH else "")


def _closed(view: View) -> View:
    view = replace(view, talk=False, closing=False, beneath="", skipped="")
    if view.state != "session":
        return view
    shown = view.machine if view.machine in _SHOWN else _resting(view)
    if shown == "acting" and view.guide:
        shown = "guide"
    view = replace(view, state=shown)  # a step still running (tap close) shows its own face
    return _warming(view) if shown == "thinking" and view.warming else view


def _skipped(view: View, event: ClausesSkipped) -> View:
    if not event.texts:
        return view
    return replace(view, skipped=cut("Skipped: " + ", ".join(event.texts), MAX_SKIPPED))


def _guiding(view: View, event: Guiding) -> View:
    if not event.on:
        if not view.guide and not view.hint:
            return view
        view = replace(view, guide="", hint="")
        return replace(view, state="acting") if view.state == "guide" else view
    if view.machine not in GUIDED:
        return view  # late: the command is over
    if event.caption:
        view = replace(view, guide=event.caption, hint=event.note)
    elif event.note:
        view = replace(view, hint=event.note)
    if view.guide and view.state == "acting":
        return replace(view, state="guide")
    return view


def cut(text: str, limit: int) -> str:
    """``text`` in at most ``limit`` chars, cut at a word with "…" (QML elides further if it must)."""
    if len(text) <= limit:
        return text
    head = text[: limit - 1]
    if " " in head:
        head = head.rsplit(" ", 1)[0]
    return head.rstrip(" ,") + "…"


def _older(view: View, utterance: int) -> bool:
    return view.utterance is not None and utterance < view.utterance


def _talk_gated(view: View, event: Gated) -> View:
    """Inside a talk only an accepted clause shows (small talk and "stop" change nothing)."""
    if not event.accepted or event.wake_only:
        return view
    if view.skipped:
        view = replace(view, skipped="")  # the next command: what was skipped is old news
    if view.state == "dictating" or not event.text or _older(view, event.utterance):
        return view
    return _words(view, event.text, event.utterance, final=True)  # warming doesn't hide it: row 2 says so


def reduce(view: View, event: object) -> View:
    if isinstance(event, StateChanged):
        return _state(view, event)
    state = view.state
    if isinstance(event, TalkChanged):
        return _talk(view, event)
    if isinstance(event, ClausesSkipped):
        return _skipped(view, event)
    if isinstance(event, Guiding):
        return _guiding(view, event)
    if isinstance(event, PartialTranscript):  # a talk keeps the mic open: his words roll in whatever Nova does
        if (state in ("listening", "dictating") or view.talk) and not _older(view, event.utterance):
            return _words(view, event.text, event.utterance, final=False, windowed=event.windowed)
        return view
    if isinstance(event, FinalTranscript):
        if view.talk:
            if state != "dictating" and event.text and not _older(view, event.utterance):
                return _words(view, event.text, event.utterance, final=True)
            return view
        mine = view.utterance is None or event.utterance == view.utterance
        newer = view.utterance is not None and event.utterance > view.utterance and state == "listening"
        if state in ("listening", "thinking") and (mine or newer) and event.text and not view.warming:
            return _words(view, event.text, event.utterance, final=True)
        return view
    if isinstance(event, DictatedText):
        if state == "dictating" and event.text:
            return _words(view, event.text, event.utterance, final=True)
        return view
    if isinstance(event, DictationTyped):  # the cleaned sentence as typed, or why it was saved instead
        shown = event.text if event.ok and event.text else event.note
        if state == "dictating" and shown:
            utterance = event.utterance if event.utterance is not None else (view.utterance or 0)
            return _words(view, shown, utterance, final=True)
        return view
    if isinstance(event, Gated):
        if view.talk:
            return _talk_gated(view, event)
        if state == "listening" and event.wake_only:
            return replace(view, words="", final=False, fresh=0, windowed=False, utterance=None)
        if state == "thinking" and event.accepted and not event.wake_only and event.text:
            if view.warming:
                return replace(view, under=event.text)  # shown once the model is loaded
            return _words(view, event.text, event.utterance, final=True)
        return view
    if isinstance(event, BrainProgress):
        if event.state == "thinking" and view.warming:
            return _warmed(view)
        if event.state != "warming":
            return view  # the state machine turns the rest into StateChanged
        view = replace(view, warming=True)
        return _warming(view) if state == "thinking" else view
    if isinstance(event, StepsChanged):
        steps = tuple(event.steps)
        return replace(view, steps=steps, step=max(0, min(event.active, len(steps) - 1)))
    if isinstance(event, CommandFinished):
        icon = "skill" if event.ok and event.skill else ("ok" if event.ok else "fail")
        return replace(view, result=event.detail, icon=icon)
    if isinstance(event, Asking):
        choices = tuple(event.choices[:MAX_CHOICES]) if event.kind == "clarify" else ()
        return replace(view, question=event.question, choices=choices)
    if isinstance(event, JobChip):
        return replace(view, chips=_chips(view.chips, event))
    if isinstance(event, PinToggled):
        view = replace(view, pinned=not view.pinned)
        return replace(view, state=_resting(view)) if view.state in ("hidden", "idle") else view
    if isinstance(event, AlwaysListeningChanged):
        return replace(view, always=event.on)
    if isinstance(event, ConfigChanged):
        on = event.settings.triggers.always_listening
        return view if on == view.file_always else replace(view, always=on, file_always=on)
    if isinstance(event, MicChanged):
        return replace(view, mic="" if event.open else event.why)
    if isinstance(event, SpeechReady):
        return replace(view, stt="ok" if event.ok else event.detail)
    return view


def _chips(chips: tuple[Chip, ...], event: JobChip) -> tuple[Chip, ...]:
    if event.status == "gone":
        return tuple(chip for chip in chips if chip.id != event.id)
    chip = Chip(event.id, event.text, event.status)
    if any(existing.id == event.id for existing in chips):
        return tuple(chip if existing.id == event.id else existing for existing in chips)
    return (*chips, chip)
