"""Server-side sessions. The cookie holds a random token; the database stores its hash."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

from foodapp.config import Settings
from foodapp.errors import AppError
from foodapp.security import consume_rate_limit, new_token, token_hash

_PURGE_INTERVAL = 60
_last_purge = 0


@dataclass
class Session:
    token_hash: str
    user_id: int | None
    csrf_token: str
    cart: dict[int, int]
    flash: str | None
    expires_at: int


def parse_cart(raw: str | None) -> dict[int, int]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict) or len(data) > 30:
        return {}
    cart: dict[int, int] = {}
    for key, value in data.items():
        if not str(key).isdigit():
            continue
        if type(value) is not int or not 1 <= value <= 20:  # noqa: E721 — reject bool
            continue
        item_id = int(key)
        if item_id > 0:
            cart[item_id] = value
    return cart


def dump_cart(cart: dict[int, int]) -> str:
    payload = {str(item_id): qty for item_id, qty in sorted(cart.items())}
    return json.dumps(payload, separators=(",", ":"))


def _from_row(row: sqlite3.Row) -> Session:
    return Session(
        token_hash=row["token_hash"],
        user_id=row["user_id"],
        csrf_token=row["csrf_token"],
        cart=parse_cart(row["cart_json"]),
        flash=row["flash"],
        expires_at=int(row["expires_at"]),
    )


def _purge_expired(conn, now: int) -> None:
    """Drop a small batch of expired rows, at most once a minute, off the checkout path."""
    global _last_purge
    if now - _last_purge < _PURGE_INTERVAL:
        return
    _last_purge = now
    conn.execute(
        """
        DELETE FROM sessions WHERE token_hash IN (
            SELECT token_hash FROM sessions WHERE expires_at <= ? LIMIT 32
        )
        """,
        (now,),
    )


def load_or_create(
    conn,
    cookie: str | None,
    settings: Settings,
    client_ip: str = "unknown",
) -> tuple[Session, str | None]:
    """Return the session and a new raw cookie token when one must be set."""
    now = int(time.time())
    _purge_expired(conn, now)
    if cookie:
        hashed = token_hash(cookie, settings.secret)
        row = conn.execute("SELECT * FROM sessions WHERE token_hash = ?", (hashed,)).fetchone()
        if row is not None and int(row["expires_at"]) > now:
            return _from_row(row), None
        if row is not None:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (hashed,))

    allowed = consume_rate_limit(
        conn,
        f"anon-session:{client_ip}",
        settings.anon_session_limit,
        settings.anon_session_window,
    )
    if not allowed:
        raise AppError(429, "Slow down", "Too many attempts. Wait a few minutes and try again.")

    raw = new_token()
    hashed = token_hash(raw, settings.secret)
    csrf = new_token()
    expires = now + settings.anon_session_ttl
    conn.execute(
        """
        INSERT INTO sessions (
            token_hash, user_id, csrf_token, cart_json, flash, checkout_nonce, created_at, expires_at
        ) VALUES (?, NULL, ?, '{}', NULL, NULL, ?, ?)
        """,
        (hashed, csrf, now, expires),
    )
    session = Session(hashed, None, csrf, {}, None, expires)
    return session, raw


def load_existing(conn, cookie: str | None, settings: Settings) -> Session | None:
    if not cookie:
        return None
    now = int(time.time())
    hashed = token_hash(cookie, settings.secret)
    row = conn.execute(
        "SELECT * FROM sessions WHERE token_hash = ? AND expires_at > ?",
        (hashed, now),
    ).fetchone()
    if row is None:
        return None
    return _from_row(row)


def rotate(conn, old: Session, settings: Settings, user_id: int | None) -> tuple[Session, str]:
    """Issue a new token on login so a pre-login cookie cannot be reused."""
    now = int(time.time())
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT cart_json FROM sessions WHERE token_hash = ?",
            (old.token_hash,),
        ).fetchone()
        cart_json = row["cart_json"] if row is not None else dump_cart(old.cart)
        raw = new_token()
        hashed = token_hash(raw, settings.secret)
        csrf = new_token()
        expires = now + settings.session_ttl
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (old.token_hash,))
        conn.execute(
            """
            INSERT INTO sessions (
                token_hash, user_id, csrf_token, cart_json, flash, checkout_nonce, created_at, expires_at
            ) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)
            """,
            (hashed, user_id, csrf, cart_json, now, expires),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return Session(hashed, user_id, csrf, parse_cart(cart_json), None, expires), raw


def set_flash(conn, token_hash_value: str, message: str) -> None:
    conn.execute("UPDATE sessions SET flash = ? WHERE token_hash = ?", (message, token_hash_value))


def take_flash(conn, session: Session) -> str | None:
    flash = session.flash
    if not flash:
        return None
    conn.execute("UPDATE sessions SET flash = NULL WHERE token_hash = ?", (session.token_hash,))
    session.flash = None
    return flash


def set_checkout_nonce(conn, token_hash_value: str, nonce: str, cart_snapshot: str) -> None:
    """Bind the one-time checkout token to the cart the customer just reviewed."""
    conn.execute(
        """
        UPDATE sessions
        SET checkout_nonce = ?, checkout_cart = ?
        WHERE token_hash = ?
        """,
        (nonce, cart_snapshot, token_hash_value),
    )


def delete_session(conn, token_hash_value: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash_value,))
