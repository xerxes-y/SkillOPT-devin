#!/usr/bin/env python3
"""Convert Windsurf/Cascade and Devin IDE local data into Claude Code-format JSONL transcripts.

Neither Windsurf (Codeium) nor Devin (Cognition) persist agent conversation
transcripts to disk in a format the sleep engine understands.  This script
bridges that gap by synthesising JSONL files from every locally available
source:

  1. **Devin transcripts** (~/.local/share/devin/cli/transcripts/*.json)
     Native ATIF-v1.7 format — source:"user" / source:"agent" messages
     converted directly to user/assistant JSONL turns.

  2. **agentmemory** (~/.agentmemory/standalone.json)
     Memories saved by the `agentmemory` MCP server — each memory's title
     becomes a synthetic user prompt; its content becomes the assistant reply.

  3. **Skill files** (.windsurf/skills/*/SKILL.md and .devin/skills/*/SKILL.md)
     Each skill description is converted to a session where the user asked
     "use the <skill> skill" and the assistant described how to apply it.

  4. **Windsurf extension logs** (~/.config/Windsurf/logs/)
     User-facing task snippets extracted from extension-host log lines
     (best-effort; empty when no useful content is present).

Output layout (mirrors ~/.claude/projects/<slug>/<sessionId>.jsonl):
    <out_dir>/projects/<slug>/<session_id>.jsonl

Workspace auto-detection order:
  1. ``SKILLOPT_WINDSURF_WORKSPACES`` env var — colon-separated abs paths
  2. Windsurf registry: ``~/.config/Windsurf/User/workspaceStorage/*/workspace.json``
  3. Devin registry:   ``~/.config/Devin/User/workspaceStorage/*/workspace.json``
  4. Working directory fallback

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

def _workspaces_from_registry(storage_root: str) -> List[tuple]:
    """Read VS Code-style workspaceStorage to get (mtime, path) pairs."""
    results: List[tuple] = []
    if not os.path.isdir(storage_root):
        return results
    for entry in os.scandir(storage_root):
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
                results.append((os.path.getmtime(ws_json), folder))
        except Exception:
            continue
    return results


def _detect_workspaces() -> List[str]:
    """Return known workspace paths (Windsurf + Devin registries), newest first."""
    env_val = os.environ.get("SKILLOPT_WINDSURF_WORKSPACES", "")
    if env_val:
        return [p for p in env_val.split(":") if p and os.path.isdir(p)]

    seen: set = set()
    results: List[tuple] = []
    for registry in (
        os.path.expanduser("~/.config/Windsurf/User/workspaceStorage"),
        os.path.expanduser("~/.config/Devin/User/workspaceStorage"),
    ):
        for mtime, folder in _workspaces_from_registry(registry):
            if folder not in seen:
                seen.add(folder)
                results.append((mtime, folder))
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

# ── source 1: Devin ATIF-v1.7 transcripts ────────────────────────────────────

def harvest_devin_transcripts(
    transcripts_dir: str, out_dir: str, workspaces: List[str]
) -> int:
    """Convert Devin CLI ATIF-v1.7 transcripts to Claude Code JSONL."""
    if not os.path.isdir(transcripts_dir):
        return 0
    written = 0
    for entry in os.scandir(transcripts_dir):
        if not entry.name.endswith(".json"):
            continue
        try:
            with open(entry.path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if data.get("schema_version", "").startswith("ATIF"):
            pass  # Devin native format
        else:
            continue
        session_id = data.get("session_id") or entry.name[:-5]
        steps = data.get("steps") or []
        user_prompts: List[str] = []
        agent_replies: List[str] = []
        project = ""
        ts_base: Optional[float] = None
        for step in steps:
            src = step.get("source", "")
            msg = str(step.get("message") or "").strip()
            if not msg or src == "system":
                continue
            if src == "user":
                user_prompts.append(msg)
                if not project:
                    project = _infer_project(msg, workspaces)
            elif src == "agent":
                agent_replies.append(msg)
            if ts_base is None:
                raw_ts = step.get("timestamp", "")
                if raw_ts:
                    try:
                        from datetime import datetime as _dt
                        ts_base = _dt.fromisoformat(
                            raw_ts.replace("Z", "+00:00")
                        ).timestamp() * 1000
                    except Exception:
                        pass
        if not user_prompts:
            continue
        if not project:
            project = workspaces[0] if workspaces else os.getcwd()
        if ts_base is None:
            ts_base = datetime.now(tz=timezone.utc).timestamp() * 1000
        # Pair turns; pad shorter list
        n = max(len(user_prompts), len(agent_replies))
        user_prompts += [""] * (n - len(user_prompts))
        agent_replies += [""] * (n - len(agent_replies))
        _write_session(
            out_dir, project, f"devin_{session_id}",
            user_prompts=[p for p in user_prompts if p],
            assistant_replies=[r if r else "[no reply recorded]" for r, p in
                               zip(agent_replies, user_prompts) if p],
            timestamp_base_ms=ts_base,
        )
        _append_history(
            out_dir,
            display=(user_prompts[0] or session_id)[:120],
            project=project,
            timestamp_ms=ts_base,
        )
        written += 1
    return written


# ── source 2: agentmemory ─────────────────────────────────────────────────────

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

# ── source 3: skill files (.windsurf/skills and .devin/skills) ───────────────

def harvest_skills(workspaces: List[str], out_dir: str) -> int:
    written = 0
    seen_ids: set = set()
    for ws in workspaces:
        for dot_dir in (".windsurf", ".devin"):
            skills_root = os.path.join(ws, dot_dir, "skills")
            if not os.path.isdir(skills_root):
                continue
            for skill_dir in os.scandir(skills_root):
                if not skill_dir.is_dir():
                    continue
                skill_md = os.path.join(skill_dir.path, "SKILL.md")
                if not os.path.isfile(skill_md):
                    continue
                sid = f"skill_{skill_dir.name}"
                if sid in seen_ids:
                    continue
                seen_ids.add(sid)
                with open(skill_md, encoding="utf-8") as f:
                    raw = f.read()
                body = re.sub(r"^---.*?---\s*", "", raw, flags=re.DOTALL).strip()
                if not body:
                    continue
                first_line = body.split("\n")[0].lstrip("# ").strip()
                user_ask = f"Please use the {skill_dir.name} skill: {first_line}"
                ts = datetime.now(tz=timezone.utc).timestamp() * 1000 - 3_600_000
                _write_session(out_dir, ws, sid,
                               user_prompts=[user_ask],
                               assistant_replies=[body[:1200]],
                               timestamp_base_ms=ts)
                _append_history(out_dir, display=user_ask[:120], project=ws, timestamp_ms=ts)
                written += 1
    return written

# ── source 4: Windsurf extension host logs ───────────────────────────────────

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
    devin_transcripts = os.path.expanduser("~/.local/share/devin/cli/transcripts")
    n = harvest_devin_transcripts(devin_transcripts, out_dir, workspaces)
    if not args.quiet:
        print(f"[harvest_windsurf] devin        : {n} sessions")
    total += n

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
