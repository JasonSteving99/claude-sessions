#!/usr/bin/env python3
"""Host-side port daemon for claude-sessions.

Runs on the host (not in the sandbox) and is the *only* component allowed to
invoke `sbx ports --publish/--unpublish`. The sandbox never contacts this
daemon; the user's browser is the trusted bridge. The dashboard (served by
the sandbox) renders JS that calls this daemon directly. The daemon binds
loopback only and uses CORS + a JSON content-type requirement so other
origins can't drive it via the user's browser.

State is persisted to $SANDBOX_DIR/.host-daemon-state.json so a hard crash
doesn't leave orphan host port publishes; on graceful shutdown all ports are
unpublished and state is cleared.
"""
import asyncio
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


SANDBOX_NAME = os.environ.get("SANDBOX_NAME", "")
SANDBOX_DIR  = os.environ.get("SANDBOX_DIR", "")
DASHBOARD_PORT = int(os.environ.get("PORT", "3000"))
HOST_PORT      = int(os.environ.get("HOST_PORT", "33001"))

STATE_PATH = Path(SANDBOX_DIR) / ".host-daemon-state.json" if SANDBOX_DIR else None

# Serialize sbx invocations: concurrent --publish/--unpublish on the same
# sandbox can race in sbx's own state machine.
_sbx_lock = asyncio.Lock()


# ── State ─────────────────────────────────────────────────────────────────────

class PortEntry(BaseModel):
    project:  str
    port:     int = Field(ge=1, le=65535)
    protocol: str = "tcp"

    def spec(self) -> str:
        """sbx port spec: HOST:SANDBOX/PROTOCOL — same port both sides."""
        return f"{self.port}:{self.port}/{self.protocol}"

    def key(self) -> tuple[int, str]:
        return (self.port, self.protocol)


def _load_state() -> list[PortEntry]:
    if not STATE_PATH or not STATE_PATH.exists():
        return []
    try:
        raw = json.loads(STATE_PATH.read_text())
        return [PortEntry(**e) for e in raw]
    except Exception as e:
        print(f"[host-daemon] failed to load state ({e}); starting empty", file=sys.stderr)
        return []


def _save_state(entries: list[PortEntry]) -> None:
    if not STATE_PATH:
        return
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps([e.model_dump() for e in entries], indent=2))
    tmp.replace(STATE_PATH)


# ── sbx wrapper ───────────────────────────────────────────────────────────────

async def _sbx(*args: str) -> tuple[int, str, str]:
    """Run an sbx command; return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "sbx", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode(), err.decode()


async def _publish(entry: PortEntry) -> None:
    async with _sbx_lock:
        rc, _, err = await _sbx("ports", SANDBOX_NAME, "--publish", entry.spec())
    if rc != 0:
        raise HTTPException(
            status_code=409,
            detail=f"sbx publish failed: {err.strip() or 'unknown error'}",
        )


async def _unpublish(entry: PortEntry) -> None:
    async with _sbx_lock:
        # --unpublish failures are best-effort: a port may already be gone
        # (sandbox restart, manual unpublish) and we still want to drop it
        # from our state.
        await _sbx("ports", SANDBOX_NAME, "--unpublish", entry.spec())


# ── Lifespan: reconcile on startup, clear on shutdown ────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not SANDBOX_NAME:
        sys.exit("refusing to start: SANDBOX_NAME is not set")
    if not SANDBOX_DIR:
        sys.exit("refusing to start: SANDBOX_DIR is not set")

    # Reconcile: try to publish every persisted entry. Failures are logged
    # but tolerated — the state file only exists because a previous run
    # crashed without cleanup, and `sbx` will reject an already-published
    # port with an error that we can safely ignore. Normal `just down` clears
    # the state file, so this loop is a no-op on the happy path.
    app.state.entries = _load_state()
    for e in list(app.state.entries):
        try:
            await _publish(e)
        except HTTPException as exc:
            print(f"[host-daemon] reconcile: {e.spec()}: {exc.detail}", file=sys.stderr)

    print(f"[host-daemon] ready on 127.0.0.1:{HOST_PORT} "
          f"(sandbox={SANDBOX_NAME}, dashboard_port={DASHBOARD_PORT}, "
          f"{len(app.state.entries)} port(s) reconciled)", file=sys.stderr)

    yield

    # Shutdown: unpublish everything we own and clear state. Processes inside
    # the sandbox are about to die anyway when `just down` continues, so
    # leaving host ports published would just be orphans.
    for e in list(app.state.entries):
        await _unpublish(e)
    app.state.entries = []
    _save_state([])
    print("[host-daemon] shutdown: all ports unpublished, state cleared",
          file=sys.stderr)


app = FastAPI(lifespan=lifespan)

# Restrict CORS to the dashboard origin so a malicious site the user visits
# can't drive this daemon via the browser. POST/DELETE bodies use JSON, which
# forces a preflight that disallowed origins can't pass.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        f"http://127.0.0.1:{DASHBOARD_PORT}",
        f"http://localhost:{DASHBOARD_PORT}",
    ],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
    allow_credentials=False,
)


def _require_json(request: Request) -> None:
    """Force a CORS preflight for writes — Content-Type: application/json is
    a non-simple header. Without this, a cross-origin form-encoded POST could
    bypass the preflight entirely."""
    ct = request.headers.get("content-type", "").split(";")[0].strip()
    if ct != "application/json":
        raise HTTPException(status_code=415, detail="Content-Type must be application/json")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/ports")
async def list_ports(project: str | None = None):
    entries = app.state.entries
    if project is not None:
        entries = [e for e in entries if e.project == project]
    return [e.model_dump() for e in entries]


@app.post("/ports")
async def add_port(entry: PortEntry, request: Request):
    _require_json(request)
    existing = {e.key() for e in app.state.entries}
    if entry.key() in existing:
        raise HTTPException(status_code=409, detail=f"port {entry.port}/{entry.protocol} already exposed")
    await _publish(entry)
    app.state.entries.append(entry)
    _save_state(app.state.entries)
    return entry.model_dump()


@app.delete("/ports/{port}")
async def remove_port(port: int, protocol: str = "tcp"):
    key = (port, protocol)
    match = next((e for e in app.state.entries if e.key() == key), None)
    if match is None:
        raise HTTPException(status_code=404, detail="port not found")
    await _unpublish(match)
    app.state.entries = [e for e in app.state.entries if e.key() != key]
    _save_state(app.state.entries)
    return Response(status_code=204)


@app.delete("/projects/{project}/ports")
async def remove_project_ports(project: str):
    """Bulk-remove all ports for a project — used by the dashboard's destroy flow."""
    targets = [e for e in app.state.entries if e.project == project]
    for e in targets:
        await _unpublish(e)
    app.state.entries = [e for e in app.state.entries if e.project != project]
    _save_state(app.state.entries)
    return {"removed": len(targets)}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=HOST_PORT, log_level="warning")
