"""Source RCON client (Tier 1).

Connections are made per-command rather than held open. Minecraft's RCON
implementation drops idle sockets without warning, and a dashboard issues
commands rarely enough that reconnecting costs nothing.
"""

import asyncio
import struct

AUTH = 3
EXEC = 2
RESPONSE = 0

_lock = asyncio.Lock()


class RconError(Exception):
    pass


def _encode(request_id: int, packet_type: int, body: str) -> bytes:
    payload = struct.pack("<ii", request_id, packet_type) + body.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


async def _read_packet(reader: asyncio.StreamReader) -> tuple[int, int, str]:
    header = await reader.readexactly(4)
    (length,) = struct.unpack("<i", header)
    if not 10 <= length <= 4096 + 16:
        raise RconError(f"implausible packet length {length}")
    payload = await reader.readexactly(length)
    request_id, packet_type = struct.unpack("<ii", payload[:8])
    body = payload[8:-2].decode("utf-8", errors="replace")
    return request_id, packet_type, body


async def execute(host: str, port: int, password: str, command: str, timeout: float = 8.0) -> str:
    """Authenticate, run one command, return its output."""
    if not password:
        raise RconError("RCON is not configured")

    # Minecraft's RCON server is single-threaded; overlapping commands from the
    # dashboard's own clients would interleave badly.
    async with _lock:
        return await asyncio.wait_for(
            _execute_locked(host, port, password, command), timeout=timeout
        )


async def _execute_locked(host: str, port: int, password: str, command: str) -> str:
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(_encode(1, AUTH, password))
        await writer.drain()

        # Some servers emit a blank RESPONSE_VALUE before the auth verdict.
        request_id, packet_type, _ = await _read_packet(reader)
        if packet_type == RESPONSE:
            request_id, packet_type, _ = await _read_packet(reader)
        if request_id == -1:
            raise RconError("RCON authentication failed — wrong password")

        # Long output (e.g. /help) arrives split across packets with no length
        # marker. Trailing an empty command lets us detect the true end: the
        # sentinel's reply cannot arrive before the real command's last packet.
        writer.write(_encode(2, EXEC, command))
        writer.write(_encode(3, EXEC, ""))
        await writer.drain()

        chunks: list[str] = []
        while True:
            request_id, _, body = await _read_packet(reader)
            if request_id == 3:
                break
            chunks.append(body)
        return "".join(chunks).strip()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


def parse_player_list(output: str) -> tuple[int, int, list[str]]:
    """Parse `/list` output.

    Vanilla: "There are 3 of a max of 20 players online: alice, bob, carol"
    Some forks vary the wording, so we anchor on the colon and the digits.
    """
    online = maximum = 0
    digits = [int(tok) for tok in output.replace(",", " ").split() if tok.isdigit()]
    if len(digits) >= 2:
        online, maximum = digits[0], digits[1]
    names: list[str] = []
    if ":" in output:
        tail = output.split(":", 1)[1]
        names = [n.strip() for n in tail.split(",") if n.strip()]
    return online, maximum, names
