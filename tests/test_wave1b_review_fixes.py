"""Regression locks for the adversarial review of Next-Fifty wave 1b (v4.589.0) — one test per confirmed
finding that the per-rec suites don't already cover."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import streamlit as st

from app.core.result import QueryResult
from app.data import change_impact_sql, etl_control_sql, mart_sql, security_sql
from app.logic.actions import deferred_summary
from app.logic.email_path import email_path_verdict
from app.logic.identity_auth import VERDICT_WILL_BREAK, classify_auth_readiness
from app.logic.insights import annotate_proc_changes, latest_proc_changes
from app.logic.remediation import autobook_books_change

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_overdue_grades_every_expected_night_since_the_anchor():
    # a weekday-only starter that last ran Friday: Monday's missed kickoff must be caught, not just anchor+1
    sql = etl_control_sql.cycle_night_health_scan("DB.S.CONTROL_STATUS", start_workflow="WF_START")
    assert "missed AS (" in sql and "CROSS JOIN missed m" in sql
    assert "BETWEEN DATEADD('day', -6, a.CYCLE_DATE)" in sql
    assert "AND DATEADD('day', -7, DATE(DATEADD('hour', -12, CURRENT_TIMESTAMP())))" in sql
    assert "DATEADD('second', 7 * 86400 + 7200, p.CYCLE_START_AT) < CURRENT_TIMESTAMP()" in sql
    assert "LEFT JOIN cyc p ON p.CYCLE_DATE = DATEADD('day', -6, a.CYCLE_DATE)" not in sql
    sqlglot = pytest.importorskip("sqlglot")
    sqlglot.parse_one(sql, read="snowflake")


@pytest.mark.parametrize(("lever", "old", "want"), [
    ("AUTO_SUSPEND", 600, True), ("AUTO_SUSPEND", 0, False), ("AUTO_SUSPEND", None, False),
    ("MAX_CLUSTERS", 4, True), ("RESIZE", "LARGE", True), ("RESIZE", "X-Large", True),
    ("RESIZE", "5XLARGE", False), ("RESIZE", "6X-LARGE", False), ("SCHEDULE", 600, False),
])
def test_autobook_mirror_matches_the_v145_direction_filters(lever, old, want):
    assert autobook_books_change(lever, old) is want


def test_optimize_books_what_the_scan_cannot():
    src = _src("app/ui/pages/cost_parts/optimize.py")
    assert 'remediation.autobook_books_change("RESIZE", _cur_size)' in src
    assert "if ok and est_sz > 0 and not _sz_autobooked:" in src


def test_alert_closed_loop_rows_are_twin_matchable():
    src = _src("app/ui/pages/alerts.py")
    blk = src.split("(alert closed loop)", 1)[0].rsplit("INSERT INTO", 1)[1]
    assert "FINDING_TYPE, TARGET_OBJECT" in blk
    assert '"MAX_CLUSTERS")' in src.split("_cl_lever = (", 1)[1][:300]


def test_acceptance_funnel_excludes_twins_from_every_count():
    sql = mart_sql.acceptance_funnel(90)
    for alias in ("SAVINGS_ESTIMATED", "SAVINGS_VERIFIED", "SAVINGS_REJECTED", "VERIFIED_USD"):
        assert "TWIN_ITEM_ID" in sql.split(f"AS {alias}", 1)[0][-2600:], alias
    assert sql.count("ITEM_ID NOT IN (SELECT TWIN_ITEM_ID") == 4


def test_org_balance_memo_clears_on_refresh(monkeypatch):
    from app.ui.pages.cost_parts import contract
    st.session_state.clear()
    calls = {"n": 0}

    def _fake_run(*_a, **_k):
        calls["n"] += 1
        return QueryResult(df=pd.DataFrame(), ok=False, source="x", error="nope", error_kind="absent")

    monkeypatch.setattr(contract, "run", _fake_run)
    assert contract.org_balance_result("p").ok is False and calls["n"] == 1
    assert contract.org_balance_result("p") is None and calls["n"] == 1        # memoized this generation
    st.session_state["_ow_refresh_salt"] = "2026-09-24T10:00"                  # the app's Refresh
    contract.org_balance_result("p")
    assert calls["n"] == 2                                                     # re-probed
    st.session_state.clear()


def test_auth_inventory_is_risk_ordered_before_the_cap():
    sql = security_sql.user_auth_inventory("ALFA")
    order = sql.rsplit("ORDER BY", 1)[1]          # the final ORDER BY (not the LISTAGG one)
    assert order.index("LEGACY_SERVICE") < order.index("HAS_PASSWORD AND NOT HAS_MFA") < order.index("IS_ADMIN")
    src = _src("app/ui/pages/security.py")
    assert 'if todo.empty and sfx:\n        empty_state("no_data_yet"' in src     # capped: never 'clean'


def test_unset_type_with_unproven_disuse_will_break():
    row = {"USER_TYPE": None, "HAS_PASSWORD": True, "HAS_MFA": False, "PASSWORD_LOGINS_30D": float("nan")}
    assert classify_auth_readiness(row, evidence_ok=False)[0] == VERDICT_WILL_BREAK


def test_assigned_to_me_keeps_the_open_item():
    src = _src("app/ui/workbench.py")
    assert '_pin = _deep_link or str(st.session_state.get("_ow_md_sel_action_center") or "")' in src
    assert '_keep = _keep | (frame["ACTION_ID"].astype(str) == _pin)' in src


def test_overview_all_deferred_is_not_done_or_dropped():
    src = _src("app/ui/pages/overview.py")
    assert "if ranked.empty and _n_def:" in src and "open item(s) are deferred" in src


def test_action_queue_sorts_deferred_last_with_uncapped_totals():
    sql = mart_sql.action_queue(100, "ALFA")
    assert sql.index("ORDER BY CASE WHEN DEFER_UNTIL >") < sql.index("CASE UPPER(SEVERITY)") < sql.rindex("LIMIT")
    assert "AS DEFERRED_TOTAL" in sql and "AS NEXT_RESUME_DATE" in sql
    df = pd.DataFrame({"STATUS": ["OPEN"], "DEFER_UNTIL": ["2026-10-09"], "DEFERRED_TOTAL": [37],
                       "NEXT_RESUME_DATE": ["2026-09-30"]})
    assert deferred_summary(df, pd.Timestamp("2026-09-24")) == (37, date(2026, 9, 30))   # uncapped wins
    sqlglot = pytest.importorskip("sqlglot")
    sqlglot.parse_one(sql, read="snowflake")


def test_proc_redeploy_feed_and_label_name_the_database():
    sql = change_impact_sql.proc_redeploys(30)
    assert "OBJECT_TYPE = 'PROCEDURE'" in sql and "QUALIFY ROW_NUMBER() OVER (PARTITION BY OBJECT_NAME" in sql
    reg = pd.DataFrame([{"OBJECT_TYPE": "PROCEDURE", "OBJECT_NAME": "DEV_EDW.ETL.SP_A", "DATABASE_NAME": "DEV_EDW",
                         "CHANGE_SEEN_AT": pd.Timestamp("2026-09-22"), "CHANGED_BY": "JDEV", "VERDICT": None}])
    ch = latest_proc_changes(reg, now=pd.Timestamp("2026-09-24"))
    out, _ = annotate_proc_changes(pd.DataFrame({"TASK_NAME": ["SP_A"]}), ch)
    assert out.loc[0, "CHANGED_RECENTLY"] == "2026-09-22 · DEV_EDW · JDEV · PENDING"


def test_email_path_red_even_when_alerts_are_not_visible():
    notif = pd.DataFrame([{"SENT_N": 0, "LAST_SENT_AT": None, "LAST_FAILED_AT": "2026-09-24 06:10",
                           "LAST_ERROR": "recipient not verified"}])
    for alerts in (pd.DataFrame(), None):
        v = email_path_verdict(alerts, None, notif, mart_sql.EMAIL_ALERT_NAMES)
        assert (v.state, v.severity) == ("FAILING", "bad")


def test_admin_run_cost_and_queue_never_fake_a_number():
    src = _src("app/ui/pages/admin.py")
    assert '"value": format_usd(att * rate) if att_known else "—"' in src
    assert "rem = max(met - att, 0.0) if (met > 0 and att_known) else None" in src
    assert '"no overload queueing in any hour"' in src
