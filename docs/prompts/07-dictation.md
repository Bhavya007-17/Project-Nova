# Stage 07 — Dictation

**Read:** CLAUDE.md, SPEC §11, §10 (Google Docs).
**Goal:** one sentence or ten minutes — clean text lands at the cursor sentence by sentence, ums gone, mis-hearings fixed. "Open a Google Doc and type this" works end to end.

## Build
1. `nova/dictation/session.py` — Dictating state; segments on VAD pauses ≥ 600 ms; ends on "stop dictation", a Copilot tap, or 10 s of silence; double-tap Copilot = locked dictation.
2. `nova/dictation/rules.py` — spoken commands, filler removal (conservative with "like" / "you know"), self-corrections, spacing/capitalization at joins using the text before the cursor (UIA TextPattern, or the extension in Docs).
3. `nova/dictation/cleanup.py` — light LLM pass (same small model; strict prompt per SPEC §11) + divergence guard; CPU fallback = rules only.
4. Insertion via clipboard borrow; confirm landing when possible; on failure save to history + island notice — never type twice.
5. Plans: "type this …" (rest of the utterance, then keep dictating), "open a Google Doc and type this" (docs.new → editor ready → dictation), "paste my last dictation".
6. History (last 50).

## Verify yourself
Unit tests for the rules and the guard on tricky cases; per-segment latency.

## Live demo (Bhavya, 3 min)
- Notepad: double-tap Copilot, say two paragraphs with some "um"s, a "scratch that" and a "new paragraph" → clean text lands per sentence.
- "Nova, open a Google Doc and type this: …" → a new doc opens and your words land, cleaned.

Commit `stage 07: dictation`. Report per-segment latency.
