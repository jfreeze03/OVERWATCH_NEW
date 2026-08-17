"""Reconciliation audit (2026-08-17) currency guards: the org-billing panels
compare/label USAGE_IN_CURRENCY dollars, which can be non-USD. On a non-USD
account the rate-card reconciliation drift/rate is FX-corrupted and the '$'
labels are wrong — both now guard, like org_accounts_spend already does. Latent
on this USD account; correctness for any non-USD one."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.logic.contract_planner import remaining_balance_summary

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_remaining_balance_summary_surfaces_currency():
    usd = remaining_balance_summary(pd.DataFrame({
        "DAY": pd.to_datetime(["2026-08-10", "2026-08-11", "2026-08-12"]),
        "TOTAL_REMAINING": [300000.0, 299000.0, 298000.0],
        "CURRENCY": ["USD", "USD", "USD"]}))
    assert usd["ok"] and usd["currency"] == "USD"
    eur = remaining_balance_summary(pd.DataFrame({
        "DAY": pd.to_datetime(["2026-08-10", "2026-08-11"]),
        "TOTAL_REMAINING": [200000.0, 199000.0],
        "CURRENCY": ["EUR", "EUR"]}))
    assert eur["currency"] == "EUR"
    # missing CURRENCY column -> defaults to USD (back-compat), never raises.
    none = remaining_balance_summary(pd.DataFrame({
        "DAY": pd.to_datetime(["2026-08-10", "2026-08-11"]),
        "TOTAL_REMAINING": [10.0, 9.0]}))
    assert none["currency"] == "USD"


def test_balance_panel_formats_in_the_ledger_currency():
    src = _src("app/ui/pages/cost_parts/contract.py")
    assert '_ccy = str(summary.get("currency", "USD")).upper()' in src
    assert "_fmt = format_usd if _ccy == \"USD\"" in src
    # the hardcoded '$' title is gone (now currency-aware).
    assert "billing truth (Snowflake org rate card, $)" not in src
    assert "org rate card, {_ccy})" in src


def test_rate_card_reconciliation_guards_non_usd():
    src = _src("app/ui/pages/cost_parts/contract.py")
    # drift + eff-rate only computed when the org side is USD (else FX-corrupted).
    assert "_rc_usd = _rc_ccy == \"USD\"" in src
    assert "if (org_usd and _rc_usd) else None" in src
    assert "if (credits_month and _rc_usd) else None" in src
    # a non-USD account is warned, and the $/cr header/format switch off '$'.
    assert "bills in {_rc_ccy}, not USD" in src
    assert 'format="$%.3f" if _rc_usd else "%.3f"' in src
