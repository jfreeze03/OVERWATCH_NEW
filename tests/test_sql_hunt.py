"""SQL-layer bug hunt (v4.343.0) — locks for the three confirmed fixes.

Adversarial SQL-layer hunt (7 lenses over app/data/*_sql.py + migrations): a
mart-vs-live parity gap, an operator settings write that silently no-op'd, and a
NULL-RATING_TYPE residual that broke the buckets-sum-to-total invariant.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.data import cost_sql, mart27_sql

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# 1) Boss-chart mart + live-fallback builders both exclude CLOUD_SERVICES_ONLY
def test_monthly_spend_builders_agree_on_cloud_services_exclusion():
    prim = mart27_sql.monthly_spend_by_warehouse(13, "ALL")
    fb = mart27_sql.fact_monthly_spend_by_warehouse(13, "ALL")
    assert "CLOUD_SERVICES_ONLY" in prim   # primary already excluded it
    assert "CLOUD_SERVICES_ONLY" in fb     # fallback must too, or the ALL boss chart
    #                                        gains/loses a phantom segment by path
    sqlglot.parse(fb, dialect="snowflake")


# 2) org all-in OTHER_USD is NULL-safe in BOTH the window and monthly builders,
#    so a NULL RATING_TYPE row lands in OTHER and the buckets reconcile to TOTAL
def test_org_other_usd_is_null_safe_in_both_builders():
    win = cost_sql.org_all_in_window_usd(30)
    mon = cost_sql.org_account_month_usd(3)
    guarded = "COALESCE(UPPER(RATING_TYPE), '') NOT IN"
    assert guarded in win and guarded in mon
    # the bare (NULL-dropping) form must be gone from the window builder
    assert "IFF(UPPER(RATING_TYPE) NOT IN" not in win
    sqlglot.parse(win, dialect="snowflake")


# 3) Admin settings write is an UPSERT (MERGE), so editing an unseeded key persists
#    instead of a silent 0-row UPDATE that falsely reports success
def test_admin_settings_writer_upserts_not_update_only():
    a = _src("app/ui/pages/admin.py")
    blk = a.split("_setting_value_input(key, current)", 1)[1].split("st.code(", 1)[0]
    assert "MERGE INTO" in blk and "WHEN NOT MATCHED THEN INSERT" in blk
    assert "WHEN MATCHED THEN UPDATE SET" in blk
    # the old UPDATE-only writer (silent no-op on a missing row) is gone
    assert "UPDATE {core_object('SETTINGS')} SET VALUE" not in a
