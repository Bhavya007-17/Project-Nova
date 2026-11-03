import wave

import numpy as np
import pytest

from nova.audio import Ring, mme_match, read_wav


def test_ring_reads_back_by_absolute_position():
    ring = Ring(capacity=8)
    ring.write(np.arange(5, dtype=np.float32), at=1.0)
    assert ring.end == 5
    np.testing.assert_array_equal(ring.read(1, 4), [1, 2, 3])
    assert ring.arrival == (5, 1.0)


def test_ring_wraps_around():
    ring = Ring(capacity=8)
    ring.write(np.arange(6, dtype=np.float32), at=1.0)
    ring.write(np.arange(6, 12, dtype=np.float32), at=2.0)
    np.testing.assert_array_equal(ring.read(5, 12), [5, 6, 7, 8, 9, 10, 11])


def test_ring_refuses_overwritten_or_future_samples():
    ring = Ring(capacity=8)
    ring.write(np.arange(12, dtype=np.float32), at=1.0)
    assert ring.read(3, 6) is None  # overwritten
    assert ring.read(10, 13) is None  # not written yet
    assert ring.oldest == 4


def test_ring_read_is_a_copy():
    ring = Ring(capacity=8)
    ring.write(np.ones(4, dtype=np.float32), at=1.0)
    block = ring.read(0, 4)
    block[:] = 7
    np.testing.assert_array_equal(ring.read(0, 4), [1, 1, 1, 1])


def test_ring_accepts_raw_bytes():
    ring = Ring(capacity=8)
    ring.write_bytes(np.array([0.5, -0.5], dtype=np.float32).tobytes(), at=3.0)
    np.testing.assert_array_equal(ring.read(0, 2), [0.5, -0.5])


def test_mme_match_prefers_name_substring():
    names = {0: "Microsoft Sound Mapper - Input", 1: "Microphone Array (Intel® Smart ", 7: "Headset Microphone (Jabra Evol"}
    full = ["Microphone Array (Intel® Smart Sound Technology for Digital Microphones)", "Headset Microphone (Jabra Evolve2 65)"]
    assert mme_match("jabra", names, full) == 7
    assert mme_match("Digital Microphones", names, full) == 1  # only in the full (WASAPI) name
    assert mme_match("usb", names, full) is None
    assert mme_match("mapper", names, full) is None  # the mapper is "default", never a match


def test_read_wav(tmp_path):
    path = tmp_path / "a.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.array([0, 16384, -32768], dtype="<i2").tobytes())
    np.testing.assert_allclose(read_wav(path), [0.0, 0.5, -1.0])


def test_read_wav_rejects_other_formats(tmp_path):
    path = tmp_path / "b.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\0\0\0\0")
    with pytest.raises(ValueError, match="16 kHz mono"):
        read_wav(path)


def test_ring_wakes_the_reader_only_when_it_has_enough():
    import threading

    wake = threading.Event()
    ring = Ring(capacity=64, wake=wake)
    ring.wake_at = 8
    ring.write(np.zeros(4, dtype=np.float32), at=0.0)
    assert not wake.is_set()
    ring.write(np.zeros(4, dtype=np.float32), at=0.0)
    assert wake.is_set()
