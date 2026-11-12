# Stage 07b — Talk sessions

**Read:** CLAUDE.md, SPEC §5 (talk sessions), §6, §7, §14, §16. Needs 07a.
**Goal:** open the mic once and talk the way you'd talk to a person. Each thing he asks for happens while he keeps talking; small talk is ignored.

The reference is a 48-second screen recording of Atticus: "Alright, can you open up the notes app for me… and once you're there can you create a new note… and inside this new note let's make the title say hello. Great, okay. Let's move on and can you open up the browser… google search Norbert Wiener… now open x.com. Nice. Now open the photo booth and let's take a picture of me. Cool, thank you." Eight actions, each 1–2 s after its clause, no key or name in between.

## Build
1. **The talk session.** A pure, unit-tested part of the state machine. `state.py` already has a class named `Session` (one listening turn) — call the new thing something else (`Talk`).
   - **This is the hard part of the stage.** Today the mic is attentive only in Listening / Dictating (`state.py:266`), and `_done` / `_progress` end the turn. A talk session keeps hearing and queueing while Nova thinks and acts, so listening and acting become two things that run side by side, not one more value in the `State` enum. Plan `state.py` and the controller in `triggers.py` first; keep every transition a bus event.
   - Opens on a Copilot tap, on "Nova" alone, and after any "Nova, <command>" (instead of the 8 s follow-up, which stays for commands that ran outside a session). Hold-to-talk stays one command, no session.
   - Closes on a closing phrase ("that's all", "thanks, that's all", "stop listening" — a Tier 0 action with synonyms, like any other), a Copilot tap, Esc, or 30 s without an accepted command (`session.idle_s`; the pill shrinks over the last 5 s). Ignored speech doesn't reset the clock.
   - Inside it nothing needs the name and every action class is allowed as if keyed. While the speakers play or another app holds the mic, only the safe actions run without the name (SPEC §5). Risky actions still confirm, with 07a's fresh-answer rule.
2. **Clauses.** Only final text executes; partials stay display-only. Every short pause (~300 ms) ends a piece of speech; split that piece at its continuations ("and", "then", "now", "once you're there") into ordered clauses. A piece may run long in a session — keep the 30 s cap.
3. **Each clause, in this order:**
   - Strip leading discourse words and polite frames ("alright", "okay", "um", "now", "and", "let's move on and", "once you're there", "can you", "for me"). A closed class of filler, not a list of commands.
   - Nothing left, or only an acknowledgement ("great", "nice, okay", "thank you") → drop it. No LLM, nothing in the island.
   - Tier 0. It never guesses a verb from a non-command (today "Great great. Okay." → "Type 'great. Okay'?").
   - Tier 1. `ignore` → dropped silently. A low-confidence plan becomes a Clarify only if the clause was worded as a request (verb-first or a polite frame); otherwise dropped silently.
4. **Context.** The router carries the last ≤ 5 executed steps with today's `{ok, detail}` results plus what is in front now, so "there", "it", "this new note" resolve. Tier 1 sees it as one compact field (count the tokens; 07a's budget holds). No referent → clarify. 07c's receipts make this richer later.
5. **Order.** One brain thread, clauses run in order. One that builds on the last waits for that step's verification. A step that fails, or a Confirm he declines or lets time out, stops the queue and says where. "stop" / "cancel" / "wait" clear the queue and abort the running action. The key or the name still interrupts, as today.
6. **Dictation.** "type this…" inside a session switches to Dictating and pauses the idle clock. Dictation ends the usual way; a Copilot tap there ends the dictation, not the session. Then the session resumes with a fresh 30 s.
7. **Warm for the whole session.** Preload on open, keep the model resident until it closes.
8. **Island.** The session pill: waveform + rolling live words + a stop button, the step line under it while acting ("Notepad → new note"). Gone when the session closes.
9. **Eval.** `docs/eval/sessions.yaml`: scripted sessions with the expected action sequence and the lines that must do nothing. Case 1 is the recording above on Windows apps, without the camera (07c adds it). `nova eval --sessions` feeds them clause by clause; a TTS replay through the live path gives timings. Targets: ≥ 90 % of clauses right, 0 actions on small talk, end of clause → action start p50 ≤ 0.8 s (Tier 0) / ≤ 1.5 s (Tier 1) with nothing queued ahead; queue wait reported separately.

**Left for later, on purpose:** acting on stable partial words before he pauses, and a learned end-of-turn check (Smart Turn). Both are Stage 11 candidates — they need word timestamps and real session audio to measure. If run-on speech with no pauses feels slow in the demo, say so in the report.

**Known risk:** a session hears everyone in the room and anything the speakers play. The explicit open, the 30 s close, the visible pill, safe-only while audio plays and confirm-before-risky are the protection. Echo cancellation is a later option.

## Verify yourself
Unit tests for the state machine (open / close / idle / queue / interrupt / dictation hand-over) and the clause splitter. `nova eval --sessions`. The TTS replay of case 1 with per-clause timings in the report.

## Live demo (Bhavya, 3 min)
Tap Copilot, then in one go, in your own words: open Notepad → start a new note → type hello → "great, okay" → open Chrome → google something → open x.com → "nice" → "that's all". Then open a second session, stay quiet for 30 s and watch it close.

Commit `stage 07b: talk sessions`. Report.
