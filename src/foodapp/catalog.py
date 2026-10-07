"""Menu queries. Category and search filters are bound parameters."""

from __future__ import annotations

from foodapp.security import like_pattern


def list_categories(conn) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT category FROM menu_items ORDER BY category COLLATE NOCASE"
    ).fetchall()
    return [row["category"] for row in rows]


def list_menu(conn, category: str | None, search: str) -> list:
    pattern = like_pattern(search) if search else "%"
    return list(
        conn.execute(
            """
            SELECT id, name, description, category, price_cents, stock, available
            FROM menu_items
            WHERE (? = '' OR category = ?)
              AND (
                    ? = ''
                    OR name LIKE ? ESCAPE '\\'
                    OR description LIKE ? ESCAPE '\\'
                  )
            ORDER BY category, name
            """,
            (category or "", category or "", search, pattern, pattern),
        )
    )
