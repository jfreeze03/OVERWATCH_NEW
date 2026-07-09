"""Render-path + logic stress harness (opt-in: OW_STRESS=1).

What this measures (sandbox-relative, honest scope):
- Whole-page renders with realistic MAX-size frames behind key-aware stubs
  (Alerts 1,000 events = SQL cap; Operations 5,000 queries = row cap;
  Overview exec board at density) — exercises tables, charts, KPI cards,
  fragments, and the safe_page boundary at volume.
- Component pipeline at 100 → 50,000 rows: proves the Styler cap keeps
  worst-case tables bounded.
- Pure logic at scale (100k-point anomaly scans, 3y forecasts).

What it deliberately does NOT measure: Snowflake-side latency, SiS
concurrency, warehouse queueing — production owns those via fleet
telemetry (Admin → Performance) and OPS_SLOW_RENDER.

Run: make stress   (or OW_STRESS=1 pytest tests/test_stress.py -q -s)
"""

from __future__ import annotations

import os
import time

import pandas as pd
import pytest

if not os.environ.get("OW_STRESS"):
    pytest.skip("stress harness is opt-in: OW_STRESS=1", allow_module_level=True)

st = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from app.core.result import QueryResult  # noqa: E402

_TIMINGS: list[tuple[str, float]] = []


def _clock(label: str, started: float) -> float:
    elapsed = time.perf_counter() - started
    _TIMINGS.append((label, elapsed))
    print(f"  {label:<52} {elapsed * 1000:>8.0f} ms")
    return elapsed


def _events_frame(n: int) -> pd.DataFrame:
    sev = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
    return pd.DataFrame({
        "EVENT_ID": [f"evt-{i:06d}" for i in range(n)],
        "RULE_ID": ["COST_WH_DAILY_CREDITS" if i % 3 else "PERF_SPILL_GB" for i in range(n)],
        "RAISED_AT": pd.date_range("2026-06-01", periods=n, freq="min"),
        "COMPANY": ["ALFA" if i % 2 else "Trexis" for i in range(n)],
        "SEVERITY": [sev[i % 4] for i in range(n)],
        "TITLE": [f"Warehouse WH_TEST_{i % 40} daily credits 42.{i % 90} over threshold" for i in range(n)],
        "DETAIL": ["detail " * 10 for _ in range(n)],
        "METRIC_VALUE": [float(i % 500) for i in range(n)],
        "STATUS": ["OPEN" if i % 4 else "ACK" for i in range(n)],
        "ACK_BY": ["" for _ in range(n)],
        "ACK_AT": [pd.NaT for _ in range(n)],
    })


def _queries_frame(n: int) -> pd.DataFrame:
    return pd.DataFrame({
        "QUERY_ID": [f"01b{i:029d}" for i in range(n)],
        "USER_NAME": [f"USER_{i % 200}" for i in range(n)],
        "WAREHOUSE_NAME": [f"WH_TEST_{i % 40}" for i in range(n)],
        "ELAPSED_SEC": [float(i % 900) / 3 for i in range(n)],
        "QUEUED_SEC": [float(i % 120) for i in range(n)],
        "SPILL_REMOTE_GB": [float(i % 50) / 10 for i in range(n)],
        "EXECUTION_STATUS": ["SUCCESS" if i % 9 else "FAIL" for i in range(n)],
        "QUERY_PREVIEW": ["SELECT col_a, col_b FROM some.table WHERE x = 42 -- " + "pad" * 20
                          for _ in range(n)],
    })


def _board_frame(n_days: int = 90, n_drivers: int = 40) -> pd.DataFrame:
    days = pd.date_range("2026-04-01", periods=n_days, freq="D")
    rows = [{"PANEL": "DAILY_SPEND", "METRIC": "SPEND", "DIMENSION": "ALL",
             "PERIOD_START": d, "VALUE": 100.0, "VALUE_USD": 368.0 + i} for i, d in enumerate(days)]
    rows += [{"PANEL": "COST_DRIVER", "METRIC": "WH", "DIMENSION": f"WH_TEST_{i}",
              "PERIOD_START": days[0], "VALUE": 10.0 * i, "VALUE_USD": 36.8 * i}
             for i in range(n_drivers)]
    for metric, val in (("QUERIES", 250000), ("FAILED_QUERIES", 1200), ("QUEUED_MINUTES", 90),
                        ("SPILL_GB", 12), ("TASK_RUNS", 4000), ("TASK_FAILURES", 25),
                        ("CREDITS", 4200)):
        rows.append({"PANEL": "KPI", "METRIC": metric, "DIMENSION": "ALL",
                     "PERIOD_START": days[-1], "VALUE": float(val), "VALUE_USD": float(val)})
    return pd.DataFrame(rows)


_KEYED_FRAMES = {
    "alert_events": lambda: _events_frame(1000),   # SQL cap for this feed
    "q_top": lambda: _queries_frame(5000),         # app-wide row cap
    "exec_board": lambda: _board_frame(),
    "fact_daily_45": lambda: pd.DataFrame({
        "DAY": pd.date_range("2026-05-01", periods=60, freq="D"),
        "CREDITS_BILLED": [40.0 + i for i in range(60)],
    }),
    "spark_activity": lambda: pd.DataFrame({
        "DAY": pd.date_range("2026-06-23", periods=14, freq="D"),
        "QUERIES": [1000 + i for i in range(14)],
        "FAILS": [i % 7 for i in range(14)],
    }),
    "ops_spark_activity": lambda: pd.DataFrame({
        "DAY": pd.date_range("2026-06-23", periods=14, freq="D"),
        "QUERIES": [1000 + i for i in range(14)],
        "FAILS": [i % 7 for i in range(14)],
    }),
    "open_alerts": lambda: _events_frame(500),
    "action_queue": lambda: pd.DataFrame({
        "SEVERITY": ["HIGH"] * 200, "TITLE": [f"Action {i}" for i in range(200)],
        "OWNER": ["DBA"] * 200, "DUE_DATE": pd.date_range("2026-07-08", periods=200, freq="D"),
        "ESTIMATED_USD": [100.0 * i for i in range(200)], "STATUS": ["OPEN"] * 200,
        "KIND": ["Idle warehouse"] * 200, "DETAIL": ["d"] * 200,
        "SOURCE": ["FACT"] * 200, "RAISED_AT": pd.date_range("2026-07-01", periods=200, freq="h"),
    }),
}


def _keyed_run(*_args, **kwargs):
    key = str(kwargs.get("key", ""))
    for prefix, maker in _KEYED_FRAMES.items():
        if key.startswith(prefix):
            return QueryResult(df=maker(), ok=True, source=str(kwargs.get("source", "stress")))
    return QueryResult(df=pd.DataFrame(), ok=True, source=str(kwargs.get("source", "stub")))


@pytest.fixture(autouse=True)
def _stub_runtime(monkeypatch):
    import app.main as main_mod
    from app.config import DEFAULT_SETTINGS
    from app.ui import ai_panel, components
    from app.ui.pages import admin, alerts, control_room, cost, operations, overview, security
    from app.ui.pages.cost_parts import ai_chargeback, contract, optimize, spend

    monkeypatch.setattr(main_mod, "connection_available", lambda: True)
    monkeypatch.setattr(main_mod, "current_role", lambda: "SNOW_SYSADMINS")
    monkeypatch.setattr(main_mod, "run", _keyed_run)
    monkeypatch.setattr(main_mod, "execute_statement", lambda *_a, **_k: (True, "stub"))
    monkeypatch.setattr(main_mod, "execute_statement_async", lambda *_a, **_k: True)
    settings = dict(DEFAULT_SETTINGS)
    settings["_source"] = "stress-stub"
    settings["MONTHLY_BUDGET_USD"] = 100_000.0
    monkeypatch.setattr(components, "load_settings", lambda _page: dict(settings))
    for module in (overview, control_room, cost, operations, alerts, security, admin,
                   spend, contract, ai_chargeback, optimize):
        if hasattr(module, "run"):
            monkeypatch.setattr(module, "run", _keyed_run)
        if hasattr(module, "run_batch"):
            monkeypatch.setattr(module, "run_batch", lambda *_a, **_k: None)  # serial path
        if hasattr(module, "execute_statement"):
            monkeypatch.setattr(module, "execute_statement", lambda *_a, **_k: (True, "stub"))
        if hasattr(module, "current_role"):
            monkeypatch.setattr(module, "current_role", lambda: "SNOW_SYSADMINS")
        if hasattr(module, "load_settings"):
            monkeypatch.setattr(module, "load_settings", lambda _page: dict(settings))
    monkeypatch.setattr(ai_panel, "cortex_complete", lambda *a, **k: (True, "stub"))


def _entry():
    import app.main

    app.main.main()


def _render_page(page: str) -> AppTest:
    at = AppTest.from_function(_entry, default_timeout=60)
    at.run()
    if page != at.radio(key="_ow_nav_radio").value:
        at.radio(key="_ow_nav_radio").set_value(page)
        at.run()
    return at


# ---------------------------------------------------------------------------
# Whole pages under load
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("page,budget_s", [("Alerts", 30), ("Operations", 30), ("Overview", 30)])
def test_page_under_load(page, budget_s):
    started = time.perf_counter()
    at = _render_page(page)
    elapsed = _clock(f"page: {page} (max-size frames)", started)
    assert not at.exception, f"{page}: {at.exception}"
    assert elapsed < budget_s, f"{page} render {elapsed:.1f}s blew the {budget_s}s sandbox budget"


def test_alerts_interaction_rerun_under_load():
    at = _render_page("Alerts")
    started = time.perf_counter()
    at.run()  # full rerun with 1,000 open events on screen
    _clock("page: Alerts rerun (warm)", started)
    assert not at.exception


# ---------------------------------------------------------------------------
# Component pipeline at scale
# ---------------------------------------------------------------------------

_TABLE_SCRIPT = """
import pandas as pd
from app.ui.components import styled_table

n = {rows}
frame = pd.DataFrame({{
    "WAREHOUSE_NAME": [f"WH_TEST_{{i % 50}}" for i in range(n)],
    "STATUS": ["OPEN" if i % 3 else "RESOLVED" for i in range(n)],
    "SEVERITY": ["HIGH" if i % 4 else "LOW" for i in range(n)],
    "SPEND_USD": [float(i) * 1.5 for i in range(n)],
    "CREDITS_TOTAL": [float(i) / 3 for i in range(n)],
    "IDLE_PCT": [float(i % 100) for i in range(n)],
    "QUERY_COUNT": list(range(n)),
    "RAISED_AT": pd.date_range("2026-01-01", periods=n, freq="min"),
}})
styled_table(frame)
"""


@pytest.mark.parametrize("rows", [100, 1500, 5000, 50000])
def test_styled_table_scales(rows):
    started = time.perf_counter()
    at = AppTest.from_string(_TABLE_SCRIPT.format(rows=rows), default_timeout=120)
    at.run()
    elapsed = _clock(f"styled_table: {rows:>6,} rows", started)
    assert not at.exception
    # the Styler cap is the whole point: 50k must not be dramatically worse
    # than 5k (Styler off above 1,500 rows)
    if rows == 50000:
        assert elapsed < 25, f"50k-row table took {elapsed:.1f}s — Styler cap regressed?"


def test_charts_at_density():
    def _app():
        import pandas as pd

        from app.ui import charts
        days = pd.DataFrame({"DAY": pd.date_range("2025-07-07", periods=365, freq="D"),
                             "USD": [100.0 + i for i in range(365)]})
        charts.spend_trend(days, daily_budget_usd=250.0)
        ev = pd.DataFrame({
            "AT": pd.date_range("2026-07-01", periods=2000, freq="4min"),
            "EVENT_TYPE": ["ALERT" if i % 2 else "DDL" for i in range(2000)],
            "SEVERITY": ["HIGH" if i % 3 else "INFO" for i in range(2000)],
            "LABEL": [f"e{i}" for i in range(2000)],
        })
        charts.event_timeline(ev)
        hm = pd.DataFrame({
            "ROW": [f"WH_{i % 45}" for i in range(45 * 24)],
            "HOUR": [i % 24 for i in range(45 * 24)],
            "VALUE": [float(i % 90) for i in range(45 * 24)],
        })
        charts.hour_heatmap(hm, "ROW", "HOUR", "VALUE")  # 45 rows -> capped at 20

    started = time.perf_counter()
    at = AppTest.from_function(_app, default_timeout=120)
    at.run()
    _clock("charts: 365d trend + 2k timeline + heatmap cap", started)
    assert not at.exception


# ---------------------------------------------------------------------------
# Pure logic at scale (no Streamlit runtime)
# ---------------------------------------------------------------------------

def test_logic_layer_at_scale():
    from app.logic.anomaly import flag_anomalies, robust_zscores
    from app.logic.forecast import month_end_projection
    from app.logic.formulas import account_today, allocate_by_share
    from app.logic.sizing import simulate_scenario
    from app.ui.components import severity_sort

    started = time.perf_counter()
    series = pd.Series([float(i % 997) for i in range(100_000)])
    robust_zscores(series)
    _clock("logic: robust_zscores 100k points", started)

    started = time.perf_counter()
    grouped = pd.DataFrame({
        "WH": [f"WH_{i % 50}" for i in range(100_000)],
        "USD": [float(i % 1250) for i in range(100_000)],
    })
    flag_anomalies(grouped, "USD", group_col="WH")
    _clock("logic: flag_anomalies 100k x 50 groups", started)

    started = time.perf_counter()
    daily = pd.DataFrame({"DAY": pd.date_range("2023-07-07", periods=1095, freq="D"),
                          "USD": [250.0 + (i % 90) for i in range(1095)]})
    month_end_projection(daily, account_today(), engine="seasonal")
    _clock("logic: 3y seasonal forecast", started)

    started = time.perf_counter()
    allocate_by_share(1_000_000.0, [float(i % 100) for i in range(10_000)])
    _clock("logic: allocate 10k weights", started)

    started = time.perf_counter()
    for i in range(10_000):
        simulate_scenario(size="MEDIUM", credits_window=100.0 + i, idle_credits_window=20.0,
                          window_days=30, rate_usd=3.68, size_delta=(i % 5) - 2,
                          autosuspend_now_s=600, autosuspend_new_s=60)
    _clock("logic: 10k what-if simulations", started)

    started = time.perf_counter()
    severity_sort(_events_frame(50_000))
    _clock("logic: severity_sort 50k events", started)


def test_zzz_print_summary():
    print("\n===== STRESS SUMMARY (sandbox-relative) =====")
    for label, secs in _TIMINGS:
        print(f"  {label:<52} {secs * 1000:>8.0f} ms")
    print("=" * 46)
