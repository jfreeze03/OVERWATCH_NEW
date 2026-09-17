"""Locks for app/logic/monitors.py — resource-monitor governance logic.

Covers: monitor inventory (used %, enforced flag, trigger summary), account-level
monitor detection and its coverage override, unmonitored-warehouse detection with
credit ranking, and tolerance of SHOW column drift / empty frames.
"""

from __future__ import annotations

import pandas as pd

from app.logic.monitors import (
    account_monitor,
    resource_monitor_inventory,
    unmonitored_warehouses,
)


def _wh(rows):
    """A SHOW WAREHOUSES-shaped frame (lower-cased quoted columns)."""
    return pd.DataFrame(rows, columns=["name", "size", "resource_monitor", "auto_suspend"])


def _mon(rows):
    """A SHOW RESOURCE MONITORS-shaped frame."""
    return pd.DataFrame(rows, columns=["name", "level", "frequency", "credit_quota",
                                       "used_credits", "remaining_credits",
                                       "notify_at", "suspend_at", "suspend_immediate_at"])


# ---- inventory --------------------------------------------------------------
def test_inventory_used_pct_and_enforced():
    inv = resource_monitor_inventory(_mon([
        ["RM_TEAM", "WAREHOUSE", "MONTHLY", "1000", "800", "200", "80", "100", ""],
        ["RM_WATCH", "WAREHOUSE", "MONTHLY", "500", "50", "450", "75,90", "", ""],
    ]))
    team = inv[inv["MONITOR"] == "RM_TEAM"].iloc[0]
    assert round(team["USED_PCT"]) == 80
    assert bool(team["ENFORCED"]) and team["ENFORCED"] is not None  # has a SUSPEND trigger
    assert "suspend 100%" in team["TRIGGERS"]
    watch = inv[inv["MONITOR"] == "RM_WATCH"].iloc[0]
    assert not watch["ENFORCED"] and watch["ENFORCED"] is not None  # notify-only, no SUSPEND/kill
    assert watch["TRIGGERS"] == "notify 75,90%"
    # sorted by used % desc -> the 80% monitor leads
    assert inv.iloc[0]["MONITOR"] == "RM_TEAM"


def test_inventory_zero_quota_is_not_a_divide_error():
    inv = resource_monitor_inventory(_mon([
        ["RM_ZERO", "ACCOUNT", "MONTHLY", "0", "0", "0", "", "100", ""]]))
    assert inv.iloc[0]["USED_PCT"] == 0.0


def test_inventory_tolerates_missing_trigger_columns():
    # a drifted SHOW that lacks suspend columns -> ENFORCED is unknown (None), never a crash
    df = pd.DataFrame([["RM_X", "ACCOUNT", "1000", "10"]],
                      columns=["name", "level", "credit_quota", "used_credits"])
    inv = resource_monitor_inventory(df)
    assert inv.iloc[0]["ENFORCED"] is None
    assert inv.iloc[0]["TRIGGERS"] == "—"


def test_inventory_empty_in_empty_out():
    out = resource_monitor_inventory(pd.DataFrame())
    assert out.empty and "USED_PCT" in out.columns


# ---- account monitor --------------------------------------------------------
def test_account_monitor_detected_and_none_when_absent():
    acct = account_monitor(_mon([
        ["RM_ACCT", "ACCOUNT", "MONTHLY", "5000", "100", "4900", "80", "100", ""]]))
    assert acct == {"name": "RM_ACCT", "enforced": True}
    assert account_monitor(_mon([
        ["RM_WH", "WAREHOUSE", "MONTHLY", "100", "1", "99", "", "100", ""]])) is None


# ---- unmonitored warehouses -------------------------------------------------
def test_uncapped_detection_ranked_by_credits():
    whs = _wh([
        ["WH_BIG", "LARGE", "null", "60"],        # no monitor -> uncapped
        ["WH_CAPPED", "SMALL", "RM_TEAM", "60"],  # ENFORCING monitor -> capped
        ["WH_SMALL", "XSMALL", "", "60"],         # blank -> uncapped
    ])
    out = unmonitored_warehouses(whs, _mon([
        ["RM_TEAM", "WAREHOUSE", "MONTHLY", "1000", "1", "999", "", "100", ""]]),
        {"WH_BIG": 500.0, "WH_SMALL": 20.0})
    assert list(out["WAREHOUSE_NAME"]) == ["WH_BIG", "WH_SMALL"]  # costliest first
    assert "WH_CAPPED" not in set(out["WAREHOUSE_NAME"])
    assert out.iloc[0]["RECENT_CREDITS"] == 500.0
    assert set(out["CEILING"]) == {"none"}


def test_notify_only_monitor_leaves_the_warehouse_uncapped():
    # a warehouse attached to a NOTIFY-only monitor (no SUSPEND) has no hard ceiling
    whs = _wh([
        ["WH_NOTIFY", "LARGE", "RM_NOTIFY", "60"],    # notify-only -> still uncapped
        ["WH_HARD", "SMALL", "RM_HARD", "60"],        # enforcing -> capped
    ])
    mon = _mon([
        ["RM_NOTIFY", "WAREHOUSE", "MONTHLY", "1000", "1", "999", "100", "", ""],
        ["RM_HARD", "WAREHOUSE", "MONTHLY", "1000", "1", "999", "80", "100", ""],
    ])
    out = unmonitored_warehouses(whs, mon, {"WH_NOTIFY": 300.0})
    assert list(out["WAREHOUSE_NAME"]) == ["WH_NOTIFY"]
    assert out.iloc[0]["CEILING"] == "notify-only (RM_NOTIFY)"


def test_enforcing_account_monitor_makes_nothing_uncapped():
    whs = _wh([["WH_A", "SMALL", "null", "60"], ["WH_B", "SMALL", "none", "60"]])
    acct = _mon([["RM_ACCT", "ACCOUNT", "MONTHLY", "9999", "1", "9998", "", "100", ""]])
    assert unmonitored_warehouses(whs, acct, {"WH_A": 9.0}).empty


def test_notify_only_account_monitor_does_not_suppress_the_list():
    # a notify-only ACCOUNT monitor enforces no ceiling, so unassigned WHs stay uncapped
    whs = _wh([["WH_A", "SMALL", "null", "60"], ["WH_B", "SMALL", "none", "60"]])
    acct_notify = _mon([["RM_ACCT", "ACCOUNT", "MONTHLY", "9999", "1", "9998", "90", "", ""]])
    out = unmonitored_warehouses(whs, acct_notify, {"WH_A": 9.0, "WH_B": 1.0})
    assert list(out["WAREHOUSE_NAME"]) == ["WH_A", "WH_B"]


def test_unknown_enforcement_is_treated_as_a_ceiling_no_false_alarm():
    # SHOW drift drops the suspend columns -> ENFORCED unknown -> benefit of the doubt (capped)
    whs = _wh([["WH_X", "SMALL", "RM_DRIFT", "60"]])
    mon = pd.DataFrame([["RM_DRIFT", "WAREHOUSE", "1000", "1"]],
                       columns=["name", "level", "credit_quota", "used_credits"])
    assert unmonitored_warehouses(whs, mon, {"WH_X": 50.0}).empty


def test_uncapped_missing_credits_default_zero_and_still_listed():
    whs = _wh([["WH_ORPHAN", "MEDIUM", "null", "60"]])
    out = unmonitored_warehouses(whs, pd.DataFrame(), None)
    assert list(out["WAREHOUSE_NAME"]) == ["WH_ORPHAN"]
    assert out.iloc[0]["RECENT_CREDITS"] == 0.0


def test_no_monitors_means_every_warehouse_uncapped():
    whs = _wh([["WH_A", "SMALL", "null", "60"], ["WH_B", "SMALL", "null", "60"]])
    out = unmonitored_warehouses(whs, pd.DataFrame(), {"WH_A": 3.0, "WH_B": 1.0})
    assert list(out["WAREHOUSE_NAME"]) == ["WH_A", "WH_B"]


def test_empty_warehouses_frame_is_safe():
    assert unmonitored_warehouses(pd.DataFrame(), pd.DataFrame(), {}).empty


def test_builder_is_metadata_show_no_account_usage():
    from app.data import security_sql
    sql = security_sql.show_resource_monitors_sql()
    assert sql == "SHOW RESOURCE MONITORS"
    assert "ACCOUNT_USAGE" not in sql  # metadata only, no view / no grant


def test_spend_ceilings_panel_reads_the_real_idle_credit_column():
    """The panel joins recent per-warehouse credits from the idle-headline frame on
    column TOTAL_CREDITS. BOTH legs of that run_mart_first must emit exactly that
    alias, or credits_by_wh silently comes back empty and every 'recent spend' reads
    $0 (a bug an adversarial verify caught: the column is TOTAL_CREDITS, not
    CREDITS_TOTAL). Pin the contract the panel binds to."""
    from app.data import insights_sql, mart27_sql
    from app.ui.pages.cost_parts import optimize
    mart = mart27_sql.eff_idle_analysis(30, "ALL")
    live = insights_sql.idle_warehouse_analysis(30, "ALL")
    assert "AS TOTAL_CREDITS" in mart and "AS TOTAL_CREDITS" in live
    panel_src = optimize._spend_ceilings_panel.__doc__ is not None  # symbol exists
    assert panel_src
    import inspect
    body = inspect.getsource(optimize._spend_ceilings_panel)
    assert '"TOTAL_CREDITS"' in body and "CREDITS_TOTAL" not in body
