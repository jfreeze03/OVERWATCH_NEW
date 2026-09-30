"""Next-Fifty #35 (storage leg): which Storage & waste tables count in Addressable $/mo. Pure module (no Streamlit,
no Snowflake, no clock).

Cost ▸ Optimization & Savings ▸ Storage & waste reads insights_sql.storage_reclaim: the top 50 tables by Time Travel
+ fail-safe + clone-retained bytes, each with its DML status (90 days), its access-history read evidence (any object
domain, 90 days), whether its database is granted to a share, whether it is at least 90 days old, and how many live
tables share its clone group (CLONE_GROUP_LIVE, counted account-wide before the Company scope and the LIMIT). This
module turns each row into one LEVER and an ESTIMATED $/mo at the storage rate.

Only a live table (RETENTION_KNOWN) nobody read in 90 days (NEVER_READ), at least 90 days old (OLDER_THAN_90D, so the
no-read claim spans the whole window) and not in a shared-out database (SHARED_DATABASE: a consumer account's reads
never reach this account's access history) counts, and then in one of two ways:
  * Archive or drop (STORAGE) — no DML in 90 days and no clone sharing its storage: its ACTIVE bytes, which a drop
    frees once they age out of Time Travel and fail-safe. Its remaining Time Travel is NOT counted (with no DML it
    ages out on its own). A stale table whose clone group has another live table, or that retains bytes for a clone
    (CLONE_RETAINED_GB > 0), is 'Check clones' and unpriced instead: a source owns the micro-partitions its clones
    share, so dropping it frees nothing a clone still references (they become retained-for-clone bytes of the
    dropped table). An unknown group count is unpriced too, like an unknown share flag.
  * Cut retention (RETENTION) — still written: the Time Travel a TARGET_RETENTION_DAYS retention would release,
    TIME_TRAVEL x (R - target) / R, the retention control's own estimate. It is a monthly run-rate only on the
    assumption, not checked here, that the table keeps being written as it was over its retention window (a one-off
    rewrite ages out of Time Travel on its own), and it assumes no account-level MIN_DATA_RETENTION_TIME_IN_DAYS
    above the target (the parameter is not read; the legend says so).
Never counted: fail-safe (a fixed 7-day tail), clone-retained bytes (a clone still holds them), a stale table's Time
Travel, retention on a table someone reads (a recovery policy, not waste), tables under 90 days old, shared-out
databases, stale tables sharing storage with a clone, dropped or replaced tables, and every row of the DML-only
fallback (no read evidence).

Precedence per row (first match wins): Object gone > Unconfirmed > Keep > Check share consumers > Newer than 90 days
> Check clones (stale tables only) > Archive or drop > Cut retention > Nothing to reclaim. The verdict words are the
unread-maintenance ones where the meaning is the same (unread_maintenance.VERDICT_*).
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping

import pandas as pd

from app.logic.formulas import safe_float
from app.logic.ledger_measure import OBJECT_FINDING_TYPES, TABLE_FINDING_TYPES
from app.logic.unread_maintenance import (
    VERDICT_GONE,
    VERDICT_KEEP,
    VERDICT_SHARED,
    VERDICT_UNCONFIRMED,
    _known_false,
    _text,
    _truthy,
)

TARGET_RETENTION_DAYS = 1        # owner default: Snowflake's default; keeps one day of UNDROP
UNREAD_DAYS = 90
LEVER_DROP = "Archive or drop"
LEVER_RETENTION = "Cut retention"
LEVER_YOUNG = f"Newer than {UNREAD_DAYS} days"
LEVER_CLONES = "Check clones"
LEVER_NOTHING = "Nothing to reclaim"
# the savings_rollup source each counted lever becomes (both are in its one-saving-per-table object group)
LEVER_SOURCE = {LEVER_DROP: "STORAGE", LEVER_RETENTION: "RETENTION"}
# A table with a live Savings-ledger booking under any of these is left out of the lever: a retention cut already
# booked (RETENTION), or an unread-maintenance saving on the same table (one saving per table).
STORAGE_BOOKED_TYPES = TABLE_FINDING_TYPES | OBJECT_FINDING_TYPES
LEVER_COLUMNS = ("LEVER", "EST_MONTHLY_USD")
_EVIDENCE = ("NEVER_READ", "OLDER_THAN_90D", "SHARED_DATABASE", "RETENTION_KNOWN", "CLONE_GROUP_LIVE")

LEVER_LEGEND = (
    "LEVER says whether a table qualifies for Addressable $/mo (Idle & sizing, Proof ▸ Pipeline); tables already "
    "booked on the Savings ledger are then left out (the line at the end of this panel counts them). "
    f"{LEVER_DROP}: no DML and no read in {UNREAD_DAYS} days, and no clone shares its storage; priced on its active "
    "bytes, which a drop frees once they age out of Time Travel and fail-safe. "
    f"{LEVER_RETENTION}: still written but not read in {UNREAD_DAYS} days; priced on the Time Travel a "
    f"{TARGET_RETENTION_DAYS}-day retention would release (Time Travel × (retention − {TARGET_RETENTION_DAYS}) ÷ "
    "retention, the retention control's own estimate). That is a monthly run-rate only while the table keeps being "
    "written as it was over its retention window (a one-off rewrite ages out of Time Travel on its own), and it "
    f"assumes no account-level MIN_DATA_RETENTION_TIME_IN_DAYS above {TARGET_RETENTION_DAYS} day (not checked "
    "here): a higher floor keeps that Time Travel whatever the table's own setting. "
    f"{VERDICT_KEEP}: read in {UNREAD_DAYS} days. "
    f"{VERDICT_SHARED}: its database is shared out, and a consumer account's reads never reach this account's access "
    "history. "
    f"{LEVER_YOUNG}: too new for a {UNREAD_DAYS}-day no-read claim. "
    f"{LEVER_CLONES}: a stale, unread table that shares storage with a clone (another live table in its clone group, "
    "or bytes it retains for a clone); not priced, because dropping it frees nothing a clone still references. "
    f"{VERDICT_GONE}: no live table under that ID (dropped or replaced). "
    f"{LEVER_NOTHING}: nothing a drop or a {TARGET_RETENTION_DAYS}-day retention would free (a written table already "
    f"at {TARGET_RETENTION_DAYS} day or less, or with no Time Travel; a stale table with no active bytes, whose Time "
    "Travel ages out on its own). "
    "Fail-safe (a fixed 7-day tail) and clone-retained bytes (a clone still holds them) never count. "
    "Confirm with the owner before dropping anything: reads from a replica in another account, and reads rarer "
    f"than every {UNREAD_DAYS} days, are invisible here."
)
# What the storage-waste figure is, as both Addressable $/mo help texts (Idle & sizing, Proof ▸ Pipeline) state it,
# so they agree with LEVER_LEGEND.
H_STORAGE_BASIS = (
    f"a stale table's active bytes, or the Time Travel a {TARGET_RETENTION_DAYS}-day retention would release on a "
    "table still written (a monthly run-rate only while it keeps being written), at your storage rate"
)
FLOOR_LEGEND = (
    f"The scan ranks tables by retention bytes, and a table with no DML for {UNREAD_DAYS} days has little or no Time "
    "Travel or fail-safe left, so most stale tables fall outside the top 50: "
    f"{LEVER_DROP} is a floor."
)


def _status(row: Mapping[str, object]) -> str:
    """STALE / ACTIVE from STATUS (the panel renames DML_STATUS to it), else DML_STATUS; '' when neither."""
    raw = row.get("STATUS")
    if raw is None or (isinstance(raw, float) and math.isnan(raw)):
        raw = row.get("DML_STATUS")
    return _text(raw).upper()


def _unknown(value: object) -> bool:
    """None / NaN / pd.NA: a missing measurement, never a measured false."""
    try:
        return value is None or bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _shares_clone_storage(row: Mapping[str, object]) -> bool:
    """True when a drop could leave bytes a clone still references: another live table in the clone group
    (CLONE_GROUP_LIVE > 1), bytes retained for a clone (CLONE_RETAINED_GB > 0), or an unknown / unreadable group
    count (unknown counts as shared, like SHARED_DATABASE)."""
    live = safe_float(row.get("CLONE_GROUP_LIVE"), default=math.nan)
    return math.isnan(live) or live > 1 or safe_float(row.get("CLONE_RETAINED_GB")) > 0


def _verdict(row: Mapping[str, object], rate_tb: float, target_days: int) -> tuple[str, float]:
    if not _truthy(row.get("RETENTION_KNOWN")):
        return VERDICT_GONE, math.nan
    never_read = row.get("NEVER_READ")
    if _unknown(never_read):
        return VERDICT_UNCONFIRMED, math.nan
    if _known_false(never_read):
        return VERDICT_KEEP, math.nan
    if not _known_false(row.get("SHARED_DATABASE")):            # unknown counts as shared
        return VERDICT_SHARED, math.nan
    if not _truthy(row.get("OLDER_THAN_90D")):
        return LEVER_YOUNG, math.nan
    status = _status(row)
    active_gb = safe_float(row.get("ACTIVE_GB"))
    if status == "STALE" and active_gb > 0:
        if _shares_clone_storage(row):
            return LEVER_CLONES, math.nan
        return LEVER_DROP, round(active_gb / 1024 * rate_tb, 2)
    retention = safe_float(row.get("RETENTION_DAYS"))
    tt_gb = safe_float(row.get("TIME_TRAVEL_GB"))
    if status == "ACTIVE" and retention > target_days and tt_gb > 0:
        return LEVER_RETENTION, round(tt_gb * (retention - target_days) / retention / 1024 * rate_tb, 2)
    return LEVER_NOTHING, math.nan


def storage_waste_verdicts(frame: pd.DataFrame | None, *, rate_tb: float,
                           target_days: int = TARGET_RETENTION_DAYS) -> pd.DataFrame:
    """The storage-waste frame with LEVER and EST_MONTHLY_USD inserted right after TABLE_NAME: the SAME rows in the
    SAME order (the panel's positional row selection still maps), one lever per row (see the module docstring).

    EST_MONTHLY_USD is ESTIMATED at ``rate_tb`` ($ per TiB-month) and NaN on every row that is not a lever; a $0 rate
    keeps the lever with an estimate of 0.0 (lever_rows then drops it). A frame without the read-evidence columns
    (the DML-only fallback) is 'Unconfirmed' on every row. None / empty -> an empty frame with the two columns.
    Pure; never raises."""
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        base = frame.copy() if isinstance(frame, pd.DataFrame) else pd.DataFrame()
        for col in LEVER_COLUMNS:
            if col not in base.columns:
                base[col] = pd.Series(dtype="object" if col == "LEVER" else "float64")
        return base
    df = frame.copy().drop(columns=[c for c in LEVER_COLUMNS if c in frame.columns])
    rate = safe_float(rate_tb)
    # never below 0, so the retention leg (RETENTION_DAYS > target) never divides by a zero retention
    target = max(0, int(safe_float(target_days, default=float(TARGET_RETENTION_DAYS))))
    if all(col in df.columns for col in _EVIDENCE):
        pairs = [_verdict(row, rate, target) for _, row in df.iterrows()]
    else:                                                      # the DML-only fallback: no read evidence
        pairs = [(VERDICT_UNCONFIRMED, math.nan)] * len(df)
    at = list(df.columns).index("TABLE_NAME") + 1 if "TABLE_NAME" in df.columns else 0
    df.insert(at, "LEVER", [lever for lever, _ in pairs])
    df.insert(at + 1, "EST_MONTHLY_USD", pd.Series([est for _, est in pairs], index=df.index, dtype="float64"))
    return df


def lever_rows(verdicts: pd.DataFrame | None) -> pd.DataFrame:
    """The rows that count in Addressable $/mo: a lever (Archive or drop / Cut retention) with a positive estimate.
    An empty frame for None or a frame without LEVER / EST_MONTHLY_USD."""
    if not isinstance(verdicts, pd.DataFrame) or not set(LEVER_COLUMNS).issubset(verdicts.columns):
        return pd.DataFrame(columns=list(LEVER_COLUMNS))
    est = pd.to_numeric(verdicts["EST_MONTHLY_USD"], errors="coerce")
    return verdicts[verdicts["LEVER"].isin(list(LEVER_SOURCE)) & (est > 0)]


def table_fqn(row: Mapping[str, object]) -> str:
    """DATABASE.SCHEMA.TABLE from the row's raw names: the RETENTION booking's TARGET_OBJECT spelling (the retention
    control joins the same three columns), so unread_maintenance.object_key matches a booking to its row."""
    return f"{_text(row.get('DATABASE_NAME'))}.{_text(row.get('SCHEMA_NAME'))}.{_text(row.get('TABLE_NAME'))}"


_ABSENT_OBJECT_RE = re.compile(r"object '([^']+)' does not exist or not authorized", re.IGNORECASE)
_NOTE_ACCESS_HISTORY = ("ACCESS_HISTORY is not visible to this app (it needs Enterprise edition and IMPORTED "
                        "PRIVILEGES on the SNOWFLAKE database)")
_NOTE_GRANTS = ("GRANTS_TO_ROLES (the share guard) is not visible to this app (it needs IMPORTED PRIVILEGES on the "
                "SNOWFLAKE database)")
_NOTE_EITHER = ("ACCESS_HISTORY or GRANTS_TO_ROLES (the share guard) is not visible to this app (both need IMPORTED "
                "PRIVILEGES on the SNOWFLAKE database; ACCESS_HISTORY also needs Enterprise edition)")


def reads_unavailable_note(error_kind: object, error: object = "") -> str:
    """Why the storage-waste scan has no read evidence, from run()'s classified error kind (the degraded caption).

    The scan's one statement reads two objects its DML-only fallback does not: ACCESS_HISTORY (the reads) and
    GRANTS_TO_ROLES (the share guard). An error naming the edition / an unsupported feature blames ACCESS_HISTORY's
    edition and the SNOWFLAKE grant. An object-not-visible failure ('absent') names the object Snowflake's error
    names, ACCESS_HISTORY or GRANTS_TO_ROLES, and both when the error names none; for any other object it shows the
    error itself. A timeout says so; anything else shows the error itself (whitespace collapsed, at most 300
    characters). This account reads ACCESS_HISTORY daily (the object-cost loader), so a timeout or a transient
    fault is the likelier cause. The Database filter never narrows this scan, so the note never offers it."""
    kind = str(error_kind or "").strip().lower()
    err = " ".join(str(error or "").split())[:300]
    low = err.lower()
    failed = "the access-history read failed" + (f": {err}" if err else "")
    if "enterprise" in low or "unsupported feature" in low:
        return _NOTE_ACCESS_HISTORY
    if kind == "absent":
        found = _ABSENT_OBJECT_RE.search(err)
        named = found.group(1).replace('"', "").strip().upper().rsplit(".", 1)[-1] if found else ""
        if not named:
            return _NOTE_EITHER
        return {"ACCESS_HISTORY": _NOTE_ACCESS_HISTORY, "GRANTS_TO_ROLES": _NOTE_GRANTS}.get(named, failed)
    if kind == "timeout":
        return f"the {UNREAD_DAYS}-day access-history read timed out"
    return failed
