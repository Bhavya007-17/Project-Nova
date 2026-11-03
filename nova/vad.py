"""Voice activity: Silero VAD, an energy pre-gate, and the SPEC §4 endpoint rules.

``EnergyGate`` and ``Endpointer`` are pure (numbers and frames in, events out) and
unit-tested; ``Silero`` wraps the ONNX model. The speech worker (``nova.speech``)
glues them to the mic and to speech-to-text.

Why a gate: capture alone costs ~1.2 % of a core here and Silero another ~0.7 %,
which would break the < 2 % idle budget. Frames near the room's noise floor
can't be speech, so Silero is skipped for them; any jump above the floor, and
the whole of an utterance, still goes through Silero.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .events import SpeechEnded, SpeechStarted

SAMPLE_RATE = 16_000
FRAME = 512  # samples per VAD frame (32 ms)


def _samples(ms: float) -> int:
    return int(round(ms * SAMPLE_RATE / 1000))


@dataclass(frozen=True)
class EndpointRules:
    """SPEC §4: start = p > 0.5 for 2 frames; end = 300 ms of silence (700 ms after 2.5 s of speech)."""

    start_threshold: float = 0.5
    start_frames: int = 2
    stop_threshold: float = 0.35  # while talking, only frames below this count as silence
    pre_roll_ms: int = 300
    short_silence_ms: int = 300
    long_silence_ms: int = 700
    long_after_ms: int = 2500  # speech at least this long uses the long silence
    tail_ms: int = 160  # audio kept after the last speech frame (a multiple of 32 ms)
    max_ms: int | None = 30_000  # cap for commands; None = no cap (dictation is segmented instead)

    def __post_init__(self) -> None:
        if self.start_frames < 1:
            raise ValueError("start_frames must be at least 1")
        if not 0 < self.stop_threshold <= self.start_threshold < 1:
            raise ValueError("thresholds must satisfy 0 < stop_threshold <= start_threshold < 1")
        if not 0 < self.short_silence_ms <= self.long_silence_ms:
            raise ValueError("short_silence_ms must be positive and no longer than long_silence_ms")
        if not 0 <= self.tail_ms <= self.short_silence_ms:
            raise ValueError("tail_ms must be between 0 and short_silence_ms")
        if self.max_ms is not None and self.max_ms <= self.long_silence_ms:
            raise ValueError("max_ms must be longer than the silence endpoint")

    @property
    def pre_roll_frames(self) -> int:
        return math.ceil(_samples(self.pre_roll_ms) / FRAME)

    @property
    def tail_samples(self) -> int:
        return _samples(self.tail_ms)


@dataclass
class Utterance:
    """The utterance in progress. Positions are absolute sample indexes."""

    id: int
    start: int  # first sample, pre-roll included
    speech_start: int  # first sample of the first speech frame
    speech_end: int  # end of the last speech frame
    end: int  # end of the last frame seen
    silence: int = 0  # samples of silence since speech_end


class _Pcm:
    """Append-only float32 buffer for one utterance. Slices are views that stay valid."""

    def __init__(self, start: int, capacity: int = 4 * SAMPLE_RATE) -> None:
        self.start = start
        self._buf = np.empty(capacity, dtype=np.float32)
        self._n = 0

    def append(self, frame: np.ndarray) -> None:
        n = len(frame)
        if self._n + n > len(self._buf):
            grown = np.empty(max(2 * len(self._buf), self._n + n), dtype=np.float32)
            grown[: self._n] = self._buf[: self._n]
            self._buf = grown  # earlier views keep the old array alive; their data never changes
        self._buf[self._n : self._n + n] = frame
        self._n += n

    def slice(self, start: int, end: int) -> np.ndarray:
        a = max(0, start - self.start)
        b = min(self._n, end - self.start)
        return self._buf[a:b]


class Endpointer:
    """Turns per-frame speech probabilities into SpeechStarted / SpeechEnded events.

    ``clock(pos)`` maps a sample position to the time it was captured.
    Single-threaded: the speech worker owns it.
    """

    def __init__(self, rules: EndpointRules | None = None, clock: Callable[[int], float] | None = None) -> None:
        self.rules = rules or EndpointRules()
        self._clock = clock or (lambda pos: pos / SAMPLE_RATE)
        r = self.rules
        self._short = _samples(r.short_silence_ms)
        self._long = _samples(r.long_silence_ms)
        self._long_after = _samples(r.long_after_ms)
        self._max = _samples(r.max_ms) if r.max_ms is not None else None
        self._history: deque[tuple[int, np.ndarray, float]] = deque(maxlen=r.pre_roll_frames + r.start_frames)
        self._streak = 0
        self._utt: Utterance | None = None
        self._pcm: _Pcm | None = None
        self._probs: list[float] = []
        self._next_id = 1
        self._draining = 0  # after a cap: samples of silence still needed before a new start (0 = not draining)
        self.held = False  # push-to-talk key down: pauses never end the utterance (release does)

    @property
    def active(self) -> Utterance | None:
        return self._utt

    @property
    def listening(self) -> bool:
        """True while talking or while a start is being confirmed (the gate must not skip then)."""
        return self._utt is not None or self._streak > 0

    @property
    def probabilities(self) -> list[float]:
        """Per-frame speech probabilities of the active utterance, from its start."""
        return self._probs

    def push(self, pos: int, frame: np.ndarray, p: float) -> SpeechStarted | SpeechEnded | None:
        r = self.rules
        end = pos + len(frame)
        utt = self._utt
        if utt is None and self._draining:
            # The cap cut a monologue; its continuation must not start a new utterance.
            self._draining = self._short if p >= r.stop_threshold else max(0, self._draining - len(frame))
            if self._draining:
                return None
        if utt is None:
            self._history.append((pos, frame, p))
            self._streak = self._streak + 1 if p > r.start_threshold else 0
            if self._streak < r.start_frames:
                return None
            return self._begin(end - r.start_frames * FRAME)

        assert self._pcm is not None
        self._pcm.append(frame)
        self._probs.append(p)
        utt.end = end
        if p >= r.stop_threshold:
            utt.speech_end = end
            utt.silence = 0
        else:
            utt.silence += len(frame)
        needed = self._short if utt.speech_end - utt.speech_start < self._long_after else self._long
        if utt.silence >= needed and not self.held:
            return self._finish("silence")
        if self._max is not None and end - utt.start >= self._max:
            return self._finish("cap")
        return None

    def final_range(self) -> tuple[int, int] | None:
        """(start, end) of the final audio if this pause turns into the endpoint; None while talking.

        Lets the worker start decoding during the pause, before the endpoint fires.
        """
        utt = self._utt
        if utt is None or self.held or utt.silence < self.rules.tail_samples:
            return None
        return utt.start, utt.speech_end + self.rules.tail_samples

    def audio(self, start: int, end: int) -> np.ndarray:
        """Samples [start, end) of the active utterance (a read-only view)."""
        if self._pcm is None:
            return np.empty(0, dtype=np.float32)
        return self._pcm.slice(start, end)

    def end_now(self) -> SpeechEnded | None:
        """Key release: end the utterance at once, keeping all audio up to now."""
        return self._finish("key") if self._utt is not None else None

    def cancel(self) -> SpeechEnded | None:
        """Mic closed (Off / lock / device change): drop the utterance; no text should follow."""
        ended = self._finish("cancel") if self._utt is not None else None
        self._history.clear()
        self._streak = 0
        return ended

    def _begin(self, speech_start: int) -> SpeechStarted:
        frames = list(self._history)
        start = frames[0][0]
        self._utt = Utterance(self._next_id, start, speech_start, speech_end=frames[-1][0] + FRAME, end=frames[-1][0] + FRAME)
        self._pcm = _Pcm(start)
        self._probs = []
        for _pos, frame, p in frames:
            self._pcm.append(frame)
            self._probs.append(p)
        self._history.clear()
        self._streak = 0
        self._next_id += 1
        return SpeechStarted(self._utt.id, self._clock(speech_start))

    def _finish(self, reason: str) -> SpeechEnded:
        utt, pcm = self._utt, self._pcm
        assert utt is not None and pcm is not None
        if reason == "silence":
            end = min(utt.end, utt.speech_end + self.rules.tail_samples)
            speech_end = utt.speech_end
        else:  # key release / cap / cancel keep everything; for a key the release is the end of speech
            end = utt.end
            speech_end = utt.end if reason == "key" else utt.speech_end
        ended = SpeechEnded(utt.id, reason, pcm.slice(utt.start, end), utt.start, end, speech_end, self._clock(speech_end))
        # Frames after the final audio can be pre-roll for the next utterance.
        self._history.clear()
        if reason != "cancel":
            for pos in range(end, utt.end, FRAME):
                self._history.append((pos, pcm.slice(pos, pos + FRAME), 0.0))
        self._utt = None
        self._pcm = None
        self._probs = []
        self._streak = 0
        self._draining = self._short if reason == "cap" else 0
        return ended


class EnergyGate:
    """Decides per frame whether Silero must run.

    Background = the 80th percentile of the last 3 s of frame energies (while nobody
    is talking). A frame more than ``margin_db`` above it runs Silero and keeps it
    running for ``hold_ms``. The threshold is clamped to [-75, -45] dBFS: anything
    louder than -45 dB always gets Silero (soft speech over a loud fan), and a
    near-silent room doesn't make every tiny sound count. Busy (talking, or a start
    being confirmed) always runs Silero; exact digital silence never does.

    Measured on this laptop's Intel mic array, whose noise suppression swings "quiet"
    between about -96 and -58 dB: a floor tracking the minimum ran Silero on 81 % of
    frames outside speech in a talkative room; this runs it on 36 %, with every speech
    start and end unchanged.
    """

    def __init__(
        self,
        margin_db: float = 15.0,
        hold_ms: float = 200.0,
        window_s: float = 3.0,
        percentile: float = 80.0,
        clamp_db: tuple[float, float] = (-75.0, -45.0),
        silence_energy: float = 1e-10,
    ) -> None:
        frame_s = FRAME / SAMPLE_RATE
        self._margin = 10 ** (margin_db / 10)
        self._low, self._high = (10 ** (db / 10) for db in clamp_db)
        self._hold_frames = math.ceil(hold_ms / 1000 / frame_s)
        self._recent: deque[float] = deque(maxlen=max(1, round(window_s / frame_s)))
        self._percentile = percentile
        self._silence = silence_energy
        self._threshold: float | None = None
        self._stale = 0
        self._hold = math.ceil(0.5 / frame_s)  # run Silero for the first 0.5 s while the background is unknown

    @property
    def threshold(self) -> float | None:
        """Mean-square energy above which a frame goes to Silero, once known."""
        return self._threshold

    def check(self, energy: float, busy: bool) -> bool:
        """``energy`` = mean of squared samples of the frame. True = run Silero on it."""
        if busy:
            return True
        if energy <= self._silence:
            return False
        self._recent.append(energy)
        self._stale += 1
        if self._threshold is None or self._stale >= 8:  # refresh 4x a second
            self._stale = 0
            background = float(np.percentile(self._recent, self._percentile))
            self._threshold = min(max(background * self._margin, self._low), self._high)
        if energy > self._threshold:
            self._hold = self._hold_frames
            return True
        if self._hold > 0:
            self._hold -= 1
            return True
        return False


class Silero:
    """Silero VAD v5 (ONNX, CPU, 1 thread). ``probability(frame)`` for 512-sample frames at 16 kHz."""

    CONTEXT = 64  # samples of the previous frame the model expects in front of each frame

    def __init__(self, model: Path) -> None:
        import onnxruntime as ort

        ort.disable_telemetry_events()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self._session = ort.InferenceSession(str(model), sess_options=options, providers=["CPUExecutionProvider"])
        self._sr = np.array(SAMPLE_RATE, dtype=np.int64)
        self._zero_state = np.zeros((2, 1, 128), dtype=np.float32)
        self.reset()

    def reset(self) -> None:
        self._state = self._zero_state
        self._context = np.zeros((1, self.CONTEXT), dtype=np.float32)

    def probability(self, frame: np.ndarray) -> float:
        if len(frame) != FRAME:
            raise ValueError(f"Silero needs {FRAME}-sample frames, got {len(frame)}")
        x = np.concatenate((self._context, frame[None, :]), axis=1)
        out, self._state = self._session.run(None, {"input": x, "state": self._state, "sr": self._sr})
        self._context = x[:, -self.CONTEXT :]
        return float(out[0, 0])

    def skip(self, frame: np.ndarray) -> None:
        """The gate skipped this frame: start fresh next time, with this frame as context."""
        self._state = self._zero_state
        self._context = frame[None, -self.CONTEXT :]
