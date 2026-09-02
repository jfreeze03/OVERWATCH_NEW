"""Regression locks for the round-5 bug hunt (v4.420.0)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import sqlglot

from app.core.identity import content_request_key, idempotency_key
from app.data import chargeback_sql, cost_sql, ops_sql
from app.logic.formulas import humanize_age, humanize_minutes_ago

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- sql-agg fin-1 (HIGH): tag_coverage must not reference same-SELECT aliases ----
def test_tag_coverage_inlines_the_sum_not_the_select_alias():
    sql = cost_sql.tag_coverage(30, "ALFA", database="DB1")
    sqlglot.parse(sql, dialect="snowflake")                 # (lenient — semantic bug below)
    # TAGGED_PCT repeats the SUM expression rather than referencing the EXEC_SEC /
    # UNTAGGED_EXEC_SEC aliases defined in the same flat SELECT list (invalid in Snowflake).
    assert "1 - SUM(IFF(NULLIF(QUERY_TAG" in sql
    assert "1 - UNTAGGED_EXEC_SEC / NULLIF(EXEC_SEC" not in sql
    # HAVING/ORDER BY alias refs are allowed in Snowflake and stay.
    assert "HAVING EXEC_SEC > 60" in sql and "ORDER BY UNTAGGED_EXEC_SEC DESC" in sql


# --- tz-window fin-2 (MED): storage_account_truth_live honors Last-month bounds ----
def test_storage_account_truth_live_is_bounds_aware():
    b = (date(2026, 8, 1), date(2026, 9, 1))
    bounded = cost_sql.storage_account_truth_live(31, bounds=b)
    assert "2026-08-01" in bounded and "2026-09-01" in bounded
    assert "DATEADD('day', -31" not in bounded
    assert "DATEADD('day', -31, CURRENT_DATE())" in cost_sql.storage_account_truth_live(31)


# --- tz-window fin-1 (MED): the per-db storage panel has a bounded (last-month) branch
def test_storage_by_db_has_a_last_month_branch():
    s = _src("app/ui/pages/cost_parts/spend.py")
    assert "if bounds is not None:" in s.split("def _storage_tab(", 1)[1][:2500]
    assert 'storage_lastmonth_' in s
    assert 'title="$/month by database (last month)"' in s


# --- tz-window fin-3 (LOW): lock_contention keeps its 7d cost cap under bounds -------
def test_lock_contention_clamps_bounds_to_the_cost_cap():
    b = (date(2026, 8, 1), date(2026, 9, 1))
    lc = ops_sql.lock_contention(7, bounds=b)
    # bounds intersected with the last 7 days of the bounded month, not the whole month
    assert "2026-08-25" in lc and "2026-09-01" in lc
    assert "2026-08-01" not in lc


# --- empty-error fin-1/2 (MED): verdicts add a degraded-data Signal, no false green --
def test_operations_and_cost_verdicts_guard_degraded_reads():
    ops = _src("app/ui/pages/operations.py")
    assert 'verdict.Signal("warn", "platform telemetry unavailable' in ops
    assert 'verdict.Signal("warn", "source freshness unavailable")' in ops
    cost = _src("app/ui/pages/cost.py")
    assert "if not _exh.usable():" in cost
    assert '"contract runway unavailable' in cost


# --- concurrency fin-1 (LOW): content_request_key is time-independent -----------------
def test_content_request_key_is_stable_across_minutes(monkeypatch):
    import app.core.identity as ident
    monkeypatch.setattr(ident, "viewer_name", lambda: "DBA1")
    k1 = content_request_key("ui_action", "a|IN_PROGRESS|x")
    k2 = content_request_key("ui_action", "a|IN_PROGRESS|x")
    assert k1 == k2 and len(k1) == 32                       # deterministic, no minute bucket
    assert content_request_key("ui_action", "a|DONE|x") != k1   # different content -> different key
    # the two content-signature callers use the time-independent helper, not idempotency_key
    assert "content_request_key(" in _src("app/ui/workbench.py")
    assert "content_request_key(" in _src("app/ui/decision_studio.py")


def test_idempotency_key_still_minute_bucketed_for_double_click_dedup(monkeypatch):
    # the alert double-click path deliberately keeps the minute bucket
    import app.core.identity as ident
    monkeypatch.setattr(ident, "viewer_name", lambda: "DBA1")
    from app.logic import formulas
    monkeypatch.setattr(formulas, "account_now", lambda: datetime(2026, 9, 1, 10, 4))
    a = idempotency_key("ALERT_ACK", "e1")
    monkeypatch.setattr(formulas, "account_now", lambda: datetime(2026, 9, 1, 10, 5))
    b = idempotency_key("ALERT_ACK", "e1")
    assert a != b                                           # minute bucket still differentiates
    assert "idempotency_key(" in _src("app/ui/pages/alerts.py")


# --- concurrency fin-2 (LOW): the work-item comment clears after a successful save ----
def test_workbench_clears_comment_after_save():
    wb = _src("app/ui/workbench.py")
    save = wb.split("Save work item", 1)[1]
    assert 'st.session_state.pop(f"action_note_{action_id}", None)' in save


# --- sql-agg fin-2 (LOW): role->department join dedups the map like _MAP_JOIN ---------
def test_role_department_join_dedups_the_map():
    sql = chargeback_sql.role_department_map_join(30, "ALFA")
    sqlglot.parse(sql, dialect="snowflake")
    assert "QUALIFY ROW_NUMBER() OVER (PARTITION BY UPPER(NAME)" in sql
    assert "MAP_TYPE = 'ROLE'" in sql


# --- formula-edge fin-2 (LOW): age humanizers promote at the unit boundary -----------
def test_age_humanizers_promote_at_the_boundary():
    now = datetime(2026, 9, 1, 12, 0, 0)
    assert humanize_age(now - timedelta(seconds=3599), now) == "1h ago"    # not "60m ago"
    assert humanize_age(now - timedelta(seconds=86399), now) == "1d ago"   # not "24h ago"
    assert humanize_age(now - timedelta(seconds=90), now) == "2m ago"      # mid-range intact
    assert humanize_minutes_ago(59.7) == "1h ago"
    assert humanize_minutes_ago(1439) == "1d ago"
    assert humanize_minutes_ago(59) == "59m ago"
