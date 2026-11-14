"""Parakeet's empty-output retry (no model file needed: the decode is faked)."""

import numpy as np

from nova.stt.parakeet import SAMPLE_RATE, ParakeetTranscriber


def _empty_decoder():
    stt = object.__new__(ParakeetTranscriber)
    decoded: list[int] = []
    stt._decode = lambda audio: decoded.append(len(audio)) or ""
    return stt, decoded


def test_an_empty_decode_gets_one_padded_retry():
    stt, decoded = _empty_decoder()
    assert stt.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32)) == ""
    assert len(decoded) == 2 and decoded[1] > decoded[0]


def test_no_retry_when_the_caller_only_wants_live_words():
    stt, decoded = _empty_decoder()
    assert stt.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), retry_empty=False) == ""
    assert len(decoded) == 1
