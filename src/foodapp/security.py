"""Passwords, request limits, and header policy."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from typing import TYPE_CHECKING

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

if TYPE_CHECKING:
    import sqlite3

    from starlette.requests import Request

    from foodapp.config import Settings

_HASHER = PasswordHasher(time_cost=2, memory_cost=19_456, parallelism=1)
# Hashed once so a missing account still pays the same verification cost.
DUMMY_PASSWORD_HASH = _HASHER.hash(secrets.token_urlsafe(32))

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_COMMON_PASSWORDS = frozenset(
    {
        "password1234",
        "password12345",
        "qwertyuiop12",
        "123456789012",
        "1234567890123",
        "letmeinplease",
        "changeme1234",
        "adminadmin12",
        "welcome12345",
        "iloveyou1234",
        "foodapp12345",
    }
)
_POLICY_MESSAGE = "Use 12 to 128 characters, and don't use a common password."
_LOOPBACK_PEERS = frozenset({"127.0.0.1", "::1", "testclient"})


def token_hash(raw_token: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), raw_token.encode("utf-8"), hashlib.sha256).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def tokens_match(expected: str, provided: object) -> bool:
    if not isinstance(expected, str) or not isinstance(provided, str):
        return False
    if not expected or len(expected) != len(provided):
        return False
    return hmac.compare_digest(expected, provided)


def verify_password(password: str, encoded_hash: str) -> bool:
    try:
        return bool(_HASHER.verify(encoded_hash, password))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def hash_password(password: str) -> str:
    return _HASHER.hash(password)


def normalize_email(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    email = value.strip().lower()
    if len(email) > 254 or not _EMAIL_RE.fullmatch(email):
        return None
    return email


def clean_person_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    name = " ".join(value.split())
    if not 1 <= len(name) <= 60:
        return None
    if any(ord(char) < 32 or char in '<>"`\\/{}' for char in name):
        return None
    if not any(char.isalpha() for char in name):
        return None
    return name


def password_problem(password: object, email: str) -> str | None:
    if not isinstance(password, str) or "\x00" in password:
        return _POLICY_MESSAGE
    if not 12 <= len(password) <= 128:
        return _POLICY_MESSAGE
    lowered = password.casefold()
    local = email.split("@", 1)[0].casefold()
    if lowered in _COMMON_PASSWORDS or lowered == email.casefold() or (len(local) >= 4 and lowered == local):
        return _POLICY_MESSAGE
    return None


def clean_note(value: object) -> str:
    if not isinstance(value, str) or "\x00" in value:
        return ""
    kept = [char for char in value if char in "\n\t" or (" " <= char and char != "\x7f")]
    return "".join(kept).strip()[:280]


def parse_item_id(value: object) -> int | None:
    if not isinstance(value, str) or not value.isdigit() or not 1 <= len(value) <= 9:
        return None
    item_id = int(value)
    if item_id <= 0:
        return None
    return item_id


def parse_qty(value: object) -> int | None:
    if not isinstance(value, str) or not value.isdigit() or len(value) > 2:
        return None
    qty = int(value)
    if 1 <= qty <= 20:
        return qty
    return None


def clean_search(value: object) -> str:
    if not isinstance(value, str):
        return ""
    kept = [char for char in value if char >= " " and char != "\x7f"]
    return " ".join("".join(kept).split())[:80]


def like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def safe_next(value: object, default: str = "/") -> str:
    """Allow only same-site paths we actually serve."""
    if not isinstance(value, str):
        return default
    candidate = value.strip()
    if not candidate or len(candidate) > 200:
        return default
    if not candidate.startswith("/") or candidate.startswith("//") or candidate.startswith("/\\"):
        return default
    if any(char in candidate for char in ("\\", "\n", "\r", "\x00")):
        return default
    path, _, query = candidate.partition("?")
    allowed = {"/", "/cart", "/checkout", "/orders", "/account"}
    if path not in allowed and not re.fullmatch(r"/orders/[0-9]+", path):
        return default
    if query and not re.fullmatch(r"[A-Za-z0-9=&%\-._~]*", query):
        return path
    return candidate


def client_ip(request: Request, settings: Settings) -> str:
    """Use the socket peer unless a trusted proxy appended X-Forwarded-For."""
    peer = _safe_token(request.client.host if request.client else "unknown")
    if not settings.trust_proxy or peer not in _LOOPBACK_PEERS:
        return peer
    forwarded = request.headers.get("x-forwarded-for", "")
    parts = [_safe_token(part.strip()) for part in forwarded.split(",") if part.strip()]
    if not parts:
        return peer
    # A proxy that appends puts the client it saw in the last hop.
    return parts[-1]


def _safe_token(value: str) -> str:
    cleaned = "".join(char for char in value if char.isalnum() or char in ".:")
    return cleaned[:64] or "unknown"


def consume_rate_limit(
    conn: sqlite3.Connection,
    bucket: str,
    limit: int,
    window_seconds: int,
) -> bool:
    """Return True when this attempt is still inside the window."""
    now = int(time.time())
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT window_start, count FROM rate_limits WHERE bucket = ?",
            (bucket,),
        ).fetchone()
        if row is None or now - int(row["window_start"]) >= window_seconds:
            conn.execute(
                """
                INSERT INTO rate_limits (bucket, window_start, count)
                VALUES (?, ?, 1)
                ON CONFLICT(bucket) DO UPDATE SET
                    window_start = excluded.window_start,
                    count = 1
                """,
                (bucket, now),
            )
            conn.execute("COMMIT")
            return True
        if int(row["count"]) >= limit:
            conn.execute("COMMIT")
            return False
        conn.execute("UPDATE rate_limits SET count = count + 1 WHERE bucket = ?", (bucket,))
        conn.execute("COMMIT")
        return True
    except Exception:
        conn.execute("ROLLBACK")
        raise


def security_headers(settings: Settings, path: str) -> list[tuple[bytes, bytes]]:
    policy = (
        "default-src 'self'; "
        "script-src 'none'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "object-src 'none'"
    )
    if settings.cookie_secure:
        policy += "; upgrade-insecure-requests"
    headers = [
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"strict-origin-when-cross-origin"),
        (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
        (b"content-security-policy", policy.encode("ascii")),
        (b"cross-origin-opener-policy", b"same-origin"),
        (b"cross-origin-resource-policy", b"same-origin"),
        (b"x-permitted-cross-domain-policies", b"none"),
    ]
    if settings.cookie_secure:
        headers.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
    if path.startswith("/static/"):
        headers.append((b"cache-control", b"public, max-age=3600"))
    else:
        headers.append((b"cache-control", b"no-store"))
    return headers
