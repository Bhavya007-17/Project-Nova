# Prompt 1: Eval harness and fuzzy-bug protocol


```
Goal: make every later stage measurable, and give us a repeatable way to catch "fuzzy" bugs (intermittent, timing-dependent, near-tie model flips, sound-alike mismatches).

Build:
1. A frozen eval layout under docs/eval/: train/dev/test splits for intents (existing 262 rows), a negatives set (target 150-300 chatter lines: small talk, acks, thinking aloud, TV/room speech), talk sessions, tasks (docs/eval/tasks.yaml), and a new speech set (fixtures/command-set from nova record-set, 200-500 of my own recordings, plus SAPI-synthesized lines for names).
2. nova eval --compare <git-ref>: runs both refs on the same split and prints per-row diffs, McNemar p-value and bootstrap 95% CIs. Fails CI if negatives' false-action rate rises.
3. Speech metrics in nova bench-stt: WER after case/punct normalization, plus "bare ack rate" (finals that are only yeah/okay/yes), "empty-final rate", and, for any corrector, repairs vs breaks (a break = a correct word changed to a wrong one).

Fuzzy-bug protocol (write it into docs/GOTCHAS.md as a section and implement the tools):
- Determinism first: temperature 0, fixed seed, same num_ctx for every caller, same runtime. Log runtime, model digest, prompt hash and cache state in every trace.
- Near-tie logging: for every Tier 1 decision, log the top-2 logprob margin at the decision token. Flag margins < 0.15 (tunable) as "near-tie"; those rows are where cached-vs-uncached flips happen (GOTCHAS already notes one). Report near-tie rate, never chase single-row diffs.
- Replay, don't re-speak: every bug report must carry a trace id; nova replay <trace-id> reruns the exact audio/text, env and context. Add `--repeat N` to expose flakiness; a bug that fails 3/20 is real.
- Timing bugs (follow-up window, talk endpoint, focus races): add a fake clock and an event log so the gate can be replayed with shifted timings (±300 ms jitter sweep). Write the failing timing as a unit test before fixing.
- Property/fuzz tests with hypothesis for: Tier 0 regexes (number words, "five over to nine"), ack-word guessers, clause splitting, alias matching. Properties: never crash, ack words never become verbs, risky verbs never skip confirm, idempotent normalization.
- Sound-alike fuzz: generate homophone/near-phoneme variants of every alias and check Tier 0 either resolves correctly or asks; it must never act on the wrong app.
- Bisect: nova eval --compare plus git bisect run for regressions.

Done when: nova eval --compare works on CI, baselines are recorded in docs/eval/BASELINE.md (WER, bare-ack rate, empty-final rate, intent accuracy, false actions, session clause accuracy, per-step and whole-task GUI accuracy, p50/p95 latency), and the fuzzy-bug protocol is in GOTCHAS.
```
