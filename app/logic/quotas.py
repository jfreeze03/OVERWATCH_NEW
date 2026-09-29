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
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from app.logic.formulas import ACCOUNT_TIMEZONE

_COLS = ["USER", "QUOTA", "DOMAIN", "REASON", "BLOCKED_ON", "RELEASED_ON", "IS_ACTIVE"]
_BLOCKED = "BLOCKED"


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
    (converted); naive values are taken as account time already (the SiS session runs in Central)."""
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
