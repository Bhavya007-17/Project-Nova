"""`nova listen`: print what the mic hears. Live words (dim) rewrite one line; finals stay, with timings.

`--wav a.wav b.wav` plays files through the exact live path (real time) instead of the mic.
"""

from __future__ import annotations

import shutil
import statistics
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path

from . import paths
from .audio import WavFeed
from .bus import Bus
from .config import ConfigStore
from .console import enable_vt
from .cpu import IdleMeter, cycles_per_core_second
from .events import FinalTranscript, MicChanged, PartialTranscript, SpeechEnded, SpeechReady, SpeechStarted
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
        after = "after you let go" if event.reason == "key" else "after you stopped"
        timing = f"stt {event.stt_ms:.0f} ms · {event.final_ms:.0f} ms {after}"
        if event.reason == "cap":
            timing += " · cut at 30 s"
        if event.text != event.raw_text:
            timing += f" · heard: {event.raw_text}"
        self.line(f"{event.text}   {self._dim(timing)}")

    def summary(self) -> str | None:
        if not self.finals:
            return None
        after = [f.final_ms for f in self.finals]
        stt = [f.stt_ms for f in self.finals]
        text = f"{len(after)} finals · after you stopped: p50 {_pct(after, 50):.0f} ms, p95 {_pct(after, 95):.0f} ms"
        return text + f" · stt p50 {_pct(stt, 50):.0f} ms"


def _pct(values: Sequence[float], q: int) -> float:
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[q - 1]


def main(device: str | None = None, wavs: Sequence[Path] = (), seconds: float | None = None) -> int:
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
        worker.close()
        if lock is not None:
            lock.close()
        trace.close()
        lines = [screen.summary() or "no finals"]
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
