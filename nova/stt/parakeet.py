"""Parakeet TDT 0.6B v3 (int8 ONNX) via onnx-asr on the CPU (SPEC §4).

Measured on this laptop (40 clean recordings, ~2.3 s each): ~170 ms p50 / ~250 ms p95
per decode with 4 threads. ONNX Runtime's default thread spinning made it ~25 ms
faster but burned ~3x the CPU, so spinning is off. Loading takes ~6-8 s (parsing the
650 MB encoder), so the model is loaded once per process.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import numpy as np

_log = logging.getLogger(__name__)

MODEL = "nemo-parakeet-tdt-0.6b-v3"
FILES = ("encoder-model.int8.onnx", "decoder_joint-model.int8.onnx", "vocab.txt", "config.json")
SAMPLE_RATE = 16_000
_MIN_SAMPLES = SAMPLE_RATE // 10  # shorter input is padded with silence
# The int8 model sometimes returns nothing for clear speech: the encoder output sits on a knife
# edge and the decoder predicts only blanks (~1 % of clean utterances, far more on the old clipped
# recordings). The same audio with 0.3 s of silence on each side almost always decodes, so an
# empty result for anything longer than half a second gets one padded retry.
_RETRY_MIN = SAMPLE_RATE // 2
_RETRY_PAD = np.zeros(int(0.3 * SAMPLE_RATE), dtype=np.float32)


class ParakeetTranscriber:
    """Thread-safe: ONNX Runtime sessions allow concurrent runs and onnx-asr keeps decode state per call."""

    def __init__(self, directory: Path, threads: int = 4) -> None:
        directory = Path(directory)
        missing = [name for name in FILES if not (directory / name).is_file()]
        if missing:
            raise FileNotFoundError(f"Parakeet model files missing in {directory}: {', '.join(missing)}")
        import onnxruntime as ort

        ort.disable_telemetry_events()
        import onnx_asr

        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        began = time.perf_counter()
        self._model = onnx_asr.load_model(
            MODEL, directory, quantization="int8", providers=["CPUExecutionProvider"], sess_options=options
        )
        self.load_ms = (time.perf_counter() - began) * 1000
        self.name = f"{MODEL} int8 cpu x{threads}"
        _log.info("loaded %s in %.1f s", self.name, self.load_ms / 1000)

    def warm_up(self, runs: int = 3) -> None:
        """The first few decodes are slow (kernel set-up); do them before anyone talks."""
        rng = np.random.default_rng(0)
        for i in range(runs):
            self.transcribe((rng.standard_normal(SAMPLE_RATE * (1 + i % 2)) * 0.003).astype(np.float32))

    def transcribe(self, pcm: np.ndarray) -> str:
        audio = np.asarray(pcm, dtype=np.float32)
        if len(audio) < _MIN_SAMPLES:
            audio = np.pad(audio, (0, _MIN_SAMPLES - len(audio)))
        text = self._decode(audio)
        if not text and len(audio) >= _RETRY_MIN:
            text = self._decode(np.concatenate((_RETRY_PAD, audio, _RETRY_PAD)))
            if text:
                _log.info("empty decode recovered by the padded retry (%.1f s of audio)", len(audio) / SAMPLE_RATE)
        return text

    def _decode(self, audio: np.ndarray) -> str:
        return str(self._model.recognize(audio, sample_rate=SAMPLE_RATE)).strip()


def load(models: Path, threads: int = 4) -> ParakeetTranscriber:
    stt = ParakeetTranscriber(Path(models) / "parakeet", threads)
    stt.warm_up()
    return stt
