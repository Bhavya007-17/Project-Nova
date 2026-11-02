# Stage 04 — Windows hands + fast commands

**Read:** CLAUDE.md, SPEC §7.1, §7.2, §8, §14. Reference only: `C:\dev\voicebar\voicebar\catalog.py`, `executor\apps.py`, `executor\windows.py`, `executor\input.py`.
**Goal:** the first real commands at hand-speed, all through Tier 0 (no LLM yet): open/switch/close apps and sites, window moves and layouts ("Fusion on the left, Claude on the right"), tabs, typing, keys, volume/media.

## Build
1. `nova/actions/catalog.py` — Start-menu apps (AUMID + names) + `.lnk` targets + `config/aliases.yaml`; background refresh every 10 min. Fuzzy resolution; ties → clarify (never silent). Seed aliases: canvas → https://canvas.vt.edu · fusion → Autodesk Fusion · chrome → Google Chrome · claude → Claude · chatgpt → ChatGPT · docs → https://docs.google.com.
2. `nova/actions/apps.py` — open (AUMID activation / exe), switch (restore + reliable foreground), close (WM_CLOSE; if an unsaved-changes dialog appears, bring it forward and report — never click it).
3. `nova/actions/windows.py` — maximize / minimize / restore / fullscreen / snap left·right·top·bottom / center / move to monitor; `layout` for 2–3 windows; DWM border compensation; multi-monitor.
4. `nova/actions/input.py` — clipboard-borrow paste, SendInput unicode, key chords; refuse password fields (UIA `IsPassword`).
5. `nova/actions/system.py` — volume, mute, media keys.
6. `nova/actions/context.py` — context snapshot (SPEC §7.1; the Chrome part is stubbed until Stage 06), ≤ 50 ms.
7. `nova/brain/tier0.py` — verb-synonym matcher + entity resolution → Plan (SPEC §7.5), with "and / then" chains.
8. `nova/brain/executor.py` — runs steps with timeout + verification + risk check (SPEC §14) → confirm via the island → result → island + log.
9. `nova say "<text>"` runs the whole brain without the mic.

## Verify yourself
Unit tests: tier0 on loose forms ("yo pull up chrome", "can you open fusion real quick", "throw claude on the right side", "put fusion left and claude right"); chrome ≠ Web Server for Chrome; risk detection. `nova say` round-trips. Latency from the logs.

## Live demo (Bhavya, 3 min) — Copilot key
"open chrome" · "pull up canvas" · "fusion on the left, claude on the right" · "maximize this" · "new tab" · "go back" · "type hello world and press enter" (in Notepad) · "volume down" · "close notepad". Each starts ≤ 0.5 s after you stop talking.

Commit `stage 04: windows hands + tier 0`. Report p50/p95 per command type.
