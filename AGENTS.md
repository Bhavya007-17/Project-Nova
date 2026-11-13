# Nova — guide for AI coding agents

Claude Code reads this through `CLAUDE.md`; Codex, Cursor, Copilot and other agents read it directly. Humans: start
with `README.md` and `CONTRIBUTING.md`.

Nova is a local voice layer for a Windows laptop: a Dynamic-Island pill at the top of the screen. You talk; the
computer does it about as fast as you would by hand. Big jobs get handed to the Claude or ChatGPT desktop apps, and
Nova says when they're done. It started as Bhavya's personal project and is now built by a small team.

## Read first
- `docs/SPEC.md` — the design and source of truth. If a stage prompt and the spec disagree, ask.
- `docs/prompts/NN-*.md` — the build, one stage per session (index: `docs/prompts/README.md`).
- `docs/GOTCHAS.md` — measured facts that cost days to find. Read the section for what you touch.
- `docs/DEPS.md` — every dependency, its version and why.

## Status
| Stage | | State |
|---|---|---|
| 00–06 | skeleton + doctor, ears (STT), triggers, island, Windows hands + Tier 0, local brain (Tier 1), Chrome + Canvas | done |
| 07 | dictation | done; the LLM misheard-word fix (`nova/dictation/cleanup.py`) shares the planner's model since 07a — check it in the 07a live demo |
| 07a | fast + hearing (model never times out, ~1 s loose requests, gate fixes) | built and measured; still open: the live demo, and the name-biasing decision (needs `nova record-set`, 3 min of reading) |
| 07b | talk sessions (open the mic once, act clause by clause) | not started |
| 07c | control anything (scroll cascade, PDFs, click by name / number / grid, camera, screenshot) | not started |
| 08 | teach + learn ("Show me?", learned skills ⚡) | not started — island UI states exist |
| 09 | handoff to Claude / ChatGPT + done notifications | not started — island chips exist; router has a stub |
| 10 | safety + polish (risk audit, settings panel, start with Windows) | not started |
| 11 | speed + soak | not started |
| 12 | optional: Todoist / Notion / Calendar, MCP server | only on request |

## Non-negotiables
1. **Local first, $0.** Speech, wake word, understanding, dictation cleanup and every action run on the laptop. The
   only things that leave it are prompts the user hands to Claude/ChatGPT through their own apps. No paid APIs, no API
   keys, no cloud speech or LLM (Ollama `*-cloud` tags included).
2. **Light.** Idle Nova ≈ mic + VAD. Speech-to-text on CPU. At most one small LLM (≤ 4B, 4-bit), loaded on demand,
   unloaded when idle. No vision model in v1.
3. **Hand-speed.** The bar is how long the user would take by hand (1–2 s). Budgets in SPEC §16. Measure every hop
   (`nova latency`); never guess.
4. **Generic, not scripted.** No phrase lists to memorize or record. Loose phrasing must work. If Nova doesn't get
   something, it asks in the island ("Show me?"), learns, and does it next time.
5. **Safe.** Confirm in the island before send / submit / delete / buy / post / Canvas submissions. Never type into
   password fields, never handle payment details, never run shell commands from voice.

## Repo map
| Path | What lives there |
|---|---|
| `nova/cli.py` | every `nova …` command |
| `nova/audio.py`, `vad.py`, `stt/`, `speech.py` | mic → Silero VAD → Parakeet → text (`stt/sherpa.py`: a bench-only candidate decoder; `recordset.py`: his own test set) |
| `nova/hook.py`, `keys.py`, `triggers.py`, `gate.py`, `state.py` | Copilot key / chord / "Nova …" / always-listening → state machine |
| `nova/island/` (+ `qml/`) | the Dynamic-Island UI (`view.py` = pure view model) |
| `nova/brain/` | `tier0.py` fast patterns, `tier1.py` + `llm.py` + `llama_server.py` local LLM (llama-server child, Ollama as fallback), `plan.py` schema, `validate.py`, `risk.py`, `router.py`, `executor.py` |
| `nova/actions/` | Windows hands: apps / Start-menu catalog, windows, input, UIA, Chrome API |
| `nova/chrome/` + `extension/` | MV3 extension + native-messaging bridge (page map, click, fill, Canvas) |
| `nova/dictation/` | dictation session, rules, target, LLM cleanup, history |
| `nova/governor.py` | GPU vs CPU model choice from free VRAM |
| `config/` | `settings.yaml`, `aliases.yaml` (short names → apps/sites), `vocab.yaml` (names to spell right) |
| `docs/eval/intents.yaml` | intent eval used by `nova eval` |
| `scripts/` | `setup.ps1`, `get_models.py`, `island_screenshots.py` |
| `models/`, `logs/`, `data/`, `fixtures/voicebar-recordings/` | local only, git-ignored — never commit |

## How to work
- One stage prompt per session. Plan first, then build. Stop at the end of the stage and report:
  **Works / Doesn't yet / Numbers / Risks for next stage.** Short.
- Before using a library API, check the installed version (signature, source or official docs). One line per
  dependency + why in `docs/DEPS.md`. No essays.
- Simple beats clever. Few dependencies. Ask before adding anything over ~50 MB or a new runtime.
- Tests only for pure logic: routing, entity resolution, cleanup rules, skill matching, risk detection, state machine,
  schemas. Don't write tests that open real windows or the mic unless the stage asks. CI runs `uv run pytest` on every
  PR (Windows runner, no models).
- Keep Python off hot paths: the low-level keyboard hook callback returns in < 10 ms; the Qt GUI thread never waits on
  models or I/O.
- Done = the stage's live demo works on a real laptop. Run everything you can yourself first (CLIs, unit tests,
  `nova latency`), then give the human a short demo script. Anything that sends keys or takes focus goes to the window
  in front: run focus-free checks first and ask once before the rest.
- Learned something non-obvious? Add it to `docs/GOTCHAS.md` in the same PR.

## Team workflow (git)
- `main` is protected: work on a branch (`stage-08-teach`, `fix/dictation-paste`, `docs/readme`), open a pull request,
  get one approval, CI green. Don't push to `main` directly or force-push it.
- Commits: `stage NN: <summary>` when a stage is done; otherwise `type: description`
  (`feat|fix|refactor|docs|test|chore|perf|ci`). No AI attribution trailers.
- Never commit `models/`, `logs/`, `data/`, `fixtures/voicebar-recordings/` or `*.pem`. Logs hold transcripts of what
  people said.

## Commands
`uv run nova` (tray app) · `nova doctor` · `nova listen [--wav f]` · `nova keys --probe` · `nova island --demo [scene]` ·
`nova say "<text>" [--dry]` · `nova eval` · `nova bench-stt [--set dir --backend both]` · `nova record-set` ·
`nova bench-llm [--runtime ollama,llama-server]` · `nova latency` ·
`nova chrome --install | --status` · `uv run pytest`
Chrome self-test (Chrome for Testing, throwaway profile): `uv run python extension/test/selftest.py [--llm]`
Setup on a fresh clone: `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1`

## Reference machine (budgets and numbers were measured here)
- HP OMEN Transcend 14 · Core Ultra 9 285H (16 cores) · 32 GB RAM · RTX 5070 Laptop **8 GB VRAM** + Intel Arc 140T iGPU
- Windows 11 Home 25H2 (build 26200) · 2880×1800 at 200 % (external monitors sometimes) · Intel Smart Sound mic array
- uv 0.11 · **Python 3.12 via uv** (not a system Python) · Node 24 · Git · Ollama 0.30.x · Chrome 156
- Claude desktop: MSIX, AUMID `Claude_pzs8sxrjxfjjc!Claude` · ChatGPT desktop: Start-menu name "ChatGPT", package
  OpenAI.Codex, AUMID `OpenAI.Codex_2p2nqsd0c76g0!App`
- Autodesk Fusion: AUMID `AutodeskInc.Fusion360` — a heavy GPU user; Nova must stay light next to it

**Different laptop?** Run `uv run nova doctor` — it prints the AUMIDs and devices it actually finds. No Copilot key →
use the chord (`ctrl+alt+n`). No NVIDIA GPU → the CPU model path. Say which machine you measured on in your PR.

## Lessons from voicebar (the previous attempt)
Archived at github.com/Bhavya007-17/voicebar (tag `v1`) — read-only reference; never import from it.
- No gate documents, evidence folders, hash manifests, socket-proof monitors or acceptance paperwork.
- No all-or-nothing grammar, no wake-word-only design, no "unsure → silently do nothing". Ask in the island instead.
- Don't score speech-to-text on exact strings; use WER after normalizing case and punctuation.
- Don't keep several models resident on the 8 GB GPU (that's why voicebar's screen clicks timed out).
- Worth reading, then re-implementing cleanly: `voicebar/stt/parakeet.py`, `voicebar/catalog.py`,
  `voicebar/executor/windows.py` and `input.py`.
