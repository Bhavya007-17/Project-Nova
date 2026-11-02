# Nova — build spec (v1)

2026-10-29 · Owner: Bhavya · Builder: Claude Code via `docs/prompts/` · Rules: `CLAUDE.md`

## 1. What Nova is
A voice layer for Bhavya's Windows laptop. He holds the Copilot key (or says "Nova…", or — with always-listening on — just gives a command) and says what he wants:
"open Chrome" · "Fusion on the left, Claude on the right" · "click on my assignment for today" · "open a Google Doc and type this…" · "put my homework for today on Claude" · "on Claude, turn on voice".

A black Dynamic-Island pill at the top of the screen shows live words, what Nova is doing, and the result. Simple things run locally at hand-speed. Big jobs go to the Claude or ChatGPT desktop apps, and Nova notifies him when they finish.

Design source: Atticus (github.com/SaarthurR/atticus-mac, MIT) — instant no-AI commands, shortcuts learned after one success that replay with no AI, hold-to-talk, cleaned dictation, background jobs with a done notice, confirm-before-risky. Nova replaces Atticus's cloud brain with a small local one and adds a wake word, always-listening, teach mode, a Chrome extension and handoff to the desktop apps.

## 2. Principles (priority order)
1. Never do something risky he didn't confirm.
2. Do what he meant, not what he literally said.
3. Hand-speed or better.
4. Stay out of the way: light on CPU/GPU, quiet UI, never steal focus.
5. Unsure → ask in the island. Unable → offer "Show me?" and learn.

## 3. Architecture
```text
mic → VAD (Silero, CPU) → STT (Parakeet, CPU) → vocab fix-up → trigger gate → router
                                                                              │
             context snapshot (foreground, windows, Chrome tab, page map*) ───┤
                                                                              ▼
   Tier 0 fast patterns (~5 ms) → Tier 0.5 learned skills (~20 ms) → Tier 1 local LLM (~0.3–0.8 s)
                                  │                                      ├→ clarify / "Show me?"
                                  ▼                                      └→ handoff → Claude / ChatGPT app
                               Plan (JSON) → risk check → confirm in island if risky
                                  ▼
             executor: Windows (Win32 / UIA / OCR) · Chrome (extension) · input (paste / keys)
                                  ▼
             verify → island feedback → log hop timings → auto-learn on verified success
```
*page map only when the request points at something on the page.

**Process model.** One Python 3.12 process (`nova`):
- Qt GUI thread: QML island + tray. Never blocks.
- Audio: the sounddevice callback copies frames into a ring buffer, nothing else.
- Speech worker: VAD + STT (onnxruntime releases the GIL).
- Brain worker: routing, LLM calls, executor (each action with a timeout).
- Keyboard-hook thread: WH_KEYBOARD_LL + message loop; callbacks < 10 ms.
- Bridge thread: named-pipe server for the Chrome native-messaging host.

Ollama runs as its own service (already installed). Nova opens no TCP listeners. Threads talk through queues and a small event bus; UI updates go through Qt signals.

## 4. Speech
- **Capture:** default input, 16 kHz mono float32, 512-sample (32 ms) blocks, shared mode. Closed when Nova is Off or Windows is locked; reopened on unlock. Survives device changes.
- **VAD:** Silero VAD ONNX, CPU, 1 thread (`models/silero/silero_vad.onnx`, copied from voicebar). Speech start: p > 0.5 for 2 frames; 300 ms pre-roll.
- **Endpointing:** 300 ms of silence ends short utterances (< 2.5 s of speech), 700 ms longer ones; key release ends immediately; 30 s cap for commands (dictation is segmented, no cap).
- **STT:** Parakeet TDT 0.6B via onnx-asr, int8, CPU, 4 intra-op threads — the int8 export is already in `models/parakeet` (copied from voicebar). Voicebar measured ≈ 180 ms p50 / 460 ms p95 from speech end to text with it. Reference loader: `C:\dev\voicebar\voicebar\stt\parakeet.py`.
- **Partials:** while he speaks, re-decode the growing buffer every ~400 ms for live words in the island. Display only — partials never execute. A final never waits behind a partial.
- **Vocab fix-up:** after STT, phonetic + fuzzy replacement against `config/vocab.yaml` (CroLabs, HIVE, ROS2, MuJoCo, professor names, course codes…) and catalog names. Strong matches only.
- **Wake detection is text-based** (on STT output; no extra model). If idle CPU in a chatty room is too high, add an acoustic pre-filter (openWakeWord) — measure first.
- **Backend interface:** `transcribe(pcm) -> text`, so whisper-turbo or a newer model can be downloaded and swapped in.
- **Eval:** WER on Bhavya's existing recordings (`fixtures/voicebar-recordings/`, expected text in each session's `manifest.json`), normalized for case/punctuation/numbers. He never has to record anything new.

## 5. Triggers, listening modes, states
All triggers are on by default; each can be toggled in settings.
1. **Copilot key** (Windows sends LWin+LShift+F23). A low-level hook swallows it so Copilot doesn't open, and masks LWin so Start doesn't open. Hold > 250 ms = push-to-talk, ends on release. Tap = listen until endpoint. Tap while listening = finish now. Double-tap = locked dictation. Esc = cancel. Verify on this laptop whether a real key-up arrives on release; if not, tap behaviour only, and say so.
2. **Wake name** "Nova" (configurable, with fuzzy spellings). "Nova, open Chrome" acts; "Nova" alone opens a listening session.
3. **Always-listening commands** — acts only on command words (gate below).
4. **Fallback chord** (default Ctrl+Alt+N; doctor checks it's free), same hold/tap semantics.

**Always-listening gate**
- The utterance starts with a command verb or a polite command frame (open / close / switch to / click / scroll / go back / new tab / minimize / snap / move / play / pause / mute / type…; "can you / could you / please" + verb).
- It resolves via Tier 0 or a learned skill with high confidence, or Tier 1 returns `act` with confidence ≥ 0.85 *and* it names a known entity (app, site, window, on-screen element). Anything else is ignored silently.
- Without key or name, only safe actions run: open / switch / snap / move / scroll / nav / tab / media / volume / skills marked safe. Typing, page clicks, closing and handoff need the key, the name, or the follow-up window.
- Name or key is required while another app is using the mic (call/meeting — CapabilityAccessManager ConsentStore `LastUsedTimeStop == 0`, excluding Nova), while the speakers are playing audio (render-endpoint peak meter above threshold), or in Passthrough.
- **Follow-up window:** for 8 s after any executed command, commands don't need the name.

**States:** Off · Idle · Listening · Thinking · Acting · Confirm · Clarify · Teaching · Dictating · Passthrough · Done/Error. An explicit, pure, unit-tested state machine; every transition is a bus event that drives the island.
- Off: mic closed. Idle: VAD only (+ STT on speech segments when the wake name or always-listening is on).
- Passthrough (Claude/ChatGPT voice mode active): Nova ignores speech except "Nova…" and the Copilot key.

## 6. The Island (UI)
- **Tech:** PySide6 QML. Frameless, transparent, always-on-top, tool window (no taskbar button). Excluded from screen capture (`SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`). Click-through when collapsed; interactive only while showing buttons. Never takes keyboard focus except for Confirm/Clarify. Shown top-center on the monitor under the mouse; DPI-aware (200 %).
- **Look:** true-black pill (#000), white text (Segoe UI Variable), radius = height/2, spring morphing between sizes like the iPhone Dynamic Island. Prefer render-thread Animators for smoothness. One accent per state: listening blue, confirm amber, error red, dictating orange, done green.
- **Sizes:** Hidden (or a 1-px glow when always-listening is on) → Compact (~360×44: waveform + live words) → Expanded (up to ~560×160: steps, results, buttons) → Chips (small pills beside it for background jobs).
- **Per state:**
  - Listening: waveform from mic level + live words (partials fade, finals settle).
  - Thinking: small animated orb (cheap shader or gradient).
  - Acting: step line, e.g. "Chrome → Canvas → Today".
  - Done: ✓ + short result ("Opened HW 5 – Thermo"), collapse after 1.5 s; ⚡ when a learned skill ran.
  - Error: what failed, plainly, + [Try again] [Show me].
  - Confirm: the exact action ("Submit 'HW 5' on Canvas?") + [Yes] [No]; voice "yes / do it / confirm" or "no / cancel"; Enter/Esc; 15 s timeout = cancel.
  - Clarify: question + up to 4 numbered choices ("the second one" works).
  - Teaching: red dot + "Recording — do it, narrate if you like, say 'done'".
  - Dictating: orange ring + last sentence typed.
  - Passthrough: "Claude voice · tap Copilot to end".
  - Chips: "Claude · HW 5 · working…" → "done ✓"; click focuses that window.
- **Tray:** On/Off, always-listening, settings, logs, quit.

## 7. Understanding (routing)
### 7.1 Context snapshot (≤ 50 ms, built in parallel with STT)
Foreground window (process, title, class), top 12 visible windows, monitors, Chrome active tab (url, title) via the extension, focused control (UIA name, role, password flag). Page map (§9) only when the utterance points at page content ("click…", "the assignment…", "this / that…", "on this page…") and Chrome is foreground or named.

### 7.2 Tier 0 — fast patterns (≤ 5 ms)
A verb-first matcher, not a grammar: verb synonyms ("open / launch / pull up / bring up / start / fire up / get me" → open) + entity resolution against the catalog (rapidfuzz + aliases). Covers app/site open/switch/close, window ops and layouts ("X on the left, Y on the right"), tabs, back/forward/reload, scroll, volume/media, "type …", "search … [on YouTube]", dictation on/off, sleep/stop listening, chained with "and / then". Anything unresolved goes on to Tier 0.5/1 — never dropped silently.

### 7.3 Tier 0.5 — learned skills (≤ 30 ms)
Embedding similarity between the utterance and each skill's trigger examples (small CPU embedding model), filtered by scope (app / URL). Above threshold → run the skill; parameters are re-extracted from the new utterance (template regex first, Tier 1 for params only if needed). See §12.

### 7.4 Tier 1 — local LLM planner (≤ 600 ms on GPU)
- **Runtime:** Ollama `/api/chat`, `format` = Plan JSON schema, `think: false`, temperature 0, `num_ctx` 4096, `keep_alive` 10m. Static system prompt (action schema, routing rules, ~10 examples) so the prefix cache makes repeats cheap.
- **Model:** the smallest that scores ≥ 90 % on the intent eval (§18) within budget. Candidates (check current tags before pulling): a current small Qwen instruct (~4B and ~2B), Gemma 4 E2B. Installed qwen2.5:7b is the accuracy baseline only.
- **Device:** RTX by default. If free VRAM (NVML) < model + 1 GB — e.g. Fusion has the GPU — use a CPU copy (`num_gpu: 0`) of the ~2B model. Stage 05 benchmarks GPU vs CPU (Arc via Vulkan only if this Ollama build supports it) and sets defaults from measured numbers.
- **Preload** on Copilot key down / wake name / speech start, so loading overlaps speaking.
- **Input:** transcript + compact context JSON + catalog names (+ top-40 page elements when relevant). **Output:** a Plan.
- **Payload rule:** text to type or search must be a substring of the transcript (code checks). The model picks spans; it doesn't invent text. Only dictation cleanup (§11) rewrites words.

### 7.5 Plan schema
```json
{
  "kind": "act | dictate | handoff | clarify | teach | ignore",
  "steps": [{ "action": "open_app", "app": "Google Chrome" }],
  "handoff": { "to": "claude | chatgpt", "mode": "text | voice", "prompt": "...", "attach": ["canvas_today", "page_text", "selection"] },
  "question": "...", "choices": ["..."],
  "confidence": 0.0,
  "say": "short island text"
}
```

### 7.6 Routing rules
- Explicit handoff words ("on Claude", "ask Claude", "put … on Claude", "have ChatGPT…", "Claude voice") → handoff.
- Big open-ended work (write / solve / research / analyze / summarize something long, multi-app jobs) → handoff to Claude by default; confirm the target only if confidence < 0.7.
- `act` with confidence < 0.6 → clarify. Execution failure or `teach` → "Show me?".

## 8. Hands (executor)
**Action set** (extend by adding a module + schema entry):
- Apps/windows: `open_app{app}`, `switch_app{app}`, `close_app{app}`, `window{op, target, monitor}` (maximize | minimize | restore | close | fullscreen | snap_left | snap_right | snap_top | snap_bottom | center | move_monitor), `layout{slots:[{app, area}]}`.
- Browser: `open_url{url, new_tab}`, `web_search{query, engine}`, `tab{op, match}` (new | close | next | prev | reopen | goto), `nav{op}` (back | forward | reload), `page_click{element|description}`, `page_fill{element|description, text, submit}`, `page_read{what}`, `scroll{dir, amount}`.
- Native UI: `ui_click{description}` (UIA first, Windows OCR text match second), `ui_read{what}`.
- Input: `type_text{text, submit}`, `press{keys}`.
- System: `volume{op}`, `media{op}`.
- Nova: `dictation{op}`, `listening{op}` (sleep | wake | off), `run_skill{id, params}`, `wait_for{condition, timeout}`.

**Implementation notes**
- App catalog: Start-menu apps (AUMIDs via shell:AppsFolder) + `.lnk` targets + `config/aliases.yaml`. Launch via `IApplicationActivationManager` (or `explorer shell:AppsFolder\<AUMID>`); if already running, restore and bring to front (handle foreground-lock rules). "chrome" must resolve to Google Chrome, not "Web Server for Chrome". Ties → clarify.
- Window geometry: per-monitor work area; compensate DWM invisible borders (`DWMWA_EXTENDED_FRAME_BOUNDS`).
- Typing: clipboard borrow (save all formats → set text → Ctrl+V via SendInput → restore after ~300 ms); SendInput unicode for ≤ 20 chars or paste-blocked fields. Refuse password fields.
- Every action: timeout (default 3 s) → verification (window exists/foreground, URL changed, element state changed, text landed) → result `{ok, detail, ms}`.
- Elevated windows can't be driven from a normal process — say so plainly.

## 9. Chrome extension + bridge
- MV3, unpacked, stable ID via manifest `key`. Permissions: tabs, scripting, nativeMessaging, storage; host permissions `<all_urls>`.
- The service worker keeps a native-messaging port open to host `com.nova.bridge` (registered under `HKCU\Software\Google\Chrome\NativeMessagingHosts`). The host (Python via `.bat`) relays JSON to Nova over a named pipe (`multiprocessing.connection`, authkey). If Nova isn't running, the host exits.
- Commands: tabs list/activate/open/close, nav, `page.map`, `page.click`, `page.fill`, `page.read`, `page.highlight`, `page.wait(selector|url|text)`, `canvas.*` (§10).
- **Page map:** visible actionable elements (links, buttons, inputs, `[role=button|link|tab|menuitem|checkbox]`, clickables via a cursor:pointer heuristic), including open shadow roots and same-origin iframes. Each: id, role, name (aria-label / label / text, trimmed), href, nearest heading/section, row context (e.g. a date column), bbox, visible. Cap 300. Python ranks the top 40 by relevance to the utterance before Tier 1 sees them.
- **Click:** scrollIntoView → 300 ms highlight → `element.click()` (or pointer events) → verify (URL or DOM mutation). Stale id → re-map once.

## 10. Site adapters (thin helpers; the generic tiers work without them)
- **Canvas (canvas.vt.edu) — no API token.** From inside the logged-in Canvas tab, read Canvas's own planner/assignment JSON with same-origin fetch (strip the `while(1);` prefix if present); fall back to the DOM (To-Do list / calendar). Provides items due today / this week (title, course, due, url) and assignment description text. "Click on my assignment for today": one → open it; several → numbered list in the island; none → say so + the next due item.
- **Google Docs:** "open a Google Doc" = `docs.new`; wait until the editor is ready and focused; dictation/typing then lands there.
- **claude.ai / chatgpt.com:** fallback handoff targets when the desktop apps can't be driven, or for a second parallel job.

## 11. Dictation
- **Start:** "type this…", "write…", "start dictation", double-tap Copilot, or a plan like "open a Google Doc and type this…". **End:** "stop dictation", Copilot tap, 10 s of silence.
- **Per segment** (VAD pause ≥ 600 ms):
  1. STT.
  2. Rules: spoken commands ("new line", "new paragraph", "scratch that" = delete last segment, "delete that", punctuation words when said explicitly); filler removal (um / uh / erm; "like" / "you know" only when clearly filler); self-corrections ("Tuesday, no wait, Wednesday" → "Wednesday").
  3. LLM light cleanup: fix obvious mis-hearings using vocab + the text before the cursor; keep his words; never add content; never answer questions.
  4. Divergence guard: > 25 % of words changed, or new words not in the input/vocab → use the rules-only text.
  5. Insert at the cursor (clipboard borrow). Target: lands ≤ 1 s after the pause.
- No GPU room → rules only.
- If insertion can't be confirmed: save to history + island notice; never type twice. History keeps the last 50 ("Nova, paste my last dictation").

## 12. Skills: auto-learn + teach
- **Skill** = `{id, name, triggers[], embedding, scope (app / URL pattern), params, steps[], safe, uses, last_ok}` as JSON in `data/skills/`. Human-readable and editable.
- **Auto-learn:** after a Tier-1 plan executes and verifies, save it as a skill only if it's deterministic — steps reference apps, URLs and stable element descriptors; not transient ids, not "this / that / the third one", not failed or partial runs. Literal values that match catalog entities become params.
- **Teach** ("Show me?" → yes, or "Nova, let me show you how to…"): record until he says "done".
  - Chrome: clicks / inputs / navigations via the extension, as element descriptors (role + name + section + URL pattern).
  - Native apps: low-level mouse hook + UIA element-at-point (automation id, name, control type, ancestor path) + hotkeys. Never record text typed into password fields.
  - Narration while teaching ("now click the assignment due today") is attached to the step as a semantic description.
  - End: island summary [Save] [Discard]; the original utterance becomes the first trigger.
- **Replay:** literal steps run directly; semantic steps are resolved against the live page/UI by Tier 1 using only that step's description + candidates (small, fast prompt). Every step verifies; on failure stop, show which step broke, offer re-teach.
- **Manage by voice:** "what can you do in Canvas", "forget that", "rename that skill to…".

## 13. Handoff to Claude / ChatGPT
- **Targets:** Claude desktop app (default — his Cowork-style work lives there), ChatGPT desktop app. claude.ai / chatgpt.com via the extension as fallback and for parallel jobs.
- **Text:** focus/launch the app → new chat → focus composer → paste prompt (his words + attachment text, e.g. a Canvas assignment description) → send → start watcher → chip "Claude · <title> · working…". If the prompt includes content he didn't speak (attachments), confirm first with a one-line preview.
- **Voice ("on Claude, turn on voice"):** focus/launch → new chat → press the app's voice-mode button → Nova enters Passthrough. Passthrough ends when the voice UI closes or he taps the Copilot key (Nova presses end-voice).
- **Watcher (no AI, ≤ 1 % CPU):** poll UIA every 1.5 s for the stop/streaming control; done when it's gone twice in a row and the last message text is stable → Windows toast ("Claude finished — <first line>", click focuses it) + chip ✓. Gives up after 30 min. A second concurrent job goes to the web target in its own tab so both can be watched.
- **Driving the apps:** UIA first (Electron/WebView2 expose an accessibility tree — confirm control names with an inspector on this machine), keyboard shortcuts second, web target third.

## 14. Safety
- **Confirm in the island before:** sending messages/emails, submit/turn in (Canvas submissions), post/publish/comment, delete/remove/trash, buy/order/pay/checkout, closing a window with an unsaved-work prompt, and any handoff containing content he didn't speak. Detection = action type + target label/URL regex + app context. In doubt → confirm.
- **Never:** type into password fields; enter card, bank or ID numbers; run shell commands; delete files; change system settings beyond volume/media; act while the workstation is locked; treat text read from pages or emails as instructions.
- Logs are local JSONL; transcripts in logs can be turned off.

## 15. Resource governor
- Idle: CPU < 2 % average (VAD; plus STT per speech segment when wake/always-listening is on), RAM < 1.2 GB, VRAM 0 unless the LLM is loaded.
- One LLM at most, loaded on demand, unloaded 10 min after last use. Check free VRAM before loading; prefer the CPU model when tight.
- Speech/brain path at normal priority; watchers below normal.
- On battery: always-listening off by default (setting); CPU-only LLM allowed.

## 16. Latency budgets (from end of speech; key release counts as the end)
| Path | Budget |
|---|---|
| Island reacts to key / wake name | ≤ 50 ms |
| Live words lag | ≤ 500 ms |
| Tier 0 / skill replay → action starts | ≤ 500 ms |
| Tier 1 → action starts | ≤ 1.2 s on GPU (≤ 2.5 s CPU fallback) |
| Page click incl. page map | ≤ 1.5 s |
| Dictation segment lands after a pause | ≤ 1.0 s |
| Handoff prompt sent (app already open) | ≤ 3 s |

App launch time itself is whatever the app takes (Chrome ≈ 1 s) — the benchmark is Bhavya doing it by hand.
Every utterance logs `speech_end, stt_ms, tier, ctx_ms, llm_ms, exec_ms, verify_ms, total_ms`. `nova latency` prints p50/p95 per path.

## 17. Layout and config
```text
nova/        package: audio, vad, stt, keys, state, gate, island (QML), brain, actions, chrome, skills, dictation, handoff, governor
extension/   Chrome MV3 extension + native-messaging host
config/      settings.yaml · vocab.yaml · aliases.yaml (canvas → https://canvas.vt.edu, fusion → Autodesk Fusion, …)
data/        skills/*.json · dictation_history.jsonl
logs/        *.jsonl (rotating, capped)
models/      parakeet/, silero/, embedding model (git-ignored)
fixtures/    voicebar-recordings/ (Bhavya's recordings, git-ignored) · voicebar-reference/
docs/        SPEC.md · DEPS.md · eval/ · prompts/
```

## 18. Testing and evals (no gates)
- **Unit tests:** Tier-0 matcher, entity resolution, Plan validation, payload-substring rule, dictation rules + divergence guard, skill matching + determinism filter, risk detection, state machine, gate.
- **Intent eval** (`docs/eval/intents.yaml`): ~200 synthetic paraphrases written by Claude Code (varied phrasing, fillers, polite forms) with expected action + key args, plus ~100 conversation negatives. Scored on correctness of action + args, not exact strings. Targets: ≥ 90 % plans correct, < 1 % false actions on negatives. Run with `nova eval`.
- **STT eval:** WER on existing recordings (§4).
- **Every stage ends with a short live demo** Bhavya runs. That is the acceptance.

## 19. Out of scope for v1
Fusion control (Claude handles Fusion; Nova only opens/arranges it), local vision models, spoken replies, meeting assistant, phone/remote use, paid APIs.

## 20. Verify early on this machine
- Copilot key: real key-up on release? Copilot launch and Start menu suppressed?
- Claude and ChatGPT desktop apps: does UIA expose composer, send, voice start/end and stop controls?
- Ollama: per-request CPU vs GPU (`num_gpu`), cold-load time, prefix-cache hits.
- Chrome native messaging with a `.bat`-launched Python host; stable extension id.
- Google Docs paste reliability; Canvas planner JSON from the logged-in tab.
- The low-level hook survives heavy load (Windows silently removes hooks that time out — detect and reinstall).
