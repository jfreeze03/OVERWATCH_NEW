"""Compare mode — period-pair math (pure, tested).

Half-open date windows [start, end): SQL reads DAY >= start AND DAY < end.
The current partial month is never a compare side by default (house
partial-honesty rule); the labeled escape hatch pairs MTD against the SAME
number of days of the prior month — equal-length windows or nothing.
"""

from __future__ import annotations

from datetime import date, timedelta


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _prev_month_start(d: date) -> date:
    return (d.replace(day=1) - timedelta(days=1)).replace(day=1)


def period_pair(kind: str, today: date, include_partial: bool = False) -> dict:
    """Return the two compare windows for a pairing kind.

    kinds: "month" (last full month vs prior — the default),
    "7d" / "30d" (trailing vs prior trailing, both ending yesterday so no
    partial day ever enters a side). "month" + include_partial=True pairs
    MTD (partial, dimmed) against the same day-count of the prior month.
    """
    kind = str(kind or "month").lower()
    if kind == "month":
        cur0 = _month_start(today)
        if include_partial:
            n = (today - cur0).days + 1          # MTD day-count incl. today
            b0 = _prev_month_start(today)
            # Equal-length windows or nothing (docstring contract): cap BOTH
            # sides to what the shorter prior month can supply. The old code
            # clamped only B, so Mar 31 (n=31) paired a 31-day A against a 28-day
            # B under a false "same 31 days" label (audit #15).
            m = min(n, (cur0 - b0).days)         # (cur0 - b0).days = prior month length
            return {
                "a": (cur0.isoformat(), (cur0 + timedelta(days=m)).isoformat()),
                "b": (b0.isoformat(), (b0 + timedelta(days=m)).isoformat()),
                "label_a": f"{cur0:%Y-%m} (MTD, partial)",
                "label_b": f"{b0:%Y-%m} (same {m} days)",
                "partial": True,
            }
        a0 = _prev_month_start(today)            # last FULL month
        b0 = _prev_month_start(a0)
        return {
            "a": (a0.isoformat(), cur0.isoformat()),
            "b": (b0.isoformat(), a0.isoformat()),
            "label_a": f"{a0:%Y-%m}",
            "label_b": f"{b0:%Y-%m}",
            "partial": False,
        }
    n = 30 if kind == "30d" else 7
    a1 = today                                   # exclusive: today never enters
    a0 = a1 - timedelta(days=n)
    b0 = a0 - timedelta(days=n)
    return {
        "a": (a0.isoformat(), a1.isoformat()),
        "b": (b0.isoformat(), a0.isoformat()),
        "label_a": f"trailing {n}d",
        "label_b": f"prior {n}d",
        "partial": False,
    }
