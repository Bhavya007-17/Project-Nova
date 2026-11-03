# Nova

Local voice layer for Windows: a Dynamic-Island bar, hand-speed commands, clean dictation, and handoff to the Claude / ChatGPT apps.

Being built stage by stage with Claude Code (`docs/prompts/README.md`). Done so far: Stage 00 — tray app and `nova doctor`; Stage 01 — ears (mic → VAD → Parakeet → text); Stage 02 — triggers (Copilot key hold / tap / double-tap, "Nova …", always-listening, follow-up window, state machine); Stage 03 — the island (Dynamic-Island bar at the top of the screen, live for every state).

```
uv run nova              # tray app (no console: .venv\Scripts\novaw.exe); listens while On
uv run nova doctor       # checks this laptop
uv run nova listen       # live words (dim) and finals with timings; Ctrl+C prints p50/p95 + idle CPU
uv run nova listen --gate  # + Copilot key / chord / "Nova …" / always-listening: ACCEPTED or IGNORED per final
uv run nova keys --probe   # what the Copilot key sends (character keys hidden)
uv run nova island --demo  # every island state with fake data (add a scene name to loop one: confirm, pinned, chips…)
uv run nova bench-stt    # WER + decode time on the voicebar recordings (--live N: real-time replay)
uv run pytest            # unit tests
```
Island: Ctrl + Copilot pins it (a small status pill stays on screen; again to unpin). Drag the pill to move it; double-click = back to top-center.

Settings: `config/settings.yaml` (mic: `audio.device`; triggers; `wake_spellings`) · names to spell right: `config/vocab.yaml` · per-utterance timings and gate verdicts: `logs/traces.jsonl`

Design: `docs/SPEC.md` · Rules for Claude Code: `CLAUDE.md`
