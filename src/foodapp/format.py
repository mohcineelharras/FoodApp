"""Display helpers for money and timestamps."""

from __future__ import annotations

from datetime import UTC, datetime


def format_cents(cents: int) -> str:
    amount = int(cents)
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    return f"{sign}${amount // 100}.{amount % 100:02d}"


def format_time(timestamp: int) -> str:
    moment = datetime.fromtimestamp(int(timestamp), UTC)
    return moment.strftime("%d %b %Y, %H:%M UTC")
