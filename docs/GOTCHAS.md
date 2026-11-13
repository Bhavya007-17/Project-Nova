# Gotchas

Hard-won facts from building Nova, measured on the reference laptop (see `AGENTS.md`). Each one cost real debugging
time. Read the section you're about to touch. **Found a new one? Add it here in the same PR** — agents' private
memories aren't shared, this file is.

## Windows input and focus
- **Copilot key** sends a real key-up (taps 112–190 ms, holds 1.0–1.4 s), so hold = push-to-talk works. F23
  auto-repeats ~30/s while held: ignore repeats. Events aren't marked injected.
- **Keyboard-hook budget (< 10 ms):** `SendInput` *inside* the `WH_KEYBOARD_LL` callback takes 4–20 ms (Windows runs
  every LL hook synchronously). Swallow the Win/Alt key-up after a trigger and replay mask-key + key-up from the hook
  thread's message loop instead. Swallowed keys never reach raw input, so the "hook dropped" watchdog counts only
  passed keys.
- **The island can never take focus** (it's `WS_EX_NOACTIVATE`; `SetForegroundWindow` fails). While it asks
  (confirm / clarify), the hook swallows Enter / 1–4 / Esc. Bringing *other* apps forward works with plain
  `SetForegroundWindow`; `nova/actions/native.activate` still escalates (unassigned VK `0xE8` → retry →
  `AttachThreadInput`) and each trace's `steps[].method` says which one worked.
- **Chrome's first UIA question lies:** the first `GetFocusedElement` on a fresh Chrome returns a Pane with
  `IsPassword` false; later ones say Edit + `IsPassword` true. Nova re-asks after 150 ms and treats unclear focus as
  unknown → asks before typing. Don't query UIA on every command (it turns Chrome's accessibility tree on): only before
  typing.
- **Synthesized Win+L does nothing** (Windows blocks it): lock with `LockWorkStation`. Print Screen isn't in Nova's
  key table yet.
- **Mic-in-use detection:** the registry `ConsentStore\microphone` timestamps stopped updating on Windows 11 25H2. Use
  WASAPI capture sessions (`IAudioSessionManager2`, state Active + another pid) — see `nova/detect.py`.

## Audio and speech-to-text
- **Parakeet TDT 0.6B v3 int8 sometimes returns `""` for clear speech.** Re-decoding with 0.3 s of silence padded on
  both sides recovers ~70 %; `nova/stt/parakeet.py` retries once.
- **Parakeet invents "Yeah." on tiny clips.** 12.6 % of a week's finals were a bare "yeah / okay / yes", and a Copilot
  key release often starts a new ~200 ms utterance. One answered a Confirm 262 ms after it appeared. Never take a
  yes from an utterance that began before the question or is shorter than ~400 ms.
- **Name mishearings are normal:** "Chrome" → "foam"/"home", "Claude" → "Claire", "close the tab" → "clothes the stab".
  Tier 0 offers sound-alikes only for names in `config/aliases.yaml` and *asks* in keyed sessions; always-listening
  stays strict and ignores them. Never "fix" this with phrase lists.
- **Live mic tests transcribe whoever is talking nearby** into the console and `logs/traces.jsonl`
  (`logs.transcripts: true` by default). Prefer `nova listen --wav <file>` / `nova bench-stt --live N`; if you must use
  the live mic, don't print transcripts and delete the trace lines your test created.
- **Idle CPU:** measure with cycle counts (`nova/cpu.py`, `QueryProcessCycleTime`), not `GetProcessTimes` (15.6 ms tick
  billing misreads 32 ms audio callbacks). Thread wake-ups dominate idle cost. Mic noise suppression makes "quiet" swing
  −96…−58 dB, so the energy gate uses p80 of the last 3 s + 15 dB, clamped −75…−45 dB.
- **Recordings:** in the private voicebar sessions only "check" is clean; "quiet"/"noise" start mid-word (~46 % WER that
  says nothing about STT). `expected_text` is the WER reference. Score WER after normalizing case and punctuation.

## Island (Qt Quick)
- **Use the "basic" render loop** (`QSG_RENDER_LOOP=basic`, set in `nova/island/window.py`): key → first frame
  10–19 ms vs 37–79 ms threaded.
- **Never run Python on the render thread.** A `DirectConnection` slot on `frameSwapped` deadlocked `grabWindow()` (GUI
  thread waits on the render thread while holding the GIL). Queued connections only.
- The island hides when idle (0 frames) and warm-starts the D3D device at launch (first show otherwise ~165 ms).
- **Screenshots don't see the island** (`WDA_EXCLUDEFROMCAPTURE`). Set `NOVA_CAPTURABLE=1` to check layouts, or use
  `scripts/island_screenshots.py` (grabs Qt's own frames).

## Local LLM (Ollama 0.30)
- **Never pick a `*-cloud` tag** — Ollama lists cloud models next to local ones. `is_cloud()` filters them.
- **`/api/chat` with `think: false` ignores `format` for qwen3.5.** Nova renders ChatML itself (+ empty
  `<think></think>`) and calls `/api/generate` with `raw: true` + `format` → schema enforced from token 1
  (`nova/brain/tier1.py`).
- **Ollama's API costs ~0.3 s per request and re-reads the whole prompt** (3,090 tokens every time on the GPU).
  The `llama-server.exe` it ships (`lib/ollama`), talked to directly with `cache_prompt`, reads ~50: warm p50
  437 vs 716 ms, p95 ~660 vs 954, cold 7.2–8.0 vs 8.5–10.6 s (2026-11-13, same GGUF / prompt / schema, 36 cases × 2).
  Nova runs it as a child (`nova/brain/llama_server.py`, `llm.runtime`). Re-measure after an Ollama upgrade:
  `nova bench-llm --runtime ollama,llama-server`.
- **That llama-server finds CUDA only with `lib/ollama/cuda_v13` on `PATH` and
  `GGML_BACKEND_PATH=…/cuda_v13/ggml-cuda.dll`** — without them it runs on the CPU and says nothing. The model is
  the first absolute `FROM` in `ollama show --modelfile` (the second is the vision projector: leave it out).
- **A child that holds 3 GB of VRAM must die with Nova, however Nova ends:** a job object with
  `KILL_ON_JOB_CLOSE` (checked: `taskkill /F` on the parent → child gone, VRAM back). `atexit` alone leaks it.
- **One command per process can't keep a child warm:** `nova say` uses Ollama when `llm.runtime` is `auto`
  (Ollama keeps the model between runs). With the tray app's model on the GPU, `nova say` may get the CPU model.
- **Ollama 0.30 can leave the "GPU" model on the CPU:** its fit reserved ~5.3 GB for qwen3.5's vision projector
  and put 0 of 33 layers on an RTX with 6.9 GB free — 11–20 tokens/s, warm calls of 1.8–3.2 s. Nova sends
  `num_gpu: 99` for the GPU choice (0 for CPU): 80 tokens/s, with or without the schema (the grammar is free).
- **qwen3.5 is hybrid-recurrent:** it can only resume from context checkpoints. A prefix-only warm-up request
  (`num_predict: 1`) cut CPU prompt eval from 2.6 s to 0.3 s.
- qwen3.5's Modelfile sets `presence_penalty 1.5` / `repeat_penalty 1.1` even at temp 0; Nova overrides them
  (`nova/brain/llm.py`). Compact few-shot JSON keeps grammar decoding at full speed.
- Ollama ignores the Intel Arc iGPU unless `OLLAMA_IGPU_ENABLE=1`. Re-run `nova bench-llm` after any Ollama upgrade.
- **A cold load outlasts the request timeout** (qwen3.5:4b on the RTX: 7–15 s; `llm.timeout_s` is 8), and dropping
  the request drops the load. Loading is its own step on its own client (`llm.Loader`): a cold request waits for
  it ("Warming up…"), Esc stops the wait, never the load; the timeout covers the answer only.
- **Two callers with different `num_ctx` make Ollama reload the model on every switch** (planner 4096 vs cleanup 2048:
  10–27 s each time in `nova.log`). One set of load options per model.
- **Warm Tier 1: 418 ms p50 / 669 ms p95 through llama-server, 765 / 1,159 ms through Ollama** (2026-11-13, GPU,
  196 eval calls in-process; 1.8–3.2 s before the two fixes above). Plans correct: 259/270 on both.
- **qwen3.5 resumed from a cached prefix isn't bit-identical to a full re-read:** a near-tie flips (one eval
  negative: p(act) 0.38 / p(ignore) 0.29 cached, 0.32 / 0.36 re-read). Don't chase single-case eval diffs
  between runtimes; compare totals.

## Chrome extension
- **Branded Chrome 137+ ignores `--load-extension`**: loading `extension/` unpacked is a one-time manual step.
  Chrome for Testing (Playwright's copy) still honours it (+ `--disable-features=DisableLoadExtensionCommandLineSwitch`,
  temp `--user-data-dir`).
- CfT reads native hosts from `HKCU\Software\Google\Chrome for Testing\NativeMessagingHosts`:
  `nova chrome --install --browser testing`. `extension/test/selftest.py` does all of this; quit the tray app first
  (it owns the pipe). Extend the self-test instead of asking a human to load things.

- **Chrome's built-in PDF viewer can't be scripted.** The content script finds nothing scrollable and reports
  `ok: true, moved: false`, which Nova reads as "Already at the bottom" (from the code, 2026-11-11 — not yet reproduced
  live). Send real keys or the wheel to the focused PDF instead.

## Dictation
- A RichEdit (what Notepad is built on) silently ignores Ctrl+V when the clipboard holds only `"\r\n\r\n"`: hold
  line breaks and paste them in front of the next words.
- Editors drop Ctrl+V when another process has the clipboard open: probe it (`_clipboard_free()`, ≤ 150 ms) before
  pasting. Don't auto-retry (a busy app may still process the first paste → double text).
- UIA TextPattern works on raw `RICHEDIT50W`, **not** on WinForms TextBox — don't test text features with WinForms.
- Google Docs draws on a canvas: only `.kix-cursor` / `.kix-appview-editor` are readable, so no text-before-cursor
  context and no in-place LLM fixes there.
- Test dictation without a mic: SAPI TTS to a 16 kHz WAV (`<break time="1300ms"/>` between sentences) + `WavFeed`
  replay + a raw Win32 RichEdit window (`GetModuleHandleW.restype = HMODULE`, or 64-bit handles truncate).

## Tooling
- **uv 0.11 makes `.venv\Scripts\pythonw.exe` a console exe** (uv #19226, fixed in 0.12) → `novaw` pops a console.
  `nova doctor`'s "No-console run" row catches it: `uv self update`, delete `.venv`, `uv sync`.
- `.venv\Scripts\python.exe` is uv's launcher: `Popen(...).kill()` leaves the real interpreter running. Use
  `taskkill /F /T /PID <pid>`.
- Bash heredocs eat backslashes on Windows (`"\\0"` became a NUL byte in a .py file): write files that contain
  backslashes with an editor tool, not heredocs.

## Working on someone's real laptop
- Anything that sends keys, pastes or takes focus goes to whatever window is in front. Run focus-free checks first,
  batch the focus-taking ones, and ask the person once ("~2 min of your screen?") before running them.
