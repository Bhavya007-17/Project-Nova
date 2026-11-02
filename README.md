# Nova

Local voice layer for Windows: a Dynamic-Island bar, hand-speed commands, clean dictation, and handoff to the Claude / ChatGPT apps.

Being built stage by stage with Claude Code (`docs/prompts/README.md`). Done so far: Stage 00 — tray app and `nova doctor`.

```
uv run nova          # tray app (no console: .venv\Scripts\novaw.exe)
uv run nova doctor   # checks this laptop
uv run pytest        # unit tests
```
Design: `docs/SPEC.md` · Rules for Claude Code: `CLAUDE.md`
