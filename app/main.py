"""FastAPI application — the dashboard that runs on the Raspberry Pi."""

import asyncio
import contextlib
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


def current_user(request: Request) -> str:
    user = auth.validate_session(request.cookies.get(auth.COOKIE_NAME))
    if not user:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


def client_ip(request: Request) -> str:
    # Cloudflare Tunnel / nginx put the real client here; fall back to the peer.
    forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self' ws: wss:; base-uri 'none'; form-action 'self'"
    )
    return response


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

    if not auth.check_credentials(username, password):
        auth.record_failure(ip)
        raise HTTPException(401, "Invalid credentials")

    auth.clear_failures(ip)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.issue_session(username),
        max_age=settings.session_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookie,
    )
    return response


@app.post("/api/logout")
async def do_logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE_NAME)
    return response


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


@app.get("/api/status")
async def api_status(_: str = Depends(current_user)):
    return status_cache


@app.get("/api/capabilities")
async def api_capabilities(_: str = Depends(current_user)):
    return {
        "rcon": settings.rcon_enabled,
        "agent": hub.connected,
        "agent_configured": settings.agent_enabled,
        "host": f"{settings.mc_host}:{settings.mc_port}",
    }


@app.post("/api/command")
async def api_command(payload: dict, user: str = Depends(current_user)):
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
        {"t": "log", "ts": time.time(), "line": f"[{user}] > /{command}", "kind": "echo"}
    )
    if output:
        await hub.broadcast({"t": "log", "ts": time.time(), "line": output, "kind": "reply"})
    return {"ok": True, "output": output}


@app.post("/api/power")
async def api_power(payload: dict, user: str = Depends(current_user)):
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
        {"t": "log", "ts": time.time(), "line": f"[{user}] requested {action}", "kind": "echo"}
    )
    return result


@app.get("/api/logs")
async def api_logs(_: str = Depends(current_user)):
    return {"lines": hub.recent_logs()}


@app.get("/api/files")
async def api_files(path: str = "", _: str = Depends(current_user)):
    if not hub.connected:
        raise HTTPException(409, "the agent is not connected — file access is unavailable")
    try:
        return await hub.request("list_files", path=path)
    except Exception as exc:
        raise HTTPException(502, str(exc))


@app.get("/api/files/read")
async def api_file_read(path: str, _: str = Depends(current_user)):
    if not hub.connected:
        raise HTTPException(409, "the agent is not connected — file access is unavailable")
    try:
        return await hub.request("read_file", path=path)
    except Exception as exc:
        raise HTTPException(502, str(exc))


@app.post("/api/backup")
async def api_backup(user: str = Depends(current_user)):
    if not hub.connected:
        raise HTTPException(409, "the agent is not connected — backups are unavailable")
    try:
        result = await hub.request("backup", timeout=agenthub.BACKUP_TIMEOUT)
    except Exception as exc:
        raise HTTPException(502, str(exc))
    await hub.broadcast(
        {"t": "log", "ts": time.time(), "line": f"[{user}] triggered a backup", "kind": "echo"}
    )
    return result


@app.get("/api/backups")
async def api_backups(_: str = Depends(current_user)):
    if not hub.connected:
        raise HTTPException(409, "the agent is not connected")
    try:
        return await hub.request("list_backups")
    except Exception as exc:
        raise HTTPException(502, str(exc))


# --------------------------------------------------------------------------
# websockets
# --------------------------------------------------------------------------


@app.websocket("/ws")
async def browser_socket(websocket: WebSocket):
    if not auth.validate_session(websocket.cookies.get(auth.COOKIE_NAME)):
        await websocket.close(code=4401, reason="not authenticated")
        return
    await websocket.accept()
    hub.add_browser(websocket)
    try:
        await websocket.send_json({"t": "status", "status": status_cache})
        await websocket.send_json({"t": "agent", "connected": hub.connected})
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

    import hmac

    token = websocket.query_params.get("token") or ""
    header = websocket.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:]
    if not hmac.compare_digest(token, settings.agent_token):
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
