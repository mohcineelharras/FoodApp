"""Registration and login."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from dataclasses import dataclass

from foodapp.config import Settings
from foodapp.db import open_db
from foodapp.security import (
    DUMMY_PASSWORD_HASH,
    clean_person_name,
    consume_rate_limit,
    hash_password,
    normalize_email,
    password_problem,
    verify_password,
)
from foodapp.sessions import Session, rotate

logger = logging.getLogger("foodapp")


@dataclass
class AuthResult:
    error: str | None = None
    raw_token: str | None = None
    limited: bool = False
    name: str = ""
    email: str = ""


def get_user(conn, user_id: int | None) -> dict[str, str] | None:
    if user_id is None:
        return None
    row = conn.execute("SELECT id, name, email FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        return None
    return {"name": row["name"], "email": row["email"]}


def register(
    settings: Settings,
    session: Session,
    *,
    name: object,
    email: object,
    password: object,
    password_confirm: object,
    ip: str,
) -> AuthResult:
    clean_name = clean_person_name(name) or ""
    normal_email = normalize_email(email) or ""
    result = AuthResult(name=clean_name, email=normal_email)
    with open_db(settings.db_path) as conn:
        allowed = consume_rate_limit(
            conn,
            f"register:ip:{ip}",
            settings.register_limit,
            settings.register_window,
        )
        if not allowed:
            logger.warning("rate limit exceeded action=register")
            result.limited = True
            result.error = "Too many attempts. Wait a few minutes and try again."
            return result

        if clean_person_name(name) is None:
            result.error = "Enter the name the counter should call."
            return result
        if normalize_email(email) is None:
            result.error = "Enter a valid email address."
            return result
        if password != password_confirm:
            result.error = "Those passwords do not match."
            return result
        problem = password_problem(password, normal_email)
        if problem is not None or not isinstance(password, str):
            result.error = problem or "Use 12 to 128 characters, and don't use a common password."
            return result

        password_hash = hash_password(password)
        now = int(time.time())
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute("SELECT id FROM users WHERE email = ?", (normal_email,)).fetchone()
            if existing is not None:
                conn.execute("ROLLBACK")
                result.error = "An account with that email already exists. Sign in instead."
                return result
            cursor = conn.execute(
                "INSERT INTO users (email, name, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (normal_email, clean_name, password_hash, now),
            )
            user_id = int(cursor.lastrowid)
            conn.execute("COMMIT")
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            result.error = "An account with that email already exists. Sign in instead."
            return result
        except Exception:
            conn.execute("ROLLBACK")
            raise

        _session, raw = rotate(conn, session, settings, user_id)
        result.raw_token = raw
        return result


def authenticate(
    settings: Settings,
    session: Session,
    *,
    email: object,
    password: object,
    ip: str,
) -> AuthResult:
    normal_email = normalize_email(email)
    result = AuthResult(email=normal_email or "")
    with open_db(settings.db_path) as conn:
        ip_ok = consume_rate_limit(
            conn,
            f"login:ip:{ip}",
            settings.login_limit,
            settings.login_window,
        )
        email_ok = True
        if normal_email is not None:
            email_key = hashlib.sha256(normal_email.encode()).hexdigest()
            email_ok = consume_rate_limit(
                conn,
                f"login:email:{email_key}",
                settings.login_limit,
                settings.login_window,
            )
        if not ip_ok or not email_ok:
            logger.warning("rate limit exceeded action=login")
            result.limited = True
            result.error = "Too many attempts. Wait a few minutes and try again."
            return result

        if normal_email is None or not isinstance(password, str) or not 12 <= len(password) <= 128:
            result.error = "Email or password is incorrect."
            return result

        user = conn.execute(
            "SELECT id, password_hash FROM users WHERE email = ?",
            (normal_email,),
        ).fetchone()
        encoded = user["password_hash"] if user is not None else DUMMY_PASSWORD_HASH
        valid = verify_password(password, encoded)
        if user is None or not valid:
            result.error = "Email or password is incorrect."
            return result

        _session, raw = rotate(conn, session, settings, int(user["id"]))
        result.raw_token = raw
        return result
