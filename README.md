# skillopt-windsurf

**SkillOpt-Sleep** integration for **Windsurf/Cascade** (Codeium) and **Devin** (Cognition).

Gives both agents a nightly *sleep cycle*: reviews past sessions, mines recurring
patterns, proposes bounded edits to a long-term `SKILL.md`, and gates every
change with a held-out validation score — so only improvements that actually
make the agent better *at your work* get adopted.

> Built on [microsoft/SkillOpt](https://github.com/microsoft/SkillOpt).
> This repo is the Windsurf+Devin plugin (`plugins/windsurf/` contribution).

---

## How it works

Neither Windsurf nor Devin write conversation transcripts to disk in a format
the sleep engine understands.  `harvest_windsurf.py` bridges this by converting
every locally available source into Claude Code-compatible JSONL transcripts:

| Source | Where | What it contributes |
|---|---|---|
| **Devin transcripts** | `~/.local/share/devin/cli/transcripts/*.json` | Native ATIF-v1.7 sessions — real user↔agent turns |
| **agentmemory** | `~/.agentmemory/standalone.json` | Saved memories from the [agentmemory MCP server](https://github.com/agentmemory/agentmemory) |
| **Skill files** | `.windsurf/skills/*/SKILL.md` and `.devin/skills/*/SKILL.md` | Skill trigger patterns and expected behavior |
| **Extension logs** | `~/.config/Windsurf/logs/` | Best-effort Cascade task snippets |

Workspaces are **auto-detected** from both registries (nothing to configure):
- Windsurf: `~/.config/Windsurf/User/workspaceStorage/*/workspace.json`
- Devin: `~/.config/Devin/User/workspaceStorage/*/workspace.json`

After `sleep_adopt` the evolved skill is synced to **both**
`.windsurf/skills/skillopt-sleep-learned/SKILL.md` **and**
`.devin/skills/skillopt-sleep-learned/SKILL.md` automatically.

---

## Install

**Requirements:** Python ≥ 3.10, Git. Works with Windsurf, Devin, or both.

```bash
git clone https://github.com/YOUR_USERNAME/skillopt-windsurf.git
cd skillopt-windsurf
bash install.sh
```

`install.sh` will:
1. Clone [microsoft/SkillOpt](https://github.com/microsoft/SkillOpt) to `~/.local/share/SkillOpt`
2. Install `skillopt_sleep` (editable) into your Python environment
3. Create `~/.skillopt-sleep-windsurf/` (runtime data dir)
4. Seed `skillopt-sleep-learned/SKILL.md` into every detected workspace (`.windsurf/skills/` **and** `.devin/skills/`)
5. Patch `~/.codeium/windsurf/mcp_config.json` to register the MCP server with Windsurf
6. Auto-register with **Devin CLI** MCP (`devin mcp add skillopt-sleep`) if the Devin CLI is on PATH

### Windsurf post-install

Reload MCP servers: `Cmd+Shift+P` → *"Windsurf: Reload MCP Servers"*

Optionally append `windsurf-rules.snippet.md` to your `.windsurfrules`.

### Devin post-install

MCP registration is automatic if the Devin CLI is installed.
Optionally copy `devin-rules.snippet.md` to `.devin/rules/skillopt-sleep.md` in your workspace so Devin knows to offer the sleep tools.

### Manual config

**Windsurf** — add to `~/.codeium/windsurf/mcp_config.json`:

```json
{
  "mcpServers": {
    "skillopt-sleep": {
      "command": "python3",
      "args": ["/abs/path/to/skillopt-windsurf/mcp_server.py"],
      "env": {
        "SKILLOPT_SLEEP_REPO": "/abs/path/to/SkillOpt",
        "SKILLOPT_WINDSURF_CLAUDE_HOME": "~/.skillopt-sleep-windsurf"
      }
    }
  }
}
```

**Devin** — run once in a terminal:

```bash
devin mcp add skillopt-sleep \
  --env "SKILLOPT_SLEEP_REPO=/abs/path/to/SkillOpt" \
  --env "SKILLOPT_WINDSURF_CLAUDE_HOME=$HOME/.skillopt-sleep-windsurf" \
  -- python3 /abs/path/to/skillopt-windsurf/mcp_server.py
```

---

## Use

Ask Cascade or Devin:

> *"run the sleep cycle"*, *"what did the last sleep propose?"*, *"adopt it"*

Or call tools directly:

| Tool | What it does |
|---|---|
| `sleep_status` | nights run so far + latest staged proposal |
| `sleep_dry_run` | preview cycle — no staging, no changes |
| `sleep_run` | full cycle; stages a proposal for your review |
| `sleep_adopt` | apply the staged proposal; syncs skill to workspace |
| `sleep_harvest` | debug: list the recurring tasks mined |

Each tool accepts:

| Argument | Values | Default |
|---|---|---|
| `project` | abs path | cwd |
| `backend` | `mock` / `claude` / `codex` | `mock` |
| `scope` | `invoked` / `all` | `invoked` |

`mock` is free (no API calls). For real LLM optimization:
- `backend: "claude"` → set `ANTHROPIC_API_KEY`
- `backend: "codex"` → set `OPENAI_API_KEY`

---

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `SKILLOPT_SLEEP_REPO` | `~/.local/share/SkillOpt` | Path to the SkillOpt repo |
| `SKILLOPT_WINDSURF_CLAUDE_HOME` | `~/.skillopt-sleep-windsurf` | Runtime data dir |
| `SKILLOPT_WINDSURF_WORKSPACES` | auto-detected | Colon-separated workspace paths |
| `SKILLOPT_MANAGED_SKILL` | `skillopt-sleep-learned` | Skill name to evolve |

---

## Verify (no Windsurf needed)

```bash
SKILLOPT_SLEEP_REPO=~/.local/share/SkillOpt \
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | python3 mcp_server.py
```

---

## Project structure

```
skillopt-windsurf/
├── mcp_server.py              MCP server (stdlib-only, stdio) — Windsurf + Devin
├── harvest_windsurf.py        Transcript generator (Devin ATIF-v1.7 + agentmemory + skills + logs)
├── mcp-config.example.json    Windsurf MCP config snippet
├── windsurf-rules.snippet.md  Paste into .windsurfrules
├── devin-rules.snippet.md     Copy to .devin/rules/skillopt-sleep.md
├── seed_skill/
│   └── SKILL.md               Initial skill seed (replaced by sleep_adopt)
├── install.sh                 One-shot installer (Windsurf + Devin auto-detected)
└── README.md
```

---

## Contributing / upstream

This plugin is being contributed back to
[microsoft/SkillOpt](https://github.com/microsoft/SkillOpt) as
`plugins/windsurf/`.  Bug reports and improvements welcome here or upstream.

## License

MIT — same as microsoft/SkillOpt.
