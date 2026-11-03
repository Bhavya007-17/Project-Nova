"""`nova island --demo [scene]`: every island state with fake data, for tuning without talking.

A worker thread publishes made-up bus events (the same path real events take) on a
script: scenes of beats. No mic, no hook, no models. Ctrl+C quits.
"""

from __future__ import annotations

import math
import random
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..bus import Bus
from ..events import (
    AlwaysListeningChanged,
    Asking,
    CommandFinished,
    DictatedText,
    FinalTranscript,
    Gated,
    JobChip,
    MicChanged,
    MicLevel,
    PartialTranscript,
    PinToggled,
    SpeechReady,
    StateChanged,
    StepsChanged,
)

LEVEL_HZ = 30


@dataclass(frozen=True)
class Go:
    """Move to a state (the runner fills in the old one)."""

    state: str
    why: str = ""


@dataclass(frozen=True)
class Talk:
    """Fake mic levels on (he talks) or off."""

    on: bool


@dataclass(frozen=True)
class Beat:
    wait: float  # seconds after the previous beat
    events: tuple[object, ...] = ()


@dataclass(frozen=True)
class Scene:
    name: str
    about: str
    beats: tuple[Beat, ...]


def _say(utterance: int, *partials: str, final: str, accept: bool = True) -> list[Beat]:
    """Listening with live words every 0.4 s, release, then the final (and the gate's verdict)."""
    beats = [Beat(0.0, (Go("listening", "Copilot key"), Talk(True)))]
    beats += [Beat(0.5 if i == 0 else 0.4, (PartialTranscript(utterance, text),)) for i, text in enumerate(partials)]
    beats.append(Beat(0.35, (Talk(False), Go("thinking", "heard you"))))
    events: list[object] = [FinalTranscript(utterance, final, final, "key", 180.0, 210.0, 2000.0)]
    if accept:
        events.append(Gated(utterance, True, "key", final, final, "Copilot key", False, False, 0.0))
    beats.append(Beat(0.2, tuple(events)))
    return beats


def _scenes() -> tuple[Scene, ...]:
    return (
        Scene("listening", "hold Copilot: waveform + live words; release: the final settles, then the orb", (
            *_say(1, "open", "open my", "open my thermo", "open my thermo homework", "open my thermo homework on canvas",
                  final="Open my thermo homework on Canvas."),
            Beat(1.2),
        )),
        Scene("acting", "the step line: done steps dim, the running one breathes", (
            Beat(0.0, (StepsChanged(("Chrome", "Canvas", "Thermo", "HW 5"), 0), Go("acting", "plan"))),
            Beat(0.7, (StepsChanged(("Chrome", "Canvas", "Thermo", "HW 5"), 1),)),
            Beat(0.7, (StepsChanged(("Chrome", "Canvas", "Thermo", "HW 5"), 2),)),
            Beat(0.7, (StepsChanged(("Chrome", "Canvas", "Thermo", "HW 5"), 3),)),
            Beat(0.7),
        )),
        Scene("done", "✓ + the result, then it collapses after 1.5 s", (
            Beat(0.0, (CommandFinished(True, "Opened HW 5 – Thermo"), Go("done", "Opened HW 5 – Thermo"))),
            Beat(1.5, (Go("idle", "done"),)),
            Beat(1.2),
        )),
        Scene("skill", "⚡ a learned skill ran (no AI)", (
            *_say(2, "pull up", "pull up canvas", final="Pull up Canvas."),
            Beat(0.25, (CommandFinished(True, "Canvas · Dashboard", skill=True), Go("done"))),
            Beat(1.5, (Go("idle", "done"),)),
            Beat(1.2),
        )),
        Scene("error", "✗ what failed, plainly, + Try again / Show me (clickable)", (
            *_say(3, "close", "close the", "close the thing", final="Close the thing."),
            Beat(0.5, (CommandFinished(False, "Couldn't find a window called “the thing”."), Go("error"))),
            Beat(4.0, (Go("idle", "error shown"),)),
            Beat(1.2),
        )),
        Scene("confirm", "the exact action + No / Yes (amber; Enter / Esc / voice / 15 s)", (
            *_say(4, "submit", "submit my thermo", "submit my thermo homework", final="Submit my thermo homework."),
            Beat(0.5, (Asking("confirm", "Submit “HW 5 – Thermo” on Canvas?"), Go("confirm"))),
            Beat(4.5, (Go("idle", "no answer"),)),
            Beat(1.2),
        )),
        Scene("clarify", "a question + numbered choices (voice / click / 1-4)", (
            *_say(5, "open", "open chrome", final="Open Chrome."),
            Beat(0.4, (Asking("clarify", "Which Chrome?", ("Google Chrome", "Chrome Canary", "Chrome Beta", "Chrome Dev")),
                       Go("clarify"))),
            Beat(4.5, (StepsChanged(("Chrome Canary",), 0), Go("acting"))),
            Beat(0.6, (CommandFinished(True, "Opened Chrome Canary"), Go("done"))),
            Beat(1.5, (Go("idle", "done"),)),
            Beat(1.2),
        )),
        Scene("teaching", "red dot: recording a new skill", (
            Beat(0.0, (Go("teaching", "show me"),)),
            Beat(3.5, (Go("idle", "saved"),)),
            Beat(1.2),
        )),
        Scene("dictating", "orange ring + the last sentence typed", (
            Beat(0.0, (Go("dictating", "double-tap: dictation"), Talk(True))),
            Beat(0.6, (PartialTranscript(20, "so the first law"),)),
            Beat(0.4, (PartialTranscript(20, "so the first law says energy"),)),
            Beat(0.4, (PartialTranscript(20, "so the first law says energy is conserved"),)),
            Beat(0.5, (DictatedText(20, "So the first law says energy is conserved.", 0.0),)),
            Beat(0.7, (PartialTranscript(21, "heat in minus"),)),
            Beat(0.4, (PartialTranscript(21, "heat in minus work out is"),)),
            Beat(0.4, (PartialTranscript(21, "heat in minus work out is the change in internal energy"),)),
            Beat(0.5, (DictatedText(21, "Heat in minus work out is the change in internal energy.", 0.0), Talk(False))),
            Beat(1.5, (Go("idle", "dictation stopped"),)),
            Beat(1.2),
        )),
        Scene("passthrough", "another app's voice mode has the mic", (
            Beat(0.0, (Go("passthrough", "Claude voice"),)),
            Beat(3.0, (Go("idle", "voice mode ended"),)),
            Beat(1.2),
        )),
        Scene("pinned", "Ctrl+Copilot: a status pill stays between commands (drag it; double-click = back)", (
            Beat(0.0, (PinToggled(0.0),)),
            Beat(2.5, (AlwaysListeningChanged(False),)),
            Beat(2.0, (AlwaysListeningChanged(True),)),
            *_say(7, "open", "open spotify", final="Open Spotify."),
            Beat(0.4, (CommandFinished(True, "Opened Spotify"), Go("done"))),
            Beat(1.5, (Go("idle", "done"),)),
            Beat(3.0, (PinToggled(0.0),)),
            Beat(1.2),
        )),
        Scene("chips", "a background job's chip: alone, then beside the island, then done", (
            Beat(0.0, (JobChip("hw5", "Claude · HW 5", "working"),)),
            Beat(2.0),
            *_say(6, "ask chatgpt", "ask chatgpt what a carnot", "ask chatgpt what a carnot cycle is",
                  final="Ask ChatGPT what a Carnot cycle is."),
            Beat(0.4, (CommandFinished(True, "Asked ChatGPT"), Go("done"), JobChip("carnot", "ChatGPT · Carnot", "working"))),
            Beat(1.5, (Go("idle", "done"), JobChip("hw5", "Claude · HW 5", "done"))),
            Beat(2.5, (JobChip("hw5", "", "gone"),)),
            Beat(1.5, (JobChip("carnot", "", "gone"),)),
            Beat(1.0),
        )),
    )


SCENES = _scenes()
NAMES = tuple(scene.name for scene in SCENES)


class Runner:
    """Plays scenes on a worker thread, publishing their events on the bus."""

    def __init__(self, bus: Bus, scenes: tuple[Scene, ...], *, loop: bool = True,
                 say: Callable[[str], None] = print) -> None:
        self._bus = bus
        self._scenes = scenes
        self._loop = loop
        self._say = say
        self._state = "idle"
        self._talking = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="nova-island-demo", daemon=True)
        self._levels = threading.Thread(target=self._mic, name="nova-island-demo-mic", daemon=True)
        self.finished = threading.Event()

    def start(self) -> None:
        self._thread.start()
        self._levels.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        bus = self._bus
        bus.publish(SpeechReady(True, "demo"))
        bus.publish(MicChanged(True, "demo mic", "demo"))
        try:
            while not self._stop.is_set():
                for scene in self._scenes:
                    self._say(f"{scene.name:<12} {scene.about}")
                    for beat in scene.beats:
                        if self._stop.wait(beat.wait):
                            return
                        for event in beat.events:
                            self._publish(event)
                if not self._loop:
                    return
        finally:
            self._talking = False
            self.finished.set()

    def _publish(self, event: object) -> None:
        if isinstance(event, Go):
            old, self._state = self._state, event.state
            self._bus.publish(StateChanged(old, event.state, event.why or event.state, "copilot", time.perf_counter()))
        elif isinstance(event, Talk):
            self._talking = event.on
        else:
            self._bus.publish(event)

    def _mic(self) -> None:
        """Syllable-like fake levels while "talking"."""
        rng = random.Random(7)
        began = time.perf_counter()
        while not self._stop.wait(1 / LEVEL_HZ):
            if not self._talking:
                continue
            t = time.perf_counter() - began
            syllables = abs(math.sin(2 * math.pi * 2.6 * t)) * (0.65 + 0.35 * math.sin(2 * math.pi * 0.7 * t))
            self._bus.publish(MicLevel(max(0.0, min(1.0, 0.15 + 0.75 * syllables + rng.uniform(-0.08, 0.08)))))


def main(scene: str | None = None) -> int:
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtGui import QGuiApplication

    from .window import Island

    app = QGuiApplication.instance() or QGuiApplication(["nova-island-demo"])
    bus = Bus()
    island = Island(bus)
    drops: list[float] = []

    def first_frame() -> None:
        if island.shown_at is not None:
            drops.append((time.perf_counter() - island.shown_at) * 1000)
            island.shown_at = None

    # Queued: Python must never run on the render thread (the GUI thread can wait on it holding the GIL).
    island.window.frameSwapped.connect(first_frame, Qt.ConnectionType.QueuedConnection)
    scenes = SCENES if scene is None else tuple(s for s in SCENES if s.name == scene)
    print(f"Island demo: {'every state' if scene is None else scene}, on a loop. Ctrl+C quits.")
    runner = Runner(bus, scenes)
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    keepalive = QTimer()  # Python only sees Ctrl+C when it gets to run
    keepalive.timeout.connect(lambda: None)
    keepalive.start(250)
    runner.start()
    try:
        return app.exec()
    finally:
        runner.stop()
        keepalive.stop()
        island.close()
        if drops:
            ordered = sorted(drops)
            print(f"\nisland drop: show -> first frame p50 {ordered[len(ordered) // 2]:.0f} ms, "
                  f"max {ordered[-1]:.0f} ms ({len(drops)} shows)")
        sys.stdout.flush()
