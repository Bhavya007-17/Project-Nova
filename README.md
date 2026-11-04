# Nova

Local voice layer for Windows: a Dynamic-Island bar, hand-speed commands, clean dictation, and handoff to the Claude / ChatGPT apps.

Being built stage by stage with Claude Code (`docs/prompts/README.md`). Done so far: Stage 00 — tray app and `nova doctor`; Stage 01 — ears (mic → VAD → Parakeet → text); Stage 02 — triggers (Copilot key hold / tap / double-tap, "Nova …", always-listening, follow-up window, state machine); Stage 03 — the island (Dynamic-Island bar at the top of the screen, live for every state); Stage 04 — Windows hands + Tier 0 (open / switch / close apps and sites, window moves and layouts, tabs, typing, keys, volume / media; no AI yet).

```
uv run nova              # tray app (no console: .venv\Scripts\novaw.exe); listens while On
uv run nova doctor       # checks this laptop
uv run nova listen       # live words (dim) and finals with timings; Ctrl+C prints p50/p95 + idle CPU
uv run nova listen --gate  # + Copilot key / chord / "Nova …" / always-listening: ACCEPTED or IGNORED per final
uv run nova keys --probe   # what the Copilot key sends (character keys hidden)
uv run nova island --demo  # every island state with fake data (add a scene name to loop one: confirm, pinned, chips…)
uv run nova bench-stt    # WER + decode time on the voicebar recordings (--live N: real-time replay)
uv run nova say "fusion on the left, claude on the right"   # the whole brain on typed text, real actions (--dry: plan only)
uv run nova latency      # p50 / p95 per command type from logs/traces.jsonl
uv run pytest            # unit tests
```
Island: Ctrl + Copilot pins it (a small status pill stays on screen; again to unpin). Drag the pill to move it; double-click = back to top-center.

Settings: `config/settings.yaml` (mic: `audio.device`; triggers; `wake_spellings`) · names to spell right: `config/vocab.yaml` · short names for apps and sites ("canvas" → https://canvas.vt.edu, "fusion" → Autodesk Fusion): `config/aliases.yaml` · per-utterance timings, gate verdicts and commands: `logs/traces.jsonl` · the app list Nova knows (re-read from the Start menu every 10 min): `data/catalog.json`

Design: `docs/SPEC.md` · Rules for Claude Code: `CLAUDE.md`
