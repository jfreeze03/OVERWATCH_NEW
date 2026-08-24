"""Repo-review wave-2 pure logic: resource-monitor coverage + token economics.

Pure pandas over frames the UI already fetched; no Streamlit, no Snowflake.
Tested in tests/test_repo_wave2.py.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from app.logic.formulas import safe_float


def _peer_ratio(values: pd.Series, *, positive_baseline: bool = False) -> pd.Series:
    """Each value over the median of the OTHER rows (leave-one-out).

    A whole-population median includes the row being tested, so in a 1- or 2-user
    cohort the heaviest user's ratio to the midpoint is mathematically < 2 and the
    peer/session flag can NEVER fire no matter how extreme the overuse. Comparing
    each user to the median of everyone ELSE removes that self-dilution; for large
    cohorts it is indistinguishable from the whole-population median (dropping one of
    many barely moves it). No peers (n<=1, or no positive baseline) -> ratio 0.
    """
    arr = pd.to_numeric(values, errors="coerce").fillna(0.0).to_numpy(dtype=float)
    n = arr.size
    ratios = np.zeros(n, dtype=float)
    for i in range(n):
        others = np.delete(arr, i)
        if positive_baseline:
            others = others[others > 0]
        med = float(np.median(others)) if others.size else 0.0
        ratios[i] = (arr[i] / med) if med > 0 else 0.0
    return pd.Series(ratios, index=values.index)


def _col(df: pd.DataFrame, *names: str) -> str:
    """First matching column name (SHOW output is lowercase; be tolerant)."""
    lower = {str(c).lower(): c for c in df.columns}
    for name in names:
        if name.lower() in lower:
            return lower[name.lower()]
    return ""


def monitor_coverage(warehouses: pd.DataFrame | None,
                     monitors: pd.DataFrame | None) -> dict:
    """The resource-monitor governance blind-spot map (repo review wave 2).

    From SHOW WAREHOUSES (carries per-warehouse ``resource_monitor``) and SHOW
    RESOURCE MONITORS: which warehouses have NO monitor (no spend cap at all),
    plus the monitor list with % consumed. Returns
    {covered, uncovered, uncovered_names, monitors_df} — zeros/empties in, out.
    """
    out = {"covered": 0, "uncovered": 0, "uncovered_names": [],
           "monitors_df": pd.DataFrame(), "account_monitor": False}
    if warehouses is not None and not warehouses.empty:
        name_c = _col(warehouses, "name")
        mon_c = _col(warehouses, "resource_monitor")
        if name_c and mon_c:
            mon = warehouses[mon_c].astype(str).str.strip().str.lower()
            uncovered = warehouses[mon.isin(("", "null", "none", "nan"))]
            out["uncovered"] = len(uncovered)
            out["covered"] = int(len(warehouses) - len(uncovered))
            out["uncovered_names"] = sorted(
                str(v) for v in uncovered[name_c].dropna().tolist())
    # Review #4: a LEVEL=ACCOUNT monitor caps EVERY warehouse even though SHOW
    # WAREHOUSES shows no per-warehouse assignment — without this flag the map
    # branded account-governed warehouses "uncapped".
    if monitors is not None and not monitors.empty:
        level_c = _col(monitors, "level")
        if level_c:
            out["account_monitor"] = bool(
                monitors[level_c].astype(str).str.strip().str.upper().eq("ACCOUNT").any())
    if monitors is not None and not monitors.empty:
        m = monitors.copy()
        quota_c = _col(m, "credit_quota")
        used_c = _col(m, "used_credits")
        if quota_c and used_c:
            quota = pd.to_numeric(m[quota_c], errors="coerce")
            used = pd.to_numeric(m[used_c], errors="coerce")
            m["PCT_CONSUMED"] = (used / quota.where(quota > 0) * 100).round(1)
        keep = [c for c in (_col(m, "name"), quota_c, used_c,
                            _col(m, "remaining_credits"), "PCT_CONSUMED",
                            _col(m, "frequency"), _col(m, "start_time"),
                            _col(m, "end_time"), _col(m, "level")) if c and c in m.columns]
        out["monitors_df"] = m[keep] if keep else m
    return out


def token_economics(frame: pd.DataFrame | None) -> pd.DataFrame:
    """Per-user prompt-cache economics from token-type grain rows
    (USER_NAME, TOKEN_TYPE, TOKENS): pivots input/output/cache-read/cache-write
    and derives CACHE_HIT_PCT = cache_read / (cache_read + input) — the share of
    prompt context served from cache instead of re-sent. Empty in, empty out."""
    cols = ["USER_NAME", "INPUT", "OUTPUT", "CACHE_READ", "CACHE_WRITE",
            "TOTAL", "CACHE_HIT_PCT"]
    if frame is None or frame.empty or "TOKEN_TYPE" not in frame.columns:
        return pd.DataFrame(columns=cols)
    df = frame.copy()
    df["TOKEN_TYPE"] = (df["TOKEN_TYPE"].astype(str).str.strip().str.lower()
                        .str.replace(" ", "_"))
    df["TOKENS"] = pd.to_numeric(df.get("TOKENS", 0), errors="coerce").fillna(0.0)
    pivot = df.pivot_table(index="USER_NAME", columns="TOKEN_TYPE",
                           values="TOKENS", aggfunc="sum").fillna(0.0)

    def _series(*names: str) -> pd.Series:
        for name in names:
            if name in pivot.columns:
                return pivot[name]
        return pd.Series(0.0, index=pivot.index)

    out = pd.DataFrame(index=pivot.index)
    # Snowflake's TOKENS_GRANULAR leaf keys are input / output / cache_read_input /
    # cache_write_input; keep the older aliases too so a shape drift can't re-zero it.
    out["INPUT"] = _series("input", "input_tokens", "prompt")
    out["OUTPUT"] = _series("output", "output_tokens", "completion")
    out["CACHE_READ"] = _series("cache_read_input", "cache_read", "cached", "cache_read_tokens")
    out["CACHE_WRITE"] = _series("cache_write_input", "cache_write", "cache_creation", "cache_write_tokens")
    out["TOTAL"] = pivot.sum(axis=1)
    denom = out["CACHE_READ"] + out["INPUT"]
    out["CACHE_HIT_PCT"] = (out["CACHE_READ"] / denom.where(denom > 0) * 100).fillna(0.0).round(1)
    out = out.reset_index()
    return out.sort_values("TOTAL", ascending=False).reset_index(drop=True)[cols]


def fleet_cache_hit_pct(economics: pd.DataFrame) -> float:
    """Fleet-wide cache-hit % (token-weighted, not an average of per-user %)."""
    if economics is None or economics.empty:
        return 0.0
    read = safe_float(economics["CACHE_READ"].sum())
    denom = read + safe_float(economics["INPUT"].sum())
    return round(read / denom * 100, 1) if denom > 0 else 0.0


# CoCo usage-review flag — a high-intensity usage pattern worth reviewing. GATED on being
# consistently over the base daily allowance (so a low-volume burst can't qualify) AND showing
# heavy sustained spend vs peers or extended autonomous sessions. All peer-relative so one
# heavy-but-productive user isn't flagged in isolation and the flag can't be argued away with a
# raw number.
_COACH_PEER_MULT = 2.0        # heavy sustained consumer: >= 2x the fleet-median window credits
_COACH_SESSION_MULT = 2.0     # extended autonomous sessions: >= 2x the median credits/request
_COACH_MIN_DAYS_OVER = 5      # consistently OVER the base daily allowance (days in the window)
_COACH_FLAG = "🚩 Review"  # flags a usage pattern to review — lights up in the table


def coco_efficiency(economics: pd.DataFrame | None, user_daily: pd.DataFrame | None,
                    *, cap_credits: float = 15.0, window_days: int = 30,
                    as_of: date | None = None) -> pd.DataFrame:
    """Per-user CoCo efficiency + a usage-review flag: distinguishes targeted, supplemental usage
    from a high-intensity pattern (heavy sustained spend, extended autonomous sessions, and
    consistently over the daily allowance).

    Merges the token-economics cache grain (``economics`` = token_economics() output) with the
    per-user-per-day credit/request rollup (``user_daily`` = cortex_code_user_daily rows:
    USER_NAME, USAGE_DATE, REQUESTS, CREDITS). ``cap_credits`` is the base daily allowance
    (DAYS_OVER_CAP counts days STRICTLY over it); ``window_days`` bounds the daily rollup.

    The population is the credit set (``user_daily``, which the caller scopes to the company) when
    present — so a company view isn't padded by other companies' account-wide token rows, and a
    credit-only user with no token grain still surfaces; it falls back to the account-wide token
    grain (no credit signals) when the daily scan is unavailable. Peer multiples are measured over
    the DISPLAYED set and compared UNROUNDED. The 🚩 flag = consistently over cap AND (heavy vs
    peers OR extended sessions). Flagged rows sort first; empty in -> empty out (never raises)."""
    cols = ["USER_NAME", "FLAG", "TOTAL_CREDITS", "PEER_MULT", "AVG_DAILY_CR", "ACTIVE_DAYS",
            "DAYS_OVER_CAP", "CR_PER_REQ", "SESSION_MULT", "CACHE_WRITE_PCT", "READ_AMP",
            "CACHE_HIT_PCT", "TOTAL", "REASON"]

    # --- cache-grain behaviour per user (from token economics) ---
    cache = pd.DataFrame(columns=["USER_NAME", "CACHE_WRITE_PCT", "READ_AMP", "CACHE_HIT_PCT", "TOTAL"])
    if economics is not None and not economics.empty and "USER_NAME" in economics.columns:
        e = economics.copy()
        e["USER_NAME"] = e["USER_NAME"].astype(str)
        for c in ("INPUT", "OUTPUT", "CACHE_READ", "CACHE_WRITE", "TOTAL", "CACHE_HIT_PCT"):
            e[c] = pd.to_numeric(e.get(c, pd.Series(0.0, index=e.index)), errors="coerce").fillna(0.0)
        # cache_read bills far cheaper than input, so the FULL-price burn is input+output+
        # cache_write; a high cache-write share = context churn (whole-file rewrites / jumping
        # around). read-amp = context re-read per token of new conversation = session length.
        _billable = e["INPUT"] + e["OUTPUT"] + e["CACHE_WRITE"]
        e["CACHE_WRITE_PCT"] = (e["CACHE_WRITE"] / _billable.where(_billable > 0) * 100).fillna(0.0).round(1)
        _newconv = e["INPUT"] + e["OUTPUT"]
        e["READ_AMP"] = (e["CACHE_READ"] / _newconv.where(_newconv > 0)).fillna(0.0).round(1)
        cache = e[["USER_NAME", "CACHE_WRITE_PCT", "READ_AMP", "CACHE_HIT_PCT", "TOTAL"]]

    # --- per-user credits / requests / days-over-cap from the (windowed) daily frame ---
    roll = pd.DataFrame(columns=["USER_NAME", "TOTAL_CREDITS", "AVG_DAILY_CR", "CR_PER_REQ",
                                 "DAYS_OVER_CAP", "ACTIVE_DAYS"])
    if (user_daily is not None and not user_daily.empty
            and {"USER_NAME", "USAGE_DATE"}.issubset(user_daily.columns)):
        d = user_daily.copy()
        d["USER_NAME"] = d["USER_NAME"].astype(str)
        d["USAGE_DATE"] = pd.to_datetime(d["USAGE_DATE"], errors="coerce")
        d["CREDITS"] = pd.to_numeric(d.get("CREDITS", pd.Series(0.0, index=d.index)), errors="coerce").fillna(0.0)
        d["REQUESTS"] = pd.to_numeric(d.get("REQUESTS", pd.Series(0.0, index=d.index)), errors="coerce").fillna(0.0)
        d = d.dropna(subset=["USAGE_DATE"])
        if not d.empty:
            # Anchor the window to as_of (the caller passes account_today(), so this reconciles
            # with the AI-users tab's cortex._window_slice, which cuts at account_today()-days);
            # fall back to the data's own max date when no anchor is given (pure/tested use).
            _anchor = pd.Timestamp(as_of) if as_of is not None else d["USAGE_DATE"].max()
            _cut = _anchor - pd.to_timedelta(max(1, int(window_days)), unit="D")
            d = d[d["USAGE_DATE"] >= _cut]
        if not d.empty:
            # cortex_code_user_daily grain is user-day-SOURCE; collapse to user-day first so a
            # multi-source day counts as ONE day and its cap test sums that day's credits.
            per_day = d.groupby(["USER_NAME", "USAGE_DATE"], as_index=False).agg(
                CREDITS=("CREDITS", "sum"), REQUESTS=("REQUESTS", "sum"))
            _over = float(cap_credits)
            roll = per_day.groupby("USER_NAME", as_index=False).agg(
                TOTAL_CREDITS=("CREDITS", "sum"), _REQ=("REQUESTS", "sum"),
                ACTIVE_DAYS=("USAGE_DATE", "nunique"),
                DAYS_OVER_CAP=("CREDITS", lambda s: int((s > _over).sum()) if _over > 0 else 0))
            roll["AVG_DAILY_CR"] = (roll["TOTAL_CREDITS"]
                                    / roll["ACTIVE_DAYS"].where(roll["ACTIVE_DAYS"] > 0)).fillna(0.0)
            roll["CR_PER_REQ"] = (roll["TOTAL_CREDITS"]
                                  / roll["_REQ"].where(roll["_REQ"] > 0)).fillna(0.0)
            roll = roll[["USER_NAME", "TOTAL_CREDITS", "AVG_DAILY_CR", "CR_PER_REQ",
                         "DAYS_OVER_CAP", "ACTIVE_DAYS"]]

    # --- population: the company-scoped credit set when available, else the account token grain ---
    if not roll.empty:
        out = roll.merge(cache, on="USER_NAME", how="left")
    elif not cache.empty:
        out = cache.copy()
        for c in ("TOTAL_CREDITS", "AVG_DAILY_CR", "CR_PER_REQ", "DAYS_OVER_CAP", "ACTIVE_DAYS"):
            out[c] = 0.0
    else:
        return pd.DataFrame(columns=cols)
    for c in ("TOTAL_CREDITS", "AVG_DAILY_CR", "CR_PER_REQ", "DAYS_OVER_CAP", "ACTIVE_DAYS",
              "CACHE_WRITE_PCT", "READ_AMP", "CACHE_HIT_PCT", "TOTAL"):
        out[c] = pd.to_numeric(out.get(c, pd.Series(0.0, index=out.index)), errors="coerce").fillna(0.0)

    # --- peer-relative multiples over the DISPLAYED set (compare UNROUNDED, round for display) ---
    # Leave-one-out medians: compare each user to the median of everyone ELSE, so a dominant user
    # in a 1-2 user company scope can actually clear the >=2 gate (a whole-population median that
    # includes the user makes the max/midpoint ratio structurally < 2, so the flag could never fire).
    # positive_baseline on BOTH: a zero-credit / zero-CR user is not a spending peer, and letting
    # zeros into the baseline could drag its median to 0 and silently drop a heavy user's flag.
    _peer = _peer_ratio(out["TOTAL_CREDITS"], positive_baseline=True)
    _sess = _peer_ratio(out["CR_PER_REQ"], positive_baseline=True)
    out["PEER_MULT"] = _peer.round(1)
    out["SESSION_MULT"] = _sess.round(1)

    # --- flag: consistently over cap AND (heavy OR extended sessions). The over-cap gate stops a
    # low-volume burst from reading as a sustained high-intensity pattern. ---
    s_over = out["DAYS_OVER_CAP"] >= _COACH_MIN_DAYS_OVER
    s_heavy = _peer >= _COACH_PEER_MULT
    s_long = _sess >= _COACH_SESSION_MULT
    flagged = s_over & (s_heavy | s_long)
    out["FLAG"] = flagged.map(lambda x: _COACH_FLAG if x else "")
    _cap = round(cap_credits)
    reasons = []
    for i in range(len(out)):
        if not bool(flagged.iloc[i]):
            reasons.append("")
            continue
        parts = [f"{int(out['DAYS_OVER_CAP'].iloc[i])}d over {_cap}cr"]
        if bool(s_heavy.iloc[i]):
            parts.append(f"{out['PEER_MULT'].iloc[i]:.1f}x median spend")
        if bool(s_long.iloc[i]):
            parts.append(f"{out['SESSION_MULT'].iloc[i]:.1f}x session weight")
        reasons.append("; ".join(parts))
    out["REASON"] = reasons

    out["_flagged"] = flagged.astype(int)
    out = out.sort_values(["_flagged", "TOTAL_CREDITS", "TOTAL"],
                          ascending=[False, False, False]).reset_index(drop=True)
    return out[cols]


def coco_coaching_count(efficiency: pd.DataFrame | None) -> int:
    """How many users tripped the review flag."""
    if efficiency is None or efficiency.empty or "FLAG" not in efficiency.columns:
        return 0
    return int((efficiency["FLAG"].astype(str) != "").sum())
