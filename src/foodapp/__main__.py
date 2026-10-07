"""Run the pickup counter on loopback unless an operator binds another host."""

from __future__ import annotations

import os
import re

import uvicorn

from foodapp.config import Settings, configure_logging
from foodapp.main import create_app

_HOST_RE = re.compile(r"[A-Za-z0-9.\-]{1,253}")
# Operators may bind a container interface explicitly. The default host is loopback.
_EXPLICIT_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0"})  # noqa: S104


def server_options(settings: Settings) -> dict[str, object]:
    host = os.environ.get("FOODAPP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    if host not in _EXPLICIT_HOSTS and _HOST_RE.fullmatch(host) is None:
        raise RuntimeError("FOODAPP_HOST is invalid.")
    raw_port = os.environ.get("FOODAPP_PORT", "8000").strip()
    if not raw_port.isdigit() or not 1 <= int(raw_port) <= 65535:
        raise RuntimeError("FOODAPP_PORT is invalid.")
    return {
        "host": host,
        "port": int(raw_port),
        "server_header": False,
        "proxy_headers": settings.trust_proxy,
        "forwarded_allow_ips": "127.0.0.1,::1" if settings.trust_proxy else "127.0.0.1",
    }


def main() -> None:
    configure_logging()
    settings = Settings.from_env()
    application = create_app(settings)
    uvicorn.run(application, **server_options(settings))


if __name__ == "__main__":
    main()
