# skillopt-windsurf

**SkillOpt-Sleep** integration for **Windsurf/Cascade** (Codeium).

Gives Cascade a nightly *sleep cycle*: reviews past sessions, mines recurring
patterns, proposes bounded edits to a long-term `SKILL.md`, and gates every
change with a held-out validation score — so only improvements that actually
make Cascade better *at your work* get adopted.

> Built on [microsoft/SkillOpt](https://github.com/microsoft/SkillOpt).
> This repo is the Windsurf-specific plugin (`plugins/windsurf/` contribution).

---

## How it works

Windsurf does not write Cascade conversation transcripts to disk.
`harvest_windsurf.py` bridges this by converting three locally available
sources into Claude Code-compatible JSONL transcripts the engine understands:

| Source | Where | What it contributes |
|---|---|---|
| **agentmemory** | `~/.agentmemory/standalone.json` | Saved memories from the [agentmemory MCP server](https://github.com/agentmemory/agentmemory) |
| **Skill files** | `.windsurf/skills/*/SKILL.md` in each workspace | Skill trigger patterns and expected behavior |
| **Extension logs** | `~/.config/Windsurf/logs/` | Best-effort user task snippets |

Workspaces are **auto-detected** from Windsurf's registry
(`~/.config/Windsurf/User/workspaceStorage/*/workspace.json`) — nothing to
configure manually.

The evolved skill is written to `.windsurf/skills/skillopt-sleep-learned/SKILL.md`
in your active workspace and synced automatically after `sleep_adopt`.

---

## Install

**Requirements:** Python ≥ 3.10, Git, Windsurf.

```bash
git clone https://github.com/YOUR_USERNAME/skillopt-windsurf.git
cd skillopt-windsurf
bash install.sh
```

`install.sh` will:
1. Clone [microsoft/SkillOpt](https://github.com/microsoft/SkillOpt) to `~/.local/share/SkillOpt`
2. Install `skillopt_sleep` (editable) into your Python environment
3. Create `~/.skillopt-sleep-windsurf/` (runtime data dir)
4. Seed `skillopt-sleep-learned/SKILL.md` into every detected workspace
5. Patch `~/.codeium/windsurf/mcp_config.json` to register the MCP server

Then **reload MCP servers** in Windsurf:
`Cmd+Shift+P` → *"Windsurf: Reload MCP Servers"*

### Optional: tell Cascade about the tools

Append `windsurf-rules.snippet.md` to your project's `.windsurfrules` (or
Windsurf global rules) so Cascade automatically offers the sleep cycle when
relevant.

### Manual config

If you prefer to configure manually, add this to
`~/.codeium/windsurf/mcp_config.json`:

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

---

## Use

Ask Cascade:

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
├── mcp_server.py             MCP server (stdlib-only, stdio)
├── harvest_windsurf.py       Windsurf-specific transcript generator
├── mcp-config.example.json   Drop-in MCP config snippet
├── windsurf-rules.snippet.md Paste into .windsurfrules
├── seed_skill/
│   └── SKILL.md              Initial skill seed (replaced by sleep_adopt)
├── install.sh                One-shot installer
└── README.md
```

---

## Contributing / upstream

This plugin is being contributed back to
[microsoft/SkillOpt](https://github.com/microsoft/SkillOpt) as
`plugins/windsurf/`.  Bug reports and improvements welcome here or upstream.

## License

MIT — same as microsoft/SkillOpt.
