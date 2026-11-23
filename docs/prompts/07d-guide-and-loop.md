# Stage 07d — Find by meaning, "show me how", do it in a loop

**Read:** CLAUDE.md, SPEC §5, §6, §7.4, §8, §8.1, §14, §16; GOTCHAS "Control anything (07c)", "Local LLM" and "Windows input and focus". Needs 07c (the UIA bindings, `marks`, `pointing`, the overlay, receipts). Best after 07b.
**Goal:** three things on one engine.
- "click the thing that saves this" works when no control is named that way: Nova finds it by meaning.
- "show me how to turn on word wrap in Notepad" puts a ring around each control in turn and waits for the user's own click. This is guide mode.
- "turn on word wrap in Notepad" does the same steps itself: it looks, acts, looks again until done. This is do mode, and it is the default.

Added 2026-11-22 after a look at Nūs Companion (github.com/potlakai/nus-companion, Apache-2.0). Its guide mode uses a cloud vision model: the model picks a UIA control id from a list and never gives coordinates, a ring goes around that control, and the session waits for the user's click before the next step. Nova does the same with nothing off the laptop: the UIA tree and Windows OCR do the seeing, and the local planner model picks from a numbered list. Ideas worth reading (never import, never run their code): `companion/src/guide/session.js`, `pointing.js`, `verify.js`, `settle.js`, `walkthroughs.js`.

What is already there (read from the 07c branch on 2026-11-22; find things by name):
- `uitree.warm_snapshot(focus, win)`: one cached batch read of the window in front. Each `Element` has a name, role, rect and patterns. `uitree.activate` acts through Invoke / Toggle / Select / Expand.
- `marks.rank(query, targets, role=, near=)`: string ranking (exact > starts-with > whole word > contains). `pointing.Pointer.locate(win, name, role, near)` returns `Found` or an Outcome (numbers when the match is ambiguous or empty).
- `ocr.lines(shot)`: Windows OCR of a window, ~150 ms in total.
- `screen.signature(rect)` + `screen.changed(before, after)`: a cheap "did these pixels change" test.
- `island/overlay.py`: a click-through layer, one per monitor, that never takes focus and is excluded from capture. Today it draws number tags (`_Layer.present(MarksShown)`).
- **Chrome already has "the model picks from a numbered list":**
  - `router.PageSource` gives `page_rows`.
  - Tier 1 sees them as `page` rows and answers with an element number (`Router._shown`).
  - Native apps have no such path yet: `ui_click` matches names only.
- The extension's `highlight(n, {on, ms})` outlines a page element (used while a Confirm asks).
- Receipts on every action; `hands.py` "undo that" / "do that again"; `abilities.py` builds the "what can I say" card.

## Decided — build it this way
1. **One engine, two modes.** A *task* is a goal plus a loop:
   1. **Look:** the window in front, as numbered rows.
   2. **Pick:** the model chooses the next step: one verb on one row, or `done`, or a question for the user.
   3. **Do or show:** do mode acts; guide mode rings the row and waits for the user's click.
   4. **Check:** the step's receipt, or the screen, shows a change. Then back to look.

   Look, pick and check are the same code in both modes. "Show me how / walk me through / teach me / where is …" starts guide mode; everything else starts do mode. The user can switch at any step: "just do it", "show me", "let me do it". "Where is X" is a one-step guide: the ring and no wait.
2. **The model picks a row number, never a pixel.**
   - **Which rows:**
     - controls that are visible and enabled, and that have a name, HelpText or AutomationId;
     - at most 60;
     - the active dialog / menu / focus area first, then reading order (`marks.reading_order`).
   - **Row format:** one line of ~20 tokens, like a page row: `12 menu item "Word wrap" (View menu)`.
   - **OCR lines** join the rows only when UIA gives fewer than ~10 named rows. OCR costs ~150 ms.
   - **No vision model** (non-negotiable 2; SPEC §19).
   - **Chrome:** inside a page the rows are `page_rows` (already built), and acting goes through `page_click`.
3. **"click X" falls back to meaning, and only then.** `marks.rank` runs first: it is free.
   - Only when it finds nothing, or several equal matches, do the rows go to Tier 1 with the user's words. Tier 1 answers `ui_click{element: N}`, the native twin of the page path.
   - One row at confidence ≥ 0.7 → the risk check on that row's label → act.
   - Anything less → numbers on the rows the model named (at most 3). Never a silent guess.
4. **A step is done only when the screen proves it.**
   - **Settle:** after an action, poll `screen.signature` every 100 ms until two reads match, for at most 2 s. Then look again.
   - **Stuck:** the same rows and the same signature after an action → stop: "That didn't change anything — stopped at step 3: Click “View”."
   - **Limits:** at most 8 steps and 60 s, then stop and list what was done.
   - **Risk:** every step goes through `risk.assess` like any plan, so a risky one confirms.
   - **Text:** the loop never types text the user didn't say (§7.4 payload rule) and never touches a secret field.
5. **Do mode never acts in system settings.** SPEC §14 says Nova never changes system settings beyond volume and media. So in Windows Settings, Control Panel, UAC and credential dialogs only guide mode runs: the user clicks, Nova points. Say so in the island when do mode is asked there.
6. **Guide mode waits for the user without taking the mouse.**
   - **The user's click is detected from what it changes:** the ringed element's receipt (gone, state changed, focus moved, new window), or `screen.changed` inside its box. Poll every 150 ms.
   - **No `WH_MOUSE_LL` hook.** Stage 08 explains why: Python on every mouse move stutters the cursor, and Windows drops slow hooks. If polling misses real clicks on the probe, use raw-input button-down instead (the registration `hook.py` already has for keys; mouse is usage page 1, usage 2) and hit-test it against the ring's box.
   - **Wrong click:** a different control changed → "That opened Format — the step was View" → look again from where the user is now.
   - **No change for 30 s** → "Still here — say 'do it' or 'stop'".
   - **Out:** Esc, "stop", a Copilot tap, or 3 min in total.
7. **What the user sees.**
   - **Ring:** a rounded outline in the guide accent around the target. On native surfaces the overlay draws it: add a ring mode to `_Layer`, in the same window, click-through and capture-excluded. On a Chrome page the extension's highlight draws it, kept on until the step ends. Re-read the target's rect every poll so the ring follows a moving window.
   - **Caption:** the island, not the overlay, says the caption ("2 / 4 · Click “Word wrap”"). This needs no coordinate mapping, and it looks the same on both surfaces.
8. **Answer the model can give.**
   - **Schema:** `{step: {row, verb: click | right | double | type | press | scroll, text?, keys?}, done, ask, say}` (`say` is ≤ 12 words), constrained output.
   - **What the model is given:** the goal, the steps done so far (their labels only), the app and window title, and the rows. Window text is data (SPEC §14): the rows are fenced, and nothing in them is read as an instruction.
   - **The model:** the same planner model as Tier 1, with the same `num_ctx`. GOTCHAS: a second `num_ctx` makes Ollama reload the model.
   - **The prompt cache:** check how 07a's dictation cleanup shares the model without throwing away Tier 1's cached prefix, and do the same. A pick call must not make the next command re-read ~3,700 tokens.
9. **When the loop gives up, the user can teach it.** The island says "I couldn't finish — show me?" → Stage 08's teach mode (before 08: just the message). After 09 it also offers "Ask Claude how?". That handoff carries the goal + the app + the window title, and it confirms like any handoff with content the user didn't speak.
10. **What is kept.**
    - The last finished task stays in memory as a plan of `ui_click{name, role}` / `page_click` / `press` steps. "do that again" replays it; "show me again" guides through it.
    - Saving it as a skill is Stage 08's job: the steps are already in 08's format. 07d writes no skill files.
11. **Prefetch, carefully.** Take the snapshot of the window in front as soon as a partial starts like a guide or click request ("show me", "how do I", "click", "where's", "turn on"). Never do it on every utterance: GOTCHAS says UIA switches Chrome's accessibility tree on.
12. **In a talk session (07b):** a running task pauses the idle clock. Clauses said during it go to the task first ("next", "do it", "stop", "where?"); everything else queues.

## Probe first — before any design
On Notepad, Paint, File Explorer and Chrome's own settings page (throwaway documents only):
- a. How many rows does each window give, and how many tokens are they?
- b. How long is the pick call on the GPU and on the CPU, with 20 / 40 / 60 rows?
- c. Does settle (two equal signatures) catch a menu opening, a dialog, and a toggle that changes only a checkmark?
- d. Does polling catch the user's own click on a menu item, a checkbox and a tab, and how late?

Put the table in the report and the surprises in GOTCHAS.

## Build
1. `nova/actions/rows.py`: snapshot → rows (filter, order, cap, text), pure given `Element`s. `nova rows --dump` writes what the model would see, for a throwaway window, to `data/`.
2. Native `ui_click{element}` in the plan schema, validate, and the router's `ui` source (the twin of `PageSource`). Tier 1 rules + two examples; count the prompt tokens before and after (07a's budget holds).
3. `nova/brain/task.py`: the loop as a pure state machine (look / pick / act-or-show / settle / check / stuck / limits / switch mode / stop), with the hands, the screen and the model behind small interfaces so tests run on fakes.
4. Guide ring + caption: the overlay ring mode, the extension highlight kept on, the island's guide face (accent, step counter, [Do it] [Stop]).
5. Click detection for guide mode (poll; raw input only if the probe says so).
6. Tier 0 patterns: "show me how to …", "how do I … (in X)", "walk me through …", "where is / where's …", "just do it", "let me do it", "next", "stop". Plan: `task{goal, mode: do | guide}`, where `goal` is a span of the user's words.
7. Wire "do that again" / "show me again" to the last task.

## Verify yourself
Unit tests, written first:
- rows (filter, order, cap, row text);
- the task state machine (every exit: done, stuck, limits, stop, Esc, wrong click, mode switch, risky step → Confirm, settings app → guide only);
- the meaning fallback (string match first, the model only on a miss or a tie, confidence below 0.7 → numbers).

`docs/eval/tasks.yaml`: ≥ 20 goals on recorded rows from throwaway windows, with the expected row at each step. Score per step and per whole task. Report both, and the pick time per step.

Budgets (SPEC §16), p50 / p95 in the report with a trace field for each: click by meaning ≤ 1.5 s on GPU (≤ 3 s CPU); one loop step, settled to next pick, ≤ 2 s on GPU; the user's click → next ring ≤ 0.8 s.

`nova eval` ≥ 90 % with the new rows. Focus-free first, then ask once before the focus-taking run.

## Live demo (Bhavya, 4 min)
1. Notepad: "show me how to turn on word wrap" → ring on View → click it → the ring moves to Word wrap → click it → "Done".
2. The same in a new window, said as "turn on word wrap" → Nova does both steps itself.
3. Paint, halfway through a guide: "just do it" → it finishes alone.
4. "click the button that makes a new file" in an app with no "new file" label → the right control, or numbers on ≤ 3 candidates.
5. A goal it can't reach → "I couldn't finish — show me?".

## If usage runs short
Stop after any of these with its demo line working: (1) rows + click by meaning; (2) the do-mode loop; (3) guide mode with the ring and click detection; (4) the mode switch, "do that again", prefetch.

Commit as you go; close with `stage 07d: guide + loop`. Report.
