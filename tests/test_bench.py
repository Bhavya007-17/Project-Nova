import json

import numpy as np
import pytest

from nova import paths
from nova.stt.bench import find_items, segment


def test_find_items_reads_expected_text_and_selected_takes(tmp_path):
    session = tmp_path / "20261010T165301Z-check-0ffa4a3f"
    session.mkdir()
    (session / "p001-take-a.wav").write_bytes(b"RIFF")
    (session / "p001-take-old.wav").write_bytes(b"RIFF")  # a retry take: not scored
    manifest = {
        "session_label": "check",
        "prompts": [
            {"id": "p001", "expected_text": "open Google Chrome", "selected_wav": "p001-take-a.wav"},
            {"id": "p002", "expected_text": "open Claude", "selected_wav": "missing.wav"},
            {"id": "p003", "expected_text": "go back"},  # never recorded
        ],
    }
    (session / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    wake = tmp_path / "stage4-wake-x"
    wake.mkdir()
    (wake / "wake-001.wav").write_bytes(b"RIFF")
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "manifest.json").write_text("{nope", encoding="utf-8")
    items, others = find_items(tmp_path)
    assert [(i.label, i.prompt, i.expected, i.wav.name) for i in items] == [("check", "p001", "open Google Chrome", "p001-take-a.wav")]
    assert others == [wake / "wake-001.wav"]


SILERO = paths.MODELS / "silero" / "silero_vad.onnx"
RECORDINGS = sorted(paths.RECORDINGS.glob("*check*/manifest.json"))


@pytest.mark.skipif(not SILERO.is_file() or not RECORDINGS, reason="needs the local Silero model and recordings")
def test_segment_finds_the_spoken_command():
    from nova.audio import read_wav
    from nova.vad import Silero

    manifest = json.loads(RECORDINGS[0].read_text(encoding="utf-8"))
    wav = RECORDINGS[0].parent / manifest["prompts"][0]["selected_wav"]
    audio = read_wav(wav)
    segments, first = segment(audio, Silero(SILERO))
    assert len(segments) == 1
    assert first is not None and 0.25 < first < len(audio) / 16000
    assert 0.6 < len(segments[0]) / 16000 < len(audio) / 16000 + 0.2


@pytest.mark.skipif(not SILERO.is_file(), reason="needs the local Silero model")
def test_segment_ignores_silence():
    from nova.vad import Silero

    quiet = (np.random.default_rng(0).standard_normal(16000 * 2) * 1e-3).astype(np.float32)
    assert segment(quiet, Silero(SILERO)) == ([], None)
