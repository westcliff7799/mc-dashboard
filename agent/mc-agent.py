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
    allow_writes  = no            ; opt in before the dashboard may change files
    max_upload_mb = 512           ; largest single upload accepted
    tunnel_api    = http://127.0.0.1:4040/api/tunnels   ; ngrok's local API
    server_port   = 25565         ; local port the tunnel forwards to
    rcon_port     = 25575
    public_mc     = 2.tcp.ngrok.io:11591   ; skip discovery, state it outright
    public_rcon   = 4.tcp.ngrok.io:17554
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os
import pathlib
import secrets
import shutil
import subprocess
import tarfile
import time
import urllib.request
import zipfile
from typing import Any, BinaryIO, TextIO

try:
    import websockets
except ImportError:
    raise SystemExit("Missing dependency. Run:  pip install websockets")

LOG_POLL_SECONDS = 0.5
STATE_PUSH_SECONDS = 10
MAX_READ_BYTES = 256 * 1024
MAX_WRITE_BYTES = 1024 * 1024
CHUNK_BYTES = 256 * 1024
DEFAULT_MAX_UPLOAD_MB = 512
UPLOAD_IDLE_SECONDS = 600
MAX_ARCHIVE_ENTRIES = 20000
SYMLINK_MODE = 0o120000
UPLOAD_SUFFIX = ".dashboard-upload"
WRITE_SUFFIX = ".dashboard-tmp"
FREE_SPACE_MARGIN = 64 * 1024 * 1024
TUNNEL_API_DEFAULT = "http://127.0.0.1:4040/api/tunnels"
TUNNEL_CACHE_SECONDS = 60
TUNNEL_TIMEOUT = 2.0
TUNNEL_MAX_BYTES = 256 * 1024



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
        "allow_writes", "max_upload_mb", "tunnel_api", "server_port", "rcon_port",
        "public_mc", "public_rcon",
    ):
        if env := os.environ.get(f"MCAGENT_{key.upper()}"):
            config[key] = env
    return config


def truthy(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in ("1", "yes", "y", "true", "on", "enabled")


def writes_allowed(config: dict[str, str]) -> bool:
    """Whether this machine's owner has opted in to file modification.

    Off unless the config says otherwise, because earlier versions of this agent
    could only read, and the README promised exactly that. Dropping in a newer
    script should not quietly widen what the dashboard may do to someone's
    files — that has to be a decision they make in their own config.
    """
    return truthy(config.get("allow_writes"), False)


def upload_limit(config: dict[str, str]) -> int:
    try:
        megabytes = int(config.get("max_upload_mb") or DEFAULT_MAX_UPLOAD_MB)
    except ValueError:
        megabytes = DEFAULT_MAX_UPLOAD_MB
    return max(megabytes, 1) * 1024 * 1024


def port_or(raw: str | None, default: int) -> int:
    try:
        return int(str(raw).strip())
    except (AttributeError, TypeError, ValueError):
        return default


class EndpointProbe:
    """The public address this machine is currently reachable on.

    A tunnel hands out a fresh host:port every time it restarts. The dashboard
    cannot see that happen from the outside — a reassigned address and a
    stopped server both just stop answering — but the tunnel's own local API
    can, and it is on this side of the connection. Reporting the address
    upward is what keeps the dashboard pointed at the right place without
    anyone hand-editing a config after every restart.

    Best effort throughout: no tunnel, no API, or an API that answers with
    something unexpected simply means no address is reported and the dashboard
    falls back to what its own config says.
    """

    def __init__(self, config: dict[str, str]) -> None:
        self.api = config.get("tunnel_api", TUNNEL_API_DEFAULT).strip()
        self.manual = {
            "mc": (config.get("public_mc") or "").strip(),
            "rcon": (config.get("public_rcon") or "").strip(),
        }
        self.local_ports = {
            "mc": port_or(config.get("server_port"), 25565),
            "rcon": port_or(config.get("rcon_port"), 25575),
        }
        self._cached: dict[str, str] = {}
        self._fetched = 0.0

    def current(self) -> dict[str, str]:
        found = {key: value for key, value in self.manual.items() if value}
        if len(found) < len(self.manual):
            for key, value in self._discover().items():
                found.setdefault(key, value)
        return found

    def _discover(self) -> dict[str, str]:
        """Ask the local tunnel API which public address maps to which port."""
        if time.time() - self._fetched < TUNNEL_CACHE_SECONDS:
            return self._cached
        self._fetched = time.time()
        self._cached = {}
        if not self.api:
            return self._cached
        try:
            with urllib.request.urlopen(self.api, timeout=TUNNEL_TIMEOUT) as response:
                payload = json.loads(response.read(TUNNEL_MAX_BYTES))
        except Exception:
            return self._cached
        for tunnel in payload.get("tunnels") or []:
            if not isinstance(tunnel, dict) or tunnel.get("proto") != "tcp":
                continue
            public = str(tunnel.get("public_url") or "").rsplit("//", 1)[-1]
            forwards = str((tunnel.get("config") or {}).get("addr") or "")
            _, _, local_port = forwards.rpartition(":")
            if not public or not local_port.isdigit():
                continue
            for key, port in self.local_ports.items():
                if int(local_port) == port:
                    self._cached[key] = public
        return self._cached


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
        self.endpoints = EndpointProbe(config)
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
        info: dict = {
            "mode": self.mode,
            "running": running,
            "server_dir": str(self.server_dir),
            "writable": writes_allowed(self.config),
        }
        if endpoints := self.endpoints.current():
            info["endpoints"] = endpoints
        with contextlib.suppress(OSError):
            usage = shutil.disk_usage(self.server_dir)
            info["disk_free"], info["disk_total"] = usage.free, usage.total
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
        if self.managed is None:
            return False, "no managed process to stop"
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
        self.handle: TextIO | None = None
        self.inode: int | None = None

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


def safe_leaf(server_dir: pathlib.Path, relative: str) -> pathlib.Path:
    """Locate something by name without resolving the name itself.

    Every operation that creates, replaces, moves or removes an entry needs a
    path that may not exist yet, so safe_path()'s resolve() cannot vet it. The
    parent goes through safe_path() as usual; the final component is only
    joined on, never followed.

    That difference matters twice. A symlink in the tree pointing outside it
    stays deletable as a link — resolving first would make it un-deletable,
    since the target it names is out of bounds. And a symlink cannot be used as
    a back door for writes either: callers that put bytes anywhere check
    is_symlink() and refuse, rather than following it out of the tree.
    """
    cleaned = relative.strip().strip("/")
    if not cleaned:
        raise PermissionError("a path is required")
    parent_part, _, name = cleaned.rpartition("/")
    if name in ("", ".", ".."):
        raise PermissionError(f"invalid name: {name!r}")
    parent = safe_path(server_dir, parent_part)
    if not parent.is_dir():
        raise NotADirectoryError(parent_part or "/")
    return parent / name


def relative_to_root(server_dir: pathlib.Path, target: pathlib.Path) -> str:
    return str(target.relative_to(server_dir))


def describe(server_dir: pathlib.Path, child: pathlib.Path) -> dict:
    """One directory entry.

    lstat(), not stat(), so a symlink is reported as itself: a broken one still
    lists instead of vanishing, and the size shown is the link's rather than
    that of whatever it points at.
    """
    stat = child.lstat()
    return {
        "name": child.name,
        "path": relative_to_root(server_dir, child),
        "dir": child.is_dir(),
        "link": child.is_symlink(),
        "size": stat.st_size,
        "modified": stat.st_mtime,
    }


def list_files(server_dir: pathlib.Path, relative: str) -> dict:
    target = safe_path(server_dir, relative)
    if not target.is_dir():
        raise NotADirectoryError(relative or "/")
    entries = []
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        try:
            entries.append(describe(server_dir, child))
        except OSError:
            continue
    listing: dict[str, Any] = {"path": relative.strip("/"), "entries": entries}
    with contextlib.suppress(OSError):
        usage = shutil.disk_usage(target)
        listing["disk_free"], listing["disk_total"] = usage.free, usage.total
    return listing


def looks_binary(blob: bytes) -> bool:
    return b"\0" in blob


def read_file(server_dir: pathlib.Path, relative: str) -> dict:
    """Read a text file for the editor.

    Binary content is refused rather than mangled. errors="replace" would hand
    back a jar as question marks, and saving that back through write_file()
    would then overwrite the real bytes with the mangled ones — so anything with
    a NUL in it is sent to the download path instead.
    """
    target = safe_path(server_dir, relative)
    if not target.is_file():
        raise FileNotFoundError(relative)
    size = target.stat().st_size
    if size > MAX_READ_BYTES:
        raise ValueError(f"file is {size} bytes; only text files under 256 KB can be viewed")
    blob = target.read_bytes()
    if looks_binary(blob[:8192]):
        raise ValueError("this is a binary file — download it instead of opening it")
    return {"path": relative, "content": blob.decode(errors="replace"), "size": size}


def stat_file(server_dir: pathlib.Path, relative: str) -> dict:
    target = safe_path(server_dir, relative)
    if not target.is_file():
        raise FileNotFoundError(relative)
    stat = target.stat()
    return {"path": relative, "name": target.name, "size": stat.st_size, "modified": stat.st_mtime}


def read_chunk(server_dir: pathlib.Path, relative: str, offset: Any, length: Any) -> dict:
    """One slice of a file, base64'd, for the download stream.

    Downloads are pulled a chunk at a time rather than in one message so that a
    600 MB world archive doesn't have to fit in a single WebSocket frame — or in
    the memory of either end.
    """
    target = safe_path(server_dir, relative)
    if not target.is_file():
        raise FileNotFoundError(relative)
    span = min(max(int(length or 0), 0), CHUNK_BYTES)
    with target.open("rb") as handle:
        handle.seek(max(int(offset or 0), 0))
        blob = handle.read(span)
    return {"offset": int(offset or 0), "bytes": len(blob), "data": base64.b64encode(blob).decode()}


def replace_atomically(target: pathlib.Path, temporary: pathlib.Path) -> None:
    """Move `temporary` onto `target`, keeping the mode `target` already had.

    A rename is atomic, so a reader — the Minecraft server itself, most of the
    time — sees either the old file or the new one, never a half-written one.
    The mode is carried across because the temp file is created with the default
    0644: without this, saving server.properties would quietly widen it.
    """
    if target.exists():
        shutil.copymode(target, temporary)
    os.replace(temporary, target)


def write_file(server_dir: pathlib.Path, relative: str, content: str, overwrite: bool) -> dict:
    target = safe_leaf(server_dir, relative)
    if target.is_symlink():
        raise PermissionError("refusing to write through a symlink")
    if target.is_dir():
        raise IsADirectoryError(relative)
    if target.exists() and not overwrite:
        raise FileExistsError(f"{relative} already exists")
    blob = content.encode()
    if len(blob) > MAX_WRITE_BYTES:
        raise ValueError(f"content is {len(blob)} bytes; the editor tops out at {MAX_WRITE_BYTES}")

    temporary = target.with_name(f".{target.name}{WRITE_SUFFIX}")
    temporary.write_bytes(blob)
    try:
        replace_atomically(target, temporary)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return {"path": relative_to_root(server_dir, target), "size": len(blob)}


def make_directory(server_dir: pathlib.Path, relative: str) -> dict:
    target = safe_leaf(server_dir, relative)
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"{relative} already exists")
    target.mkdir(parents=True)
    return {"path": relative_to_root(server_dir, target)}


def member_target(destination: pathlib.Path, name: str) -> pathlib.Path:
    """Where one archive member is allowed to land.

    Member names come from whoever built the zip, not from the dashboard, so
    they are the one path in this file that no amount of checking upstream can
    vouch for. `../../server.properties` and `/etc/cron.d/x` are both legal
    strings in the format, and both are refused here rather than normalised:
    silently rewriting an escape to something harmless would extract an archive
    that is lying about its contents and report success.
    """
    cleaned = name.replace("\\", "/")
    if cleaned.startswith("/"):
        raise PermissionError(f"archive member has an absolute path: {name}")
    parts = [part for part in cleaned.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise PermissionError(f"archive member escapes the destination: {name}")
    if not parts:
        raise PermissionError(f"archive member has no name: {name}")

    target = destination.joinpath(*parts)
    if destination not in target.parents:
        raise PermissionError(f"archive member escapes the destination: {name}")
    return target


def extract_mkdir(destination: pathlib.Path, target: pathlib.Path) -> None:
    """Create the levels of `target` under `destination`, following no symlink.

    Checked one level at a time rather than with parents=True, because a symlink
    already sitting inside the destination would otherwise be followed out of
    the tree by the mkdir itself.
    """
    walked = destination
    for part in target.relative_to(destination).parts:
        walked = walked / part
        if walked.is_symlink():
            raise PermissionError(f"refusing to extract through a symlink: {part}")
        walked.mkdir(exist_ok=True)


def extract_archive(
    server_dir: pathlib.Path, relative: str, destination_relative: str, limit_bytes: int
) -> dict:
    """Unpack a .zip into a directory inside the tree.

    Three things an archive can do that a plain upload cannot, all refused:
    point a member outside the destination (see member_target), carry a symlink
    that would later be written through, and declare a modest compressed size
    that expands to fill the disk. The last is why the running total is checked
    against the budget as bytes are copied rather than trusting the sizes in the
    central directory, which the archive itself supplies.
    """
    archive = safe_path(server_dir, relative)
    if not archive.is_file():
        raise FileNotFoundError(relative)
    if archive.suffix.lower() != ".zip":
        raise ValueError(f"{archive.name} is not a .zip file")

    destination = safe_leaf(server_dir, destination_relative)
    if destination.is_symlink():
        raise PermissionError("refusing to extract through a symlink")
    if destination.exists() and not destination.is_dir():
        raise NotADirectoryError(destination_relative)

    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if len(members) > MAX_ARCHIVE_ENTRIES:
            raise ValueError(
                f"archive holds {len(members)} entries; this agent extracts at most "
                f"{MAX_ARCHIVE_ENTRIES}"
            )
        for member in members:
            if (member.external_attr >> 16) & 0o170000 == SYMLINK_MODE:
                raise PermissionError(f"archive contains a symlink: {member.filename}")

        declared = sum(member.file_size for member in members)
        if declared > limit_bytes:
            raise ValueError(
                f"archive expands to {declared} bytes; this agent accepts at most {limit_bytes}"
            )
        free = shutil.disk_usage(destination.parent).free
        if declared + FREE_SPACE_MARGIN > free:
            raise OSError(f"not enough room: {free} bytes free, {declared} needed")

        destination.mkdir(exist_ok=True)
        files = 0
        written = 0
        for member in members:
            target = member_target(destination, member.filename)
            if member.is_dir():
                extract_mkdir(destination, target)
                continue
            extract_mkdir(destination, target.parent)
            if target.is_symlink():
                raise PermissionError(f"refusing to overwrite a symlink: {member.filename}")
            with bundle.open(member) as source, target.open("wb") as sink:
                while blob := source.read(CHUNK_BYTES):
                    written += len(blob)
                    if written > limit_bytes:
                        raise ValueError(f"archive expands past the {limit_bytes} byte limit")
                    sink.write(blob)
            files += 1

    return {
        "path": relative_to_root(server_dir, destination),
        "files": files,
        "size": written,
    }


def move_path(server_dir: pathlib.Path, relative: str, destination: str) -> dict:
    """Rename or move one entry, both ends confined to the tree.

    Refuses an existing destination outright. shutil.move() would otherwise
    treat a directory destination as "move inside it", which turns a mistyped
    rename into a silent reorganisation.
    """
    source = safe_leaf(server_dir, relative)
    if not source.exists() and not source.is_symlink():
        raise FileNotFoundError(relative)
    target = safe_leaf(server_dir, destination)
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"{destination} already exists")
    if target == source:
        return {"path": relative_to_root(server_dir, target), "from": relative}
    shutil.move(str(source), str(target))
    return {"path": relative_to_root(server_dir, target), "from": relative}


def delete_paths(server_dir: pathlib.Path, relatives: Any) -> dict:
    """Remove each path, reporting per-entry outcomes rather than stopping.

    A multi-select delete where the third of five fails should still tell the
    operator which four went, so each entry is caught on its own. safe_leaf()
    rejects an empty path, which is what keeps server_dir itself from being the
    thing that gets removed.
    """
    removed: list[str] = []
    failed: list[dict] = []
    for relative in relatives if isinstance(relatives, (list, tuple)) else []:
        try:
            target = safe_leaf(server_dir, str(relative))
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
            else:
                raise FileNotFoundError(relative)
            removed.append(str(relative))
        except Exception as exc:
            failed.append({"path": str(relative), "error": f"{type(exc).__name__}: {exc}"})
    return {"removed": removed, "failed": failed}


class UploadStore:
    """Uploads in flight, each streaming into a temp file beside its target.

    The bytes land next to the final path rather than in /tmp for two reasons:
    the commit is then a rename on the same filesystem, so it is atomic and
    cheap even for a 500 MB archive, and a partial upload never appears under
    the real name where the server might try to load it.

    An upload the dashboard never finishes — browser closed, tunnel dropped —
    would otherwise leave its temp file and an open handle behind forever, so
    anything idle past UPLOAD_IDLE_SECONDS is swept when the next one starts.
    """

    def __init__(self, limit_bytes: int) -> None:
        self.limit = limit_bytes
        self.sessions: dict[str, dict[str, Any]] = {}

    def begin(self, server_dir: pathlib.Path, relative: str, declared: Any, overwrite: bool) -> dict:
        self.sweep()
        target = safe_leaf(server_dir, relative)
        if target.is_symlink():
            raise PermissionError("refusing to write through a symlink")
        if target.is_dir():
            raise IsADirectoryError(relative)
        if target.exists() and not overwrite:
            raise FileExistsError(f"{target.name} already exists here")

        size = max(int(declared or 0), 0)
        if size > self.limit:
            raise ValueError(f"upload is {size} bytes; this agent accepts at most {self.limit}")
        free = shutil.disk_usage(target.parent).free
        if size and size + FREE_SPACE_MARGIN > free:
            raise OSError(f"not enough room: {free} bytes free, {size} needed")

        upload_id = secrets.token_hex(8)
        temporary = target.with_name(f".{target.name}.{upload_id}{UPLOAD_SUFFIX}")
        handle: BinaryIO = temporary.open("wb")
        self.sessions[upload_id] = {
            "handle": handle,
            "temporary": temporary,
            "target": target,
            "path": relative_to_root(server_dir, target),
            "written": 0,
            "touched": time.time(),
        }
        return {"upload": upload_id, "path": relative_to_root(server_dir, target)}

    def _session(self, upload_id: Any) -> dict[str, Any]:
        session = self.sessions.get(str(upload_id))
        if session is None:
            raise KeyError(f"unknown or expired upload: {upload_id}")
        session["touched"] = time.time()
        return session

    def chunk(self, upload_id: Any, data: Any) -> dict:
        session = self._session(upload_id)
        blob = base64.b64decode(data or "")
        if session["written"] + len(blob) > self.limit:
            self.abort(upload_id)
            raise ValueError(f"upload exceeds the {self.limit} byte limit")
        session["handle"].write(blob)
        session["written"] += len(blob)
        return {"received": session["written"]}

    def commit(self, upload_id: Any) -> dict:
        session = self._session(upload_id)
        handle = session["handle"]
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        try:
            replace_atomically(session["target"], session["temporary"])
        except OSError:
            session["temporary"].unlink(missing_ok=True)
            raise
        finally:
            self.sessions.pop(str(upload_id), None)
        return {"path": session["path"], "size": session["written"]}

    def abort(self, upload_id: Any) -> dict:
        session = self.sessions.pop(str(upload_id), None)
        if session is None:
            return {"aborted": False}
        with contextlib.suppress(Exception):
            session["handle"].close()
        with contextlib.suppress(OSError):
            session["temporary"].unlink(missing_ok=True)
        return {"aborted": True, "path": session["path"]}

    def sweep(self) -> None:
        now = time.time()
        for upload_id in [
            key for key, session in self.sessions.items()
            if now - session["touched"] > UPLOAD_IDLE_SECONDS
        ]:
            self.abort(upload_id)

    def abort_all(self) -> None:
        for upload_id in list(self.sessions):
            self.abort(upload_id)


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



MUTATING_ACTIONS = frozenset({
    "write_file", "make_directory", "move_path", "delete_paths",
    "upload_begin", "upload_chunk", "upload_commit", "upload_abort",
    "extract_archive",
})


async def handle_command(
    message: dict, controller: Controller, config: dict, uploads: UploadStore
) -> dict:
    action = message.get("action")
    server_dir = controller.server_dir
    loop = asyncio.get_running_loop()

    if action in MUTATING_ACTIONS and not writes_allowed(config):
        return {
            "t": "result",
            "id": message.get("id"),
            "ok": False,
            "error": "this agent is read-only — set `allow_writes = yes` in agent.conf to change files",
        }

    def blocking() -> dict:
        if action == "power":
            value = str(message.get("value") or "")
            handler = {
                "start": controller.start,
                "stop": controller.stop,
                "restart": controller.restart,
            }.get(value)
            if handler is None:
                return {"ok": False, "detail": f"unknown power action: {value!r}"}
            ok, detail = handler()
            return {"ok": ok, "detail": detail, "state": controller.state()}
        if action == "list_files":
            return {"ok": True, **list_files(server_dir, message.get("path", ""))}
        if action == "read_file":
            return {"ok": True, **read_file(server_dir, message.get("path", ""))}
        if action == "stat_file":
            return {"ok": True, **stat_file(server_dir, message.get("path", ""))}
        if action == "read_chunk":
            return {"ok": True, **read_chunk(
                server_dir, message.get("path", ""), message.get("offset"), message.get("length")
            )}
        if action == "write_file":
            return {"ok": True, **write_file(
                server_dir,
                message.get("path", ""),
                str(message.get("content") or ""),
                bool(message.get("overwrite", True)),
            )}
        if action == "make_directory":
            return {"ok": True, **make_directory(server_dir, message.get("path", ""))}
        if action == "move_path":
            return {"ok": True, **move_path(
                server_dir, message.get("path", ""), message.get("to", "")
            )}
        if action == "extract_archive":
            return {"ok": True, **extract_archive(
                server_dir, message.get("path", ""), message.get("to", ""), uploads.limit
            )}
        if action == "delete_paths":
            return {"ok": True, **delete_paths(server_dir, message.get("paths"))}
        if action == "upload_begin":
            return {"ok": True, **uploads.begin(
                server_dir,
                message.get("path", ""),
                message.get("size"),
                bool(message.get("overwrite", False)),
            )}
        if action == "upload_chunk":
            return {"ok": True, **uploads.chunk(message.get("upload"), message.get("data"))}
        if action == "upload_commit":
            return {"ok": True, **uploads.commit(message.get("upload"))}
        if action == "upload_abort":
            return {"ok": True, **uploads.abort(message.get("upload"))}
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


async def session(url: str, controller: Controller, config: dict, uploads: UploadStore) -> None:
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
                    reply = await handle_command(message, controller, config, uploads)
                    await ws.send(json.dumps(reply))

        try:
            done, pending = await asyncio.wait(
                [asyncio.create_task(pump_logs()), asyncio.create_task(pump_commands())],
                return_when=asyncio.FIRST_EXCEPTION,
            )
        finally:
            uploads.abort_all()
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
    uploads = UploadStore(upload_limit(config))
    print(f"[agent] server_dir={controller.server_dir} mode={controller.mode}", flush=True)
    print(
        "[agent] files: read/write, uploads up to "
        f"{uploads.limit // (1024 * 1024)} MB"
        if writes_allowed(config)
        else "[agent] files: read-only (set `allow_writes = yes` in agent.conf to allow changes)",
        flush=True,
    )

    separator = "&" if "?" in config["dashboard_url"] else "?"
    url = f"{config['dashboard_url']}{separator}token={config['token']}"

    backoff = 1
    while True:
        try:
            await session(url, controller, config, uploads)
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
