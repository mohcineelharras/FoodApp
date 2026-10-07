from __future__ import annotations

import re

from starlette.testclient import TestClient
from tests.conftest import PASSWORD, add_item, csrf_from, register


def test_menu_lists_dishes_and_hides_nothing_on_empty_search(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "Tomato lentil soup" in page.text
    assert "Fig tart" in page.text
    assert "Not tonight" in page.text
    assert "Add to cart" in page.text


def test_empty_cart_and_add_then_remove(client):
    empty = client.get("/cart")
    assert "The cart is empty" in empty.text

    added = add_item(client)
    assert added.status_code == 303
    cart = client.get("/cart")
    assert "Tomato lentil soup" in cart.text
    assert "$6.50" in cart.text

    removed = client.post(
        "/cart/remove",
        data={"csrf_token": csrf_from(cart.text), "item_id": "1"},
        follow_redirects=False,
    )
    assert removed.status_code == 303
    assert "The cart is empty" in client.get("/cart").text


def test_unavailable_dish_and_quantity_limit(client):
    unavailable = add_item(client, "8")
    assert unavailable.status_code == 303
    menu = client.get("/")
    assert "That dish is not available." in menu.text
    assert "In your cart" not in menu.text

    add_item(client, "1")
    cart = client.get("/cart")
    updated = client.post(
        "/cart/update",
        data={"csrf_token": csrf_from(cart.text), "item_id": "1", "qty": "99"},
        follow_redirects=False,
    )
    assert updated.status_code == 303
    assert "Choose a quantity between 1 and 20." in client.get("/cart").text


def test_register_keeps_cart_and_checkout_uses_menu_price(client):
    add_item(client)
    created = register(client, nxt="/checkout")
    assert created.status_code == 303
    assert created.headers["location"] == "/checkout"

    checkout = client.get("/checkout")
    assert "Tomato lentil soup" in checkout.text
    assert "$6.50" in checkout.text
    placed = client.post(
        "/checkout",
        data={
            "csrf_token": csrf_from(checkout.text),
            "checkout_nonce": _nonce(checkout.text),
            "pickup_name": "Ada Lovelace",
            "note": "<img src=x onerror=alert(1)>",
            "price": "0",
        },
        follow_redirects=False,
    )
    assert placed.status_code == 303
    assert placed.headers["location"].startswith("/orders/")
    order = client.get(placed.headers["location"])
    assert order.status_code == 200
    assert "Tomato lentil soup" in order.text
    assert "$6.50" in order.text
    assert "<img" not in order.text
    assert "&lt;img" in order.text
    assert "The cart is empty" in client.get("/cart").text


def test_checkout_requires_sign_in_and_login_round_trip(client):
    add_item(client)
    checkout = client.get("/checkout")
    assert "Sign in to place this order" in checkout.text
    blocked = client.post(
        "/checkout",
        data={"csrf_token": csrf_from(checkout.text), "pickup_name": "Ada Lovelace"},
        follow_redirects=False,
    )
    assert blocked.status_code == 303
    assert blocked.headers["location"].startswith("/login")

    register(client)
    account = client.get("/account")
    assert "Ada Lovelace" in account.text
    signed_out = client.post(
        "/logout",
        data={"csrf_token": csrf_from(account.text)},
        follow_redirects=False,
    )
    assert signed_out.status_code == 303
    login = client.get("/login")
    failed = client.post(
        "/login",
        data={
            "csrf_token": csrf_from(login.text),
            "email": "ada@example.com",
            "password": "not-the-right-password",
            "next": "https://evil.example/phish",
        },
        follow_redirects=False,
    )
    assert failed.status_code == 401
    assert "Email or password is incorrect." in failed.text
    assert "https://evil.example" not in failed.text
    retry = client.post(
        "/login",
        data={
            "csrf_token": csrf_from(failed.text),
            "email": "ada@example.com",
            "password": PASSWORD,
            "next": "//evil.example",
        },
        follow_redirects=False,
    )
    assert retry.status_code == 303
    assert retry.headers["location"] == "/"


def test_orders_are_private_to_the_account(client, app):
    add_item(client)
    register(client, name="Unique Pickup Person")
    checkout = client.get("/checkout")
    placed = client.post(
        "/checkout",
        data={
            "csrf_token": csrf_from(checkout.text),
            "checkout_nonce": _nonce(checkout.text),
            "pickup_name": "Unique Pickup Person",
            "note": "",
        },
        follow_redirects=False,
    )
    order_path = placed.headers["location"]
    with TestClient(app) as other:
        register(other, email="lin@example.com", name="Lin Okonkwo")
        stolen = other.get(order_path)
    assert stolen.status_code == 404
    assert "Unique Pickup Person" not in stolen.text


def test_password_rules_and_duplicate_email(client):
    page = client.get("/register")
    token = csrf_from(page.text)
    short = client.post(
        "/register",
        data={
            "csrf_token": token,
            "name": "Ada Lovelace",
            "email": "ada@example.com",
            "password": "short-pass",
            "password_confirm": "short-pass",
        },
        follow_redirects=False,
    )
    assert short.status_code == 400
    assert "12 to 128" in short.text

    mismatched = client.post(
        "/register",
        data={
            "csrf_token": csrf_from(short.text),
            "name": "Ada Lovelace",
            "email": "ada@example.com",
            "password": PASSWORD,
            "password_confirm": PASSWORD + "x",
        },
        follow_redirects=False,
    )
    assert mismatched.status_code == 400
    assert "do not match" in mismatched.text

    created = register(client)
    assert created.status_code == 303
    client.post("/logout", data={"csrf_token": csrf_from(client.get("/account").text)}, follow_redirects=False)
    again = register(client)
    assert again.status_code == 400
    assert "already exists" in again.text


def test_checkout_rejects_a_cart_changed_after_review(client):
    add_item(client, "1")
    created = register(client, nxt="/checkout")
    assert created.status_code == 303
    checkout = client.get("/checkout")
    assert "Roast chicken plate" not in checkout.text
    assert add_item(client, "3").status_code == 303
    stale = client.post(
        "/checkout",
        data={
            "csrf_token": csrf_from(checkout.text),
            "checkout_nonce": _nonce(checkout.text),
            "pickup_name": "Ada Lovelace",
            "note": "",
        },
        follow_redirects=False,
    )
    assert stale.status_code == 303
    assert stale.headers["location"] == "/checkout"
    review = client.get(stale.headers["location"])
    assert "Your cart changed" in review.text
    assert "Roast chicken plate" in review.text
    assert "Placed for pickup" not in client.get("/orders").text


def test_search_and_category_do_not_break_the_menu(client):
    injected = client.get("/", params={"q": "%' OR 1=1 --", "category": "<script>alert(1)</script>"})
    assert injected.status_code == 200
    assert "<script>" not in injected.text
    assert "Tomato lentil soup" not in injected.text
    soups = client.get("/", params={"category": "Soups"})
    assert "Tomato lentil soup" in soups.text
    assert "Roast chicken plate" not in soups.text
    empty = client.get("/", params={"q": "not-a-dish"})
    assert "No dishes match" in empty.text


def _nonce(page: str) -> str:
    match = re.search(r'name="checkout_nonce" value="([^"]+)"', page)
    assert match is not None
    return match.group(1)
