# Stage 05 — Local brain: loose phrasing → plans

**Read:** CLAUDE.md, SPEC §7, §15, §16, §18.
**Goal:** anything Tier 0 misses is understood by a small local LLM well under a second, with clarify and "Show me?" fallbacks — light enough to run next to Fusion.

## Build
1. `nova/brain/llm.py` — Ollama client: `/api/chat`, `format` = Plan JSON schema, `think: false`, temperature 0, `num_ctx` 4096, `keep_alive` from config. Preload on `PreloadHint`. Timeouts; clean errors to the island.
2. Static system prompt (action schema, SPEC §7.6 routing rules, payload-substring rule, ~10 examples) built for prefix caching; per-request message = transcript + compact context JSON.
3. `nova/brain/router.py` — Tier 0 → skills (stub) → Tier 1 → validate (schema, payload-substring rule, entities exist) → clarify if confidence < 0.6 → executor. Handoff plans go to a stub that shows "Would hand off to Claude: …" (Stage 09 implements it).
4. `nova/governor.py` — NVML free-VRAM check before loading; GPU or CPU model per SPEC §15; unload on idle; never two LLMs.
5. `docs/eval/intents.yaml` per SPEC §18 (~200 positives with expected action + key args, ~100 conversation negatives) and `nova eval` (accuracy by action, false-action rate, latency).
6. `nova bench-llm` — for each candidate (check current small tags in Ollama first: small Qwen instruct ~4B and ~2B, Gemma 4 E2B; qwen2.5:7b as the accuracy baseline) × device (GPU, CPU; Arc/Vulkan only if this Ollama build supports it): eval accuracy, p50/p95 latency, cold-load time, VRAM. Pick the defaults from the numbers → settings.yaml + a short table in DEPS.md.
7. Clarify flow: island question + numbered choices; answer by voice ("the second one", "chrome", "yes") or click.

## Verify yourself
`nova eval`: ≥ 90 % plans correct, < 1 % false actions on negatives. Tier-1 p95 ≤ 1.2 s on GPU. Simulate low VRAM via config → the CPU path works.

## Live demo (Bhavya, 3 min) — phrase it however you like
"can you get canvas up for me" · "make this window take the whole screen" · "shove chrome onto the left half" · "search youtube for lofi hip hop" · "I need a new tab" · something vague like "open the thing" → it asks instead of guessing.

Commit `stage 05: local brain`. Report the benchmark table and eval numbers.
