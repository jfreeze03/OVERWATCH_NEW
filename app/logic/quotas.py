"""Pure logic for Snowflake per-user AI cost quotas.

Snowflake's native per-user quotas (``SNOWFLAKE.CORE.QUOTA``) cap a user's
daily/monthly AI credits and can auto-block new AI requests at the limit. The ONE
account-wide, plain-SELECT read Snowflake exposes is the block-history view
``SNOWFLAKE.ACCOUNT_USAGE.QUOTA_ACCESS_BLOCK_HISTORY`` (who was blocked, by which
quota, when) — the per-quota config/limits/consumption are read only via
admin-scoped CALL methods on each quota object, which a read-only monitoring app
cannot reach. So this module normalizes the block-history frame; the panel pairs
it with OVERWATCH's own per-user AI spend to quantify the unguarded exposure.

The view's SQL-reference page 404s. Its real columns, read from the owner's Snowsight data preview on
2026-09-29 (v4.601.1): ACTION_AT, QUOTA_ID, QUOTA_NAME, USER_ID, USER_NAME, CYCLE (e.g. DAILY), ACTION (e.g.
BLOCKED), PER_USER_LIMIT, CREDITS and BLOCKED_UNTIL (the start of the next quota cycle). There is NO
CREATED_ON: the v4.543 reader windowed on it, so every read failed -- silently, as a probe -- and the panel
claimed "no blocks". Columns are still bound at runtime and tolerated if absent (older guessed spellings
stay as fallbacks), and if nothing maps the raw frame is handed back rather than a blank one. Same
silent-break defense as ``logic/monitors.py``.

Next-Fifty #37b adds the review-only quota RECOMMENDER (``recommend_quotas``): each user's own p95
day (and p95 rolling-30-day total) over a FIXED 90-day history, a walk-forward back-test of what
those limits would have held back, and ``runaway_days`` -- the Python twin of the per-user AI
runaway arm of SP_ALERT_SCAN_DAILY (V163 arm [28]; locked by tests/test_ai_runaway_parity.py).
Pure pandas over the Cortex Code user-day frame the AI-users tab already fetched: no new read.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Iterable
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from app.logic.anomaly import robust_z_vs_history
from app.logic.formulas import ACCOUNT_TIMEZONE

_COLS = ["USER", "QUOTA", "DOMAIN", "REASON", "BLOCKED_ON", "RELEASED_ON", "IS_ACTIVE"]
_BLOCKED = "BLOCKED"

# ---- the runaway rule's knobs, as seeded (V163) -- the arm reads them live; the app replays the seeds.
# Each one is pinned against the latest SP_ALERT_SCAN_DAILY body and the V163 seeds by
# tests/test_ai_runaway_parity.py, so a re-tune of the seed or the arm fails there until both move.
DEFAULT_CAP_CREDITS = 15.0          # COCO_DAILY_CAP_CREDITS fallback: TRY_TO_DOUBLE(..) <= 0 or junk -> 15
RUNAWAY_CAP_MULTIPLE = 2.0          # ALERT_CONFIG THRESHOLD_NUM seed: COALESCE(c.THRESHOLD_NUM, 2)
RUNAWAY_ROBUST_Z = 3.5              # SETTINGS AI_RUNAWAY_ROBUST_Z seed: COALESCE(TRY_TO_DOUBLE(..), 3.5)
RUNAWAY_BASELINE_DAYS = 90          # h.DAY >= DATEADD('day', -90, c.DAY) AND h.DAY < c.DAY
RUNAWAY_SCORED_DAYS = 3             # u.DAY >= DATEADD('day', -3, k.TODAY) -- the last 3 complete days
# USER_NAME values that are not a person. The mart writes 'ACCOUNT' (AI Functions rows) and 'UNKNOWN'
# (no USERS match); the live Cortex Code frame writes 'UNKNOWN (<user id>)' for the latter.
NOT_A_USER = ("ACCOUNT", "UNKNOWN")
_UNKNOWN_PREFIX = "UNKNOWN ("
FUNCTIONS_SOURCE = "Functions"      # (f.SOURCE <> 'Functions' OR n.INCL_FN)

# ---- the recommender's defaults
QUOTA_LOOKBACK_DAYS = 90
QUOTA_PCTILE = 0.95
QUOTA_MIN_ACTIVE_DAYS = 10
_MONTH_DAYS = 30

TOTALS_COLS = ["USER_NAME", "DAY", "CREDITS"]
RUNAWAY_COLS = ["USER_NAME", "DAY", "CREDITS", "CAP_MULTIPLE", "ROBUST_Z", "BASELINE_DAYS", "NO_BASELINE"]
QUOTA_COLS = ["USER_NAME", "ACTIVE_DAYS", "P95_DAILY_CREDITS", "SUGGESTED_DAILY_CREDITS",
              "SUGGESTED_MONTHLY_CREDITS", "DAYS_OVER_CAP", "BACKTEST_DAYS", "BACKTEST_DAYS_OVER",
              "BACKTEST_CREDITS_OVER", "BACKTEST_USD_OVER", "RUNAWAY_DAYS"]


def _pick(df: pd.DataFrame, *names: str) -> str | None:
    """First column present (case-insensitive) among ``names``, else None."""
    lower = {str(c).lower(): c for c in df.columns}
    for name in names:
        if name.lower() in lower:
            return lower[name.lower()]
    return None


def _txt(value: object) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):  # np.nan AND pd.NaT (a NULL TIMESTAMP from Snowpark)
            return ""
    except (TypeError, ValueError):
        pass  # array-like / unhashable -> not a scalar NA
    return str(value).strip()


def _account_ts(value: object) -> pd.Timestamp | None:
    """A timestamp as tz-NAIVE account (Central) wall time, or None. TIMESTAMP_LTZ values arrive tz-aware
    (converted); naive values are taken as account time already (the SiS session runs in Central, and the
    fact's FIRST_TS / LAST_TS are converted to Central before the ::TIMESTAMP_NTZ cast, V167)."""
    if _txt(value) == "":
        return None
    try:
        ts = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is not None:
        ts = ts.tz_convert(ACCOUNT_TIMEZONE).tz_localize(None)
    return ts


def is_block_action(value: object) -> bool:
    """True for a row that records a block (ACTION = 'BLOCKED'); a blank ACTION (an older view shape with no
    ACTION column value) counts as a block too, so a frame without the column is not emptied."""
    action = _txt(value).upper()
    return action == "" or action == _BLOCKED


def block_events(blocks: pd.DataFrame | None) -> int:
    """How many block events a normalized frame holds: ACTION = 'BLOCKED' rows when the view carries an ACTION
    column (it may also record other actions, e.g. an admin unblock), else every row."""
    if blocks is None or blocks.empty:
        return 0
    if "ACTION" not in blocks.columns:
        return len(blocks)
    return int(blocks["ACTION"].map(is_block_action).sum())


def _truthy(value: object) -> bool:
    text = _txt(value).upper()
    return text in ("TRUE", "1", "T", "Y", "YES")


def in_window_rows(blocks: pd.DataFrame | None) -> pd.DataFrame:
    """The normalized rows inside the page window (IN_WINDOW true), without the helper column; every row when the
    frame has no IN_WINDOW column (an older read)."""
    if blocks is None or blocks.empty:
        return pd.DataFrame(columns=_COLS)
    if "IN_WINDOW" not in blocks.columns:
        return blocks
    keep = blocks["IN_WINDOW"].map(_truthy)
    return blocks.loc[keep].drop(columns=["IN_WINDOW"]).reset_index(drop=True)


def _active_until(out: pd.DataFrame, now: datetime) -> pd.Series:
    """IS_ACTIVE from BLOCKED_UNTIL: the row is its user's LATEST action on that quota, that action is a block,
    and the block runs past ``now`` (account time). A later non-block action (e.g. an unblock) ends it."""
    until = out["BLOCKED_UNTIL"].map(_account_ts)
    now_ts = pd.Timestamp(now)
    live = until.map(lambda t: t is not None and t > now_ts)
    if "ACTION" in out.columns:
        live = live & out["ACTION"].map(is_block_action)
    if "BLOCKED_ON" in out.columns and "USER" in out.columns:
        at = out["BLOCKED_ON"].map(_account_ts)
        group = out["USER"].map(_txt) + "\x1f" + (out["QUOTA"].map(_txt) if "QUOTA" in out.columns else "")
        order = pd.DataFrame({"g": group, "at": at.map(lambda t: t if t is not None else pd.Timestamp.min)})
        latest = order.groupby("g")["at"].transform("max")
        live = live & (order["at"] == latest)
    return live.astype(bool)


def block_history(blk_df: pd.DataFrame | None, *, now: datetime | None = None) -> tuple[pd.DataFrame, bool]:
    """Normalize ``QUOTA_ACCESS_BLOCK_HISTORY`` into USER / QUOTA / CYCLE / ACTION / CREDITS / PER_USER_LIMIT /
    BLOCKED_ON / BLOCKED_UNTIL / IS_ACTIVE (plus DOMAIN / REASON / RELEASED_ON when an older view shape carries
    them), picking each column defensively -- a column appears only when the view has it.

    IS_ACTIVE (blocked right now): with BLOCKED_UNTIL (the real view) and ``now`` (account time, from the
    caller -- this module never reads the clock), a block is live while its user's latest action on that
    quota is a block that runs past ``now``; without ``now`` it is not derived from BLOCKED_UNTIL. An older
    shape with a release column keeps the old rule (no release timestamp = still blocked). Returns
    ``(frame, mapped)``: ``mapped`` is False when none of the expected columns could be identified (the view
    drifted beyond the candidate names), so the caller can show the raw frame instead of an empty one."""
    if blk_df is None or blk_df.empty:
        return pd.DataFrame(columns=_COLS), True
    picks = {
        "USER": _pick(blk_df, "user_name", "blocked_user", "principal_name", "user"),
        "QUOTA": _pick(blk_df, "quota_name", "quota", "budget_name"),
        "CYCLE": _pick(blk_df, "cycle"),
        "ACTION": _pick(blk_df, "action"),
        "DOMAIN": _pick(blk_df, "domain", "service_type", "quota_domain"),
        "REASON": _pick(blk_df, "block_reason", "reason", "blocked_reason", "block_type"),
        "CREDITS": _pick(blk_df, "credits"),
        "PER_USER_LIMIT": _pick(blk_df, "per_user_limit"),
        "BLOCKED_ON": _pick(blk_df, "action_at", "blocked_on", "block_start", "blocked_at",
                            "start_time", "created_on"),
        "BLOCKED_UNTIL": _pick(blk_df, "blocked_until"),
        "RELEASED_ON": _pick(blk_df, "released_on", "unblocked_on", "released_at",
                             "release_time", "block_end", "end_time"),
        # cortex_sql marks the rows inside the page window (the read also takes the last 32 days for the state)
        "IN_WINDOW": _pick(blk_df, "in_window"),
    }
    found = {norm: col for norm, col in picks.items() if col}
    if not any(k in found for k in ("USER", "QUOTA", "BLOCKED_ON")):
        return blk_df.copy(), False  # unmappable -> hand back the raw view
    out = pd.DataFrame({norm: blk_df[col].to_numpy() for norm, col in found.items()})
    if "BLOCKED_UNTIL" in out.columns:
        if now is not None:
            out["IS_ACTIVE"] = _active_until(out, now)
    elif "RELEASED_ON" in out.columns:
        # Still blocked when no release timestamp has been written. Snowpark returns
        # a NULL TIMESTAMP as pd.NaT in a datetime64 column, so test with the
        # vectorized NA check (catches NaT / None / NaN) OR an empty/blank string.
        rel = out["RELEASED_ON"]
        out["IS_ACTIVE"] = rel.isna() | rel.map(lambda v: _txt(v) == "")
    return out, True


# =====================================================================================================
# #37b: suggested per-user AI quotas + the runaway-rule replay
# =====================================================================================================
def _finite(value: object) -> float | None:
    """``value`` as a finite float, else None (the TRY_TO_DOUBLE of the arm's knobs)."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def effective_cap(value: object) -> float:
    """COCO_DAILY_CAP_CREDITS exactly as the runaway arm reads it:
    ``COALESCE(NULLIF(GREATEST(COALESCE(TRY_TO_DOUBLE(v), 15), 0), 0), 15)`` -- junk, 0 or negative -> 15."""
    number = _finite(value)
    return number if number is not None and number > 0 else DEFAULT_CAP_CREDITS


def effective_z_min(value: object) -> float:
    """AI_RUNAWAY_ROBUST_Z as the arm reads it: ``COALESCE(TRY_TO_DOUBLE(v), 3.5)`` (0 is honoured)."""
    number = _finite(value)
    return RUNAWAY_ROBUST_Z if number is None else number


def _ceil(value: float) -> float:
    """A limit rounded UP to a whole credit; float fuzz (3.0000000001) never costs a whole credit."""
    return float(math.ceil(round(float(value), 6)))


def user_day_totals(user_daily: pd.DataFrame | None, *, include_functions: bool = False) -> pd.DataFrame:
    """USER_NAME x DAY credit totals across SOURCE -- the cap is per user per DAY, not per source (the
    wave2.coco_efficiency collapse rule). Drops rows that are not a person (``ACCOUNT``, ``UNKNOWN`` and
    the live frame's ``UNKNOWN (<id>)``), AI Functions rows unless ``include_functions`` (the arm's
    AI_RUNAWAY_INCLUDE_FUNCTIONS switch; the Cortex Code frame never carries them), unparseable days and
    zero-credit days. Columns USER_NAME / DAY (a ``date``) / CREDITS, sorted by user then day."""
    need = {"USER_NAME", "USAGE_DATE", "CREDITS"}
    if user_daily is None or user_daily.empty or not need.issubset(user_daily.columns):
        return pd.DataFrame(columns=TOTALS_COLS)
    df = user_daily.copy()
    df = df[df["USER_NAME"].notna()]
    name = df["USER_NAME"].astype(str)
    keep = ~name.isin(NOT_A_USER) & ~name.str.startswith(_UNKNOWN_PREFIX)
    if "SOURCE" in df.columns and not include_functions:
        # (f.SOURCE <> 'Functions' OR INCL_FN): a NULL SOURCE is not "not Functions" in SQL either
        keep &= df["SOURCE"].notna() & (df["SOURCE"].astype(str) != FUNCTIONS_SOURCE)
    df = df[keep].assign(USER_NAME=name[keep])
    df["DAY"] = pd.to_datetime(df["USAGE_DATE"], errors="coerce").dt.date
    df["CREDITS"] = pd.to_numeric(df["CREDITS"], errors="coerce").fillna(0.0).astype(float)
    df = df[df["DAY"].notna()]
    if df.empty:
        return pd.DataFrame(columns=TOTALS_COLS)
    out = df.groupby(["USER_NAME", "DAY"], as_index=False)["CREDITS"].sum()
    out = out[out["CREDITS"] > 0]
    return out.sort_values(["USER_NAME", "DAY"]).reset_index(drop=True)[TOTALS_COLS]


def _user_series(totals: pd.DataFrame) -> Iterable[tuple[str, list[date], np.ndarray]]:
    """(user, days ascending, credits) per user of a user_day_totals frame (DAY coerced to ``date``)."""
    t = totals.copy()
    t["DAY"] = pd.to_datetime(t["DAY"], errors="coerce").dt.date
    t["CREDITS"] = pd.to_numeric(t["CREDITS"], errors="coerce").fillna(0.0).astype(float)
    t = t[t["DAY"].notna()]
    for user, g in t.groupby("USER_NAME", sort=True):
        g = g.sort_values("DAY")
        yield str(user), list(g["DAY"]), g["CREDITS"].to_numpy(dtype=float)


def runaway_days(totals: pd.DataFrame | None, cap_credits: float,
                 cap_multiple: float = RUNAWAY_CAP_MULTIPLE, z_min: float = RUNAWAY_ROBUST_Z, *,
                 today: date, scored_days: int = RUNAWAY_SCORED_DAYS) -> pd.DataFrame:
    """Every user-day the per-user AI runaway arm (V163 [28]) raises, replayed in pandas.

    A scored day is a complete day in ``[today - scored_days, today)`` (the arm: the last 3). It raises
    when its credits exceed ``cap_multiple x cap`` (strictly) AND its robust z against the user's OWN
    active days in the 90 days BEFORE it (never itself) is at least ``z_min`` -- or the user has fewer
    than 5 such days, when the cap leg alone decides (NO_BASELINE, ROBUST_Z NaN). ``cap_credits`` and
    ``z_min`` go through the arm's fallbacks (junk / non-positive cap -> 15, junk z -> 3.5). CAP_MULTIPLE
    is the event's METRIC_VALUE, ``ROUND(CR / CAP_CR, 4)``. ``scored_days`` > 3 replays the same test
    over a longer window (the recommender's 90-day RUNAWAY_DAYS)."""
    if totals is None or totals.empty:
        return pd.DataFrame(columns=RUNAWAY_COLS)
    cap = effective_cap(cap_credits)
    mult = _finite(cap_multiple)
    mult = RUNAWAY_CAP_MULTIPLE if mult is None else mult
    zmin = effective_z_min(z_min)
    first = today - timedelta(days=max(0, int(scored_days)))
    rows: list[dict] = []
    for user, days, cr in _user_series(totals):
        for i, day in enumerate(days):
            if not (first <= day < today) or not cr[i] > cap * mult:
                continue
            # the user's active days in [day - 90, day): days ascend, so the slice ends just before i
            hist = cr[bisect_left(days, day - timedelta(days=RUNAWAY_BASELINE_DAYS)):i]
            z = robust_z_vs_history(cr[i], hist)
            if z is None or z >= zmin:
                rows.append({"USER_NAME": user, "DAY": day, "CREDITS": float(cr[i]),
                             "CAP_MULTIPLE": round(float(cr[i]) / cap, 4),
                             "ROBUST_Z": np.nan if z is None else float(z),
                             "BASELINE_DAYS": len(hist), "NO_BASELINE": z is None})
    return pd.DataFrame(rows, columns=RUNAWAY_COLS)


def recommend_quotas(user_daily: pd.DataFrame | None, cap_credits: float, ai_rate_usd: float, *,
                     today: date, pctile: float = QUOTA_PCTILE, lookback_days: int = QUOTA_LOOKBACK_DAYS,
                     min_active_days: int = QUOTA_MIN_ACTIVE_DAYS,
                     cap_multiple: float = RUNAWAY_CAP_MULTIPLE,
                     z_min: float = RUNAWAY_ROBUST_Z) -> pd.DataFrame:
    """Review-only per-user AI quota suggestions over a FIXED history: the ``lookback_days`` complete days
    before ``today`` (never the page's Window -- a quota is a standing limit, not a window statistic).

    One row per user active in that history (Cortex Code frame -> ``user_day_totals``):
      ACTIVE_DAYS               days with credits > 0 in the history
      P95_DAILY_CREDITS         the ``pctile`` of those active-day totals (2 dp)
      SUGGESTED_DAILY_CREDITS   that p95 rounded UP to a whole credit
      SUGGESTED_MONTHLY_CREDITS the ``pctile`` of the zero-filled rolling 30-day totals, rounded up
      DAYS_OVER_CAP             active days strictly above the org daily cap (COCO_DAILY_CAP_CREDITS)
      BACKTEST_*                a WALK-FORWARD back-test: every active day in the history judged
                                against the limit the SAME rule would have set from the user's active
                                days in the ``lookback_days`` BEFORE it (the day itself never sets its
                                own limit -- no look-ahead); only days with ``min_active_days`` such prior
                                days are judged. DAYS = judged, DAYS_OVER = days above that limit,
                                CREDITS_OVER = the credits above it, USD_OVER = CREDITS_OVER x rate.
      RUNAWAY_DAYS              days in the history the runaway rule's test (``runaway_days``) flags
    Recommendation and back-test values are NaN (shown as an em-dash) below ``min_active_days``; the
    counts are facts and always filled. Sorted by P95 descending (NaN last), then user."""
    totals = user_day_totals(user_daily)
    if totals.empty:
        return pd.DataFrame(columns=QUOTA_COLS)
    cap = effective_cap(cap_credits)
    rate = _finite(ai_rate_usd) or 0.0
    q = min(max(float(pctile), 0.0), 1.0)
    look = max(1, int(lookback_days))
    need = max(1, int(min_active_days))
    start = today - timedelta(days=look)
    grid = [start + timedelta(days=k) for k in range(look)]
    flagged = runaway_days(totals, cap, cap_multiple, z_min, today=today, scored_days=look)
    runaway_n = flagged.groupby("USER_NAME").size().to_dict() if not flagged.empty else {}
    rows: list[dict] = []
    for user, days, cr in _user_series(totals):
        in_win = [i for i, d in enumerate(days) if start <= d < today]
        if not in_win:
            continue
        win = cr[in_win]
        row: dict = {"USER_NAME": user, "ACTIVE_DAYS": len(in_win),
                     "P95_DAILY_CREDITS": np.nan, "SUGGESTED_DAILY_CREDITS": np.nan,
                     "SUGGESTED_MONTHLY_CREDITS": np.nan, "DAYS_OVER_CAP": int((win > cap).sum()),
                     "BACKTEST_DAYS": np.nan, "BACKTEST_DAYS_OVER": np.nan,
                     "BACKTEST_CREDITS_OVER": np.nan, "BACKTEST_USD_OVER": np.nan,
                     "RUNAWAY_DAYS": int(runaway_n.get(user, 0))}
        if len(in_win) >= need:
            p95 = float(np.quantile(win, q))
            row["P95_DAILY_CREDITS"] = round(p95, 2)
            row["SUGGESTED_DAILY_CREDITS"] = _ceil(p95)
            by_day = dict(zip(days, cr, strict=True))
            daily = np.array([by_day.get(d, 0.0) for d in grid], dtype=float)
            if len(daily) >= _MONTH_DAYS:
                sums = np.convolve(daily, np.ones(_MONTH_DAYS), mode="valid")
                row["SUGGESTED_MONTHLY_CREDITS"] = _ceil(float(np.quantile(sums, q)))
            judged = over = 0
            credits_over = 0.0
            for i in in_win:
                prior = cr[bisect_left(days, days[i] - timedelta(days=look)):i]   # strictly before day i
                if len(prior) < need:
                    continue
                limit = _ceil(float(np.quantile(prior, q)))
                judged += 1
                if cr[i] > limit:
                    over += 1
                    credits_over += float(cr[i]) - limit
            row["BACKTEST_DAYS"] = float(judged)
            row["BACKTEST_DAYS_OVER"] = float(over)
            row["BACKTEST_CREDITS_OVER"] = round(credits_over, 4)
            row["BACKTEST_USD_OVER"] = round(credits_over * rate, 2)
        rows.append(row)
    if not rows:
        return pd.DataFrame(columns=QUOTA_COLS)
    out = pd.DataFrame(rows, columns=QUOTA_COLS)
    return (out.sort_values(["P95_DAILY_CREDITS", "USER_NAME"], ascending=[False, True], na_position="last")
               .reset_index(drop=True))


def quota_summary(rec: pd.DataFrame | None, cap_credits: float) -> dict:
    """KPI facts for the suggested-quota table (a ``recommend_quotas`` frame)."""
    empty = {"users": 0, "with_history": 0, "p95_over_cap": 0, "runaway_days": 0, "runaway_users": 0,
             "backtest_days_over": 0, "backtest_credits_over": 0.0, "backtest_usd_over": 0.0}
    if rec is None or rec.empty:
        return empty
    cap = effective_cap(cap_credits)
    p95 = pd.to_numeric(rec["P95_DAILY_CREDITS"], errors="coerce")
    runaway = pd.to_numeric(rec["RUNAWAY_DAYS"], errors="coerce").fillna(0)
    return {
        "users": len(rec),
        "with_history": int(p95.notna().sum()),
        "p95_over_cap": int((p95 > cap).sum()),
        "runaway_days": int(runaway.sum()),
        "runaway_users": int((runaway > 0).sum()),
        "backtest_days_over": int(pd.to_numeric(rec["BACKTEST_DAYS_OVER"], errors="coerce").fillna(0).sum()),
        "backtest_credits_over": float(pd.to_numeric(rec["BACKTEST_CREDITS_OVER"], errors="coerce")
                                       .fillna(0.0).sum()),
        "backtest_usd_over": float(pd.to_numeric(rec["BACKTEST_USD_OVER"], errors="coerce").fillna(0.0).sum()),
    }
