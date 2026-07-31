"""Minecraft dashboard agent — runs on the machine hosting the server.

Connects OUTBOUND to the dashboard over wss:// and holds the socket open, so
this machine needs no port forwarding, no static IP, and no inbound firewall
rule. Everything it can do is bounded by SERVER_DIR and the four power actions;
it is not a general remote shell.

    pip install websockets
    python3 mc-agent.py --config agent.conf

Config file (INI-style `key = value`, or use environment variables):

    dashboard_url = wss://mc.example.com/agent/ws
    token         = <same AGENT_TOKEN as the dashboard>
    server_dir    = /home/minecraft/server
    mode          = auto          ; auto | systemd | docker | screen | managed
    service_name  = minecraft     ; systemd unit  (mode=systemd)
    container     = minecraft     ; container name (mode=docker)
    screen_name   = minecraft     ; screen session (mode=screen)
    start_command = java -Xmx4G -jar server.jar nogui   ; (mode=managed)
    backup_dir    = /home/minecraft/backups
    backup_keep   = 7
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import shutil
import subprocess
import tarfile
import time

try:
    import websockets
except ImportError:
    raise SystemExit("Missing dependency. Run:  pip install websockets")

LOG_POLL_SECONDS = 0.5
STATE_PUSH_SECONDS = 10
MAX_READ_BYTES = 256 * 1024



def load_config(path: str | None) -> dict[str, str]:
    config: dict[str, str] = {}
    if path and os.path.exists(path):
        for line in pathlib.Path(path).read_text().splitlines():
            line = line.split(";", 1)[0].strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            config[key.strip().lower()] = value.strip()
    for key in (
        "dashboard_url", "token", "server_dir", "mode", "service_name",
        "container", "screen_name", "start_command", "backup_dir", "backup_keep",
    ):
        if env := os.environ.get(f"MCAGENT_{key.upper()}"):
            config[key] = env
    return config


def run(args: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except FileNotFoundError:
        return 127, f"command not found: {args[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"



class Controller:
    """Knows how to start/stop/inspect the server, whatever form it takes."""

    def __init__(self, config: dict[str, str]) -> None:
        self.config = config
        self.mode = config.get("mode", "auto")
        self.service = config.get("service_name", "minecraft")
        self.container = config.get("container", "minecraft")
        self.screen = config.get("screen_name", "minecraft")
        self.start_command = config.get("start_command", "")
        self.server_dir = pathlib.Path(config["server_dir"]).resolve()
        self.managed: subprocess.Popen | None = None
        if self.mode == "auto":
            self.mode = self._detect()

    def _detect(self) -> str:
        if shutil.which("systemctl"):
            code, _ = run(["systemctl", "list-unit-files", f"{self.service}.service"])
            if code == 0:
                return "systemd"
        if shutil.which("docker"):
            code, out = run(["docker", "ps", "-a", "--format", "{{.Names}}"])
            if code == 0 and self.container in out.split():
                return "docker"
        if shutil.which("screen"):
            _, out = run(["screen", "-ls"])
            if self.screen in out:
                return "screen"
        return "managed"

    def is_running(self) -> bool:
        if self.mode == "systemd":
            code, _ = run(["systemctl", "is-active", "--quiet", self.service])
            return code == 0
        if self.mode == "docker":
            _, out = run(["docker", "inspect", "-f", "{{.State.Running}}", self.container])
            return out.strip() == "true"
        if self.mode == "screen":
            _, out = run(["screen", "-ls"])
            return self.screen in out
        return self.managed is not None and self.managed.poll() is None

    def state(self) -> dict:
        running = self.is_running()
        info: dict = {"mode": self.mode, "running": running, "server_dir": str(self.server_dir)}
        if running and self.mode == "docker":
            _, out = run(["docker", "stats", "--no-stream", "--format",
                          "{{.CPUPerc}}|{{.MemUsage}}", self.container])
            if "|" in out:
                cpu, mem = out.split("|", 1)
                info["cpu"], info["memory"] = cpu.strip(), mem.strip()
        elif running and self.mode == "systemd":
            _, out = run(["systemctl", "show", self.service,
                          "--property=MainPID", "--property=ActiveEnterTimestamp"])
            for line in out.splitlines():
                if line.startswith("MainPID="):
                    info["pid"] = line.split("=", 1)[1]
                elif line.startswith("ActiveEnterTimestamp="):
                    info["since"] = line.split("=", 1)[1]
        return info

    def start(self) -> tuple[bool, str]:
        if self.is_running():
            return True, "already running"
        if self.mode == "systemd":
            code, out = run(["systemctl", "start", self.service])
            return code == 0, out or "started"
        if self.mode == "docker":
            code, out = run(["docker", "start", self.container])
            return code == 0, out or "started"
        if self.mode == "screen":
            if not self.start_command:
                return False, "start_command is required for screen mode"
            code, out = run(["screen", "-dmS", self.screen, "bash", "-c",
                             f"cd {self.server_dir} && {self.start_command}"])
            return code == 0, out or "started"
        if not self.start_command:
            return False, "start_command is required for managed mode"
        self.managed = subprocess.Popen(
            self.start_command, shell=True, cwd=self.server_dir,
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True, f"started (pid {self.managed.pid})"

    def stop(self) -> tuple[bool, str]:
        """Always try a graceful in-game `stop` first so chunks get flushed."""
        if not self.is_running():
            return True, "already stopped"
        if self.mode == "systemd":
            code, out = run(["systemctl", "stop", self.service], timeout=120)
            return code == 0, out or "stopped"
        if self.mode == "docker":
            code, out = run(["docker", "stop", "-t", "60", self.container], timeout=120)
            return code == 0, out or "stopped"
        if self.mode == "screen":
            run(["screen", "-S", self.screen, "-X", "stuff", "stop\n"])
            for _ in range(60):
                if not self.is_running():
                    return True, "stopped"
                time.sleep(1)
            return False, "server did not stop within 60s"
        if self.managed and self.managed.stdin:
            try:
                self.managed.stdin.write(b"stop\n")
                self.managed.stdin.flush()
            except Exception:
                pass
        try:
            self.managed.wait(timeout=60)
            return True, "stopped"
        except Exception:
            self.managed.kill()
            return True, "killed after graceful stop timed out"

    def restart(self) -> tuple[bool, str]:
        ok, message = self.stop()
        if not ok:
            return False, f"stop failed: {message}"
        time.sleep(3)
        return self.start()

class LogTail:
    """Follows logs/latest.log, surviving rotation and truncation."""

    def __init__(self, server_dir: pathlib.Path) -> None:
        self.path = server_dir / "logs" / "latest.log"
        self.handle = None
        self.inode = None

    def _reopen(self) -> None:
        if self.handle:
            self.handle.close()
            self.handle = None
        if not self.path.exists():
            return
        self.handle = self.path.open("r", errors="replace")
        stat = os.fstat(self.handle.fileno())
        self.inode = stat.st_ino
        self.handle.seek(0, os.SEEK_END)

    def read_new(self) -> list[str]:
        try:
            if self.handle is None:
                self._reopen()
                return []
            stat = self.path.stat()
            if stat.st_ino != self.inode or stat.st_size < self.handle.tell():
                self._reopen()
                if self.handle:
                    self.handle.seek(0)
            lines = [line.rstrip("\n") for line in self.handle.readlines() if line.strip()]
            return lines[-200:]
        except FileNotFoundError:
            self.handle = None
            return []
        except Exception:
            return []



def safe_path(server_dir: pathlib.Path, relative: str) -> pathlib.Path:
    """Resolve `relative` inside server_dir, refusing anything that escapes it.

    resolve() collapses `..` and follows symlinks, so a symlink pointing at
    /etc/shadow fails this check just as `../../etc/shadow` does.
    """
    target = (server_dir / relative.lstrip("/")).resolve()
    if target != server_dir and server_dir not in target.parents:
        raise PermissionError(f"path escapes server directory: {relative}")
    return target


def list_files(server_dir: pathlib.Path, relative: str) -> dict:
    target = safe_path(server_dir, relative)
    if not target.is_dir():
        raise NotADirectoryError(relative or "/")
    entries = []
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        try:
            stat = child.stat()
        except OSError:
            continue
        entries.append({
            "name": child.name,
            "path": str(child.relative_to(server_dir)),
            "dir": child.is_dir(),
            "size": stat.st_size,
            "modified": stat.st_mtime,
        })
    return {"path": relative.strip("/"), "entries": entries}


def read_file(server_dir: pathlib.Path, relative: str) -> dict:
    target = safe_path(server_dir, relative)
    if not target.is_file():
        raise FileNotFoundError(relative)
    size = target.stat().st_size
    if size > MAX_READ_BYTES:
        raise ValueError(f"file is {size} bytes; only text files under 256 KB can be viewed")
    return {"path": relative, "content": target.read_text(errors="replace"), "size": size}


def make_backup(config: dict, server_dir: pathlib.Path) -> dict:
    backup_dir = pathlib.Path(config.get("backup_dir") or server_dir.parent / "backups")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive = backup_dir / f"world-{stamp}.tar.gz"

    worlds = [p for p in server_dir.iterdir() if p.is_dir() and (p / "level.dat").exists()]
    if not worlds:
        worlds = [p for p in server_dir.iterdir() if p.is_dir() and p.name.startswith("world")]
    if not worlds:
        raise FileNotFoundError("no world directories found in server_dir")

    with tarfile.open(archive, "w:gz") as tar:
        for world in worlds:
            tar.add(world, arcname=world.name)

    keep = int(config.get("backup_keep", 7))
    existing = sorted(backup_dir.glob("world-*.tar.gz"), key=lambda p: p.stat().st_mtime)
    removed = []
    for old in existing[:-keep] if keep > 0 else []:
        old.unlink()
        removed.append(old.name)

    return {
        "archive": archive.name,
        "size": archive.stat().st_size,
        "worlds": [w.name for w in worlds],
        "pruned": removed,
    }


def list_backups(config: dict, server_dir: pathlib.Path) -> dict:
    backup_dir = pathlib.Path(config.get("backup_dir") or server_dir.parent / "backups")
    if not backup_dir.is_dir():
        return {"backups": []}
    items = [
        {"name": p.name, "size": p.stat().st_size, "modified": p.stat().st_mtime}
        for p in sorted(backup_dir.glob("world-*.tar.gz"), reverse=True)
    ]
    return {"backups": items}



async def handle_command(message: dict, controller: Controller, config: dict) -> dict:
    action = message.get("action")
    server_dir = controller.server_dir
    loop = asyncio.get_running_loop()

    def blocking() -> dict:
        if action == "power":
            value = message.get("value")
            ok, detail = {
                "start": controller.start,
                "stop": controller.stop,
                "restart": controller.restart,
            }[value]()
            return {"ok": ok, "detail": detail, "state": controller.state()}
        if action == "list_files":
            return {"ok": True, **list_files(server_dir, message.get("path", ""))}
        if action == "read_file":
            return {"ok": True, **read_file(server_dir, message.get("path", ""))}
        if action == "backup":
            return {"ok": True, **make_backup(config, server_dir)}
        if action == "list_backups":
            return {"ok": True, **list_backups(config, server_dir)}
        if action == "state":
            return {"ok": True, "state": controller.state()}
        raise ValueError(f"unknown action: {action}")

    try:
        result = await loop.run_in_executor(None, blocking)
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"t": "result", "id": message.get("id"), **result}


async def session(url: str, controller: Controller, config: dict) -> None:
    async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**22) as ws:
        print(f"[agent] connected ({controller.mode} mode)", flush=True)
        tail = LogTail(controller.server_dir)
        last_state = 0.0

        async def pump_logs() -> None:
            nonlocal last_state
            while True:
                if lines := tail.read_new():
                    await ws.send(json.dumps({"t": "log", "ts": time.time(), "lines": lines}))
                if time.time() - last_state > STATE_PUSH_SECONDS:
                    last_state = time.time()
                    state = await asyncio.get_running_loop().run_in_executor(None, controller.state)
                    await ws.send(json.dumps({"t": "state", "state": state}))
                await asyncio.sleep(LOG_POLL_SECONDS)

        async def pump_commands() -> None:
            async for raw in ws:
                message = json.loads(raw)
                if message.get("t") == "cmd":
                    reply = await handle_command(message, controller, config)
                    await ws.send(json.dumps(reply))

        done, pending = await asyncio.wait(
            [asyncio.create_task(pump_logs()), asyncio.create_task(pump_commands())],
            return_when=asyncio.FIRST_EXCEPTION,
        )
        for task in pending:
            task.cancel()
        for task in done:
            task.result()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Minecraft dashboard agent")
    parser.add_argument("--config", default="agent.conf")
    args = parser.parse_args()

    config = load_config(args.config)
    for required in ("dashboard_url", "token", "server_dir"):
        if not config.get(required):
            raise SystemExit(f"config error: '{required}' is required")

    controller = Controller(config)
    print(f"[agent] server_dir={controller.server_dir} mode={controller.mode}", flush=True)

    separator = "&" if "?" in config["dashboard_url"] else "?"
    url = f"{config['dashboard_url']}{separator}token={config['token']}"

    backoff = 1
    while True:
        try:
            await session(url, controller, config)
            backoff = 1
        except Exception as exc:
            print(f"[agent] disconnected: {type(exc).__name__}: {exc}", flush=True)
        print(f"[agent] reconnecting in {backoff}s", flush=True)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
