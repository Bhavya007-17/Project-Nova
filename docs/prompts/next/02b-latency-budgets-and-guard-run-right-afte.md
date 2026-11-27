# Prompt 2b: Latency budgets and guard (run right after Prompt 2)


```
Goal: stop latency from growing as features land, and cut the slow tail now. README numbers: Tier 1 first action p50 1,670 ms but p95 6,958 ms; GOTCHAS says warm Tier 1 is ~418 ms p50 / 669 ms p95 through llama-server, and cold loads take 7-15 s. So the tail is mostly cold starts, evictions and reloads, not the model's thinking time. Fix the tail first.

1. Measure where time goes:
   - Add timing spans to every trace: vad_end, stt_final, gate, tier0, prefetch, retrieve, llm_queue, llm_load, llm_prefill, llm_decode, validate, confirm_wait, action_start, action_done. One trace = one waterfall.
   - nova latency --breakdown prints p50/p95 per span and per path (Tier 0, Tier 1, click, dictation, task step), and lists the 20 slowest traces with their dominant span.
   - nova latency --replay <log>: replays logged utterances through the brain with fake hands, so latency can be measured without the mic and run in CI.

2. Budgets as data: config/latency_budgets.yaml with p50/p95 per path and per span, set from today's measured baseline (not typed-in guesses), plus a tolerance. CI runs the replay and fails on regression. Each later stage adds its own span and budget.

3. Kill the cold tail:
   - Classify every slow Tier 1 trace: cold load, model reload (num_ctx or options mismatch between callers, see GOTCHAS), eviction by another model, CPU fallback, VRAM pressure from other GPU apps, queueing behind another request, or long decode.
   - Keep exactly one set of load options per model across planner, picker, rewrite and corrector, enforced by one shared loader (a test fails if two callers differ).
   - Keep the planner warm while Nova is in use: warm it at startup and on first partial transcript (prefix-only warm-up request, already measured in GOTCHAS), keep_alive long during sessions, release it when idle per the existing design.
   - Never load a second LLM next to the planner on the 8 GB GPU. The corrector (nova-fix) and rewrite share the planner model or run on CPU.
   - If the GPU model is cold or evicted and the request is simple, answer from the CPU 2B or ask, rather than waiting 7-15 s silently; show "Warming up…" with Esc as today.

4. Make the warm path faster:
   - Static prefix byte-identical across requests (prefix cache hits); log cache hit rate and prompt tokens per request.
   - Try llama-server speculative decoding: draft-free n-gram modes first (plans copy UIA row names and app names from the prompt), then a small draft model if it fits. Accept only if outputs are identical with the grammar on.
   - Quantization sweep (Q4_K_M vs Q5_K_M vs Q6_K): accuracy and latency together.
   - Start on partials: Tier 0 and prefetch already react to partial transcripts; extend so an obvious command can start before the final if the partial is stable for N ms (N fitted from logs), with a cancel if the final differs.
   - Stream the plan: start step 1 as soon as it is complete and valid in the JSON stream, while later steps decode (safe only for non-risky steps).

5. Keep new features off the hot path:
   - Second-opinion ASR runs only on low-confidence utterances, in parallel, with a deadline; if it misses the deadline the first result wins.
   - Dictation correction runs after the text has landed and swaps in place; never delays landing.
   - Memory/skill retrieval: in-process index, budget ~10-20 ms; skip if it misses.
   - Logging and learning happen on a background thread.

6. Soak: a 2-hour replay plus idle test (Stage 11 goal) tracking p95 drift, memory growth and VRAM; it must show no upward trend.

Done when: the breakdown and replay tools exist, budgets are in config and enforced in CI, Tier 1 p95 is reported before/after with the top tail causes fixed, and no path's p50 got worse.
```
