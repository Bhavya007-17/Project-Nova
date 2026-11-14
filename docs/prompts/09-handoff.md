# Stage 09 — Handoff to Claude / ChatGPT + done notifications

**Read:** CLAUDE.md, SPEC §13, §5 (Passthrough, talk sessions), §7.6, §10, §14, §16; GOTCHAS "Windows input and focus". Needs 07c (the UIA bindings, receipts).
**Goal:** "put my homework for today on Claude", "ask ChatGPT …", "have Claude research X and tell me when it's done" and "on Claude, turn on voice" all work through the desktop apps, and a notice arrives when they finish.

What is already there (read from the code on 2026-11-13, before 07b / 07c landed — find things by name):
- Tier 1 already answers `handoff{to, mode, prompt}` and `validate._handoff` keeps `prompt` to his own words. Then nothing happens: `brain.handle` shows `describe_handoff(plan)` and `Executor.run` refuses plans that aren't `act`. The schema has no `attach`.
- Tier 0 has no handoff patterns, so even "ask ChatGPT what a Carnot cycle is" costs an LLM call.
- `State.PASSTHROUGH` and its island face exist; the gate ignores speech without the name in it; a Copilot tap leaves it and publishes `PassthroughStopped`. Nothing enters it and nothing listens to that event. A "Nova, …" command said during Passthrough runs, but its result never shows (`_done` ignores it there).
- `JobChip` and the chips work; a chip click publishes `IslandAction("chip", chip=id)` and nothing listens.
- `Chrome.last_text` keeps the last page / assignment text read. `canvas_item_text` and `page_read{what: page | selection}` fetch it.
- `risk.messaging()` leaves Claude and ChatGPT out, so Enter there doesn't ask "Send it?" — right for his own words.
- `detect.AudioDetector.recording_apps()` names every other process with a live mic session.
- `apps.open_app` launches and focuses by AUMID (both AUMIDs are in CLAUDE.md; `nova doctor` finds them). `input.type_text` pastes through the clipboard borrow, puts his clipboard back after ~300 ms, and stops if another window came forward.

## Probe first — before any design
`nova uia-dump <app> [--depth N] [--find text]` on 07c's bindings: the tree of the Claude and ChatGPT windows (name, control type, automation id, patterns, rectangle). Both are probably Chromium inside: expect the first question to return a bare pane (GOTCHAS, "Chrome's first UIA question lies") — ask again after 150–500 ms and note how long the tree takes to appear. For each app find: new chat, composer, send, stop-generating, voice start / end, the last message, and what the signed-out screen looks like. Then answer, with what you measured:
- a. Can each control be found by something stable (automation id, or role + name)?
- b. Does the tree keep updating while the window is behind others? Minimized? On another virtual desktop?
- c. Does Invoke work on new chat / voice / send, or only a real click or a shortcut? Which shortcuts does each app list in its own menus or help (read them there, not from memory)?
- d. When voice mode starts, which process shows up in `recording_apps()`, and how soon after voice ends is it gone?
- e. Does the app's CPU stay higher once its tree has been read? Measure it with and without a 1.5 s poll.
The answers set the driver order for each app: UIA → shortcuts → the website. Put the table in the report and the surprises in GOTCHAS. If neither app shows a usable tree, the website becomes the default and the stage still ships — say so on day one.

## Build
1. **One driver, three targets.** `open(new_chat)`, `compose(text)`, `send()`, `busy() -> True | False | None` (None = can't see), `last_message()`, `voice(on)`, `focus()`. The two desktop apps are one class + a table of control descriptors from the probe (with the date they were checked), not two code paths. The third is claude.ai / chatgpt.com through the extension: the page map and `fill` already handle contenteditable composers. When a control isn't found the error names it ("Can't find Claude's new-chat button — its window may have changed; using the website") and the next target is tried.
2. **Schema and routing.**
   - `handoff.attach: [canvas_today | page_text | selection]` in `prompt.SCHEMA`, `validate._handoff` (checked against the enum; `prompt` stays a span of his words), the Tier 1 rules + two examples, and the eval notation (`handoff claude +canvas_today`). Count the prompt's tokens before and after: 07a's budget holds.
   - Tier 0 patterns for explicit handoff words (SPEC §7.6), no LLM: "ask / tell / have <Claude | ChatGPT> …", "on <Claude>, …", "put … on <Claude>", "<Claude> voice". The names go through the catalog's aliases and sound-alikes ("Claire", "cloud") and are asked, not guessed, the way Tier 0 treats app names today. "open Claude" stays `open_app`. Anything that needs judgement about what to attach goes to Tier 1.
3. **Compose.** His words first, then each attachment under a plain heading (title, due date, the text). Cap attachment text at ~20,000 characters, cut at a paragraph, and say it was cut.
   - Attachments are fetched before any window moves: `canvas_today` → `canvas_items` + `canvas_text` (one item → use it; several → the island's numbered pick; none → say so and stop); `page_text` → the page read; `selection` → the selection in Chrome (say plainly if it only works in Chrome).
   - Content he didn't speak → Confirm with a one-line preview: "Send HW 5 – Thermo (1,240 words) to Claude with “explain the first problem”?". His words alone → no Confirm (SPEC §13). This rule goes in `risk.py` as `assess_handoff`, with tests, next to the others.
   - Page and Canvas text is data: it is pasted to Claude, and Nova never reads it for instructions (SPEC §14). Keep it out of the logs: `TRANSCRIPT_FIELDS` strips `prompt` — put attachment text under a stripped key too.
4. **Send — every step checked, nothing sent blind.**
   1. Remember the window he is in.
   2. Launch or focus by AUMID. Signed-out screen → stop: "Claude wants you to sign in".
   3. New chat (unless he said "in this chat").
   4. Focus the composer and **prove it is the composer** (the probe's descriptor) — a sign-in box takes text too.
   5. Paste through `input.type_text` so the password, focus and foreground guards stay.
   6. **Check the text landed** (composer length ≈ what was pasted). The clipboard borrow gives his clipboard back after ~300 ms, and a long paste into an app that just launched can lose that race: wait for the landing before the restore.
   7. Send.
   8. **Check it left** (composer empty or `busy()` true within 2 s).
   A failed check stops there, leaves the text where it is and says what to do. Sending can't be undone; everything before it can.
   - Budget: app already open → sent ≤ 3 s (SPEC §16). Trace each hop (`attach_ms`, `focus_ms`, `paste_ms`, `send_ms`); `nova latency` gets a "handoff sent" row.
   - Where he ends up: a direct ask ("ask ChatGPT …") leaves that app in front. A background job ("have Claude … and tell me when it's done", or any handoff from inside a talk session) returns to the window he was in, and the session stays open. If Bhavya wants it otherwise, he'll say so at the plan.
5. **Jobs and the watcher.** `nova/handoff/jobs.py` is pure (state per job: working / can't see / done / failed / gave up); a watcher thread at below-normal priority polls `busy()` every 1.5 s. Chip text: the target + the attachment's title or his first few words.
   - **Done needs positive proof.** "The stop button is gone" is also what a minimized window, a restarted app or a redesigned UI looks like. Done = an anchor that proves the tree is readable (the composer) is found, **and** `busy()` is False twice in a row, **and** the last message has not changed for ~5 s and isn't empty. Anchor missing → "can't see" (the chip shows it), keep trying, never say done.
   - Gives up after 30 min. App closed → failed. Nova quitting drops its jobs; don't persist them.
   - A second job while the app is already being watched → the website in its own tab (`tabs.open`, not active). Poll it from Nova with `page.call`; a timer inside a background tab is throttled.
   - Cost: the watcher ≤ 1 % of a core by cycle counts (`nova/cpu.py`), and the watched app's own CPU with the watcher on and off (probe e).
6. **The done notice.** The chip turns ✓ — always. Plus a Windows toast: "Claude finished — <first line>".
   - Try Qt's `QSystemTrayIcon.showMessage` first: `tray.py` already uses it and it adds nothing. Check on the laptop: does the toast say "Nova" (not "Python"), does a click reach `messageClicked`, what does Do Not Disturb do to it?
   - If the name or the click is wrong: give the process its own AppUserModelID at start and register it for the user (read Microsoft's current page on toasts from unpackaged apps), then raise a real toast with an in-process click handler — 07c's `winrt-*` packages cover that namespace.
   - A toast click arrives on a foreign thread: hop to the Qt thread by signal, never touch QML from it. A chip click and a toast click do the same thing: focus that job's window or tab, then remove the chip.
7. **Voice.** "on Claude, turn on voice" → focus / launch → new chat → the voice button → wait until that app's process holds a live mic session (probe d; ≤ 3 s, else "Voice didn't start") → `BrainProgress("passthrough", "Claude voice")`.
   - **The mic session is the truth; the button is only how Nova presses it.** Leaving: the app's mic session ends (the watcher polls every 1 s with its own `AudioDetector` — that class is single-thread COM) → `BrainProgress("idle", "voice mode closed")`. Or he taps Copilot: the state machine already leaves and publishes `PassthroughStopped` — subscribe and press end-voice.
   - In Passthrough the gate already wants the name or the key. Two things to add: a "Nova, …" command that runs there shows its result, and what he says to Claude isn't Nova's to keep — finals the gate ignores in Passthrough are logged without their text.
   - From a talk session: the session pauses, as it does for dictation, and resumes when voice ends.
8. **Wire it.** `brain.handle`'s handoff branch → resolve attachments → Confirm → send → job. `nova say "…" --dry` prints the target and the composed prompt and sends nothing. While the workstation is locked nothing is sent.

## Verify yourself
Unit tests, written first: the jobs state machine (the done rule, "can't see", give-up), compose (order, cap, headings), `assess_handoff`, the Tier 0 patterns, attach validation, Passthrough in / out. A fake target for the send sequence: fail each check in turn → nothing is sent. `nova eval` ≥ 90 % with the new rows, negatives not higher. Focus-free: `uia-dump`, `--dry`. Then ask once and run real sends with a harmless prompt ("say hello") to both apps and one website target, and a 10-minute watcher CPU measurement.

## Live demo (Bhavya, 5 min)
1. "Nova, put my homework for today on Claude and ask it to explain the first problem" → the Confirm with the preview → a new Claude chat with the assignment text + his ask, chip "working…", toast when done; click it → Claude comes forward.
2. "ask ChatGPT what a Carnot cycle is" → the same in ChatGPT, no Confirm.
3. In a talk session: "have Claude research <something> and tell me when it's done" → he is back in his window at once and keeps talking; a second job goes to a browser tab; both chips finish.
4. "on Claude, turn on voice" → talk to Claude, Nova ignores him; "Nova, volume down" still works; tap Copilot to end.

## If usage runs short
Stop after any of these with its demo line working: (1) the probe + text handoff to Claude; (2) attachments + Confirm; (3) jobs, watcher, chip, toast; (4) ChatGPT; (5) voice + Passthrough; (6) the website target + a second job.

Commit as you go; close with `stage 09: handoff`. Report (include the probe table).
