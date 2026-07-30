"""DO-FIRST wave lock tests (v4.82.0).

Covers the trust/actionability fixes on the headline exec KPIs and the morning
surfaces: C1 (score fail-open), N1 (forecast partial-day low bias), C3/N14/N15
(freshness never-loaded + cadence-aware + named), N2 (undelivered criticals),
N3 (triage actionability + linking ids), C4/C7 (uncapped alert counts),
N6 (boss chart account time), N8 (score-driver nav), N9 (small-frame CSV defer).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from app.data import mart_sql
from app.logic.actions import triage_queue
from app.logic.forecast import month_end_projection
from app.logic.scoring import REQUIRED_SIGNAL_SOURCES, platform_score

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# C1 — platform score must not fail OPEN (outage suppressing failures != Healthy)
# ---------------------------------------------------------------------------
def test_c1_missing_required_source_is_incomplete_not_healthy():
    # The cardinal sin: every health signal read failed, so nothing adds penalty
    # and a naive score would be 100/Healthy. With coverage gating it is Incomplete.
    res = platform_score(signals={}, available=set())
    assert res.state == "Incomplete"
    assert res.score == 0
    assert res.state != "Healthy"


def test_c1_partial_coverage_still_incomplete():
    # Throughput loaded but alerts did not — a real critical could be hidden.
    res = platform_score(signals={"critical_alerts": 0}, available={"throughput"})
    assert res.state == "Incomplete"
    # C2/N5: the required health source is the fixed-window throughput read, not
    # the spend-windowed exec board.
    assert set(REQUIRED_SIGNAL_SOURCES) == {"throughput", "alerts"}


def test_c1_full_coverage_scores_normally():
    healthy = platform_score(signals={}, available={"throughput", "alerts"})
    assert healthy.state == "Healthy" and healthy.score == 100
    degraded = platform_score(signals={"critical_alerts": 9}, available={"throughput", "alerts"})
    assert degraded.state != "Incomplete" and degraded.score < 100


def test_c1_available_none_is_backward_compatible():
    # Callers that don't pass coverage (older code / retro history) still score.
    res = platform_score(signals={"critical_alerts": 1}, available=None)
    assert res.state != "Incomplete"


# ---------------------------------------------------------------------------
# C2 / N5 — score health pinned to a fixed window, required source repointed
# ---------------------------------------------------------------------------
def test_c2n5_score_health_reads_a_fixed_window():
    ov = _src("app/ui/pages/overview.py")
    assert "_SCORE_HEALTH_WINDOW_DAYS = 1" in ov
    assert "fact_query_window_summary(_SCORE_HEALTH_WINDOW_DAYS" in ov
    assert "fact_task_daily(_SCORE_HEALTH_WINDOW_DAYS" in ov
    # the spend-windowed board metrics no longer feed the score's queue/spill
    assert '_board_metric(board, "QUEUED_MINUTES")' not in ov
    assert '_board_metric(board, "SPILL_GB")' not in ov


def test_c2n5_required_source_repointed_and_wired_failclosed():
    # C1 fail-open guard: repointing the required source WITHOUT adding it to the
    # coverage set would silently zero the query/task/queue/spill penalties.
    from app.logic.scoring import REQUIRED_SIGNAL_SOURCES
    assert "throughput" in REQUIRED_SIGNAL_SOURCES and "board" not in REQUIRED_SIGNAL_SOURCES
    ov = _src("app/ui/pages/overview.py")
    assert '_available.add("throughput")' in ov  # both halves land together


# ---------------------------------------------------------------------------
# C11 / N7 — account-wide badges + storage/transfer exclusion disclosure
# ---------------------------------------------------------------------------
def test_c11_account_wide_badges_on_metering_kpis():
    ov = _src("app/ui/pages/overview.py")
    # 3 MTD returns in _mtd_pace_kpi + the MTD fallback + Projected month-end
    assert ov.count('"badge": "account-wide"') >= 5


def test_n7_storage_transfer_disclosure_on_overview_and_brief():
    assert "Storage and data-transfer bill separately" in _src("app/ui/pages/overview.py")
    assert "storage and data-transfer bill separately" in _src("app/ui/pages/brief.py")


# ---------------------------------------------------------------------------
# N10 — spend collapse gets a distinct identity from a spike
# ---------------------------------------------------------------------------
def test_n10_spend_collapse_distinct_from_spike():
    collapse = triage_queue(None, None, [{"label": "WH_DEAD", "value": 10.0, "z": -6.5}])
    row = collapse.iloc[0]
    assert row["KIND"] == "Spend collapse"
    assert "collapsed" in row["TITLE"]
    assert "stalled" in row["DETAIL"].lower()
    assert row["SEVERITY"] == "HIGH"  # abs(z) >= 5
    # a positive-z spike keeps the original "Spend anomaly" identity
    spike = triage_queue(None, None, [{"label": "WH_HOT", "value": 900.0, "z": 6.2}])
    assert spike.iloc[0]["KIND"] == "Spend anomaly"


# ---------------------------------------------------------------------------
# C18 — per-node timing reader + Operations panel
# ---------------------------------------------------------------------------
def test_c18_task_nodes_reader():
    from app.data import mart27_sql
    sql = mart27_sql.task_nodes(30, "ALFA")
    assert "MART_TASK_NODE_DAILY" in sql
    assert "P95_QUEUE_SEC" in sql and "P95_EXEC_SEC" in sql
    assert "ORDER BY P95_QUEUE_SEC DESC" in sql
    # no COMPANY column on the mart — must scope via DATABASE_NAME, not COMPANY=
    assert "COMPANY" not in sql
    assert "DATABASE_NAME" in sql


def test_c18_operations_panel_wired_mart_only():
    ops = _src("app/ui/pages/operations.py")
    assert "task_nodes(days, company" in ops
    assert "Per-node timing" in ops
    # mart-only: must NOT be wrapped in run_mart_first (no numerically-agreeing live leg)
    assert "run_mart_first(mart27_sql.task_nodes" not in ops


# ---------------------------------------------------------------------------
# N4 — Overview batches its two independent live first-paint reads
# ---------------------------------------------------------------------------
def test_n4_overview_batches_live_first_paint_reads():
    ov = _src("app/ui/pages/overview.py")
    assert "run_batch(" in ov
    assert 'open_alerts_{company}' in ov and '"key": "action_queue"' in ov
    # health_strip stays OUT of the batch (shared shell cache, r15 #14)
    assert 'run_batch' in ov and 'health_strip' not in ov.split("run_batch(", 1)[1].split("], page", 1)[0]
    # the batched action_queue result is reused, not re-read blindly
    assert '_live_pf.get("action_queue")' in ov


# ---------------------------------------------------------------------------
# N12 — telemetry/usage single-row INSERTs buffered into one multi-row flush
# ---------------------------------------------------------------------------
def test_n12_flushed_statement_passes_allow_list():
    from app.core.query import _statement_allowed
    usage_prefix = "INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_USAGE (PAGE, RENDER_MS, EVENT_KIND, IS_RERUN, USER_NAME) "
    tel_prefix = ("INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_QUERY_TELEMETRY "
                  "(PAGE, TIER, QUERY_KEY, ELAPSED_MS, ROWS_RETURNED, OK) ")
    for prefix, body in ((usage_prefix, "SELECT 'Overview', 5, 'page_visit', FALSE, CURRENT_USER()"),
                         (tel_prefix, "SELECT 'Overview', 'live', 'k', 5.0, 1, TRUE")):
        stmt = prefix + " UNION ALL ".join([body, body, body])
        ok, why = _statement_allowed(stmt)
        assert ok, why
        assert stmt.count(" UNION ALL ") == 2  # N rows -> N-1 joiners


def test_n12_semicolon_in_quoted_value_still_allowed():
    from app.core.query import _statement_allowed
    from app.core.sqlsafe import sql_literal
    prefix = "INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_USAGE (PAGE, EVENT_KIND, IS_RERUN, USER_NAME) "
    tricky = sql_literal("O'Brien; DROP")  # embedded quote + semicolon, escaped
    body = f"SELECT {tricky}, {sql_literal('csv;export')}, FALSE, CURRENT_USER()"
    stmt = prefix + " UNION ALL ".join([body, body])
    ok, why = _statement_allowed(stmt)
    assert ok, why  # the interior-semicolon guard blanks quoted literals first


def test_n12_buffer_accumulates_and_flushes(monkeypatch):
    import streamlit as st

    from app.core import query as q
    st.session_state.clear()
    sent: list[str] = []
    monkeypatch.setattr(q, "execute_statement_async", lambda sql, *, page: (sent.append(sql), True)[1])
    prefix = "INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_USAGE (PAGE, RENDER_MS) "
    for i in range(3):
        q._buffer_write(prefix, f"SELECT 'p{i}', {i}", off_flag="_ow_usage_off")
    assert not sent  # nothing sent until flush (below the 25-row auto-flush cap)
    q.flush_write_buffer()
    assert len(sent) == 1 and sent[0].count(" UNION ALL ") == 2  # one 3-row INSERT
    st.session_state.clear()


def test_n12_two_tables_flush_as_two_statements(monkeypatch):
    import streamlit as st

    from app.core import query as q
    st.session_state.clear()
    sent: list[str] = []
    monkeypatch.setattr(q, "execute_statement_async", lambda sql, *, page: (sent.append(sql), True)[1])
    q._buffer_write("INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_USAGE (PAGE, RENDER_MS) ", "SELECT 'p', 1")
    for i in range(5):
        q._buffer_write("INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_QUERY_TELEMETRY "
                        "(PAGE, TIER, QUERY_KEY, ELAPSED_MS, ROWS_RETURNED, OK) ",
                        f"SELECT 'p', 'live', 'k{i}', 1.0, 1, TRUE")
    q.flush_write_buffer()
    assert len(sent) == 2  # exactly one INSERT per target table+shape
    st.session_state.clear()


def test_n12_auto_flush_at_cap(monkeypatch):
    import streamlit as st

    from app.core import query as q
    st.session_state.clear()
    sent: list[str] = []
    monkeypatch.setattr(q, "execute_statement_async", lambda sql, *, page: (sent.append(sql), True)[1])
    prefix = "INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_USAGE (PAGE, RENDER_MS) "
    for i in range(q._WRITE_BUFFER_FLUSH_N):
        q._buffer_write(prefix, f"SELECT 'p', {i}", off_flag="_ow_usage_off")
    assert len(sent) == 1  # auto-flushed exactly at the cap
    assert sent[0].count(" UNION ALL ") == q._WRITE_BUFFER_FLUSH_N - 1
    st.session_state.clear()


def test_n12_producers_enqueue_not_direct_insert():
    assert "_buffer_write(" in _src("app/main.py")           # _log_usage
    assert "_buffer_write(" in _src("app/ui/components.py")  # log_ui_event
    assert "_buffer_write(" in _src("app/core/query.py")     # _persist_telemetry
    # flush wired at both ends of the rerun
    assert _src("app/main.py").count("flush_write_buffer()") >= 2


# ---------------------------------------------------------------------------
# N1 — forecasts must exclude today's PARTIAL day from the forward daily rate
# ---------------------------------------------------------------------------
def test_n1_forecast_excludes_partial_today_from_rate():
    days = [date(2026, 7, 9 + i) for i in range(6)]  # six complete days at $100
    rows = [{"DAY": d, "USD": 100.0} for d in days]
    rows.append({"DAY": date(2026, 7, 15), "USD": 10.0})  # today, partial
    f = month_end_projection(pd.DataFrame(rows), date(2026, 7, 15), engine="linear")
    assert f.ok
    # Rate is built from complete days only ($100), NOT dragged down by today's $10
    # (the old inclusive baseline would have averaged to ~$87).
    assert f.daily_rate_usd == 100.0
    # MTD still counts today's actual spend.
    assert f.mtd_usd == 610.0


def test_n1_forecast_source_comment_present():
    assert "N1:" in _src("app/logic/forecast.py")
    assert 'frame["DAY"] < today' in _src("app/logic/forecast.py")
    contract = _src("app/ui/pages/cost_parts/contract.py")
    assert contract.count("< account_today()") >= 1  # burn tail + planner both fixed
    assert 'cydf["DAY"] < today' in contract


# ---------------------------------------------------------------------------
# C3 / N14 / N15 — freshness: never-loaded counts, cadence-aware, named
# ---------------------------------------------------------------------------
def test_c3_stale_count_includes_never_loaded():
    hs = mart_sql.health_strip()
    assert "LAST_LOAD_TS IS NULL OR" in hs  # STALE_SOURCES arm counts NULL ts


def test_n14_stalest_is_cadence_aware_and_n15_named():
    hs = mart_sql.health_strip()
    assert "STALEST_SOURCE_NAME" in hs      # N15: names the source
    assert "MAX_BY" in hs and "URGENCY" in hs
    assert "1e9" in hs                       # never-loaded sentinel outranks all
    # cadence limits come from THRESHOLDS, not a flat 26h/3h pair
    assert "'%DAILY%' OR SOURCE_NAME LIKE '%METERING%'" in hs


def test_c3_board_renders_not_loaded_state():
    cr = _src("app/ui/pages/control_room.py")
    assert '"NOT LOADED"' in cr
    assert 'df["NOT_LOADED"] = df["LAST_LOAD_TS"].isna()' in cr
    assert "STATUS" in cr


def test_n15_brief_stalest_label():
    from app.ui.pages.brief import _stalest_label
    assert _stalest_label({"STALEST_SOURCE_H": "7.0", "STALEST_SOURCE_NAME": "FACT_QUERY_HOURLY"}) == \
        "FACT_QUERY_HOURLY 7.0h"
    assert _stalest_label({"STALEST_SOURCE_H": "-1", "STALEST_SOURCE_NAME": "FACT_STORAGE_DAILY"}) == \
        "FACT_STORAGE_DAILY never loaded"
    assert _stalest_label({"STALEST_SOURCE_H": "-1", "STALEST_SOURCE_NAME": "none"}) == "no data yet"


# ---------------------------------------------------------------------------
# N2 — undelivered criticals surfaced on the morning surfaces
# ---------------------------------------------------------------------------
def test_n2_undelivered_arm_in_strip():
    hs = mart_sql.health_strip()
    assert "'UNDELIVERED_CRITICAL'" in hs
    assert "ALERT_DELIVERIES" in hs and "NOT EXISTS" in hs
    assert "-30, CURRENT_TIMESTAMP()" in hs  # 30+ min old


def test_n2_surfaced_on_shell_brief_and_control_room():
    assert "UNDELIVERED_CRITICAL" in _src("app/main.py")
    assert "reached nobody" in _src("app/main.py")
    assert "UNDELIVERED_CRITICAL" in _src("app/ui/pages/brief.py")
    assert "UNDELIVERED_CRITICAL" in _src("app/ui/pages/control_room.py")


# ---------------------------------------------------------------------------
# N3 — triage queue carries linking ids and is selectable
# ---------------------------------------------------------------------------
def test_n3_triage_carries_event_and_rule_ids():
    alerts = pd.DataFrame([{"SEVERITY": "CRITICAL", "TITLE": "t", "DETAIL": "d",
                            "RAISED_AT": "2026-07-07", "EVENT_ID": "E1", "RULE_ID": "R1"}])
    q = triage_queue(alerts, None, None)
    assert "EVENT_ID" in q.columns and "RULE_ID" in q.columns
    row = q.iloc[0]
    assert row["EVENT_ID"] == "E1" and row["RULE_ID"] == "R1"


def test_n3_triage_non_alert_rows_have_empty_ids():
    tasks = pd.DataFrame([{"TASK_NAME": "T", "DATABASE_NAME": "DB", "SCHEMA_NAME": "S",
                           "FAILED": 2, "LAST_ERROR": "", "DAY": "2026-07-06"}])
    q = triage_queue(None, tasks, [{"label": "WH", "value": 1.0, "z": 6.0}])
    assert set(q["EVENT_ID"]) == {""}  # task + anomaly rows carry no event id


def test_n3_control_room_queue_is_actionable():
    cr = _src("app/ui/pages/control_room.py")
    assert 'selectable_table(queue[' in cr
    assert "request_navigation" in cr


# ---------------------------------------------------------------------------
# C4 / C7 — alert KPI tiles count with one uncapped aggregate
# ---------------------------------------------------------------------------
def test_c4c7_uncapped_severity_counts():
    sql = mart_sql.open_alert_severity_counts("ALFA")
    assert "COUNT_IF(UPPER(SEVERITY) = 'CRITICAL')" in sql
    assert "COUNT(*) AS TOTAL" in sql
    assert "STATUS IN ('OPEN', 'ACK')" in sql
    # company filter keeps account-level rows
    assert "UPPER(COMPANY) = 'ALL'" in sql


def test_c4c7_alerts_page_uses_aggregate():
    al = _src("app/ui/pages/alerts.py")
    assert "open_alert_severity_counts" in al
    assert "total_n" in al


# ---------------------------------------------------------------------------
# N6 / N8 — boss chart account time + score-driver navigation
# ---------------------------------------------------------------------------
def test_n6_boss_chart_uses_account_today():
    ov = _src("app/ui/pages/overview.py")
    assert 'account_today().strftime("%Y-%m")' in ov


def test_n8_score_driver_navigation():
    ov = _src("app/ui/pages/overview.py")
    assert "_SCORE_DRIVER_NAV" in ov
    assert "Investigate →" in ov
    # every live driver name maps to a page
    from app.ui.pages.overview import _SCORE_DRIVER_NAV
    for driver in ("Over budget", "Critical alerts", "Query failures",
                   "Stale telemetry", "Owner queue"):
        assert driver in _SCORE_DRIVER_NAV


# ---------------------------------------------------------------------------
# N9 — small-frame CSV serialized once per content, not every rerun
# ---------------------------------------------------------------------------
def test_n9_small_frame_csv_is_memoized():
    comp = _src("app/ui/components.py")
    assert "_ow_dlsmall" in comp
    assert "N9:" in comp
    # the memoized branch reuses cached bytes when the fingerprint matches
    assert '_slot.get("fp") == _fp' in comp
