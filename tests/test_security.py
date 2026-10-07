from __future__ import annotations

import stat
from threading import Thread

import pytest
from starlette.testclient import TestClient
from tests.conftest import STRONG_SECRET, add_item, csrf_from, make_settings, register

from foodapp.__main__ import server_options
from foodapp.config import Settings
from foodapp.db import connect, init_db
from foodapp.errors import OrderError
from foodapp.main import create_app
from foodapp.ordering import place_order
from foodapp.security import new_token


def test_security_headers_and_cookie_flags(client):
    page = client.get("/")
    headers = {key.lower(): value.lower() for key, value in page.headers.items()}
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "deny"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert "script-src 'none'" in headers["content-security-policy"]
    assert headers["cache-control"] == "no-store"
    assert "server" not in headers
    cookie = page.headers["set-cookie"]
    attributes = {part.strip().lower() for part in cookie.split(";")}
    assert "httponly" in attributes
    assert "samesite=lax" in attributes
    assert "secure" not in attributes


def test_csrf_and_logout_method(client):
    rejected = client.post("/cart/add", data={"item_id": "1"}, follow_redirects=False)
    assert rejected.status_code == 403
    assert "Refresh the page" in rejected.text
    missing = client.get("/logout")
    assert missing.status_code == 405
    register(client)
    account = client.get("/account")
    wrong = client.post("/logout", data={"csrf_token": "not-the-token"}, follow_redirects=False)
    assert wrong.status_code == 403
    assert "Ada Lovelace" in client.get("/account").text
    signed_out = client.post(
        "/logout",
        data={"csrf_token": csrf_from(account.text)},
        follow_redirects=False,
    )
    assert signed_out.status_code == 303
    follow = client.get("/account", follow_redirects=False)
    assert follow.status_code == 303
    assert follow.headers["location"].startswith("/login")


def test_session_is_rotated_on_login(client, app):
    add_item(client)
    old = client.cookies.get("session")
    assert old
    created = register(client)
    assert created.status_code == 303
    new = client.cookies.get("session")
    assert new and new != old
    assert "Tomato lentil soup" in client.get("/cart").text
    with TestClient(app) as reused:
        reused.cookies.set("session", old)
        account = reused.get("/account", follow_redirects=False)
    assert account.status_code == 303
    assert account.headers["location"].startswith("/login")


def test_login_rate_limit_ignores_forwarded_for(tmp_path):
    app = create_app(make_settings(tmp_path, login_limit=2))
    with TestClient(app) as client:
        _assert_login_rate_limit(client)


def _assert_login_rate_limit(client):
    login = client.get("/login")
    token = csrf_from(login.text)
    for index in range(2):
        failed = client.post(
            "/login",
            data={
                "csrf_token": token,
                "email": "ada@example.com",
                "password": "not-the-right-password",
            },
            headers={"x-forwarded-for": f"203.0.113.{index}"},
            follow_redirects=False,
        )
        assert failed.status_code == 401
        token = csrf_from(failed.text)
    blocked = client.post(
        "/login",
        data={
            "csrf_token": token,
            "email": "ada@example.com",
            "password": "not-the-right-password",
        },
        headers={"x-forwarded-for": "198.51.100.10"},
        follow_redirects=False,
    )
    assert blocked.status_code == 429
    assert "Too many attempts" in blocked.text


def test_trusted_proxy_uses_the_appended_client(tmp_path):
    app = create_app(make_settings(tmp_path, trust_proxy=True, login_limit=1))
    with TestClient(app) as client:
        page = client.get("/login")

        def attempt(page_html: str, email: str, forwarded: str):
            return client.post(
                "/login",
                data={
                    "csrf_token": csrf_from(page_html),
                    "email": email,
                    "password": "not-the-right-password",
                },
                headers={"x-forwarded-for": forwarded},
                follow_redirects=False,
            )

        first = attempt(page.text, "ada@example.com", "203.0.113.5, 198.51.100.8")
        assert first.status_code == 401
        second = attempt(first.text, "lin@example.com", "203.0.113.5, 198.51.100.9")
        assert second.status_code == 401
        repeated = attempt(second.text, "sam@example.com", "198.51.100.20, 198.51.100.8")
        assert repeated.status_code == 429


def test_register_rate_limit(tmp_path):
    app = create_app(make_settings(tmp_path, register_limit=2))
    with TestClient(app) as client:
        _assert_register_rate_limit(client)


def _assert_register_rate_limit(client):
    assert register(client, email="one@example.com").status_code == 303
    client.post("/logout", data={"csrf_token": csrf_from(client.get("/account").text)}, follow_redirects=False)
    assert register(client, email="two@example.com", name="Lin Okonkwo").status_code == 303
    client.post("/logout", data={"csrf_token": csrf_from(client.get("/account").text)}, follow_redirects=False)
    blocked = register(client, email="three@example.com", name="Sam Rivera")
    assert blocked.status_code == 429


def test_open_redirect_and_docs_are_closed(client):
    page = client.get("/login?next=https://evil.example")
    assert 'value="/"' in page.text or 'name="next" value="/"' in page.text
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_unhandled_errors_hide_internals(app):
    with TestClient(app, raise_server_exceptions=False) as quiet:
        boom = quiet.get("/__test__/boom")
    assert boom.status_code == 500
    assert "The kitchen hit a snag" in boom.text
    assert "secret boom marker" not in boom.text
    assert "Traceback" not in boom.text


def test_health_static_and_body_limit(client):
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert "set-cookie" not in health.headers
    styles = client.get("/static/app.css")
    assert styles.status_code == 200
    assert "site-header" in styles.text
    traversal = client.get("/static/%2e%2e/%2e%2e/pyproject.toml")
    assert traversal.status_code in {400, 404}
    assert "fastapi" not in traversal.text.lower()
    large = client.post(
        "/login",
        content=b"a=" + b"b" * 70_000,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert large.status_code == 413
    assert large.headers["x-content-type-options"] == "nosniff"


def test_rejects_unknown_host(client):
    response = client.get("/", headers={"host": "evil.example"})
    assert response.status_code == 400


def test_production_refuses_weak_configuration(tmp_path):
    db_path = str(tmp_path / "prod.sqlite")
    base = {
        "env": "production",
        "db_path": db_path,
        "allowed_hosts": ("example.com",),
        "trust_proxy": False,
        "cookie_secure": True,
    }
    with pytest.raises(RuntimeError):
        create_app(Settings(secret="replace-with-at-least-32-random-characters", **base))
    with pytest.raises(RuntimeError):
        create_app(Settings(secret="a" * 40, **base))
    with pytest.raises(RuntimeError):
        create_app(
            Settings(
                secret=STRONG_SECRET,
                allowed_hosts=(),
                env="production",
                db_path=db_path,
                trust_proxy=False,
                cookie_secure=True,
            )
        )


def test_production_forces_secure_cookies(tmp_path):
    app = create_app(
        Settings(
            env="production",
            db_path=str(tmp_path / "prod.sqlite"),
            secret=STRONG_SECRET,
            cookie_secure=False,
            allowed_hosts=("testserver",),
            trust_proxy=False,
        )
    )
    assert app.state.settings.cookie_secure is True
    with TestClient(app) as client:
        page = client.get("/")
        assert "strict-transport-security" in {key.lower() for key in page.headers}
        attributes = {part.strip().lower() for part in page.headers["set-cookie"].split(";")}
        assert "secure" in attributes
        assert client.get("/__test__/boom").status_code == 404
        assert client.get("/docs").status_code == 404


def test_last_plate_cannot_be_sold_twice(tmp_path):
    settings = make_settings(tmp_path)
    create_app(settings)
    now = 1_700_000_000
    nonce_a = new_token()
    nonce_b = new_token()
    with connect(settings.db_path) as conn:
        conn.execute("UPDATE menu_items SET stock = 1, available = 1 WHERE id = 1")
        cursor = conn.execute(
            "INSERT INTO users (email, name, password_hash, created_at) VALUES (?, ?, ?, ?)",
            ("stock@example.com", "Stock Tester", "not-a-login-hash", now),
        )
        user_id = int(cursor.lastrowid)
        for suffix, nonce in (("a", nonce_a), ("b", nonce_b)):
            conn.execute(
                """
                INSERT INTO sessions (
                    token_hash, user_id, csrf_token, cart_json, flash,
                    checkout_nonce, checkout_cart, created_at, expires_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)
                """,
                (
                    f"hash-{suffix}",
                    user_id,
                    f"csrf-{suffix}",
                    '{"1":1}',
                    nonce,
                    '{"1":1}',
                    now,
                    now + 3600,
                ),
            )

    results: list[int] = []
    errors: list[str] = []

    def buy(token_hash: str, nonce: str) -> None:
        try:
            results.append(place_order(settings.db_path, token_hash, "Stock Tester", "", nonce))
        except OrderError as exc:
            errors.append(exc.message)
        except Exception as exc:  # pragma: no cover - makes a lock failure visible
            errors.append(f"{type(exc).__name__}: {exc}")

    first = Thread(target=buy, args=("hash-a", nonce_a))
    second = Thread(target=buy, args=("hash-b", nonce_b))
    first.start()
    second.start()
    first.join()
    second.join()
    assert len(results) == 1
    assert len(errors) == 1
    with connect(settings.db_path) as conn:
        stock = conn.execute("SELECT stock FROM menu_items WHERE id = 1").fetchone()["stock"]
        orders = conn.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"]
    assert stock == 0
    assert orders == 1


def test_server_stays_on_loopback_without_proxy_headers(monkeypatch, settings):
    monkeypatch.delenv("FOODAPP_HOST", raising=False)
    monkeypatch.delenv("FOODAPP_PORT", raising=False)
    options = server_options(settings)
    assert options["host"] == "127.0.0.1"
    assert options["port"] == 8000
    assert options["server_header"] is False
    assert options["proxy_headers"] is False


def test_database_files_are_private(tmp_path):
    database = tmp_path / "shop" / "foodapp.sqlite"
    init_db(str(database))
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    assert stat.S_IMODE(database.parent.stat().st_mode) == 0o700
    for suffix in ("-wal", "-shm"):
        side = database.parent / f"{database.name}{suffix}"
        if side.exists():
            assert stat.S_IMODE(side.stat().st_mode) == 0o600


def test_anonymous_sessions_are_short_and_limited(tmp_path):
    settings = make_settings(tmp_path, anon_session_ttl=600, anon_session_limit=2)
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        with connect(settings.db_path) as conn:
            row = conn.execute("SELECT created_at, expires_at, user_id FROM sessions").fetchone()
        assert row["user_id"] is None
        assert int(row["expires_at"]) - int(row["created_at"]) == 600
        client.cookies.clear()
        assert client.get("/cart").status_code == 200
        client.cookies.clear()
        blocked = client.get("/")
    assert blocked.status_code == 429
    assert "Slow down" in blocked.text
