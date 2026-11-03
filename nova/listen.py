"""`nova listen`: print what the mic hears. Live words (dim) rewrite one line; finals stay, with timings.

`--wav a.wav b.wav` plays files through the exact live path (real time) instead of the mic.
`--gate` adds Stage 02: the keyboard hook, the state machine and the gate; each final
prints as ACCEPTED (and why) or IGNORED (and why), state changes dim.
"""

from __future__ import annotations

import shutil
import statistics
import sys
import threading
import time
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from . import paths
from .audio import WavFeed
from .bus import Bus
from .config import ConfigStore
from .console import enable_vt
from .cpu import IdleMeter, cycles_per_core_second
from .events import (
    DictatedText,
    DictationChanged,
    FinalTranscript,
    Gated,
    MicChanged,
    PartialTranscript,
    PreloadHint,
    SessionCancelled,
    SpeechEnded,
    SpeechReady,
    SpeechStarted,
    StateChanged,
)
from .log import TraceLog, setup_app_log
from .session import LockWatcher
from .speech import SpeechWorker, standard_worker
from .vocab import VocabFile

_DIM, _RESET = "\x1b[2m", "\x1b[0m"
_CLEAR = "\r\x1b[2K"


class Screen:
    """Thread-safe console output; bus handlers call it from the speech thread."""

    def __init__(self, vt: bool, out=None) -> None:
        self._vt = vt
        self._out = out or sys.stdout
        self._lock = threading.Lock()
        self._partial = False
        self.finals: list[FinalTranscript] = []

    def _dim(self, text: str) -> str:
        return f"{_DIM}{text}{_RESET}" if self._vt else text

    def _clear(self) -> None:
        if self._partial:
            self._out.write(_CLEAR)
            self._partial = False

    def line(self, text: str) -> None:
        with self._lock:
            self._clear()
            self._out.write(text + "\n")
            self._out.flush()

    def partial(self, event: PartialTranscript) -> None:
        if not self._vt:
            return  # no in-place lines without a real console
        width = max(20, shutil.get_terminal_size((100, 20)).columns - 1)
        shown = ("… " if event.windowed else "") + event.text
        if len(shown) > width:
            shown = "…" + shown[-(width - 1) :]
        with self._lock:
            self._out.write(_CLEAR + self._dim(shown))
            self._out.flush()
            self._partial = True

    def ended(self, event: SpeechEnded) -> None:
        if event.reason == "cancel":
            with self._lock:
                self._clear()
                self._out.flush()

    def final(self, event: FinalTranscript) -> None:
        if not event.text:
            with self._lock:
                self._clear()
                self._out.flush()
            return
        self.finals.append(event)
        self.line(f"{event.text}   {self._dim(timing(event))}")

    def summary(self) -> str | None:
        if not self.finals:
            return None
        after = [f.final_ms for f in self.finals]
        stt = [f.stt_ms for f in self.finals]
        text = f"{len(after)} finals · after you stopped: p50 {_pct(after, 50):.0f} ms, p95 {_pct(after, 95):.0f} ms"
        return text + f" · stt p50 {_pct(stt, 50):.0f} ms"


def timing(event: FinalTranscript) -> str:
    after = "after you let go" if event.reason == "key" else "after you stopped"
    text = f"stt {event.stt_ms:.0f} ms · {event.final_ms:.0f} ms {after}"
    if event.reason == "cap":
        text += " · cut at 30 s"
    if event.text != event.raw_text:
        text += f" · heard: {event.raw_text}"
    return text


_HOW = {"copilot": "Copilot key", "chord": "chord", "wake": "wake name", "named": "wake name"}


class GateView:
    """`nova listen --gate`: one line per final with the gate's verdict; state changes and hints dim."""

    def __init__(self, screen: Screen) -> None:
        self._screen = screen
        self._finals: dict[int, FinalTranscript] = {}
        self.accepted: Counter[str] = Counter()
        self.ignored: Counter[str] = Counter()

    def _dim(self, text: str) -> str:
        return self._screen._dim(text)

    def final(self, event: FinalTranscript) -> None:
        if event.text:
            self._screen.finals.append(event)
        self._finals[event.utterance] = event

    def gated(self, event: Gated) -> None:
        final = self._finals.pop(event.utterance, None)
        when = f"   {self._dim(timing(final))}" if final is not None and event.accepted else ""
        if event.wake_only:
            self.accepted["name only"] += 1
            self._screen.line(f"WAKE      listening for the command{when}")
        elif event.accepted:
            self.accepted[event.via] += 1
            safe = self._dim(" (safe actions only)") if event.safe_only else ""
            said = f"   {self._dim('said: ' + event.heard)}" if event.text != event.heard else ""
            self._screen.line(f"ACCEPTED  {event.via:<10}{event.text}{safe}{said}{when}")
        elif event.via == "empty":
            self.ignored[event.via] += 1
            self._screen.line(self._dim("IGNORED   nothing heard"))
        else:
            self.ignored[event.via] += 1
            self._screen.line(f"IGNORED   {self._dim(event.why + ':')} {event.heard}")

    def state(self, event: StateChanged) -> None:
        how = f" ({_HOW.get(event.trigger, event.trigger)})" if event.trigger and event.new == "listening" else ""
        self._screen.line(self._dim(f"[{event.old} -> {event.new}{how}: {event.why}]"))

    def preload(self, event: PreloadHint) -> None:
        if event.reason in ("key", "wake"):
            self._screen.line(self._dim(f"[preload hint: {event.reason}]"))

    def dictation(self, event: DictationChanged) -> None:
        if event.on:
            self._screen.line(f"DICTATION on ({event.why}): Stage 07 types it; tap the key to stop")
        else:
            self._screen.line(f"DICTATION off ({event.why})")

    def dictated(self, event: DictatedText) -> None:
        self._screen.line(self._dim(f"  dictated: {event.text}"))

    def cancelled(self, event: SessionCancelled) -> None:
        self._screen.line(self._dim(f"[cancelled: {event.why}]"))

    def summary(self) -> str:
        def counts(counter: Counter[str]) -> str:
            return ", ".join(f"{name} {n}" for name, n in counter.most_common())

        accepted, ignored = sum(self.accepted.values()), sum(self.ignored.values())
        text = f"gate: {accepted} accepted"
        if accepted:
            text += f" ({counts(self.accepted)})"
        text += f", {ignored} ignored"
        if ignored:
            text += f" ({counts(self.ignored)})"
        return text


def _pct(values: Sequence[float], q: int) -> float:
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[q - 1]


def main(device: str | None = None, wavs: Sequence[Path] = (), seconds: float | None = None, gate: bool = False) -> int:
    vt = enable_vt()
    close_log = setup_app_log(paths.LOGS, console=False)
    store = ConfigStore(paths.SETTINGS)
    store.load()
    screen = Screen(vt)
    if store.error:
        screen.line(f"settings: {store.error} (using {'the last good' if store.loaded_once else 'default'} settings)")
    settings = store.settings
    vocab = VocabFile(paths.VOCAB)
    vocab.current()
    if vocab.error:
        screen.line(f"vocab: {vocab.error} (using the last good names)")
    trace = TraceLog(paths.LOGS, transcripts=settings.logs.transcripts)
    bus = Bus()
    feeds: list[WavFeed] = []

    def wav_source(ring, wake):
        feed = WavFeed(ring, wake, [Path(p) for p in wavs])
        feeds.append(feed)
        return feed

    worker = standard_worker(
        bus, vocab=vocab, trace=trace, device=device or settings.audio.device, source=wav_source if wavs else None
    )
    first_open = [True]
    idle = IdleMeter(cycles_per_core_second())
    idle.busy("mic")  # nothing counts until the mic is open

    def on_mic(event: MicChanged) -> None:
        if event.open:
            idle.free("mic")
        else:
            idle.busy("mic")  # a closed mic would flatter the idle number
        if event.open and first_open[0] and event.why == "listening":
            first_open[0] = False
            screen.line(f"Listening on {event.device}. Talk; Ctrl+C stops.\n")
        elif event.open:
            screen.line(_dim_status(vt, f"[mic: {event.why} - {event.device}]"))
        else:
            screen.line(_dim_status(vt, f"[mic closed: {event.why}]"))

    def on_ready(event: SpeechReady) -> None:
        if event.ok:
            screen.line(f"Speech model ready in {event.load_ms / 1000:.1f} s ({event.detail}); {len(vocab.current().terms)} vocab names.")
        else:
            screen.line(event.detail)

    bus.subscribe(SpeechReady, on_ready)
    bus.subscribe(MicChanged, on_mic)
    bus.subscribe(PartialTranscript, screen.partial)
    bus.subscribe(SpeechEnded, screen.ended)
    view: GateView | None = None
    gate_parts = None
    if gate:
        view = GateView(screen)
        bus.subscribe(FinalTranscript, view.final)
        gate_parts = _start_gate(bus, worker, settings, trace, screen, view, keys=not wavs)
    else:
        bus.subscribe(FinalTranscript, screen.final)
    bus.subscribe(SpeechStarted, lambda event: idle.busy(event.utterance))
    bus.subscribe(FinalTranscript, lambda event: idle.free(event.utterance))
    bus.subscribe(SpeechEnded, lambda event: idle.free(event.utterance) if event.reason == "cancel" else None)
    lock = None if wavs else LockWatcher(worker.set_locked)
    screen.line("Nova listen: loading the speech model (about 7 s)...")
    if lock is not None and not lock.start():
        screen.line(f"note: {lock.error}")
    worker.start()
    try:
        return _wait(worker, feeds, idle, seconds)
    except KeyboardInterrupt:
        return 0
    finally:
        hook_line = None
        if gate_parts is not None:
            triggers, hook = gate_parts
            triggers.close()
            if hook is not None:
                hook.close()
                stats = hook.hook.stats
                hook_line = (
                    f"keyboard hook: {stats.calls} keys, callback mean {stats.mean_ms * 1000:.0f} us, "
                    f"max {stats.max_ms:.2f} ms, reinstalled {stats.reinstalls} times"
                )
        worker.close()
        if lock is not None:
            lock.close()
        trace.close()
        lines = [screen.summary() or "no finals"]
        if view is not None:
            lines.append(view.summary())
        if hook_line is not None:
            lines.append(hook_line)
        if not wavs:
            if idle.percent is not None:
                share = 100 * worker.vad_frames / worker.frames if worker.frames else 0.0
                lines.append(
                    f"idle CPU {idle.percent:.1f} % of one core over {idle.idle_seconds:.0f} s of quiet"
                    f" (Silero ran on {share:.0f} % of all frames)"
                )
            else:
                lines.append("idle CPU: no quiet stretch long enough to measure")
        screen.line("\n" + "\n".join(lines))
        close_log()


def _start_gate(bus: Bus, worker: SpeechWorker, settings, trace: TraceLog, screen: Screen, view: GateView, *, keys: bool):
    """Stage 02 parts: controller + stub brain (+ the keyboard hook on the live mic)."""
    from .hook import TriggerHook
    from .keys import TriggerKeys, parse_chord
    from .triggers import Triggers, load_start_menu_names, stub_brain

    for kind, handler in (
        (Gated, view.gated), (StateChanged, view.state), (PreloadHint, view.preload),
        (DictationChanged, view.dictation), (DictatedText, view.dictated), (SessionCancelled, view.cancelled),
    ):
        bus.subscribe(kind, handler)
    stub_brain(bus)
    trigger_keys = TriggerKeys() if keys else None
    triggers = Triggers(bus, worker, settings, keys=trigger_keys, trace=trace)
    t = settings.triggers
    onoff = {True: "on", False: "off"}
    screen.line(
        f"Gate: Copilot key {onoff[t.copilot_key and keys]}, {parse_chord(settings.chord).label} "
        f"{onoff[t.chord and keys]}, wake name {settings.wake_name!r} {onoff[t.wake_name]}, "
        f"always-listening {onoff[t.always_listening]}."
    )
    hook = None
    if trigger_keys is not None:
        hook = TriggerHook(trigger_keys, triggers.key_event)
        if not hook.start():
            screen.line(f"keys: {hook.error}")
            hook = None

    def names_loaded(names: list[str]) -> None:
        triggers.set_names(names)
        screen.line(_dim_status(screen._vt, f"[{len(names)} Start-menu apps known to always-listening]"))

    load_start_menu_names(names_loaded)
    triggers.start()
    return triggers, hook


def _dim_status(vt: bool, text: str) -> str:
    return f"{_DIM}{text}{_RESET}" if vt else text


def _wait(worker: SpeechWorker, feeds: list[WavFeed], idle: IdleMeter, seconds: float | None) -> int:
    while not worker.ready.wait(0.1):
        pass
    if worker.failed:
        return 1
    stop_at = time.monotonic() + seconds if seconds else None
    while True:
        time.sleep(0.25)  # each wake-up costs CPU too
        idle.sample()
        if stop_at is not None and time.monotonic() >= stop_at:
            return 0
        if feeds and feeds[0].done.is_set() and worker.idle:
            time.sleep(0.2)
            if worker.idle:
                return 0
