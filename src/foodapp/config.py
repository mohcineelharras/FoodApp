"""Process configuration. Production refuses placeholder secrets."""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, replace

logger = logging.getLogger("foodapp")

_PLACEHOLDER_SECRETS = frozenset(
    {
        "replace-with-at-least-32-random-characters",
        "change-me-change-me-change-me-change-me",
    }
)


def configure_logging() -> None:
    logger = logging.getLogger("foodapp")
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def secret_is_strong(secret: str) -> bool:
    if not 32 <= len(secret) <= 256:
        return False
    lowered = secret.lower()
    if lowered in _PLACEHOLDER_SECRETS or lowered.startswith("replace-with"):
        return False
    return len(set(secret)) >= 8


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class Settings:
    env: str
    db_path: str
    secret: str
    cookie_secure: bool
    allowed_hosts: tuple[str, ...]
    trust_proxy: bool
    login_limit: int = 10
    login_window: int = 900
    register_limit: int = 8
    register_window: int = 3600
    session_ttl: int = 14 * 24 * 3600
    anon_session_ttl: int = 2 * 3600
    anon_session_limit: int = 40
    anon_session_window: int = 600
    max_body_bytes: int = 65_536

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get("FOODAPP_ENV", "development").strip().lower()
        if env not in {"development", "test", "production"}:
            raise RuntimeError("FOODAPP_ENV must be development, test, or production.")

        hosts_raw = os.environ.get("FOODAPP_ALLOWED_HOSTS", "").strip()
        if hosts_raw:
            allowed_hosts = tuple(part.strip() for part in hosts_raw.split(",") if part.strip())
        elif env == "production":
            allowed_hosts = ()
        else:
            allowed_hosts = ("localhost", "127.0.0.1", "testserver")

        if env != "production" and "testserver" not in allowed_hosts:
            allowed_hosts = (*allowed_hosts, "testserver")

        cookie_secure = True if env == "production" else _env_flag("FOODAPP_COOKIE_SECURE")
        return cls(
            env=env,
            db_path=os.environ.get("FOODAPP_DB", "data/foodapp.sqlite").strip() or "data/foodapp.sqlite",
            secret=os.environ.get("FOODAPP_SECRET", ""),
            cookie_secure=cookie_secure,
            allowed_hosts=allowed_hosts,
            trust_proxy=_env_flag("FOODAPP_TRUST_PROXY"),
            login_limit=_env_int("FOODAPP_LOGIN_LIMIT", 10, 1, 100),
            login_window=_env_int("FOODAPP_LOGIN_WINDOW", 900, 60, 86_400),
            register_limit=_env_int("FOODAPP_REGISTER_LIMIT", 8, 1, 100),
            register_window=_env_int("FOODAPP_REGISTER_WINDOW", 3600, 60, 86_400),
            session_ttl=_env_int("FOODAPP_SESSION_TTL", 14 * 24 * 3600, 300, 30 * 24 * 3600),
            anon_session_ttl=_env_int("FOODAPP_ANON_SESSION_TTL", 2 * 3600, 300, 86_400),
            anon_session_limit=_env_int("FOODAPP_ANON_SESSION_LIMIT", 40, 1, 1_000),
            anon_session_window=_env_int("FOODAPP_ANON_SESSION_WINDOW", 600, 60, 86_400),
            max_body_bytes=_env_int("FOODAPP_MAX_BODY_BYTES", 65_536, 1024, 1_048_576),
        )


def load_settings(settings: Settings) -> Settings:
    """Reject a production boot that would ship with a known or empty secret."""
    if settings.env == "production":
        if not secret_is_strong(settings.secret):
            raise RuntimeError(
                "Refusing to start in production without a unique FOODAPP_SECRET "
                "of at least 32 characters."
            )
        if not settings.allowed_hosts:
            raise RuntimeError("Refusing to start in production without FOODAPP_ALLOWED_HOSTS.")
        if not settings.cookie_secure:
            settings = replace(settings, cookie_secure=True)
        return settings

    if settings.secret == "":
        logger.warning("FOODAPP_SECRET is unset; using an ephemeral process secret.")
        settings = replace(settings, secret=secrets.token_urlsafe(32))
    return settings
