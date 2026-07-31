"""Minecraft Server List Ping.

This is the protocol your own client uses to draw the server entry in the
multiplayer menu, so it works against any vanilla/Paper/Spigot server with no
cooperation from the server owner at all. That makes it our Tier 0 source.
"""

import asyncio
import json
import struct
import time
from typing import Any

# Servers echo back their own version regardless of what we claim here, so any
# recent protocol number works. 767 == 1.21.
PROTOCOL_VERSION = 767

# A status response is a few KB; the favicon is the only large field. The cap
# stops a hostile host from getting us to allocate on a made-up length prefix.
MAX_STATUS_BYTES = 512 * 1024


def _write_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _write_string(text: str) -> bytes:
    raw = text.encode("utf-8")
    return _write_varint(len(raw)) + raw


async def _read_varint(reader: asyncio.StreamReader) -> int:
    value = 0
    for shift in range(0, 35, 7):
        byte = (await reader.readexactly(1))[0]
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value
    raise ValueError("VarInt exceeded 5 bytes")


def _packet(packet_id: int, payload: bytes) -> bytes:
    body = _write_varint(packet_id) + payload
    return _write_varint(len(body)) + body


def _flatten_motd(desc: Any) -> str:
    """The MOTD may be a plain string, a chat component, or a nested tree."""
    if isinstance(desc, str):
        return desc
    if isinstance(desc, list):
        return "".join(_flatten_motd(part) for part in desc)
    if isinstance(desc, dict):
        text = desc.get("text", "")
        text += "".join(_flatten_motd(part) for part in desc.get("extra", []))
        return text
    return ""


def _strip_formatting(text: str) -> str:
    """Remove legacy section-sign colour codes so the MOTD renders cleanly."""
    out = []
    skip = False
    for char in text:
        if skip:
            skip = False
            continue
        if char == "§":
            skip = True
            continue
        out.append(char)
    return "".join(out)


async def ping(host: str, port: int, timeout: float = 5.0) -> dict[str, Any]:
    """Return a normalised status dict. Never raises; reports offline instead."""
    started = time.perf_counter()
    writer = None
    try:
        # One deadline over the whole exchange. Timing each read separately
        # leaves a host that accepts the connection and then dribbles bytes
        # (tarpits, misconfigured firewalls) able to stall the poll loop forever.
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection(host, port)

            handshake = (
                _write_varint(PROTOCOL_VERSION)
                + _write_string(host)
                + struct.pack(">H", port)
                + _write_varint(1)  # next state: status
            )
            writer.write(_packet(0x00, handshake))
            writer.write(_packet(0x00, b""))  # status request
            await writer.drain()

            await _read_varint(reader)  # frame length
            packet_id = await _read_varint(reader)
            if packet_id != 0x00:
                raise ValueError(f"unexpected packet id {packet_id}")
            length = await _read_varint(reader)
            if not 0 < length <= MAX_STATUS_BYTES:
                raise ValueError(f"implausible status length {length}")
            raw = await reader.readexactly(length)

        latency_ms = round((time.perf_counter() - started) * 1000, 1)

        data = json.loads(raw.decode("utf-8"))
        players = data.get("players") or {}
        version = data.get("version") or {}
        sample = [p.get("name", "?") for p in (players.get("sample") or [])]

        return {
            "online": True,
            "host": host,
            "port": port,
            "motd": _strip_formatting(_flatten_motd(data.get("description", ""))).strip(),
            "version": version.get("name", "unknown"),
            "protocol": version.get("protocol"),
            "players_online": players.get("online", 0),
            "players_max": players.get("max", 0),
            # Vanilla caps this sample at 12 names and servers may disable it.
            # Tier 1 (`/list` over RCON) gives the authoritative roster.
            "players_sample": sample,
            "favicon": data.get("favicon"),
            "latency_ms": latency_ms,
            "error": None,
        }
    except Exception as exc:  # offline is a normal state, not an exception path
        return {
            "online": False,
            "host": host,
            "port": port,
            "motd": "",
            "version": None,
            "protocol": None,
            "players_online": 0,
            "players_max": 0,
            "players_sample": [],
            "favicon": None,
            "latency_ms": None,
            "error": f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__,
        }
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
