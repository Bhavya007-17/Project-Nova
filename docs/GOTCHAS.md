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
- **sherpa-onnx hotwords on Parakeet TDT (2.0.1)** work only with `modeling_unit="bpe"` plus a SentencePiece vocab,
  which the export lacks: `nova/stt/sherpa.py` writes one from `tokens.txt` (score = −id). With the default
  "cjkchar" every name fails to encode (stderr says so; decoding goes on unbiased). Boost 2.0 fixed names on synthetic
  speech where 1.5 didn't. Boosting every installed app (180) raised WER on "check" from 14.4 % to 19.0 %; his own
  names only (32): 14.4 % vs 13.7 % greedy onnx-asr. Its wheel brings its own `onnxruntime.dll` (1.28); it loads
  next to `onnxruntime` 1.31 in either import order (the Python package links ORT into its own `.pyd`).
  2026-11-13, "check" session through Nova's own path (segmented, 40 recordings): sherpa + hotwords 15.7 % WER,
  p95 196 ms; Parakeet as shipped 14.4 %, p95 156 ms; no empty outputs on either. Not adopted on that — the decision
  waits for `nova record-set` (his voice, his app names): `nova bench-stt --set fixtures/command-set --backend both`.
- **A Silero restarted from zero after every skipped frame misses soft speech.** The energy gate skips frames in the
  middle of quiet words. 134 fixture recordings at −30 dB: 14 with no speech start when `skip` reset the state, 7
  when it keeps it — the same 7 as with no gate at all (normal level: 4 → 3). The gate still skips ~55 % of frames.
- **The padded empty-output retry belongs on finals only.** On partials it doubled the decode and held up the next
  one: live stt p95 800 ms against 339 ms in the bench. `transcribe(pcm, retry_empty=False)` for live words.
- **Judge the follow-up window by when he started speaking.** The text arrives 0.3–2 s after he stops; judged by
  arrival, a command begun inside the 8 s window was rejected. Speech that began before the window opened is not a
  follow-up either (chatter from before the command).
- **Un-addressed speech is capped at 10 s** (no key, no session): room chatter ran to the 30 s cap 148 times in a
  week, each one a 30 s decode. A key or the name lifts the cap, also mid-utterance.
- **Replay before changing the gate.** A week of logged decisions (7,050) through the Stage 07a gate: accepted
  314 → 326 (follow-up 9 → 14, name 110 → 116, always-listening 5 → 6), none that ran before is rejected now. The
  throwaway script rebuilds each situation from the logged `via`; counts only, never the text.
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
- A `ScaleAnimator`'s `item.scale()` reads 1.0 from Python until the animator ends (it animates on the render
  thread). An offscreen `QQuickView` does run Animators: test the end value, not the middle.

## Talk sessions (07b)
- **`TalkChanged(paused)` goes out before the clause's `Gated`** (the clock pauses, then the state moves, then the
  brain gets the clause). Anything that keys off `Gated` sees a paused clock already.
- **Tier 0 heard "great" as the verb "write"**: "Great great. Okay." became "Type 'great. Okay'?" until
  acknowledgements were kept out of its verb guess. Every ack word must stay out of the guessers.
- **`nova eval --sessions`** (2026-11-14, qwen3.5:4b on the GPU, 7 sessions / 38 lines): 34/41 clauses right
  (82.9 %), 1 action on 10 small-talk lines — "hold on" went to Tier 1, which opened Calculator again (the session
  context said "launch the calculator: Opened Calculator"). On his Start menu "the notes app" is Sticky Notes to
  Tier 1, and the next clause then builds on Sticky Notes. "go ahead and minimize this window" splits after
  "go ahead" (a clause of its own, which Tier 1 acted on).
- **SAPI pauses at commas long enough for the 300 ms talk endpoint.** Replay of the recording (28 s WAV): "Alright,
  can you open up the notes app…" came out as two finals and "Cool, thank you. That's all." as three. Harmless
  there (the ack pieces drop) — expect the same from real speech with pauses at commas. Every final landed
  321–482 ms after the speech ended.
- **Replay timings, fake hands** (same run): end of clause → action start p50 323 ms on Tier 0 (3 clauses), 1,006 ms
  on Tier 1 (4); nothing queued ahead of any of them. The fake hands take ~0 ms, so a real queue wait is longer by
  the steps ahead.
- The session context costs ~12–13 prompt tokens per `before` entry on qwen3.5:4b; five add ~64 tokens and
  10–16 ms. llama-server's `prompt_tokens` counts only what isn't cached: 42–82 per Tier 1 clause in the replay,
  the static prefix not included.

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
- **Ollama 0.30.10 rejects `keep_alive: "-1"` as a string** (HTTP 400 "missing unit"): keep a model for a whole
  talk session with `"24h"`.
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

## Control anything (07c)
- **Chromium builds its accessibility tree only when asked.** The first UIA snapshot of a Claude / Chrome / VS Code
  window from a fresh process held only Minimize / Maximize / Close; the same window seconds later had 199 elements,
  131 clickable. `uitree.warm_snapshot` (numbers, click by name, PDF fit width) re-asks every 150 ms for up to 1 s
  while a `Chrome_WidgetWin_*` window shows ≤ 3 clickable elements.
- **The subtree cache costs ~0.6 ms per node** (Chrome 202 nodes 142–168 ms, Claude 475 nodes 210–271 ms, Explorer
  ~110 ms). Win32 controls reached through the MSAA proxy cost ~11 ms each: a window with 300 classic buttons takes
  3–4 s by any method. OCR of a window: grab 50 + bitmap 20 + recognize 76 ms.
- **This laptop has no S3 sleep, only Modern Standby** (`GetPwrCapabilities`: S3 false, AoAc true). There is no call
  that enters Modern Standby, and `SetSuspendState` there hibernates — "go to sleep" turns the screen off
  (`SC_MONITORPOWER`), which is how standby starts.
- **`open_path` checks at the sink, not only in the planner:** the path must exist, sit under one of his known
  folders (resolved through `SHGetKnownFolderPath`, so OneDrive moves follow) and not be something that runs
  (`risky`: .exe/.bat/.ps1/.lnk… are refused, folders are fine).
- **Tier 1's static prefix grew 3,313 → 3,743 tokens** with the 07c rules and examples (qwen3.5 tokenizer, before
  his aliases), ~3,900 with them; `NUM_CTX` went 4096 → 6144 so a 40-row page map, a talk session's `before` and
  the answer still fit. Every caller must send the same `num_ctx`, or Ollama reloads the model.
- **Greedy number words in Tier 0 regexes:** "drag five over to nine" read "five over" as the first number. The
  first number group is lazy (`??`) and "over" optional.
- **Eval rows must not repeat a Tier 1 prompt example** (a test checks): the model has the answer in front of it,
  so the row measures nothing.
- `nova numbers --dump [--title T]` saves what "show numbers" would tag (JSON) and the window's pixels (PNG) to
  `data/`, nothing clicked. A window that isn't in front shows whatever covers it in the picture.
- **Chrome's PDF viewer (measured live, CfT 149):** `#page=N` on the URL is ignored once the PDF is open; the
  viewer's UIA subtree arrives ~1 s after the page; the fit button is a toggle (a second "fit to width" undoes the
  first, so read its state); with the omnibox focused its dropdown covers the toolbar (Esc first); UIA `SetFocus`
  on the page box reports success while keyboard focus stays put — click it for real and check; Left / Right
  scroll sideways when zoomed, so page turns go through the page box (its UIA value is the current page, ±1).
- **Win11 Notepad restores unsaved tabs:** closing the window (WM_CLOSE) keeps them, no dialog, and they come back
  next start (`LocalState\TabState`). Ctrl+W on a dirty tab is what asks to save — an in-app XAML dialog inside the
  main window, which Tier 1 never sees. A bare button name ("don't save") is matched against the front window's
  buttons before Tier 1 (`marks.button_named`, tier "screen").
- **Tab keys follow the window in front:** "close the tab" with Notepad / Explorer / Terminal in front went to
  Chrome (or failed "Chrome isn't open"). A front window with UIA tab items gets the keys; Windows Terminal needs
  Ctrl+Shift+W / T (Ctrl+W deletes a word in the shell).

## Chrome extension
- **Branded Chrome 137+ ignores `--load-extension`**: loading `extension/` unpacked is a one-time manual step.
  Chrome for Testing (Playwright's copy) still honours it (+ `--disable-features=DisableLoadExtensionCommandLineSwitch`,
  temp `--user-data-dir`).
- CfT reads native hosts from `HKCU\Software\Google\Chrome for Testing\NativeMessagingHosts`:
  `nova chrome --install --browser testing`. `extension/test/selftest.py` does all of this; quit the tray app first
  (it owns the pipe). Extend the self-test instead of asking a human to load things. CfT 149 also started the host
  registered under `Google\Chrome`, so a test harness sets `NOVA_BRIDGE_FILE` to make whichever host starts read
  its bridge.

- **Chrome's built-in PDF viewer can't be scripted.** The content script finds nothing scrollable and reports
  `ok: true, moved: false`, which Nova reads as "Already at the bottom". Send real keys or the wheel to the focused
  PDF instead (07c does; see "Chrome's PDF viewer" above).

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
- Bare `python` in Git Bash can be the Microsoft Store stub: it hangs instead of failing, and `a || b` never reaches
  `b`. Always `uv run python`. Give pytest a `timeout` when its output goes through a pipe.
- A git worktree has no `models/` (git-ignored): link it with a directory junction
  (`mklink /J models C:\dev\nova\models`), and remove the junction before removing the worktree.
- `nova eval --sessions --replay N` writes its WAV, SSML and traces to `.foreman/scratch/replay/`; SAPI speaks into
  the file, never the speakers. Set `PYTHONIOENCODING=utf-8` when redirecting its output (the console code page
  turns "…" and "≥" into "?").

## Working on someone's real laptop
- Anything that sends keys, pastes or takes focus goes to whatever window is in front. Run focus-free checks first,
  batch the focus-taking ones, and ask the person once ("~2 min of your screen?") before running them.
