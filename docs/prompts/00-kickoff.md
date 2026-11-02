# Stage 00 — Kickoff: repo, skeleton, doctor

**Read:** CLAUDE.md, docs/SPEC.md (all of it, once).
**Goal:** a real but empty app — tray icon, config, logging, event bus — and a `doctor` that checks this laptop for everything later stages need. No speech yet.

## Build
1. `git init`; `.gitignore` (models/, logs/, data/, fixtures/voicebar-recordings/, .venv/, __pycache__/, any extension private key).
2. uv project, `uv python pin 3.12`, package `nova`, entry point `nova` with subcommands: `run` (default), `doctor`, `listen`, `say "<text>"`, `eval`, `latency` (stubs that later stages fill in).
3. `nova/config.py` — load/validate `config/settings.yaml` (create defaults if missing), hot-reload on save with last-good fallback. Keep it small.
4. `nova/bus.py` — tiny thread-safe pub/sub; events are dataclasses.
5. `nova/log.py` — JSONL utterance traces (rotate 2 MB × 10) + a plain app log.
6. Tray app (PySide6): icon, menu (On/Off placeholder, Open settings → settings.yaml in the default editor, Open logs, Quit). Single instance (named mutex). Runs fine under `pythonw` (no console).
7. `nova doctor` — one table: Python/uv; input devices + default mic; NVIDIA free/total VRAM (NVML); Ollama reachable + models; Chrome path/version; Claude + ChatGPT desktop apps and Fusion present (AUMIDs in CLAUDE.md); fallback chord Ctrl+Alt+N free (RegisterHotKey probe); contents of `models/`. Never downloads anything.
8. Models are already in `models/` (parakeet int8 + silero, copied from voicebar) and Bhavya's recordings are in `fixtures/voicebar-recordings/`. Don't download or move them — `doctor` just checks they're present. List them in `docs/DEPS.md`.
9. Start `docs/DEPS.md` (one line per dependency + why).

## Constraints
No mic, hooks or network yet. Package layout per SPEC §17.

## Verify yourself
`uv run nova doctor` clean · `uv run pytest` (config + bus) green · tray starts and quits.

## Live demo (Bhavya, 1 min)
1. `uv run nova` → tray icon appears; the menu works; Quit exits.
2. `uv run nova doctor` → all green, or each red line says what to do.

Commit `stage 00: skeleton + doctor`. Report. Don't start Stage 01.
