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

from . import agenthub, auth, permissions, ping, rcon
from .agenthub import hub
from .config import ROOT, settings

STATIC = ROOT / "static"

status_cache: dict[str, Any] = {"online": False, "error": "not polled yet", "checked_at": None}


async def poll_once() -> dict[str, Any]:
    result = await ping.ping(settings.mc_host, settings.mc_port)
    result["checked_at"] = time.time()

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
        except Exception as exc:
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


def current_session(request: Request) -> tuple[str, str]:
    session = auth.validate_session(request.cookies.get(auth.COOKIE_NAME))
    if not session:
        raise HTTPException(status_code=401, detail="not authenticated")
    return session


def current_user(request: Request) -> str:
    """Any signed-in account, guest included. Read-only views only."""
    return current_session(request)[0]


def require_admin(request: Request) -> str:
    """The .env owner only. Guards account management itself.

    Deliberately not expressed as a grantable permission: anyone who can edit
    grants can grant themselves everything, so "may manage users" and "is the
    owner" are the same authority. Keeping it tied to the signed role means no
    combination of checkboxes in the panel can mint a second owner.
    """
    user, role = current_session(request)
    if role != auth.ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="owner only")
    return user


def require(*needed: str):
    """Build a dependency that passes only if the caller holds every `needed`.

    Authorization is resolved from storage per request rather than from the
    session token, so a revoked permission takes effect on the caller's very
    next request instead of whenever their week-long session happens to expire.
    """

    def dependency(request: Request) -> str:
        user, role = current_session(request)
        held = auth.permissions_for(user, role)
        if missing := [name for name in needed if name not in held]:
            raise HTTPException(status_code=403, detail=f"missing permission: {', '.join(missing)}")
        return user

    return dependency


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
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'"
    )
    return response


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
    response.delete_cookie(
        auth.COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookie,
    )
    return response


@app.get("/api/status")
async def api_status(_: str = Depends(current_user)):
    return status_cache


@app.get("/api/capabilities")
async def api_capabilities(request: Request):
    user, role = current_session(request)
    return {
        "rcon": settings.rcon_enabled,
        "agent": hub.connected,
        "agent_configured": settings.agent_enabled,
        "host": f"{settings.mc_host}:{settings.mc_port}",
        "role": role,
        "permissions": sorted(auth.permissions_for(user, role)),
    }


@app.post("/api/command")
async def api_command(payload: dict, user: str = Depends(require(permissions.CONSOLE_COMMAND))):
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
        permission=permissions.CONSOLE_VIEW,
    )
    if output:
        await hub.broadcast(
            {"t": "log", "ts": time.time(), "line": output, "kind": "reply"},
            permission=permissions.CONSOLE_VIEW,
        )
    return {"ok": True, "output": output}


@app.post("/api/power")
async def api_power(payload: dict, request: Request):
    action = payload.get("action")
    if action not in {"start", "stop", "restart"}:
        raise HTTPException(400, "action must be start, stop or restart")

    user, role = current_session(request)
    needed = f"power.{action}"
    if needed not in auth.permissions_for(user, role):
        raise HTTPException(403, f"missing permission: {needed}")

    if not hub.connected:
        raise HTTPException(409, "the agent is not connected — lifecycle control is unavailable")
    try:
        result = await hub.request("power", timeout=agenthub.POWER_TIMEOUT, value=action)
    except Exception as exc:
        raise HTTPException(502, str(exc))
    await hub.broadcast(
        {"t": "log", "ts": time.time(), "line": f"[{user}] requested {action}", "kind": "echo"},
        permission=permissions.CONSOLE_VIEW,
    )
    return result


@app.get("/api/logs")
async def api_logs(_: str = Depends(require(permissions.CONSOLE_VIEW))):
    return {"lines": hub.recent_logs()}


@app.get("/api/files")
async def api_files(path: str = "", _: str = Depends(require(permissions.FILES_BROWSE))):
    if not hub.connected:
        raise HTTPException(409, "the agent is not connected — file access is unavailable")
    try:
        return await hub.request("list_files", path=path)
    except Exception as exc:
        raise HTTPException(502, str(exc))


@app.get("/api/files/read")
async def api_file_read(path: str, _: str = Depends(require(permissions.FILES_READ))):
    if not hub.connected:
        raise HTTPException(409, "the agent is not connected — file access is unavailable")
    try:
        return await hub.request("read_file", path=path)
    except Exception as exc:
        raise HTTPException(502, str(exc))


@app.post("/api/backup")
async def api_backup(user: str = Depends(require(permissions.BACKUP_CREATE))):
    if not hub.connected:
        raise HTTPException(409, "the agent is not connected — backups are unavailable")
    try:
        result = await hub.request("backup", timeout=agenthub.BACKUP_TIMEOUT)
    except Exception as exc:
        raise HTTPException(502, str(exc))
    await hub.broadcast(
        {"t": "log", "ts": time.time(), "line": f"[{user}] triggered a backup", "kind": "echo"},
        permission=permissions.CONSOLE_VIEW,
    )
    return result


@app.get("/api/backups")
async def api_backups(_: str = Depends(require(permissions.BACKUP_LIST))):
    if not hub.connected:
        raise HTTPException(409, "the agent is not connected")
    try:
        return await hub.request("list_backups")
    except Exception as exc:
        raise HTTPException(502, str(exc))


PM2_BIN = shutil.which("pm2") or "/usr/bin/pm2"


@app.get("/api/admin/pm2")
async def api_pm2(_: str = Depends(require(permissions.DEBUG_PM2))):
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
            raw = (await process.communicate())[0]
    except TimeoutError:
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
        "catalogue": permissions.GROUPS,
        "users": [
            {"username": entry["username"], "permissions": entry["permissions"]}
            for entry in auth.load_users()
        ],
    }


@app.post("/api/admin/users")
async def api_add_user(payload: dict, _: str = Depends(require_admin)):
    try:
        auth.add_user(
            str(payload.get("username", "")),
            str(payload.get("password", "")),
            payload.get("permissions"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


@app.put("/api/admin/users/{username}/permissions")
async def api_set_permissions(username: str, payload: dict, _: str = Depends(require_admin)):
    """Replace one account's grant outright.

    The whole set is sent rather than a delta, so two panels open at once can't
    interleave into a grant neither operator chose — the last save wins as a
    unit and the panel reloads to show it.
    """
    granted = payload.get("permissions")
    if not isinstance(granted, list):
        raise HTTPException(400, "permissions must be a list")
    if not auth.set_user_permissions(username, granted):
        raise HTTPException(404, "no such user")
    return {"ok": True, "permissions": permissions.sanitize(granted)}


@app.delete("/api/admin/users/{username}")
async def api_delete_user(username: str, _: str = Depends(require_admin)):
    if not auth.delete_user(username):
        raise HTTPException(404, "no such user")
    return {"ok": True}


@app.websocket("/ws")
async def browser_socket(websocket: WebSocket):
    session = auth.validate_session(websocket.cookies.get(auth.COOKIE_NAME))
    if not session:
        await websocket.close(code=4401, reason="not authenticated")
        return
    username, role = session
    await websocket.accept()
    hub.add_browser(websocket, username, role)
    try:
        await websocket.send_json({"t": "status", "status": status_cache})
        await websocket.send_json({"t": "agent", "connected": hub.connected})
        if permissions.CONSOLE_VIEW in auth.permissions_for(username, role):
            await websocket.send_json({"t": "backlog", "lines": hub.recent_logs()})
        if hub.state:
            await websocket.send_json({"t": "state", "state": hub.state})
        while True:
            await websocket.receive_text()
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
