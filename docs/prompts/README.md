# Building Nova with Claude Code

## Each stage
1. Open PowerShell in `C:\dev\nova` and run `claude`.
2. Start a fresh session for every stage (`/clear`), then paste:
   ```
   Read CLAUDE.md and docs/SPEC.md, then execute docs/prompts/NN-name.md. Plan first, then build. Stop when the stage is done and report.
   ```
3. When it reports, do the **Live demo** at the bottom of that prompt. If something's off, say so in plain words in the same session. When the demo works, it commits — move to the next file.
4. Hit your Pro usage limit mid-stage? Later, in a new session: `Continue docs/prompts/NN-name.md from where it stopped — check git status and the last report first.`

## Order
| File | Stage | You get |
|---|---|---|
| 00-kickoff.md | Kickoff | Repo, tray icon, `nova doctor` |
| 01-ears.md | Ears | Mic → text in the console, ~0.3 s |
| 02-triggers.md | Triggers | Copilot key, "Nova…", always-listening, follow-up window |
| 03-island.md | Island | The Dynamic-Island bar reacting live |
| 04-windows-hands.md | Windows hands | Open/switch/close apps, layouts, typing — instant |
| 05-local-brain.md | Local brain | Loose phrasing understood locally; asks when unsure |
| 06-chrome-canvas.md | Chrome + Canvas | Exact on-page clicks, "my assignment for today" |
| 07-dictation.md | Dictation | Clean text at the cursor, Google Docs |
| 08-teach-learn.md | Teach + learn | "Show me?" and auto-learned shortcuts |
| 09-handoff.md | Handoff | Claude/ChatGPT apps, voice mode, done notifications |
| 10-safety-polish.md | Safety + polish | Confirmations, settings, start with Windows |
| 11-speed-soak.md | Speed + soak | Budgets met, stable all day |
| 12-optional.md | Optional | Todoist/Notion/Calendar hooks, MCP server |

First useful version: after 05. Daily driver: after 09.

## One manual step later
Stage 06 asks you to load the Chrome extension once: chrome://extensions → Developer mode → Load unpacked → `C:\dev\nova\extension`.
