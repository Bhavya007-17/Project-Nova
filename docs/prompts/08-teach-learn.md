# Stage 08 — Teach + learn

**Read:** CLAUDE.md, SPEC §7.3, §8, §8.1, §12, §14, §16; GOTCHAS "Windows input and focus" and "Chrome extension". Needs 07c (receipts, `ui_click`, the UIA bindings); best after 07b.
**Goal:** Nova gets faster with use. A loose request the local model got right once runs instantly the next time, in other words (⚡). Something Nova can't do, he shows once and then it can.

What is already there (read from the code on 2026-11-13, before 07b / 07c landed — find things by name, the lines have moved):
- `router.py` already asks `Skills.match(text, ctx)` between Tier 0 and Tier 1 and returns `Routed(plan, "skill")`. `NoSkills` is the stub.
- `CommandFinished.skill` draws ⚡ in the island, but `brain.handle` never sets it.
- The Error face has [Try again] and [Show me]. They publish `IslandAction("retry" | "show-me")` and nothing listens. Tier 1's `teach` answer ends as that Error line ("I don't know how to do that yet — show me?").
- `State.TEACHING` exists with its island face (red dot + note). Nothing enters it, a Copilot tap there does nothing, and a final heard there goes to the gate as if it were a command.
- The always-listening gate resolves through `Tier0Resolver` only, so a skill could never pass it.
- `content.js` has no listeners at all, on purpose. The worker can already send Nova messages nobody asked for (`event: "tab"`, read in `Bridge._deliver`).

## Decided — build it this way
1. **A skill is a stored Plan plus the words that trigger it.** Replay is the normal path: `Skills.match` returns a Plan → risk check → Confirm if needed → executor → receipts. No second executor and no `run_skill` step inside plans (say in the report that SPEC §8 lists one that isn't used). So a skill with a risky step confirms every time, for free.
2. **Teaching compiles to actions that already exist.** A click on a page → `page_click{description, …}`. A click in a native app → `ui_click{name, role}`. Typed text → `type_text` / `page_fill`. A hotkey → `press`. A new address → `open_url`. Another app → `open_app`. A recorded step that can't be written as one of those is refused at that moment ("I can't replay a drag yet"), not saved as something that won't run.
3. **Words match on the frame, not on the names.** An embedding can't tell "open my thermo course" from "open my physics course", or "snap this left" from "snap this right" — both pairs score ~0.9. So a match needs all four:
   - what varies is masked out of the stored trigger and the new sentence before embedding: catalog and alias names, numbers, the payload span;
   - the contrast words are the same set on both sides (left / right, up / down, next / previous, on / off, open / close, mute / unmute, in / out, first / last, more / less …) — `pagemap._PARTICLES` solves the same problem for "sign in" vs "sign up";
   - similarity is above a threshold picked from data (below);
   - every param is filled again from the new sentence: names through the catalog, text as a span of his words. SPEC §7.4's payload rule holds on replay — a stored literal is never typed for a sentence that didn't say it.
   Anything less → no match, Tier 1 takes it. A wrong ⚡ does the wrong thing; a miss costs half a second.
4. **Learn late.** A plan is saved only when the next accepted command arrives, or the follow-up window closes, without "stop", "undo that", "no, I meant …" or "that's wrong" in between. That one rule keeps most of Tier 1's mistakes out.
5. **Skill files are untrusted.** `data/skills/*.json` is meant to be edited by hand: every load goes through `plan.check` and the catalog. A bad file is skipped with one log line, never a crash.

## Build
1. `nova/skills/store.py` — the SPEC §12 record as frozen dataclasses, one JSON file per skill, atomic save (write + replace, as `ConfigStore.save_value` does). Fields beyond SPEC: `taught` (bool), `stale`, `learned_at`. `nova skills list | show <id> | delete <id>`.
2. `nova/skills/match.py` — pure, given vectors. The embedding model sits behind `embed(texts) -> array` so tests use a fake.
   - **Pick the model by rule, then measure:** ≤ 50 MB on disk, permissive licence, runs on the `onnxruntime` already installed (1 thread) or in plain numpy, ≤ 10 ms a sentence warm. Check what is current: a MiniLM-class sentence model as int8 ONNX (+ the `tokenizers` wheel), or a static-embedding model (numpy only, microseconds). Don't add `fastembed` or `sentence-transformers` without reading what they pull in (hub downloads at run time, torch): Nova never downloads. The file comes through `scripts/get_models.py` with a pinned hash; one line in DEPS.md.
   - It loads on a worker at start, not on the first command. Report the RAM it adds (SPEC §15: idle < 1.2 GB).
   - **Threshold from data:** in `docs/eval/intents.yaml`, phrasings with the same `want` are each other's positives, different `want`s and the ~100 conversation lines are negatives. Add a hand-written table of contrast pairs (step 3 of "Decided"). Take the lowest threshold with 0 false matches, then report recall. If recall is under ~70 %, say so — don't lower the bar.
3. **Auto-learn** in `brain.handle`, after a Tier 1 `act` plan ran:
   - Learn when: every step's receipt (07c) shows the change it expected; confidence ≥ 0.8, or he answered its question (then his answer is the truth); not cancelled; rule 4 above.
   - Don't learn: plans that needed session context to find "it" / "there" (07b); a `type_text` / `web_search` whose text isn't a span that can become a param; dictation starts; anything pointed at only by a page-row number.
   - Make it deterministic: drop transient ids (`element`, tab ids) and keep description, label, role, section and a URL pattern; names that resolve in the catalog become params; `target: active` stays (the skill means "whatever is in front").
   - `safe` = every step passes the same test as `tier0.safe_plan`. `scope` = the app or host it ran in when that matters (page and UI steps), none for app / window / media steps.
   - Pass `skill=True` to `CommandFinished` when a skill ran. Trace: `tier: "skill"`, the skill id, `match_ms`. `nova latency` gets a "skill" row (budget: action starts ≤ 500 ms, SPEC §16).
4. **When a skill fails or is wrong.**
   - A step whose receipt shows nothing changed → stop, say which ("Step 2 of 4 didn't work: Click “Grades”"). An auto-learned skill then goes to Tier 1 once with skills off: if that works, the skill is replaced; if not, it is marked stale (kept on disk, never matched, shown in `nova skills list`). A taught skill offers [Show me] again.
   - "no, I meant …" / "that's wrong" / "undo that" right after a ⚡ removes the trigger that matched (the skill, if it was its only one); "no, I meant X" then runs X the normal way.
5. **Teach mode.** Starts from [Show me], from "yes" / "show me" said after that Error line, or from "Nova, let me show you how to …" (a Tier 0 pattern; the rest of the sentence is the first trigger). `[Try again]` gets wired here too: it runs the last command's text again.
   - **Teaching is a mode like Dictating**, with its own branch in the state machine: finals are narration, never commands. "done" / "that's it" / "stop recording" or a Copilot tap ends it → summary. Esc or "cancel" discards. 3 min at most; 60 s with no event ends it with what it has. Inside a talk session it pauses the idle clock, as dictation does (07b). A state that can be entered must have every way out tested.
   - **Chrome recorder.** Only while Teaching, the worker puts capture-phase listeners (click, change, submit, Enter) into every frame of the tab in front and again after each navigation, and takes them out on stop — "no listeners while idle" stays true. Each event is sent as the page map's own description of that element (role, name, section heading, row text, href, `secret`) + the page address. Nothing else leaves the page. Bump the extension version: he reloads it once at chrome://extensions (put that in the demo script). Extend `extension/test/selftest.py`: start → click → event arrives → stop → no listeners left.
   - **Native recorder — the hard part is the mouse.** A `WH_MOUSE_LL` hook runs Python on every mouse move; whenever another thread holds the GIL the cursor stutters, and Windows silently drops hooks that answer late (the keyboard hook's < 10 ms rule, GOTCHAS). Use raw input for the mouse instead (`hook.py` already registers raw input for keys; mouse is usage page 1, usage 2): it can't stall the cursor and can't be dropped. On button-down read the cursor position and ask UIA for the element there, on the UIA thread — apps act on button-up, so the element is still the one he saw. Keep name, role, automation id, the ancestor path up to the window, and the app. Hotkeys come from the keyboard hook that is already there. Measure and report: CPU while the mouse moves, button-down → element read (p95), and that the cursor feels the same.
   - **One click, one step.** A click inside Chrome's page is seen by both recorders: keep the extension's. UIA's is for Chrome's own toolbar and every other app. Drop Nova's own input (`NOVA_MARK`) and clicks on the island.
   - **Typed text.** Never from a secret field (UIA `IsPassword`, the page map's `secret`): the step is refused and the island says a skill can't include a password. Text that also appears in his first sentence becomes a param. Other text is kept as typed and shown in the summary before he saves.
   - **Not recorded in v1:** drags, hovers, scrolls (replay scrolls things into view by itself), timing.
   - **Narration.** A final belongs to the recorded event nearest in time (within 3 s; the one after it wins a tie). A narrated step keeps both his words ("the assignment due today") and the name the element had. Replay tries his words first — `pagemap.rank` already understands "today", "first", "newest". One clear element → go; several → ask; none → Tier 1 with only that description and the candidates (the router's page path does this today). The recorded name is the last fallback, and is not used when his words carried a date or an order word: next week it is the wrong assignment.
   - **Summary.** The name (his first sentence), one line per step (the step-line labels), [Save] [Discard]; Enter / "save it" / Esc / "discard" through the hook, as Confirm works. More than 4 steps → "… +3 more". Scope = the app or host of the first step.
6. **Manage by voice.** "what can you do in Canvas" → a numbered island list for that scope (say a number to run one). "forget that" → the skill that just ran (it is a delete: confirm). "rename that to …".
7. **Skills in always-listening — last, and only if there is time.** SPEC §5 lets a safe skill with high confidence run without the name. The gate must stay pure: hand its resolver a snapshot of the safe skills, as it is handed the catalog. Use a stricter threshold and replay 07a's logged rejects through it: 0 new accepts. Not done → say so; nothing else depends on it.

## Verify yourself
Unit tests, written first: masking, the contrast table, param refill, the learn filter (a table of plans in / out), learn-late (undo inside the window → nothing saved), stale → relearn, store round-trip and bad files, the Teaching branch of the state machine (enter / narration / done / tap / Esc / both timeouts), events → steps for Chrome and native fixtures, the both-recorders dedupe. `nova eval` with skills on: plans correct not lower, false actions on negatives not higher. Matcher numbers (threshold, false matches, recall, ms) and RAM in the report. The recorder self-test in Chrome for Testing. Ask once before the native recorder run.

## Live demo (Bhavya, 4 min)
1. Ask something new that the LLM handles ("open my thermo course on canvas") → works. Say something else. Ask again in other words → instant (⚡). `nova skills list` shows it.
2. "that's wrong" right after a ⚡ → it is forgotten; the same words go through the LLM again.
3. Ask something it can't do → "Show me?" → yes → do it in Chrome while narrating → "done" → Save. Ask again in other words → it replays. Then the same in a native app.

## If usage runs short
Stop after any of these with its demo working, and say what is left: (1) store + matcher + auto-learn + ⚡ + CLI; (2) wrong / stale handling; (3) teach in Chrome; (4) teach in native apps; (5) voice management; (6) always-listening skills.

Commit as you go (`feat:` / `fix:`); close with `stage 08: teach + learn`. Report.
