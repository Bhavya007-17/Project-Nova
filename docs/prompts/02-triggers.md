# Stage 02 — Triggers and modes

**Read:** CLAUDE.md, SPEC §5.
**Goal:** every way of talking to Nova works, on a solid state machine: Copilot key (hold / tap / double-tap), "Nova …", always-listening commands, fallback chord, follow-up window, passthrough, off.

## Build
1. `nova keys --probe` first: a WH_KEYBOARD_LL hook that logs raw key events with timestamps while Bhavya taps and holds the Copilot key. Establish whether a real key-up arrives on release. Put the finding in the report.
2. `nova/keys.py` — the hook on its own thread with a message loop. Detect Copilot (LWin+LShift+F23), swallow it so Copilot doesn't open, mask LWin so Start doesn't open. Hold > 250 ms = push-to-talk; tap = listen until endpoint; tap while listening = finish; double-tap = locked dictation (emit the event; Stage 07 uses it); Esc cancels. Same semantics for the fallback chord (default Ctrl+Alt+N). Callbacks return in < 10 ms; detect if Windows drops the hook and reinstall it.
3. `nova/state.py` — pure state machine per SPEC §5, unit-tested; publishes transitions on the bus.
4. `nova/gate.py` — may this final act? Key session / wake name ("Nova" + fuzzy spellings from config; strip it) / follow-up window (8 s) / always-listening gate (command-verb start + resolvable — use a stub resolver for now: verb + known app/site name) / passthrough / sleep.
5. Detectors feeding the gate: mic in use by another app (ConsentStore `LastUsedTimeStop == 0`, excluding Nova); speakers playing (render-endpoint peak meter).
6. `PreloadHint` event on key down / wake name / speech start (Stage 05 warms the LLM with it).
7. Tray toggles: On/Off, Always-listening.

## Verify yourself
Unit tests for the state machine and the gate, including negatives: "I'm gonna open a restaurant someday", "did you close the door", "we should switch to the other lab" → ignored; "Nova is a cool name" → wakes only because it starts with Nova (then it asks what he wants).

## Live demo (Bhavya, 3 min) — `uv run nova listen --gate`
1. Hold the Copilot key, talk, release → ACCEPTED.
2. Tap, talk, pause → ACCEPTED when you stop.
3. "Nova, open Chrome" → ACCEPTED with the name stripped.
4. Always-listening on: "open spotify" → ACCEPTED; normal chatter → IGNORED.
5. Copilot and the Start menu never pop up.

Commit `stage 02: triggers`. Report (include the key-up finding).
