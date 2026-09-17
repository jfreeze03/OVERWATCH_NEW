"""Pure logic for Snowflake per-user AI cost quotas.

Snowflake's native per-user quotas (``SNOWFLAKE.CORE.QUOTA``) cap a user's
daily/monthly AI credits and can auto-block new AI requests at the limit. The ONE
account-wide, plain-SELECT read Snowflake exposes is the block-history view
``SNOWFLAKE.ACCOUNT_USAGE.QUOTA_ACCESS_BLOCK_HISTORY`` (who was blocked, by which
quota, when) — the per-quota config/limits/consumption are read only via
admin-scoped CALL methods on each quota object, which a read-only monitoring app
cannot reach. So this module normalizes the block-history frame; the panel pairs
it with OVERWATCH's own per-user AI spend to quantify the unguarded exposure.

The view's columns are UNDOCUMENTED as of 2026-09 (its SQL-reference page 404s;
only ``CREATED_ON`` is confirmed), so every column is bound at runtime, tolerated
if absent, and — if nothing maps — the raw frame is handed back rather than a
blank one. Same silent-break defense as ``logic/monitors.py``.
"""

from __future__ import annotations

import pandas as pd

_COLS = ["USER", "QUOTA", "DOMAIN", "REASON", "BLOCKED_ON", "RELEASED_ON", "IS_ACTIVE"]


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


def block_history(blk_df: pd.DataFrame | None) -> tuple[pd.DataFrame, bool]:
    """Normalize ``QUOTA_ACCESS_BLOCK_HISTORY`` into
    USER / QUOTA / DOMAIN / REASON / BLOCKED_ON / RELEASED_ON / IS_ACTIVE, picking
    each column defensively. IS_ACTIVE (still blocked) is derived only when a
    release-timestamp column is found — a block with no release row is still in
    effect. Returns ``(frame, mapped)``: ``mapped`` is False when none of the
    expected columns could be identified (the view drifted beyond the candidate
    names), so the caller can show the raw frame instead of an empty one."""
    if blk_df is None or blk_df.empty:
        return pd.DataFrame(columns=_COLS), True
    picks = {
        "USER": _pick(blk_df, "user_name", "blocked_user", "principal_name", "user"),
        "QUOTA": _pick(blk_df, "quota_name", "quota", "budget_name"),
        "DOMAIN": _pick(blk_df, "domain", "service_type", "quota_domain"),
        "REASON": _pick(blk_df, "block_reason", "reason", "blocked_reason", "block_type"),
        "BLOCKED_ON": _pick(blk_df, "blocked_on", "block_start", "blocked_at",
                            "start_time", "created_on"),
        "RELEASED_ON": _pick(blk_df, "released_on", "unblocked_on", "released_at",
                             "release_time", "block_end", "end_time"),
    }
    found = {norm: col for norm, col in picks.items() if col}
    if not any(k in found for k in ("USER", "QUOTA", "BLOCKED_ON")):
        return blk_df.copy(), False  # unmappable -> hand back the raw view
    out = pd.DataFrame({norm: blk_df[col].to_numpy() for norm, col in found.items()})
    if "RELEASED_ON" in out.columns:
        # Still blocked when no release timestamp has been written. Snowpark returns
        # a NULL TIMESTAMP as pd.NaT in a datetime64 column, so test with the
        # vectorized NA check (catches NaT / None / NaN) OR an empty/blank string.
        rel = out["RELEASED_ON"]
        out["IS_ACTIVE"] = rel.isna() | rel.map(lambda v: _txt(v) == "")
    return out, True
