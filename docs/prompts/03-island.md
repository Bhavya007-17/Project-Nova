# Stage 03 — The Island

**Read:** CLAUDE.md, SPEC §6.
**Goal:** the Dynamic-Island pill reacting live to every state, wired to Stages 01–02.

## Build
1. `nova/island/` — PySide6 QML window: frameless, transparent, always-on-top, `Qt.Tool`; `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`; click-through (`WS_EX_TRANSPARENT`) when collapsed, interactive only with buttons showing; never takes focus except Confirm/Clarify. Top-center of the monitor under the mouse; follows monitor/DPI changes.
2. QML components: Pill (spring morph of width/height/radius), Waveform (mic level), LiveText (partials fade, finals settle), Orb (cheap thinking animation), StepLine, ResultLine (✓ / ✗ / ⚡), ConfirmRow, ChoiceRow (numbered), Chips, Teaching and Dictating indicators.
3. Python ↔ QML bridge: one QObject exposing state, text, steps, choices and chips as properties/signals, fed from the bus.
4. `nova island --demo` cycles every state with fake data, for tuning without talking.
5. Performance: GPU rendering; no timers or animations running while Hidden; render-thread Animators where possible.

## Style
True black (#000), white Segoe UI Variable, radius = height/2, iOS-like spring (stiff, slightly bouncy). Accents: listening blue, confirm amber, error red, dictating orange, done green. Nothing flashes or steals focus.

## Verify yourself
Run the demo driver with a temporary capturable flag (`NOVA_CAPTURABLE=1`) and screenshot each state at 200 % scaling to check the layout.

## Live demo (Bhavya, 2 min)
`uv run nova`: hold the Copilot key → the island drops down with waveform + live words; release → final text, then it collapses. Run `nova island --demo` and say what to tweak.

Commit `stage 03: island`. Report.
