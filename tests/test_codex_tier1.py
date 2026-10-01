"""Tier-1 fixes from the 2026-09-08 Codex review adjudication.

R12 cache-hit provenance · R27 forecast interval coverage · R38 CI fail-closed ·
R43 real statement-timeout visibility.
(R41 per-interaction rerun budget lives in tests/test_usage_sim.py. R40's overdue-verification
signal fed the Experiments board, retired by Option C in v4.597; its helper and locks went
in v4.607.)
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.core.result import QueryResult
from app.logic.forecast import backtest_forecasts

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- R27: forecast interval coverage ----------------------------------------

def test_r27_backtest_scores_the_interval_not_just_the_point():
    days = pd.date_range(end="2026-06-30", periods=150, freq="D")
    daily = pd.DataFrame({"DAY": days.date, "USD": [100.0] * 150})
    bt = backtest_forecasts(daily, months=3)
    assert not bt.empty
    assert {"LOW_USD", "HIGH_USD", "COVERED"}.issubset(bt.columns)
    assert bt["COVERED"].dtype == bool
    # the band is ordered and brackets the point estimate
    assert (bt["LOW_USD"] <= bt["HIGH_USD"]).all()
    # flat spend -> near-exact projection -> the actual lands inside the band at least sometimes
    assert bt["COVERED"].any()


# --- R12: cache-hit provenance ----------------------------------------------

def test_r12_cache_hit_field_and_honest_caption():
    assert QueryResult().cache_hit is False
    assert QueryResult(cache_hit=True).cache_hit is True
    q = _read("app/core/query.py")
    assert "cache_hit=cache_hit" in q   # main run() threads the real flag
    assert "cache_hit=True" in q        # batch member cache-hit path
    comps = _read("app/ui/components.py")
    assert "if result.cache_hit:" in comps          # caption distinguishes a hit
    assert "cached (≤ tier TTL)" in comps       # served-cached label, not "fetched"


# --- R38: CI fails closed on isolation prerequisites ------------------------

def test_r38_ci_no_prod_role_or_warehouse_fallback():
    ci = _read(".github/workflows/ci.yml")
    assert "SNOW_SYSADMINS}" not in ci        # old CI_ROLE fallback to a prod admin role
    assert ":-WH_ALFA_ADMIN}" not in ci       # old fallback to the prod admin warehouse
    assert "SNOWFLAKE_CI_WAREHOUSE" in ci
    # the run step skips rather than run on prod-capable compute when prereqs are unset
    assert 'if [ -z "${SNOWFLAKE_CI_ROLE:-}" ] || [ -z "${SNOWFLAKE_CI_WAREHOUSE:-}" ]; then' in ci


# --- R43: real statement-timeout is visible, not just documented ------------

def test_r43_admin_exposes_real_statement_timeout():
    a = _read("app/ui/pages/admin.py")
    assert "SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS'" in a
    assert "IN WAREHOUSE {APP_WAREHOUSE}" in a
