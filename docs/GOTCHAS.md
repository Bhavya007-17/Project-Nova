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
- **Mic-in-use detection:** the registry `ConsentStore\microphone` timestamps stopped updating on Windows 11 25H2. Use
  WASAPI capture sessions (`IAudioSessionManager2`, state Active + another pid) — see `nova/detect.py`.

## Audio and speech-to-text
- **Parakeet TDT 0.6B v3 int8 sometimes returns `""` for clear speech.** Re-decoding with 0.3 s of silence padded on
  both sides recovers ~70 %; `nova/stt/parakeet.py` retries once.
- **Name mishearings are normal:** "Chrome" → "foam"/"home", "Claude" → "Claire", "close the tab" → "clothes the stab".
  Tier 0 offers sound-alikes only for names in `config/aliases.yaml` and *asks* in keyed sessions; always-listening
  stays strict and ignores them. Never "fix" this with phrase lists.
- **Live mic tests transcribe whoever is talking nearby** into the console and `logs/traces.jsonl`
  (`logs.transcripts: true` by default). Prefer `nova listen --wav <file>` / `nova bench-stt --live N`; if you must use
  the live mic, don't print transcripts and delete the trace lines your test created.
- **Idle CPU:** measure with cycle counts (`nova/cpu.py`, `QueryProcessCycleTime`), not `GetProcessTimes` (15.6 ms tick
  billing misreads 32 ms audio callbacks). Thread wake-ups dominate idle cost. Mic noise suppression makes "quiet" swing
  −96…−58 dB, so the energy gate uses p80 of the last 3 s + 12 dB, clamped −75…−48 dB
  (`audio.sensitivity: high`, or the tray's "Hear me better", opens it further for quiet voices / far mics).
- **A fixed end-of-speech silence is always wrong for someone:** short cuts off "open chrome and … go to canvas",
  long makes every command feel slow. `nova/pause.py` reads the live partial: a sentence that looks finished ends at
  `finished_silence_ms` (450), one that trails off ("and", "to", "the" …) waits the long silence. Dictation only ever
  ends *sooner* from this, never later.
- **The wake name is misheard as often as app names** ("Noah", "Nova's", "no va"). `gate.loose_wake` accepts close
  sound-alikes at the start of an utterance only; `strip_wake` stays exact so dictated text is never eaten.
- **Not verified on a real laptop yet (written on a Mac, unit tests only):** smart endpointing, loose wake, the
  sensitivity switch, learned skills and the Claude/ChatGPT handoff. Measure with `nova latency` and
  `nova listen --gate` before trusting any number here.
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
- **~0.75–0.85 s fixed overhead per request** (Ollama re-opens the model per call) — about half of Tier 1's time.
  Bypassing it means talking to `llama-server` directly: a new runtime, so ask first.
- **qwen3.5 is hybrid-recurrent:** it can only resume from context checkpoints. A prefix-only warm-up request
  (`num_predict: 1`) cut CPU prompt eval from 2.6 s to 0.3 s.
- qwen3.5's Modelfile sets `presence_penalty 1.5` / `repeat_penalty 1.1` even at temp 0; Nova overrides them
  (`nova/brain/llm.py`). Compact few-shot JSON keeps grammar decoding at full speed.
- Ollama ignores the Intel Arc iGPU unless `OLLAMA_IGPU_ENABLE=1`. Re-run `nova bench-llm` after any Ollama upgrade.

## Learned skills and handoff
- **Skills match on words, not embeddings:** content words in order, long words may be a near-miss (rapidfuzz ≥ 88),
  digits and short words must be exact. An embedding model would add ~100 MB and a second model in RAM for little gain
  at this size. Phrases that point at the screen ("this", "that one", "the third") are never learned.
- Skills never run in always-listening (strict) mode, and every replayed step still goes through `risk.py`.
- **Handoff is keyboard only:** open the app, type the prompt, Enter. It assumes the app opens with its prompt box
  focused; if a release changes that, the words go nowhere. No watcher: Nova does not know when the job is done.

## Chrome extension
- **Branded Chrome 137+ ignores `--load-extension`**: loading `extension/` unpacked is a one-time manual step.
  Chrome for Testing (Playwright's copy) still honours it (+ `--disable-features=DisableLoadExtensionCommandLineSwitch`,
  temp `--user-data-dir`).
- CfT reads native hosts from `HKCU\Software\Google\Chrome for Testing\NativeMessagingHosts`:
  `nova chrome --install --browser testing`. `extension/test/selftest.py` does all of this; quit the tray app first
  (it owns the pipe). Extend the self-test instead of asking a human to load things.

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

- **The suite off Windows:** `conftest.py` stubs the Win32 DLLs so the pure-logic tests run on macOS / Linux.
  22 tests still fail there (dictation session, cpu counters, log rollover, one path test) and `test_island_qt.py` is
  skipped; all of them need real Windows. CI on Windows is the truth.

## Working on someone's real laptop
- Anything that sends keys, pastes or takes focus goes to whatever window is in front. Run focus-free checks first,
  batch the focus-taking ones, and ask the person once ("~2 min of your screen?") before running them.

## macOS (`nova/macos/`, measured on a MacBook Air M3, 8 GB, macOS 26.6)
- **How the port works:** `nova/__init__.py` installs an import redirect on a Mac: `nova.actions.native`, `apps`,
  `input`, `system`, `windows`, `uia`, `nova.hook`, `session`, `detect`, `console`, `cpu`, `startup` and
  `nova.chrome.install` load from `nova/macos/` (same public API). The brain, executor, dictation and Chrome code are
  shared. `conftest.py` sets `NOVA_WIN_STUBS=1`, so tests on a Mac still exercise the Windows modules; shared code
  that branches by OS checks `nova.platform.MAC`, not `sys.platform`, for the same reason.
- **iCloud Drive hides files in dot-folders** (Desktop & Documents sync). Python 3.12 skips hidden `.pth` files, so
  `import nova` fails, and Qt skips hidden plugins ("Could not find the Qt platform plugin cocoa"). Fix:
  the venv lives in `.venv.nosync/` (iCloud ignores `*.nosync`), `.venv` is a symlink to it, then
  `chflags -R nohidden .venv.nosync`. `scripts/setup_mac.sh` does this.
- **python.org's Python has no CA certificates** until "Install Certificates.command" runs: `get_models.py` falls back
  to `/etc/ssl/cert.pem` (downloads are still SHA-256 checked).
- **Permissions:** Accessibility for the app that runs Nova (Terminal / iTerm / VS Code) covers the event tap, window
  moves, focus reads and typing. Without it `nova doctor` shows the Accessibility and Keyboard hook rows red.
- **The system-wide AX element fails** (`AXFocusedApplication` → kAXErrorCannotComplete) for processes started from a
  terminal on macOS 26: ask the frontmost app's own element instead (`native.front_app`).
- **NSWorkspace / NSRunningApplication go stale without a run loop** (`nova say`, any CLI run): `frontmostApplication`,
  `runningApplications`, `isTerminated` and `isFinishedLaunching` keep their first answer. Ask the window server
  (CGWindowList z-order, owner pids) and `os.kill(pid, 0)` instead; `activateWithOptions` itself works.
- **Mac apps launch with no window** (Safari) and some call their main window `AXDialog` (WhatsApp, Catalyst):
  `open_app` "reopens" an app that finished launching windowless; a dialog with a minimize button is a main window.
- **Minimum sizes:** WhatsApp won't go below 800 px, wider than half of a 1512-pt display; snapping keeps its size
  and pins it to the outer edge instead of failing.
- **Chrome / Electron build their AX tree only when asked:** `uia._focus` sets `AXManualAccessibility` once per app.
- **Hwnd = CGWindowID**, mapped to the AXUIElement with the private `_AXUIElementGetWindow`. `Win.exe` is the .app
  path, `aumids` the bundle id; `exe_name` says `chrome.exe` for Chrome (and `explorer.exe` for Finder) so the shared
  Chrome / risk code keeps working.
- **Keys:** plans use Windows chords; `macos.input.mac_keys` maps them (Ctrl+T → Cmd+T, Alt+Left → Cmd+[, F5 →
  Cmd+R, Ctrl+Tab stays). The chord `ctrl+alt+n` is **Control + Option + N**. The event tap never swallows modifier
  changes (a swallowed Control release leaves apps thinking it is held). There is no Copilot key.
- **"Close <app>" quits it** (Cmd+Q semantics, `NSRunningApplication.terminate`); "maximize" fills the display's
  visible area (Mac zoom is per-app); "full screen" is the native AXFullScreen Space.
- **Local LLM on 8 GB:** qwen3.5:4b doesn't fit comfortably next to the apps; the governor falls back to
  qwen3.5:2b-q4_K_M on the GPU ("metal", not `num_gpu: 0`: shared RAM, CPU offload frees nothing). Reading the
  ~2.9k-token prefix cold takes ~11 s on the M3 GPU vs ~0.1 s cached, so Tier 1 warms once per process before its
  first plan; warm plans take ~2 s. The 2b model is noticeably weaker (left/right swaps): 16 GB Macs should pull 4b.
- **Mic:** PortAudio's Core Audio default is read when the stream opens; a default-device change is not noticed
  until the mic reopens. No "which app has the mic" / speaker-peak API on macOS: `macos.detect` reports nothing.
- **Screenshots** need Screen Recording permission; use `scripts/island_screenshots.py` (Qt's own frames) instead.
