# Stage 11 — Speed + soak

**Read:** CLAUDE.md, SPEC §15, §16, §18, §20; all of GOTCHAS. Needs everything before it.
**Goal:** every latency budget met, shown with numbers from real use; Nova survives a normal day.

**The rule of this stage:** measure → fix the biggest → measure again. No fix without a before-number from his logs, and nothing "likely" is kept on faith. The logs are private and stay on the reference laptop, so the stage is built there; reports carry counts and timings, never what was said.

What is already there (2026-11-13): `nova latency` prints start p50 / p95 per command type and per tier, from `logs/traces.jsonl`. Each command's trace has `wait_ms`, `parse_ms`, `ctx_ms`, `page_ms`, the LLM's timings, `start_ms`, `exec_ms`, `total_ms` and `steps[]` with `ms` and `method`; each utterance has `final_ms`. `nova/cpu.py` measures CPU by cycle counts. Warm Tier 1 was 418 / 669 ms (p50 / p95) on 2026-11-13.

## Do
1. **`nova latency` grows up.**
   - One row per line of the SPEC §16 table: budget, p50, p95, n, pass / fail — island reacts, live-words lag, Tier 0 start, skill replay start, Tier 1 start (GPU / CPU), session clause (Tier 0 / Tier 1, queue wait apart), cold first request, page click, UIA click, numbers shown, dictation segment, handoff sent. A budget line with no trace field yet gets one. Rows with n < 20 say "too few".
   - `--slowest 10`: the per-hop breakdown of each. `--since 7d`.
   - **A hop nobody logs can't be fixed.** For 20 random commands, check that the hops add up to the total. A gap over 50 ms that no field explains is the first thing to find.
2. **Fix the top 3**, ranked by (p95 − budget) × how often that path runs. Candidates, each to be proven before it is kept:
   - start the context snapshot and the preload at speech start, not at the final;
   - run Tier 0 on stable partials so an obvious command is already parsed when he stops (it still runs only on the final);
   - the prompt — count its tokens; the page rows are the part that varies;
   - app activation — `steps[].method` says which escalation worked. If it is the third one every time, the first two tries are wasted time;
   - cache the page map per tab until the DOM changes (a mutation flag in the content script, armed only after a map was asked for);
   - the endpoint — 300 ms of silence sits inside every number (see 3).
   Keep a fix only if its row improves, no other row gets more than 5 % worse, and `nova eval` is unchanged.
3. **Session speed candidates** (left open by 07b). They need real session audio: with his OK, record three of his demo runs into `fixtures/session-audio/` (git-ignored).
   - Act on words that are stable across two partial decodes. Needs word timestamps, and a way to take words that already ran out of the final.
   - A learned end-of-turn check in place of the fixed 300 ms (Smart Turn v3, ~8 MB, CPU — check the current version and its licence).
   Keep one only if clauses start ≥ 150 ms sooner at p50 with no extra cut-offs across his script. Otherwise delete it and say so.
4. **Soak.** `nova soak start | stop | report`: every 60 s one line to `logs/soak.jsonl` — CPU of the process tree (cycle counts), private bytes, handles, GDI and USER objects, threads, VRAM, hook reinstalls, mic reopens, queue depth, whether the model is loaded.
   - Run A: 2 h with always-listening on while he works normally (YouTube, a call, Fusion).
   - Run B: 30 min of talk sessions with a video playing and someone else talking.
   - **Actions he didn't ask for:** `nova soak report` lists every command that ran without the key or the name (time + action type); he marks the ones that weren't his. Target per run: 0 risky, ≤ 1 safe.
   - **Resources:** memory growth < 50 MB after the first 10 minutes; handles, threads and GDI objects flat; no hook loss left unrepaired; idle CPU < 2 %.
   - Other people get transcribed during a soak. Tell them, or set `logs.transcripts: false` for the run — the counts still work.
   - Where leaks hide in this code: COM pointers in the ctypes modules (UIA, the audio detector, app activation) — every path must release; clipboard memory; one `threading.Timer` per notice; the llama-server child across reloads; QML objects for chips and number tags; the content script's element map.
5. **`tier0.py`** (~1,300 lines, `parse()` ~100): split it only if it slowed this stage's own work. By verb family, `nova eval` and `tests/test_tier0.py` green before and after, and no behaviour change in the same commit.
6. **Final `README.md`:** install (`scripts\setup.ps1`), first run (load the extension, the Copilot key or the chord), what Nova does by category — generated from the action list like "what can I say?" (07c), never a phrase list — settings, privacy (what stays on the laptop, what the logs hold, how to turn transcripts off), troubleshooting from GOTCHAS and `nova doctor`. Update the status table in AGENTS.md. Leave `docs/prompts/README.md` alone.

## Report
A before / after table per budget line. The three fixes with their numbers. What was tried and dropped. Both soak runs (resources + the unasked-action list, types only). Known issues.

Commit `stage 11: speed + soak`.
