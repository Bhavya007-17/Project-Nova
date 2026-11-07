"""SpeechWorker scheduling with fakes: no mic, no models. Frames are fed faster than real time."""

import queue
import threading
import time

import numpy as np
import pytest

from nova.bus import Bus
from nova.events import FinalTranscript, MicChanged, MicLevel, PartialTranscript, SpeechEnded, SpeechReady, SpeechStarted
from nova.speech import SpeechWorker
from nova.vad import FRAME


class FakeSource:
    name = "fake mic"
    setting = "fake"
    latency = 0.0

    def __init__(self, ring, wake):
        self.ring, self.wake = ring, wake
        self.is_open = False
        self.opens = 0

    def open(self):
        self.is_open = True
        self.opens += 1

    def close(self):
        self.is_open = False

    def check(self, now):
        return None

    def feed(self, *, speech_ms=0, silence_ms=0):
        rng = np.random.default_rng(len(str(speech_ms)) + silence_ms)
        for ms, level in ((speech_ms, 0.3), (silence_ms, 0.001)):
            for _ in range(int(np.ceil(ms / 32))):
                self.ring.write((rng.standard_normal(FRAME) * level).astype(np.float32), time.perf_counter())
        self.wake.set()


class FakeVad:
    def probability(self, frame):
        return 0.9 if np.abs(frame).max() > 0.1 else 0.05

    def reset(self):
        pass

    def skip(self, frame):
        pass


class FakeStt:
    name = "fake"

    def __init__(self, partial_s=0.0, final_s=0.0):
        self.delays = {"nova-stt-partial": partial_s, "nova-stt-final": final_s}
        self.calls = []

    def transcribe(self, pcm):
        lane = threading.current_thread().name
        self.calls.append((lane, len(pcm)))
        time.sleep(self.delays.get(lane, 0.0))
        return f"{len(pcm) // FRAME} frames"


class Harness:
    def __init__(self, stt):
        self.bus = Bus()
        self.events = queue.Queue()
        self.bus.subscribe(object, self.events.put)
        self.sources = []

        def make_source(ring, wake):
            source = FakeSource(ring, wake)
            self.sources.append(source)
            return source

        self.worker = SpeechWorker(self.bus, load_stt=lambda: stt, load_vad=FakeVad, source=make_source)
        self.worker.start()
        assert isinstance(self.next(SpeechReady), SpeechReady)
        assert self.next(MicChanged).open

    @property
    def source(self):
        return self.sources[0]

    def next(self, kind, timeout=3.0):
        deadline = time.monotonic() + timeout
        while True:
            event = self.events.get(timeout=max(0.01, deadline - time.monotonic()))
            if isinstance(event, kind):
                return event

    def collect(self, seconds):
        out, deadline = [], time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                out.append(self.events.get(timeout=0.02))
            except queue.Empty:
                pass
        return out

    def close(self):
        self.worker.close()


@pytest.fixture
def make():
    made = []

    def factory(stt):
        harness = Harness(stt)
        made.append(harness)
        return harness

    yield factory
    for harness in made:
        harness.close()


def test_started_partial_ended_final_in_order(make):
    stt = FakeStt()
    h = make(stt)
    h.source.feed(silence_ms=500)
    h.source.feed(speech_ms=1200, silence_ms=600)
    events = [e for e in h.collect(0.6) if not isinstance(e, MicChanged)]
    kinds = [type(e) for e in events]
    assert kinds[0] is SpeechStarted
    ended_at = kinds.index(SpeechEnded)
    assert kinds[-1] is FinalTranscript
    assert all(k is PartialTranscript for k in kinds[1:ended_at])
    ended, final = events[ended_at], events[-1]
    assert final.utterance == ended.utterance == events[0].utterance
    assert final.raw_text == f"{len(ended.pcm) // FRAME} frames"  # decoded exactly the final audio
    assert final.reason == "silence"


def test_final_reuses_the_early_decode_from_the_pause(make):
    stt = FakeStt()
    h = make(stt)
    h.source.feed(silence_ms=500)
    h.source.feed(speech_ms=900, silence_ms=600)
    h.next(FinalTranscript)
    finals = [n for lane, n in stt.calls if lane == "nova-stt-final"]
    assert len(finals) == 1  # one decode, started at 160 ms into the pause, became the final


def test_final_never_waits_behind_a_slow_partial(make):
    stt = FakeStt(partial_s=0.8, final_s=0.01)
    h = make(stt)
    h.source.feed(silence_ms=300)
    h.source.feed(speech_ms=1500, silence_ms=500)
    ended = h.next(SpeechEnded)
    t_end = time.monotonic()
    final = h.next(FinalTranscript)
    assert time.monotonic() - t_end < 0.4
    assert final.utterance == ended.utterance
    partial_calls = [c for c in stt.calls if c[0] == "nova-stt-partial"]
    assert len(partial_calls) == 1  # later partials were skipped while it ran


def test_off_cancels_the_utterance_and_closes_the_mic(make):
    h = make(FakeStt())
    h.source.feed(silence_ms=300)
    h.source.feed(speech_ms=600)
    h.next(SpeechStarted)
    h.worker.set_enabled(False)
    ended = h.next(SpeechEnded)
    assert ended.reason == "cancel"
    closed = h.next(MicChanged)
    assert not closed.open and "off" in closed.why
    assert not [e for e in h.collect(0.3) if isinstance(e, FinalTranscript)]
    h.worker.set_enabled(True)
    reopened = h.next(MicChanged)
    assert reopened.open and h.source.opens == 2


def test_lock_closes_and_unlock_reopens(make):
    h = make(FakeStt())
    h.worker.set_locked(True)
    assert "locked" in h.next(MicChanged).why
    h.worker.set_locked(False)
    event = h.next(MicChanged)
    assert event.open and "unlocked" in event.why


def test_key_release_ends_at_once(make):
    h = make(FakeStt())
    h.source.feed(silence_ms=300)
    h.source.feed(speech_ms=800)
    h.next(SpeechStarted)
    h.worker.end_now()
    ended = h.next(SpeechEnded)
    assert ended.reason == "key"
    final = h.next(FinalTranscript)
    assert final.reason == "key"


def test_worker_survives_an_internal_error(make, monkeypatch):
    h = make(FakeStt())
    calls = {"n": 0}
    real = FakeVad.probability

    def flaky(self, frame):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("boom")
        return real(self, frame)

    monkeypatch.setattr(FakeVad, "probability", flaky)
    h.source.feed(silence_ms=300)
    h.source.feed(speech_ms=600, silence_ms=400)
    time.sleep(0.5)  # the audio pending at the error is dropped; the worker carries on
    h.source.feed(speech_ms=600, silence_ms=400)
    final = h.next(FinalTranscript)
    assert final.text


def test_back_to_back_utterances_each_get_a_final(make):
    stt = FakeStt(final_s=0.05)
    h = make(stt)
    h.source.feed(silence_ms=300)
    for _ in range(3):
        h.source.feed(speech_ms=500, silence_ms=350)
    finals = [h.next(FinalTranscript) for _ in range(3)]
    assert [f.utterance for f in finals] == [1, 2, 3]


def test_held_key_bridges_pauses_until_release(make):
    h = make(FakeStt())
    h.worker.set_held(True)
    h.source.feed(silence_ms=300)
    h.source.feed(speech_ms=500, silence_ms=900)  # a pause that would end an utterance
    h.source.feed(speech_ms=500)
    h.next(SpeechStarted)
    assert not [e for e in h.collect(0.3) if isinstance(e, SpeechEnded)]
    h.worker.set_held(False)
    h.worker.end_now()
    ended = h.next(SpeechEnded)
    assert ended.reason == "key" and len(ended.pcm) >= 1900 * 16  # both halves, one utterance
    assert h.next(FinalTranscript).reason == "key"


def test_cancel_drops_the_utterance_without_text(make):
    h = make(FakeStt())
    h.source.feed(silence_ms=300)
    h.source.feed(speech_ms=700)
    h.next(SpeechStarted)
    h.worker.cancel()
    assert h.next(SpeechEnded).reason == "cancel"
    assert not [e for e in h.collect(0.4) if isinstance(e, FinalTranscript)]
    h.source.feed(speech_ms=500, silence_ms=400)  # listening goes on
    assert h.next(FinalTranscript).text


def test_attentive_runs_vad_on_every_frame(make):
    h = make(FakeStt())
    h.worker.set_attentive(True)
    h.source.feed(silence_ms=1000)
    time.sleep(0.3)
    assert h.worker.vad_frames == h.worker.frames  # the energy gate never skips while a session waits for speech


def test_mic_level_flows_only_while_a_session_listens(make):
    h = make(FakeStt())
    h.source.feed(speech_ms=300, silence_ms=100)
    assert not [e for e in h.collect(0.3) if isinstance(e, MicLevel)]  # nobody asked: the island is hidden
    h.worker.set_attentive(True)
    time.sleep(0.05)
    h.source.feed(speech_ms=300)
    levels = [e.level for e in h.collect(0.3) if isinstance(e, MicLevel)]
    assert levels and max(levels) > 0.9  # loud fake speech: a full waveform
    h.worker.set_attentive(False)
    time.sleep(0.05)
    h.collect(0.1)
    h.source.feed(speech_ms=300)
    assert not [e for e in h.collect(0.3) if isinstance(e, MicLevel)]


# --- dictation (Stage 07) ---------------------------------------------------------------------------


def test_dictation_announces_a_pause_that_may_end_the_segment(make):
    from nova.events import PauseCandidate

    h = make(FakeStt())
    h.worker.set_dictating(True)
    h.source.feed(speech_ms=1000, silence_ms=400)
    candidate = h.next(PauseCandidate)
    assert not [e for e in h.collect(0.2) if isinstance(e, SpeechEnded)]  # 400 ms isn't the end of a segment
    h.source.feed(silence_ms=400)
    final = h.next(FinalTranscript)
    assert final.text == candidate.text and final.early


def test_no_pause_candidates_for_commands(make):
    from nova.events import PauseCandidate

    h = make(FakeStt())
    h.source.feed(speech_ms=1000, silence_ms=900)
    events = [*h.collect(0.5)]
    assert any(isinstance(e, FinalTranscript) for e in events)
    assert not any(isinstance(e, PauseCandidate) for e in events)
