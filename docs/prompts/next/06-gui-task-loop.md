# Prompt 6: GUI task loop

> Builds on: stage 05 act/ask/ignore schema and calibrated gate; stage 02b budgets (add spans for row ranking, menu walk and postcondition check; menu walks run only when the goal matches no visible row).


```
Goal: raise per-step accuracy (baseline 48.5%, 5/30 whole tasks).

Research to apply: pruning candidates before the model sees them lifted a 3B model's grounding from 46.8% to 88.3% on web pages (Prune4Web); completion checks and loop breakers helped GUI agents modestly; small models hallucinate in long plans.

Do:
1. Row pre-filter: BM25 (and optionally a small local embedding model) over UIA/DOM row names vs the goal and step; show the planner the top 10-20 rows plus always-present "none of these" and "open menu" options. Measure recall@10 on logged steps (target >= 90%).
2. Menu walk: when the goal matches no visible row, or the model picks "open menu", discover the app's menu bar and ribbon items from the UIA tree, open them one at a time, and re-rank the rows found inside. No menu names in code. This targets "never opens menus" in GOTCHAS.
3. Discovered app maps, not a hand table: the first time Nova walks an app's menus (or Windows Settings pages), save what it found (path of names, control type, automation id) to data/appmaps/<app>.json, keyed by app version. Later goals search this map first, so Nova learns where things are by exploring. Index Windows Settings once by walking its navigation tree and recording each page's controls; use any ms-settings: link Settings itself exposes, not a typed-in list. Maps expire when the app version changes. Hand-added entries are allowed only as editable seed data.
4. Done = code: derive a postcondition from the goal (toggle state, window title, control value) and check it via UIA; the model's "done" is never trusted alone (task.proven stays, gets stronger).
5. Loop breaker: stop on a repeated (screen hash, action) pair; ask me instead of clicking again.
6. Vision fallback (issue #23) only when UIA has no matching element: evaluate a small detector locally; do not ship one that cannot fit beside the planner and Parakeet in 8 GB.

Done when: nova eval --tasks per-step accuracy and whole-task success rise vs baseline, early-done and repeated-click counts fall, latency per step reported.
```
