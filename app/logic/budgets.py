"""Pure logic for Snowflake native Budgets (SNOWFLAKE.CORE.BUDGET).

The read layer hands in the live ``GET_SERVICE_TYPE_USAGE_V2`` rows (month-to-date
spend by service type for the account budget). This folds them into a tidy summary the
panel renders and reconciles against OVERWATCH's own MONTHLY_BUDGET_USD pacing.

No Streamlit, no I/O. The native spending LIMIT and daily history are CALL-only in
Snowflake (not readable from a single SELECT), so the live read gives MTD spend but not
the native limit — that lands only via a budget mart, a follow-up.
"""

from __future__ import annotations

import calendar
import datetime as _dt

import pandas as pd

from app.logic.formulas import safe_float

_SERVICE_COLS = ["SERVICE_TYPE", "CREDITS", "USD"]


def native_budget_summary(usage_df: pd.DataFrame | None, credit_rate: float) -> tuple[dict, pd.DataFrame]:
    """Fold ``GET_SERVICE_TYPE_USAGE_V2`` rows (SERVICE_TYPE, CREDITS) into month-to-date
    credits + USD and a by-service breakdown (credits + USD, sorted desc). Empty/None in
    -> zero totals + an empty typed frame."""
    if usage_df is None or usage_df.empty or "CREDITS" not in usage_df.columns:
        return {"mtd_credits": 0.0, "mtd_usd": 0.0}, pd.DataFrame(columns=_SERVICE_COLS)
    df = usage_df.copy()
    df["CREDITS"] = df["CREDITS"].map(safe_float)
    if "SERVICE_TYPE" not in df.columns:
        df["SERVICE_TYPE"] = "ALL"
    df["SERVICE_TYPE"] = df["SERVICE_TYPE"].map(lambda v: str(v) if v is not None else "ALL")
    grouped = df.groupby("SERVICE_TYPE", as_index=False)["CREDITS"].sum()
    grouped["USD"] = grouped["CREDITS"] * credit_rate
    grouped = grouped.sort_values("CREDITS", ascending=False, kind="stable").reset_index(drop=True)
    mtd_credits = float(grouped["CREDITS"].sum())
    return ({"mtd_credits": mtd_credits, "mtd_usd": mtd_credits * credit_rate},
            grouped[_SERVICE_COLS])


def project_month_end(mtd_usd: float, today: _dt.date) -> float:
    """Straight-line projection of month-end spend from month-to-date, by day-of-month —
    the same pacing shape OVERWATCH uses for MONTHLY_BUDGET_USD."""
    dom = today.day
    if dom <= 0:
        return float(mtd_usd)
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    return float(mtd_usd) / dom * days_in_month
