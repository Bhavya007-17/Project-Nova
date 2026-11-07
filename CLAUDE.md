# Nova — rules for Claude Code

@AGENTS.md

## Claude Code specifics
- Start every stage in a fresh session (`/clear`), then:
  `Read CLAUDE.md and docs/SPEC.md, then execute docs/prompts/NN-name.md. Plan first, then build. Stop when the stage is done and report.`
  Out of usage mid-stage? New session: `Continue docs/prompts/NN-name.md from where it stopped — check git status and the last report first.`
- Your auto-memory lives on your own machine; teammates and their agents never see it. Put anything worth keeping in
  `docs/GOTCHAS.md` (facts) or the PR description (decisions).
- Personal settings go in `CLAUDE.local.md` or `.claude/settings.local.json` (both git-ignored).
- Write files that contain backslashes with Write / Edit, not Bash heredocs (they eat backslashes here).
- Before commands that send keys, paste or take focus, check whether the person is using the laptop and ask once.
- Finishing a stage: branch + PR per "Team workflow" above (the repo owner may push stage commits to `main`
  directly). Report: **Works / Doesn't yet / Numbers / Risks for next stage.**
