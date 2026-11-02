# Stage 06 — Chrome hands + Canvas

**Read:** CLAUDE.md, SPEC §9, §10, §14.
**Goal:** exact, fast on-page commands from the page structure (no screenshots): "click on my assignment for today", "click sign in", "scroll to the comments", "what's due this week".

## Build
1. `extension/` (MV3): manifest with a fixed `key` so the ID is stable (keep any private key out of git); service worker with a persistent native-messaging port; content script in all frames for `page.map / click / fill / read / highlight / wait`; tab ops in the worker.
2. `extension/host/` — native-messaging host (Python launched by a `.bat`) ↔ named pipe to Nova. `nova chrome --install` writes the host manifest + HKCU registry key and prints the one manual step (chrome://extensions → Developer mode → Load unpacked → `extension/`).
3. `nova/actions/chrome.py` — the API the executor uses; tab url/title into the context snapshot.
4. Page-map ranking in Python: top 40 by relevance to the utterance (+ section/date context), sent to Tier 1 only when the utterance points at the page.
5. Canvas adapter (SPEC §10): same-origin planner/assignment JSON from the logged-in tab (strip `while(1);`), DOM fallback. Actions `canvas_due{range}`, `canvas_open_item{item}`, `canvas_item_text{item}`. "Click on my assignment for today": one → open it; several → numbered island list; none → say so + the next due item.
6. Risky page clicks (submit / turn in / post / send / delete / buy…) → island confirm first.

## Verify yourself
Unit tests for ranking and the risk regex. A local test page (`extension/test/page.html`) for map/click/fill. Measure `page.map` and click latency.

## Live demo (Bhavya, 3 min) — Chrome open, logged in to Canvas
"go to canvas" → "click on my assignment for today" (opens it or lists them) · "what's due this week" · on any site: "click sign in", "scroll down", "go back" · point it at a Submit button → it must ask first.

Commit `stage 06: chrome + canvas`. Report latencies.
