# Stage 08 — Teach + learn

**Read:** CLAUDE.md, SPEC §7.3, §12.
**Goal:** Nova gets faster with use. Anything the LLM does successfully becomes an instant shortcut; anything Nova can't do, Bhavya shows it once.

## Build
1. `nova/skills/store.py` — JSON skills (SPEC §12), load/save; `nova skills list | show | delete`.
2. `nova/skills/match.py` — small CPU embedding model (verify the package, e.g. fastembed / onnxruntime) + scope filter + threshold tuned on the intent-eval paraphrases; parameter re-extraction.
3. Auto-learn in the router after verified success, with the SPEC §12 determinism rules. Learned skills replay with no LLM (island ⚡); a failing skill is marked stale and Tier 1 takes over.
4. Teach mode — from "Show me?" or "Nova, let me show you how to …": record Chrome steps via the extension and native steps via a low-level mouse hook + UIA element-at-point + hotkeys; attach narration to steps as semantic descriptions; "done" ends; island summary [Save] [Discard].
5. Replay engine: literal steps run directly; semantic steps are resolved by Tier 1 with only that step's description + candidates; every step verifies; on failure stop and offer re-teach.
6. Voice management: "what can you do in canvas", "forget that", "rename that to …".

## Verify yourself
Unit tests: determinism filter, matching (paraphrases match, different intents don't), replay against a fake executor.

## Live demo (Bhavya, 4 min)
1. Ask something new that the LLM handles (e.g. "open my thermo course on canvas") → works. Ask again in different words → instant (⚡).
2. Ask something it can't do → "Show me?" → yes → do it while narrating → "done" → Save. Ask again in other words → it replays.

Commit `stage 08: teach + learn`. Report.
