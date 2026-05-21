#!/usr/bin/env python3
"""Claude session manager — FastAPI + ttyd + tmux + SQLite."""
import asyncio
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx
import websockets
from fastapi import FastAPI, Form, Request, WebSocket
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

@asynccontextmanager
async def lifespan(app: FastAPI):
    PROJECT_BASE.mkdir(parents=True, exist_ok=True)
    _db_init()
    for name, info in _tmux_status().items():
        _db_add(name, name, str(PROJECT_BASE / name), info["created"])
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

TMUX          = "/usr/bin/tmux"
TTYD          = "/usr/bin/ttyd"
CLAUDE        = "/home/agent/.local/bin/claude"
# Project dirs live inside the sandbox's workspace mount so generated files are
# host-visible. SANDBOX_DIR is validated by _require_sandbox() before use.
PROJECT_BASE  = Path(os.environ.get("SANDBOX_DIR", "/var/empty")) / "projects"
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
                project            TEXT NOT NULL,
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


def _next_session_name(project: str) -> str:
    """Return a unique session name for the given project: 'foo', 'foo-2', 'foo-3', ..."""
    with _db() as conn:
        existing = {r["name"] for r in conn.execute(
            "SELECT name FROM sessions WHERE project=?", (project,)
        ).fetchall()}
    if project not in existing:
        return project
    n = 2
    while f"{project}-{n}" in existing:
        n += 1
    return f"{project}-{n}"


def _db_add(name: str, project: str, project_dir: str, created_at: int = 0) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sessions (name, project, project_dir, created_at) VALUES (?,?,?,?)",
            (name, project, project_dir, created_at or int(time.time())),
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

def _project_dir(project: str) -> Path:
    d = PROJECT_BASE / project
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


async def _new_tmux_session(name: str, project: str) -> None:
    project_dir = str(_project_dir(project))
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

def _scrape_session_data(project_key: str | None, session_id: str | None) -> dict:
    """Return title + usage stats scraped from the session's claude jsonl file."""
    if not project_key or not session_id:
        return {}
    jsonl = CLAUDE_PROJECTS / project_key / f"{session_id}.jsonl"
    if not jsonl.exists():
        return {}

    first_user_msg = None
    custom_title = None
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
                t = obj.get("type")
                # /rename writes a `custom-title` entry; last one wins.
                if t == "custom-title":
                    ct = obj.get("customTitle")
                    if isinstance(ct, str) and ct.strip():
                        custom_title = ct.strip()
                # First real user message → fallback session title. Skip Claude Code's
                # auto-generated entries: <bash-input>/<bash-stdout>/<bash-stderr>,
                # <local-command-caveat>, <command-name>, etc. — they're regular user
                # messages without isMeta:true, but their content starts with `<tag>`.
                if first_user_msg is None and t == "user" and not obj.get("isMeta"):
                    content = obj.get("message", {}).get("content", "")
                    if isinstance(content, str):
                        stripped = content.strip()
                        if stripped and not stripped.startswith("<"):
                            first_user_msg = stripped
                if t != "assistant":
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

    result: dict = {}
    title = custom_title or first_user_msg
    if title:
        # Compact for display: collapse whitespace, truncate
        flat = " ".join(title.split())
        result["title"] = flat[:80] + ("…" if len(flat) > 80 else "")
    if total_in or total_out:
        max_ctx = CONTEXT_MAX.get(latest_model, 200_000)
        ctx_pct = round(latest_ctx / max_ctx * 100, 1) if latest_ctx else 0
        result["usage"] = {
            "in":         _fmt(total_in),
            "out":        _fmt(total_out),
            "cache_read": _fmt(cache_read),
            "ctx":        _fmt(latest_ctx),
            "max_ctx":    _fmt(max_ctx),
            "ctx_pct":    ctx_pct,
        }
    return result


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


# ── View context ──────────────────────────────────────────────────────────────

def _cmd_class(command: str) -> str:
    if command == "claude":           return "cmd-claude"
    if command in ("bash","sh","zsh","fish"): return "cmd-shell"
    return "cmd-other"


def _build_session_ctx(s, tmux: dict[str, dict]) -> dict:
    """Build the template context dict for a single session card."""
    name = s["name"]
    live = name in tmux
    tm   = tmux.get(name, {})
    conn = _registry.get(name, {}).get("connections", 0)

    data = _scrape_session_data(s["claude_project_key"], s["claude_session_id"])
    usage = None
    u = data.get("usage")
    if u:
        ctx_pct = u["ctx_pct"]
        fill_cls = ("ctx-fill danger" if ctx_pct >= 80
                    else "ctx-fill warn" if ctx_pct >= 60
                    else "ctx-fill")
        usage = {
            **u,
            "fill_cls":  fill_cls,
            "ctx_width": min(ctx_pct, 100),
        }

    return {
        "name":         name,
        "live":         live,
        "card_cls":     "card" if live else "card stopped",
        "dot_cls":      ("dot" if conn > 0 else "dot idle") if live else "dot stopped",
        "cmd":          tm.get("command", "—"),
        "cmd_cls":      _cmd_class(tm.get("command", "—")),
        "views_cls":    "views-live" if conn > 0 else "",
        "views_label":  f"{conn} active" if conn > 0 else "none",
        "created_rel":  _rel(s["created_at"]),
        "activity_rel": tm.get("activity_rel", "—"),
        "title":        data.get("title"),
        "usage":        usage,
    }


def _build_projects_ctx(db_sessions: list, tmux: dict[str, dict]) -> list[dict]:
    """Group sessions by project (preserving creation order) and build template context."""
    by_project: dict[str, list] = {}
    for s in db_sessions:
        by_project.setdefault(s["project"], []).append(s)

    projects: list[dict] = []
    for project, sessions in by_project.items():
        count = len(sessions)
        projects.append({
            "project":     project,
            "project_dir": sessions[0]["project_dir"],
            "count":       count,
            "suffix":      "s" if count != 1 else "",
            "sessions":    [_build_session_ctx(s, tmux) for s in sessions],
        })
    return projects


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def landing(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {
        "projects":     _build_projects_ctx(_db_all(), _tmux_status()),
        "project_base": str(PROJECT_BASE),
    })


@app.get("/partial/groups")
async def partial_groups(request: Request):
    """Just the project groups HTML, for live in-page refresh from JS."""
    return templates.TemplateResponse(request, "groups.html", {
        "projects":     _build_projects_ctx(_db_all(), _tmux_status()),
        "project_base": str(PROJECT_BASE),
    })


@app.post("/sessions")
async def create_session(name: str = Form(...)):
    # The form field is the *project* name; the session name may be auto-suffixed
    # (e.g. "frontend-2") when multiple sessions share the same project dir.
    project = name
    session_name = _next_session_name(project)
    project_dir = str(_project_dir(project))
    created_at = int(time.time())
    _db_add(session_name, project, project_dir, created_at)

    # Snapshot existing jsonl files so detection only picks up new ones
    snapshot: set[Path] = set()
    if CLAUDE_PROJECTS.exists():
        for d in CLAUDE_PROJECTS.iterdir():
            if d.is_dir():
                snapshot.update(d.glob("*.jsonl"))

    if session_name not in _tmux_status():
        await _new_tmux_session(session_name, project)
    await _ensure_ttyd(session_name)

    asyncio.create_task(_detect_claude_session(session_name, created_at, snapshot))
    return RedirectResponse(f"/sessions/{session_name}/", status_code=303)


@app.post("/sessions/{name}/kill")
async def kill_session(name: str):
    entry = _registry.pop(name, None)
    if entry:
        entry["proc"].terminate()
    subprocess.run([TMUX, "kill-session", "-t", name], capture_output=True)
    _db_remove(name)
    return RedirectResponse("/", status_code=303)


@app.post("/projects/{project}/destroy")
async def destroy_project(project: str, confirm: str = Form("")):
    # Belt-and-suspenders: refuse without the typed confirmation, even though the
    # UI gates the button. Keeps curl/scripts from accidentally nuking a project.
    if confirm != "DELETE":
        return Response("missing or wrong confirmation token", status_code=400)
    project_dir = None
    sessions = [r for r in _db_all() if r["project"] == project]
    for s in sessions:
        n = s["name"]
        project_dir = s["project_dir"]
        entry = _registry.pop(n, None)
        if entry:
            entry["proc"].terminate()
        subprocess.run([TMUX, "kill-session", "-t", n], capture_output=True)
        _db_remove(n)
    if project_dir:
        import shutil
        shutil.rmtree(project_dir, ignore_errors=True)
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


def _require_sandbox() -> None:
    """Refuse to run outside an sbx microvm — Claude is launched with
    --dangerously-skip-permissions, which is only safe inside the sandbox."""
    if not os.environ.get("SANDBOX_VM_ID"):
        sys.exit(
            "refusing to start: $SANDBOX_VM_ID is not set, so this process is not "
            "running inside an sbx microvm. claude-sessions launches Claude with "
            "--dangerously-skip-permissions on behalf of anyone who can reach the "
            "dashboard, which is only safe inside the sandbox isolation boundary."
        )
    if not os.environ.get("SANDBOX_DIR"):
        sys.exit(
            "refusing to start: $SANDBOX_DIR is not set. The justfile sets this from "
            ".env and propagates it into the sandbox; this process is missing it, "
            "which suggests it was launched outside the normal `just up` flow."
        )


if __name__ == "__main__":
    _require_sandbox()
    port = int(os.environ.get("PORT", "3000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
