"""FastAPI application — the dashboard that runs on the Raspberry Pi."""

import asyncio
import base64
import contextlib
import json
import posixpath
import re
import shutil
import time
import urllib.parse
from typing import Any, AsyncIterator

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
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


async def audit(user: str, line: str) -> None:
    """Put an operator action into the same console feed as everything else.

    Every mutating request lands here, so the record of who uploaded, renamed or
    deleted what sits inline with the server's own log and the commands people
    ran — one timeline rather than three.
    """
    await hub.announce(f"[{user}] {line}")


async def ask_agent(action: str, timeout: float = agenthub.REQUEST_TIMEOUT, **params: Any) -> dict:
    """Round-trip to the agent, turning transport failures into HTTP errors."""
    if not hub.connected:
        raise HTTPException(409, "the agent is not connected — file access is unavailable")
    try:
        return await hub.request(action, timeout=timeout, **params)
    except Exception as exc:
        raise HTTPException(502, str(exc))


def agent_result(reply: dict[str, Any]) -> dict[str, Any]:
    """Unwrap a reply, raising whatever the agent refused with.

    The agent reports a rejected path or a full disk as `ok: false` with a
    message meant for a person, so it becomes a 400 the browser can show rather
    than a success the caller has to inspect.

    One case is worth translating. The agent lives on someone else's machine and
    is updated by hand, so it can be older than this dashboard — and an older one
    answers a request it has never heard of with a bare "unknown action", which
    reads like a bug here rather than a version skew. Naming the actual problem
    saves whoever sees it from debugging the wrong end.
    """
    if not reply.get("ok", False):
        detail = str(reply.get("error") or reply.get("detail") or "the agent could not do that")
        if "unknown action" in detail:
            raise HTTPException(
                409,
                "the agent on the server machine is running an older mc-agent.py that does not "
                "support this — copy the current one over and restart it",
            )
        raise HTTPException(400, detail)
    return {key: value for key, value in reply.items() if key not in ("t", "id", "ok")}


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
async def limit_request_body(request: Request, call_next):
    """Reject an oversized upload before a byte of it is read.

    Starlette parses the whole multipart body — spooling it to this machine's
    disk — before the endpoint that would check its size ever runs, so the check
    has to happen out here. A request without a Content-Length is let through
    and counted as it streams instead.
    """
    if request.method == "POST" and request.url.path == "/api/files/upload":
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_upload_bytes:
            return JSONResponse(
                {"detail": f"upload exceeds the {settings.max_upload_mb} MB limit"},
                status_code=413,
            )
    return await call_next(request)


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

    await audit(user, f"> /{command}")
    if output:
        await hub.announce(output, kind="reply")
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
    await audit(user, f"requested {action}")
    return result


@app.get("/api/logs")
async def api_logs(_: str = Depends(require(permissions.CONSOLE_VIEW))):
    return {"lines": hub.recent_logs()}


FILE_CHUNK_BYTES = 256 * 1024
UPLOAD_TIMEOUT = 120.0

UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]")


def clean_relative(path: Any) -> str:
    """Normalise a client-supplied path to a plain relative one.

    The agent is the real boundary — every path it touches goes through
    safe_path()/safe_leaf() against its own server_dir, which is the only place
    that can actually decide what is inside the tree. This is the cheap check in
    front of it, and it *rejects* `..` rather than collapsing it. Normalising
    would be the tempting thing to do and is the wrong thing: `../../x.txt`
    would quietly become `x.txt`, so a traversal attempt would land as a
    successful write somewhere the caller never named. Refusing keeps a
    surprising path surprising.

    A leading slash is only ever a way of writing "from the server directory",
    so that one is stripped rather than refused.
    """
    text = str(path or "").replace("\\", "/").strip()
    if any(character in text for character in "\0\n\r"):
        raise HTTPException(400, "invalid path")
    if any(segment == ".." for segment in text.split("/")):
        raise HTTPException(400, "paths cannot step outside the server directory")
    normalised = posixpath.normpath("/" + text).lstrip("/")
    return "" if normalised == "." else normalised


def clean_name(name: Any) -> str:
    """A single path component: no directories, no traversal, no separators."""
    text = str(name or "").replace("\\", "/").strip().strip("/")
    if not text or "/" in text or text in (".", "..") or "\0" in text:
        raise HTTPException(400, "invalid name")
    return text


def join_relative(directory: str, name: str) -> str:
    return f"{directory}/{name}" if directory else name


@app.get("/api/files")
async def api_files(path: str = "", _: str = Depends(require(permissions.FILES_BROWSE))):
    return agent_result(await ask_agent("list_files", path=clean_relative(path)))


@app.get("/api/files/read")
async def api_file_read(path: str, _: str = Depends(require(permissions.FILES_READ))):
    return agent_result(await ask_agent("read_file", path=clean_relative(path)))


def disposition(name: str) -> str:
    """A Content-Disposition value that a filename cannot break out of.

    The name comes from the server's own filesystem, but it still ends up in a
    response header, so it is spelled twice: an ASCII-only fallback with every
    awkward character replaced, and an RFC 5987 form for anything Unicode. Both
    are escaped, which is what keeps a file called `x";\\nSet-Cookie: …` from
    becoming a second header.
    """
    ascii_name = UNSAFE_FILENAME.sub("_", name) or "download"
    quoted = urllib.parse.quote(name, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"


@app.get("/api/files/download")
async def api_file_download(path: str, user: str = Depends(require(permissions.FILES_READ))):
    """Stream a file of any size out through the agent socket.

    Pulled chunk by chunk and yielded straight to the browser, so a 400 MB world
    archive never has to be held whole in this process's memory — the Pi has
    little enough of it, and several people may be downloading at once.
    """
    relative = clean_relative(path)
    info = agent_result(await ask_agent("stat_file", path=relative))
    size = int(info.get("size") or 0)

    async def pull() -> AsyncIterator[bytes]:
        offset = 0
        while offset < size:
            reply = agent_result(
                await ask_agent(
                    "read_chunk", path=relative, offset=offset, length=FILE_CHUNK_BYTES
                )
            )
            blob = base64.b64decode(reply.get("data") or "")
            if not blob:
                break
            offset += len(blob)
            yield blob

    await audit(user, f"downloaded {relative}")
    return StreamingResponse(
        pull(),
        media_type="application/octet-stream",
        headers={
            "Content-Length": str(size),
            "Content-Disposition": disposition(str(info.get("name") or "download")),
        },
    )


@app.post("/api/files/write")
async def api_file_write(payload: dict, user: str = Depends(require(permissions.FILES_WRITE))):
    relative = clean_relative(payload.get("path"))
    if not relative:
        raise HTTPException(400, "a path is required")
    result = agent_result(
        await ask_agent(
            "write_file",
            path=relative,
            content=str(payload.get("content") or ""),
            overwrite=bool(payload.get("overwrite", True)),
        )
    )
    await audit(user, f"saved {result.get('path', relative)} ({result.get('size', 0)} bytes)")
    return result


@app.post("/api/files/folder")
async def api_file_mkdir(payload: dict, user: str = Depends(require(permissions.FILES_WRITE))):
    target = join_relative(clean_relative(payload.get("path")), clean_name(payload.get("name")))
    result = agent_result(await ask_agent("make_directory", path=target))
    await audit(user, f"created folder {result.get('path', target)}")
    return result


@app.post("/api/files/rename")
async def api_file_rename(payload: dict, user: str = Depends(require(permissions.FILES_WRITE))):
    """Rename in place, or move by giving a `to` that contains a slash."""
    source = clean_relative(payload.get("path"))
    if not source:
        raise HTTPException(400, "a path is required")

    requested = str(payload.get("to") or "")
    if "/" in requested.replace("\\", "/").strip("/"):
        destination = clean_relative(requested)
    else:
        destination = join_relative(posixpath.dirname(source), clean_name(requested))
    if not destination:
        raise HTTPException(400, "a destination is required")

    result = agent_result(await ask_agent("move_path", path=source, to=destination))
    await audit(user, f"renamed {source} → {result.get('path', destination)}")
    return result


@app.post("/api/files/delete")
async def api_file_delete(payload: dict, user: str = Depends(require(permissions.FILES_DELETE))):
    raw = payload.get("paths")
    if not isinstance(raw, list) or not raw:
        raise HTTPException(400, "paths must be a non-empty list")
    if len(raw) > 200:
        raise HTTPException(400, "too many paths in one request")

    targets = [clean_relative(item) for item in raw]
    if not all(targets):
        raise HTTPException(400, "the server directory itself cannot be deleted")

    result = agent_result(await ask_agent("delete_paths", paths=targets, timeout=120.0))
    if removed := result.get("removed"):
        await audit(user, f"deleted {', '.join(removed)}")
    return result


@app.post("/api/files/upload")
async def api_file_upload(
    path: str = Form(""),
    overwrite: bool = Form(False),
    upload: UploadFile = File(...),
    user: str = Depends(require(permissions.FILES_WRITE)),
):
    """Forward one uploaded file to the agent as a sequence of chunks.

    The agent writes them to a temp file beside the destination and renames it
    on commit, so a connection that drops halfway leaves nothing behind under
    the real name. Anything that goes wrong after the upload has been opened
    aborts it explicitly rather than waiting for the idle sweep.
    """
    name = clean_name(posixpath.basename(str(upload.filename or "").replace("\\", "/")))
    target = join_relative(clean_relative(path), name)

    declared = int(upload.size or 0)
    if declared > settings.max_upload_bytes:
        raise HTTPException(413, f"file is larger than the {settings.max_upload_mb} MB limit")

    opened = agent_result(
        await ask_agent("upload_begin", path=target, size=declared, overwrite=overwrite)
    )
    upload_id = opened.get("upload")

    try:
        sent = 0
        while blob := await upload.read(FILE_CHUNK_BYTES):
            sent += len(blob)
            if sent > settings.max_upload_bytes:
                raise HTTPException(413, f"file exceeds the {settings.max_upload_mb} MB limit")
            agent_result(
                await ask_agent(
                    "upload_chunk",
                    timeout=UPLOAD_TIMEOUT,
                    upload=upload_id,
                    data=base64.b64encode(blob).decode(),
                )
            )
        result = agent_result(
            await ask_agent("upload_commit", timeout=UPLOAD_TIMEOUT, upload=upload_id)
        )
    except Exception:
        with contextlib.suppress(Exception):
            await hub.request("upload_abort", upload=upload_id)
        raise

    await audit(user, f"uploaded {result.get('path', target)} ({result.get('size', 0)} bytes)")
    return result


@app.post("/api/backup")
async def api_backup(user: str = Depends(require(permissions.BACKUP_CREATE))):
    if not hub.connected:
        raise HTTPException(409, "the agent is not connected — backups are unavailable")
    try:
        result = await hub.request("backup", timeout=agenthub.BACKUP_TIMEOUT)
    except Exception as exc:
        raise HTTPException(502, str(exc))
    await audit(user, "triggered a backup")
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
