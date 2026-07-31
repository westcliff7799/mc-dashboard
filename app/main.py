"""FastAPI application — the dashboard that runs on the Raspberry Pi."""

import asyncio
import contextlib
import json
import shutil
import time
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import agenthub, auth, ping, rcon
from .agenthub import hub
from .config import ROOT, settings

STATIC = ROOT / "static"

status_cache: dict[str, Any] = {"online": False, "error": "not polled yet", "checked_at": None}


# --------------------------------------------------------------------------
# background polling
# --------------------------------------------------------------------------


async def poll_once() -> dict[str, Any]:
    result = await ping.ping(settings.mc_host, settings.mc_port)
    result["checked_at"] = time.time()

    # The ping sample is capped and often disabled; `/list` is authoritative.
    if result["online"] and settings.rcon_enabled:
        try:
            output = await rcon.execute(
                settings.effective_rcon_host,
                settings.rcon_port,
                settings.rcon_password,
                "list",
            )
            online, maximum, names = rcon.parse_player_list(output)
            result["players_sample"] = names
            result["players_source"] = "rcon"
            if maximum:
                result["players_online"], result["players_max"] = online, maximum
            result["rcon_ok"] = True
        except Exception as exc:
            result["rcon_ok"] = False
            result["rcon_error"] = str(exc)
    else:
        result["players_source"] = "ping"

    result["tiers"] = {
        "ping": True,
        "rcon": settings.rcon_enabled and result.get("rcon_ok", False),
        "agent": hub.connected,
    }
    result["agent_state"] = hub.state
    return result


async def poller() -> None:
    while True:
        try:
            global status_cache
            status_cache = await poll_once()
            await hub.broadcast({"t": "status", "status": status_cache})
        except Exception as exc:  # never let the loop die
            status_cache = {"online": False, "error": str(exc), "checked_at": time.time()}
        await asyncio.sleep(settings.poll_seconds)


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(poller())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


app = FastAPI(title="Minecraft Dashboard", lifespan=lifespan, docs_url=None, redoc_url=None)


# --------------------------------------------------------------------------
# auth plumbing
# --------------------------------------------------------------------------


def current_session(request: Request) -> tuple[str, str]:
    session = auth.validate_session(request.cookies.get(auth.COOKIE_NAME))
    if not session:
        raise HTTPException(status_code=401, detail="not authenticated")
    return session


def current_user(request: Request) -> str:
    """Any signed-in account, guest included. Read-only views only."""
    return current_session(request)[0]


def require_admin(request: Request) -> str:
    """Anything that can change the server, read its files, or see its console.

    The role comes out of the signed session token, so this cannot be talked
    into passing by editing a cookie.
    """
    user, role = current_session(request)
    if role != auth.ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="this account is read-only")
    return user


TRUSTED_PROXIES = {"127.0.0.1", "::1"}


def client_ip(request: Request) -> str:
    """The address the login lockout is keyed on.

    This value decides whether an attacker gets 8 password guesses or an
    unlimited number, so it is only ever read from a source the client cannot
    choose:

    * the headers are ignored entirely unless TRUST_PROXY_HEADERS is on *and*
      the immediate peer is loopback (the tunnel is the sole way in);
    * CF-Connecting-IP is preferred — Cloudflare overwrites it, so the client
      cannot forge it;
    * X-Forwarded-For is read right-to-left. A proxy *appends* the peer it saw,
      so the rightmost entry is the one our own trusted hop wrote. The leftmost
      is whatever the client sent, and keying on it lets one attacker mint a
      fresh identity per request and never trip the lockout.
    """
    peer = request.client.host if request.client else "unknown"
    if not (settings.trust_proxy_headers and peer in TRUSTED_PROXIES):
        return peer

    if cf := request.headers.get("cf-connecting-ip"):
        return cf.strip()
    if forwarded := request.headers.get("x-forwarded-for"):
        if hops := [hop.strip() for hop in forwarded.split(",") if hop.strip()]:
            return hops[-1]
    return peer


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    # Revalidate the UI every load. Without this a browser happily keeps a
    # cached app.js after a deploy, so the page renders the new HTML against
    # the old script — which looks exactly like a broken feature. The ETag
    # makes the check cheap: unchanged files come back as an empty 304.
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    response.headers["Content-Security-Policy"] = (
        # 'self' covers the ws:/wss: upgrade to this same origin; naming the bare
        # schemes would also permit a socket to any host on the internet.
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'"
    )
    return response


# --------------------------------------------------------------------------
# liveness
# --------------------------------------------------------------------------


@app.get("/healthz")
async def healthz():
    """Whether our own poll loop is still turning.

    Unauthenticated, because the supervisor has to reach it before anyone logs
    in — and therefore deliberately mute: no hostname, no player list, no error
    text, nothing an unauthenticated caller could learn from.

    The distinction that matters is "this process is wedged", not "the Minecraft
    server is down". The loop re-polls every poll_seconds and swallows its own
    errors, stamping checked_at either way, so a stale timestamp means the task
    itself died or the event loop stopped turning — the two failures that leave
    systemd looking at a perfectly healthy "active" process serving nothing.
    """
    checked_at = status_cache.get("checked_at")
    age = None if checked_at is None else time.time() - checked_at
    deadline = max(30.0, settings.poll_seconds * 3 + 10)
    alive = age is not None and age <= deadline
    return JSONResponse(
        {"ok": alive, "poll_age_seconds": None if age is None else round(age, 1)},
        status_code=200 if alive else 503,
    )


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------


@app.get("/")
async def index(request: Request):
    if not auth.validate_session(request.cookies.get(auth.COOKIE_NAME)):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(STATIC / "index.html")


@app.get("/login")
async def login_page():
    return FileResponse(STATIC / "login.html")


@app.post("/api/login")
async def do_login(request: Request, username: str = Form(...), password: str = Form(...)):
    ip = client_ip(request)
    if (wait := auth.throttle_check(ip)) > 0:
        raise HTTPException(429, f"Too many attempts. Try again in {wait // 60 + 1} min.")

    if not settings.admin_password_hash:
        raise HTTPException(500, "ADMIN_PASSWORD_HASH is not set — run `python -m app.hashpw`.")

    role = auth.check_credentials(username, password)
    if role is None:
        auth.record_failure(ip)
        raise HTTPException(401, "Invalid credentials")

    auth.clear_failures(ip)
    response = JSONResponse({"ok": True, "role": role})
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.issue_session(username, role),
        max_age=settings.session_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookie,
    )
    return response


@app.post("/api/logout")
async def do_logout():
    response = JSONResponse({"ok": True})
    # The attributes have to mirror the ones used at set_cookie time or the
    # browser keeps the original cookie alongside the expired one.
    response.delete_cookie(
        auth.COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookie,
    )
    return response


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


@app.get("/api/status")
async def api_status(_: str = Depends(current_user)):
    return status_cache


@app.get("/api/capabilities")
async def api_capabilities(request: Request):
    _, role = current_session(request)
    return {
        "rcon": settings.rcon_enabled,
        "agent": hub.connected,
        "agent_configured": settings.agent_enabled,
        "host": f"{settings.mc_host}:{settings.mc_port}",
        # The UI hides what this account can't use. The server enforces it
        # regardless — this is a courtesy, not the boundary.
        "role": role,
    }


@app.post("/api/command")
async def api_command(payload: dict, user: str = Depends(require_admin)):
    command = (payload.get("command") or "").strip().lstrip("/")
    if not command:
        raise HTTPException(400, "empty command")
    if not settings.rcon_enabled:
        raise HTTPException(409, "RCON is not configured on this dashboard")
    try:
        output = await rcon.execute(
            settings.effective_rcon_host, settings.rcon_port, settings.rcon_password, command
        )
    except Exception as exc:
        raise HTTPException(502, f"RCON failed: {exc}")

    await hub.broadcast(
        {"t": "log", "ts": time.time(), "line": f"[{user}] > /{command}", "kind": "echo"},
        roles=agenthub.ADMIN_ONLY,
    )
    if output:
        await hub.broadcast(
            {"t": "log", "ts": time.time(), "line": output, "kind": "reply"},
            roles=agenthub.ADMIN_ONLY,
        )
    return {"ok": True, "output": output}


@app.post("/api/power")
async def api_power(payload: dict, user: str = Depends(require_admin)):
    action = payload.get("action")
    if action not in {"start", "stop", "restart"}:
        raise HTTPException(400, "action must be start, stop or restart")
    if not hub.connected:
        raise HTTPException(409, "the agent is not connected — lifecycle control is unavailable")
    try:
        result = await hub.request("power", timeout=agenthub.POWER_TIMEOUT, value=action)
    except Exception as exc:
        raise HTTPException(502, str(exc))
    await hub.broadcast(
        {"t": "log", "ts": time.time(), "line": f"[{user}] requested {action}", "kind": "echo"},
        roles=agenthub.ADMIN_ONLY,
    )
    return result


@app.get("/api/logs")
async def api_logs(_: str = Depends(require_admin)):
    return {"lines": hub.recent_logs()}


@app.get("/api/files")
async def api_files(path: str = "", _: str = Depends(require_admin)):
    if not hub.connected:
        raise HTTPException(409, "the agent is not connected — file access is unavailable")
    try:
        return await hub.request("list_files", path=path)
    except Exception as exc:
        raise HTTPException(502, str(exc))


@app.get("/api/files/read")
async def api_file_read(path: str, _: str = Depends(require_admin)):
    if not hub.connected:
        raise HTTPException(409, "the agent is not connected — file access is unavailable")
    try:
        return await hub.request("read_file", path=path)
    except Exception as exc:
        raise HTTPException(502, str(exc))


@app.post("/api/backup")
async def api_backup(user: str = Depends(require_admin)):
    if not hub.connected:
        raise HTTPException(409, "the agent is not connected — backups are unavailable")
    try:
        result = await hub.request("backup", timeout=agenthub.BACKUP_TIMEOUT)
    except Exception as exc:
        raise HTTPException(502, str(exc))
    await hub.broadcast(
        {"t": "log", "ts": time.time(), "line": f"[{user}] triggered a backup", "kind": "echo"},
        roles=agenthub.ADMIN_ONLY,
    )
    return result


@app.get("/api/backups")
async def api_backups(_: str = Depends(require_admin)):
    if not hub.connected:
        raise HTTPException(409, "the agent is not connected")
    try:
        return await hub.request("list_backups")
    except Exception as exc:
        raise HTTPException(502, str(exc))


# --------------------------------------------------------------------------
# debug panel (owner only)
# --------------------------------------------------------------------------

PM2_BIN = shutil.which("pm2") or "/usr/bin/pm2"


@app.get("/api/admin/pm2")
async def api_pm2(_: str = Depends(require_admin)):
    """PM2 process table.

    Only the handful of fields below are passed through. `pm2 jlist` embeds
    each process's entire environment under pm2_env, so returning the raw
    output would hand over every secret those processes were started with.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            PM2_BIN, "jlist",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        raise HTTPException(502, f"could not run pm2: {type(exc).__name__}")

    try:
        async with asyncio.timeout(15):
            raw = (await process.communicate())[0]  # stderr goes to DEVNULL
    except TimeoutError:
        # Don't leave a hung pm2 behind on every refresh.
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        raise HTTPException(502, "pm2 did not respond within 15s")

    try:
        entries = json.loads(raw or b"[]")
    except json.JSONDecodeError:
        raise HTTPException(502, "pm2 returned output that wasn't JSON")

    now = time.time()
    processes = []
    for entry in entries:
        environment = entry.get("pm2_env") or {}
        monitor = entry.get("monit") or {}
        started_ms = environment.get("pm_uptime")
        processes.append(
            {
                "id": entry.get("pm_id"),
                "name": entry.get("name"),
                "status": environment.get("status"),
                "pid": entry.get("pid"),
                "restarts": environment.get("restart_time"),
                "cpu": monitor.get("cpu"),
                "memory_mb": round((monitor.get("memory") or 0) / 1048576, 1),
                "uptime_seconds": round(now - started_ms / 1000) if started_ms else None,
            }
        )
    return {"processes": processes}


@app.get("/api/admin/users")
async def api_list_users(_: str = Depends(require_admin)):
    return {
        "admin": settings.admin_user,
        # Hashes stay server-side; the panel only ever needs the names.
        "users": [
            {"username": entry["username"], "role": entry.get("role", auth.ROLE_GUEST)}
            for entry in auth.load_users()
        ],
    }


@app.post("/api/admin/users")
async def api_add_user(payload: dict, _: str = Depends(require_admin)):
    try:
        auth.add_user(str(payload.get("username", "")), str(payload.get("password", "")))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


@app.delete("/api/admin/users/{username}")
async def api_delete_user(username: str, _: str = Depends(require_admin)):
    if not auth.delete_user(username):
        raise HTTPException(404, "no such user")
    return {"ok": True}


# --------------------------------------------------------------------------
# websockets
# --------------------------------------------------------------------------


@app.websocket("/ws")
async def browser_socket(websocket: WebSocket):
    session = auth.validate_session(websocket.cookies.get(auth.COOKIE_NAME))
    if not session:
        await websocket.close(code=4401, reason="not authenticated")
        return
    _, role = session
    await websocket.accept()
    hub.add_browser(websocket, role)
    try:
        await websocket.send_json({"t": "status", "status": status_cache})
        await websocket.send_json({"t": "agent", "connected": hub.connected})
        # The console backlog is the same admin-only data as the live feed —
        # handing it over at connect time would undo the broadcast filter.
        if role == auth.ROLE_ADMIN:
            await websocket.send_json({"t": "backlog", "lines": hub.recent_logs()})
        if hub.state:
            await websocket.send_json({"t": "state", "state": hub.state})
        while True:
            await websocket.receive_text()  # client keepalives; nothing to parse
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        hub.discard_browser(websocket)


@app.websocket("/agent/ws")
async def agent_socket(websocket: WebSocket):
    """The remote agent's outbound connection. Token is checked before accept."""
    if not settings.agent_enabled:
        await websocket.close(code=4403, reason="agent support is disabled")
        return

    token = websocket.query_params.get("token") or ""
    header = websocket.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:]
    if not auth.constant_time_equal(token, settings.agent_token):
        await websocket.close(code=4401, reason="bad agent token")
        return

    await websocket.accept()
    await hub.attach_agent(websocket)
    try:
        while True:
            await hub.on_agent_message(await websocket.receive_json())
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await hub.detach_agent(websocket)


app.mount("/static", StaticFiles(directory=STATIC), name="static")
