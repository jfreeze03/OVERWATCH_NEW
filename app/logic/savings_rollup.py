"""Aggregate the addressable-savings backlog into one de-duplicated headline (rec#16).

Each advisor estimates recoverable dollars independently and books into
SAVINGS_LEDGER; nothing summed them into a single "total addressable monthly
savings". Naive summing DOUBLE-COUNTS, because idle-tune and size-down on the SAME
warehouse both recover the same idle credits, and stopping maintenance on a table,
cutting its retention or dropping it address overlapping money on the SAME table.
This module de-duplicates those overlaps on the same warehouse or table (keep the
larger), then ranks the rest by confidence x dollars. Pure; no Streamlit.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd

from app.logic.formulas import humanize_duration, safe_float
from app.logic.storage_waste import LEVER_SOURCE, lever_rows, table_fqn
from app.logic.unread_maintenance import ACTION_VERDICTS, object_key

# Sources that address the SAME money on the same target — keep only the largest.
# Idle-tune and right-sizing both recover a warehouse's idle credits (keyed on the raw warehouse name).
# One saving per table (Next-Fifty #35, keyed on unread_maintenance.object_key, so a quoted or lower-case twin
# of the same db.schema.table merges): suspending clustering on an unread table (#30) and fixing its churny
# clustering both recover its clustering credits; recluster rewrites create the Time Travel a retention cut
# would release; and a drop ends all of it. The two groups are disjoint.
_OBJECT_GROUP = frozenset({"CLUSTERING", "UNREAD_MAINT", "STORAGE", "RETENTION"})
_OVERLAP_GROUPS: tuple[frozenset[str], ...] = (frozenset({"IDLE", "RESIZE"}), _OBJECT_GROUP)

_CONFIDENCE_WEIGHT = {"HIGH": 1.0, "VERIFIED": 1.0, "MEDIUM": 0.6,
                      "ESTIMATED": 0.5, "LOW": 0.3}


def confidence_weight(label: object) -> float:
    """Map an advisor's confidence label to a 0..1 weight (unknown -> low)."""
    return _CONFIDENCE_WEIGHT.get(str(label).upper(), 0.3)


# Effort proxy from the advisor source: a single ALTER (IDLE/RESIZE) vs a costly
# re-cluster (CLUSTERING). Lets the panel flag quick wins a dollar ranking hides.
_EFFORT_TIER = {
    "IDLE": "LOW", "RESIZE": "LOW", "UNREAD_MAINT": "LOW",
    "WASTE": "MEDIUM", "STORAGE": "MEDIUM", "RETENTION": "MEDIUM", "LEDGER": "MEDIUM",
    "CLUSTERING": "HIGH",
}


def effort_tier(source: object) -> str:
    """Rough implementation effort for an advisor source (Cost #8) — the quick-win
    signal a dollar ranking hides. IDLE/RESIZE are one ALTER; CLUSTERING is a costly
    re-cluster. Unknown sources default to MEDIUM."""
    return _EFFORT_TIER.get(str(source).upper(), "MEDIUM")


@dataclass(frozen=True)
class SavingsOpportunity:
    source: str            # IDLE | RESIZE | STORAGE | RETENTION | CLUSTERING | WASTE | LEDGER | UNREAD_MAINT
    target: str            # the warehouse / table / db the saving is on
    monthly_usd: float
    confidence: float      # 0..1


@dataclass(frozen=True)
class SavingsRollup:
    total_monthly_usd: float
    items: tuple[SavingsOpportunity, ...]     # kept, ranked by confidence x dollars desc
    dropped: tuple[SavingsOpportunity, ...]   # overlap double-counts removed


def idle_opportunities(advisor: pd.DataFrame | None) -> list[SavingsOpportunity]:
    """rec#16 IDLE leg, extracted from Cost ▸ Optimize (v4.597) so Optimize and Proof ▸ Pipeline build
    the identical list: one SavingsOpportunity per insights.idle_advisor row with a positive NET
    actionable timer saving (ACTIONABLE_MONTHLY_USD), weighted by its SAVINGS_CONFIDENCE label.
    Behaviour is byte-equal to the inline generator it replaced; None / empty -> []."""
    if advisor is None or advisor.empty:
        return []
    return [SavingsOpportunity("IDLE", str(r["WAREHOUSE_NAME"]),
                               safe_float(r["ACTIONABLE_MONTHLY_USD"]),
                               confidence_weight(r.get("SAVINGS_CONFIDENCE")))
            for _, r in advisor.iterrows()
            if safe_float(r["ACTIONABLE_MONTHLY_USD"]) > 0]


def resize_opportunities(sized: pd.DataFrame | None) -> list[SavingsOpportunity]:
    """rec#16 RESIZE leg (right-sizing; overlaps IDLE per warehouse — rollup_savings keeps the larger),
    extracted from Cost ▸ Optimize (v4.597): one SavingsOpportunity per sizing.size_recommendations row
    with a positive POTENTIAL_MONTHLY_SAVING_USD, weighted by its CONFIDENCE label. Byte-equal to the
    inline generator it replaced; None / empty -> []."""
    if sized is None or sized.empty:
        return []
    return [SavingsOpportunity("RESIZE", str(r["WAREHOUSE_NAME"]),
                               safe_float(r.get("POTENTIAL_MONTHLY_SAVING_USD")),
                               confidence_weight(r.get("CONFIDENCE")))
            for _, r in sized.iterrows()
            if safe_float(r.get("POTENTIAL_MONTHLY_SAVING_USD")) > 0]


def unread_maintenance_opportunities(verdicts: pd.DataFrame | None) -> list[SavingsOpportunity]:
    """Next-Fifty #30 UNREAD_MAINT leg (the #35 de-duplicated total; reaches Addressable $/mo only through
    unread_handoff, i.e. from a CONFIRMED Storage & waste scan): one SavingsOpportunity per
    unread_maintenance.unread_maintenance_verdicts row with an ACTION verdict and a positive EST_MONTHLY_USD
    (ESTIMATED, so MEDIUM confidence), keyed on OBJECT_FQN so it de-duplicates against a CLUSTERING leg on the
    same table. None / empty -> []."""
    if verdicts is None or verdicts.empty or "VERDICT" not in verdicts.columns:
        return []
    return [SavingsOpportunity("UNREAD_MAINT", str(r["OBJECT_FQN"]), safe_float(r.get("EST_MONTHLY_USD")),
                               confidence_weight("MEDIUM"))
            for _, r in verdicts.iterrows()
            if str(r.get("VERDICT")) in ACTION_VERDICTS and safe_float(r.get("EST_MONTHLY_USD")) > 0]


def storage_waste_opportunities(verdicts: pd.DataFrame | None) -> list[SavingsOpportunity]:
    """Next-Fifty #35 storage leg (reaches Addressable $/mo only through storage_handoff, i.e. from a Storage & waste
    scan WITH read evidence): one SavingsOpportunity per storage_waste.lever_rows row, sourced STORAGE (Archive or
    drop) or RETENTION (Cut retention), keyed on DATABASE.SCHEMA.TABLE (storage_waste.table_fqn) so it de-duplicates
    against an UNREAD_MAINT or CLUSTERING leg on the same table (one saving per table), at the row's ESTIMATED
    EST_MONTHLY_USD, so MEDIUM confidence. None / empty / no LEVER column -> []."""
    rows = lever_rows(verdicts)
    return [SavingsOpportunity(LEVER_SOURCE[str(r["LEVER"])], table_fqn(r), safe_float(r.get("EST_MONTHLY_USD")),
                               confidence_weight("MEDIUM"))
            for _, r in rows.iterrows()]


# --- Next-Fifty #35: the UNREAD_MAINT lever's session handoff ------------------------------------------------
# A confirmed-unread verdict exists only after the Storage & waste toggle + its access-history confirm, so
# that section publishes a primitives-only snapshot under UNREAD_HANDOFF_KEY (a NON-widget session key,
# never cleared on toggle-off: Streamlit resets the toggle whenever the viewer leaves the section) and the
# Addressable $/mo headlines (Cost ▸ Optimize ▸ Idle & sizing, Proof ▸ Pipeline) read it back through
# unread_lever. The ONLY path from unread data to a headline is unread_handoff -> session -> unread_lever.
UNREAD_HANDOFF_KEY = "_ow_unread_maint_handoff"
# 1h, set equal to app.core.query.CACHE_TTLS["historical"] (test-pinned). It bounds the time since the Storage &
# waste panel was last SHOWN (the stamp is re-taken on every show, disclosed as '1h after this panel was last
# shown'), NOT the age of the access-history confirm behind it: that is served from the historical cache, so it
# can be up to 1h old when stamped. A 'no read in 90 days' verdict does not move in that time (R1-17).
UNREAD_HANDOFF_MAX_AGE_SEC = 3600
# R1-17: the stamp and the reader's 'now' are aware UTC (formulas.utc_now, passed in by the UI), so the age never
# jumps at a DST change; a stamp more than this far in the future (a clock step) is stale, never fresh.
UNREAD_HANDOFF_MAX_SKEW_SEC = 60
# Review r2 R2-6 / R2-9: the Savings-ledger read that leaves booked objects out (Storage & waste, key
# booked_unread_ledger) is on the 'recent' tier, so it can be up to this old when the scan runs: set equal to
# app.core.query.CACHE_TTLS["recent"] (test-pinned, with the read's tier). No cache salt sees another session's
# booking, so a re-run inside that time still counts the object, and its stamp lives UNREAD_HANDOFF_MAX_AGE_SEC more.
BOOKED_LEDGER_CACHE_SEC = 300
UNREAD_CONFIRMED = "confirmed"
UNREAD_CLEAN = "clean"
UNREAD_CONFIRM_FAILED = "confirm_failed"
UNREAD_SHORTLIST_FAILED = "shortlist_failed"
UNREAD_LEDGER_FAILED = "ledger_failed"
_UNREAD_STATUSES = frozenset({UNREAD_CONFIRMED, UNREAD_CLEAN, UNREAD_CONFIRM_FAILED, UNREAD_SHORTLIST_FAILED,
                              UNREAD_LEDGER_FAILED})
LEVER_LABEL = {"IDLE": "idle timer", "RESIZE": "right-sizing", "UNREAD_MAINT": "unread maintenance",
               "STORAGE_WASTE": "storage waste"}

_MAX_AGE = humanize_duration(UNREAD_HANDOFF_MAX_AGE_SEC)
_LEDGER_CACHE = humanize_duration(BOOKED_LEDGER_CACHE_SEC)
_MAX_ELSEWHERE = humanize_duration(UNREAD_HANDOFF_MAX_AGE_SEC + BOOKED_LEDGER_CACHE_SEC)
# why the lever is absent ({where} = the call site's path to Storage & waste)
R_NOT_RUN = "not checked this session: run the unread-maintenance scan in {where}"
R_COMPANY = "last checked for {handoff_company}, not {company}: re-run the scan in {where}"
R_DATABASE = ("last checked for database {database} only: clear the Database filter and re-run the scan in "
              "{where}")
R_STALE = ("the last check was shown over " + _MAX_AGE + " ago, or cached data was refreshed since: re-run the "
           "scan in {where}")
R_SHORTLIST = "the object-cost ledger could not be read in {where}"
R_CONFIRM = "the access-history check failed, so no object is confirmed unread"
R_LEDGER = "the Savings ledger could not be read, so objects already booked there could not be left out"
R_RATE = ("the credit rate changed since the last check, which priced its objects at the old rate: re-run the scan "
          "in {where}")
# qualifiers on a counted lever
N_FLOOR = "only the top {checked:,} shortlisted objects were checked, so this is a floor"
N_BOOKED = "{n:,} already booked on the Savings ledger left out"
# R1-16: the booked set comes from a 'recent'-tier ledger read when the scan runs, so it can be up to
# BOOKED_LEDGER_CACHE_SEC old (R2-6 / R2-9); a same-session booking updates it in that run, but no cache salt can see
# another session's INSERT, so that case is disclosed (the Storage & waste line + both headlines' help): a re-run
# inside the cache time still counts the object and re-stamps the handoff, so it counts until the scan is re-run at
# least that long after the booking, and at most the handoff's hour plus the cache time after it.
N_ELSEWHERE = ("An object booked in another session keeps counting here until the scan is re-run at least "
               + _LEDGER_CACHE + " after that booking (the Savings-ledger read that leaves booked objects out is "
               "cached for up to " + _LEDGER_CACHE + "): at most " + _MAX_ELSEWHERE + " after the booking.")
# The same rule in both Addressable $/mo headlines' help (Cost ▸ Optimization & Savings, Proof ▸ Pipeline).
H_BOOKED = ("less any already booked on the Savings ledger as of the scan's ledger read, which is cached for up to "
            + _LEDGER_CACHE + " (an object booked in another session keeps counting until the scan is re-run at "
            "least " + _LEDGER_CACHE + " after that booking: at most " + _MAX_ELSEWHERE + ")")
# Storage & waste, when the booked-objects read failed (rendered as 'unavailable')
S_LEDGER_UNAVAILABLE = ("The Savings ledger could not be read, so confirmed objects are not added to Addressable "
                        "$/mo (objects already booked could not be left out).")

# --- Next-Fifty #35 storage leg: the STORAGE_WASTE lever's session handoff ----------------------------------
# The same design as the unread-maintenance lever: the Storage & waste storage-waste scan (behind its toggle)
# publishes a primitives-only snapshot under STORAGE_HANDOFF_KEY, never cleared on toggle-off, and both Addressable
# $/mo headlines read it back through storage_lever (zero reads). The ONLY path from storage data to a headline is
# storage_handoff -> session -> storage_lever. Freshness, Company and cache-scope rules are the unread lever's
# (_scope_reason; the Database filter never narrows this scan, so its handoff is never Database-scoped); the rows
# are priced at the storage rate (STORAGE_USD_PER_TB_MONTH), so a storage-rate change drops them.
STORAGE_HANDOFF_KEY = "_ow_storage_waste_handoff"
STORAGE_CONFIRMED = "confirmed"          # the scan with read evidence ran (rows = the lever rows)
STORAGE_CLEAN = "clean"                  # no table of 1 GB or more in scope: counted at $0
STORAGE_NO_READS = "no_reads"            # only the DML-only fallback answered: nothing is confirmed unread
STORAGE_SCAN_FAILED = "scan_failed"      # both reads failed
STORAGE_LEDGER_FAILED = "ledger_failed"  # the booked-tables read failed or was row-capped, with rows to count
_STORAGE_STATUSES = frozenset({STORAGE_CONFIRMED, STORAGE_CLEAN, STORAGE_NO_READS, STORAGE_SCAN_FAILED,
                               STORAGE_LEDGER_FAILED})
_STORAGE_SOURCES = frozenset(LEVER_SOURCE.values())
R_S_NOT_RUN = "not checked this session: run the storage-waste scan in {where}"
R_S_SCAN = "the storage-waste scan could not be read in {where}"
R_S_NO_READS = "the storage-waste scan had no read evidence, so no table is confirmed unread"
R_S_RATE = ("the storage rate changed since the last check, which priced its tables at the old rate: re-run the "
            "scan in {where}")
N_S_FLOOR = "only the top {checked:,} tables by retention bytes were checked, so this is a floor"
S_STORAGE_LEDGER_UNAVAILABLE = ("The Savings ledger could not be read, so unread tables are not added to "
                                "Addressable $/mo (tables already booked could not be left out).")


@dataclass(frozen=True)
class UnreadLever:
    included: bool                                   # counted in the headline (possibly at $0)
    opportunities: tuple[SavingsOpportunity, ...]    # UNREAD_MAINT rows to extend the rollup with
    reason: str                                      # why it is absent ('' when included)
    note: str                                        # qualifier when included (floor / booked left out)


def _norm_company(value: object) -> str:
    return str(value or "").strip().upper() or "ALL"


def unread_handoff(verdicts: pd.DataFrame | None, *, status: str, company: str, database: object, scope: str,
                   as_of: datetime, rate: float, checked: int = 0, truncated: bool = False,
                   booked: frozenset[str] | None = frozenset()) -> dict[str, object]:
    """The Storage & waste snapshot of the UNREAD_MAINT lever (primitives only: safe in session state).

    Only a CONFIRMED scan carries rows (the action verdicts' ESTIMATED $/mo via
    unread_maintenance_opportunities, the generator's ONLY call site outside tests), less the objects already
    booked on the Savings ledger (``booked``, from unread_maintenance.booked_objects). ``booked`` None = that
    read failed: with rows to count, the status becomes UNREAD_LEDGER_FAILED and nothing is counted, rather
    than risk a double count. ``as_of`` is the caller's aware UTC 'now' (formulas.utc_now; this module reads no
    clock): a naive stamp never reads fresh. ``rate`` is the credit rate the rows' $/mo were priced at, so a
    reader on another rate drops them (R1-16) instead of adding old-rate dollars to a new-rate idle figure."""
    opps = unread_maintenance_opportunities(verdicts) if status == UNREAD_CONFIRMED else []
    if status == UNREAD_CONFIRMED and opps and booked is None:
        status, opps = UNREAD_LEDGER_FAILED, []
    left_out = booked or frozenset()
    kept = [o for o in opps if object_key(o.target) not in left_out]
    return {
        "status": str(status),
        "company": _norm_company(company),
        "database": str(database or "").strip().upper(),
        "scope": str(scope or ""),
        "as_of": as_of.isoformat(timespec="seconds"),
        "rate": float(safe_float(rate)),
        "checked": int(checked),
        "truncated": bool(truncated),
        "booked_excluded": len(opps) - len(kept),
        "rows": [[str(o.target), float(o.monthly_usd), float(o.confidence)] for o in kept],
    }


def _absent(reason: str) -> UnreadLever:
    return UnreadLever(False, (), reason, "")


def _handoff_age_sec(as_of: object, now: datetime) -> float:
    """Seconds from the handoff's stamp to ``now``; inf (stale) for a malformed stamp or when either side is
    naive, so a wall-clock stamp is never compared across a DST change (R1-17). Taken in UTC: Python subtracts
    two datetimes sharing one tzinfo as wall clocks, which would repeat the fall-back hour."""
    try:
        stamped = datetime.fromisoformat(str(as_of))
    except (TypeError, ValueError):
        return math.inf
    if stamped.utcoffset() is None or now.utcoffset() is None:
        return math.inf
    return (now.astimezone(UTC) - stamped.astimezone(UTC)).total_seconds()


def _scope_reason(handoff: Mapping[str, object], *, company: str, scope: str, now: datetime, where: str) -> str:
    """The checks every session handoff shares (the unread-maintenance and storage-waste levers), in order: another
    Company -> R_COMPANY; a Database-scoped scan -> R_DATABASE; a different cache scope (Refresh or a global-salt
    write since), older than UNREAD_HANDOFF_MAX_AGE_SEC, stamped over UNREAD_HANDOFF_MAX_SKEW_SEC in the future, or a
    naive clock on either side -> R_STALE. '' when the handoff is in scope and fresh. Pure; never raises."""
    handoff_company = _norm_company(handoff.get("company"))
    if handoff_company != _norm_company(company):
        return R_COMPANY.format(handoff_company=handoff_company, company=_norm_company(company), where=where)
    database = str(handoff.get("database") or "").strip().upper()
    if database:
        return R_DATABASE.format(database=database, where=where)
    age = _handoff_age_sec(handoff.get("as_of"), now)
    if (str(handoff.get("scope") or "") != str(scope or "")
            or not -UNREAD_HANDOFF_MAX_SKEW_SEC <= age <= UNREAD_HANDOFF_MAX_AGE_SEC):
        return R_STALE.format(where=where)
    return ""


def _same_rate(stamped: object, rate: float) -> bool:
    """The handoff's pricing rate equals the reader's (a missing or non-numeric stamp never matches)."""
    if isinstance(stamped, bool) or not isinstance(stamped, (int, float)):
        return False
    a, b = float(stamped), safe_float(rate, default=math.nan)
    return math.isfinite(a) and math.isfinite(b) and math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


def unread_lever(handoff: object, *, company: str, scope: str, now: datetime, rate: float,
                 where: str) -> UnreadLever:
    """Read the Storage & waste handoff back for a headline. The first failing check wins: no (or a malformed)
    handoff -> not checked; another Company; a Database-scoped scan (Idle & sizing, right-sizing and Proof ▸
    Pipeline ignore that filter, so it is never mixed in); a different cache scope (Refresh or a global-salt
    write since), older than UNREAD_HANDOFF_MAX_AGE_SEC, stamped over UNREAD_HANDOFF_MAX_SKEW_SEC in the
    future, or a naive clock on either side -> stale; a failed shortlist / confirm / ledger read; rows priced
    at a credit rate other than ``rate`` (the reader's current one: R1-16) -> re-run. A clean or confirmed
    scan is included (at $0 when it has no rows), with a note for a top-N floor or booked objects left out.
    ``now`` is the caller's aware UTC 'now' (formulas.utc_now). Pure; never raises."""
    status = handoff.get("status") if isinstance(handoff, Mapping) else None
    if not isinstance(handoff, Mapping) or not isinstance(status, str) or status not in _UNREAD_STATUSES:
        return _absent(R_NOT_RUN.format(where=where))
    scoped = _scope_reason(handoff, company=company, scope=scope, now=now, where=where)
    if scoped:
        return _absent(scoped)
    if status == UNREAD_SHORTLIST_FAILED:
        return _absent(R_SHORTLIST.format(where=where))
    if status == UNREAD_CONFIRM_FAILED:
        return _absent(R_CONFIRM)
    if status == UNREAD_LEDGER_FAILED:
        return _absent(R_LEDGER)
    rows = handoff.get("rows")
    rows = rows if isinstance(rows, (list, tuple)) else ()
    if rows and not _same_rate(handoff.get("rate"), rate):
        return _absent(R_RATE.format(where=where))       # $0 (no rows) is the same at any rate
    opps: list[SavingsOpportunity] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 3:
            continue
        target, usd, conf = row
        if str(target or "").strip() and safe_float(usd) > 0:
            opps.append(SavingsOpportunity("UNREAD_MAINT", str(target), safe_float(usd), safe_float(conf)))
    notes: list[str] = []
    if bool(handoff.get("truncated")):
        notes.append(N_FLOOR.format(checked=int(safe_float(handoff.get("checked")))))
    booked_n = int(safe_float(handoff.get("booked_excluded")))
    if booked_n > 0:
        notes.append(N_BOOKED.format(n=booked_n))
    return UnreadLever(True, tuple(opps), "", "; ".join(notes))


def _lever_label(source: str) -> str:
    return LEVER_LABEL.get(str(source).upper(), str(source).lower())


def lever_basis(counted: Sequence[str], absent: Mapping[str, str], notes: Mapping[str, str] | None = None) -> str:
    """The 'Levers counted / Not counted' line under an Addressable $/mo headline: every lever named, each
    counted one with its qualifier, each absent one with why."""
    parts = []
    for source in counted:
        note = (notes or {}).get(source, "")
        parts.append(f"{_lever_label(source)} ({note})" if note else _lever_label(source))
    text = "Levers counted: " + (" + ".join(parts) if parts else "none") + "."
    missing = [f"{_lever_label(source)} ({why})" if why else _lever_label(source) for source, why in absent.items()]
    if missing:
        text += " Not counted: " + "; ".join(missing) + "."
    return text


def lever_short(counted: Sequence[str]) -> str:
    """Proof ▸ Pipeline's delta wording: 'idle-timer only' (the pre-#35 default), or the labels joined."""
    if list(counted) == ["IDLE"]:
        return "idle-timer only"
    if not counted:
        return "no lever counted"
    return " + ".join(_lever_label(source) for source in counted)


def unread_handoff_note(handoff: object) -> str:
    """The Storage & waste line saying whether this scan's objects join Addressable $/mo ('' when there is
    nothing to say: no scan, a clean scan, a failed shortlist, or a failed ledger read, which renders its own
    'unavailable' state)."""
    if not isinstance(handoff, Mapping) or handoff.get("status") in (UNREAD_CLEAN, UNREAD_SHORTLIST_FAILED,
                                                                     UNREAD_LEDGER_FAILED):
        return ""
    status = handoff.get("status")
    database = str(handoff.get("database") or "").strip().upper()
    if database:
        return (f"Not added to Addressable $/mo: this scan is narrowed to database {database}. Clear the Database "
                "filter and re-run it to count confirmed objects in Idle & sizing and on Proof ▸ Pipeline.")
    if status == UNREAD_CONFIRM_FAILED:
        return "Not added to Addressable $/mo: no object is confirmed unread."
    if status != UNREAD_CONFIRMED:
        return ""
    rows = handoff.get("rows")
    n = len(rows) if isinstance(rows, (list, tuple)) else 0
    booked_n = int(safe_float(handoff.get("booked_excluded")))
    if not n:
        if booked_n:
            return (f"No confirmed-unread object to add to Addressable $/mo: the {booked_n:,} confirmed are "
                    "already booked on the Savings ledger.")
        return "No confirmed-unread object to add to Addressable $/mo."
    floor = (f" (a floor: only the top {int(safe_float(handoff.get('checked'))):,} shortlisted objects were "
             "checked)" if bool(handoff.get("truncated")) else "")
    booked = f" {booked_n:,} already booked on the Savings ledger are left out." if booked_n else ""
    return (f"{n:,} confirmed-unread object(s) join Addressable $/mo in Idle & sizing and on Proof ▸ Pipeline for "
            f"this Company{floor}.{booked} They drop out when cached data is refreshed, the credit rate changes, or "
            f"{_MAX_AGE} after this panel was last shown. {N_ELSEWHERE}")


def storage_handoff(verdicts: pd.DataFrame | None, *, status: str, company: str, database: object, scope: str,
                    as_of: datetime, rate: float, checked: int = 0, truncated: bool = False,
                    booked: frozenset[str] | None = frozenset()) -> dict[str, object]:
    """The Storage & waste snapshot of the STORAGE_WASTE lever (primitives only: safe in session state).

    Only a CONFIRMED scan (the read-evidence frame, run through storage_waste.storage_waste_verdicts) carries rows,
    via storage_waste_opportunities (the generator's ONLY call site outside tests), less the tables already booked
    on the Savings ledger (``booked``: unread_maintenance.booked_objects over storage_waste.STORAGE_BOOKED_TYPES).
    ``booked`` None = that read failed or was row-capped: with rows to count, the status becomes
    STORAGE_LEDGER_FAILED and nothing is counted, rather than risk a double count. ``as_of`` is the caller's aware
    UTC 'now' (formulas.utc_now); ``rate`` is the storage $/TiB-month the rows were priced at, so a reader on
    another rate drops them. Rows are [source, target, usd, confidence]: the lever has two sources."""
    opps = storage_waste_opportunities(verdicts) if status == STORAGE_CONFIRMED else []
    if status == STORAGE_CONFIRMED and opps and booked is None:
        status, opps = STORAGE_LEDGER_FAILED, []
    left_out = booked or frozenset()
    kept = [o for o in opps if object_key(o.target) not in left_out]
    return {
        "status": str(status),
        "company": _norm_company(company),
        "database": str(database or "").strip().upper(),
        "scope": str(scope or ""),
        "as_of": as_of.isoformat(timespec="seconds"),
        "rate": float(safe_float(rate)),
        "checked": int(checked),
        "truncated": bool(truncated),
        "booked_excluded": len(opps) - len(kept),
        "rows": [[str(o.source), str(o.target), float(o.monthly_usd), float(o.confidence)] for o in kept],
    }


def storage_lever(handoff: object, *, company: str, scope: str, now: datetime, rate: float,
                  where: str) -> UnreadLever:
    """Read the storage-waste handoff back for a headline (UnreadLever is the shared lever shape). The first failing
    check wins: no (or a malformed) handoff -> not checked; the shared scope checks (_scope_reason: another Company,
    a Database-scoped handoff, a different cache scope, older than UNREAD_HANDOFF_MAX_AGE_SEC, stamped over
    UNREAD_HANDOFF_MAX_SKEW_SEC in the future, a naive clock); both reads failed; no read evidence; a failed booked-
    tables read; rows priced at a storage rate other than ``rate`` -> re-run. A clean or confirmed scan is included
    (at $0 when it has no rows), with a note for the top-50 floor or booked tables left out. A malformed row is
    skipped. ``now`` is the caller's aware UTC 'now' (formulas.utc_now). Pure; never raises."""
    status = handoff.get("status") if isinstance(handoff, Mapping) else None
    if not isinstance(handoff, Mapping) or not isinstance(status, str) or status not in _STORAGE_STATUSES:
        return _absent(R_S_NOT_RUN.format(where=where))
    scoped = _scope_reason(handoff, company=company, scope=scope, now=now, where=where)
    if scoped:
        return _absent(scoped)
    if status == STORAGE_SCAN_FAILED:
        return _absent(R_S_SCAN.format(where=where))
    if status == STORAGE_NO_READS:
        return _absent(R_S_NO_READS)
    if status == STORAGE_LEDGER_FAILED:
        return _absent(R_LEDGER)
    rows = handoff.get("rows")
    rows = rows if isinstance(rows, (list, tuple)) else ()
    if rows and not _same_rate(handoff.get("rate"), rate):
        return _absent(R_S_RATE.format(where=where))     # $0 (no rows) is the same at any rate
    opps: list[SavingsOpportunity] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 4:
            continue
        source, target, usd, conf = row
        if (isinstance(source, str) and source in _STORAGE_SOURCES and str(target or "").strip()
                and safe_float(usd) > 0):
            opps.append(SavingsOpportunity(str(source), str(target), safe_float(usd), safe_float(conf)))
    notes: list[str] = []
    if bool(handoff.get("truncated")):
        notes.append(N_S_FLOOR.format(checked=int(safe_float(handoff.get("checked")))))
    booked_n = int(safe_float(handoff.get("booked_excluded")))
    if booked_n > 0:
        notes.append(N_BOOKED.format(n=booked_n))
    return UnreadLever(True, tuple(opps), "", "; ".join(notes))


def storage_handoff_note(handoff: object) -> str:
    """The Storage & waste line saying whether this scan's tables join Addressable $/mo ('' when there is nothing to
    say: no scan, a clean scan, a failed scan (guard() already said so), or a failed booked-tables read, which renders
    its own 'unavailable' state)."""
    if not isinstance(handoff, Mapping):
        return ""
    status = handoff.get("status")
    if status == STORAGE_NO_READS:
        return "Not added to Addressable $/mo: without read evidence no table is confirmed unread."
    if status != STORAGE_CONFIRMED:
        return ""
    rows = handoff.get("rows")
    n = len(rows) if isinstance(rows, (list, tuple)) else 0
    booked_n = int(safe_float(handoff.get("booked_excluded")))
    if not n:
        if booked_n:
            return (f"No unread table to add to Addressable $/mo: the {booked_n:,} that qualify are already booked "
                    "on the Savings ledger.")
        return "No unread table to add to Addressable $/mo."
    floor = (f" (a floor: only the top {int(safe_float(handoff.get('checked'))):,} tables by retention bytes were "
             "checked)" if bool(handoff.get("truncated")) else "")
    booked = f" {booked_n:,} already booked on the Savings ledger are left out." if booked_n else ""
    return (f"{n:,} unread table(s) join Addressable $/mo in Idle & sizing and on Proof ▸ Pipeline for this "
            f"Company{floor}.{booked} They drop out when cached data is refreshed, the storage rate changes, or "
            f"{_MAX_AGE} after this panel was last shown. {N_ELSEWHERE}")


def _overlap_group(source: str) -> frozenset[str] | None:
    for group in _OVERLAP_GROUPS:
        if source in group:
            return group
    return None


def rollup_savings(opportunities: list[SavingsOpportunity]) -> SavingsRollup:
    """De-duplicate overlapping opportunities on the same warehouse or table, then rank.

    Within an overlap group on the same target, only the largest dollar estimate
    survives — the rest are dropped as double-counts: IDLE + RESIZE on the same
    warehouse (the raw name), and one saving per table across CLUSTERING,
    UNREAD_MAINT, STORAGE and RETENTION (the target normalized by
    unread_maintenance.object_key; the kept item keeps its own raw target).
    Opportunities in no overlap group are always kept. Non-positive estimates are
    ignored. Kept items are ranked by confidence x dollars; the total sums the
    kept items.
    """
    positive = [o for o in opportunities if safe_float(o.monthly_usd) > 0]
    kept: list[SavingsOpportunity] = []
    dropped: list[SavingsOpportunity] = []
    overlap_buckets: dict[tuple[str, frozenset[str]], list[SavingsOpportunity]] = defaultdict(list)

    for opp in positive:
        group = _overlap_group(opp.source)
        if group is None:
            kept.append(opp)
        else:
            key = object_key(opp.target) if group == _OBJECT_GROUP else opp.target
            overlap_buckets[(key, group)].append(opp)

    for bucket in overlap_buckets.values():
        ranked = sorted(bucket, key=lambda o: safe_float(o.monthly_usd), reverse=True)
        kept.append(ranked[0])
        dropped.extend(ranked[1:])

    kept.sort(key=lambda o: safe_float(o.confidence) * safe_float(o.monthly_usd), reverse=True)
    total = round(sum(safe_float(o.monthly_usd) for o in kept), 2)
    return SavingsRollup(total, tuple(kept), tuple(dropped))
