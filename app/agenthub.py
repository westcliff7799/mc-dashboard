"""Broker between the remote agent and connected browsers (Tier 2).

The agent dials *out* to this dashboard and holds the socket open. That
direction matters: the machine running Minecraft needs no port forwarding, no
static IP, and no inbound firewall change — which is the difference between
"the owner will do this" and "the owner won't".
"""

import asyncio
import contextlib
import time
from collections import deque
from typing import Any

from . import auth, permissions

MAX_LOG_LINES = 800
REQUEST_TIMEOUT = 30.0
POWER_TIMEOUT = 240.0
BACKUP_TIMEOUT = 900.0


class AgentHub:
    def __init__(self) -> None:
        self.agent: Any = None
        self.agent_connected_at: float | None = None
        self.state: dict[str, Any] = {}
        self.logs: deque[dict[str, Any]] = deque(maxlen=MAX_LOG_LINES)
        self.browsers: dict[Any, tuple[str, str]] = {}
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0


    @property
    def connected(self) -> bool:
        return self.agent is not None

    async def attach_agent(self, websocket: Any) -> None:
        if self.agent is not None:
            with contextlib.suppress(Exception):
                await self.agent.close(code=4000, reason="replaced by newer agent")
        self.agent = websocket
        self.agent_connected_at = time.time()
        await self.broadcast({"t": "agent", "connected": True})

    async def detach_agent(self, websocket: Any) -> None:
        if self.agent is not websocket:
            return
        self.agent = None
        self.agent_connected_at = None
        self.state = {}
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("agent disconnected"))
        self._pending.clear()
        await self.broadcast({"t": "agent", "connected": False})

    async def request(
        self, action: str, timeout: float = REQUEST_TIMEOUT, **params: Any
    ) -> dict[str, Any]:
        """Send a command to the agent and wait for its matching reply."""
        if self.agent is None:
            raise ConnectionError("agent is not connected")
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self.agent.send_json({"t": "cmd", "id": request_id, "action": action, **params})
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"agent did not answer '{action}' within {timeout:g}s")
        finally:
            self._pending.pop(request_id, None)

    async def on_agent_message(self, message: dict[str, Any]) -> None:
        kind = message.get("t")

        if kind == "result":
            request_id = message.get("id")
            future = self._pending.get(request_id) if isinstance(request_id, int) else None
            if future and not future.done():
                future.set_result(message)

        elif kind == "log":
            for line in message.get("lines", [message.get("line", "")]):
                if not line:
                    continue
                entry = {"ts": message.get("ts") or time.time(), "line": line}
                self.logs.append(entry)
                await self.broadcast({"t": "log", **entry}, permission=permissions.CONSOLE_VIEW)

        elif kind == "state":
            self.state = message.get("state", {})
            await self.broadcast({"t": "state", "state": self.state})


    def add_browser(self, websocket: Any, username: str, role: str) -> None:
        self.browsers[websocket] = (username, role)

    def discard_browser(self, websocket: Any) -> None:
        self.browsers.pop(websocket, None)

    async def broadcast(self, payload: dict[str, Any], permission: str | None = None) -> None:
        """Send to every browser, or only those holding `permission`.

        Console lines and command echoes are restricted. Blocking the REST
        endpoints is not enough on its own — without this filter an account
        without console access would still watch the server console, and every
        command anyone ran, stream past on their socket.

        The grant is resolved per send rather than cached on the socket, so
        revoking console access takes hold on the next line rather than
        whenever that browser happens to reconnect.
        """
        if not self.browsers:
            return
        dead = []
        for websocket, (username, role) in list(self.browsers.items()):
            if permission is not None and permission not in auth.permissions_for(username, role):
                continue
            try:
                await websocket.send_json(payload)
            except Exception:
                dead.append(websocket)
        for websocket in dead:
            self.browsers.pop(websocket, None)

    def recent_logs(self, limit: int = 300) -> list[dict[str, Any]]:
        return list(self.logs)[-limit:]


hub = AgentHub()
