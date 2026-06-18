#!/usr/bin/env python3
"""SkillOpt-Sleep — Windsurf MCP server (stdio, stdlib-only).

Exposes the sleep engine as MCP tools so Windsurf/Cascade can drive it.
Speaks JSON-RPC 2.0 over stdio with just the handful of MCP methods Windsurf
needs.  No third-party deps beyond the SkillOpt repo itself.

Before each tool call this server runs ``harvest_windsurf.py`` to convert
locally available Windsurf data (agentmemory memories, .windsurf skill files,
and extension-host log snippets) into the Claude Code-compatible JSONL
transcripts that the sleep engine consumes.

After ``sleep_adopt`` the evolved SKILL.md is also synced back into the active
Windsurf workspace's ``.windsurf/skills/`` directory so Cascade picks it up
immediately.

Tools exposed (identical interface to the Copilot plugin):
  sleep_status    show how many nights have run + latest staged proposal
  sleep_dry_run   harvest+mine+replay, report only (no staging)
  sleep_run       full cycle; stages a reviewed proposal
  sleep_adopt     apply the latest staged proposal
  sleep_harvest   debug: list mined recurring tasks

Configure Windsurf to launch::

    python plugins/windsurf/mcp_server.py

with ``SKILLOPT_SLEEP_REPO`` set to this repo's root.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

# ── constants ─────────────────────────────────────────────────────────────────

REPO_ROOT = (
    os.environ.get("SKILLOPT_SLEEP_REPO")
    or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
)
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
CLAUDE_HOME = os.environ.get(
    "SKILLOPT_WINDSURF_CLAUDE_HOME",
    os.path.expanduser("~/.skillopt-sleep-windsurf"),
)
MANAGED_SKILL_NAME = os.environ.get("SKILLOPT_MANAGED_SKILL", "skillopt-sleep-learned")
PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "sleep_status",
        "action": "status",
        "description": "Show how many SkillOpt-Sleep nights have run and the latest staged proposal.",
    },
    {
        "name": "sleep_dry_run",
        "action": "dry-run",
        "description": "Preview a sleep cycle (harvest+mine+replay) without staging anything.",
    },
    {
        "name": "sleep_run",
        "action": "run",
        "description": "Run a full sleep cycle; stages a reviewed proposal. Nothing live changes until adopt.",
    },
    {
        "name": "sleep_adopt",
        "action": "adopt",
        "description": (
            "Apply the latest staged proposal to the managed SKILL.md. "
            "Also syncs the evolved skill into the Windsurf workspace so Cascade picks it up immediately."
        ),
    },
    {
        "name": "sleep_harvest",
        "action": "harvest",
        "description": "Debug: list the recurring tasks mined from recent Windsurf sessions.",
    },
]
_BY_NAME = {t["name"]: t for t in TOOLS}

_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "project": {
            "type": "string",
            "description": "Project dir to evolve (default: cwd).",
        },
        "backend": {
            "type": "string",
            "enum": ["mock", "claude", "codex"],
            "description": "mock = no API spend (default); claude/codex = real.",
        },
        "scope": {"type": "string", "enum": ["invoked", "all"]},
    },
    "additionalProperties": False,
}

# ── harvest step ──────────────────────────────────────────────────────────────

def _run_harvest() -> str:
    harvester = os.path.join(PLUGIN_DIR, "harvest_windsurf.py")
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    try:
        proc = subprocess.run(
            [sys.executable, harvester, "--out-dir", CLAUDE_HOME],
            capture_output=True, text=True, timeout=60, env=env,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        return out + (("\n[harvest stderr]\n" + err) if err else "")
    except Exception as exc:
        return f"[harvest_windsurf] warning: {exc}"

# ── post-adopt: sync evolved skill into workspace (.windsurf + .devin) ────────

def _sync_skill(project: str) -> str:
    src = os.path.join(CLAUDE_HOME, "skills", MANAGED_SKILL_NAME, "SKILL.md")
    if not os.path.isfile(src):
        return ""
    if not project or not os.path.isdir(project):
        return ""
    synced = []
    for dot_dir in (".windsurf", ".devin"):
        dot_root = os.path.join(project, dot_dir)
        if not os.path.isdir(dot_root):
            continue
        dst_dir = os.path.join(dot_root, "skills", MANAGED_SKILL_NAME)
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, "SKILL.md")
        shutil.copy2(src, dst)
        synced.append(dst)
    return ("\n" + "\n".join(f"[sleep] synced evolved skill → {p}" for p in synced)
            if synced else "")

# ── engine call ───────────────────────────────────────────────────────────────

def _run_engine(action: str, args: dict) -> str:
    harvest_out = _run_harvest()

    project = args.get("project") or os.getcwd()
    backend = args.get("backend") or "mock"
    scope = args.get("scope") or "invoked"

    cmd = [
        sys.executable, "-m", "skillopt_sleep", action,
        "--claude-home", CLAUDE_HOME,
        "--project", project,
        "--scope", scope,
        "--backend", backend,
        "--source", "claude",
    ]
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=3600, env=env,
        )
    except Exception as exc:
        return f"[harvest]\n{harvest_out}\n[error] failed to run engine: {exc}"

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    result = f"[harvest]\n{harvest_out}\n\n[engine]\n{out}"
    if err:
        result += f"\n[stderr]\n{err}"
    if action == "adopt":
        result += _sync_skill(project)
    return result

# ── JSON-RPC / MCP plumbing ───────────────────────────────────────────────────

def _result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def handle(req: dict):
    method = req.get("method")
    id_ = req.get("id")
    if method == "initialize":
        return _result(id_, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "skillopt-sleep-windsurf", "version": "0.1.0"},
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "tools/list":
        return _result(id_, {"tools": [
            {"name": t["name"], "description": t["description"],
             "inputSchema": _TOOL_SCHEMA}
            for t in TOOLS
        ]})
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        tool = _BY_NAME.get(name)
        if not tool:
            return _error(id_, -32602, f"unknown tool: {name}")
        text = _run_engine(tool["action"], params.get("arguments") or {})
        return _result(id_, {"content": [{"type": "text", "text": text}]})
    if method == "ping":
        return _result(id_, {})
    return _error(id_, -32601, f"method not found: {method}")


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        resp = handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
