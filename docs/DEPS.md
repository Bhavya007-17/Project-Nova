# Dependencies

One line each: what, version, why. Ask before adding anything over ~50 MB or a new runtime.

## Runtime (Python 3.12 via uv)
- **pyside6-essentials** 6.12 (~78 MB wheel) — tray and the QML island (SPEC §6; Qt Quick, D3D11 on the Intel iGPU, "basic" render loop: 10-19 ms key -> first frame vs 37-79 ms threaded; ~70 MB RAM, ~40 of it the D3D11 device). "Essentials" skips the Addons wheel (WebEngine, 3D…) Nova doesn't use.
- **pyyaml** 6.0 — reads `config/*.yaml`; safe loader only, duplicate keys rejected.
- **sounddevice** 0.5 — PortAudio mic capture (MME Sound Mapper, raw float32 callback into a ring; measured cheaper than WASAPI auto-convert here).
- **nvidia-ml-py** 13.615 (`pynvml`) — free/total VRAM via NVML, so the LLM can pick GPU or CPU (SPEC §7.4, §15).
- **numpy** 2.5 (~12 MB wheel) — audio buffers; needed by onnx-asr and ONNX Runtime anyway.
- **onnxruntime** 1.31 (CPU, ~15 MB wheel) — runs Silero VAD (1 thread) and Parakeet (4 threads, spinning off). Telemetry events disabled at load.
- **onnx-asr** 0.12 (~4 MB, pure Python) — Parakeet TDT loader/decoder for the local int8 files; no hub extra, so it can't download.
- **rapidfuzz** 3.14 (~2 MB) — fuzzy spelling for the vocab fix-up; word-level Levenshtein for WER.
- **jellyfish** 1.3 (~0.2 MB) — metaphone sound keys for the vocab fix-up ("crow labs" ≈ "CroLabs").

## Windows APIs (ctypes, no new packages)
- **WH_KEYBOARD_LL + raw input** (user32) — Copilot key / chord / Esc (`nova/hook.py`); raw input (RIDEV_INPUTSINK) counts keys so a hook Windows silently dropped is reinstalled.
- **SendInput** (user32) — replays a swallowed Win/Alt release behind the unassigned key 0xE8 so Start / menu bars don't open; never inside the hook callback (measured 4-20 ms there).
- **SetWindowDisplayAffinity** (user32, WDA_EXCLUDEFROMCAPTURE) — screenshots and screen shares never show the island (`NOVA_CAPTURABLE=1` turns it off for layout checks). **DwmSetWindowAttribute** (DWMWA_TRANSITIONS_FORCEDISABLED) — no Windows fade on top of the island's own drop-down. Click-through (WS_EX_LAYERED | WS_EX_TRANSPARENT), no-activate and the input region (SetWindowRgn) come through Qt window flags / `setMask`.
- **Fonts** (built into Windows 11) — Segoe UI Variable (text), Segoe Fluent Icons (✓ ✗ ⚡ ⚠ mic).
- **MMDevice API** (COM: IAudioSessionManager2, IAudioMeterInformation) — "another app is recording" and "speakers playing" for the gate (`nova/detect.py`). SPEC's ConsentStore `LastUsedTimeStop` stopped updating on this laptop on 2026-10-28, for every app.
- **Start-menu catalog** (PowerShell `Shell.Application` AppsFolder: name, AUMID, shortcut target) — the app list (`nova/actions/catalog.py`), ~1 s, below-normal priority, at start and every 10 min; cached in `data/catalog.json`.
- **Window identity** (`GetApplicationUserModelId`, `SHGetPropertyStoreForWindow` → `System.AppUserModel.ID`, `QueryFullProcessImageNameW`) — which app a window belongs to, never by its title (Chrome: window AUMID; Claude / ChatGPT / Notepad: process AUMID; Fusion: same folder as its shortcut).
- **IApplicationActivationManager** (COM) — launches packaged apps (Claude, ChatGPT, Calculator…); other apps via `explorer shell:AppsFolder\<AUMID>`.
- **SetForegroundWindow** escalation — plain first; then Nova becomes the last input source (SendInput of the unassigned key 0xE8) and tries again; then `AttachThreadInput`. Each command's trace says which one worked.
- **SetWindowPos + DWMWA_EXTENDED_FRAME_BOUNDS** (dwmapi) — snaps and layouts land edge to edge (invisible resize borders compensated); per-monitor DPI aware (`SetThreadDpiAwarenessContext`), `GetDpiForMonitor` for moving between monitors.
- **SendInput** Unicode / key chords (marked with NOVA_MARK so Nova's own hook lets them through) and a **clipboard borrow** (all formats saved and put back ~300 ms after Ctrl+V; kept out of Win+V history) for text over 20 characters.
- **UI Automation** (COM `CUIAutomation`, on its own thread with a deadline) — the focused control's type and `IsPassword` before typing (SPEC §14). Chrome answers "pane" to its first question while it builds its tree, so Nova asks twice.
- **IAudioEndpointVolume / IAudioMeterInformation** (COM) — exact volume (read back) and "is anything playing" before pause / play; media keys via SendInput.
- **UI Automation TextPattern** (same COM object; `IUIAutomationTextPattern` / `TextRange` vtables in `nova/actions/uia.py`) — dictation reads the text just before the caret (spacing / capitals at joins, "did the paste land", "scratch that" only takes back its own text). Checked on RichEdit (`RICHEDIT50W`, what Notepad is built on): ~9 ms warm.

## Chrome (Stage 06; no new Python packages)
- **Nova's Chrome extension** (`extension/`, MV3, plain JS, no libraries) — worker keeps a native-messaging port to Nova, content script in every frame maps / clicks / fills / scrolls; permissions tabs, scripting, nativeMessaging, storage, alarms (retry every 30 s while Nova is down), host `<all_urls>`. Stable id `dnhapjgbmkmjofkmgpjoaidnhadgnkhf` from the manifest `key`; its private key (`extension/key.pem`, git-ignored) was made once with Node 24's `crypto`.
- **Native-messaging host** (`extension/host/nova_host.bat` -> `nova_host.py`, standard library) — Chrome's stdio frames <-> Nova's named pipe (`multiprocessing.connection`, 256-bit key in `data/chrome-bridge.json`); registered under HKCU by `nova chrome --install` (`winreg`). No TCP port.
- **Chrome for Testing 149** (Playwright's copy, already on this laptop) — only for `extension/test/selftest.py`: it still honours `--load-extension` (Chrome 137+ doesn't). Nova never uses it.

## Dictation (Stage 07; no new packages)
- Rules are plain Python (`nova/dictation/rules.py`). Cleanup reuses Tier 1's model through the same governor (GPU only; rules only on the CPU): it lists misheard words as schema-forced JSON and Nova applies them. Measured: ~1.0-1.25 s per call, ~0.8 s of it Ollama's per-request overhead, so text is typed first and a fix lands in place later.
- History: `data/dictation_history.jsonl` (last 50, local). Google Docs: the extension's `docsReady` / `caret` (Docs draws text on a canvas: only its cursor element is readable).

## Dev
- **pytest** 9.2 — unit tests for pure logic.
- **pytest-cov** 7.1 — coverage (80 % target; GUI shell omitted, checked by live demo).
- **Pillow** (not a project dependency) — only `scripts/island_screenshots.py` (README images / GIF), pulled in for that run with `uv run --with pillow`.
- **GitHub Actions** (`.github/workflows/tests.yml`) — `uv sync --frozen` + `uv run pytest` on a Windows runner for every PR and push to main.

## Models and data (in the repo folder, git-ignored; Nova itself never downloads)
- `models/parakeet/` — Parakeet TDT 0.6B v3, int8 ONNX for onnx-asr (`nemo-parakeet-tdt-0.6b-v3`, ~639 MB, CC-BY-4.0). STT (SPEC §4). `scripts/get_models.py` fetches it from huggingface.co/istupakov/parakeet-tdt-0.6b-v3-onnx at commit 8f23f0c (SHA-256 checked; identical to the copy voicebar used).
- `models/silero/silero_vad.onnx` — Silero VAD v6.2 (~2.2 MB, MIT). Speech start/end. Same script, from github.com/snakers4/silero-vad tag v6.2.
- `fixtures/voicebar-recordings/` — Bhavya's private recordings (4 sessions with `manifest.json`, 134 audio files), kept off GitHub; doctor shows them as optional. STT WER eval. Only the "check" session is clean: in "quiet" and "noise" the old recorder started late, so most files begin mid-word (`nova bench-stt` flags them).
- `fixtures/voicebar-reference/` — voicebar phrasing fixtures (committed).

## Local LLM (Stage 05; Ollama models, pulled once, never by Nova)
- No new Python package: the Ollama client is `http.client` (`nova/brain/llm.py`), NVML comes from `nvidia-ml-py` above.
- **qwen3.5:4b** (Q4_K_M, ~2.7 GB VRAM) — GPU model: the only candidate ≥ 90 % (94.9 % on every phrasing, 91.6 % behind Tier 0). **qwen3.5:2b-q4_K_M** (1.3 GB) — CPU model: the only one inside the 2.5 s CPU budget.
- Qwen prompts are rendered by Nova and sent raw to /api/generate: Ollama 0.30.10's /api/chat with `think: false` ignores `format` for Qwen 3.5. Every request also pays ~0.4-0.7 s of Ollama overhead (it re-reads the model header) — half of Tier 1's time.
- `nova bench-llm` 2026-11-04 (334 eval cases on GPU, 40 on CPU / Arc; "false" = conversation acted on with no gate in front):

| model | device | alone | behind Tier 0 | false | p50 ms | p95 ms | overhead ms | cold ms | GB |
|---|---|---|---|---|---|---|---|---|---|
| qwen3.5:4b | GPU | 94.9% | 91.6% | 5.2% | 1132 | 1417 | 724 | 8714 | 2.7 |
| qwen3.5:4b | CPU | 89.7% | 93.1% | 0.0% | 3290 | 4530 | 672 | 34765 | 2.9 |
| qwen3.5:2b-q4_K_M | GPU | 62.4% | 81.4% | 15.5% | 1010 | 1199 | 733 | 8394 | 1.3 |
| qwen3.5:2b-q4_K_M | CPU | 62.1% | 89.7% | 18.2% | 1842 | 2374 | 599 | 17791 | 1.5 |
| gemma4:e2b | GPU | 62.4% | 79.7% | 11.3% | 1265 | 1426 | 977 | 16325 | 0.1 |
| gemma4:e2b | CPU | 58.6% | 89.7% | 27.3% | 2004 | 3863 | 829 | 9372 | 1.0 |
| qwen2.5:7b (baseline) | GPU | 86.1% | 87.8% | 36.1% | 935 | 1169 | 440 | 8706 | 4.4 |
| qwen3.5:4b | Arc (Vulkan) | 93.1% | 93.1% | 0.0% | 986 | 1333 | 546 | 73874 | 2.7 |
| qwen3.5:2b-q4_K_M | Arc (Vulkan) | 65.5% | 89.7% | 18.2% | 865 | 1130 | 555 | 10011 | 1.3 |
| gemma4:e2b | Arc (Vulkan) | 48.3% | 86.2% | 27.3% | 1095 | 8021 | 751 | 27425 | 0.1 |

## Outside Python (already on this laptop)
- **uv** ≥ 0.12 recommended — 0.11 makes venvs whose `pythonw.exe` is a console exe (uv #19226); `nova doctor` checks this. The first `uv` on PATH here is a pip install (Python 3.11 Scripts); a second 0.11.19 sits in `~/.local/bin`.
- **Ollama** 0.30 — local LLM runtime (Stage 05). Only local tags are allowed; `*-cloud` tags never.
