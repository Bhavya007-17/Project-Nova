# Stage 01 — Ears: mic → VAD → Parakeet → text

**Read:** CLAUDE.md, SPEC §4, §16. Reference only: `C:\dev\voicebar\voicebar\stt\parakeet.py`, `voicebar\audio.py`.
**Goal:** Bhavya talks and sees accurate text within ~0.3 s of stopping, with almost no CPU used while it's quiet.

## Build
1. `nova/audio.py` — sounddevice InputStream (16 kHz mono float32, 512-sample blocks) → ring buffer; the callback only copies. Device from config; recovers from unplug / default-device change; closes on Off and on workstation lock (WTS session notifications), reopens on unlock.
2. `nova/vad.py` — Silero VAD (CPU, 1 thread) with the SPEC §4 start/endpoint rules and 300 ms pre-roll; emits `SpeechStarted` / `SpeechEnded(pcm)`; `end_now()` for key release.
3. `nova/stt/` — `Transcriber` interface + Parakeet via onnx-asr (int8, CPU, 4 intra-op threads) from `models/parakeet`, warm-up on load. Partials every ~400 ms on the same worker; skip a partial if one is still running; finals never wait behind partials.
4. `nova/vocab.py` — post-correction (phonetic key + rapidfuzz, strong matches only) against `config/vocab.yaml` + catalog names. Seed vocab: CroLabs, HIVE, ROS2, MuJoCo, Canvas, Fusion, Claude, ChatGPT, Notion, Todoist, Virginia Tech, plus commented slots for professor names and course codes.
5. `nova listen` — console: partials (dim) and finals with `stt_ms` and ms-from-speech-end.
6. `nova bench-stt` — WER over `fixtures/voicebar-recordings/` using the expected text in each session's `manifest.json` (normalize case/punctuation/numbers); p50/p95 `stt_ms`. If references can't be found, report timings only and say so.
7. JSONL trace per utterance.

## Constraints
Idle (silence) CPU < 2 % of one core. STT loaded once. No GPU.

## Verify yourself
Unit tests: endpoint logic on synthetic frames, vocab correction. Run `bench-stt` and put the numbers in the report.

## Live demo (Bhavya, 2 min)
`uv run nova listen` and talk normally: "open chrome and go to canvas", "put fusion on the left and claude on the right", one long rambling sentence. Words appear live; the final lands ≤ ~0.5 s after you stop.

Commit `stage 01: ears`. Report WER, latency p50/p95, idle CPU.
