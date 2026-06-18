# SkillOpt-Sleep (Devin IDE)

You have access to a nightly self-evolution cycle via the `skillopt-sleep` MCP
server.  Use these tools to improve your long-term skills over time:

- **`sleep_status`** — show how many nights have run and the latest staged proposal
- **`sleep_dry_run`** — preview a sleep cycle without changing anything
- **`sleep_run`** — run a full cycle; stages a proposal for your review
- **`sleep_adopt`** — apply the staged proposal to `.devin/skills/skillopt-sleep-learned/SKILL.md`
- **`sleep_harvest`** — debug: list the recurring tasks mined from recent sessions

When a user asks about the sleep cycle, skill evolution, or improving your
long-term memory, prefer calling these tools over explaining the concept.

Default backend is `mock` (no API spend).  Pass `backend: "claude"` or
`backend: "codex"` with your own API key for real LLM-driven optimization.

Place this file in `.devin/rules/skillopt-sleep.md` in your workspace.
