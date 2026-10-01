"""NULL cells in two insights paths are facts, not zeros or unknowns.

* R1-071: SHOW WAREHOUSES reports a warehouse that never suspends (AUTO_SUSPEND = NULL) as a NULL
  ``auto_suspend`` ("a value of null indicates the warehouse never automatically suspends"). The merge
  coerced it to NaN = unknown, the same as a warehouse SHOW did not list, so the costliest idle burner read
  VERIFY SETTING with $0 actionable, left the idle-opportunity rollup and got no ALTER.
* R1-076: a registered SLA table PIPELINE_SLA_STATUS cannot find in ACCOUNT_USAGE.TABLES has NULL
  HOURS_SINCE; its Breached DETAIL read "already 0s old (past its 24h limit)" (a fabricated zero).
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from app.logic import remediation
from app.logic.insights import (
    idle_advisor,
    pipeline_sla_forecast,
    show_auto_suspend,
    with_auto_suspend_settings,
)
from app.logic.savings_rollup import idle_opportunities


@pytest.mark.parametrize(("cell", "want"), [(None, 0.0), (float("nan"), 0.0), (pd.NA, 0.0), ("null", 0.0),
                                            ("NULL", 0.0), (0, 0.0), ("0", 0.0), (600, 600.0), ("300", 300.0)])
def test_show_auto_suspend_reads_null_as_never(cell, want):
    assert show_auto_suspend(cell) == want


def test_show_auto_suspend_garbage_is_unknown():
    assert show_auto_suspend("abc") is None


def _idle() -> pd.DataFrame:
    return pd.DataFrame([{"WAREHOUSE_NAME": wh, "TOTAL_CREDITS": 100.0, "IDLE_CREDITS": 60.0,
                          "METERED_HOURS": 100.0, "IDLE_HOURS": 60.0}
                         for wh in ("WH_NEVER", "WH_ZERO", "WH_600", "WH_GONE")])


def _show() -> pd.DataFrame:
    # WH_GONE is not listed: the one genuinely unknown setting
    return pd.DataFrame({"name": ["WH_NEVER", "wh_zero", "WH_600"], "auto_suspend": [None, 0, 600]})


def test_a_listed_null_is_a_known_never_suspend_and_an_unlisted_warehouse_stays_unknown():
    out = with_auto_suspend_settings(_idle(), _show()).set_index("WAREHOUSE_NAME")
    assert bool(out.loc["WH_NEVER", "AUTO_SUSPEND_KNOWN"]) and out.loc["WH_NEVER", "AUTO_SUSPEND"] == 0
    assert bool(out.loc["WH_ZERO", "AUTO_SUSPEND_KNOWN"]) and out.loc["WH_ZERO", "AUTO_SUSPEND"] == 0
    assert not bool(out.loc["WH_GONE", "AUTO_SUSPEND_KNOWN"]) and pd.isna(out.loc["WH_GONE", "AUTO_SUSPEND"])


def test_a_never_suspend_warehouse_is_actionable_rolled_up_and_gets_an_alter():
    adv = idle_advisor(with_auto_suspend_settings(_idle(), _show()), 3.68, 30).set_index("WAREHOUSE_NAME")
    never, zero = adv.loc["WH_NEVER"], adv.loc["WH_ZERO"]
    assert never["ACTION_STATUS"] == "ACTIONABLE" and bool(never["ACTIONABLE"])
    assert math.isclose(never["ACTIONABLE_MONTHLY_USD"], zero["ACTIONABLE_MONTHLY_USD"]) and never[
        "ACTIONABLE_MONTHLY_USD"] > 0
    assert adv.loc["WH_GONE", "ACTION_STATUS"] == "VERIFY SETTING"
    targets = {o.target for o in idle_opportunities(adv.reset_index())}
    assert {"WH_NEVER", "WH_ZERO", "WH_600"} <= targets and "WH_GONE" not in targets
    plan = remediation.tighten_suspend_plan("WH_NEVER", never["AUTO_SUSPEND"], bool(never["AUTO_SUSPEND_KNOWN"]))
    assert plan["stmt"] == remediation.auto_suspend_fix("WH_NEVER", 60)


def _sla(hours_since) -> pd.DataFrame:
    return pd.DataFrame([{"TABLE_NAME": "T", "SLA_MET": False, "HOURS_SINCE": hours_since, "MAX_AGE_HOURS": 24,
                          "RUNWAY_HOURS": None if hours_since is None else 24 - hours_since,
                          "MEDIAN_GAP_MIN": None, "LONG_GAP_MIN": None, "REFRESHES": None}])


def test_a_table_missing_from_account_usage_never_reads_zero_seconds_old():
    row = pipeline_sla_forecast(_sla(None)).iloc[0]
    assert row["FORECAST"] == "Breached" and row["SEVERITY"] == "High"      # the verdict and KPIs are unchanged
    assert "0s" not in row["DETAIL"] and "0h" not in row["DETAIL"] and "already" not in row["DETAIL"]
    assert "not found in ACCOUNT_USAGE.TABLES" in row["DETAIL"]


def test_a_measured_breach_keeps_its_age():
    assert pipeline_sla_forecast(_sla(30.0)).iloc[0]["DETAIL"] == "already 30h old (past its 24h limit)"
