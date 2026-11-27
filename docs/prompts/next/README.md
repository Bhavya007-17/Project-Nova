# Next stages: overview

Read only the stage file you are working on. Do not load all stages at once.

## How to run a stage

One-time setup:
1. This folder lives at `docs/prompts/next/` in the repo.
2. `AGENTS.md` contains the line `Standing rules: @docs/prompts/next/RULES.md`, so the rules load every session (check with `/memory`). No need to paste Prompt 0.

Every stage:
1. `/clear` or a new session.
2. Plan mode (Shift+Tab). Paste the stage's kickoff line from `NEXT.md` (or: "Read docs/prompts/next/<file> and the code it touches. Make a plan; don't edit yet.").
3. Review the plan (Ctrl+G to edit), then approve.
4. Ask for evidence: the stage's "Done when" numbers, before/after `nova eval` and `nova latency`, test output.
5. Live demo, commit, tick the box below, add new facts to `docs/GOTCHAS.md`, and update `NEXT.md` to the following stage.

If a stage file disagrees with the real code, trust the code and edit the stage file.

## Rundown plan

| # | Stage | What it fixes | Needs training? |
|---|---|---|---|
| 1 | Eval harness and fuzzy-bug protocol | Makes every later change measurable; tames flaky, non-deterministic bugs | No |
| 2 | Speech recognition fixes | Spurious "Yeah.", empty decodes, unsure words, unlimited speech | No |
| 2b | Latency budgets and guard | Slow Tier 1 tail (p95 ~7 s), regressions as features land | No |
| 3 | Names and fuzzy matching | "Claire" → Claude, "foam" → Chrome, learned aliases | No |
| 4 | Dictation misheard-word fix (#18) | "meat" → "meet" in place, without breaking correct text | No (optional LoRA later) |
| 5 | Planner schema, calibration, session fix (#19) | False actions, useless 0.85 confidence, context mixing | No |
| 6 | GUI task loop | ~48.5% per-step accuracy, early "done", repeated clicks, menus | No |
| 7 | Training module A: datasets | Builds clean, verified, multilingual, noise-robust training data with no APIs | Data only |
| 8 | Training module B: train `nova-brain` and adapt Ollama | Short prompt, fewer false actions, more languages | Yes, free compute |
| 9 | Adaptive capability (Stage 08) | Learned shortcuts, teach-by-showing, alias learning | No |
| 10 | Better language input and multilingual | European languages now, Hindi and Hinglish next | Optional |
| 11 | Mac version: notch island, haptics, touch | macOS port with a NotchNook-style island | No |

Order matters: stage 1 first, because stages 2 to 10 are each judged on its numbers. Stages 7 and 8 come after 2 to 6, because the logs and fixes from those stages become the training data.

## Model names (my naming, change freely)

| Name | Base model | Job | Where it runs |
|---|---|---|---|
| `nova-brain:2b` | Qwen3.5-2B, LoRA fine-tune | Tier 1 planner, CPU fallback and laptop-trainable | Ollama / llama-server |
| `nova-brain:4b` | Qwen3.5-4B, LoRA fine-tune | Tier 1 planner on GPU | Ollama / llama-server |
| `nova-fix` | Qwen3.5-0.8B or 2B, LoRA fine-tune (optional) | Dictation word picker for #18 | Ollama, same server |
| Fallback base | Qwen3-4B-Instruct | Use if Qwen3.5's hybrid architecture causes export or adapter problems | Same |

## Free tools only

| Need | Free option |
|---|---|
| Speech to text | Parakeet TDT 0.6B v3 (Windows: onnx-asr; Mac: FluidAudio CoreML), Whisper large-v3-turbo, Qwen3-ASR, IndicConformer |
| Planner inference | Ollama / llama-server with open weights |
| Training 2B | Your RTX 5070 Laptop (Unsloth, ~5 GB) or your Mac (MLX) |
| Training 4B | Kaggle (~30 GPU-h/week) or Colab free (T4, not guaranteed). If a quota runs out, wait for the weekly reset or split training into shorter runs that resume from a checkpoint |
| Synthetic data | Templates + a local open model through Ollama. No API teacher, by rule |
| Mac distribution | Unsigned build; users allow it once in System Settings > Privacy & Security > Open Anyway |

## Stage files

- `00-shared-context-paste-first-every-session.md`: Shared context (paste first, every session)
- `01-eval-harness-and-fuzzy-bug-protocol.md`: Eval harness and fuzzy-bug protocol
- `02-speech-recognition-fixes-never-restrict-.md`: Speech recognition fixes (never restrict speech)
- `02b-latency-budgets-and-guard-run-right-afte.md`: Latency budgets and guard (run right after Prompt 2)
- `03-names-and-fuzzy-matching.md`: Names and fuzzy matching
- `04-dictation-misheard-word-fix-18.md`: Dictation misheard-word fix (#18)
- `05-planner-schema-calibration-and-session-f.md`: Planner schema, calibration, and session fix (#19)
- `06-gui-task-loop.md`: GUI task loop
- `07-training-module-a-datasets-no-apis.md`: Training module A: datasets (no APIs)
- `08-training-module-b-train-nova-brain-and-a.md`: Training module B: train nova-brain and adapt Ollama
- `09-adaptive-capability-stage-08.md`: Adaptive capability (Stage 08)
- `10-better-language-input-and-multilingual.md`: Better language input and multilingual
- `11-mac-version-notch-island-haptics-touch.md`: Mac version: notch island, haptics, touch

## Progress

- [x] 00 shared context
- [ ] 01 eval harness and fuzzy bugs
- [ ] 02 speech recognition fixes
- [ ] 02b latency budgets and guard
- [ ] 03 names and fuzzy matching
- [ ] 04 dictation fix (#18)
- [ ] 05 planner schema and #19
- [ ] 06 GUI task loop
- [ ] 07 training datasets
- [ ] 08 train nova-brain, Ollama
- [ ] 09 adaptive capability
- [ ] 10 language input and multilingual
- [ ] 11 Mac notch island
