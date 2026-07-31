"""Configuration, loaded from environment with a .env fallback."""

import os
import pathlib
import secrets
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_dotenv(path: pathlib.Path | None = None) -> None:
    """Minimal .env loader so we don't pull in a dependency for 10 lines."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    # --- The Minecraft server we are watching (Tier 0) ---
    mc_host: str = field(default_factory=lambda: os.environ.get("MC_HOST", "127.0.0.1"))
    mc_port: int = field(default_factory=lambda: _int("MC_PORT", 25565))

    # --- RCON (Tier 1). Blank password disables the whole feature. ---
    rcon_host: str = field(default_factory=lambda: os.environ.get("RCON_HOST", ""))
    rcon_port: int = field(default_factory=lambda: _int("RCON_PORT", 25575))
    rcon_password: str = field(default_factory=lambda: os.environ.get("RCON_PASSWORD", ""))

    # --- Agent (Tier 2). Blank token disables the agent endpoint entirely. ---
    agent_token: str = field(default_factory=lambda: os.environ.get("AGENT_TOKEN", ""))

    # --- Dashboard auth ---
    admin_user: str = field(default_factory=lambda: os.environ.get("ADMIN_USER", "admin"))
    admin_password_hash: str = field(
        default_factory=lambda: os.environ.get("ADMIN_PASSWORD_HASH", "")
    )
    secret_key: str = field(
        default_factory=lambda: os.environ.get("SECRET_KEY", "") or secrets.token_hex(32)
    )
    session_hours: int = field(default_factory=lambda: _int("SESSION_HOURS", 168))
    secure_cookie: bool = field(
        default_factory=lambda: os.environ.get("SECURE_COOKIE", "true").lower() == "true"
    )

    poll_seconds: int = field(default_factory=lambda: _int("POLL_SECONDS", 10))

    @property
    def rcon_enabled(self) -> bool:
        return bool(self.rcon_password)

    @property
    def agent_enabled(self) -> bool:
        return bool(self.agent_token)

    @property
    def effective_rcon_host(self) -> str:
        return self.rcon_host or self.mc_host


load_dotenv()
settings = Settings()
