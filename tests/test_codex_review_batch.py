"""Regression locks for the Codex 50-rec review — app-first batch.

Behavioural tests for the logic-layer fixes; source-locks for the SQL/UI-layer ones.
(The mart_accept fail-to-live #33 is covered in test_codex_r11; the forecast today-remainder
#16 also in test_p2_pilot_forecast; the verdict allowlist #7 wiring in test_v051_action_layer.)"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# #6 — proc-missing detection must name the CALLed proc (not a generic id error)
# ---------------------------------------------------------------------------
def test_looks_like_missing_proc_requires_the_proc_name():
    from app.core.query import _looks_like_missing_proc
    call = "CALL DBA_MAINT_DB.OVERWATCH.SP_ALERT_LIFECYCLE(1, 2)"
    assert _looks_like_missing_proc(
        "Unknown user-defined function DBA_MAINT_DB.OVERWATCH.SP_ALERT_LIFECYCLE", call)
    # a generic identifier/does-not-exist error from INSIDE a deployed proc must NOT match
    assert not _looks_like_missing_proc("invalid identifier 'SOME_COLUMN'", call)
    assert not _looks_like_missing_proc(
        "SQL compilation error: Object 'OTHER_TABLE' does not exist", call)


# ---------------------------------------------------------------------------
# #8 — legacy fallback stops at the first failure (no audit/later mutation after)
# ---------------------------------------------------------------------------
def test_legacy_action_stops_on_first_failure(monkeypatch):
    import app.core.query as q
    calls = []

    def _fake_run_stmt(stmt, *, page):
        calls.append(stmt)
        return (False, "boom") if len(calls) == 1 else (True, "ok")

    monkeypatch.setattr(q, "execute_statement", _fake_run_stmt)
    ok, msg = q._legacy_action(["UPDATE x SET y = 1;", "INSERT INTO audit VALUES (1);"], page="T")
    assert ok is False
    assert len(calls) == 1                     # the audit/second statement never ran
    assert "stopped at failure" in msg


# ---------------------------------------------------------------------------
# #9 — lifecycle statements are a structured list (semicolon-in-note safe)
# ---------------------------------------------------------------------------
def test_lifecycle_stmts_are_a_list_not_a_split_string():
    from app.ui.pages.alerts import _lifecycle_stmts
    stmts = _lifecycle_stmts("evt-1", "RESOLVE", "fixed; then verified", "ACTIONED")
    assert isinstance(stmts, list) and len(stmts) == 2       # update + audit, never split
    # the note's ';' stays inside ONE quoted literal, not fractured across statements
    assert any("fixed; then verified" in s for s in stmts)


# ---------------------------------------------------------------------------
# #27 — sql_number rejects NaN / inf (invalid Snowflake literals)
# ---------------------------------------------------------------------------
def test_sql_number_rejects_non_finite():
    from app.core.sqlsafe import sql_number
    assert sql_number(float("nan")) == "0.0"
    assert sql_number(float("inf")) == "0.0"
    assert sql_number(float("-inf")) == "0.0"
    assert sql_number(42.5) == "42.5"
    assert sql_number("bad", default=7.0) == "7.0"


# ---------------------------------------------------------------------------
# #45 — cache-domain token uses the REAL table name
# ---------------------------------------------------------------------------
def test_domain_tokens_use_the_real_table_name():
    from app.core.query import _DOMAIN_TOKENS
    assert _DOMAIN_TOKENS["mappings"] == ("DEPARTMENT_MAP",)
    assert "DEPT_MAPPING" not in str(_DOMAIN_TOKENS)


# ---------------------------------------------------------------------------
# #16 — month-end projection estimates TODAY's remainder
# ---------------------------------------------------------------------------
def test_month_end_projection_estimates_today():
    from app.logic.forecast import month_end_projection
    today = date(2026, 7, 21)   # 10 days remain after today (Jul 22-31)
    rows = [{"DAY": (pd.Timestamp(today) - pd.Timedelta(days=i)).date(), "USD": 100.0}
            for i in range(1, 21)]   # Jul 1-20 complete days at $100/day, no today row
    f = month_end_projection(pd.DataFrame(rows), today, engine="linear")
    assert f.ok
    # complete-day MTD 2000 + rate 100 x (today + 10 remaining = 11) = 3100 (was 3000)
    assert abs(f.projected_usd - 3100) < 1
    assert "today +" in f.basis


# ---------------------------------------------------------------------------
# SQL / UI source-locks
# ---------------------------------------------------------------------------
def test_action_queue_orders_by_severity_before_cap():   # #23
    from app.data import mart_sql
    sql = mart_sql.action_queue(200)
    assert "CASE UPPER(SEVERITY) WHEN 'CRITICAL' THEN 0" in sql
    assert sql.index("ORDER BY CASE") < sql.rindex("LIMIT")


def test_platform_score_counts_critical_actions():       # #24
    assert 'isin(("HIGH", "CRITICAL"))' in _src("app/ui/pages/overview.py")


def test_ledger_verify_has_state_guard():                # #26
    assert "AND STATE = 'ESTIMATED';" in _src("app/ui/pages/cost_parts/optimize.py")


def test_ai_exception_queue_is_idempotent():             # #39
    cb = _src("app/ui/pages/cost_parts/ai_chargeback.py")
    assert "WHERE NOT EXISTS (SELECT 1 FROM" in cb and "q.TITLE = {sql_literal(title)}" in cb


def test_allocation_caption_reconciles_with_chart():     # #32
    sp = _src("app/ui/pages/cost_parts/spend.py")
    assert "_top_usd = float(_top" in sp and "shown = (_top_usd / _pool)" in sp
