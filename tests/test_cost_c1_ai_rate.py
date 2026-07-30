"""Locks for cost review C1: AI/Cortex credits must price at the AI rate, not the
compute rate, in every all-service billed rollup (docs/reviews/COST_ACCOUNTING_REVIEW_2026-07-29.md).
"""
from pathlib import Path

from pytest import approx

from app.data import mart27_sql, mart_sql
from app.logic.formulas import (
    DEFAULT_AI_CREDIT_PRICE_USD,
    DEFAULT_CREDIT_PRICE_USD,
    blended_billed_usd,
    blended_credit_rate,
)

_ROOT = Path(__file__).resolve().parents[1]


# --- the formula ------------------------------------------------------------

def test_blended_billed_usd_prices_each_partition_at_its_own_rate():
    other, ai = 100.0, 50.0
    got = blended_billed_usd(other, ai, 3.68, 2.20)
    assert got == round(100.0 * 3.68 + 50.0 * 2.20, 2)
    # AI credits are cheaper: the blend is strictly below pricing everything at compute
    assert got < (other + ai) * 3.68
    # defaults are the house rates
    assert blended_billed_usd(1, 0) == round(DEFAULT_CREDIT_PRICE_USD, 2)
    assert blended_billed_usd(0, 1) == round(DEFAULT_AI_CREDIT_PRICE_USD, 2)


def test_blended_credit_rate_is_effective_dollar_per_credit():
    # all-AI mix -> the AI rate; all-compute -> the compute rate; empty -> compute
    assert blended_credit_rate(0, 10, 3.68, 2.20) == approx(2.20)
    assert blended_credit_rate(10, 0, 3.68, 2.20) == approx(3.68)
    assert blended_credit_rate(0, 0, 3.68, 2.20) == 3.68  # exact short-circuit, no division
    mixed = blended_credit_rate(30, 10, 3.68, 2.20)
    assert 2.20 < mixed < 3.68


# --- the shared readers carry the split ------------------------------------

def test_metering_readers_emit_the_ai_other_split():
    for sql in (mart_sql.fact_daily_spend(30), mart_sql.fact_daily_spend_year(),
                mart27_sql.compare_billed("2026-06-01", "2026-06-08",
                                          "2026-05-01", "2026-05-08")):
        assert "CREDITS_BILLED_AI" in sql
        assert "CREDITS_BILLED_OTHER" in sql
        # the proven AI predicate, not some looser contains-form
        assert "SERVICE_TYPE ILIKE 'AI%'" in sql
        assert "SERVICE_TYPE ILIKE '%CORTEX%'" in sql
        assert "SERVICE_TYPE ILIKE '%INTELLIGENCE%'" in sql


def test_health_strip_mtd_arm_carries_the_split():
    sql = mart_sql.health_strip()
    assert "'MTD_CREDITS_AI'" in sql
    assert "'MTD_CREDITS_OTHER'" in sql
    assert "'MTD_CREDITS'" in sql  # the total survives for the credit-count delta


# --- the consumers dollarize with the blend, not the flat rate -------------

def test_overview_consumers_use_the_blend():
    ov = (_ROOT / "app" / "ui" / "pages" / "overview.py").read_text(encoding="utf-8")
    assert "ai_rate = safe_float(settings.get(\"AI_CREDIT_PRICE_USD\"), 2.20)" in ov
    assert "_billed_usd_series(" in ov          # per-day frames priced via the split
    assert "blended_billed_usd(" in ov          # scalar MTD priced via the split
    # the four hot sites no longer multiply the mixed total by the flat rate
    assert 'CREDITS_BILLED"].map(lambda c: safe_float(c) * rate)' not in ov


def test_brief_mtd_uses_the_blend():
    br = (_ROOT / "app" / "ui" / "pages" / "brief.py").read_text(encoding="utf-8")
    assert "blended_billed_usd(" in br
    assert "MTD_CREDITS_OTHER" in br and "MTD_CREDITS_AI" in br
    assert "format_usd(mtd_usd)" in br


def test_contract_year_and_planner_use_the_blend():
    ct = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "contract.py").read_text(encoding="utf-8")
    assert "blended_billed_usd(" in ct          # year projection legs
    assert "blended_credit_rate(" in ct         # planner effective rate
    # the planner no longer prices the mixed daily burn at the flat compute rate
    assert 'CREDITS_BILLED"], errors="coerce").fillna(0).mean()) * rate_now' not in ct


def test_compare_account_billed_uses_the_blend():
    cp = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "compare.py").read_text(encoding="utf-8")
    assert "blended_billed_usd(" in cp
    assert 'CREDITS_BILLED_OTHER"' in cp and 'CREDITS_BILLED_AI"' in cp
