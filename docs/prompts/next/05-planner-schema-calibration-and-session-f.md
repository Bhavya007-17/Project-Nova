# Prompt 5: Planner schema, calibration, and session fix (#19)

> Builds on: stage 01 eval splits and nova eval --compare; stage 02b timing spans (add a planner-gate span; the logprob read must not add a second request).


```
Goal: fewer false actions and a real confidence signal.

Research to apply: putting answer fields before reasoning fields in constrained JSON hurt reasoning; grammar decoding makes the model fill every required field (GOTCHAS: a required "ask" won every pick); self-reported confidence is the few-shot number (always 0.85).

Do:
1. Schema: first field "kind": enum [act, ask, ignore]. Clarification text exists only in the ask branch; plan steps only in act. A short "why" (<= 12 tokens) may come before steps. Keep exclusive shapes (as the pick schema already does).
2. Calibrated gate: read logprobs at the "kind" token; compute p(act), p(ignore), margin. Fit thresholds on the dev split (replayed logs): pick the operating point that keeps false actions on negatives <= current while maximizing coverage. Optionally a tiny logistic regression over margin, p(act) and entropy. Remove the self-reported confidence field.
3. Issue #19: each talk session gets its own llama-server slot / context id; all dynamic content goes after a byte-identical static prefix; a tap-closed session's running clauses can never read a name-opened session's context. Add a replay test reproducing #19.
4. A/B with nova eval --compare on intents, negatives and sessions.

Done when: false-action rate on negatives drops (significance reported), intent accuracy does not drop below 95.8% beyond noise, #19 test passes.
```
