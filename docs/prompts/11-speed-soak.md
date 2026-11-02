# Stage 11 — Speed + soak

**Read:** CLAUDE.md, SPEC §16, §18.
**Goal:** every latency budget met; Nova survives a normal day.

## Do
1. `nova latency` — p50/p95 per path (the SPEC §16 table) + the 10 slowest utterances with a per-hop breakdown.
2. Fix the top 3 offenders. Likely wins: start the context snapshot and LLM preload at speech start; run Tier 0 on stable partials so obvious commands are ready at speech end (still execute only on the final); trim the LLM prompt; faster app activation; cache the page map per tab until the DOM changes.
3. Soak: 2 h with always-listening on while Bhavya works normally (YouTube, a call, Fusion). Count false actions (target: 0 risky, ≤ 1 safe), memory growth (< 50 MB), handle/thread leaks.
4. Final `README.md`: install, first run (extension load, Copilot key), what Nova can do by category (not phrase lists), troubleshooting. Leave `docs/prompts/README.md` as is.

## Report
Before/after latency table, soak numbers, known issues.

Commit `stage 11: speed + soak`.
