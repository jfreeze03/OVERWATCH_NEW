"""rec#11: data-transfer (egress) is dollarized on the Cost page — a billable-
flagged breakdown priced from the org rate-card implied $/TB, falling back to a
configured setting, never an inlined rate.
"""

from pathlib import Path

from app.config import DEFAULT_SETTINGS
from app.data import cost_sql
from app.logic.formulas import egress_effective_rate_per_tb

_ROOT = Path(__file__).resolve().parents[2]
_SPEND = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "spend.py").read_text(encoding="utf-8")


# --- pure rate resolver ----------------------------------------------------
def test_rate_prefers_org_truth_then_setting_fallback():
    # org billing truth present + plausible -> implied rate (reconciles to the bill)
    rate, used_org = egress_effective_rate_per_tb(1000.0, 50.0, 20.0)
    assert used_org is True and rate == 20.0            # 1000 / 50 TB
    # no org currency visible -> configured setting fallback
    rate, used_org = egress_effective_rate_per_tb(0.0, 50.0, 22.0)
    assert used_org is False and rate == 22.0
    # org truth but zero billable TB -> cannot imply, fall back (no divide-by-zero)
    rate, used_org = egress_effective_rate_per_tb(1000.0, 0.0, 22.0)
    assert used_org is False and rate == 22.0


def test_rate_rejects_an_implausibly_high_implied_rate():
    # tiny billable TB (app under-counts) vs a real org bill would imply $100k/TB;
    # the sanity bound (default 10x the setting) rejects it back to the setting.
    rate, used_org = egress_effective_rate_per_tb(10000.0, 0.1, 20.0)
    assert used_org is False and rate == 20.0           # 100000 > 10*20 -> fall back
    # just under the bound is accepted as org truth
    rate, used_org = egress_effective_rate_per_tb(200.0, 1.0, 20.0)
    assert used_org is True and rate == 200.0           # 200 == 10*20, not > -> kept


# --- builder ---------------------------------------------------------------
def test_builder_reads_transfer_history_and_flags_cross_boundary():
    sql = cost_sql.transfer_egress_priced(30)
    assert "SNOWFLAKE.ACCOUNT_USAGE.DATA_TRANSFER_HISTORY" in sql
    assert "AS BILLABLE" in sql
    # billable = crosses a region OR a cloud boundary; same-region same-cloud is free
    assert "COALESCE(TARGET_REGION, '') <> COALESCE(SOURCE_REGION, '')" in sql
    assert "COALESCE(TARGET_CLOUD, '') <> COALESCE(SOURCE_CLOUD, '')" in sql
    # grouped by source/target cloud+region + transfer type
    for col in ("SOURCE_CLOUD", "SOURCE_REGION", "TARGET_CLOUD", "TARGET_REGION", "TRANSFER_TYPE"):
        assert col in sql
    assert "POWER(1024, 4)" in sql   # binary TB


def test_builder_bounds_the_window():
    # a hostile window is clamped, not interpolated raw
    assert "DATEADD('day', -365," in cost_sql.transfer_egress_priced(99999)


# --- config + panel wiring -------------------------------------------------
def test_setting_default_exists():
    assert "DATA_TRANSFER_USD_PER_TB" in DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["DATA_TRANSFER_USD_PER_TB"] > 0


def test_panel_prices_via_the_resolver_and_reconciles_to_org_truth():
    assert "Egress / data transfer" in _SPEND
    assert "transfer_egress_priced(" in _SPEND
    assert "egress_effective_rate_per_tb(" in _SPEND
    # reconciliation to the org bill is disclosed
    assert "Reconciliation:" in _SPEND
    # same-region transfer is priced at zero, not billed
    assert '.where(edf["BILLABLE"], 0.0)' in _SPEND


def test_panel_reconciles_over_the_same_window_via_verified_path():
    # skeptic P1/P2 fixes: org billing truth is read over the SAME `days` window
    # (not a hardcoded 30d) via the verified RATING_TYPE builder, and reconciliation
    # only engages when the org bill is USD.
    block = _SPEND.split('elif detail == "Egress / data transfer":', 1)[1].split("\n        else:", 1)[0]
    assert "org_all_in_window_usd(days, bounds=bounds)" in block   # same window + verified RATING_TYPE
    assert "org_usage_in_currency(30)" not in block  # the old fixed-30d path is gone
    assert 'ccy == "USD"' in block                   # USD guard on the reconciliation
