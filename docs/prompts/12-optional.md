# Stage 12 (optional) — Integrations + MCP

Only when Bhavya asks for one of these.

**A. Direct app hooks (zero clicking).** Todoist ("add a task to … tomorrow"), Notion ("add this to my notes page…"), Google Calendar ("what's next today"). Free tokens/OAuth he sets up once, stored in Windows Credential Manager. Actions go through the same Plan + risk system; anything that writes confirms in the island.

**B. Nova as an MCP server.** Expose Nova's actions over stdio so Claude desktop, Claude Code or Jarvis can use Nova as hands. Read-only and safe actions by default; risky ones still confirm in the island.
