"""Cost-layer bug-hunt #4 locks (2026-08-30, v4.362.0).

Fourth adversarial pass (finders: attribution/tagging, chargeback/budget, warehouse/credit math,
ROI/verified-savings, self-cost/mart-vs-live, storage/egress/replication + serverless). Storage/
egress/replication and serverless came back clean. Six confirmed (0 refuted); five app-side, one
owner-gated migration (V106).
  - [MED] cortex short-window false all-clear: the min-tenure gate reused the window-clamped
    OBSERVABLE_DAYS, so a short window clamped every veteran's tenure and skipped the whole scope.
  - [MED] object_cost_top ranked the synthetic UNATTRIBUTED residual row against real objects.
  - [MED] tag_coverage / untagged_executions_for_user scoped company by WAREHOUSE on a USER-grain board.
  - [MED] ROI "pays for itself" divided monthly-magnitude savings by a QTD run cost (mismatched horizon).
  - [MED] savings_by_month included the partial current month, reading as a velocity collapse.
  - [LOW/V106] COST_DEPT_BUDGET_PACE alert join folded case on one side only.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pandas as pd

from app.data import cost_sql, mart_sql
from app.logic import actions, cortex

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Finding #1 -- cortex tenure gate decoupled from the rate denominator (behavioral)
# --------------------------------------------------------------------------- #
def test_short_window_still_flags_a_veteran_breach(monkeypatch) -> None:
    """A short window (window_days=2) must NOT clamp a long-tenured heavy spender out of the
    breach evaluation. The min-tenure guard reads TENURE_DAYS (true days-since-first-usage),
    not the window-clamped OBSERVABLE_DAYS."""
    monkeypatch.setattr(cortex, "account_today", lambda: _dt.date(2026, 8, 30))
    veteran = pd.DataFrame(
        {
            "USER_NAME": ["VET"],
            "SOURCE": ["cortex_analyst"],
            "FIRST_USAGE": ["2026-01-01"],  # ~242 days of tenure
            "TOTAL_CREDITS": [100.0],
            "TOTAL_REQUESTS": [500.0],
            "CREDITS_PER_REQUEST": [0.2],
        }
    )
    enriched = cortex.enrich_user_rollup(veteran, ai_rate_usd=2.20, window_days=2)
    # OBSERVABLE_DAYS is the window-clamped rate denominator (2); TENURE_DAYS is the true tenure.
    assert float(enriched.loc[0, "OBSERVABLE_DAYS"]) == 2.0
    assert float(enriched.loc[0, "TENURE_DAYS"]) >= 200.0
    # Budget of $100 at $2.20/credit => ~45 credits; projection = 100/2*30 = 1500 credits >> budget.
    flagged = cortex.classify_exceptions(enriched, ai_budget_usd=100.0, ai_rate_usd=2.20)
    assert not flagged.empty, "veteran breach must survive a short window (was a false all-clear)"
    assert "VET" in set(flagged["USER_NAME"])


def test_brand_new_user_is_still_small_n_suppressed(monkeypatch) -> None:
    """The tenure decoupling must NOT weaken the genuine small-N guard: a user first seen TODAY
    (tenure = 1 day) is still skipped even though a 1-day projection looks enormous."""
    monkeypatch.setattr(cortex, "account_today", lambda: _dt.date(2026, 8, 30))
    newbie = pd.DataFrame(
        {
            "USER_NAME": ["NEW"],
            "SOURCE": ["cortex_analyst"],
            "FIRST_USAGE": ["2026-08-30"],  # first seen today -> tenure 1
            "TOTAL_CREDITS": [100.0],
            "TOTAL_REQUESTS": [500.0],
            "CREDITS_PER_REQUEST": [0.2],
        }
    )
    enriched = cortex.enrich_user_rollup(newbie, ai_rate_usd=2.20, window_days=30)
    assert float(enriched.loc[0, "TENURE_DAYS"]) == 1.0
    flagged = cortex.classify_exceptions(enriched, ai_budget_usd=100.0, ai_rate_usd=2.20)
    assert flagged.empty, "a first-day user must not project into a false breach"


def test_tenure_falls_back_to_observable_for_old_shape_frames() -> None:
    """Old-shape enriched frames (no FIRST_USAGE) get TENURE_DAYS = OBSERVABLE_DAYS = window, so a
    long window still evaluates them and the small-N guard remains coherent."""
    df = pd.DataFrame({"USER_NAME": ["X"], "TOTAL_CREDITS": [50.0], "TOTAL_REQUESTS": [10.0]})
    enriched = cortex.enrich_user_rollup(df, ai_rate_usd=2.20, window_days=30)
    assert float(enriched.loc[0, "TENURE_DAYS"]) == 30.0
    assert float(enriched.loc[0, "OBSERVABLE_DAYS"]) == 30.0
    # rollup_summary gates on _TEN and still counts the user (30 >= 4).
    summary = cortex.rollup_summary(enriched, window_days=30)
    assert isinstance(summary, dict)


def test_cortex_source_decouples_tenure_from_the_rate_denominator() -> None:
    src = _src("app/logic/cortex.py")
    assert 'out["TENURE_DAYS"] = _elapsed.clip(lower=1).fillna(float(window))' in src
    # the min-tenure gate reads TENURE (falling back to OBSERVABLE), not OBSERVABLE directly
    assert '_tenure = u.get("TENURE_DAYS", u.get("OBSERVABLE_DAYS", _MIN_OBSERVABLE_DAYS))' in src
    assert 'if safe_float(_tenure) < _MIN_OBSERVABLE_DAYS:' in src
    assert '_pu = _pu[_pu["_TEN"] >= _MIN_OBSERVABLE_DAYS]' in src
    # the projection divisor stays OBSERVABLE (window-clamped), unchanged
    assert 'out["PROJECTED_30D_CREDITS"] = out.get("TOTAL_CREDITS", 0.0) / out["OBSERVABLE_DAYS"] * 30.0' in src


# --------------------------------------------------------------------------- #
# Finding #2 -- per-object top-N excludes the synthetic residual bucket
# --------------------------------------------------------------------------- #
def test_object_cost_top_excludes_unattributed_residual() -> None:
    sql = cost_sql.object_cost_top(days=30, company="ALFA", limit=25)
    assert "OBJECT_FQN <> 'UNATTRIBUTED'" in sql


# --------------------------------------------------------------------------- #
# Finding #3 -- user-grain tag boards scope company by USER, not warehouse
# --------------------------------------------------------------------------- #
def test_tag_boards_scope_company_by_user_not_warehouse() -> None:
    cov = cost_sql.tag_coverage(days=30, company="ALFA")
    unt = cost_sql.untagged_executions_for_user("ALICE", days=30, company="ALFA")
    for sql in (cov, unt):
        assert "COMPANY_FOR_USER(USER_NAME) = 'ALFA'" in sql
        # the old warehouse predicate must be gone from these USER-grain boards
        assert "WAREHOUSE_NAME IN (" not in sql


# --------------------------------------------------------------------------- #
# Finding #4 -- ROI denominator is a trailing-30d (monthly) window, matching the numerator
# --------------------------------------------------------------------------- #
def test_app_cost_uses_trailing_30d_not_quarter() -> None:
    sql = mart_sql.app_cost_last_30d()
    assert "DATEADD('day', -30, CURRENT_DATE())" in sql
    assert "DAY < CURRENT_DATE()" in sql
    assert "APP_CREDITS_30D" in sql
    assert "DATE_TRUNC('quarter'" not in sql
    assert "WAREHOUSE_NAME = 'WH_ALFA_ADMIN'" in sql
    # the QTD builder name is fully retired
    assert not hasattr(mart_sql, "app_cost_quarter")


def test_roi_readers_call_the_30d_builder() -> None:
    for rel in ("app/ui/pages/brief.py", "app/ui/decision_studio.py", "app/data/canary.py"):
        src = _src(rel)
        assert "app_cost_quarter" not in src, f"{rel} still calls the retired QTD builder"
        assert "app_cost_last_30d" in src


# --------------------------------------------------------------------------- #
# Finding #5 -- verified-savings-by-month drops the partial current month
# --------------------------------------------------------------------------- #
def test_savings_by_month_drops_the_partial_current_month(monkeypatch) -> None:
    monkeypatch.setattr(actions, "account_now", lambda: _dt.datetime(2026, 8, 30, 12, 0, 0))
    ledger = pd.DataFrame(
        {
            "STATE": ["VERIFIED", "VERIFIED", "VERIFIED"],
            "VERIFIED_AT": ["2026-06-15", "2026-07-15", "2026-08-15"],
            "VERIFIED_USD": [100.0, 200.0, 5.0],
        }
    )
    out = actions.savings_by_month(ledger, months=12)
    assert list(out["MONTH"]) == ["2026-06", "2026-07"], "current partial month must be excluded"
    assert float(out["VERIFIED_USD"].sum()) == 300.0


# --------------------------------------------------------------------------- #
# Finding #6 / V106 -- COST_DEPT_BUDGET_PACE alert join is case-insensitive on both sides
# --------------------------------------------------------------------------- #
def test_v106_alert_join_is_case_insensitive_and_guarded() -> None:
    mig = _src("snowflake/migrations/V106__cost_dept_budget_pace_case_insensitive_join.sql")
    assert "ON UPPER(f.WAREHOUSE_NAME) = UPPER(m.NAME)" in mig
    assert "ON f.WAREHOUSE_NAME = UPPER(m.NAME)" not in mig
    # re-derives SP_ALERT_SCAN only; no schema change
    assert mig.count("CREATE OR REPLACE PROCEDURE") == 1
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN" in mig
    assert "CREATE OR REPLACE VIEW" not in mig and "CREATE TABLE " not in mig
    assert "ALTER TABLE " not in mig and "CREATE TASK" not in mig
    # ordered-apply guard + version stamp
    assert "EXCEPTION (-20106" in mig and "IF (v < 105) THEN" in mig
    assert "SELECT 106 AS VERSION" in mig and "WHERE VERSION = 106)" in mig
    # the V104 cred-expiry fix we build on top of survives untouched
    assert "COST_DEPT_BUDGET_PACE" in mig
