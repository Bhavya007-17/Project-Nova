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
| 07a-fast-hearing.md | Fast + hearing | Reacts every time; loose requests in ~1 s, never "took too long" |
| 07b-sessions.md | Talk sessions | Open the mic once, talk naturally, each clause acts |
| 07c-control-anything.md | Control anything | Scroll PDFs, click by name or number, grid, camera, screenshot, folders |
| 08-teach-learn.md | Teach + learn | "Show me?" and auto-learned shortcuts |
| 09-handoff.md | Handoff | Claude/ChatGPT apps, voice mode, done notifications |
| 10-safety-polish.md | Safety + polish | Confirmations, settings, start with Windows |
| 11-speed-soak.md | Speed + soak | Budgets met, stable all day |
| 12-optional.md | Optional | Todoist/Notion/Calendar hooks, MCP server |

First useful version: after 05. Hands-free: after 07c. Daily driver: after 09.

07a–07c were added on 2026-11-11 after a week of real use (what the logs showed is at the top of each file). Each lands on its own branch and pull request, with its live demo done before the next one starts.

## Stages 07b–12 on Opus 5.5
08–12 were rewritten on 2026-11-13 (and 07b / 07c touched up) so that a model with less headroom than the one that designed them can build them. The design choices, the traps and the order of work are already in each file. The builder's job is to check them against the code as it is that day, and build — not to redesign.

Pick Opus 5.5 with the effort turned up for the planning turn, and paste this instead of the short line above:
```
Read CLAUDE.md, docs/SPEC.md and docs/GOTCHAS.md, then docs/prompts/NN-name.md. Before writing any code:
1. Open every file the prompt names. Check each line under "What is already there" against the code as it is today, and list what has moved or is no longer true.
2. Do the prompt's "Probe first" and "measure" items on this laptop and show me what you found.
3. Write the plan: files, order, the tests you will write first, what you will measure before and after, and anything in the prompt you think is wrong (say why — don't quietly change it).
Show me the plan and wait for my OK. Then build in the prompt's order: tests first, run them after every step, commit as you go. If something in the prompt turns out to be wrong halfway, stop and tell me instead of working around it. Stop when the stage is done and report: Works / Doesn't yet / Numbers / Risks for next stage.
```
(Take out "and wait for my OK" to let it run straight through.)

How to read those files:
- **"What is already there"** was read from the code on the date it gives. Stages before it have changed the code since: find things by name, never by line number.
- **"Decided"** is settled. Build it that way; if it looks wrong, say so in the plan, in words.
- A line that says **"check"** or **"probably"** was reasoned, not measured. Measure it on the laptop and write the answer into `docs/GOTCHAS.md`.
- **"If usage runs short"** is the order to stop in. Every stop is a working demo, never a half-wired state.
- A step the code already does (an earlier stage got there first) is skipped — say so in the report.
- No real transcript ever goes into a test, a fixture or a report. Use the timings of the logged case with made-up words.

## One manual step later
Stage 06 asks you to load the Chrome extension once: chrome://extensions → Developer mode → Load unpacked → `C:\dev\nova\extension`. Stage 08 changes the extension, so reload it there once after that stage.
