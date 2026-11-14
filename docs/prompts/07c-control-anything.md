# Stage 07c — Control anything on screen

**Read:** CLAUDE.md, SPEC §7.5, §8, §8.1 (finding things on screen), §9, §14; GOTCHAS "Windows input and focus" and "Chrome extension". Needs 07a; best after 07b.
**Goal:** no keyboard, no mouse. Scroll any document (PDFs in Chrome included), click anything he can see, press any key, take a photo or a screenshot, open a folder — by voice.

Found in the 2026-11-11 audit: "click OK" in a native app brings Chrome forward and clicks there; a PDF in Chrome answers "Already at the bottom" without moving; right-click, double-click, drag, screenshot, camera shutter, open-a-folder and "keep scrolling" don't exist.

## Build
1. **Action receipts.** Every hands action returns what changed (focus, window, URL, scroll position, a new dialog, text that landed), on top of today's `{ok, detail}`. Used for verification, the island line, learning (08), and to enrich the session context 07b already carries. "Nothing changed after clicking X" is a failure, not a success.
   - **UIA needs real bindings first.** `nova/actions/uia.py` is hand-written ctypes for the focused control and TextPattern only. FindAll with a cache request, Invoke, ScrollPattern, bounding rectangles and element-from-point are all new: extend the ctypes or add a small binding (check size and licence, one line in DEPS.md). Tagging 300 elements in 0.5 s needs cached batch reads.
   - GOTCHAS says not to query UIA on every command (it switches Chrome's accessibility tree on). That still holds: UIA runs for `ui_click`, numbers and scroll fallbacks only, and inside Chrome the page map is used for the page — UIA only for Chrome's own toolbar when he asks for numbers.
2. **Scroll that always moves.** Try in order until the receipt shows movement: the page (extension) → UIA `ScrollPattern` on the content under focus → PageDown / arrow / Home / End keys to the focused content → mouse wheel at the content's centre. "Already at the end" only when two methods agree.
   - Amounts: "a bit" (¼ page), a page, "halfway", "to the top / bottom".
   - "keep scrolling" / "scroll slowly": steady wheel ticks until "stop", the end, or the session closes; "faster" / "slower".
   - Chrome's PDF viewer can't be scripted by an extension: focus the PDF content and send real keys / wheel. "go to page N" → `chrome.tabs.update` with `#page=N` (verify on Chrome 156; fall back to the page box via UIA). "next / previous page", "zoom in / out", "fit to width".
3. **Click by name, wherever he is.** New `ui_click{name, role?, near?, button?, count?}` through UIA on the window in front. Rank exact > starts-with > whole word > contains. One match that isn't risky → act (Invoke pattern, else a real click at its centre). Several → numbers on those candidates. None → step 4.
   - "click X" goes to the surface in front: the page map when Chrome is in front, UIA otherwise. Chrome only comes forward when he names Chrome or the page.
   - Dialog buttons count (OK, Cancel, Save, Don't save). The risk check runs on the resolved label, and a plain "OK" is risky when the dialog's own text is (delete, discard, send).
4. **Numbers.** "show numbers" — and automatically when a named click is ambiguous or finds nothing — puts a small numbered tag on everything clickable. See SPEC §8.1 for the sources and their order. "twelve" / "click 12" / "right-click 12" / "double-click 12" / "hover 12" / "drag 12 to 30" act. Tags go away after the action, on "hide numbers", Esc, or 15 s.
   - "numbers here" = the window in front (default); "numbers everywhere" = the monitor under it.
   - A bare number is a command only while tags are showing.
   - The tag layer is its own click-through, always-on-top window, excluded from capture so tags never feed back into detection.
   - OCR is Windows' own (`Windows.Media.Ocr` through the `winrt-*` packages — check the cp312 wheels and read `OcrEngine.MaxImageDimension` at runtime), run only on the window in scope, words grouped into lines. One screenshot per request (`mss` is enough).
   - Busy screens: cap at 99 tags, page the rest ("more numbers"); skip anything under 8 logical px.
5. **Grid.** "show grid": 3 × 3 numbered cells on the monitor, say a cell to zoom in (3 levels), then "click" / "right-click" / "double-click" / "drag to …". The fallback for anything nothing else finds.
6. **Mouse verbs.** Left / right / double / middle click, hover and drag at an element's centre via SendInput absolute coordinates (per-monitor DPI).
7. **System.**
   - `screenshot{target: screen | window}` → `Pictures\Screenshots` + clipboard; the island says where it went.
   - `camera{op: photo}` — "take a picture": open the Camera app if needed, wait for the preview, invoke its shutter through UIA (fall back to the Space key), confirm a new file in `Pictures\Camera Roll`.
   - `system{op: lock}` (the `LockWorkStation` call — synthesized Win+L does nothing). Sleeping the PC confirms first.
   - `open_path{name}`: known folders (Downloads, Documents, Desktop, Pictures, …) and a fuzzy file or folder name inside them. In File Explorer: up / back / forward, and items open by name through step 3.
8. **Talking to Nova itself.** "what can I say?" → an island card by category, generated from the action list (not a phrase list). "do that again". "undo that" (Ctrl+Z in the app it ran in). Guessed-verb questions set `guess=True` so Tier 1 gets a look first (`tier0.py:1062`).
9. **Schema.** Plan + validate + risk + Tier 0 templates + Tier 1 prompt + `docs/eval/intents.yaml` rows for every new action. The Tier 1 prompt must still meet 07a's budget — count its tokens before and after.

**Not in this stage:** a screenshot detector for unlabeled icons (SPEC §8.1). Bhavya decides after this demo. To make that decision easy, `nova numbers --dump` saves the tagged boxes + a screenshot of the window in scope to `data/` (git-ignored), so the things numbers missed on his real apps can be counted.

## Verify yourself
Unit tests: name ranking, box merging, number parsing ("twenty one", "21"), scroll-cascade order against a fake hands layer, risk on dialog labels. Focus-free checks on a throwaway window (the raw Win32 RichEdit pad from Stage 07, Chrome for Testing with a local PDF). `nova eval` ≥ 90 % including the new rows. Ask once before the focus-taking run.

## Live demo (Bhavya, 4 min)
1. A PDF in Chrome: "scroll down", "keep scrolling" … "stop", "go to page 5", "zoom in".
2. Notepad: type something, "close this", then answer the dialog by name ("don't save").
3. "show numbers" → say the number of an icon with no label (taskbar or a toolbar).
4. "take a screenshot". "take a picture".
5. "open my downloads folder", then open a file by name.
6. In a session, the whole recording from 07b, camera included.

Commit `stage 07c: control anything`. Report.
