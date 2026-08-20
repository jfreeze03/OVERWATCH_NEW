"""Compute-pool (Snowpark Container Services) cost attribution.

`SNOWPARK_CONTAINER_SERVICES_HISTORY` meters compute pools at the POOL level — it
carries no user column — so the only per-user attribution Snowflake exposes for
compute-pool cost is `NOTEBOOKS_CONTAINER_RUNTIME_HISTORY` (notebook workloads).
A native-app pool (e.g. a Posit Workbench pool) therefore has no per-user split in
ACCOUNT_USAGE; its users live in the app's own admin/usage console.

Pure pandas; no Streamlit, no Snowflake. Tested in tests/test_spcs.py.
"""

from __future__ import annotations

import pandas as pd

from app.logic.formulas import safe_float

_USER_COST_COLS = ["USER_NAME", "CREDITS", "EXECUTION_TIME_SEC", "SESSIONS", "NOTEBOOKS", "USD"]


def compute_pool_user_costs(notebook_df: pd.DataFrame | None, pool_name: object,
                            rate: float = 0.0) -> pd.DataFrame:
    """Per-user notebook-runtime cost on ONE compute pool.

    From `notebook_container_usage` rows (NOTEBOOK_NAME, USER_NAME,
    COMPUTE_POOL_NAME, SERVICE_NAME, EXECUTION_TIME_SEC, CREDITS): filter to
    ``pool_name`` and aggregate by USER_NAME — the users driving that pool's cost,
    priced at ``rate`` $/credit. Empty for a native-app pool (no notebook rows) or
    empty/missing input. Returns columns
    [USER_NAME, CREDITS, EXECUTION_TIME_SEC, SESSIONS, NOTEBOOKS, USD]; never raises.
    """
    empty = pd.DataFrame(columns=_USER_COST_COLS)
    if (notebook_df is None or notebook_df.empty
            or "COMPUTE_POOL_NAME" not in notebook_df.columns
            or "USER_NAME" not in notebook_df.columns):
        return empty
    df = notebook_df[notebook_df["COMPUTE_POOL_NAME"].astype(str) == str(pool_name)].copy()
    if df.empty:
        return empty
    # coerce from a real Series only when the column exists; a bare df.get(col, 0)
    # would yield scalar 0 (no .fillna) and break the "never raises" contract.
    df["CREDITS"] = (pd.to_numeric(df["CREDITS"], errors="coerce").fillna(0.0)
                     if "CREDITS" in df.columns else 0.0)
    df["EXECUTION_TIME_SEC"] = (pd.to_numeric(df["EXECUTION_TIME_SEC"], errors="coerce").fillna(0.0)
                                if "EXECUTION_TIME_SEC" in df.columns else 0.0)
    named: dict = {"CREDITS": ("CREDITS", "sum"),
                   "EXECUTION_TIME_SEC": ("EXECUTION_TIME_SEC", "sum")}
    if "SERVICE_NAME" in df.columns:
        named["SESSIONS"] = ("SERVICE_NAME", "nunique")
    if "NOTEBOOK_NAME" in df.columns:
        named["NOTEBOOKS"] = ("NOTEBOOK_NAME", "nunique")
    agg = df.groupby("USER_NAME", as_index=False).agg(**named)
    agg["USD"] = agg["CREDITS"] * safe_float(rate)
    keep = [c for c in _USER_COST_COLS if c in agg.columns]
    return agg.sort_values("CREDITS", ascending=False).reset_index(drop=True)[keep]
