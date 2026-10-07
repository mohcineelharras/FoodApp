"""SQLite storage. Statements stay parameterized; this module owns the schema."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    csrf_token TEXT NOT NULL,
    cart_json TEXT NOT NULL DEFAULT '{}',
    flash TEXT,
    checkout_nonce TEXT,
    checkout_cart TEXT,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS sessions_expires ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS menu_items (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    category TEXT NOT NULL,
    price_cents INTEGER NOT NULL CHECK (price_cents >= 0),
    stock INTEGER NOT NULL CHECK (stock >= 0),
    available INTEGER NOT NULL CHECK (available IN (0, 1))
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    status TEXT NOT NULL,
    total_cents INTEGER NOT NULL CHECK (total_cents >= 0),
    pickup_name TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS orders_user ON orders(user_id);

CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    item_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    qty INTEGER NOT NULL CHECK (qty > 0),
    price_cents INTEGER NOT NULL CHECK (price_cents >= 0)
);

CREATE TABLE IF NOT EXISTS rate_limits (
    bucket TEXT PRIMARY KEY,
    window_start INTEGER NOT NULL,
    count INTEGER NOT NULL
);
"""

# id, name, description, category, price_cents, stock, available
SEED: tuple[tuple[int, str, str, str, int, int, int], ...] = (
    (1, "Tomato lentil soup", "Red lentils, fire-roasted tomato, and olive oil.", "Soups", 650, 20, 1),
    (2, "Charred broccoli grain bowl", "Broccoli, farro, chili, and lemon yogurt.", "Bowls", 1400, 20, 1),
    (3, "Roast chicken plate", "Half chicken, pan juices, and a crisp salad.", "Plates", 1800, 12, 1),
    (4, "Mushroom rye toast", "Roasted mushrooms, rye, and herb butter.", "Small plates", 1100, 16, 1),
    (5, "Citrus olive salad", "Orange, fennel, olives, and bitter greens.", "Salads", 900, 18, 1),
    (6, "Brown butter cookie", "One warm cookie, salted at the edges.", "Sweets", 350, 30, 1),
    (7, "Mint iced tea", "Black tea, mint, and a little citrus.", "Drinks", 300, 40, 1),
    (8, "Fig tart", "On the menu another night.", "Sweets", 700, 0, 0),
)


# Never chmod a shared temp root. File mode 0600 still covers a database that lives there.
_SHARED_DIR_NAMES = frozenset({"tmp", "temp", "shm"})


def _restrict_db_files(path: str) -> None:
    """Keep password hashes and session rows readable only by the service user."""
    if path == ":memory:":
        return
    database = Path(path).expanduser()
    for candidate in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
        if candidate.is_file():
            os.chmod(candidate, stat.S_IRUSR | stat.S_IWUSR)
    parent = database.resolve().parent
    if parent == Path("/") or parent == Path.home() or parent.name in _SHARED_DIR_NAMES:
        return
    try:
        os.chmod(parent, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    except OSError:
        return


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    _restrict_db_files(path)
    return conn


@contextmanager
def open_db(path: str) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


def init_db(path: str) -> None:
    if path != ":memory:":
        directory = Path(path).expanduser().resolve().parent
        directory.mkdir(parents=True, exist_ok=True)
        _restrict_db_files(path)
    with open_db(path) as conn:
        conn.executescript(SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
        if "checkout_cart" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN checkout_cart TEXT")
        count = conn.execute("SELECT COUNT(*) AS c FROM menu_items").fetchone()["c"]
        if count == 0:
            conn.executemany(
                """
                INSERT INTO menu_items (id, name, description, category, price_cents, stock, available)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                SEED,
            )
