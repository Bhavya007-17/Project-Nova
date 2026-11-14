# Stage 12 (optional) — Integrations + MCP

Only when Bhavya asks for one of these. One service or one feature per pull request.

**Read:** CLAUDE.md (non-negotiable 1 and 5), SPEC §9, §10, §14; Stage 10's risk tables.

## A. Direct app hooks (zero clicking)
Todoist ("add a task to … tomorrow"), Notion ("add this to my notes page …"), Google Calendar ("what's next today").

- **First choice: the Canvas pattern.** A site adapter that reads and writes through his own signed-in tab (the extension, same origin, no token) — see `nova/chrome/canvas.py` and SPEC §10. All three have web apps. Nothing new leaves the laptop: only what his browser already sends.
- **Second choice: the service's API.** It breaks "no API keys" (CLAUDE.md), so it is an exception he approves per service, in words, before any code. Then: free tier only; the token lives in Windows Credential Manager (never in `settings.yaml`, the logs or a trace); calls go to that service's host only; a `nova doctor` row says whether it is set up.
- Either way the new actions go through Plan → validate → risk → executor like every other one. Reads are safe. Anything that writes confirms with the exact text ("Add “buy filament” to Todoist for tomorrow?"). The payload rule holds: a task's text is a span of his words. Add the risk-table rows — Stage 10's test fails until they exist — and eval rows for each action.
- Demo: three phrasings that work, and one Confirm he declines.

## B. Nova as an MCP server
So Claude desktop, Claude Code or Jarvis can use Nova as hands.

- **Shape.** An MCP client starts its server as its own child process over stdio, so the server can't be the tray app. `nova mcp` is a thin relay to the running Nova over a named pipe — the same shape as `extension/host/nova_host.py` (a key in `data/`; it exits when Nova isn't running). Check the current MCP spec version and the size of its Python SDK before choosing between the SDK and ~150 lines of JSON-RPC (initialize, list tools, call tool).
- **The caller is a model that may have read anything. Treat every call as untrusted.**
  - Tools are generated from the action schema. On by default: read-only and safe ones (list windows and tabs, page map and page text, open an app or address, arrange windows, scroll, screenshot to a file).
  - Typing, clicks, fills and closing are off by default, switched on per setting, and each call confirms in the island with the exact action and who asked ("Claude wants to type “…” into Notepad"). There is no transcript here, so the payload rule can't vouch for the text.
  - Never over MCP: shell, files, password fields, or a handoff (no loops).
- A rate limit (about 2 actions a second, 60 a minute). Every call traced with `via: "mcp"`. A chip while a client is connected; a tray item to disconnect.
- Risk-table rows and injection tests as in Stage 10.
- Demo, from Claude desktop: "put Chrome on the left and Notepad on the right" works; "type hello into Notepad" asks in the island; saying no gives Claude a refusal it can read.
