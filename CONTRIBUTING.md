# Contributing to Nova

## 1. Set up (once)
Windows 11, Git and [uv](https://docs.astral.sh/uv/) installed, then:

```powershell
git clone https://github.com/Bhavya007-17/Project-Nova.git
cd Project-Nova
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

The script installs Python 3.12 + dependencies, downloads the speech models (~670 MB, SHA-256 checked), pulls the
local LLMs through [Ollama](https://ollama.com/download/windows) (~4 GB), registers the Chrome bridge and runs
`nova doctor`. Load the extension once: `chrome://extensions` → Developer mode → **Load unpacked** → the repo's
`extension` folder. `OPT` rows in the doctor are fine (Fusion, the private recordings…); `FIX` rows aren't.

The stage prompts say `C:\dev\nova`: that's the reference laptop's path. Any folder works.

## 2. Pick work
- Remaining stages are in `docs/prompts/` (08 teach, 09 handoff, 10 safety + polish, 11 speed + soak) plus the
  roadmap in `README.md`. Tell the group what you're taking (or open a draft PR early) so two people don't build the
  same stage.
- Stage 10 and 11 touch everything — do them after 08 and 09 land.

## 3. Build
- Branch from an up-to-date `main`: `git switch -c stage-09-handoff` (or `fix/…`, `docs/…`).
- With an AI agent: Claude Code reads `CLAUDE.md`; other agents read `AGENTS.md`. Give it one stage prompt per
  session.
- Rules that matter most (full list in `AGENTS.md`): local-only, no paid APIs; stay light; measure latency, don't
  guess; ask in the island instead of guessing; confirm risky actions; never type into password fields.
- Tests for pure logic only: `uv run pytest`. If you touch a hot path, compare `uv run nova latency` before and after.

## 4. Pull request
- `main` is protected: push your branch and open a PR. It needs one approval and a green CI run (unit tests on a
  Windows runner).
- Commit messages: `stage NN: <summary>` for a finished stage, otherwise `type: description`
  (`feat|fix|refactor|docs|test|chore|perf|ci`).
- Say in the PR what you checked live and on which laptop.
- UI changed? Re-render the README images: `uv run --with pillow python scripts/island_screenshots.py`.

## Never commit
`models/` (downloaded), `logs/` and `data/` (they contain transcripts of what you said), `fixtures/voicebar-recordings/`
(private voice recordings), `*.pem`. They're all in `.gitignore` — don't force-add them.

## Privacy
Nova's logs (`logs/traces.jsonl`) record what the mic heard unless `logs.transcripts: false` in
`config/settings.yaml`. Don't paste them into issues or PRs.
