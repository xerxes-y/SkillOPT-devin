# Memento (Devin IDE)

You have access to a nightly self-evolution cycle via the `memento` MCP
server.  Use these tools to improve your long-term skills over time:

- **`memento_auto`** — run a full cycle **and** auto-adopt the staged proposal,
  then show the user the SKILL.md diff. This is the default for hands-off
  self-improvement.
- **`memento_status`** — show how many nights have run and the latest staged proposal
- **`memento_dry_run`** — preview a sleep cycle without changing anything
- **`memento_run`** — run a full cycle; stages a proposal for manual review
- **`memento_adopt`** — apply the staged proposal to `.devin/skills/memento-learned/SKILL.md`
- **`memento_harvest`** — debug: list the recurring tasks mined from recent sessions

## When to act

- For routine self-improvement, prefer **`memento_auto`**. It only adopts a change
  when it strictly improves the engine's held-out validation score (and, if
  `MEMENTO_AUTO_ADOPT_MIN_SCORE` is set, clears that floor too), so it is safe
  to run unattended.
- **Always surface the skill-change report** that `memento_auto` returns to the
  user — show the diff and the validation score so they can see what changed.
- If the user wants to review before anything is written, use `memento_run` (stage
  only) then `memento_adopt` after they approve.

When a user asks about the sleep cycle, skill evolution, or improving your
long-term memory, prefer calling these tools over explaining the concept.

Default backend is `mock` (no API spend).  Pass `backend: "claude"` or
`backend: "codex"` with your own API key for real LLM-driven optimization.

Place this file in `.devin/rules/memento.md` in your workspace.
