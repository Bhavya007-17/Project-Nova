# Nova

Local voice layer for Windows: a Dynamic-Island bar, hand-speed commands, clean dictation, and handoff to the Claude / ChatGPT apps.

Being built stage by stage with Claude Code (`docs/prompts/README.md`). Done so far: Stage 00 — tray app and `nova doctor`; Stage 01 — ears (mic → VAD → Parakeet → text).

```
uv run nova              # tray app (no console: .venv\Scripts\novaw.exe); listens while On
uv run nova doctor       # checks this laptop
uv run nova listen       # live words (dim) and finals with timings; Ctrl+C prints p50/p95 + idle CPU
uv run nova bench-stt    # WER + decode time on the voicebar recordings (--live N: real-time replay)
uv run pytest            # unit tests
```
Settings: `config/settings.yaml` (mic: `audio.device`) · names to spell right: `config/vocab.yaml` · per-utterance timings: `logs/traces.jsonl`

Design: `docs/SPEC.md` · Rules for Claude Code: `CLAUDE.md`
