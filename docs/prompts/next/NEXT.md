# Next stage

Current: **Stage 01, eval harness and fuzzy-bug protocol**. Nothing from stages 01-11 is built yet (checked
2026-11-27: `main`, every branch and `mac_shaashwat_test` have no eval splits, `nova eval --compare`, per-word
confidence, latency budgets or G2P).

Kickoff (paste in plan mode after /clear):

```
Read docs/prompts/next/01-eval-harness-and-fuzzy-bug-protocol.md and the code it touches (nova/evals.py, nova/session_eval.py, nova/task_eval.py, nova/stt/bench.py, nova/stt/wer.py, nova/latency.py, nova/log.py, nova/gate.py, docs/eval/). Freeze the splits before anything else and dedupe them against every prompt's few-shot examples. Make a plan; don't edit yet.
```

Check before approving: the splits are frozen and hashed, `nova eval --compare <ref>` runs both refs on the same split, and the plan says what runs on CI (Windows runner, no models) vs on the laptop.

Evidence before commit: `docs/eval/BASELINE.md` filled from real runs on the reference laptop, `nova eval --compare main` output, the fuzzy-bug protocol section in `docs/GOTCHAS.md`.

After this stage, replace this file's contents with the kickoff for stage 02.
