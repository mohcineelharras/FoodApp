from __future__ import annotations

import re

import pytest
from starlette.testclient import TestClient

from foodapp.config import Settings
from foodapp.main import create_app

PASSWORD = "correct-horse-battery"
STRONG_SECRET = "k3m9-Qv7L_2pR8sT4wY6uI0oP1aZ5bC9"


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "env": "test",
        "db_path": str(tmp_path / "foodapp.sqlite"),
        "secret": "test-secret-value-with-enough-length",
        "cookie_secure": False,
        "allowed_hosts": ("testserver", "localhost"),
        "trust_proxy": False,
        "login_limit": 8,
        "login_window": 900,
        "register_limit": 8,
        "register_window": 3600,
        "session_ttl": 3600,
    }
    values.update(overrides)
    return Settings(**values)


def csrf_from(page: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', page)
    if match is None:
        match = re.search(r'name="csrf-token" content="([^"]+)"', page)
    assert match is not None
    return match.group(1)


def register(client: TestClient, email: str = "ada@example.com", name: str = "Ada Lovelace", nxt: str = "/"):
    page = client.get(f"/register?next={nxt}")
    assert page.status_code == 200
    return client.post(
        "/register",
        data={
            "csrf_token": csrf_from(page.text),
            "name": name,
            "email": email,
            "password": PASSWORD,
            "password_confirm": PASSWORD,
            "next": nxt,
        },
        follow_redirects=False,
    )


def add_item(client: TestClient, item_id: str = "1"):
    page = client.get("/")
    assert page.status_code == 200
    return client.post(
        "/cart/add",
        data={"csrf_token": csrf_from(page.text), "item_id": item_id, "return_to": "/", "price": "0"},
        follow_redirects=False,
    )


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client
