"""Compute-pool per-user cost attribution (owner 2026-08-20): drill a compute pool
to the users driving its cost. SNOWPARK_CONTAINER_SERVICES_HISTORY is pool-grain
(no user), so attribution comes from the notebook-runtime feed — notebook pools
only; a native-app pool yields an empty (honest) result."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.logic.spcs import compute_pool_user_costs, native_app_rollup

_ROOT = Path(__file__).resolve().parents[1]


def _nb():
    # NOTEBOOKS_CONTAINER_RUNTIME_HISTORY-shaped rows across two pools.
    return pd.DataFrame([
        {"NOTEBOOK_NAME": "NB1", "USER_NAME": "ALICE", "COMPUTE_POOL_NAME": "POOL_A",
         "SERVICE_NAME": "SVC1", "EXECUTION_TIME_SEC": 100.0, "CREDITS": 2.0},
        {"NOTEBOOK_NAME": "NB2", "USER_NAME": "ALICE", "COMPUTE_POOL_NAME": "POOL_A",
         "SERVICE_NAME": "SVC2", "EXECUTION_TIME_SEC": 50.0, "CREDITS": 1.0},
        {"NOTEBOOK_NAME": "NB3", "USER_NAME": "BOB", "COMPUTE_POOL_NAME": "POOL_A",
         "SERVICE_NAME": "SVC3", "EXECUTION_TIME_SEC": 20.0, "CREDITS": 0.5},
        {"NOTEBOOK_NAME": "NBX", "USER_NAME": "CAROL", "COMPUTE_POOL_NAME": "OTHER_POOL",
         "SERVICE_NAME": "SVCX", "EXECUTION_TIME_SEC": 999.0, "CREDITS": 9.0},
    ])


def test_aggregates_by_user_for_the_selected_pool():
    out = compute_pool_user_costs(_nb(), "POOL_A", rate=3.0)
    assert list(out["USER_NAME"]) == ["ALICE", "BOB"]        # credits desc
    alice = out.iloc[0]
    assert alice["CREDITS"] == 3.0                            # 2.0 + 1.0
    assert alice["USD"] == 9.0                                # 3 credits * $3
    assert alice["EXECUTION_TIME_SEC"] == 150.0
    assert alice["SESSIONS"] == 2 and alice["NOTEBOOKS"] == 2
    # CAROL on OTHER_POOL is excluded
    assert "CAROL" not in set(out["USER_NAME"])


def test_native_app_pool_has_no_user_rows():
    # a pool with no notebook rows (a native-app pool) -> empty, honest "no attribution".
    out = compute_pool_user_costs(_nb(), "POSIT_TEAM_NATIVE_APP_WORKBENCH", rate=3.0)
    assert out.empty
    assert list(out.columns) == ["USER_NAME", "CREDITS", "EXECUTION_TIME_SEC",
                                 "SESSIONS", "NOTEBOOKS", "USD"]


def test_empty_and_missing_columns_are_safe():
    assert compute_pool_user_costs(None, "P").empty
    assert compute_pool_user_costs(pd.DataFrame(), "P").empty
    # missing USER_NAME / COMPUTE_POOL_NAME -> empty, no raise
    assert compute_pool_user_costs(pd.DataFrame({"CREDITS": [1]}), "P").empty


def test_missing_optional_columns_still_aggregates():
    # no SERVICE_NAME / NOTEBOOK_NAME columns -> still sums credits per user, no crash.
    df = pd.DataFrame({"USER_NAME": ["A", "A"], "COMPUTE_POOL_NAME": ["P", "P"],
                       "CREDITS": [1.0, 2.0], "EXECUTION_TIME_SEC": [10.0, 5.0]})
    out = compute_pool_user_costs(df, "P", rate=2.0)
    assert out.iloc[0]["CREDITS"] == 3.0 and out.iloc[0]["USD"] == 6.0
    assert "SESSIONS" not in out.columns and "NOTEBOOKS" not in out.columns


def test_absent_credits_column_never_raises():
    # 'never raises' contract: a non-empty frame missing CREDITS/EXECUTION_TIME_SEC
    # must not throw (bare df.get(col, 0).fillna would).
    df = pd.DataFrame({"USER_NAME": ["A", "A"], "COMPUTE_POOL_NAME": ["P", "P"]})
    out = compute_pool_user_costs(df, "P", rate=2.0)
    assert not out.empty and out.iloc[0]["CREDITS"] == 0.0 and out.iloc[0]["USD"] == 0.0


def test_drill_is_wired_into_spend_page():
    src = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "spend.py").read_text(encoding="utf-8")
    assert "compute_pool_user_costs(" in src
    assert 'selectable_table(pool_df, key="spcs_pool_sel"' in src
    assert "Users driving" in src
    # bounds guard on the sticky selection (no IndexError on a shrunk frame)
    assert "0 <= int(_pool_sel) < len(pool_df)" in src
    # honest per-case empty notes — not a blanket "native-app pool" for every empty case
    assert "native-app pool" in src            # genuine native-app pool
    assert "non-notebook Snowpark" in src      # user-owned 'Unassigned' pool
    assert "rows for this window" in src        # notebook feed empty this window


# ---- native-apps rollup (Spend summary) ------------------------------------
def _pools():
    """compute_pool_usage-shaped rows: two native apps + one Unassigned user pool."""
    return pd.DataFrame([
        {"COMPUTE_POOL_NAME": "POSIT_POOL", "APPLICATION_NAME": "POSIT_TEAM", "CREDITS": 80.0},
        {"COMPUTE_POOL_NAME": "POSIT_POOL2", "APPLICATION_NAME": "POSIT_TEAM", "CREDITS": 6.9},
        {"COMPUTE_POOL_NAME": "OTHER_APP_POOL", "APPLICATION_NAME": "SOME_APP", "CREDITS": 10.0},
        {"COMPUTE_POOL_NAME": "MY_POOL", "APPLICATION_NAME": "Unassigned", "CREDITS": 5.0},
    ])


def test_rollup_groups_by_application_and_splits_unassigned():
    summary, apps = native_app_rollup(_pools(), 2.0)
    # native-app spend excludes 'Unassigned'; POSIT_TEAM's two pools are summed
    assert summary["n_apps"] == 2
    assert round(summary["app_credits"], 1) == 96.9        # 86.9 + 10.0, NOT the 5.0 unassigned
    assert summary["app_usd"] == round(96.9 * 2.0, 4)
    assert summary["top_app"] == "POSIT_TEAM" and round(summary["top_app_usd"], 1) == round(86.9 * 2.0, 1)
    assert summary["unassigned_usd"] == 10.0               # 5.0 credits * $2
    # per-app frame is sorted desc and priced, Unassigned excluded
    assert list(apps["APPLICATION"]) == ["POSIT_TEAM", "SOME_APP"]
    assert apps.iloc[0]["USD"] == round(86.9 * 2.0, 4)


def test_rollup_unassigned_only_reports_zero_apps():
    only_unassigned = pd.DataFrame([{"COMPUTE_POOL_NAME": "P", "APPLICATION_NAME": "Unassigned",
                                     "CREDITS": 9.0}])
    summary, apps = native_app_rollup(only_unassigned, 3.0)
    assert summary["n_apps"] == 0 and summary["app_usd"] == 0.0
    assert summary["unassigned_usd"] == 27.0 and apps.empty


def test_rollup_empty_and_missing_columns():
    z, empty = native_app_rollup(pd.DataFrame(), 3.68)
    assert z["n_apps"] == 0 and z["app_usd"] == 0.0 and empty.empty
    assert native_app_rollup(None, 3.68)[0]["app_usd"] == 0.0
    # a drifted frame without the expected columns degrades to zero, never crashes
    assert native_app_rollup(pd.DataFrame([{"X": 1}]), 1.0)[0]["n_apps"] == 0


def test_rollup_is_wired_onto_the_spend_summary():
    src = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "spend.py").read_text(encoding="utf-8")
    # rendered on the summary (before the detail toggle), reusing the prefetched pool read
    assert "_native_apps_rollup(_pool_res, rate)" in src
    assert "napp_res" in src and "Installed native apps" in src
    # the detail reuses the same pool read (no double SPCS scan)
    assert "pools = _pool_res" in src
    cost = (_ROOT / "app" / "ui" / "pages" / "cost.py").read_text(encoding="utf-8")
    assert '"key": "napp"' in cost and "napp_res=_pf.get(\"napp\")" in cost
