#!/usr/bin/env python3
"""Claude session manager — FastAPI + ttyd + tmux + SQLite."""
import asyncio
import json
import socket
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path

import httpx
import websockets
from fastapi import FastAPI, Form, Request, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse, Response
import uvicorn

app = FastAPI()

TMUX          = "/usr/bin/tmux"
TTYD          = "/usr/bin/ttyd"
CLAUDE        = "/home/agent/.local/bin/claude"
PROJECT_BASE  = Path("/home/agent/projects")
CLAUDE_PROJECTS = Path("/home/agent/.claude/projects")
DB_PATH       = Path("/home/agent/.session-manager.db")

CONTEXT_MAX = {
    "claude-opus-4-7":   1_000_000,
    "claude-sonnet-4-6":   200_000,
    "claude-haiku-4-5":    200_000,
}

# In-memory only: ttyd process/port/open connection count
_registry: dict[str, dict] = {}


# ── Database ──────────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _db_init() -> None:
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                name               TEXT PRIMARY KEY,
                project_dir        TEXT NOT NULL,
                created_at         INTEGER NOT NULL,
                claude_project_key TEXT,
                claude_session_id  TEXT
            )
        """)


def _db_all() -> list[sqlite3.Row]:
    with _db() as conn:
        return conn.execute("SELECT * FROM sessions ORDER BY created_at").fetchall()


def _db_get(name: str) -> sqlite3.Row | None:
    with _db() as conn:
        return conn.execute("SELECT * FROM sessions WHERE name=?", (name,)).fetchone()


def _db_add(name: str, project_dir: str, created_at: int = 0) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sessions (name, project_dir, created_at) VALUES (?,?,?)",
            (name, project_dir, created_at or int(time.time())),
        )


def _db_set_claude(name: str, project_key: str, session_id: str) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE sessions SET claude_project_key=?, claude_session_id=? WHERE name=?",
            (project_key, session_id, name),
        )


def _db_remove(name: str) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM sessions WHERE name=?", (name,))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _project_dir(name: str) -> Path:
    d = PROJECT_BASE / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _rel(epoch: int) -> str:
    delta = int(time.time()) - epoch
    if delta < 60:    return f"{delta}s ago"
    if delta < 3600:  return f"{delta // 60}m ago"
    if delta < 86400: return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _fmt(n: int) -> str:
    if n < 1_000:     return str(n)
    if n < 1_000_000: return f"{n / 1_000:.1f}k"
    return f"{n / 1_000_000:.1f}M"


def _iso_epoch(ts: str) -> int:
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


# ── tmux ──────────────────────────────────────────────────────────────────────

def _tmux_status() -> dict[str, dict]:
    """Return {name: {created, activity_rel, command}} for all live tmux sessions."""
    r = subprocess.run(
        [TMUX, "list-sessions", "-F",
         "#{session_name}\t#{session_created}\t#{session_activity}\t#{pane_current_command}"],
        capture_output=True, text=True,
    )
    out = {}
    for line in r.stdout.strip().splitlines():
        parts = line.split("\t", 3)
        if len(parts) == 4:
            name, created_s, activity_s, command = parts
            try:
                created  = int(created_s)
                activity = int(activity_s)
            except ValueError:
                created = activity = 0
            out[name] = {"created": created, "activity_rel": _rel(activity), "command": command}
    return out


async def _new_tmux_session(name: str) -> None:
    project_dir = str(_project_dir(name))
    subprocess.run([TMUX, "new-session", "-d", "-s", name, "-c", project_dir], check=True)
    # cd explicitly: tmux -c silently falls back to $HOME if the dir doesn't exist
    subprocess.run([TMUX, "send-keys", "-t", name,
                    f"cd {project_dir} && {CLAUDE} --dangerously-skip-permissions", "Enter"])


# ── Claude session ID detection ───────────────────────────────────────────────

def _first_epoch_in_jsonl(path: Path) -> int:
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                ts = _iso_epoch(obj.get("timestamp", ""))
                if ts:
                    return ts
    except Exception:
        pass
    return 0


async def _detect_claude_session(name: str, created_at: int, snapshot: set[Path]) -> None:
    """Background task: watch for a new .claude jsonl file and record its session ID."""
    deadline = time.monotonic() + 30
    await asyncio.sleep(2)
    while time.monotonic() < deadline:
        if not CLAUDE_PROJECTS.exists():
            await asyncio.sleep(1)
            continue
        for proj_dir in CLAUDE_PROJECTS.iterdir():
            if not proj_dir.is_dir():
                continue
            for jsonl in proj_dir.glob("*.jsonl"):
                if jsonl in snapshot:
                    continue
                first = _first_epoch_in_jsonl(jsonl)
                if first and first >= created_at - 5:
                    _db_set_claude(name, proj_dir.name, jsonl.stem)
                    return
        await asyncio.sleep(1)


# ── Usage scraping ────────────────────────────────────────────────────────────

def _scrape_usage(project_key: str | None, session_id: str | None) -> dict:
    if not project_key or not session_id:
        return {}
    jsonl = CLAUDE_PROJECTS / project_key / f"{session_id}.jsonl"
    if not jsonl.exists():
        return {}

    total_in = total_out = cache_read = cache_write = 0
    latest_ctx = 0
    latest_model = None
    try:
        with open(jsonl) as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                usage = obj.get("message", {}).get("usage", {})
                if not usage:
                    continue
                inp = usage.get("input_tokens", 0)
                out = usage.get("output_tokens", 0)
                cr  = usage.get("cache_read_input_tokens", 0)
                cw  = usage.get("cache_creation_input_tokens", 0)
                total_in  += inp
                total_out += out
                cache_read  += cr
                cache_write += cw
                ctx = inp + cr + cw
                if ctx:
                    latest_ctx   = ctx
                    latest_model = obj.get("message", {}).get("model")
    except Exception:
        pass

    if not total_in and not total_out:
        return {}
    max_ctx = CONTEXT_MAX.get(latest_model, 200_000)
    ctx_pct = round(latest_ctx / max_ctx * 100, 1) if latest_ctx else 0
    return {
        "in":         _fmt(total_in),
        "out":        _fmt(total_out),
        "cache_read": _fmt(cache_read),
        "ctx":        _fmt(latest_ctx),
        "max_ctx":    _fmt(max_ctx),
        "ctx_pct":    ctx_pct,
    }


# ── ttyd proxy ────────────────────────────────────────────────────────────────

async def _ensure_ttyd(name: str) -> int:
    entry = _registry.get(name)
    if entry and entry["proc"].poll() is None:
        return entry["port"]
    port = _free_port()
    proc = subprocess.Popen(
        [TTYD, "-p", str(port), "-i", "127.0.0.1", "--writable",
         TMUX, "attach-session", "-t", name],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    prev = _registry.get(name, {})
    _registry[name] = {"port": port, "proc": proc, "connections": prev.get("connections", 0)}
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            async with httpx.AsyncClient() as c:
                await c.get(f"http://127.0.0.1:{port}/", timeout=0.3)
            break
        except Exception:
            await asyncio.sleep(0.1)
    return port


# ── HTML ──────────────────────────────────────────────────────────────────────

STYLE = """\
<style>
*, *::before, *::after { box-sizing: border-box; }
body { font-family: 'SF Mono','Fira Code',ui-monospace,monospace;
       background: #0d1117; color: #c9d1d9; margin: 0; padding: 2rem; }
header { display: flex; align-items: baseline; gap: 1rem; margin-bottom: 2rem; }
h1 { color: #58a6ff; font-size: 1.35rem; margin: 0; }
.hint { color: #484f58; font-size: 0.78rem; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
        gap: 0.75rem; margin-bottom: 2rem; }
.card { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
        padding: 1rem 1.1rem; display: flex; flex-direction: column; gap: 0.55rem; }
.card.stopped { opacity: 0.55; }
.card-top { display: flex; align-items: center; justify-content: space-between; }
.card-title { display: flex; align-items: center; gap: 0.5rem; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: #3fb950; flex-shrink: 0; }
.dot.idle    { background: #484f58; }
.dot.stopped { background: #da3633; }
.sname { font-weight: bold; color: #58a6ff; font-size: 1rem; }
.stopped .sname { color: #8b949e; }
.actions { display: flex; gap: 0.4rem; }
.card-dir { color: #484f58; font-size: 0.78rem; }
.meta { display: grid; grid-template-columns: 1fr 1fr; gap: 0.25rem 1.5rem;
        border-top: 1px solid #21262d; padding-top: 0.55rem; }
.mi { display: flex; gap: 0.4rem; align-items: baseline; font-size: 0.82rem; }
.ml { color: #484f58; min-width: 3.5rem; flex-shrink: 0; }
.mv { color: #8b949e; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mv.cmd-claude  { color: #3fb950; }
.mv.cmd-shell   { color: #8b949e; }
.mv.cmd-other   { color: #e3b341; }
.mv.views-live  { color: #58a6ff; }
.usage { border-top: 1px solid #21262d; padding-top: 0.55rem;
         display: flex; flex-direction: column; gap: 0.3rem; }
.usage-row { display: flex; gap: 1rem; font-size: 0.82rem; flex-wrap: wrap; }
.ustat { display: flex; gap: 0.35rem; }
.ul { color: #484f58; }
.uv { color: #8b949e; }
.uv.dim { color: #3d444d; }
.ctx-wrap { display: flex; align-items: center; gap: 0.5rem; font-size: 0.82rem; }
.ctx-bar { flex: 1; height: 4px; background: #21262d; border-radius: 2px; min-width: 60px; }
.ctx-fill { height: 100%; border-radius: 2px; background: #58a6ff; }
.ctx-fill.warn   { background: #e3b341; }
.ctx-fill.danger { background: #da3633; }
.ctx-label { color: #8b949e; white-space: nowrap; }
.no-usage { color: #2d333b; font-size: 0.78rem; font-style: italic; }
.btn { padding: 0.28rem 0.75rem; border: 1px solid; border-radius: 5px; cursor: pointer;
       font-family: inherit; font-size: 0.8rem; text-decoration: none;
       display: inline-block; white-space: nowrap; }
.open   { background: #0d4429; border-color: #3fb950; color: #3fb950; }
.open:hover { background: #144620; }
.kill   { background: #2d1117; border-color: #da3633; color: #da3633; }
.kill:hover { background: #3d1a17; }
.create { background: #0c2d6b; border-color: #58a6ff; color: #58a6ff; padding: 0.45rem 1.3rem; }
.create:hover { background: #0e3a87; }
.empty { color: #484f58; font-style: italic; margin: 0.5rem 0 2rem; }
form.new { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
           padding: 1.4rem; max-width: 400px; }
form.new h2 { margin: 0 0 1.1rem; font-size: 0.9rem; color: #8b949e; font-weight: normal; }
.field { margin-bottom: 0.9rem; }
label { display: block; font-size: 0.76rem; color: #8b949e; margin-bottom: 0.25rem; }
.field-hint { font-size: 0.72rem; color: #484f58; margin-top: 0.25rem; }
input[type=text] { width: 100%; background: #0d1117; border: 1px solid #30363d;
                   color: #c9d1d9; padding: 0.4rem 0.65rem; border-radius: 5px;
                   font-family: inherit; font-size: 0.88rem; }
input[type=text]:focus { outline: none; border-color: #58a6ff; }
</style>"""


def _cmd_class(command: str) -> str:
    if command == "claude":           return "cmd-claude"
    if command in ("bash","sh","zsh","fish"): return "cmd-shell"
    return "cmd-other"


def _render_landing(db_sessions: list, tmux: dict[str, dict]) -> str:
    cards = ""
    for s in db_sessions:
        name = s["name"]
        live = name in tmux
        tm   = tmux.get(name, {})
        conn = _registry.get(name, {}).get("connections", 0)

        dot_cls  = ("dot" if conn > 0 else "dot idle") if live else "dot stopped"
        card_cls = "card" if live else "card stopped"
        cmd      = tm.get("command", "—")
        cmd_cls  = _cmd_class(cmd)
        views_cls  = "views-live" if conn > 0 else ""
        views_label = f"{conn} active" if conn > 0 else "none"

        u = _scrape_usage(s["claude_project_key"], s["claude_session_id"])
        if u:
            ctx_pct   = u["ctx_pct"]
            fill_cls  = ("ctx-fill danger" if ctx_pct >= 80
                         else "ctx-fill warn" if ctx_pct >= 60
                         else "ctx-fill")
            usage_html = f"""
      <div class="usage">
        <div class="usage-row">
          <div class="ustat"><span class="ul">in</span><span class="uv">{u['in']}</span></div>
          <div class="ustat"><span class="ul">out</span><span class="uv">{u['out']}</span></div>
          <div class="ustat"><span class="ul">cache↑</span><span class="uv dim">{u['cache_read']}</span></div>
        </div>
        <div class="ctx-wrap">
          <span class="ul">ctx</span>
          <div class="ctx-bar"><div class="{fill_cls}" style="width:{min(ctx_pct,100)}%"></div></div>
          <span class="ctx-label">{u['ctx']} / {u['max_ctx']} ({ctx_pct}%)</span>
        </div>
      </div>"""
        else:
            usage_html = '<div class="usage"><span class="no-usage">no usage data yet</span></div>'

        open_btn = (f'<a class="btn open" href="/sessions/{name}/" target="_blank">Open ↗</a>'
                    if live else '<span style="color:#484f58;font-size:0.8rem">stopped</span>')

        cards += f"""
    <div class="{card_cls}">
      <div class="card-top">
        <div class="card-title">
          <div class="{dot_cls}"></div>
          <span class="sname">{name}</span>
        </div>
        <div class="actions">
          {open_btn}
          <form method="POST" action="/sessions/{name}/kill" style="margin:0">
            <button type="submit" class="btn kill">Kill</button>
          </form>
        </div>
      </div>
      <div class="card-dir">{s['project_dir']}</div>
      <div class="meta">
        <div class="mi"><span class="ml">cmd</span><span class="mv {cmd_cls}">{cmd}</span></div>
        <div class="mi"><span class="ml">created</span><span class="mv">{_rel(s['created_at'])}</span></div>
        <div class="mi"><span class="ml">views</span><span class="mv {views_cls}">{views_label}</span></div>
        <div class="mi"><span class="ml">activity</span><span class="mv">{tm.get('activity_rel','—')}</span></div>
      </div>{usage_html}
    </div>"""

    if not cards:
        cards = '<p class="empty">No sessions yet. Create one below.</p>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="15">
  <title>Claude Sessions</title>
  {STYLE}
</head>
<body>
<header>
  <h1>Claude Sessions</h1>
  <span class="hint">auto-refreshes every 15s</span>
</header>
<div class="grid">{cards}</div>
<form class="new" method="POST" action="/sessions">
  <h2>New session</h2>
  <div class="field">
    <label>Project name</label>
    <input type="text" name="name" placeholder="my-project" required autofocus />
    <div class="field-hint">Creates /home/agent/projects/{{name}}</div>
  </div>
  <button type="submit" class="btn create">Create &amp; Open</button>
</form>
</body>
</html>"""


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def landing():
    return _render_landing(_db_all(), _tmux_status())


@app.post("/sessions")
async def create_session(name: str = Form(...)):
    project_dir = str(_project_dir(name))
    created_at  = int(time.time())
    _db_add(name, project_dir)

    # Snapshot existing jsonl files so detection only picks up new ones
    snapshot: set[Path] = set()
    if CLAUDE_PROJECTS.exists():
        for d in CLAUDE_PROJECTS.iterdir():
            if d.is_dir():
                snapshot.update(d.glob("*.jsonl"))

    if name not in _tmux_status():
        await _new_tmux_session(name)
    await _ensure_ttyd(name)

    asyncio.create_task(_detect_claude_session(name, created_at, snapshot))
    return RedirectResponse(f"/sessions/{name}/", status_code=303)


@app.post("/sessions/{name}/kill")
async def kill_session(name: str):
    entry = _registry.pop(name, None)
    if entry:
        entry["proc"].terminate()
    subprocess.run([TMUX, "kill-session", "-t", name], capture_output=True)
    _db_remove(name)
    return RedirectResponse("/", status_code=303)


@app.websocket("/sessions/{name}/ws")
async def ws_proxy(websocket: WebSocket, name: str):
    port = await _ensure_ttyd(name)
    _registry.setdefault(name, {}).setdefault("connections", 0)
    _registry[name]["connections"] += 1
    await websocket.accept(subprotocol="tty")
    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{port}/ws", subprotocols=["tty"]
        ) as upstream:
            async def to_upstream():
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg["type"] == "websocket.disconnect":
                            break
                        if msg.get("bytes"):  await upstream.send(msg["bytes"])
                        elif msg.get("text"): await upstream.send(msg["text"])
                except Exception:
                    pass
                finally:
                    await upstream.close()

            async def to_client():
                try:
                    async for msg in upstream:
                        if isinstance(msg, bytes): await websocket.send_bytes(msg)
                        else:                      await websocket.send_text(msg)
                except Exception:
                    pass

            tasks = [asyncio.create_task(to_upstream()), asyncio.create_task(to_client())]
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
    except Exception:
        pass
    finally:
        if name in _registry:
            _registry[name]["connections"] = max(0, _registry[name].get("connections", 1) - 1)


@app.get("/sessions/{name}/{path:path}")
async def http_proxy(name: str, path: str, request: Request):
    if not _db_get(name):
        return Response("Session not found", status_code=404)
    port = await _ensure_ttyd(name)
    skip = {"transfer-encoding", "connection", "keep-alive", "content-encoding", "content-length"}
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"http://127.0.0.1:{port}/{path}", params=dict(request.query_params)
        )
        headers = {k: v for k, v in r.headers.items() if k.lower() not in skip}
        return Response(content=r.content, status_code=r.status_code, headers=headers)


# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    PROJECT_BASE.mkdir(parents=True, exist_ok=True)
    _db_init()
    tmux = _tmux_status()
    for name, info in tmux.items():
        _db_add(name, str(PROJECT_BASE / name), info["created"])


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=3000, log_level="warning")
