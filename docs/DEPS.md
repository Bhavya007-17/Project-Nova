# Dependencies

One line each: what, version, why. Ask before adding anything over ~50 MB or a new runtime.

## Runtime (Python 3.12 via uv)
- **pyside6-essentials** 6.12 (~78 MB wheel) — tray now, QML island in Stage 03 (SPEC §6). "Essentials" skips the Addons wheel (WebEngine, 3D…) Nova doesn't use.
- **pyyaml** 6.0 — reads `config/*.yaml`; safe loader only, duplicate keys rejected.
- **sounddevice** 0.5 — PortAudio mic capture (MME Sound Mapper, raw float32 callback into a ring; measured cheaper than WASAPI auto-convert here).
- **nvidia-ml-py** 13.615 (`pynvml`) — free/total VRAM via NVML, so the LLM can pick GPU or CPU (SPEC §7.4, §15).
- **numpy** 2.5 (~12 MB wheel) — audio buffers; needed by onnx-asr and ONNX Runtime anyway.
- **onnxruntime** 1.31 (CPU, ~15 MB wheel) — runs Silero VAD (1 thread) and Parakeet (4 threads, spinning off). Telemetry events disabled at load.
- **onnx-asr** 0.12 (~4 MB, pure Python) — Parakeet TDT loader/decoder for the local int8 files; no hub extra, so it can't download.
- **rapidfuzz** 3.14 (~2 MB) — fuzzy spelling for the vocab fix-up; word-level Levenshtein for WER.
- **jellyfish** 1.3 (~0.2 MB) — metaphone sound keys for the vocab fix-up ("crow labs" ≈ "CroLabs").

## Dev
- **pytest** 9.2 — unit tests for pure logic.
- **pytest-cov** 7.1 — coverage (80 % target; GUI shell omitted, checked by live demo).

## Models and data (in the repo folder, git-ignored, never downloaded by Nova)
- `models/parakeet/` — Parakeet TDT 0.6B v3, int8 ONNX for onnx-asr (`nemo-parakeet-tdt-0.6b-v3`, ~639 MB), copied from voicebar. STT (SPEC §4).
- `models/silero/silero_vad.onnx` — Silero VAD (~2.2 MB), copied from voicebar. Speech start/end.
- `fixtures/voicebar-recordings/` — Bhavya's recordings (4 sessions with `manifest.json`, 134 audio files). STT WER eval. Only the "check" session is clean: in "quiet" and "noise" the old recorder started late, so most files begin mid-word (`nova bench-stt` flags them).
- `fixtures/voicebar-reference/` — voicebar phrasing fixtures (committed).

## Outside Python (already on this laptop)
- **uv** ≥ 0.12 recommended — 0.11 makes venvs whose `pythonw.exe` is a console exe (uv #19226); `nova doctor` checks this. The first `uv` on PATH here is a pip install (Python 3.11 Scripts); a second 0.11.19 sits in `~/.local/bin`.
- **Ollama** 0.30 — local LLM runtime (Stage 05). Only local tags are allowed; `*-cloud` tags never.
