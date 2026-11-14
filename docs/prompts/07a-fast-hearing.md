# Stage 07a — Fast + hearing

**Read:** CLAUDE.md, SPEC §4, §5, §7.4, §11, §16; GOTCHAS "Local LLM" and "Audio and speech-to-text".
**Goal:** Nova reacts every time he speaks to it, and a loosely worded request answers in about a second — never "took too long".

Everything below was measured on the reference laptop on 2026-11-11 (logs 11-02 → 11-09: 7,107 utterances, 314 accepted). Re-measure before and after; put both in the report.

The logs are private and stay on that laptop, so this stage is built there. Tests reproduce each logged case with its timings and made-up words — never a real transcript.

## Build
1. **One model, no thrash.** The planner (`tier1.py`, `num_ctx` 4096) and dictation cleanup (`cleanup.py`, 2048) reload the same model on every switch (10–27 s each). One shared client + one set of load options. `Cleaner.ready` goes stale when Ollama unloads: check what is loaded before a job. `nova/dictation/session.py:415` cancels before it checks `done`, so late fixes are logged as "failed".
2. **A cold start never fails.** Cold load is 15 s, the timeout is 8 s, and the abort kills the load. Split loading from answering: a cold request waits for the load (island "Warming up…"), the timeout covers generation only, an in-flight load is never aborted. `nova say` warms the same way as the tray app.
3. **Runtime bench, adopt if faster.** Warm calls take 1.8–3.2 s; ~0.8 s is Ollama's per-request overhead. `nova bench-llm --runtime ollama,llama-server` on the same GGUF, prompt and schema. If llama.cpp's server is clearly faster (p50 ≥ 30 % better), add it behind the existing client interface: child process on 127.0.0.1, started on demand, stopped after `keep_alive`, governor still picks GPU / CPU, `llm.runtime: auto | ollama | llama-server`, Ollama stays the fallback. Pinned download in `scripts/`, one line in DEPS.md. (A new runtime: Bhavya approved it on 2026-11-11, on the condition that it measures faster.)
4. **Output speed.** Schema-constrained JSON comes out at 15–20 tokens/s on the RTX. Find out why (grammar cost vs model) and fix it (flatter schema, shorter plans, or validate-then-retry instead of a grammar). Target: Tier 1 warm p50 ≤ 1.0 s, p95 ≤ 1.5 s on GPU.
5. **Hearing fixes** — a unit test for each, built from the logged case:
   - A Confirm / Clarify answer counts only from an utterance that began after the question appeared and is ≥ 400 ms long. After a key release, ~250 ms of quiet before a new utterance may start. (12.6 % of all finals are a bare "yeah / okay / yes"; one answered a confirm 262 ms after it appeared.)
   - The wake name counts within the first 3 words; fillers before it are skipped ("um, Nova, open Chrome"). Default spellings from the logs: "no va", "noba", "noma".
   - "Nova" … pause … command: the wake-only final claims the utterance already in progress (≈ 7 % are dropped as "busy" today).
   - Speech that passes the gate while Nova is busy (follow-up, always-listening) is queued and runs next, not dropped (34 dropped today). "stop" / "cancel" while busy aborts the running action. The key or the name still interrupts and replaces it, as today.
   - While the speakers play: media and volume commands always pass, and in the follow-up window the safe actions pass; typing, clicks, closing and handoff still need the name or key (SPEC §5). 287 rejects today, "stop" and "pause" among them.
   - The follow-up window is judged by when he started speaking, not when the text arrived.
   - The energy gate keeps Silero's state across skips (soft speech at −30 dB: 28–30 of 134 fixture files missed with the gate, 9–10 without).
   - The padded empty-output retry runs on finals only (it runs on partials today: live stt p95 800 ms vs 339 ms in the bench).
   - Outside a session, an utterance with no key is capped at 10 s (148 finals ran to the 30 s cap on room chatter).
6. **Always-listening on the real Tier 0.** Replace the Stage 02 `StubResolver` (`gate.py`, its own copy of the verb tables) with strict Tier 0. Bare media words ("next", "pause", "skip") count while media is the target. It accepted 5 of 7,050 decisions. The gate stays pure: the resolver is handed a catalog + running-apps snapshot, it doesn't fetch one. No Tier 1 for speech nobody addressed to Nova (SPEC §5).
7. **Name biasing — measure, then decide.**
   - **A real test set first.** Computer voices don't make his mishearings and the logs hold text only. `nova record-set` shows 30 short commands built from his own aliases and apps (half of them starting with "Nova, …"), records each to `fixtures/command-set/` with its expected text. Git-ignore that folder. Bhavya agreed on 2026-11-11 to read them once (~3 min) — ask him when it is ready.
   - **Baseline** with today's decoder: app-name errors, wake-name misses, empty outputs, p50 / p95.
   - **Candidate:** the same Parakeet v3 int8 through sherpa-onnx with hotwords generated from `aliases.yaml`, the catalog and `vocab.yaml` (hotwords in the model's own casing). It needs the model in sherpa's layout — a second ~640 MB download, approved together with the test.
   - **Adopt** behind `transcribe(pcm) -> text` only if app-name errors at least halve (and by ≥ 3 in absolute terms), p95 rises ≤ 100 ms and empty outputs don't rise. Otherwise delete the download. Report the numbers either way.
   - The same set answers the open wake-name question (SPEC §4): if "Nova" is missed more than 1 time in 15, say so in the report.
8. **Two safety holes found in the audit.** `press` has no password-field guard (Ctrl+V into a password box). `chrome.py` brings Chrome forward while resolving a click, before the risk check.
9. **Finish Stage 07's cleanup.** With 1–2 fixed it runs live; add the output guards it lacks (more words than heard + 1/8, changed negations, fewer than 1/3 of the words kept, more than 3 unfamiliar new words → rules-only text).

## Verify yourself
`uv run pytest`. `nova bench-llm` and `nova latency` before / after. Replay the logged rejects through the gate (counts only, no transcripts in the report). 20 loose phrases through `nova say --dry`, cold and warm: 0 timeouts.

## Live demo (Bhavya, 3 min)
1. Right after start (model cold): a loosely worded command → "Warming up…", then it acts. Again in other words → about a second.
2. Music playing: "pause" works with no name.
3. "um, Nova, open Chrome". Then "Nova" … pause … "new tab".
4. Two commands back to back ("open Notepad", straight away "maximize it") → both run.

Commit `stage 07a: fast + hearing`. Report (before / after table).
