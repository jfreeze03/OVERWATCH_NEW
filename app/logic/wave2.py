"""Repo-review wave-2 pure logic: resource-monitor coverage + token economics.

Pure pandas over frames the UI already fetched; no Streamlit, no Snowflake.
Tested in tests/test_repo_wave2.py.
"""

from __future__ import annotations

import pandas as pd

from app.logic.formulas import safe_float


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
