"""Regression locks for the round-16 hunt (v4.439.0).

  TWIN   live idle + sizing fallbacks scope company by the COMPANY_FOR_WAREHOUSE UDF
         (matching their mart twins eff_idle_analysis / eff_sizing_profile), not name pattern
  PACE   the Overview pace-vs-budget card feeds a COMPLETE-days MTD (today excluded) into
         budget_pace_variance, so an on-budget account no longer reads "burning fast"
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pandas as pd
import sqlglot

from app.core.result import QueryResult
from app.data import insights_sql

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- TWIN: idle + sizing live fallbacks use the COMPANY_FOR_WAREHOUSE UDF axis ----------
def test_idle_and_sizing_scope_by_company_udf_not_name_pattern():
    for fn in (insights_sql.idle_warehouse_analysis, insights_sql.warehouse_sizing_profile):
        sql = fn(30, "ALFA")
        assert "COMPANY_FOR_WAREHOUSE" in sql
        # every company FILTER uses the UDF; no name pattern survives (label + filters agree)
        assert "LIKE 'WH" not in sql and "WH_ALFA_%" not in sql
        assert "warehouse_clause" not in sql
        sqlglot.parse_one(sql, read="snowflake")
        # ALL: only the label UDF, no company-filter clause
        assert fn(30, "ALL").count("COMPANY_FOR_WAREHOUSE") == 1


# --- class guard: insights_sql has NO name-pattern warehouse-company scope left ---------
def test_insights_sql_company_scope_is_all_udf_axis():
    """Every warehouse-COMPANY scope in insights_sql uses the COMPANY_FOR_WAREHOUSE UDF axis
    (matching cost_sql + the mart twins + COMPANY_SCOPE semantics), never the name-pattern
    companies.warehouse_clause — so a COMPANY_SCOPE-mapped-but-off-pattern warehouse scopes
    consistently across pages. Closes the MC-1 class in insights_sql (round-16 audit)."""
    assert "companies.warehouse_clause(company" not in _src("app/data/insights_sql.py")


# --- PACE: exclude_today drops the partial day, and the pace card uses the complete MTD --
def test_mtd_spend_exclude_today_drops_partial(monkeypatch):
    from app.ui.pages import overview as ov

    monkeypatch.setattr(ov, "account_today", lambda: datetime.date(2026, 8, 15))
    df = pd.DataFrame({"DAY": [datetime.date(2026, 8, 10), datetime.date(2026, 8, 15)],
                       "CREDITS_BILLED": [100.0, 40.0]})
    res = QueryResult(df=df, ok=True)
    incl = ov._mtd_spend_usd(3.68, 2.20, preloaded=res, exclude_today=False)[0]
    excl = ov._mtd_spend_usd(3.68, 2.20, preloaded=res, exclude_today=True)[0]
    assert round(incl, 2) == round(140.0 * 3.68, 2)      # both days
    assert round(excl, 2) == round(100.0 * 3.68, 2)      # today's partial dropped
    assert incl > excl
    # the pace card feeds the COMPLETE-days MTD into budget_pace_variance
    src = _src("app/ui/pages/overview.py")
    assert "exclude_today=True)[0]" in src
    assert "budget_pace_variance(_mtd_complete, budget, account_today())" in src
