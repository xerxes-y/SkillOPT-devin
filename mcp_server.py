#!/usr/bin/env python3
"""Memento — Devin MCP server (stdio, stdlib-only).

Exposes the sleep engine as MCP tools so Devin can drive it.
Speaks JSON-RPC 2.0 over stdio with just the handful of MCP methods Devin
needs.  No third-party deps beyond the SkillOpt repo itself.

Before each tool call this server runs ``harvest_devin.py`` to convert
locally available Devin data (ATIF-v1.7 transcripts, agentmemory memories,
and .devin skill files) into the Claude Code-compatible JSONL transcripts
that the sleep engine consumes.

After ``memento_adopt`` the evolved SKILL.md is also synced back into the active
Devin workspace's ``.devin/skills/`` directory so Devin picks it up immediately.

Tools exposed (identical interface to the Copilot plugin):
  memento_status    show how many nights have run + latest staged proposal
  memento_dry_run   harvest+mine+replay, report only (no staging)
  memento_run       full cycle; stages a reviewed proposal
  memento_adopt     apply the latest staged proposal
  memento_harvest   debug: list mined recurring tasks
  memento_auto      run + auto-adopt above the gate; report the SKILL.md diff

Configure Devin to launch::

    python plugins/devin/mcp_server.py

with ``MEMENTO_ENGINE_REPO`` set to this repo's root.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import sys

# ── constants ─────────────────────────────────────────────────────────────────

REPO_ROOT = (
    os.environ.get("MEMENTO_ENGINE_REPO")
    or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
)
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
CLAUDE_HOME = os.environ.get(
    "MEMENTO_HOME",
    os.path.expanduser("~/.memento"),
)
MANAGED_SKILL_NAME = os.environ.get("MEMENTO_MANAGED_SKILL", "memento-learned")
PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "memento_status",
        "action": "status",
        "description": "Show how many Memento nights have run and the latest staged proposal.",
    },
    {
        "name": "memento_dry_run",
        "action": "dry-run",
        "description": "Preview a sleep cycle (harvest+mine+replay) without staging anything.",
    },
    {
        "name": "memento_run",
        "action": "run",
        "description": "Run a full sleep cycle; stages a reviewed proposal. Nothing live changes until adopt.",
    },
    {
        "name": "memento_adopt",
        "action": "adopt",
        "description": (
            "Apply the latest staged proposal to the managed SKILL.md. "
            "Also syncs the evolved skill into the Devin workspace so Devin picks it up immediately."
        ),
    },
    {
        "name": "memento_harvest",
        "action": "harvest",
        "description": "Debug: list the recurring tasks mined from recent Devin sessions.",
    },
    {
        "name": "memento_auto",
        "action": "auto",
        "description": (
            "Fully automatic: run a sleep cycle and immediately adopt the staged "
            "proposal. Adoption is gated by the engine's held-out validation, plus "
            "an optional MEMENTO_AUTO_ADOPT_MIN_SCORE floor. Returns a before/after "
            "diff report of the SKILL.md change so the user can see what changed."
        ),
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
    harvester = os.path.join(PLUGIN_DIR, "harvest_devin.py")
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
        return f"[harvest_devin] warning: {exc}"

# ── post-adopt: sync evolved skill into workspace (.devin) ────────────────────

def _sync_skill(project: str) -> str:
    src = os.path.join(CLAUDE_HOME, "skills", MANAGED_SKILL_NAME, "SKILL.md")
    if not os.path.isfile(src):
        return ""
    if not project or not os.path.isdir(project):
        return ""
    synced = []
    dot_root = os.path.join(project, ".devin")
    if os.path.isdir(dot_root):
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

# ── fully automatic: run → gate → adopt → report diff ─────────────────────────

def _managed_skill_path() -> str:
    return os.path.join(CLAUDE_HOME, "skills", MANAGED_SKILL_NAME, "SKILL.md")


def _read_skill() -> str:
    try:
        with open(_managed_skill_path(), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _extract_score(text: str):
    """Best-effort parse of a validation score from engine stdout.

    Returns a float in roughly [0, 1] or None. Used only to enforce the optional
    MEMENTO_AUTO_ADOPT_MIN_SCORE floor; the engine's own held-out gate is the
    real safety mechanism, so a None here just means "defer to the engine".
    """
    matches = re.findall(r"score[^0-9\-]*([0-9]+(?:\.[0-9]+)?)", text, re.IGNORECASE)
    if not matches:
        return None
    try:
        return float(matches[-1])
    except ValueError:
        return None


def _skill_diff(before: str, after: str) -> str:
    if before == after:
        return ""
    return "\n".join(difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile="SKILL.md (before)", tofile="SKILL.md (after)", lineterm="",
    ))


def _run_auto(args: dict) -> str:
    """Run a cycle, adopt the staged proposal if it clears the gate, report the diff.

    Two gates apply, narrowest first:
      1. the engine only *stages* a proposal when it strictly improves its
         held-out validation score (upstream behavior, always on);
      2. an optional MEMENTO_AUTO_ADOPT_MIN_SCORE floor enforced here, applied
         only when a score is parseable from the run output.
    """
    raw_floor = os.environ.get("MEMENTO_AUTO_ADOPT_MIN_SCORE")
    try:
        min_score = float(raw_floor) if raw_floor else None
    except ValueError:
        min_score = None

    before = _read_skill()
    run_out = _run_engine("run", args)
    score = _extract_score(run_out)

    if min_score is not None and score is not None and score < min_score:
        return (
            f"{run_out}\n\n[auto] validation score {score:.3f} < threshold "
            f"{min_score:.3f} — proposal NOT adopted."
        )

    adopt_out = _run_engine("adopt", args)
    after = _read_skill()
    diff = _skill_diff(before, after)

    report = [run_out, "", adopt_out, "", "[auto] === skill change report ==="]
    if score is not None:
        line = f"[auto] validation score: {score:.3f}"
        if min_score is not None:
            line += f" (threshold {min_score:.3f})"
        report.append(line)
    if diff:
        report.append(f"[auto] SKILL.md updated ({MANAGED_SKILL_NAME}):\n{diff}")
    else:
        report.append("[auto] no change to SKILL.md "
                      "(engine staged nothing, or the proposal was empty).")
    return "\n".join(report)

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
            "serverInfo": {"name": "memento", "version": "0.1.0"},
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
        tool_args = params.get("arguments") or {}
        if tool["action"] == "auto":
            text = _run_auto(tool_args)
        else:
            text = _run_engine(tool["action"], tool_args)
        return _result(id_, {"content": [{"type": "text", "text": text}]})
    if method == "ping":
        return _result(id_, {})
    return _error(id_, -32601, f"method not found: {method}")


def run_auto_cli(argv) -> int:
    """Standalone, non-MCP entrypoint for scheduled (launchd/cron) runs.

    Usage: python3 mcp_server.py --auto [--project PATH] [--backend mock|claude|codex]
                                        [--scope invoked|all]
    Runs one full auto cycle (run → gate → adopt) and prints the change report.
    """
    args = {}
    it = iter(argv)
    for tok in it:
        if tok == "--auto":
            continue
        if tok in ("--project", "--backend", "--scope"):
            args[tok[2:]] = next(it, "")
    sys.stdout.write(_run_auto(args) + "\n")
    sys.stdout.flush()
    return 0


def main() -> int:
    if "--auto" in sys.argv[1:]:
        return run_auto_cli(sys.argv[1:])
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
