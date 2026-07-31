"""Password hashing, signed sessions, and login throttling.

This dashboard is exposed to the public internet and can control someone
else's machine, so the login path gets more care than a hobby project usually
warrants: scrypt for the password, constant-time comparisons everywhere, and a
per-IP lockout so the password can't be ground down by brute force.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from typing import Any

from . import permissions
from .config import ROOT, settings

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
COOKIE_NAME = "mcdash_session"

# Two roles still, but they now mean "who you are", not "what you may do".
# ROLE_ADMIN is the .env owner and always holds every permission; every account
# in users.json is ROLE_GUEST and holds exactly what it has been granted.
ROLE_ADMIN = "admin"
ROLE_GUEST = "guest"

# Extra accounts added from the debug panel. The owner's account stays in .env,
# so no combination of grants made here can produce a second owner.
USERS_FILE = ROOT / "users.json"
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{2,32}$")
MIN_PASSWORD_LENGTH = 12

MAX_ATTEMPTS = 8
LOCKOUT_SECONDS = 900
_attempts: dict[str, list[float]] = {}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32
    )
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt_hex),
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            dklen=32,
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, AttributeError):
        return False


def constant_time_equal(left: str, right: str) -> bool:
    """Compare two attacker-supplied strings without leaking timing.

    `hmac.compare_digest` raises TypeError on str with non-ASCII characters, and
    every caller here feeds it something off the wire — a cookie, a form field, a
    query param. Comparing the encoded bytes keeps a stray "é" from turning into
    an unhandled 500.
    """
    try:
        return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
    except UnicodeEncodeError:
        return False  # unencodable input cannot equal a value we produced


def _sign(message: str) -> str:
    signature = hmac.new(settings.secret_key.encode(), message.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(signature).decode().rstrip("=")


def issue_session(username: str, role: str) -> str:
    """Stateless signed token, so a dashboard restart doesn't log you out.

    The role is inside the signed message, not alongside it. A guest editing
    their own cookie to say "admin" changes the message, so the signature stops
    matching and the session is rejected outright.
    """
    expires = int(time.time()) + settings.session_hours * 3600
    message = f"{username}:{role}:{expires}"
    return f"{message}:{_sign(message)}"


def validate_session(token: str | None) -> tuple[str, str] | None:
    """Return (username, role), or None if the token is bad or expired."""
    if not token:
        return None
    try:
        username, role, expires_raw, signature = token.rsplit(":", 3)
    except ValueError:
        return None
    if not constant_time_equal(signature, _sign(f"{username}:{role}:{expires_raw}")):
        return None
    if role not in (ROLE_ADMIN, ROLE_GUEST):
        return None
    try:
        if int(expires_raw) < time.time():
            return None
    except ValueError:
        return None
    return username, role


def throttle_check(client_ip: str) -> int:
    """Return seconds remaining in a lockout, or 0 if the IP may try again."""
    now = time.time()
    recent = [t for t in _attempts.get(client_ip, []) if now - t < LOCKOUT_SECONDS]
    _attempts[client_ip] = recent
    if len(recent) >= MAX_ATTEMPTS:
        return int(LOCKOUT_SECONDS - (now - recent[0]))
    return 0


def record_failure(client_ip: str) -> None:
    _attempts.setdefault(client_ip, []).append(time.time())


def clear_failures(client_ip: str) -> None:
    _attempts.pop(client_ip, None)


_cache_stamp: tuple[int, int] | None = None
_cache_users: list[dict[str, Any]] = []


def _stamp() -> tuple[int, int] | None:
    """Cheap identity for the current file contents, or None if unreadable."""
    try:
        stat = USERS_FILE.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def load_users() -> list[dict[str, Any]]:
    """Read the added-user store. Never raises — a corrupt or missing file
    must not lock the owner out of their own dashboard.

    Cached against the file's mtime and size, because authorization now reads
    this on every request and on every console line broadcast. save_users()
    replaces the file, which moves the stamp, so a write invalidates the cache
    without needing to remember to clear it.
    """
    global _cache_stamp, _cache_users

    stamp = _stamp()
    if stamp is not None and stamp == _cache_stamp:
        return [dict(entry) for entry in _cache_users]

    try:
        data = json.loads(USERS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        _cache_stamp, _cache_users = stamp, []
        return []

    entries = data.get("users") if isinstance(data, dict) else None
    parsed: list[dict[str, Any]] = []
    for entry in entries or []:
        if not (isinstance(entry, dict) and entry.get("username") and entry.get("password_hash")):
            continue
        normalized = dict(entry)
        # Accounts written before permissions existed carry `role: guest` and
        # nothing else; they come back with an empty grant, which is exactly
        # what "read-only guest" already meant.
        normalized["permissions"] = permissions.sanitize(entry.get("permissions"))
        parsed.append(normalized)

    _cache_stamp, _cache_users = stamp, parsed
    return [dict(entry) for entry in parsed]


def permissions_for(username: str, role: str) -> frozenset[str]:
    """What this account may actually do, right now.

    Deliberately read from storage rather than from the session token. Tokens
    live for SESSION_HOURS (a week by default), so a token-embedded grant would
    make revocation take up to that long to bite. Looking it up per request
    means un-ticking a box logs the change in immediately.
    """
    if role == ROLE_ADMIN:
        return permissions.ALL
    for entry in load_users():
        if entry["username"] == username:
            return frozenset(entry["permissions"])
    return frozenset()


def save_users(users: list[dict[str, Any]]) -> None:
    """Write via a temp file and rename, so a crash mid-write can't leave a
    truncated store that would lock every added user out."""
    temporary = USERS_FILE.with_name(USERS_FILE.name + ".tmp")
    temporary.write_text(json.dumps({"users": users}, indent=2))
    temporary.chmod(0o600)  # password hashes: same treatment as .env
    os.replace(temporary, USERS_FILE)


def add_user(username: str, password: str, granted: Any = None) -> None:
    """Add an account. Raises ValueError with a message fit to show.

    `granted` is passed through sanitize(), so an unknown permission string is
    dropped rather than stored.
    """
    username = username.strip()
    if not USERNAME_PATTERN.match(username):
        raise ValueError("Username must be 2-32 characters: letters, digits, dot, dash, underscore.")
    if username.lower() == settings.admin_user.lower():
        raise ValueError("That name belongs to the admin account.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")

    users = load_users()
    if any(existing["username"].lower() == username.lower() for existing in users):
        raise ValueError("That user already exists.")
    users.append(
        {
            "username": username,
            "role": ROLE_GUEST,
            "permissions": permissions.sanitize(granted),
            "password_hash": hash_password(password),
        }
    )
    save_users(users)


def set_user_permissions(username: str, granted: Any) -> bool:
    """Replace one account's grant. False if there is no such account.

    Matching is exact, unlike the duplicate check in add_user() — the name
    comes from a panel that was populated by load_users(), so it is already in
    the stored casing.
    """
    users = load_users()
    for entry in users:
        if entry["username"] == username:
            entry["permissions"] = permissions.sanitize(granted)
            save_users(users)
            return True
    return False


def delete_user(username: str) -> bool:
    users = load_users()
    remaining = [entry for entry in users if entry["username"] != username]
    if len(remaining) == len(users):
        return False
    save_users(remaining)
    return True


_dummy_hash: str | None = None


def check_credentials(username: str, password: str) -> str | None:
    """Return the role this username/password pair grants, or None.

    Exactly one scrypt verification runs per attempt, whether or not the
    username exists — an unknown name is checked against a throwaway hash. That
    keeps the response time flat (so it doesn't answer "does this account
    exist?") without re-hashing once per account, which would make login slower
    with every user added.
    """
    global _dummy_hash

    accounts: dict[str, tuple[str, str]] = {}
    if settings.admin_password_hash:
        accounts[settings.admin_user] = (settings.admin_password_hash, ROLE_ADMIN)
    for entry in load_users():
        # setdefault: the .env admin always wins a name collision.
        accounts.setdefault(entry["username"], (entry["password_hash"], ROLE_GUEST))

    found = accounts.get(username)
    if found is None:
        if _dummy_hash is None:
            _dummy_hash = hash_password(secrets.token_urlsafe(32))
        stored_hash, role = _dummy_hash, None
    else:
        stored_hash, role = found

    password_ok = verify_password(password, stored_hash)
    return role if (password_ok and role) else None
