# Prompt 0: Shared context (paste first, every session)


```
You are working on Nova (this repo), a local-first voice layer: Silero VAD + Parakeet TDT 0.6B v3 for speech, a Tier 0 pattern fast path, a Tier 1 planner (qwen3.5 4B GPU / 2B CPU via Ollama or the bundled llama-server) producing schema-forced JSON plans, Win32 + UI Automation hands, a Chrome MV3 extension over native messaging, dictation with cleanup, and a PySide6 island.

Read AGENTS.md, docs/SPEC.md and docs/GOTCHAS.md before touching code. GOTCHAS holds measured facts; do not re-learn them.

Hard rules for every stage:
1. No hosted model APIs, ever: no OpenAI, Anthropic, Google, or any cloud inference at runtime or for generating training data. Only open-weight models running locally. Training compute may use free notebooks (Kaggle, Colab) or my own machines. Nothing paid: no paid GPUs, subscriptions, licences or developer accounts. If something seems to need one, stop and offer a free alternative.
2. Never restrict what I say: no maximum utterance length for addressed speech, no vocabulary whitelist, no rejecting a transcript because of its words or length alone. Improve recognition and interpretation instead. Narrowing what the MODEL may output (enums, candidate lists, pruned rows) is fine; narrowing what I may SAY is not.
3. Measure before and after. Every change gets a number from nova eval, nova bench-stt, nova latency, or a new eval this stage adds. Report p50/p95 latency, accuracy, and false-action rate where relevant.
4. A/B every prompt or schema change on the negatives (chatter) set against main. Use a paired test (McNemar) on a frozen test split; with ~262 rows, swings under ~3 points are noise.
5. Eval rows must never appear in the prompt examples or training data (dedupe on normalized text).
6. Privacy: transcripts and recordings stay local. Never print transcripts from live mic tests; delete trace lines your tests create.
7. Risky verbs (send, submit, delete, buy, post) always confirm, enforced in code, not in the prompt.
8. Add every new hard-won fact to docs/GOTCHAS.md in the same PR.
9. Stop and show me a plan before any change that touches more than ~5 files or changes the plan JSON schema.
10. No hard-coded command logic. Do not write one function or if-branch per app, command, phrase, setting or language. Behaviour must come from, in this order: what Nova discovers at runtime (Start menu / installed apps, running windows, the UIA or AX tree, app menus, the page DOM), learned stores (learned aliases, skills, corrections), config/data files the user can edit, and the model. Hand-written lists are allowed only as seed data in config/ or data/ that the system extends, overrides and can work without. Generic code paths only: one "open app" handler that works for any app in the catalog, one "toggle setting" handler that works on any discovered control. Exceptions that stay in code on purpose: the risky-verb confirmation rules and the plan schema. Thresholds are never typed in by hand; they are fitted on the dev split and stored in config with the date and data they came from.
11. Latency is a feature. Every stage must state its latency cost and stay inside config/latency_budgets.yaml (p50 and p95 per path). New work stays off the hot path: anything slow (second-opinion decoding, correction, memory lookup, logging, learning) runs in parallel or after the action, never before the first action unless it is required for safety. CI fails if nova latency --replay shows a p95 regression beyond the budget's tolerance. If a feature cannot meet its budget, ship it behind a setting that is off by default and tell me the cost.

At the end of the session: summarize what changed, the before/after numbers, what you could not verify, and the exact commands to reproduce.
```
