#!/usr/bin/env python3
"""Convert Windsurf/Cascade local data into Claude Code-format JSONL transcripts.

Windsurf (Codeium) does not persist Cascade conversation transcripts to the
local filesystem the way Claude Code does.  This script bridges that gap by
synthesising transcript JSONL files from sources that *are* available locally:

  1. **agentmemory** (~/.agentmemory/standalone.json)
     Memories saved by the `agentmemory` MCP server — each memory's title
     becomes a synthetic user prompt; its content becomes the assistant reply.

  2. **Windsurf skill files** (.windsurf/skills/*/SKILL.md in each workspace)
     Each skill description is converted to a session where the user asked
     "use the <skill> skill" and the assistant described how to apply it.

  3. **Windsurf extension logs** (~/.config/Windsurf/logs/)
     User-facing task snippets extracted from the Cascade extension log lines
     (best-effort; empty when no useful content is present).

Output layout (mirrors ~/.claude/projects/<slug>/<sessionId>.jsonl):
    <out_dir>/projects/<slug>/<session_id>.jsonl

Workspace auto-detection order:
  1. ``SKILLOPT_WINDSURF_WORKSPACES`` env var — colon-separated abs paths
  2. ``~/.config/Windsurf/User/workspaceStorage/*/workspace.json`` — Windsurf's
     own registry of recently opened folders
  3. Working directory fallback

Usage (standalone):
    python harvest_windsurf.py [--out-dir PATH] [--workspaces PATH ...]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── workspace auto-detection ─────────────────────────────────────────────────

def _detect_workspaces() -> List[str]:
    """Return list of known Windsurf workspace paths, newest first."""
    env_val = os.environ.get("SKILLOPT_WINDSURF_WORKSPACES", "")
    if env_val:
        return [p for p in env_val.split(":") if p and os.path.isdir(p)]

    storage = os.path.expanduser("~/.config/Windsurf/User/workspaceStorage")
    results: List[tuple] = []
    if os.path.isdir(storage):
        for entry in os.scandir(storage):
            ws_json = os.path.join(entry.path, "workspace.json")
            if not os.path.isfile(ws_json):
                continue
            try:
                with open(ws_json, encoding="utf-8") as f:
                    data = json.load(f)
                folder = data.get("folder", "")
                if folder.startswith("file://"):
                    folder = folder[len("file://"):]
                if folder and os.path.isdir(folder):
                    mtime = os.path.getmtime(ws_json)
                    results.append((mtime, folder))
            except Exception:
                continue
    results.sort(reverse=True)
    paths = [p for _, p in results]
    return paths if paths else [os.getcwd()]

# ── helpers ───────────────────────────────────────────────────────────────────

def _slug(path: str) -> str:
    """SHA-256 of abs-path, first 16 hex chars — matches Claude Code's scheme."""
    return hashlib.sha256(os.path.abspath(path).encode()).hexdigest()[:16]


def _iso(epoch_ms: Optional[float] = None) -> str:
    dt = (datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc)
          if epoch_ms is not None else datetime.now(tz=timezone.utc))
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _write_session(
    out_dir: str, project: str, session_id: str,
    user_prompts: List[str], assistant_replies: List[str],
    timestamp_base_ms: float,
) -> None:
    slug = _slug(project)
    session_dir = os.path.join(out_dir, "projects", slug)
    os.makedirs(session_dir, exist_ok=True)
    out_path = os.path.join(session_dir, f"{session_id}.jsonl")
    ts = timestamp_base_ms
    with open(out_path, "w", encoding="utf-8") as f:
        for user_text, asst_text in zip(user_prompts, assistant_replies):
            f.write(json.dumps({
                "type": "user",
                "message": {"role": "user", "content": user_text},
                "cwd": project,
                "timestamp": _iso(ts),
                "sessionId": session_id,
                "version": "1.0",
            }, ensure_ascii=False) + "\n")
            ts += 1000
            f.write(json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "content": asst_text},
                "timestamp": _iso(ts),
                "sessionId": session_id,
                "version": "1.0",
            }, ensure_ascii=False) + "\n")
            ts += 2000


def _append_history(out_dir: str, display: str, project: str, timestamp_ms: float) -> None:
    record = {"display": display, "timestamp": timestamp_ms, "project": project}
    with open(os.path.join(out_dir, "history.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _infer_project(text: str, workspaces: List[str]) -> str:
    for ws in workspaces:
        if os.path.basename(ws.rstrip("/")).lower() in text.lower():
            return ws
    return workspaces[0] if workspaces else os.getcwd()

# ── source 1: agentmemory ─────────────────────────────────────────────────────

def harvest_agentmemory(agentmemory_path: str, out_dir: str,
                        workspaces: List[str]) -> int:
    if not os.path.isfile(agentmemory_path):
        return 0
    with open(agentmemory_path, encoding="utf-8") as f:
        data = json.load(f)
    memories: Dict[str, Any] = data.get("mem:memories", {})
    written = 0
    base_ts = datetime.now(tz=timezone.utc).timestamp() * 1000 - len(memories) * 60_000
    for i, (mem_id, mem) in enumerate(memories.items()):
        title = str(mem.get("title", "")).strip()
        content = str(mem.get("content", "")).strip()
        if not title or not content:
            continue
        project = _infer_project(title + " " + content, workspaces)
        ts = base_ts + i * 60_000
        _write_session(out_dir, project, mem_id,
                       user_prompts=[title],
                       assistant_replies=[content],
                       timestamp_base_ms=ts)
        _append_history(out_dir, display=title[:120], project=project, timestamp_ms=ts)
        written += 1
    return written

# ── source 2: Windsurf skill files ────────────────────────────────────────────

def harvest_skills(workspaces: List[str], out_dir: str) -> int:
    written = 0
    for ws in workspaces:
        skills_root = os.path.join(ws, ".windsurf", "skills")
        if not os.path.isdir(skills_root):
            continue
        for skill_dir in os.scandir(skills_root):
            if not skill_dir.is_dir():
                continue
            skill_md = os.path.join(skill_dir.path, "SKILL.md")
            if not os.path.isfile(skill_md):
                continue
            with open(skill_md, encoding="utf-8") as f:
                raw = f.read()
            body = re.sub(r"^---.*?---\s*", "", raw, flags=re.DOTALL).strip()
            if not body:
                continue
            first_line = body.split("\n")[0].lstrip("# ").strip()
            user_ask = f"Please use the {skill_dir.name} skill: {first_line}"
            ts = datetime.now(tz=timezone.utc).timestamp() * 1000 - 3_600_000
            _write_session(out_dir, ws, f"skill_{skill_dir.name}",
                           user_prompts=[user_ask],
                           assistant_replies=[body[:1200]],
                           timestamp_base_ms=ts)
            _append_history(out_dir, display=user_ask[:120], project=ws, timestamp_ms=ts)
            written += 1
    return written

# ── source 3: extension host logs ────────────────────────────────────────────

def harvest_logs(windsurf_logs_root: str, out_dir: str, workspaces: List[str],
                 max_sessions: int = 20) -> int:
    if not os.path.isdir(windsurf_logs_root):
        return 0
    log_dirs = sorted(
        [d for d in os.scandir(windsurf_logs_root) if d.is_dir()],
        key=lambda d: d.name, reverse=True,
    )
    written = 0
    seen: set = set()
    for log_day in log_dirs:
        if written >= max_sessions:
            break
        for win_dir in os.scandir(log_day.path):
            if not win_dir.is_dir() or not win_dir.name.startswith("window"):
                continue
            cascade_log = os.path.join(
                win_dir.path, "exthost", "codeium.windsurf", "Windsurf.log"
            )
            if not os.path.isfile(cascade_log):
                continue
            with open(cascade_log, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            user_lines: List[str] = []
            for line in lines:
                m = re.search(r'user.*?:\s*"?(.{20,200})"?', line, re.IGNORECASE)
                if m:
                    snippet = m.group(1).strip().rstrip('"').strip()
                    if len(snippet) > 20 and snippet not in user_lines:
                        user_lines.append(snippet)
            if not user_lines:
                continue
            sid = f"log_{log_day.name}_{win_dir.name}"
            if sid in seen:
                continue
            seen.add(sid)
            project = _infer_project(" ".join(user_lines[:3]), workspaces)
            ts = datetime.now(tz=timezone.utc).timestamp() * 1000 - 7_200_000
            _write_session(out_dir, project, sid,
                           user_prompts=user_lines[:5],
                           assistant_replies=(
                               ["[log-extracted — no assistant reply recorded]"]
                               * min(len(user_lines), 5)
                           ),
                           timestamp_base_ms=ts)
            written += 1
    return written

# ── main ─────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate SkillOpt-Sleep transcripts from Windsurf local data"
    )
    parser.add_argument(
        "--out-dir",
        default=os.path.expanduser("~/.skillopt-sleep-windsurf"),
        help="Output claude_home dir (default: ~/.skillopt-sleep-windsurf)",
    )
    parser.add_argument(
        "--agentmemory",
        default=os.path.expanduser("~/.agentmemory/standalone.json"),
        help="Path to agentmemory standalone.json",
    )
    parser.add_argument(
        "--windsurf-logs",
        default=os.path.expanduser("~/.config/Windsurf/logs"),
        help="Windsurf logs root directory",
    )
    parser.add_argument(
        "--workspaces", nargs="*",
        help="Workspace paths (default: auto-detect from Windsurf registry)",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    out_dir = os.path.expanduser(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(os.path.join(out_dir, "projects"), exist_ok=True)

    workspaces = args.workspaces or _detect_workspaces()
    workspaces = [ws for ws in workspaces if os.path.isdir(ws)]
    if not workspaces:
        workspaces = [os.getcwd()]

    total = 0
    n = harvest_agentmemory(args.agentmemory, out_dir, workspaces)
    if not args.quiet:
        print(f"[harvest_windsurf] agentmemory  : {n} sessions")
    total += n

    n = harvest_skills(workspaces, out_dir)
    if not args.quiet:
        print(f"[harvest_windsurf] skill files  : {n} sessions")
    total += n

    n = harvest_logs(args.windsurf_logs, out_dir, workspaces)
    if not args.quiet:
        print(f"[harvest_windsurf] logs         : {n} sessions")
    total += n

    if not args.quiet:
        print(f"[harvest_windsurf] total        : {total} synthetic sessions → {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
