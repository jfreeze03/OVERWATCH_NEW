"""Pure logic for Snowflake resource-monitor governance.

The read layer hands in raw ``SHOW RESOURCE MONITORS`` and ``SHOW WAREHOUSES``
frames; this turns them into a tidy monitor inventory and the set of warehouses
that have no spend ceiling. No Streamlit, no I/O — cost_parts/optimize.py renders
the result.

Every column is picked case-insensitively and tolerated-if-absent: SHOW output
column sets drift across Snowflake releases (the same silent-break class that
renamed ``SEARCH_OPTIMIZATION_HISTORY.TABLE_NAME`` and broke a nightly loader),
so a missing column degrades to "unknown" here, never a crash.

Coverage rule: a warehouse has a hard spend ceiling only if it is attached to its
own resource monitor OR an account-level monitor exists (which caps every
otherwise-unassigned warehouse). Everything else is uncapped.
"""

from __future__ import annotations

import pandas as pd

from app.companies import classify_warehouse
from app.logic.formulas import safe_float

# A SHOW WAREHOUSES ``resource_monitor`` cell reads one of these when the
# warehouse has no monitor attached (Snowflake renders the literal 'null').
_NO_MONITOR = {"", "null", "none", "nan"}

_INV_COLS = ["MONITOR", "LEVEL", "FREQUENCY", "QUOTA_CREDITS", "USED_CREDITS",
             "REMAINING_CREDITS", "USED_PCT", "ENFORCED", "TRIGGERS"]
_WH_COLS = ["WAREHOUSE_NAME", "SIZE", "RECENT_CREDITS", "CEILING"]


def _pick(df: pd.DataFrame, *names: str) -> str | None:
    """The first column present (case-insensitive) among ``names``, else None."""
    lower = {str(c).lower(): c for c in df.columns}
    for name in names:
        if name.lower() in lower:
            return lower[name.lower()]
    return None


def _txt(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    return str(value).strip()


def _has_threshold(value: object) -> bool:
    """A trigger cell is 'set' when it names a percentage (not blank/null)."""
    return _txt(value).lower() not in ("", "null", "none")


def resource_monitor_inventory(mon_df: pd.DataFrame | None) -> pd.DataFrame:
    """``SHOW RESOURCE MONITORS`` -> one tidy row per monitor: quota / used /
    remaining credits, used %, level, reset frequency, an ENFORCED flag (True when
    the monitor has a SUSPEND or SUSPEND_IMMEDIATE trigger, False when it is
    notify-only, None when the trigger columns are absent), and a human trigger
    summary. Empty/None in -> empty (typed) out. Sorted by used % desc."""
    if mon_df is None or mon_df.empty:
        return pd.DataFrame(columns=_INV_COLS)
    c_name = _pick(mon_df, "name")
    c_level = _pick(mon_df, "level")
    c_freq = _pick(mon_df, "frequency")
    c_quota = _pick(mon_df, "credit_quota")
    c_used = _pick(mon_df, "used_credits")
    c_remain = _pick(mon_df, "remaining_credits")
    c_notify = _pick(mon_df, "notify_at")
    c_susp = _pick(mon_df, "suspend_at")
    c_kill = _pick(mon_df, "suspend_immediate_at", "suspend_immediately_at")
    have_trigger_cols = bool(c_susp or c_kill)
    rows = []
    for _, r in mon_df.iterrows():
        quota = safe_float(r[c_quota]) if c_quota else 0.0
        used = safe_float(r[c_used]) if c_used else 0.0
        remaining = safe_float(r[c_remain]) if c_remain else (quota - used if quota else 0.0)
        pct = (used / quota * 100.0) if quota > 0 else 0.0
        susp = _has_threshold(r[c_susp]) if c_susp else False
        kill = _has_threshold(r[c_kill]) if c_kill else False
        enforced: bool | None = bool(susp or kill) if have_trigger_cols else None
        parts = []
        if c_notify and _has_threshold(r[c_notify]):
            parts.append(f"notify {_txt(r[c_notify])}%")
        if susp:
            parts.append(f"suspend {_txt(r[c_susp])}%")
        if kill:
            parts.append(f"kill {_txt(r[c_kill])}%")
        rows.append({
            "MONITOR": _txt(r[c_name]) if c_name else "",
            "LEVEL": (_txt(r[c_level]).upper() or "UNASSIGNED") if c_level else "UNKNOWN",
            "FREQUENCY": _txt(r[c_freq]).upper() if c_freq else "",
            "QUOTA_CREDITS": quota,
            "USED_CREDITS": used,
            "REMAINING_CREDITS": remaining,
            "USED_PCT": pct,
            "ENFORCED": enforced,
            "TRIGGERS": " · ".join(parts) if parts else "—",
        })
    out = pd.DataFrame(rows, columns=_INV_COLS)
    return out.sort_values("USED_PCT", ascending=False, kind="stable").reset_index(drop=True)


def account_monitor(mon_df: pd.DataFrame | None) -> dict | None:
    """The account-level monitor (LEVEL == 'ACCOUNT') if one exists — it caps every
    warehouse that has no monitor of its own. Returns ``{"name", "enforced"}`` or
    None. When several are marked account-level (should not happen), the first by
    used % wins."""
    if mon_df is None or mon_df.empty:
        return None
    inv = resource_monitor_inventory(mon_df)
    acct = inv[inv["LEVEL"] == "ACCOUNT"]
    if acct.empty:
        return None
    row = acct.iloc[0]
    # Return a plain Python bool/None: the DataFrame may coerce ENFORCED to a
    # numpy bool, which would break an ``is False`` check at the call site.
    enf = row["ENFORCED"]
    enforced = None if pd.isna(enf) else bool(enf)
    return {"name": str(row["MONITOR"]), "enforced": enforced}


def unmonitored_warehouses(wh_df: pd.DataFrame | None, mon_df: pd.DataFrame | None,
                           credits_by_wh: dict[str, float] | None = None,
                           *, company: str = "ALL") -> pd.DataFrame:
    """Warehouses with NO hard spend ceiling. A warehouse is uncapped when it has
    no resource monitor at all, OR its monitor is a KNOWN notify-only monitor (a
    NOTIFY trigger with no SUSPEND enforces nothing — the warehouse keeps running).
    A ``CEILING`` column says which ('none' vs 'notify-only (<name>)'). Ranked by
    recent credit burn (``credits_by_wh``, keyed by upper-cased warehouse name).

    An account-level monitor caps every otherwise-unassigned warehouse ONLY when it
    actually enforces; a notify-only account monitor is no ceiling, so it does not
    suppress the list. A monitor whose enforcement is UNKNOWN (trigger columns
    absent from a drifted SHOW) is given the benefit of the doubt — treated as a
    ceiling — so schema drift never fabricates a fleet-wide false alarm.
    ``wh_df`` is a raw SHOW WAREHOUSES frame (columns lower-cased here). ``company``
    (non-ALL) drops warehouses that classify to another tenant — SHOW WAREHOUSES is
    account-wide and can't be server-scoped, so the caller passes the tab's company."""
    if wh_df is None or wh_df.empty:
        return pd.DataFrame(columns=_WH_COLS)
    acct = account_monitor(mon_df)
    if acct is not None and acct["enforced"] is not False:
        return pd.DataFrame(columns=_WH_COLS)  # an enforcing (or unknown) account monitor caps all
    inv = resource_monitor_inventory(mon_df)
    # Monitors that are DEFINITIVELY notify-only (ENFORCED is False, not None) — a
    # warehouse attached to one still has no hard ceiling.
    notify_only = {str(n).strip().upper()
                   for n, e in zip(inv["MONITOR"], inv["ENFORCED"], strict=False)
                   if pd.notna(e) and not bool(e)}
    df = wh_df.copy()
    df.columns = [str(c).lower() for c in df.columns]
    c_name = _pick(df, "name")
    if c_name is None:
        return pd.DataFrame(columns=_WH_COLS)
    c_rm = _pick(df, "resource_monitor")
    c_size = _pick(df, "size")
    credits_by_wh = credits_by_wh or {}
    # NB: compare against the RAW company (classify_warehouse returns 'ALFA'/'Trexis' in
    # canonical case) — only upper-case to detect the ALL no-op, exactly like companies.classify_databases.
    _co = str(company or "ALL")
    _scoped = _co.upper() not in ("ALL", "")
    rows = []
    for _, r in df.iterrows():
        name = _txt(r[c_name])
        if not name:
            continue
        # SHOW WAREHOUSES is account-wide; under a company scope drop other tenants'
        # warehouses (names encode the tenant) so the uncapped list + count don't leak
        # cross-company AND agree with the company-scoped RECENT_CREDITS (R1 fix).
        if _scoped and classify_warehouse(name) != _co:
            continue
        assigned = _txt(r[c_rm]) if c_rm else ""
        if assigned.lower() in _NO_MONITOR:
            ceiling = "none"
        elif assigned.strip().upper() in notify_only:
            ceiling = f"notify-only ({assigned})"
        else:
            continue  # attached to an enforcing (or unknown) monitor -> capped
        rows.append({
            "WAREHOUSE_NAME": name,
            "SIZE": _txt(r[c_size]).upper() if c_size else "",
            "RECENT_CREDITS": float(credits_by_wh.get(name.upper(), 0.0)),
            "CEILING": ceiling,
        })
    out = pd.DataFrame(rows, columns=_WH_COLS)
    if out.empty:
        return out
    return out.sort_values(["RECENT_CREDITS", "WAREHOUSE_NAME"],
                           ascending=[False, True], kind="stable").reset_index(drop=True)
