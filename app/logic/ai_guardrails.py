"""AI Trust & Guardrails — behavioral analytics over Cortex Code usage.

Owner ask 2026-08-17 (repo review, urgency: CoCo spend is ~13% of total and
growing): OVERWATCH instruments Cortex *spend* thoroughly but nothing watches
Cortex *behavior*. This module derives per-user behavioral flags from the
user-day grain frame the Cost page already fetches (cortex_code_user_daily) —
zero new scans:

- VELOCITY: recent 7d average daily requests vs the user's OWN prior-28d
  baseline. The dual condition (ratio AND an absolute floor) is the anti-noise
  pattern from the guardrails repo — 3x-of-nearly-nothing is not a spike.
- TOKEN_Z: the user's 7d token total z-scored against the population of active
  users (median/MAD, shared robust_zscores) — "who is an outlier this week."
- WEEKEND_PCT: share of requests on Sat/Sun — a coarse off-pattern signal at
  day grain (hour grain would need a new scan; deliberately not paid yet).
- NEW_USER: first-ever CoCo activity inside the last 7 days with material
  credits — adoption is fine, but new + immediately-heavy deserves a look.

Pure pandas; no Streamlit, no Snowflake. Tested in tests/test_ai_guardrails.py.
"""

from __future__ import annotations

import pandas as pd

from app.logic.anomaly import robust_zscores
from app.logic.formulas import safe_float

# Flag thresholds (module constants, mirrored in the UI captions).
VELOCITY_RATIO = 3.0        # recent daily rate >= 3x own baseline ...
VELOCITY_MIN_REQUESTS = 20  # ... AND at least this many requests in 7d (noise floor)
TOKEN_Z_FLAG = 3.0          # 7d tokens >= 3 robust-z above the active population
NEW_USER_MIN_CREDITS = 1.0  # new-user flag needs material credits, not a hello-world


def user_behavior(frame: pd.DataFrame | None, now: pd.Timestamp) -> pd.DataFrame:
    """Per-user behavioral summary + flags from user-day grain usage.

    ``frame`` columns (cortex_code_user_daily): USER_NAME, USAGE_DATE, REQUESTS,
    CREDITS, TOKENS. Returns one row per user active in the last 7 days with
    REQUESTS_7D, CREDITS_7D, TOKENS_7D, BASELINE_DAILY, VELOCITY, TOKEN_Z,
    WEEKEND_PCT, NEW_USER, FLAGS (comma-joined, '' = clean). Empty in, empty out.
    """
    cols = ["USER_NAME", "REQUESTS_7D", "CREDITS_7D", "TOKENS_7D", "BASELINE_DAILY",
            "VELOCITY", "TOKEN_Z", "WEEKEND_PCT", "NEW_USER", "FLAGS"]
    if frame is None or frame.empty or "USAGE_DATE" not in frame.columns:
        return pd.DataFrame(columns=cols)
    df = frame.copy()
    df["USAGE_DATE"] = pd.to_datetime(df["USAGE_DATE"], errors="coerce")
    df = df.dropna(subset=["USAGE_DATE"])
    if df.empty:
        return pd.DataFrame(columns=cols)
    today = pd.Timestamp(now).normalize()
    recent_start = today - pd.Timedelta(days=7)
    base_start = today - pd.Timedelta(days=35)

    for col in ("REQUESTS", "CREDITS", "TOKENS"):
        df[col] = pd.to_numeric(df.get(col, 0), errors="coerce").fillna(0.0)

    # Half-open [today-7, today): exactly 7 COMPLETE days, today's partial day
    # excluded (review 2026-08-17 #1 — ">= start" alone spanned 8 dates incl. a
    # partial today, inflating every _7D metric and VELOCITY by ~14%). Mirrors
    # anomaly.complete_days_only.
    recent = df[(df["USAGE_DATE"] >= recent_start) & (df["USAGE_DATE"] < today)]
    if recent.empty:
        return pd.DataFrame(columns=cols)
    base = df[(df["USAGE_DATE"] >= base_start) & (df["USAGE_DATE"] < recent_start)]
    first_seen = df.groupby("USER_NAME")["USAGE_DATE"].min()

    g = recent.groupby("USER_NAME")
    out = pd.DataFrame({
        "REQUESTS_7D": g["REQUESTS"].sum(),
        "CREDITS_7D": g["CREDITS"].sum(),
        "TOKENS_7D": g["TOKENS"].sum(),
    })
    # weekend share of recent requests (day-grain off-pattern proxy).
    wk = recent[recent["USAGE_DATE"].dt.dayofweek >= 5].groupby("USER_NAME")["REQUESTS"].sum()
    req7 = out["REQUESTS_7D"].astype(float)
    out["WEEKEND_PCT"] = (wk.reindex(out.index).fillna(0.0)
                          / req7.where(req7 > 0) * 100).fillna(0.0).round(1)
    # Own prior-28d baseline daily rate, divided by the days the user actually
    # EXISTED in the baseline window (review 2026-08-17 #2 — a fixed /28 treated
    # pre-adoption days as zero-usage, so a steady user onboarded 10 days ago
    # read ~10x velocity and false-flagged for weeks). Users first seen inside
    # the recent window have no baseline -> NaN velocity -> the NEW_USER path.
    base_req = base.groupby("USER_NAME")["REQUESTS"].sum().reindex(out.index).fillna(0.0)
    _fs = first_seen.reindex(out.index)
    base_days = ((recent_start - _fs.clip(lower=base_start)).dt.days).clip(lower=1, upper=28)
    out["BASELINE_DAILY"] = (base_req / base_days.astype(float)).round(2)
    recent_daily = out["REQUESTS_7D"] / 7.0
    base_daily = out["BASELINE_DAILY"].astype(float)
    out["VELOCITY"] = (recent_daily / base_daily.where(base_daily > 0)).round(1)
    out["TOKEN_Z"] = robust_zscores(out["TOKENS_7D"]).round(1)
    out["NEW_USER"] = first_seen.reindex(out.index) >= recent_start

    def _flags(row: pd.Series) -> str:
        hits: list[str] = []
        vel = safe_float(row.get("VELOCITY"), 0.0)
        if vel >= VELOCITY_RATIO and safe_float(row.get("REQUESTS_7D")) >= VELOCITY_MIN_REQUESTS:
            hits.append(f"velocity x{vel:.0f}")
        if safe_float(row.get("TOKEN_Z")) >= TOKEN_Z_FLAG:
            hits.append("token outlier")
        if bool(row.get("NEW_USER")) and safe_float(row.get("CREDITS_7D")) >= NEW_USER_MIN_CREDITS:
            hits.append("new + heavy")
        return ", ".join(hits)

    out["FLAGS"] = out.apply(_flags, axis=1)
    out = out.reset_index()
    return out.sort_values(
        ["FLAGS", "TOKENS_7D"], ascending=[False, False], key=lambda s: (
            s.ne("") if s.name == "FLAGS" else s)
    ).reset_index(drop=True)[cols]


def behavior_kpis(behavior: pd.DataFrame) -> dict:
    """Headline counts for the KPI row; zeros on an empty frame."""
    if behavior is None or behavior.empty:
        return {"active_users": 0, "flagged_users": 0, "velocity_spikes": 0,
                "token_outliers": 0, "new_heavy": 0}
    flags = behavior["FLAGS"].astype(str)
    return {
        "active_users": len(behavior),
        "flagged_users": int((flags != "").sum()),
        "velocity_spikes": int(flags.str.contains("velocity").sum()),
        "token_outliers": int(flags.str.contains("token outlier").sum()),
        "new_heavy": int(flags.str.contains("new \\+ heavy").sum()),
    }
