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

## Added 2026-11-11
- The soak includes talk sessions: 30 min of sessions with a video playing and someone else talking. Count actions he didn't ask for (target: 0 risky, ≤ 1 safe).
- `tier0.py` is ~1,300 lines with `parse()` at ~100: split it only if it is slowing work down, and only with the eval green before and after.
- Session speed candidates, each measured on real session audio before it is kept: acting on words that are stable across two partial decodes (needs word timestamps and a way to take already-run words out of the final), and a learned end-of-turn check (Smart Turn v3, 8 MB, CPU) in place of the fixed 300 ms pause. Keep one only if clauses start ≥ 150 ms sooner at p50 with no extra cut-offs across his demo script.
