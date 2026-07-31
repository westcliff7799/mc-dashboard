"""Password hashing, signed sessions, and login throttling.

This dashboard is exposed to the public internet and can control someone
else's machine, so the login path gets more care than a hobby project usually
warrants: scrypt for the password, constant-time comparisons everywhere, and a
per-IP lockout so the password can't be ground down by brute force.
"""

import base64
import hashlib
import hmac
import secrets
import time

from .config import settings

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
COOKIE_NAME = "mcdash_session"

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


def _sign(message: str) -> str:
    signature = hmac.new(settings.secret_key.encode(), message.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(signature).decode().rstrip("=")


def issue_session(username: str) -> str:
    """Stateless signed token, so a dashboard restart doesn't log you out."""
    expires = int(time.time()) + settings.session_hours * 3600
    message = f"{username}:{expires}"
    return f"{message}:{_sign(message)}"


def validate_session(token: str | None) -> str | None:
    if not token:
        return None
    try:
        username, expires_raw, signature = token.rsplit(":", 2)
    except ValueError:
        return None
    if not hmac.compare_digest(signature, _sign(f"{username}:{expires_raw}")):
        return None
    try:
        if int(expires_raw) < time.time():
            return None
    except ValueError:
        return None
    return username


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


def check_credentials(username: str, password: str) -> bool:
    if not settings.admin_password_hash:
        return False
    # Compare both fields even when the username is wrong, so response timing
    # doesn't reveal whether the username exists.
    user_ok = hmac.compare_digest(username, settings.admin_user)
    pass_ok = verify_password(password, settings.admin_password_hash)
    return user_ok and pass_ok
