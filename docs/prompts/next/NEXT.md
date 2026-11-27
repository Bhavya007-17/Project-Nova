# Next stage

Current: **Stage 04, dictation misheard-word fix (#18)**. Stages 00-03 and 02b are done.

Kickoff (paste in plan mode after /clear):

```
Read docs/prompts/next/04-dictation-misheard-word-fix-18.md and the code it touches (nova/dictation/cleanup.py, the Parakeet confidence work from stage 02, the phonetic matcher from stage 03). Reuse stage 03's G2P and phoneme matching instead of building a second one. The corrector must run after text lands and stay inside the dictation budget in config/latency_budgets.yaml from stage 02b. Start with the diagnosis step. Make a plan; don't edit yet.
```

Check before approving: the plan reuses stage 03's phonetic code and adds a dictation-fix timing span with its own budget.

Evidence before commit: repairs vs breaks (break rate), p95 time-to-fix, whether live attempts land.

After this stage, replace this file's contents with the kickoff for stage 05.
