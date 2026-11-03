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
    AlwaysListeningChanged,
    Asking,
    CommandFinished,
    ConfigChanged,
    DictatedText,
    FinalTranscript,
    Gated,
    JobChip,
    MicChanged,
    PartialTranscript,
    PinToggled,
    SpeechReady,
    StateChanged,
    StepsChanged,
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
}
EXPANDED = frozenset({"confirm", "clarify", "error"})
BUTTONS = {"confirm": ("No", "Yes"), "error": ("Try again", "Show me")}
TEACHING_NOTE = "Recording — do it, narrate if you like, say “done”"
PASSTHROUGH_END = "tap Copilot to end"
MAX_CHOICES = 4


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
            or (self.pinned and self.open)
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
        result="", icon="", question="", choices=(),
    )


def _resting(view: View) -> str:
    """What shows while nothing is going on."""
    return "idle" if view.pinned and not view.off else "hidden"


def _state(view: View, event: StateChanged) -> View:
    if (event.new == "off") != view.off:
        view = replace(view, off=event.new == "off")
    new = event.new if event.new in _SHOWN else _resting(view)
    if new == view.state:
        return view
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
        view = _session(view)  # a command without a session (always-listening / follow-up): it arrives with Gated
    return replace(view, state=new)


def reduce(view: View, event: object) -> View:
    if isinstance(event, StateChanged):
        return _state(view, event)
    state = view.state
    if isinstance(event, PartialTranscript):
        if state in ("listening", "dictating") and (view.utterance is None or event.utterance >= view.utterance):
            return _words(view, event.text, event.utterance, final=False, windowed=event.windowed)
        return view
    if isinstance(event, FinalTranscript):
        mine = view.utterance is None or event.utterance == view.utterance
        newer = view.utterance is not None and event.utterance > view.utterance and state == "listening"
        if state in ("listening", "thinking") and (mine or newer) and event.text:
            return _words(view, event.text, event.utterance, final=True)
        return view
    if isinstance(event, DictatedText):
        if state == "dictating" and event.text:
            return _words(view, event.text, event.utterance, final=True)
        return view
    if isinstance(event, Gated):
        if state == "listening" and event.wake_only:
            return replace(view, words="", final=False, fresh=0, windowed=False, utterance=None)
        if state == "thinking" and event.accepted and not event.wake_only and event.text:
            return _words(view, event.text, event.utterance, final=True)
        return view
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
