# Stage 10 — Safety, settings, startup, resilience

**Read:** CLAUDE.md, SPEC §14, §15, §6 (tray), §17.
**Goal:** safe and boring to live with every day.

## Build
1. Central `nova/risk.py`, audited against every action path, with tests (labels, URLs, app context: Gmail send, Canvas submit, Discord/Slack Enter-to-send, delete/purchase flows). Password-field guard on every input path (keys, paste, dictation, page_fill). Confirm timeout/cancel paths.
2. Settings panel (QML): triggers, wake name, always-listening, fallback chord, model/device, keep-alive, transcripts in logs, start with Windows, handoff targets → writes settings.yaml.
3. Start with Windows (Startup-folder shortcut to `pythonw -m nova`), single instance, crash watchdog (restart once, then an island error), clean shutdown (mic closed, hooks removed, LLM unloaded).
4. Governor polish: AC/battery detection, Fusion running + tight VRAM → CPU model, idle unload, thread priorities. `nova doctor --live` samples Nova's CPU/RAM/VRAM for 60 s.
5. Every island error says what happened + one next step, in plain words.

## Verify yourself
Risk tests green. A 30-min idle measurement (CPU, RAM, VRAM) in the report.

## Live demo (Bhavya, 3 min)
Reboot → Nova starts on its own. Try a send/submit command → the confirm appears; say "no" → nothing happens. Open Fusion → Nova is still snappy and Fusion is unaffected.

Commit `stage 10: safety + polish`. Report.
