"""Regression locks for the round-10 bug hunt (v4.425.0)."""

from __future__ import annotations

import warnings
from pathlib import Path

import pandas as pd

from app.logic.ask.registry import _analyze_warehouse_waste
from app.logic.ask.types import AskParams

_ROOT = Path(__file__).resolve().parents[1]


# --- DEP-1 (MED): no pd.Timedelta in app/ (numpy generic-unit deprecation -> future error)
def test_no_pd_timedelta_anywhere():
    """Recurrence guard: `Timestamp/Series - pd.Timedelta(...)` emits numpy 2.5+'s
    'generic unit timedelta is deprecated, will raise in the future' warning. All app AND
    test datetime arithmetic must use datetime.timedelta (exempt); keep the pandas form out
    of both trees so the warning can never turn into a hard-error on a future numpy."""
    import re
    pat = re.compile(r"\bpd\.Timedelta\(")
    offenders = []
    for base in ("app", "tests"):
        for path in (_ROOT / base).rglob("*.py"):
            if path.name == "test_bughunt_round10.py":
                continue                                    # this guard file references the name
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pat.search(line):
                    offenders.append(f"{path.relative_to(_ROOT)}:{i}")
    assert not offenders, (
        "pd.Timedelta in datetime arithmetic triggers numpy's generic-unit deprecation "
        f"(a future hard-error) — use datetime.timedelta instead: {offenders}")


def test_watch_monitor_window_has_no_numpy_deprecation():
    # The known anchor: the watch-monitor recency window must not emit the numpy warning.
    from app.logic import watch_monitor
    days = pd.to_datetime(pd.Series(["2026-01-05", "2026-01-06", "2026-01-01"]))
    latest = days.max()
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        # exercises the exact `_latest.normalize() - timedelta(days=1)` compare shape
        _ = days[days >= (latest.normalize() - watch_monitor.timedelta(days=1))]


# --- ASK-WASTE (LOW): warehouse-waste answerer discloses a top-100-capped total ---------
def _idle_frame(n: int) -> pd.DataFrame:
    return pd.DataFrame({
        "WAREHOUSE_NAME": [f"WH_{i}" for i in range(n)],
        "IDLE_CREDITS": [float(n - i) for i in range(n)],   # descending, all > 0
        "TOTAL_CREDITS": [float(2 * (n - i)) for i in range(n)],
        "IDLE_HOURS": [1.0] * n, "METERED_HOURS": [2.0] * n,
    })


def test_warehouse_waste_discloses_when_frame_is_capped():
    capped = _analyze_warehouse_waste(AskParams(30, "ALL"), {"idle": _idle_frame(100)})
    assert any("top 100 warehouses by idle credits" in b for b in capped.bullets)
    # a sub-cap frame makes no such claim
    small = _analyze_warehouse_waste(AskParams(30, "ALL"), {"idle": _idle_frame(20)})
    assert not any("top 100 warehouses" in b for b in small.bullets)
    assert any("idle waste this window" in b.lower() for b in small.bullets)
