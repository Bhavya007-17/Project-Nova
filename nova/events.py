"""Bus events shared across Nova. Frozen dataclasses only."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

    from .config import Settings


@dataclass(frozen=True)
class ConfigChanged:
    """settings.yaml loaded with new values."""

    settings: Settings


@dataclass(frozen=True)
class ConfigInvalid:
    """settings.yaml could not be used; Nova keeps the last good settings (the defaults if none loaded yet)."""

    path: str
    error: str
    using_defaults: bool = False


@dataclass(frozen=True)
class PowerChanged:
    """Tray On/Off. Off closes the mic."""

    on: bool


# --- speech (Stage 01). Times are time.perf_counter() seconds; positions are sample indexes. ---


@dataclass(frozen=True)
class SpeechReady:
    """The speech models finished loading (ok) or could not load (detail says why)."""

    ok: bool
    detail: str
    load_ms: float = 0.0


@dataclass(frozen=True)
class MicChanged:
    """The mic opened or closed; ``why`` is plain words for the console / island."""

    open: bool
    device: str
    why: str


@dataclass(frozen=True)
class SpeechStarted:
    utterance: int
    at: float  # when the first speech frame was captured


@dataclass(frozen=True, eq=False)
class SpeechEnded:
    """Every SpeechStarted is followed by exactly one SpeechEnded.

    reason: "silence" (endpoint), "key" (key release / end_now), "cap" (30 s), "cancel" (mic closed; no text follows).
    """

    utterance: int
    reason: str
    pcm: np.ndarray  # 16 kHz mono float32: exactly what goes to speech-to-text
    start: int
    end: int
    speech_end: int
    speech_end_at: float  # when speech ended (key release counts as the end)


@dataclass(frozen=True)
class PartialTranscript:
    """Live words while talking. Display only: partials never execute."""

    utterance: int
    text: str
    windowed: bool = False  # only the last few seconds were decoded (show "…" in front)


@dataclass(frozen=True)
class FinalTranscript:
    utterance: int
    text: str  # after vocab fix-up
    raw_text: str  # straight from speech-to-text
    reason: str  # how the utterance ended (see SpeechEnded)
    stt_ms: float  # decode time
    final_ms: float  # speech end -> text ready
    audio_ms: float
    early: bool = False  # decoding started in the pause, before the endpoint fired


# --- triggers and states (Stage 02). ``at`` = time.perf_counter() seconds. ---


@dataclass(frozen=True)
class StateChanged:
    """Every state-machine transition (SPEC §5); drives the island."""

    old: str
    new: str
    why: str
    trigger: str | None  # copilot | chord | wake | named
    at: float


@dataclass(frozen=True)
class PreloadHint:
    """A command may be coming: warm the LLM (Stage 05).

    reason: key | wake | speech (inside a key session) | follow-up | always (always-listening chatter: ignored)."""

    reason: str
    at: float


@dataclass(frozen=True)
class Gated:
    """The gate's verdict on a final. Accepted (and not wake_only) = a command for the brain."""

    utterance: int
    accepted: bool
    via: str  # accepted: key | wake | follow-up | always; ignored: the reason
    text: str  # what to act on (wake name stripped)
    heard: str
    why: str
    safe_only: bool  # always-listening: only safe actions may run
    wake_only: bool  # only the name: Nova is listening for the command
    at: float


@dataclass(frozen=True)
class CommandFinished:
    """The brain / executor finished the last accepted command (Stage 02: a stub).

    detail = the island's short result ("Opened HW 5 – Thermo"); skill = a learned skill ran (⚡);
    hold = an answer to read ("Nothing due today — next: …"): Done stays up this long instead of 1.5 s.
    """

    ok: bool
    detail: str = ""
    skill: bool = False
    hold: float = 0.0


@dataclass(frozen=True)
class BrainProgress:
    """The brain moved on: acting / confirm / clarify / passthrough / teaching / dictating / idle.

    The controller feeds it to the state machine, which ignores moves it doesn't allow.
    why = island text where a state has one (passthrough: "Claude voice").
    """

    state: str
    why: str = ""


# --- the island (Stage 03): what the brain shows, and what he does in it. ---


@dataclass(frozen=True)
class MicLevel:
    """Mic loudness 0..1 for the island's waveform, ~30x a second while a session listens."""

    level: float


@dataclass(frozen=True)
class StepsChanged:
    """Acting: the plan's steps for the island's step line ("Chrome → Canvas → Today"); active = running step."""

    steps: tuple[str, ...]
    active: int = 0


@dataclass(frozen=True)
class Asking:
    """Published right before Confirm / Clarify: the exact action ("Submit 'HW 5' on Canvas?"),
    or a question with up to 4 choices ("the second one" / a click / a number key picks one)."""

    kind: str  # confirm | clarify
    question: str
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class JobChip:
    """A background job's chip beside the island (Stage 09). status: working | done | failed | gone (removes it)."""

    id: str
    text: str  # "Claude · HW 5"
    status: str = "working"


@dataclass(frozen=True)
class PinToggled:
    """Ctrl + Copilot: keep the island on screen as a small status pill, or stop."""

    at: float


@dataclass(frozen=True)
class IslandAction:
    """He answered in the island without speaking (the island never takes focus; keys come from the hook).

    what: yes | no | choice (index = 0-based) | retry | show-me | chip (chip = its id). via: click | key.
    """

    what: str
    at: float
    index: int = -1
    chip: str = ""
    via: str = "click"


@dataclass(frozen=True)
class DictationChanged:
    """Locked dictation on (double-tap) or off (tap / Esc). Stage 07 types it."""

    on: bool
    why: str
    at: float


@dataclass(frozen=True)
class DictatedText:
    utterance: int
    text: str
    at: float


@dataclass(frozen=True)
class AnswerHeard:
    """What he said while Nova asked (Confirm / Clarify)."""

    utterance: int
    text: str
    at: float


@dataclass(frozen=True)
class SessionCancelled:
    why: str  # Esc | No (island button) | superseded | no answer | timed out
    at: float


@dataclass(frozen=True)
class PassthroughStopped:
    why: str
    at: float


@dataclass(frozen=True)
class AlwaysListeningChanged:
    """Tray toggle (also saved to settings.yaml when possible)."""

    on: bool


@dataclass(frozen=True)
class SleepRequested:
    """ "stop listening" / "wake up": only the name or the key count while asleep (SPEC §5)."""

    on: bool
