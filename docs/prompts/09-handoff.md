# Stage 09 — Handoff to Claude / ChatGPT + done notifications

**Read:** CLAUDE.md, SPEC §13, §5 (Passthrough), §10, §14.
**Goal:** "put my homework for today on Claude", "ask ChatGPT …" and "on Claude, turn on voice" all work through the desktop apps, and a notification arrives when they finish.

## Build
1. `nova uia-dump <app>` first — print the UIA tree of the Claude and ChatGPT windows (names, control types, automation ids). Record which controls exist: new chat, composer, send, voice start/end, stop-generating, last message. Put the findings in the report.
2. `nova/handoff/claude_app.py`, `chatgpt_app.py` — launch/focus via AUMID (CLAUDE.md), new chat, compose (clipboard borrow), send, voice start/end, read the last response. UIA first, keyboard shortcuts second, web target third (claude.ai / chatgpt.com through the extension, same interface).
3. Prompt composition: his words + attachments (`canvas_today` → titles, due dates, description text; `page_text`; `selection`). Attachments → island confirm with a one-line preview before sending.
4. Voice handoff → Passthrough (SPEC §5): only "Nova …" and the Copilot key act; ends when the voice UI disappears or on a key tap (presses end-voice).
5. Watcher (SPEC §13): UIA polling, no AI, ≤ 1 % CPU; done → Windows toast (pick a toast library that works for an unpackaged Python app; register an AUMID / Start shortcut for Nova if needed) + chip ✓; clicking focuses the window. A second concurrent job → web target in its own tab.
6. Routing: big open-ended asks → Claude by default (SPEC §7.6). `--dry-run` shows the composed prompt without sending.

## Live demo (Bhavya, 4 min)
- "Nova, put my homework for today on Claude and ask it to explain the first problem" → new Claude chat with the assignment text + your ask (after the confirm), chip "working…", toast when done.
- "ask ChatGPT what a Carnot cycle is" → same in ChatGPT.
- "on Claude, turn on voice" → Claude's voice mode starts; talk to Claude; Nova ignores you; tap Copilot to end.

Commit `stage 09: handoff`. Report (include the uia-dump findings).

## Added 2026-11-11
- Agents by voice: "Nova, have Claude research X and tell me when it's done" starts the job, shows a chip and returns at once — also from inside a talk session (07b), which stays open.
- Done detection needs two checks in a row (stop control gone + last message unchanged for ~5 s) before the toast.
- Today handoff plans are only described, never run (`brain.py:223`, `router.py:199`), the schema has no `attach`, and Passthrough is never entered.
