# Nova — rules for Claude Code

Nova is Bhavya's local voice layer for his Windows laptop: a Dynamic-Island pill at the top of the screen. He talks; the computer does it about as fast as he would by hand. Big jobs get handed to the Claude or ChatGPT desktop apps, and Nova tells him when they're done.

- Full design: `docs/SPEC.md` — the source of truth. If a stage prompt and the spec disagree, ask.
- Build order: `docs/prompts/` — one stage per session.

## This machine
- HP OMEN Transcend 14 · Core Ultra 9 285H (16 cores) · 32 GB RAM · RTX 5070 Laptop **8 GB VRAM** + Intel Arc 140T iGPU
- Windows 11 Home 25H2 (build 26200) · built-in 2880×1800 display at 200 % (external monitors sometimes) · default mic: Intel Smart Sound mic array
- uv 0.11 · use **Python 3.12 via uv** (not the 3.14 on PATH) · Node 24 · Git
- Ollama 0.30.x (has qwen2.5:7b, gemma4:e4b, llama3) · Chrome 156
- Claude desktop: MSIX, AUMID `Claude_pzs8sxrjxfjjc!Claude`
- ChatGPT desktop: Start-menu name "ChatGPT", package OpenAI.Codex, AUMID `OpenAI.Codex_2p2nqsd0c76g0!App`
- Autodesk Fusion: AUMID `AutodeskInc.Fusion360` — a heavy GPU user; Nova must stay light next to it

## Non-negotiables
1. **Local first, $0.** Speech, wake word, understanding, dictation cleanup and every action run on this laptop. The only things that leave it are prompts Bhavya hands to Claude/ChatGPT through their own apps. No paid APIs, no API keys, no cloud speech or LLM.
2. **Light.** Idle Nova ≈ mic + VAD. Speech-to-text on CPU. At most one small LLM (≤ 4B, 4-bit), loaded on demand, unloaded when idle. No vision model in v1.
3. **Hand-speed.** The bar is how long Bhavya would take himself (1–2 s). Budgets in SPEC §16. Measure every hop; never guess.
4. **Generic, not scripted.** No phrase lists for him to memorize or record. Loose phrasing must work. If Nova doesn't get something, it asks in the island ("Show me?"), learns, and does it next time.
5. **Safe.** Confirm in the island before send / submit / delete / buy / post / Canvas submissions. Never type into password fields, never handle payment details, never run shell commands from voice.

## How to work
- One stage prompt per session. Plan first, then build. Stop at the end of the stage and report.
- Before using a library API, check the installed version (signature, source or official docs). One line per dependency + why in `docs/DEPS.md`. No essays.
- Simple beats clever. Few dependencies. Ask before adding anything over ~50 MB or a new runtime.
- Tests only for pure logic: routing, entity resolution, cleanup rules, skill matching, risk detection, state machine, schemas. Don't write tests that open real windows or the mic unless the stage asks.
- Keep Python off hot paths: the low-level keyboard hook callback returns in < 10 ms; the Qt GUI thread never waits on models or I/O.
- Done = the stage's live demo works on this laptop. Run everything you can yourself first (CLIs, unit tests, `nova latency`), then give Bhavya the short demo script.
- Commit at the end of each stage: `stage NN: <summary>`.
- Report: **Works / Doesn't yet / Numbers / Risks for next stage.** Short.

## Do NOT repeat C:\dev\voicebar (the previous attempt — read-only reference)
- No gate documents, evidence folders, hash manifests, socket-proof monitors or acceptance paperwork.
- No all-or-nothing grammar, no wake-word-only design, no "unsure → silently do nothing". Ask in the island instead.
- Don't score speech-to-text on exact strings; use WER after normalizing case and punctuation.
- Don't keep several models resident on the 8 GB GPU (that's why voicebar's screen clicks timed out).
- Worth reading, then re-implementing cleanly: `voicebar/stt/parakeet.py` (onnx-asr Parakeet + Silero VAD), `voicebar/catalog.py` (Start-menu apps / AUMIDs), `voicebar/executor/windows.py` and `input.py`.
- Already copied into this repo: `models/parakeet` (int8 ONNX for onnx-asr), `models/silero/silero_vad.onnx`, Bhavya's recordings in `fixtures/voicebar-recordings/` (each session's `manifest.json` has the expected text), voicebar's phrasing fixtures in `fixtures/voicebar-reference/`.
- Voicebar's source stays at `C:\dev\voicebar` (read-only; its venvs, caches and other models are gone) and is archived at github.com/Bhavya007-17/voicebar (tag `v1`). Never import from or modify it. Download any other model you need.

## Commands (as they get built)
`uv run nova` (tray app) · `nova doctor` · `nova listen` · `nova say "<text>"` (`--chrome` for page / Canvas steps) · `nova eval` · `nova latency` · `nova chrome --install | --status` · `nova skills list`
Chrome self-test (Chrome for Testing, throwaway profile): `uv run python extension/test/selftest.py [--llm]`
