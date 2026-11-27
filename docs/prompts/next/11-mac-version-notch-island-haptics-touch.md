# Prompt 11: Mac version: notch island, haptics, touch

> Builds on: the shared Python core from all earlier stages; reuse RULES.md, eval and latency tooling. Can run in parallel on its own branch.


```
Goal: Nova on macOS with a Dynamic Island / NotchNook-style island in the notch.

Recommended architecture (decide with me before coding):
- Native Swift shell (SwiftUI + AppKit) for the island, hotkeys, haptics and the macOS "hands" (Accessibility API / AXUIElement). Reuse the Python core (gate, Tier 0, Tier 1 brain, dictation logic, skills, training) through a local Unix domain socket (no TCP port, like the Windows named pipe).
- Speech: FluidAudio (Swift, CoreML, runs Parakeet TDT v3 on the Apple Neural Engine; Apache-2.0 model conversion) or keep onnx-asr in Python. Benchmark both.
- Planner: Ollama on Apple Silicon (Metal), same nova-brain GGUF.
- Training on Mac: mlx-lm / mlx-tune LoRA (Prompt 8, mac-mlx backend).

Island engineering:
1. Notch geometry: read NSScreen.safeAreaInsets.top and auxiliaryTopLeftArea / auxiliaryTopRightArea to compute the notch rect; on Macs without a notch or on external displays, show a floating pill in the same spot. Handle display changes, multiple monitors and fullscreen spaces.
2. Window: a borderless non-activating NSPanel at a level above the menu bar, on all Spaces, black notch-shaped background that visually extends the hardware notch, never steals focus. Toggle ignoresMouseEvents so only the pill shape catches the pointer; everywhere else is click-through. Exclude from screen capture (NSWindow.sharingType = .none; verify on current macOS, ScreenCaptureKit rules changed recently).
3. Start from DynamicNotchKit (MIT, Swift Package) or write our own. Do not copy code from boring.notch (its README says CC BY-NC-ND while listings say GPL-3.0) or other GPL/AGPL notch apps unless we accept that license; study their behavior only.
4. States, matching Windows: idle (hidden, 0 frames), listening (live words), thinking, acting (steps), confirm/clarify (numbered choices), done/error, dictation. Spring animations; compact -> expanded morph from the notch.

"Touch" on the island (Mac screens are not touchscreens; this means pointer + trackpad):
- Hover over the notch: expand after a short dwell (tune ~150-300 ms) so passing to the menu bar does not trigger it; collapse on exit with a grace delay.
- Click: open the expanded island (history, last action, mic state); click a numbered choice to answer.
- Two-finger swipe/scroll down on the island: expand; swipe up: dismiss; horizontal swipe: switch panels.
- Force click (Force Touch trackpads): pin the island / quick "repeat last command".
- Drag a file or text onto the island: "do something with this" (optional, like NotchNook's shelf).
- Keyboard always works too: hold-to-talk hotkey (no Copilot key on Mac; default hold Right ⌘ or Fn/Globe, configurable), Enter/1-4/Esc while asking.

Haptics (Force Touch trackpad only):
- Use NSHapticFeedbackManager.defaultPerformer (respects the user's trackpad settings). Patterns: .alignment when the island snaps open under the pointer and when hovering moves between choices; .levelChange on confirm/cancel; .generic on errors.
- Haptics only reach the user when a finger is on the trackpad, so pair every haptic with a visual cue and an optional soft sound for hands-free voice use. Mice and Macs without Force Touch get no haptics; never rely on them.
- Add a settings toggle; use haptics sparingly.

Permissions and packaging: Microphone, Accessibility (AX hands), Input Monitoring (global hotkey via CGEventTap); a first-run checklist like nova doctor. Launch at login via SMAppService. Ship unsigned builds only, with first-run instructions for System Settings > Privacy & Security > Open Anyway (or `xattr -dr com.apple.quarantine`). Use only free tools and services; do not add anything that needs a paid account or subscription.

Mac hands: open/switch/quit apps (NSWorkspace), window arrange (AX position/size), keys (CGEvent), text at cursor, the same Chrome extension with a macOS native-messaging host manifest, Safari later.

Done when: on a notched MacBook the island lives in the notch (pill on external displays), hover/click/swipe/force-click behave as above with haptics on a Force Touch trackpad, voice commands open/arrange apps and run through the shared Python brain, dictation lands at the cursor, and nova doctor (mac) checks all permissions. Add a GOTCHAS section for macOS.
```


## Sources behind the numbers

Unsloth Qwen3.5 fine-tune guide (unsloth.ai/docs/models/qwen3.5/fine-tune); Ollama import docs (docs.ollama.com/import); BAIR TinyAgent blog; Hammer (arXiv 2410.04587); ToolACE (arXiv 2409.00920); COLING 2025 Industry paper on LoRA for SLU; Repair-Damage analysis (arXiv 2610.30385); Yamashita et al. phoneme-aware correction (arXiv 2505.17410); Whisper non-speech hallucination study (arXiv 2501.11378); TurboBias (arXiv 2508.07014); Let Me Speak Freely? (arXiv 2408.02442); Prune4Web (arXiv 2511.21398); VLAA-GUI (arXiv 2604.21375); Agent Workflow Memory (arXiv 2409.07429); EchoTrail-GUI (arXiv 2512.19396); UI-Mem (arXiv 2602.05832); MINJA (arXiv 2503.03704); Parakeet TDT 0.6B v3, IndicConformer and Qwen3-ASR model cards; Lost in Execution (arXiv 2601.05366); Apple NSHapticFeedbackManager and NSScreen docs; DynamicNotchKit and boring.notch READMEs; FluidAudio README; mlx-tune docs.
