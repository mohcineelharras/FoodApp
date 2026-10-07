"""Cart updates and pickup checkout.

Stock moves inside one immediate transaction so two orders cannot take the last plate.
Prices are read from the menu row, not from the browser.
"""

from __future__ import annotations

import logging
import sqlite3
import time

from foodapp.db import connect, open_db
from foodapp.errors import OrderError
from foodapp.format import format_cents, format_time
from foodapp.security import clean_note, clean_person_name, tokens_match
from foodapp.sessions import dump_cart, parse_cart

logger = logging.getLogger("foodapp")


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        logger.exception("rollback failed")


def add_item(db_path: str, token_hash_value: str, item_id: int) -> str:
    with open_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            session = conn.execute(
                "SELECT cart_json FROM sessions WHERE token_hash = ?",
                (token_hash_value,),
            ).fetchone()
            if session is None:
                conn.execute("ROLLBACK")
                return "Your session expired. Refresh the page and try again."
            item = conn.execute(
                "SELECT id, name, stock, available FROM menu_items WHERE id = ?",
                (item_id,),
            ).fetchone()
            if item is None or item["available"] != 1 or item["stock"] <= 0:
                conn.execute("ROLLBACK")
                return "That dish is not available."
            cart = parse_cart(session["cart_json"])
            current = cart.get(item_id, 0)
            if current >= 20 or current >= int(item["stock"]):
                conn.execute("ROLLBACK")
                return f"That's all the {item['name']} we can hold in one order."
            cart[item_id] = current + 1
            _save_cart(conn, token_hash_value, cart)
            conn.execute("COMMIT")
            return f"Added {item['name']} to your cart."
        except Exception:
            _rollback(conn)
            raise


def update_qty(db_path: str, token_hash_value: str, item_id: int, qty: int) -> str:
    with open_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            session = conn.execute(
                "SELECT cart_json FROM sessions WHERE token_hash = ?",
                (token_hash_value,),
            ).fetchone()
            item = conn.execute(
                "SELECT id, name, stock, available FROM menu_items WHERE id = ?",
                (item_id,),
            ).fetchone()
            if session is None or item is None:
                conn.execute("ROLLBACK")
                return "That dish is not in your cart."
            if item["available"] != 1 or qty > int(item["stock"]):
                conn.execute("ROLLBACK")
                return f"Only {item['stock']} {item['name']} left."
            cart = parse_cart(session["cart_json"])
            if item_id not in cart:
                conn.execute("ROLLBACK")
                return "That dish is not in your cart."
            cart[item_id] = qty
            _save_cart(conn, token_hash_value, cart)
            conn.execute("COMMIT")
            return f"Updated {item['name']}."
        except Exception:
            _rollback(conn)
            raise


def remove_item(db_path: str, token_hash_value: str, item_id: int) -> str:
    with open_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            session = conn.execute(
                "SELECT cart_json FROM sessions WHERE token_hash = ?",
                (token_hash_value,),
            ).fetchone()
            if session is None:
                conn.execute("ROLLBACK")
                return "Your session expired. Refresh the page and try again."
            item = conn.execute("SELECT name FROM menu_items WHERE id = ?", (item_id,)).fetchone()
            cart = parse_cart(session["cart_json"])
            cart.pop(item_id, None)
            _save_cart(conn, token_hash_value, cart)
            conn.execute("COMMIT")
            name = item["name"] if item is not None else "That dish"
            return f"Removed {name}."
        except Exception:
            _rollback(conn)
            raise


def _save_cart(conn, token_hash_value: str, cart: dict[int, int]) -> None:
    conn.execute(
        """
        UPDATE sessions
        SET cart_json = ?, checkout_nonce = NULL, checkout_cart = NULL
        WHERE token_hash = ?
        """,
        (dump_cart(cart), token_hash_value),
    )


def cart_lines(conn, cart: dict[int, int]) -> tuple[list[dict[str, object]], int, bool]:
    lines: list[dict[str, object]] = []
    total = 0
    ready = bool(cart)
    for item_id, qty in sorted(cart.items()):
        row = conn.execute(
            """
            SELECT id, name, price_cents, stock, available
            FROM menu_items WHERE id = ?
            """,
            (item_id,),
        ).fetchone()
        if row is None:
            ready = False
            continue
        orderable = row["available"] == 1 and int(row["stock"]) >= qty
        if not orderable:
            ready = False
        line_total = qty * int(row["price_cents"])
        if orderable:
            total += line_total
        lines.append(
            {
                "id": row["id"],
                "name": row["name"],
                "qty": qty,
                "stock": int(row["stock"]),
                "price": format_cents(int(row["price_cents"])),
                "line_total": format_cents(line_total),
                "orderable": orderable,
            }
        )
    return lines, total, ready and bool(lines)


def place_order(
    db_path: str,
    token_hash_value: str,
    pickup_name: object,
    note: object,
    nonce: object,
) -> int:
    name = clean_person_name(pickup_name)
    if name is None:
        raise OrderError("Enter the name we should call at pickup.")
    cleaned_note = clean_note(note)
    if not isinstance(nonce, str):
        raise OrderError("This checkout page expired. Review your order and submit it again.")

    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT user_id, checkout_nonce, checkout_cart, cart_json
            FROM sessions WHERE token_hash = ?
            """,
            (token_hash_value,),
        ).fetchone()
        if row is None or row["user_id"] is None:
            conn.execute("ROLLBACK")
            raise OrderError("Sign in before placing a pickup order.")
        reviewed = row["checkout_cart"] or ""
        live = dump_cart(parse_cart(row["cart_json"]))
        if not reviewed or live != reviewed:
            conn.execute("ROLLBACK")
            raise OrderError(
                "Your cart changed after this page was shown. Review the order and submit it again."
            )
        if not tokens_match(row["checkout_nonce"] or "", nonce):
            conn.execute("ROLLBACK")
            raise OrderError("This checkout page expired. Review your order and submit it again.")
        cart = parse_cart(row["cart_json"])
        if not cart:
            conn.execute("ROLLBACK")
            raise OrderError("Your cart is empty.")

        lines: list[tuple[int, str, int, int]] = []
        total = 0
        for item_id, qty in sorted(cart.items()):
            item = conn.execute(
                """
                SELECT id, name, price_cents, stock, available
                FROM menu_items WHERE id = ?
                """,
                (item_id,),
            ).fetchone()
            if item is None or item["available"] != 1 or int(item["stock"]) < qty:
                conn.execute("ROLLBACK")
                raise OrderError(
                    "A dish sold out while you were checking out. Review your cart and try again."
                )
            updated = conn.execute(
                """
                UPDATE menu_items
                SET stock = stock - ?
                WHERE id = ? AND available = 1 AND stock >= ?
                """,
                (qty, item_id, qty),
            )
            if updated.rowcount != 1:
                conn.execute("ROLLBACK")
                raise OrderError(
                    "A dish sold out while you were checking out. Review your cart and try again."
                )
            price = int(item["price_cents"])
            total += qty * price
            lines.append((int(item["id"]), str(item["name"]), qty, price))

        now = int(time.time())
        cursor = conn.execute(
            """
            INSERT INTO orders (user_id, status, total_cents, pickup_name, note, created_at)
            VALUES (?, 'placed', ?, ?, ?, ?)
            """,
            (row["user_id"], total, name, cleaned_note, now),
        )
        order_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO order_items (order_id, item_id, name, qty, price_cents)
            VALUES (?, ?, ?, ?, ?)
            """,
            [(order_id, item_id, item_name, qty, price) for item_id, item_name, qty, price in lines],
        )
        conn.execute(
            """
            UPDATE sessions
            SET cart_json = '{}', checkout_nonce = NULL, checkout_cart = NULL
            WHERE token_hash = ?
            """,
            (token_hash_value,),
        )
        conn.execute("COMMIT")
        return order_id
    except OrderError:
        raise
    except Exception:
        logger.exception("order failed")
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            logger.exception("order rollback failed")
        raise
    finally:
        conn.close()


def list_orders(conn, user_id: int) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT id, status, total_cents, pickup_name, created_at
        FROM orders WHERE user_id = ? ORDER BY id DESC
        """,
        (user_id,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "status": "Placed for pickup" if row["status"] == "placed" else row["status"],
            "total": format_cents(int(row["total_cents"])),
            "pickup_name": row["pickup_name"],
            "created": format_time(int(row["created_at"])),
        }
        for row in rows
    ]


def get_order(conn, user_id: int, order_id: int) -> dict[str, object] | None:
    row = conn.execute(
        """
        SELECT id, status, total_cents, pickup_name, note, created_at
        FROM orders WHERE id = ? AND user_id = ?
        """,
        (order_id, user_id),
    ).fetchone()
    if row is None:
        return None
    items = conn.execute(
        """
        SELECT name, qty, price_cents FROM order_items
        WHERE order_id = ? ORDER BY id
        """,
        (order_id,),
    ).fetchall()
    return {
        "id": row["id"],
        "status": "Placed for pickup" if row["status"] == "placed" else row["status"],
        "total": format_cents(int(row["total_cents"])),
        "pickup_name": row["pickup_name"],
        "note": row["note"],
        "created": format_time(int(row["created_at"])),
        "items": [
            {
                "name": item["name"],
                "qty": item["qty"],
                "price": format_cents(int(item["price_cents"])),
                "line_total": format_cents(int(item["qty"]) * int(item["price_cents"])),
            }
            for item in items
        ],
    }
