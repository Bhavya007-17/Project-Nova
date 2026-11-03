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
