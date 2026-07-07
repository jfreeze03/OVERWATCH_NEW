"""Shared helpers for SQL builders."""

from __future__ import annotations

from app.config import clamp_days


def and_where(*clauses: str) -> str:
    """Join non-empty clauses with AND; always returns a valid predicate."""
    parts = [c.strip() for c in clauses if c and c.strip()]
    return " AND ".join(parts) if parts else "1 = 1"


def bounded_days(days: object, maximum: int = 90) -> int:
    """Every live ACCOUNT_USAGE builder must run through this clamp."""
    return clamp_days(days, maximum)


def lag_offset_start(days: int, lag_hours: int = 24) -> str:
    """Window start that ends before the ACCOUNT_USAGE completeness horizon.

    Comparing a complete prior window to a still-filling current window is the
    classic latency mistake; offsetting both windows by the lag avoids it.
    """
    return f"DATEADD('day', -{int(days)}, DATEADD('hour', -{int(lag_hours)}, CURRENT_TIMESTAMP()))"
