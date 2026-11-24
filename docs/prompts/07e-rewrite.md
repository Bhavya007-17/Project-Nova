# Stage 07e — Rewrite the selection

**Read:** CLAUDE.md, SPEC §7.4, §8, §11, §14, §16; GOTCHAS "Dictation" (the paste quirks) and "Local LLM". Needs 07 (dictation's clipboard borrow and cleanup model). Independent of 07c / 07d.
**Goal:** select text anywhere, say "make this more formal" / "shorter" / "fix the grammar" / "turn this into bullets" / "translate this to Spanish", and the selection is replaced in place by the rewritten text, in ~2 s, on the laptop.

Added 2026-11-22 after a look at Nūs Companion (its `rewrite.js` sends the selection to a cloud model). Nova runs it on the planner model it already has: nothing leaves the laptop.

What is already there (read from the 07c branch on 2026-11-22; find things by name):
- `input.paste(text, target, landed, wait_s)`: dictation's clipboard borrow. It saves the clipboard, pastes, waits for the text to land, and gives the clipboard back. It never retries.
- `uia.FocusReader.caret()` reads the text *before* the selection (dictation's context). Nothing reads the selected text itself yet.
- The extension's `read("selection")` returns the page selection; the plan has `page_read{what: page | selection}`.
- `dictation/cleanup.py` `Cleaner`: shares the planner model. Its static prefix is byte-identical between calls, so the prompt cache keeps it. Copy that pattern.
- `risk.py` knows password fields (`Focus.is_password`) and refuses to type into them.

## Decided — build it this way
1. **The user's words choose the rewrite.** Plan: `rewrite{instruction}`, where `instruction` is a span of what the user said ("more formal", "into bullets", "to Spanish"). There is no fixed list of styles. Tier 0 catches "make this / that / it …", "rewrite this …", "fix the grammar / spelling", "translate this to …", "turn this into …", "shorten / expand this". Tier 1 learns the action from two examples.
   - "summarize this" and "explain this" are questions, not rewrites: they go to Stage 09's screen-question path, and nothing is replaced.
2. **Reading the selection**, cheapest first:
   - **Chrome page:** `page_read{what: selection}`.
   - **Native:** a new `FocusReader.selection()` through TextPattern `GetSelection`. This is the twin of `caret()`; it runs on the same thread, under the same timeout.
   - **Fallback when neither works** (WinForms has no TextPattern, per GOTCHAS):
     1. Save the clipboard and note its sequence number.
     2. Send Ctrl+C.
     3. Wait ≤ 300 ms for the sequence number to change.
     4. Read the clipboard, then put the saved clipboard back.
   - **No change = nothing was selected** → the island says "Select the text first". Nothing else happens.
   - **Never from a password field** (`Focus.is_password`, re-asked as GOTCHAS says for Chrome).
3. **The model.**
   - **Which model:** the planner model, with the same `num_ctx`, and a static prefix the cache keeps, as `Cleaner` does.
   - **Data, not instructions:** the selected text is fenced. The prompt says the fenced text is data, so an instruction inside it is rewritten, never followed (SPEC §14).
   - **Length limit:** set from measured tok/s so a rewrite lands in ≤ 3 s on the GPU (~300 words at 80 tok/s; check).
   - **Longer than the limit** → "That's long — send it to Claude?" (Stage 09). Before 09: "That's too long to rewrite here."
   - **CPU path:** the same limit in seconds, so fewer words.
4. **Guards before anything is replaced:**
   - Strip a preamble ("Here is…", quotes, a code fence).
   - **Output is empty, or its length is odd for the instruction** (shorter than 0.2× or longer than 3× the input, and the instruction isn't "shorter" / "expand" / "bullets") → show a preview card instead: [Replace] [Copy] [Cancel].
   - Otherwise replace at once. The island shows "Rewrote 84 words · undo".
5. **Replacing in place:**
   - Re-check focus first: the same window, the same control, and the selection still there (UIA, or the page). If the user moved, the result goes to the clipboard plus an island card: "Copied — you moved, so I didn't paste".
   - Then `input.paste` with a `landed` check through TextPattern where it exists. Never press Enter. Never type into a field that turned into a password field.
   - **Read-only selection** (a web page's body text, a PDF): the result goes to the clipboard and an island card [Copy] [Open in Notepad]. Nothing is pasted.
6. **Undo:**
   - "undo that" → Ctrl+Z in the same control, then read back.
   - If the original isn't back, put it on the clipboard and say "Couldn't undo here — the original is on your clipboard".
   - Add `rewrite` to `hands.py` `_UNDOABLE`.
7. **Logs:** the trace keeps word counts, the instruction's kind and the timings, never the text. The preview card and the island never show more than ~2 lines.
8. **In dictation mode** (07): "make that more formal" right after a dictated segment rewrites that segment, with no selection needed. Dictation already knows what it pasted (`paste_last`).

## Probe first
- a. TextPattern `GetSelection` on Notepad, Word (if installed), Outlook / Gmail compose in Chrome, Google Docs (canvas, so expect no), Slack or Discord (Electron). Which give the text, which need the Ctrl+C fallback?
- b. The model's tok/s for a 100 / 200 / 300-word rewrite on the GPU and the CPU, with and without the prompt cache warm.
- c. Does `paste` over a live selection replace it in each of those apps? GOTCHAS lists a line-break-only paste being ignored. Check multi-line results and bullets.

## Build
1. `FocusReader.selection()` + the Ctrl+C fallback (pure helpers for the clipboard-sequence wait; tests on fakes).
2. `nova/brain/rewrite.py`: prompt (static prefix + fenced text), length limit, preamble strip, length-ratio guard. Pure, tested.
3. Plan schema `rewrite{instruction}`, validate, Tier 0 patterns, Tier 1 rules + two examples (count the prompt's tokens before and after).
4. The executor path: read → model (off the Qt thread, cancellable by Esc / a Copilot tap) → guards → re-check → paste / card → receipt → undo.
5. The preview card in the island: [Replace] [Copy] [Cancel], answered by the hook keys like the other cards (1-3, Enter, Esc).

## Verify yourself
Unit tests first: Tier 0 phrases (and that "summarize this" is not a rewrite), the guard table, the preamble strip, the fallback's sequence-number wait, the undo read-back decision. `nova eval` ≥ 90 % with ≥ 10 new rewrite rows and ≥ 5 negatives. A synthetic run on the raw RichEdit test pad (GOTCHAS "Synthetic dictation testing"): select → rewrite → read back → undo → read back. Ask once before any run that takes focus.

## Live demo (Bhavya, 3 min)
1. Notepad: select a sloppy sentence → "make this more formal" → it is replaced; "undo that" → it's back.
2. Gmail compose in Chrome: select a paragraph → "shorter" → replaced.
3. A web page's body text: select → "translate this to Spanish" → card with the result + [Copy].
4. Nothing selected → "Select the text first".

## If usage runs short
Stop after any of these with its demo line working: (1) Notepad + Chrome compose replace and undo; (2) the read-only card; (3) the Ctrl+C fallback; (4) the dictation-segment rewrite.

Commit as you go; close with `stage 07e: rewrite`. Report.
