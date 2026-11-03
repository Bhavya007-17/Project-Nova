"""Mic device choice, open/close and the watchdog, with a fake sounddevice (no hardware)."""

import sys
import threading
import time
import types
import wave

import numpy as np
import pytest

from nova import audio
from nova.audio import BLOCK, Mic, MicError, Ring, WavFeed

MAPPER = "Microsoft Sound Mapper - Input"
ARRAY = "Microphone Array (Intel® Smart "
ARRAY_FULL = "Microphone Array (Intel® Smart Sound Technology for Digital Microphones)"
JABRA = "Headset Microphone (Jabra Evol"
JABRA_FULL = "Headset Microphone (Jabra Evolve2 65)"


class FakeStream:
    def __init__(self, sd, **kwargs):
        if sd.fail_open:
            raise RuntimeError("device unavailable")
        self.kwargs = kwargs
        self.latency = 0.032
        self.started = self.closed = False
        sd.streams.append(self)

    def start(self):
        self.started = True

    def abort(self):
        pass

    def close(self, ignore_errors=False):
        self.closed = True


@pytest.fixture
def sd(monkeypatch):
    fake = types.SimpleNamespace(streams=[], fail_open=False, restarts=0, jabra=True)

    def query_devices():
        devices = [
            {"index": 0, "name": MAPPER, "hostapi": 0, "max_input_channels": 2},
            {"index": 1, "name": ARRAY, "hostapi": 0, "max_input_channels": 4},
            {"index": 5, "name": "Speakers (Realtek)", "hostapi": 0, "max_input_channels": 0},
            {"index": 9, "name": ARRAY_FULL, "hostapi": 1, "max_input_channels": 2},
        ]
        if fake.jabra:
            devices += [
                {"index": 2, "name": JABRA, "hostapi": 0, "max_input_channels": 1},
                {"index": 10, "name": JABRA_FULL, "hostapi": 1, "max_input_channels": 1},
            ]
        return devices

    fake.query_devices = query_devices
    fake.query_hostapis = lambda: [{"name": "MME", "default_input_device": 1}, {"name": "Windows WASAPI", "default_input_device": 9}]
    fake.RawInputStream = lambda **kwargs: FakeStream(fake, **kwargs)
    fake._terminate = lambda: None

    def initialize():
        fake.restarts += 1

    fake._initialize = initialize
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    monkeypatch.setattr(audio, "preferred_input_name", lambda: ARRAY)
    return fake


def test_default_opens_the_sound_mapper_and_shows_the_real_mic(sd):
    mic = Mic(Ring(), threading.Event())
    mic.open()
    stream = sd.streams[-1]
    assert stream.kwargs["device"] == 0
    assert (stream.kwargs["samplerate"], stream.kwargs["channels"], stream.kwargs["dtype"], stream.kwargs["blocksize"]) == (16000, 1, "float32", 512)
    assert mic.name == ARRAY_FULL
    assert mic.is_open and mic.latency == pytest.approx(0.032)
    mic.close()
    assert stream.closed and not mic.is_open


def test_named_mic_by_part_of_its_name(sd):
    mic = Mic(Ring(), threading.Event(), device="evolve2")
    mic.open()
    assert sd.streams[-1].kwargs["device"] == 2
    assert mic.name == JABRA_FULL


def test_missing_named_mic_falls_back_and_looks_again_later(sd):
    sd.jabra = False
    mic = Mic(Ring(), threading.Event(), device="jabra")
    mic.open()
    assert sd.streams[-1].kwargs["device"] == 0
    mic.close()
    sd.jabra = True
    mic.open()  # the device list is restarted before this open
    assert sd.restarts == 1
    assert sd.streams[-1].kwargs["device"] == 2


def test_open_failure_is_a_mic_error_and_restarts_portaudio_next_time(sd):
    sd.fail_open = True
    mic = Mic(Ring(), threading.Event())
    with pytest.raises(MicError, match="cannot open"):
        mic.open()
    assert not mic.is_open
    sd.fail_open = False
    mic.open()
    assert sd.restarts == 1


def test_failure_while_listing_devices_is_a_mic_error(sd):
    def broken():
        raise RuntimeError("PortAudio not initialized")

    sd.query_devices = broken
    with pytest.raises(MicError, match="cannot list"):
        Mic(Ring(), threading.Event()).open()


def test_callback_copies_into_the_ring(sd):
    ring = Ring()
    mic = Mic(ring, threading.Event())
    mic.open()
    block = np.full(BLOCK, 0.25, dtype=np.float32)
    mic._callback(block.tobytes(), BLOCK, None, None)
    np.testing.assert_array_equal(ring.read(0, BLOCK), block)


def test_watchdog_reopens_when_audio_stops(sd):
    ring = Ring()
    mic = Mic(ring, threading.Event())
    mic.open()
    now = time.perf_counter()
    ring.write(np.zeros(BLOCK, dtype=np.float32), at=now)
    assert mic.check(now + 0.5) is None
    assert "stopped" in mic.check(now + audio.STALL_S + 0.1)


def test_watchdog_notices_a_new_windows_default(sd, monkeypatch):
    ring = Ring()
    mic = Mic(ring, threading.Event())
    mic.open()
    now = time.perf_counter()
    ring.write(np.zeros(BLOCK, dtype=np.float32), at=now + 10)
    assert mic.check(now + audio.POLL_DEFAULT_S + 0.1) is None  # same default
    monkeypatch.setattr(audio, "preferred_input_name", lambda: JABRA)
    assert "default" in mic.check(now + 2 * audio.POLL_DEFAULT_S + 0.2)


def test_closed_mic_has_nothing_to_check(sd):
    assert Mic(Ring(), threading.Event()).check(time.perf_counter()) is None


def test_preferred_input_name_reads_windows():
    name = audio.preferred_input_name()  # read-only winmm query; never opens the mic
    assert name is None or (isinstance(name, str) and len(name) <= 31)


def test_wav_feed_plays_files_in_real_time(tmp_path):
    path = tmp_path / "a.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.full(1600, 8192, dtype="<i2").tobytes())  # 0.1 s
    ring = Ring()
    feed = WavFeed(ring, threading.Event(), [path], lead_s=0.064, gap_s=0.064)
    began = time.perf_counter()
    feed.open()
    assert feed.done.wait(3)
    elapsed = time.perf_counter() - began
    assert feed.starts == [(path, 2 * BLOCK)]
    assert ring.end == 2 * BLOCK + 4 * BLOCK + 2 * BLOCK  # lead, audio padded to whole blocks, gap
    assert elapsed >= 0.2  # real time, not as fast as possible
    assert ring.read(2 * BLOCK, 2 * BLOCK + 4)[0] == pytest.approx(0.25)
    feed.close()
    assert not feed.is_open and feed.check(0.0) is None
