"""The controller thread that runs Nova's state machine (SPEC §5).

Everything that can change Nova's state arrives on one queue and is handled in
order on the "nova-triggers" thread: trigger keys from the hook, speech events,
settings, tray toggles, results. So the pure machine (``nova.state``) and gate
(``nova.gate``) need no locks, and nothing here runs inside the hook callback.
Effects go out as bus events (StateChanged, PreloadHint, Gated, ...) and as calls
on the speech worker (end the utterance now, drop it, bridge pauses, stay attentive).

Detectors are sampled here, around utterances only: which other apps are
recording (once when speech starts) and the speaker meter (10x a second while
someone talks). Nothing runs while the room is quiet.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

from .bus import Bus
from .config import Settings
from .events import (
    AlwaysListeningChanged,
    AnswerHeard,
    BrainProgress,
    CommandFinished,
    ConfigChanged,
    DictatedText,
    DictationChanged,
    FinalTranscript,
    Gated,
    IslandAction,
    PartialTranscript,
    PassthroughStopped,
    PinToggled,
    PowerChanged,
    PreloadHint,
    SessionCancelled,
    SleepRequested,
    SpeechEnded,
    SpeechStarted,
    StateChanged,
)
from .gate import Env, StubResolver, WakeName, strip_wake
from .keys import Cancel, Gestures, TriggerEvent, TriggerKeys, parse_chord
from .log import TraceLog
from .state import (
    Answer,
    Cancelled,
    Command,
    Dictated,
    Dictation,
    Done,
    DropUtterance,
    EndUtterance,
    Heard,
    Ignored,
    Machine,
    PassthroughEnded,
    Power,
    Preload,
    Progress,
    Sleep,
    SpeechEnd,
    SpeechStart,
    State,
    Tick,
    Transition,
    WakeHeard,
    Woke,
    next_deadline,
    outputs,
    step,
)

_log = logging.getLogger(__name__)

SPEAKER_EVERY_S = 0.1  # speaker meter samples while someone talks
_KEEP_UTTERANCES = 8  # per-utterance detector notes kept for finals still on their way


class SpeechControl(Protocol):
    def end_now(self) -> None: ...
    def cancel(self) -> None: ...
    def set_held(self, held: bool) -> None: ...
    def set_attentive(self, on: bool) -> None: ...


class Detector(Protocol):
    def recording_apps(self) -> list[str]: ...
    def refresh_speakers(self) -> None: ...
    def speaker_peak(self) -> float | None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class _Names:
    names: tuple[str, ...]


@dataclass(frozen=True)
class _Resolver:
    resolver: object


_STOP = object()
_SUBSCRIBED = (
    SpeechStarted, SpeechEnded, PartialTranscript, FinalTranscript, PowerChanged, ConfigChanged,
    AlwaysListeningChanged, CommandFinished, BrainProgress, IslandAction, SleepRequested,
)
_STATES = {state.value: state for state in State}


def _audio_detector() -> Detector:
    from .detect import AudioDetector

    return AudioDetector()


class Triggers:
    def __init__(
        self,
        bus: Bus,
        speech: SpeechControl,
        settings: Settings,
        *,
        keys: TriggerKeys | None = None,
        detector: Callable[[], Detector] | None = _audio_detector,
        trace: TraceLog | None = None,
        clock: Callable[[], float] = time.perf_counter,
        names: Iterable[str] = (),
    ) -> None:
        self._bus = bus
        self._speech = speech
        self._keys = keys
        self._make_detector = detector
        self._detector: Detector | None = None
        self._trace = trace
        self._clock = clock
        self._queue: queue.SimpleQueue[object] = queue.SimpleQueue()
        self._machine = Machine()
        self._gestures = Gestures()
        self._resolver = StubResolver(names)
        self._settings = settings
        self._always = settings.triggers.always_listening
        self._wake = WakeName.of(settings.wake_name)
        self._apply_settings(settings, first=True)
        # Per-utterance detector notes (controller thread only).
        self._talking: int | None = None
        self._next_sample = 0.0
        self._peaks: dict[int, list[float]] = {}
        self._recording: dict[int, list[str]] = {}
        self._wake_seen: list[int] = []
        self._unsubscribe = [bus.subscribe(kind, self._queue.put) for kind in _SUBSCRIBED]
        self._thread = threading.Thread(target=self._run, name="nova-triggers", daemon=True)

    # --- any thread ------------------------------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def close(self, timeout: float = 3.0) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._queue.put(_STOP)
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    def key_event(self, event: TriggerEvent) -> None:
        """From inside the hook callback: hand over only."""
        self._queue.put(event)

    def set_names(self, names: Iterable[str]) -> None:
        """Known app names (Start menu) for the always-listening stub resolver."""
        self._queue.put(_Names(tuple(names)))

    def set_resolver(self, resolver: object) -> None:
        """The always-listening resolver (Stage 04: strict Tier 0 against the app catalog)."""
        self._queue.put(_Resolver(resolver))

    @property
    def machine(self) -> Machine:
        return self._machine

    @property
    def known_names(self) -> int:
        return self._resolver.count

    # --- controller thread -----------------------------------------------------------------------

    def _run(self) -> None:
        if self._make_detector is not None:
            try:
                self._detector = self._make_detector()
            except Exception:  # noqa: BLE001 - Nova still works; the gate just can't see calls / speakers
                _log.exception("audio detectors unavailable: calls and playing speakers won't be noticed")
        try:
            while True:
                timeout = self._timeout()
                try:
                    item = self._queue.get(timeout=timeout)
                except queue.Empty:
                    item = None
                if item is _STOP:
                    return
                now = self._clock()
                try:
                    if item is not None:
                        self._handle(item, now)
                    self._feed(Tick(now), now)
                    self._sample(now)
                except Exception:  # noqa: BLE001 - one bad event must not stop the controller
                    _log.exception("trigger controller error on %r", item)
        finally:
            if self._detector is not None:
                self._detector.close()

    def _timeout(self) -> float | None:
        now = self._clock()
        times = [t for t in (next_deadline(self._machine),) if t is not None]
        if self._talking is not None and self._detector is not None:
            times.append(self._next_sample)
        if not times:
            return None
        return max(0.0, min(times) - now)

    def _handle(self, item: object, now: float) -> None:
        if isinstance(item, TriggerEvent):
            if item.source == "answer":
                self._answer_key(item, now)
                return
            if item.source == "pin":
                if item.down:
                    self._bus.publish(PinToggled(now))
                return
            for gesture in self._gestures.feed(item):
                self._feed(gesture, now)
        elif isinstance(item, SpeechStarted):
            self._speech_started(item, now)
        elif isinstance(item, SpeechEnded):
            if self._talking == item.utterance:
                self._talking = None
            if item.reason == "cancel":
                self._forget(item.utterance)
            self._feed(SpeechEnd(item.utterance, item.reason, item.speech_end_at), now)
        elif isinstance(item, PartialTranscript):
            if self._wake.enabled and item.utterance not in self._wake_seen and strip_wake(item.text, self._wake) is not None:
                self._wake_seen = [*self._wake_seen[-_KEEP_UTTERANCES:], item.utterance]
                self._feed(WakeHeard(item.utterance, now), now)
        elif isinstance(item, FinalTranscript):
            self._feed(Heard(item.utterance, item.text, now, self._env(item.utterance)), now)
        elif isinstance(item, PowerChanged):
            self._feed(Power(item.on, now), now)
            self._configure_keys()
        elif isinstance(item, ConfigChanged):
            self._apply_settings(item.settings, first=False)
        elif isinstance(item, AlwaysListeningChanged):
            self._always = item.on
        elif isinstance(item, CommandFinished):
            self._feed(Done(item.ok, now, item.detail), now)
        elif isinstance(item, BrainProgress):
            state = _STATES.get(item.state)
            if state is None:
                _log.warning("brain asked for an unknown state %r", item.state)
            else:
                self._feed(Progress(state, now, item.why), now)
        elif isinstance(item, IslandAction):
            if item.what == "no" and outputs(self._machine).asking:
                self._feed(Cancel(now, "No"), now)  # the island's No = Esc; Yes / a choice is the brain's
        elif isinstance(item, _Names):
            self._resolver = self._resolver.with_names(item.names)
        elif isinstance(item, _Resolver):
            self._resolver = item.resolver
        elif isinstance(item, SleepRequested):
            self._feed(Sleep(item.on, now), now)

    def _answer_key(self, event: TriggerEvent, now: float) -> None:
        """Enter / 1-4 from the hook while the island asks: the same as clicking it."""
        asking = outputs(self._machine).asking
        if asking == "confirm" and event.key == "enter":
            self._bus.publish(IslandAction("yes", now, via="key"))
        elif asking == "clarify" and event.key.isdigit():
            self._bus.publish(IslandAction("choice", now, index=int(event.key) - 1, via="key"))

    def _apply_settings(self, settings: Settings, first: bool) -> None:
        triggers = settings.triggers
        self._wake = WakeName.of(settings.wake_name, settings.wake_spellings, enabled=triggers.wake_name)
        if first or triggers.always_listening != self._settings.triggers.always_listening:
            self._always = triggers.always_listening  # a tray toggle stays until the file says otherwise
        self._settings = settings
        self._configure_keys()

    def _configure_keys(self) -> None:
        if self._keys is None:
            return
        triggers = self._settings.triggers
        on = self._machine.state is not State.OFF  # Off: the keys go back to Windows
        try:
            chord = parse_chord(self._settings.chord) if triggers.chord and on else None
        except ValueError:
            chord = None
        self._keys.configure(chord=chord, copilot=triggers.copilot_key and on)

    def _speech_started(self, event: SpeechStarted, now: float) -> None:
        utterance = event.utterance
        self._talking = utterance
        self._peaks[utterance] = []
        self._recording[utterance] = []
        detector = self._detector
        if detector is not None:
            detector.refresh_speakers()
            self._recording[utterance] = detector.recording_apps()
            self._next_sample = now
        for notes in (self._peaks, self._recording):
            while len(notes) > _KEEP_UTTERANCES:
                notes.pop(next(iter(notes)))
        self._feed(SpeechStart(utterance, event.at), now)
        m = self._machine
        if m.state in (State.IDLE, State.DONE, State.ERROR) and m.session is None:
            # A follow-up may need the LLM; always-listening only acts on what Tier 0 resolves, so "always" is a
            # hint the LLM ignores (room chatter must not keep a model loaded).
            if now < m.follow_up_until:
                self._bus.publish(PreloadHint("follow-up", now))
            elif self._always and not m.asleep:
                self._bus.publish(PreloadHint("always", now))

    def _sample(self, now: float) -> None:
        if self._talking is None or self._detector is None or now < self._next_sample:
            return
        peak = self._detector.speaker_peak()
        if peak is not None:
            self._peaks.setdefault(self._talking, []).append(peak)
        self._next_sample = now + SPEAKER_EVERY_S

    def _forget(self, utterance: int) -> None:
        self._peaks.pop(utterance, None)
        self._recording.pop(utterance, None)

    def _env(self, utterance: int) -> Env:
        from .detect import speakers_playing

        peaks = self._peaks.pop(utterance, [])
        recording = sorted(set(self._recording.pop(utterance, [])))
        return Env(
            self._wake, self._always, self._resolver,
            mic_busy=", ".join(recording) or None, speakers=speakers_playing(peaks),
        )

    def _feed(self, event: object, now: float) -> None:
        before = outputs(self._machine)
        result = step(self._machine, event)
        self._machine = result.machine
        for effect in result.effects:
            self._effect(effect, now)
        after = outputs(self._machine)
        if after.held != before.held:
            self._speech.set_held(after.held)
        if after.attentive != before.attentive:
            self._speech.set_attentive(after.attentive)
        if self._keys is not None and after.esc != before.esc:
            self._keys.esc_armed = after.esc
        if self._keys is not None and after.asking != before.asking:
            self._keys.asking = after.asking

    def _effect(self, effect: object, now: float) -> None:
        bus = self._bus
        if isinstance(effect, Transition):
            _log.info("state %s -> %s (%s)", effect.old.value, effect.new.value, effect.why)
            bus.publish(StateChanged(effect.old.value, effect.new.value, effect.why, effect.trigger, now))
        elif isinstance(effect, EndUtterance):
            self._speech.end_now()
        elif isinstance(effect, DropUtterance):
            self._speech.cancel()
        elif isinstance(effect, Preload):
            bus.publish(PreloadHint(effect.reason, now))
        elif isinstance(effect, (Command, Ignored, Woke)):
            self._gated(effect, now)
        elif isinstance(effect, Answer):
            bus.publish(AnswerHeard(effect.utterance, effect.text, now))
        elif isinstance(effect, Dictated):
            bus.publish(DictatedText(effect.utterance, effect.text, now))
        elif isinstance(effect, Dictation):
            bus.publish(DictationChanged(effect.on, effect.why, now))
        elif isinstance(effect, Cancelled):
            bus.publish(SessionCancelled(effect.why, now))
        elif isinstance(effect, PassthroughEnded):
            bus.publish(PassthroughStopped(effect.why, now))

    def _gated(self, effect: Command | Ignored | Woke, now: float) -> None:
        d = effect.decision
        if self._trace is not None:
            self._trace.write(
                kind="gate", utterance=effect.utterance, accepted=d.accepted, via=d.via, why=d.why,
                safe_only=d.safe_only, text=d.heard,
            )
        self._bus.publish(Gated(effect.utterance, d.accepted, d.via, d.text, d.heard, d.why, d.safe_only, d.wake_only, now))


def stub_brain(bus: Bus) -> Callable[[], None]:
    """No actions (`nova listen --gate`): every accepted command "runs" at once, opening the follow-up window.
    The island shows ✓ + the command it would act on. The tray app runs the real brain (``nova.brain.brain``)."""

    def on_gated(event: Gated) -> None:
        if event.accepted and not event.wake_only:
            bus.publish(CommandFinished(True, event.text))

    return bus.subscribe(Gated, on_gated)
