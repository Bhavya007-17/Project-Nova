# Prompt 9: Adaptive capability (Stage 08)

> Builds on: stage 03 learned aliases, stage 05 gate, stage 06 postcondition checks (the only proof a skill succeeded), stage 02b budget for retrieval (~10-20 ms, skip on miss).

> Earlier WIP exists on branch mac_shaashwat_test (b7a1391 skills/recall, 2139d18 Mac port); reuse what fits.


```
Goal: Nova gets better from my use, on-device, without retraining every time.

Research to apply: stored successful workflows help agents (Agent Workflow Memory), but unfiltered memory scored below no memory and more than ~2 retrieved examples hurt (EchoTrail-GUI); abstracting values into placeholders beat raw recordings (UI-Mem); memory can be poisoned by injected content (MINJA).

Do:
1. Skill store: data/skills.jsonl, one record per verified success: normalized utterance with slots ({app}, {file}), plan, context fingerprint (app, URL pattern, locale), per-step selectors (Chrome: role + name; native: UIA control type + name + automation id + ancestors), postcondition, source (llm | demo | correction), success/correction counts, last used, schema version.
2. Only verified successes enter: postcondition passed or I confirmed. Never the model's own "done".
3. Lookup order: Tier 0 -> alias normalization -> learned skill (fingerprint must match) -> Tier 1 with 1-2 retrieved skills as examples (embedding or BM25 over utterances) -> Tier 1 alone. Retrieved examples go after the static prefix to keep the cache warm.
4. Promotion ladder: stored -> suggested ("run your learned shortcut ⚡?") after 2 verified successes -> automatic after 3+ with no corrections, only for low-risk reversible actions. Send/delete/buy/settings always confirm. Any undo, Esc or "no, I meant" demotes; unused skills expire after N weeks.
5. Teach mode ("Show me?"): record my Chrome and native steps with narration, store as a soft reference with role/name selectors and a postcondition, replay with verification at each step, fall back to asking on mismatch.
6. Corrections: "no, I meant X" creates a correction record; confirmed alias corrections go to learned_aliases.yaml (Prompt 3).
7. Safety: page or UIA text can never create a skill; only my confirmed actions can. Everything is plain text I can list, edit, delete: nova skills list/forget/export.
8. Eval: memory on vs off on replayed logs; plant 10 deliberately wrong skills and measure harm.

Done when: success rate and tokens/latency with memory on vs off are reported, planted bad skills do not get promoted, and teach mode replays 5 demos correctly.
```
