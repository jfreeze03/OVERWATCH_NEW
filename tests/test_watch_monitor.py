"""Watch automation (owner ask 2026-08-17): a watched entity now surfaces its own
cost spike/drop + health-grade status, instead of waiting passively in the list."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.logic.watch_monitor import watch_summary, watched_status

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _daily(name: str, steady: float, outlier: float, at_index: int = 14) -> pd.DataFrame:
    """15 days of steady credits for one warehouse with a single outlier day —
    enough history to clear the anomaly materiality gates (>=10 active days)."""
    days = pd.date_range("2026-07-01", periods=15, freq="D")
    rows = []
    for i, day in enumerate(days):
        credits = outlier if i == at_index else steady + (i % 3 - 1)  # tiny jitter
        rows.append({"WAREHOUSE_NAME": name, "DAY": day.strftime("%Y-%m-%d"),
                     "CREDITS_TOTAL": float(credits)})
    return pd.DataFrame(rows)


def _watch(etype: str, key: str, label: str = "") -> dict:
    return {"ENTITY_TYPE": etype, "ENTITY_KEY": key, "LABEL": label or key}


# --------------------------------------------------------------- cost ----

def test_watched_warehouse_cost_spike_is_flagged():
    wl = pd.DataFrame([_watch("WAREHOUSE", "WH_SPIKE")])
    daily = _daily("WH_SPIKE", steady=20.0, outlier=200.0)   # ~$73/day -> ~$736 spike
    out = watched_status(wl, daily, None, rate=3.68)
    row = out.iloc[0]
    assert bool(row["ATTENTION"]) and row["SEVERITY"] == "warn"
    assert "spend spike" in row["STATUS"] and "z +" in row["STATUS"]


def test_watched_warehouse_cost_drop_reads_as_drop_not_spike():
    wl = pd.DataFrame([_watch("WAREHOUSE", "WH_DROP")])
    daily = _daily("WH_DROP", steady=135.0, outlier=1.0)     # ~$497/day collapses to ~$4
    out = watched_status(wl, daily, None, rate=3.68)
    row = out.iloc[0]
    assert bool(row["ATTENTION"]) and "spend drop" in row["STATUS"]
    assert "z -" in row["STATUS"]        # signed z carries the direction


# ------------------------------------------------------------- health ----

def test_watched_warehouse_health_grade_below_healthy_is_flagged():
    wl = pd.DataFrame([_watch("WAREHOUSE", "WH_SICK")])
    health = pd.DataFrame([{"WAREHOUSE_NAME": "WH_SICK", "SCORE": 55, "GRADE": "Degraded"}])
    out = watched_status(wl, None, health)
    row = out.iloc[0]
    assert bool(row["ATTENTION"]) and "health: Degraded" in row["STATUS"]
    assert row["SEVERITY"] == "warn"


def test_cost_and_health_stack_in_one_status():
    wl = pd.DataFrame([_watch("WAREHOUSE", "WH_BOTH")])
    daily = _daily("WH_BOTH", steady=20.0, outlier=200.0)
    health = pd.DataFrame([{"WAREHOUSE_NAME": "WH_BOTH", "SCORE": 45, "GRADE": "At risk"}])
    out = watched_status(wl, daily, health)
    status = out.iloc[0]["STATUS"]
    assert "spend spike" in status and "health: At risk" in status   # canonical casing kept
    assert out.iloc[0]["SEVERITY"] == "warn"


def test_watch_grade_is_a_soft_watch_not_a_warn():
    wl = pd.DataFrame([_watch("WAREHOUSE", "WH_SOFT")])
    health = pd.DataFrame([{"WAREHOUSE_NAME": "WH_SOFT", "SCORE": 78, "GRADE": "Watch"}])
    out = watched_status(wl, None, health)
    row = out.iloc[0]
    assert bool(row["ATTENTION"]) and row["SEVERITY"] == "watch"
    assert "health: Watch" in row["STATUS"]


def test_healthy_steady_warehouse_is_not_flagged():
    wl = pd.DataFrame([_watch("WAREHOUSE", "WH_OK")])
    daily = _daily("WH_OK", steady=20.0, outlier=21.0)   # no outlier worth flagging
    health = pd.DataFrame([{"WAREHOUSE_NAME": "WH_OK", "SCORE": 95, "GRADE": "Healthy"}])
    out = watched_status(wl, daily, health)
    row = out.iloc[0]
    assert not bool(row["ATTENTION"]) and row["STATUS"] == "steady"


# ------------------------------------------------- non-warehouse & shape ----

def test_non_warehouse_watch_passes_through_without_a_false_signal():
    # cost + health are warehouse-grain; a watched query family / product must not
    # borrow a warehouse's anomaly. It stays steady here (covered elsewhere).
    wl = pd.DataFrame([_watch("QUERY", "WH_SPIKE", "nightly ETL")])
    daily = _daily("WH_SPIKE", steady=20.0, outlier=200.0)
    out = watched_status(wl, daily, None)
    row = out.iloc[0]
    # not evaluated here (blank), and never borrows the warehouse's anomaly.
    assert not bool(row["ATTENTION"]) and row["STATUS"] == ""


def test_attention_rows_sort_first_and_summary_counts():
    wl = pd.DataFrame([
        _watch("WAREHOUSE", "WH_OK"),
        _watch("WAREHOUSE", "WH_SICK"),
        _watch("WAREHOUSE", "WH_SPIKE"),
    ])
    daily = pd.concat([
        _daily("WH_OK", 20.0, 21.0),
        _daily("WH_SPIKE", 20.0, 200.0),
    ], ignore_index=True)
    health = pd.DataFrame([
        {"WAREHOUSE_NAME": "WH_OK", "SCORE": 95, "GRADE": "Healthy"},
        {"WAREHOUSE_NAME": "WH_SICK", "SCORE": 40, "GRADE": "At risk"},
    ])
    out = watched_status(wl, daily, health)
    assert bool(out.iloc[0]["ATTENTION"]) and bool(out.iloc[1]["ATTENTION"])
    assert not bool(out.iloc[-1]["ATTENTION"])       # steady sinks to the bottom
    s = watch_summary(out)
    assert s["watched"] == 3 and s["attention"] == 2 and s["warn"] == 2


def test_empty_watchlist_is_safe():
    out = watched_status(pd.DataFrame(), None, None)
    assert out.empty and list(out.columns) == \
        ["ENTITY_TYPE", "ENTITY_KEY", "LABEL", "ATTENTION", "STATUS", "SEVERITY"]
    assert watched_status(None, None, None).empty
    assert watch_summary(None) == {"watched": 0, "attention": 0, "warn": 0}
    assert watch_summary(pd.DataFrame()) == {"watched": 0, "attention": 0, "warn": 0}


# ------------------------------------------------------------- wiring ----

def test_watch_monitor_is_wired_into_the_surfaces():
    wb = _src("app/ui/workbench.py")
    # Watchlist tab annotates + banners; the Brief badge is the shared entry point.
    assert "watched_status(" in wb and "watch_summary(" in wb
    assert "def watched_attention(" in wb and "def render_watch_badge(" in wb
    brief = _src("app/ui/pages/brief.py")
    assert "render_watch_badge(" in brief
