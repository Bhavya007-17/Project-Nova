# Stage 09a — Talk back (Nova speaks, and stops when told)

**Read:** CLAUDE.md, SPEC §4, §5, §6, §13, §14, §16; GOTCHAS "Speech", "Triggers and gate" and "Island". Needs 09 (the answer card and the done notices are the main things it says). 07d's guide captions are spoken too if 07d is built.
**Goal:** a voice mode the user turns on and off. When it is on, Nova says its answers, its questions and its done notices out loud. Any word, tap or click stops it at once. When it is off, Nova is silent, as it is today.

Added 2026-11-22 after a look at Nūs Companion. Its `voice/lines.js` pre-renders short acks ("On it.") so they play instantly; Kokoro, an 82M-parameter ONNX voice, does the rest. Nova starts with the voice Windows already has and keeps Kokoro as an option.

What is already there (read from the 07c branch on 2026-11-22; find things by name):
- `nova/tts.py`: text → a WAV through SAPI in a PowerShell child. It is for the eval replay only: it costs ~1 s to start PowerShell, so it is too slow for live speech.
- `sounddevice` (PortAudio) is already a dependency, used for the mic. It can also play.
- The winrt packages for OCR (07c) are already installed, `winrt-windows-storage-streams` among them.
- `actions/system.py` reads the **endpoint** peak meter ("are the speakers playing"). The gate uses it: while the speakers play, only media / volume commands pass without the name (SPEC §5). **Nova's own voice will trip it** — see Decided 5.
- The island already has a Done line and (after 09) an answer card and done notices.

## Decided — build it this way
1. **Off by default.** It is turned on and off by:
   - **voice:** "talk back on / off", "speak to me", "stop talking to me" (the setting) vs "stop" / "quiet" (this one sentence);
   - **the tray menu**, and **settings** (`speech_out: off | answers | everything`).

   Not to be confused with "on Claude, turn on voice" (Claude's own voice mode, Passthrough, §13). A bare "turn on voice" is asked: "Claude's voice mode, or should I talk back?"
2. **What it says, at `answers`:**
   - answers (the 09 answer card);
   - questions it asks (Clarify, Confirm, "Show me?");
   - errors that need the user;
   - job-done notices ("Claude's done");
   - guide captions (07d).

   Plain "Done" lines are silent at `answers` and spoken at `everything`.
   - **Long answers:** the first ~2 sentences or 40 words, then "the rest is on screen". Markdown, URLs and code are stripped and never read out.
   - **Short fixed lines** ("On it", "Sent", "Done", "Stopped", "Say that again?") are rendered once at start-up and played from memory.
3. **The engine: Windows' own WinRT voice.**
   - **How:** `Windows.Media.SpeechSynthesis.SpeechSynthesizer` (the `winrt-windows-media-speechsynthesis` package; check that it exists at the same version as the OCR ones). `SynthesizeTextToStreamAsync` → the WAV bytes in memory → a `sounddevice` output stream that Nova owns, so Nova can stop it within one audio block (≤ 50 ms).
   - **Long answers** are split into sentences. The first sentence plays while the next is made.
   - **Voices:** list `AllVoices` on the laptop. The "natural" Narrator voices may not be reachable through this API: check, and pick the best one that is.
   - **Kokoro-82M (ONNX)** sounds better, but it is > 50 MB and needs a phonemizer. That needs Bhavya's OK (AGENTS: ask before adding anything over ~50 MB). Build behind the same small interface (`say(text) -> Playback`, `Playback.stop()`) so it can drop in later.
   - **Not PowerShell SAPI:** too slow to start.
4. **Stop, every way:**
   - a Copilot tap (it stops the speech *and* starts listening, because the user wants to talk);
   - "stop" / "quiet" / "enough" / "shut up" heard while speaking (Decided 5 says how);
   - a click on the island's speaker glyph;
   - Esc, but only in states where the island already takes Esc (a card is up). Elsewhere Esc belongs to the app in front, so it is not taken.
   - Starting a new command also stops the old speech.
5. **Nova must not hear itself.** This is the crux, so probe it first:
   - **Playback flag:** Nova knows when it plays. While Nova speaks, and for 300 ms after, the endpoint meter's "speakers are playing" reads `nova_speaking` instead, so Nova's own voice never counts as a video playing.
   - **During that time:**
     - only the Copilot key, the name and the stop words act;
     - VAD alone never barges in;
     - a final that overlaps what Nova is saying (≥ 60 % of its words in order, normalized) is dropped as echo, and the drop is logged as `echo` (count only).
   - **Other audio playing:** the gate sees speakers = true, as today. Nova still speaks then, and the rules of SPEC §5 hold.
   - **Never speak** while another app holds the mic (a call) or in Passthrough: the island only.
   - **Echo cancellation:** later, and only if the probe shows the drop rule isn't enough.
6. **Off the hot path.**
   - Synthesis and playback run on their own thread, never the Qt GUI thread.
   - An action never waits for speech: the speech of a Done line starts after the action has run.
   - Budget: time to first audio ≤ 400 ms from the moment there is something to say (pre-rendered lines ≤ 50 ms).
7. **Logs:** what was spoken is not logged, only its kind, word count, time to first audio, and whether and how it was stopped.
8. **The island:** a small speaker glyph while speaking (it is also the click-to-stop target). The tray shows talk-back on / off.

## Probe first
- a. WinRT synthesis time for 5 / 20 / 40 words, and which voices `AllVoices` lists.
- b. **Echo:** play 20 Nova lines through the laptop speakers at normal volume with the mic open, in always-listening and in a talk session.
  - What does STT hear?
  - What does the gate do, today and with Decided 5?
  - How many stop words said over the speech are caught?

  Use the eval replay rig where possible; ask before a live run.
- c. The `sounddevice` output: does opening it while the mic stream runs glitch the mic? How long does `stop()` take?

## Build
1. `nova/voice_out.py`: `Speaker` with `say(text, kind) -> Playback`, `stop()`, the pre-rendered lines, sentence splitting, the markdown strip, the 40-word cut. Pure parts tested (split, strip, cut, echo-overlap match).
2. The `nova_speaking` flag into the gate and the endpoint-meter check; the echo-drop rule; the stop words while speaking (Tier 0, strict).
3. Settings key + voice commands + tray item; the bare "turn on voice" question.
4. Wire the speakers: the 09 answer card, Clarify / Confirm questions, errors, done notices, guide captions (07d), Done lines at `everything`.
5. `nova say --speak "<text>"` for a quick check of the voice and the time to first audio.

## Verify yourself
Unit tests first: the setting's state machine, the echo-overlap match, the gate with `nova_speaking`, the stop words. A replay test with recorded Nova speech in the mic path: 0 actions run from Nova's own voice. `nova latency` shows time to first audio. Ask once before anything plays out loud.

## Live demo (Bhavya, 3 min)
1. "talk back on" → ask a question that goes to Claude by 09's auto-route ("why is the sky blue") → the answer card appears and its first two sentences are spoken.
2. Midway through, say "stop" → silent at once. Ask again → tap the Copilot key mid-answer → silent and listening.
3. With a YouTube video playing: ask something → Nova speaks, and the video's words still don't act.
4. "talk back off" → the next answer is silent.

## If usage runs short
Stop after any of these with its demo line working: (1) the engine + stop (key, click) + the setting; (2) the echo rules + stop words; (3) everything wired to speak; (4) Kokoro (only with the OK).

Commit as you go; close with `stage 09a: talk back`. Report.
