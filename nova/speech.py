"""The speech worker (SPEC §3): mic -> energy gate -> Silero -> endpoints -> Parakeet -> vocab -> bus.

One thread owns the audio path and all utterance state, and never waits on
speech-to-text. Two lanes share the one loaded model:
- partial lane: live words every ~400 ms while talking; a partial is skipped
  while the previous one is still running;
- final lane: finals only, so a final never waits behind a partial.
At 160 ms into a pause the audio of the would-be final is already known, so the
final lane starts decoding it right then; if the pause turns into the endpoint
(300 ms) that decode *is* the final and most of it overlapped the wait. If he
keeps talking, the result just refreshes the live words.

Bus handlers run on this thread: keep them quick.
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

import numpy as np

from . import paths
from .audio import BLOCK, SAMPLE_RATE, Mic, MicError, Ring, level_of
from .bus import Bus
from .events import (
    FinalTranscript,
    MicChanged,
    MicLevel,
    PartialTranscript,
    PauseCandidate,
    SpeechEnded,
    SpeechReady,
    SpeechStarted,
)
from .log import TraceLog
from .stt.base import Transcriber
from .vad import DICTATION_RULES, FRAME, EndpointRules, Endpointer, EnergyGate, Silero, unaddressed
from .vocab import VocabFile

_log = logging.getLogger(__name__)

PARTIAL_EVERY = int(0.4 * SAMPLE_RATE)
PARTIAL_WINDOW = 5 * SAMPLE_RATE  # long utterances: partials decode only the last 5 s (display only)
PARTIAL_SNAP_FRAMES = 20  # ...cut at the quietest frame in the window's first 640 ms
RETRY_S = (1.0, 2.0, 5.0, 10.0)  # reopen back-off after a failed open
SILENT_MIC_S = 3.0  # only digital zeros this long after opening: say so
IDLE_BATCH = 6  # nobody talking: wake for every 6th block (~192 ms); while talking: every block
CANDIDATE_SAMPLES = int(0.3 * SAMPLE_RATE)  # dictating: a pause this long may end the segment (PauseCandidate)

assert BLOCK == FRAME  # one mic block = one VAD frame


class Source(Protocol):
    """Where audio comes from: the Mic, or a WavFeed in tests and benchmarks."""

    name: str
    setting: str
    latency: float

    @property
    def is_open(self) -> bool: ...
    def open(self) -> None: ...
    def close(self) -> None: ...
    def check(self, now: float) -> str | None: ...


@dataclass(frozen=True, eq=False)
class _Job:
    kind: str  # "partial" | "final"
    utterance: int
    start: int
    end: int
    pcm: np.ndarray
    windowed: bool = False

    @property
    def key(self) -> tuple[int, int, int]:
        return self.utterance, self.start, self.end


@dataclass(frozen=True, eq=False)
class _Result:
    job: _Job
    text: str
    stt_ms: float
    error: str | None = None


@dataclass(eq=False)
class _Final:
    """A final-lane decode, started early in a pause or at the endpoint."""

    key: tuple[int, int, int]
    ended: SpeechEnded | None = None  # set when the utterance really ended with this audio
    endpoint_at: float = 0.0
    early: bool = False  # decoding started before the endpoint
    result: _Result | None = None
    announced: bool = False  # published as a PauseCandidate


@dataclass(eq=False)
class _Talk:
    """Per-utterance bookkeeping for traces."""

    started: SpeechStarted
    partials: int = 0
    shown_end: int = 0
    shown_text: str = ""
    next_partial: int = 0
    extra: dict = field(default_factory=dict)


class _Lane:
    """One speech-to-text thread with a small queue of waiting jobs."""

    def __init__(self, name: str, stt: Transcriber, deliver: Callable[[_Result], None]) -> None:
        self._stt = stt
        self._deliver = deliver
        self._cv = threading.Condition()
        self._queue: collections.deque[_Job] = collections.deque()
        self._running = False
        self._closed = False
        self._thread = threading.Thread(target=self._loop, name=f"nova-stt-{name}", daemon=True)
        self._thread.start()

    def busy(self) -> bool:
        with self._cv:
            return self._running or bool(self._queue)

    def offer(self, job: _Job) -> bool:
        """Take the job only if idle (partials: skip rather than queue)."""
        with self._cv:
            if self._running or self._queue:
                return False
            self._queue.append(job)
            self._cv.notify()
            return True

    def put(self, job: _Job, keep: Callable[[_Job], bool]) -> None:
        """Queue the job; waiting jobs that ``keep`` rejects (stale early decodes) are dropped."""
        with self._cv:
            self._queue = collections.deque([waiting for waiting in self._queue if keep(waiting)])
            self._queue.append(job)
            self._cv.notify()

    def close(self, timeout: float = 5.0) -> None:
        with self._cv:
            self._closed = True
            self._queue.clear()
            self._cv.notify()
        self._thread.join(timeout)

    def _loop(self) -> None:
        while True:
            with self._cv:
                while not self._queue and not self._closed:
                    self._cv.wait()
                if self._closed:
                    return
                job = self._queue.popleft()
                self._running = True
            began = time.perf_counter()
            error = None
            try:
                text = self._stt.transcribe(job.pcm, retry_empty=job.kind == "final")
            except Exception as exc:  # noqa: BLE001 - one bad decode must not kill the lane
                _log.exception("speech-to-text failed")
                text, error = "", f"{type(exc).__name__}: {exc}"
            stt_ms = (time.perf_counter() - began) * 1000
            with self._cv:
                self._running = False
            self._deliver(_Result(job, text, stt_ms, error))


def _wall(perf: float) -> str:
    """perf_counter seconds -> local ISO time."""
    stamp = time.time() - (time.perf_counter() - perf)
    return datetime.fromtimestamp(stamp).astimezone().isoformat(timespec="milliseconds")


class SpeechWorker:
    def __init__(
        self,
        bus: Bus,
        *,
        load_stt: Callable[[], Transcriber],
        load_vad: Callable[[], Silero],
        vocab: VocabFile | None = None,
        trace: TraceLog | None = None,
        device: str = "default",
        rules: EndpointRules | None = None,
        source: Callable[[Ring, threading.Event], Source] | None = None,
    ) -> None:
        self._bus = bus
        self._load_stt = load_stt
        self._load_vad = load_vad
        self._vocab = vocab
        self._trace = trace
        self._rules = rules or EndpointRules()
        self._idle_rules = unaddressed(self._rules)  # no key held, no session listening: capped at 10 s
        self._wake = threading.Event()
        self._ring = Ring(wake=self._wake)
        self._source: Source = (source or (lambda ring, wake: Mic(ring, wake, device)))(self._ring, self._wake)
        self._ep = Endpointer(self._rules, clock=self._time_of)
        self._gate = EnergyGate()
        self._vad: Silero | None = None
        self._stt: Transcriber | None = None
        self._partial_lane: _Lane | None = None
        self._final_lane: _Lane | None = None
        self._results: collections.deque[_Result] = collections.deque()
        self._finals: dict[tuple[int, int, int], _Final] = {}
        self._talks: dict[int, _Talk] = {}
        self._pos = 0
        # Requests from other threads: plain attribute writes, then a wake-up.
        self._enabled = True
        self._locked = False
        self._device = device
        self._device_changed = False
        self._end_requested = False
        self._cancel_requested = False
        self._held = False
        self._attentive = False
        self._dictating = False  # requested (any thread)
        self._dictation_rules = False  # in effect (worker thread)
        self._idle = True
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._run, name="nova-speech", daemon=True)
        # Mic state (worker thread only).
        self._retry_at = 0.0
        self._failures = 0
        self._last_error = ""
        self._opened_at = 0.0
        self._heard_sound = False
        self._warned_silent = False
        self._why_next = "listening"
        # Counters for `nova listen` and the bench.
        self.frames = 0
        self.vad_frames = 0
        self.ready = threading.Event()
        self.failed: str | None = None

    # --- control (any thread) ---------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def close(self, timeout: float = 5.0) -> None:
        self._stopping.set()
        self._wake.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    def set_enabled(self, on: bool) -> None:
        self._enabled = bool(on)
        self._wake.set()

    def set_locked(self, locked: bool) -> None:
        self._locked = bool(locked)
        self._wake.set()

    def set_device(self, device: str) -> None:
        if device != self._device:
            self._device = device
            self._device_changed = True
            self._wake.set()

    def end_now(self) -> None:
        """Key release: finish the current utterance now (no-op if nobody is talking)."""
        self._end_requested = True
        self._wake.set()

    def cancel(self) -> None:
        """Esc / double-tap: drop the current utterance; no text follows (SpeechEnded reason "cancel")."""
        self._cancel_requested = True
        self._wake.set()

    def set_held(self, held: bool) -> None:
        """Push-to-talk key down: pauses don't end the utterance; ``end_now`` on release does."""
        self._held = bool(held)
        self._wake.set()

    def set_attentive(self, on: bool) -> None:
        """A session is waiting for speech: run Silero on every frame and wake per block."""
        self._attentive = bool(on)
        self._wake.set()

    def set_dictating(self, on: bool) -> None:
        """Dictation: segments end on 600 ms pauses with no cap (SPEC §11), and a pause that may end one is
        announced (PauseCandidate) so cleanup can start early."""
        self._dictating = bool(on)
        self._wake.set()

    @property
    def idle(self) -> bool:
        """Nobody talking and no final pending (updated by the worker every loop)."""
        return self._idle

    @property
    def source(self) -> Source:
        return self._source

    # --- the worker thread ---------------------------------------------------------------

    def _run(self) -> None:
        try:
            began = time.perf_counter()
            self._vad = self._load_vad()
            self._stt = self._load_stt()
            load_ms = (time.perf_counter() - began) * 1000
        except Exception as error:  # noqa: BLE001 - reported on the bus
            _log.exception("speech models failed to load")
            self.failed = f"speech models failed to load: {error}"
            self._bus.publish(SpeechReady(False, self.failed))
            self.ready.set()
            return
        self._partial_lane = _Lane("partial", self._stt, self._deliver)
        self._final_lane = _Lane("final", self._stt, self._deliver)
        self._bus.publish(SpeechReady(True, self._stt.name, load_ms))
        self.ready.set()
        try:
            while not self._stopping.is_set():
                self._wake.wait(0.25)
                self._wake.clear()
                try:
                    self._step()
                except Exception:  # noqa: BLE001 - a bug must not leave Nova deaf: drop the utterance, keep going
                    _log.exception("speech worker error; dropping the current utterance")
                    self._recover()
        finally:
            self._shutdown()

    def _step(self) -> None:
        self._sync_source(time.perf_counter())
        self._dictation_rules = self._dictating
        addressed = self._held or self._attentive  # a key or a session: the full 30 s, also from mid-utterance on
        wanted = DICTATION_RULES if self._dictating else (self._rules if addressed else self._idle_rules)
        if wanted is not self._ep.rules:
            self._ep.set_rules(wanted)
        if self._held:
            if not self._ep.held:
                self._ep.listen_now()  # a new press: no quiet to wait for after the last release
            self._ep.held = True  # a press takes effect before this audio is read...
        if self._source.is_open:
            self._read_audio()
        if self._cancel_requested:
            self._cancel_requested = False
            self._end_requested = False
            self._cancel_utterance()
        if self._end_requested:
            self._end_requested = False
            ended = self._ep.end_now()
            if ended is not None:
                self._on_vad_event(ended)
        self._ep.held = self._held  # ...a release after it, so the release (not a pause) ends the utterance
        self._take_results()
        self._idle = (
            self._ep.active is None
            and not self._results
            and all(final.ended is None for final in self._finals.values())
        )

    def _recover(self) -> None:
        try:
            self._cancel_utterance()
        except Exception:  # noqa: BLE001
            _log.exception("could not cancel the utterance")
            self._ep = Endpointer(self._rules, clock=self._time_of)
        self._finals.clear()
        self._talks.clear()
        self._pos = self._ring.end
        self._stopping.wait(0.2)  # never spin on a repeating error

    def _shutdown(self) -> None:
        if self._source.is_open:
            self._close_source("Nova stopped listening")
        for lane in (self._partial_lane, self._final_lane):
            if lane is not None:
                lane.close()

    # mic open / close -------------------------------------------------------------------

    def _sync_source(self, now: float) -> None:
        source = self._source
        if self._device_changed:
            self._device_changed = False
            if isinstance(source, Mic):
                source.setting = self._device
                if source.is_open:
                    self._close_source("switching mic")
                    self._why_next = "mic setting changed"
                    self._retry_at = 0.0
        want_open = self._enabled and not self._locked
        if source.is_open and not want_open:
            self._close_source("Nova is off" if not self._enabled else "screen locked")
            self._why_next = "Nova is on" if not self._enabled else "screen unlocked"
            self._retry_at = 0.0
            return
        if not source.is_open:
            if want_open and now >= self._retry_at:
                self._open_source()
            return
        reason = source.check(now)
        if reason is not None:
            _log.warning("reopening the mic: %s", reason)
            self._close_source(reason)
            self._why_next = reason
            self._open_source()
            return
        if not self._heard_sound and not self._warned_silent and now - self._opened_at > SILENT_MIC_S:
            self._warned_silent = True
            self._bus.publish(MicChanged(True, source.name, "the mic sends only silence: muted, or blocked in Windows privacy settings?"))

    def _open_source(self) -> None:
        source = self._source
        try:
            source.open()
        except MicError as error:
            delay = RETRY_S[min(self._failures, len(RETRY_S) - 1)]
            self._failures += 1
            self._retry_at = time.perf_counter() + delay
            message = str(error)
            if message != self._last_error:
                self._last_error = message
                _log.warning("mic: %s (retrying)", message)
                self._bus.publish(MicChanged(False, source.setting, message))
            return
        self._failures = 0
        self._last_error = ""
        self._pos = self._ring.end
        self._opened_at = time.perf_counter()
        self._heard_sound = False
        self._warned_silent = False
        self._gate = EnergyGate()
        if self._vad is not None:
            self._vad.reset()
        self._bus.publish(MicChanged(True, source.name, self._why_next))

    def _close_source(self, why: str) -> None:
        self._cancel_utterance()
        self._source.close()
        self._bus.publish(MicChanged(False, self._source.name, why))

    def _cancel_utterance(self) -> None:
        ended = self._ep.cancel()
        if ended is not None:
            self._on_vad_event(ended)

    # audio --------------------------------------------------------------------------------

    def _time_of(self, pos: int) -> float:
        end, at = self._ring.arrival
        return at - (end - pos) / SAMPLE_RATE - self._source.latency

    def _read_audio(self) -> None:
        ring = self._ring
        if ring.end - self._pos > ring.capacity - FRAME:
            _log.warning("speech worker fell %.1f s behind; skipping ahead", (ring.end - self._pos) / SAMPLE_RATE)
            self._cancel_utterance()
            self._pos = ring.end
        vad = self._vad
        assert vad is not None
        loudest = -1.0  # for the island's waveform, while a session listens
        while ring.end - self._pos >= FRAME:
            pos = self._pos
            frame = ring.read(pos, pos + FRAME)
            if frame is None:
                self._pos = ring.end
                return
            self._pos = pos + FRAME
            energy = float(np.dot(frame, frame)) / FRAME
            loudest = max(loudest, energy)
            if energy > 0.0:
                self._heard_sound = True
            self.frames += 1
            if self._gate.check(energy, busy=self._ep.listening or self._attentive):
                self.vad_frames += 1
                p = vad.probability(frame)
            else:
                vad.skip(frame)
                p = 0.0
            event = self._ep.push(pos, frame, p)
            if event is not None:
                self._on_vad_event(event)
            self._schedule()
        if self._attentive and loudest >= 0.0:
            self._bus.publish(MicLevel(level_of(loudest)))
        # Wake per block once speech may be starting (endpoints stay exact); in batches otherwise.
        ring.wake_at = self._pos + FRAME * (1 if self._ep.listening or self._attentive else IDLE_BATCH)

    def _on_vad_event(self, event: SpeechStarted | SpeechEnded) -> None:
        if isinstance(event, SpeechStarted):
            utt = self._ep.active
            assert utt is not None
            self._talks[event.utterance] = _Talk(event, next_partial=utt.speech_start + PARTIAL_EVERY)
            self._bus.publish(event)
            return
        self._bus.publish(event)
        stale = [key for key, final in self._finals.items() if key[0] == event.utterance and final.ended is None]
        if event.reason == "cancel":
            for key in stale:
                del self._finals[key]
            self._talks.pop(event.utterance, None)
            return
        key = (event.utterance, event.start, event.end)
        final = self._finals.get(key)
        for other in stale:
            if other != key:
                del self._finals[other]
        final_is_new = final is None
        if final is None:
            final = _Final(key)
            self._finals[key] = final
        else:
            final.early = True
        final.ended = event
        if final_is_new:
            assert self._final_lane is not None
            self._final_lane.put(_Job("final", *key, pcm=event.pcm), self._wanted)
        final.endpoint_at = time.perf_counter()
        if final.result is not None:
            self._emit_final(final)

    def _schedule(self) -> None:
        utt = self._ep.active
        if utt is None:
            return
        pause = self._ep.final_range()
        if pause is not None:
            key = (utt.id, *pause)
            if key not in self._finals:
                for other in [k for k, f in self._finals.items() if k[0] == utt.id and f.ended is None]:
                    del self._finals[other]
                self._finals[key] = _Final(key)
                assert self._final_lane is not None
                self._final_lane.put(_Job("final", *key, pcm=self._ep.audio(*pause)), self._wanted)
            self._candidate(self._finals[key])
            return  # nothing new to show during a pause
        talk = self._talks.get(utt.id)
        lane = self._partial_lane
        # Partials only while words are coming: in a pause a new one would just compete with the early final.
        if talk is None or lane is None or utt.silence > 0 or utt.end < talk.next_partial or lane.busy():
            return
        start, windowed = utt.start, False
        if utt.end - utt.start > PARTIAL_WINDOW:
            start, windowed = self._quiet_cut(utt.end - PARTIAL_WINDOW), True
        if lane.offer(_Job("partial", utt.id, start, utt.end, self._ep.audio(start, utt.end), windowed)):
            talk.next_partial = utt.end + PARTIAL_EVERY

    def _wanted(self, job: _Job) -> bool:
        """Still worth decoding: a real final, or the early decode of the current pause."""
        return job.key in self._finals

    def _quiet_cut(self, target: int) -> int:
        """Window start for a long partial: the quietest frame near ``target`` (fewest cut words)."""
        utt = self._ep.active
        assert utt is not None
        probs = self._ep.probabilities
        first = max(0, (target - utt.start) // FRAME)
        last = min(len(probs), first + PARTIAL_SNAP_FRAMES)
        best = min(range(first, last), key=probs.__getitem__) if last > first else first
        return utt.start + best * FRAME

    # results ------------------------------------------------------------------------------

    def _deliver(self, result: _Result) -> None:  # lane threads
        self._results.append(result)
        self._wake.set()

    def _take_results(self) -> None:
        while self._results:
            result = self._results.popleft()
            job = result.job
            if job.kind == "partial":
                self._show(job, result.text)
                continue
            final = self._finals.get(job.key)
            if final is None:
                continue  # speech resumed after that pause; a newer decode replaced it
            final.result = result
            if final.ended is not None:
                self._emit_final(final)
            else:
                self._show(job, result.text)  # an early decode in a pause: refresh the live words
                self._candidate(final)

    def _candidate(self, final: _Final) -> None:
        """Dictating and the pause is long enough: say what this segment will be if he stays quiet."""
        if not self._dictation_rules or final.announced or final.result is None or final.ended is not None:
            return
        utt = self._ep.active
        if utt is None or utt.id != final.key[0] or utt.silence < CANDIDATE_SAMPLES:
            return
        final.announced = True
        raw = final.result.text
        text = self._vocab.current().correct(raw).text if self._vocab is not None and raw else raw
        if text:
            self._bus.publish(PauseCandidate(utt.id, text, raw))

    def _show(self, job: _Job, text: str) -> None:
        utt = self._ep.active
        talk = self._talks.get(job.utterance)
        if utt is None or utt.id != job.utterance or talk is None or job.end <= talk.shown_end:
            return
        if self._vocab is not None:
            text = self._vocab.current().correct(text).text
        talk.shown_end = job.end
        if text and text != talk.shown_text:
            talk.shown_text = text
            talk.partials += 1
            self._bus.publish(PartialTranscript(job.utterance, text, job.windowed))

    def _emit_final(self, final: _Final) -> None:
        self._finals.pop(final.key, None)
        result, ended = final.result, final.ended
        assert result is not None and ended is not None
        raw = result.text
        began = time.perf_counter()
        changes: tuple[tuple[str, str], ...] = ()
        text = raw
        if self._vocab is not None and raw:
            corrected = self._vocab.current().correct(raw)
            text, changes = corrected.text, corrected.changes
        now = time.perf_counter()
        vocab_ms = (now - began) * 1000
        final_ms = (now - ended.speech_end_at) * 1000
        audio_ms = len(ended.pcm) * 1000 / SAMPLE_RATE
        self._bus.publish(
            FinalTranscript(ended.utterance, text, raw, ended.reason, result.stt_ms, final_ms, audio_ms, final.early)
        )
        talk = self._talks.pop(ended.utterance, None)
        if self._trace is None:
            return
        record = {
            "kind": "utterance",
            "utterance": ended.utterance,
            "source": "wav" if self._source.setting == "wav" else "mic",
            "device": self._source.name,
            "speech_end": _wall(ended.speech_end_at),
            "reason": ended.reason,
            "audio_ms": round(audio_ms),
            "speech_ms": round((ended.speech_end_at - talk.started.at) * 1000) if talk else None,
            "stt_ms": round(result.stt_ms, 1),
            "final_ms": round(final_ms, 1),
            "endpoint_ms": round((final.endpoint_at - ended.speech_end_at) * 1000, 1),
            "wait_ms": round((now - final.endpoint_at) * 1000, 1),
            "early_decode": final.early,
            "partials": talk.partials if talk else 0,
            "vocab_ms": round(vocab_ms, 2),
            "stt": self._stt.name if self._stt else "",
            "raw_text": raw,
            "text": text,
            "changes": [list(change) for change in changes],
        }
        if result.error:
            record["error"] = result.error
        self._trace.write(**record)


def standard_worker(
    bus: Bus,
    *,
    vocab: VocabFile | None,
    trace: TraceLog | None,
    device: str = "default",
    source: Callable[[Ring, threading.Event], Source] | None = None,
) -> SpeechWorker:
    """The worker with Nova's own models (Parakeet + Silero from models/)."""

    def load_stt() -> Transcriber:
        from .stt import parakeet

        return parakeet.load(paths.MODELS)

    return SpeechWorker(
        bus,
        load_stt=load_stt,
        load_vad=lambda: Silero(paths.MODELS / "silero" / "silero_vad.onnx"),
        vocab=vocab,
        trace=trace,
        device=device,
        source=source,
    )
