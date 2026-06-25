#!/usr/bin/env python3
"""memento_memory — memento's own memory engine (stdlib-only, no external deps).

A self-contained, agentmemory-inspired memory store **owned by the memento
project**: SQLite-backed, full-text (BM25) search, memory tiers, secret
redaction, a local web dashboard, and an agentmemory-compatible JSON export so
the sleep-cycle harvester keeps working unchanged.

This is Phase 1 — the foundation. Richer agentmemory-style capabilities
(vector/semantic search + RRF fusion, knowledge-graph traversal, 4-tier
auto-consolidation + decay, capture hooks, governance/snapshots) layer on top
of this schema in later phases.

Everything here is standard library only: sqlite3 + http.server.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ── configuration ─────────────────────────────────────────────────────────────

TIERS = ("working", "episodic", "semantic", "procedural")
DEFAULT_TIER = "episodic"

DEFAULT_DB = os.environ.get(
    "MEMENTO_MEMORY_DB",
    os.path.expanduser("~/.memento/memory.db"),
)
# agentmemory-compatible export the sleep-cycle harvester already reads
DEFAULT_EXPORT = os.environ.get(
    "MEMENTO_MEMORY_PATH",
    os.path.expanduser("~/.agentmemory/standalone.json"),
)
DEFAULT_PORT = int(os.environ.get("MEMENTO_DASHBOARD_PORT", "3114"))

# ── secret redaction (privacy filtering before storage) ───────────────────────

_SECRET_PATTERNS = [
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9]{16,}\b"),          # openai-style keys
    re.compile(r"\bpypi-[A-Za-z0-9_\-]{16,}\b"),               # pypi tokens
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),                   # github tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                       # aws access key id
    re.compile(r"(?i)\b(?:secret|token|password|api[_-]?key)\s*[=:]\s*\S+"),
]


def redact_secrets(text: str) -> str:
    """Strip obvious secrets/keys from text before it is persisted."""
    out = text or ""
    for pat in _SECRET_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


def _fts_query(raw: str) -> str:
    """Turn free text into a safe FTS5 AND-of-terms query."""
    terms = [t for t in re.split(r"\W+", raw or "") if t]
    return " ".join('"%s"' % t for t in terms)


# ── the store ─────────────────────────────────────────────────────────────────

class MemoryStore:
    def __init__(self, db_path: str = None, export_path: str = None):
        self.db_path = db_path or DEFAULT_DB
        self.export_path = export_path or DEFAULT_EXPORT
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self.fts = True
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS memories(
                    id           TEXT PRIMARY KEY,
                    tier         TEXT NOT NULL DEFAULT 'episodic',
                    title        TEXT NOT NULL,
                    content      TEXT NOT NULL,
                    tags         TEXT NOT NULL DEFAULT '',
                    session      TEXT NOT NULL DEFAULT '',
                    source       TEXT NOT NULL DEFAULT 'manual',
                    created_ts   REAL NOT NULL,
                    accessed_ts  REAL NOT NULL,
                    access_count INTEGER NOT NULL DEFAULT 0,
                    pinned       INTEGER NOT NULL DEFAULT 0
                )
            """)
            try:
                c.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                    USING fts5(id UNINDEXED, title, content, tags)
                """)
            except sqlite3.OperationalError:
                self.fts = False  # SQLite built without FTS5 → fall back to LIKE

    # ── writes ────────────────────────────────────────────────────────────────

    @staticmethod
    def _norm_tags(tags) -> str:
        if isinstance(tags, (list, tuple)):
            return ",".join(t.strip() for t in tags if str(t).strip())
        return str(tags or "").strip()

    def save(self, title, content, tier=None, tags=None, session="",
             source="manual") -> str:
        title = redact_secrets((title or "").strip())
        content = redact_secrets((content or "").strip())
        if not title or not content:
            raise ValueError("both 'title' and 'content' are required")
        tier = tier if tier in TIERS else DEFAULT_TIER
        tags = self._norm_tags(tags)
        mem_id = "mem-" + hashlib.sha1(
            (title + "\x00" + content).encode("utf-8")).hexdigest()[:12]
        now = time.time()
        with self._connect() as c:
            c.execute("""
                INSERT INTO memories(id,tier,title,content,tags,session,source,
                                     created_ts,accessed_ts,access_count,pinned)
                VALUES(?,?,?,?,?,?,?,?,?,0,0)
                ON CONFLICT(id) DO UPDATE SET
                    tier=excluded.tier, tags=excluded.tags,
                    session=excluded.session, accessed_ts=excluded.accessed_ts
            """, (mem_id, tier, title, content, tags, session, source, now, now))
            if self.fts:
                c.execute("DELETE FROM memories_fts WHERE id=?", (mem_id,))
                c.execute(
                    "INSERT INTO memories_fts(id,title,content,tags) VALUES(?,?,?,?)",
                    (mem_id, title, content, tags))
        self._export()
        return mem_id

    def forget(self, mem_id=None, query=None) -> int:
        with self._connect() as c:
            if mem_id:
                ids = [r["id"] for r in c.execute(
                    "SELECT id FROM memories WHERE id=?", (mem_id,))]
            elif query:
                ids = [m["id"] for m in self.search(query, limit=1000)]
            else:
                return 0
            for i in ids:
                c.execute("DELETE FROM memories WHERE id=?", (i,))
                if self.fts:
                    c.execute("DELETE FROM memories_fts WHERE id=?", (i,))
        self._export()
        return len(ids)

    # ── reads ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _row(r) -> dict:
        return {k: r[k] for k in r.keys()}

    def get(self, mem_id) -> dict:
        with self._connect() as c:
            r = c.execute("SELECT * FROM memories WHERE id=?", (mem_id,)).fetchone()
            return self._row(r) if r else None

    def list(self, limit=20, tier=None, session=None) -> list:
        sql = "SELECT * FROM memories"
        clauses, params = [], []
        if tier in TIERS:
            clauses.append("tier=?"); params.append(tier)
        if session:
            clauses.append("session=?"); params.append(session)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_ts DESC LIMIT ?"; params.append(int(limit))
        with self._connect() as c:
            return [self._row(r) for r in c.execute(sql, params)]

    def search(self, query, limit=10, tier=None) -> list:
        q = (query or "").strip()
        if not q:
            return self.list(limit=limit, tier=tier)
        tier_clause = " AND m.tier=?" if tier in TIERS else ""
        with self._connect() as c:
            if self.fts:
                match = _fts_query(q)
                if match:
                    sql = ("SELECT m.* FROM memories_fts f JOIN memories m ON m.id=f.id "
                           "WHERE memories_fts MATCH ?" + tier_clause +
                           " ORDER BY bm25(memories_fts) LIMIT ?")
                    params = [match] + ([tier] if tier in TIERS else []) + [int(limit)]
                    rows = [self._row(r) for r in c.execute(sql, params)]
                    self._touch(c, [r["id"] for r in rows])
                    return rows
            like = f"%{q}%"
            sql = ("SELECT * FROM memories WHERE (title LIKE ? OR content LIKE ? "
                   "OR tags LIKE ?)" + tier_clause + " ORDER BY created_ts DESC LIMIT ?")
            params = [like, like, like] + ([tier] if tier in TIERS else []) + [int(limit)]
            rows = [self._row(r) for r in c.execute(sql, params)]
            self._touch(c, [r["id"] for r in rows])
            return rows

    @staticmethod
    def _touch(c, ids):
        now = time.time()
        for i in ids:
            c.execute("UPDATE memories SET accessed_ts=?, access_count=access_count+1 "
                      "WHERE id=?", (now, i))

    def sessions(self) -> list:
        with self._connect() as c:
            return [self._row(r) for r in c.execute(
                "SELECT session, COUNT(*) AS n, MAX(created_ts) AS last "
                "FROM memories WHERE session<>'' GROUP BY session ORDER BY last DESC")]

    def stats(self) -> dict:
        with self._connect() as c:
            total = c.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            by_tier = {r["tier"]: r["n"] for r in c.execute(
                "SELECT tier, COUNT(*) AS n FROM memories GROUP BY tier")}
        return {"total": total, "by_tier": by_tier, "fts": self.fts,
                "db": self.db_path}

    # ── agentmemory-compatible export ──────────────────────────────────────────

    def _export(self):
        """Mirror the store to the agentmemory standalone.json the harvester reads."""
        try:
            os.makedirs(os.path.dirname(self.export_path) or ".", exist_ok=True)
            mems = {}
            with self._connect() as c:
                for r in c.execute(
                        "SELECT id,title,content FROM memories ORDER BY created_ts"):
                    mems[r["id"]] = {"title": r["title"], "content": r["content"]}
            tmp = self.export_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"mem:memories": mems}, f, indent=2)
            os.replace(tmp, self.export_path)
        except OSError:
            pass


# ── local web dashboard (stdlib http.server, vanilla JS) ──────────────────────

_PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>memento · memory</title><style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;
 background:linear-gradient(160deg,#0b1022,#2e1a5e 60%,#5b21b6);color:#e9d5ff;min-height:100vh}
header{display:flex;align-items:center;gap:12px;padding:18px 24px;border-bottom:1px solid #ffffff22}
header h1{font-size:20px;margin:0;color:#f8fafc;letter-spacing:1px}
.badge{font-size:12px;color:#67e8f9;border:1px solid #67e8f955;border-radius:99px;padding:2px 10px}
main{max-width:880px;margin:0 auto;padding:24px}
input,textarea,select,button{font:inherit;border-radius:10px;border:1px solid #ffffff33;
 background:#ffffff10;color:#f8fafc;padding:10px 12px}
input,textarea{width:100%}
.row{display:flex;gap:10px;margin:8px 0}.row>*{flex:1}
button{cursor:pointer;background:#7c3aed;border-color:#7c3aed;font-weight:600}
button.ghost{background:#ffffff10;border-color:#ffffff33}
.card{background:#ffffff0e;border:1px solid #ffffff1f;border-radius:14px;padding:14px 16px;margin:12px 0}
.card h3{margin:0 0 4px;color:#f8fafc;font-size:16px}
.meta{font-size:12px;color:#c4b5fd99;margin-top:8px;display:flex;gap:10px;flex-wrap:wrap}
.tier{text-transform:uppercase;letter-spacing:.5px;color:#67e8f9}
.x{float:right;color:#fca5a5;cursor:pointer;font-size:13px}
</style></head><body>
<header><h1>🌙 memento memory</h1><span class=badge id=stat>…</span></header>
<main>
 <div class=card>
  <input id=q placeholder="Search memories (BM25)…" oninput="load()">
 </div>
 <details class=card><summary style=cursor:pointer>+ Add a memory</summary>
  <div class=row><input id=t placeholder="Title"></div>
  <textarea id=c rows=3 placeholder="Content"></textarea>
  <div class=row>
   <select id=tier><option>episodic<option>working<option>semantic<option>procedural</select>
   <input id=tags placeholder="tags (comma separated)">
   <button onclick=save()>Save</button>
  </div>
 </details>
 <div id=list></div>
</main>
<script>
async function load(){
 const q=document.getElementById('q').value;
 const r=await fetch('/api/memories?q='+encodeURIComponent(q));
 const d=await r.json();
 document.getElementById('list').innerHTML=d.memories.map(m=>`<div class=card>
   <span class=x onclick="forget('${m.id}')">forget ✕</span>
   <h3>${esc(m.title)}</h3><div>${esc(m.content)}</div>
   <div class=meta><span class=tier>${m.tier}</span>${m.tags?'<span>#'+esc(m.tags)+'</span>':''}
   ${m.session?'<span>'+esc(m.session)+'</span>':''}<span>${new Date(m.created_ts*1000).toLocaleString()}</span></div>
 </div>`).join('')||'<p style=opacity:.6>No memories yet.</p>';
 const s=await(await fetch('/api/stats')).json();
 document.getElementById('stat').textContent=s.total+' memories';
}
function esc(x){return (x||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
async function save(){
 const b={title:t.value,content:c.value,tier:tier.value,tags:tags.value};
 await fetch('/api/memories',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(b)});
 t.value=c.value=tags.value='';load();
}
async function forget(id){await fetch('/api/forget',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({id})});load();}
load();
</script></body></html>"""


def _make_handler(store: MemoryStore):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence default stderr logging
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _json_body(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return {}

        def do_GET(self):
            from urllib.parse import urlparse, parse_qs
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                return self._send(200, _PAGE, "text/html; charset=utf-8")
            if u.path == "/api/stats":
                return self._send(200, json.dumps(store.stats()))
            if u.path == "/api/memories":
                qs = parse_qs(u.query)
                q = (qs.get("q") or [""])[0]
                tier = (qs.get("tier") or [None])[0]
                rows = store.search(q, limit=200, tier=tier)
                return self._send(200, json.dumps({"memories": rows}))
            return self._send(404, json.dumps({"error": "not found"}))

        def do_POST(self):
            from urllib.parse import urlparse
            path = urlparse(self.path).path
            body = self._json_body()
            if path == "/api/memories":
                try:
                    mid = store.save(body.get("title"), body.get("content"),
                                     tier=body.get("tier"), tags=body.get("tags"),
                                     session=body.get("session", ""))
                    return self._send(200, json.dumps({"id": mid}))
                except ValueError as e:
                    return self._send(400, json.dumps({"error": str(e)}))
            if path == "/api/forget":
                n = store.forget(mem_id=body.get("id"), query=body.get("query"))
                return self._send(200, json.dumps({"forgotten": n}))
            return self._send(404, json.dumps({"error": "not found"}))

    return Handler


def make_server(store: MemoryStore, host="127.0.0.1", port=DEFAULT_PORT):
    return ThreadingHTTPServer((host, port), _make_handler(store))


_DASHBOARD = {"thread": None, "url": None}


def start_dashboard(store: MemoryStore, host="127.0.0.1", port=DEFAULT_PORT) -> str:
    """Start the dashboard once in a daemon thread; return its URL."""
    if _DASHBOARD["url"]:
        return _DASHBOARD["url"]
    srv = make_server(store, host, port)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    _DASHBOARD["thread"] = t
    _DASHBOARD["url"] = f"http://{host}:{srv.server_address[1]}"
    return _DASHBOARD["url"]


def serve_forever(host="127.0.0.1", port=DEFAULT_PORT):
    """Blocking dashboard server — used by `mcp_server.py --web`."""
    store = MemoryStore()
    srv = make_server(store, host, port)
    print(f"[memento] memory dashboard → http://{host}:{srv.server_address[1]}")
    srv.serve_forever()
