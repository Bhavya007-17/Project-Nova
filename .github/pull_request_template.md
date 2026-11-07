## What and why
<!-- One or two lines. Link the stage prompt (docs/prompts/NN-*.md) or issue. -->

## Checks
- [ ] `uv run pytest` passes
- [ ] Touched a hot path (hook, STT, router, executor, island)? `uv run nova latency` before / after below
- [ ] Safety holds: risky actions still confirm in the island; nothing types into password fields
- [ ] Learned something non-obvious? Added it to `docs/GOTCHAS.md`
- [ ] No `models/`, `logs/`, `data/`, recordings or `*.pem` in the diff

## Live check
<!-- What you ran on a real laptop and what happened. Which machine, if not the reference one. -->
