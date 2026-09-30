"""Next-Fifty #30: maintenance spend on objects nobody reads. Pure module (no Streamlit, no Snowflake).

Cost ▸ Optimization & Savings ▸ Storage & waste shortlists objects paying clustering, search-optimization or
MV-refresh credits with no read credits in the object ledger (cost_sql.maintenance_on_unread), confirms each
against ACCESS_HISTORY (insights_sql.object_reads_confirm), and this module turns the two frames into one
verdict per object, review-only SQL, and an ESTIMATED savings-ledger booking.

Verdict precedence: Unconfirmed (no measured read evidence: the confirm read failed or missed the object)
> Object gone (MATCHED_BY_ID false: no live TABLES row has this name, so it was dropped or renamed; no SQL,
no estimate in the totals) > Keep (any read) > Check share consumers (its database is shared out; a consumer account's reads never
reach this account's access history) > No recent spend (nothing in the last 30 complete days) > the action
for the arm with the most credits. Only an action verdict carries SQL, and only for a plain upper-case
three-part name (remediation fails closed on anything else). A missing measurement stays NaN (renders '—').

The booking is an idempotent INSERT of one ESTIMATED SAVINGS_LEDGER row with a runnable proof query; no
scan settles these object-level rows, so they stay ESTIMATED until verified by hand on the Savings ledger.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

import pandas as pd

from app.config import core_object
from app.core.sqlsafe import sql_literal, sql_number
from app.logic import remediation
from app.logic.formulas import safe_float

ARMS = ("CLUSTERING", "SEARCH_OPT", "MV_REFRESH")
ARM_CREDIT_COLS = {arm: f"{arm}_CREDITS" for arm in ARMS}
ARM_VERDICT = {"CLUSTERING": "Suspend clustering", "SEARCH_OPT": "Drop search optimization",
               "MV_REFRESH": "Suspend MV refresh"}
# SAVINGS_LEDGER.FINDING_TYPE (VARCHAR(40)); none is an autobooked lever, so no scan settles these rows.
ARM_FINDING_TYPE = {"CLUSTERING": "SUSPEND_RECLUSTER", "SEARCH_OPT": "DROP_SEARCH_OPTIMIZATION",
                    "MV_REFRESH": "SUSPEND_MV_REFRESH"}
# the booking dedupe spans every arm's type: one object is one saving, whichever arm dominates (review C6)
_BOOKED_TYPES_SQL = ", ".join(sql_literal(t, 40) for t in ARM_FINDING_TYPE.values())
VERDICT_KEEP = "Keep"
VERDICT_SHARED = "Check share consumers"
VERDICT_NO_RECENT = "No recent spend"
VERDICT_UNCONFIRMED = "Unconfirmed"
# PR C review C11: no live object has this name any more (the confirm's TABLES bridge found no undeleted
# row), so it was dropped or renamed: its maintenance already stopped and an ALTER would fail.
VERDICT_GONE = "Object gone"
ACTION_VERDICTS = frozenset(ARM_VERDICT.values())
UNREAD_WINDOW_DAYS = 90
# display order: the actions first, then what needs a person, then what is settled
_VERDICT_RANK = {**dict.fromkeys(ARM_VERDICT.values(), 0), VERDICT_SHARED: 1, VERDICT_NO_RECENT: 2,
                 VERDICT_GONE: 3, VERDICT_UNCONFIRMED: 4, VERDICT_KEEP: 5}
_READ_COLS = ("MATCHED_BY_ID", "SHARED_DATABASE", "READ_QUERIES", "READ_USERS", "LAST_READ", "WRITE_QUERIES")
VERDICT_COLUMNS = ("OBJECT_FQN", "OBJECT_DOMAIN", "COMPANY", "VERDICT", "ARM", "FINDING_TYPE", "EST_MONTHLY_USD",
                   "MAINT_USD", "CLUSTERING_CREDITS", "SEARCH_OPT_CREDITS", "MV_REFRESH_CREDITS", "MAINT_CREDITS",
                   "MAINT_CREDITS_30D", "WRITE_CREDITS", *_READ_COLS, "FIRST_MAINT_DAY", "LAST_MAINT_DAY",
                   "REVIEW_SQL", "REVERSE_SQL")
BOOK_NOTE = ("Stays ESTIMATED: no scan settles object-level serverless savings — verify it with the proof "
             "query on the Savings ledger.")


def _truthy(value: object) -> bool:
    """A Snowflake BOOLEAN as the driver or the shaped harness hands it back (bool, 0/1, 'TRUE'/'false')."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return False
    if isinstance(value, str):
        return value.strip().upper() in ("TRUE", "T", "Y", "YES", "1")
    try:
        return bool(value)
    except (TypeError, ValueError):
        return False


def _known_false(value: object) -> bool:
    """True only for a MEASURED false (False / 0 / 'FALSE'); None, NaN or pd.NA is unknown, never false."""
    try:
        if value is None or bool(pd.isna(value)):
            return False
    except (TypeError, ValueError):
        pass
    return not _truthy(value)


def _text(value: object) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value).strip()


def _statements(row: Mapping[str, object]) -> tuple[str | None, str | None]:
    """Review + reverse SQL for every arm the object pays for; (None, None) when the name is not plain."""
    fqn = _text(row.get("OBJECT_FQN"))
    is_mv = (safe_float(row.get("MV_REFRESH_CREDITS")) > 0
             or _text(row.get("OBJECT_DOMAIN")).upper() == "MATERIALIZED_VIEW")
    stmts: list[str] = []
    revs: list[str] = []
    try:
        if safe_float(row.get("CLUSTERING_CREDITS")) > 0:
            s, r = remediation.suspend_recluster_object(fqn, materialized_view=is_mv)
            stmts.append(s)
            revs.append(r)
        if safe_float(row.get("SEARCH_OPT_CREDITS")) > 0:
            s, r = remediation.drop_search_optimization(fqn)
            stmts.append(s)
            revs.append(r)
        if safe_float(row.get("MV_REFRESH_CREDITS")) > 0:
            s, r = remediation.suspend_mv_refresh(fqn)
            stmts.append(s)
            revs.append(r)
    except ValueError:
        return None, None
    if not stmts:
        return None, None
    return "\n".join(stmts), "\n".join(revs)


def _dominant_arm(row: Mapping[str, object]) -> str:
    """The arm with the most window credits; ties go in ARMS order."""
    best, best_credits = ARMS[0], float("-inf")
    for arm in ARMS:
        credits = safe_float(row.get(ARM_CREDIT_COLS[arm]))
        if credits > best_credits:
            best, best_credits = arm, credits
    return best


def unread_maintenance_verdicts(shortlist: pd.DataFrame | None, reads: pd.DataFrame | None, *,
                                rate: float) -> pd.DataFrame:
    """One verdict per shortlisted object (see the module docstring). ``reads`` None / empty / without
    OBJECT_FQN = the confirm read failed: every row is Unconfirmed with NaN read counts, never Keep or an
    action. EST_MONTHLY_USD = the last 30 complete days of maintenance credits x ``rate`` (ESTIMATED);
    MAINT_USD = the whole window's. Sorted action verdicts first, then by EST_MONTHLY_USD and MAINT_USD
    descending. Empty VERDICT_COLUMNS frame for no shortlist. Pure; never raises."""
    if (not isinstance(shortlist, pd.DataFrame) or shortlist.empty
            or "OBJECT_FQN" not in shortlist.columns):
        return pd.DataFrame(columns=list(VERDICT_COLUMNS))
    df = shortlist.copy().reset_index(drop=True)
    df["OBJECT_FQN"] = df["OBJECT_FQN"].map(_text)
    for col in ("CLUSTERING_CREDITS", "SEARCH_OPT_CREDITS", "MV_REFRESH_CREDITS", "MAINT_CREDITS_30D",
                "WRITE_CREDITS"):
        df[col] = df[col].map(safe_float) if col in df.columns else 0.0
    if "MAINT_CREDITS" in df.columns:
        df["MAINT_CREDITS"] = df["MAINT_CREDITS"].map(safe_float)
    else:
        df["MAINT_CREDITS"] = df[list(ARM_CREDIT_COLS.values())].sum(axis=1)
    r = safe_float(rate)
    df["MAINT_USD"] = (df["MAINT_CREDITS"] * r).round(2)
    df["EST_MONTHLY_USD"] = (df["MAINT_CREDITS_30D"] * r).round(2)
    df["ARM"] = [_dominant_arm(row) for _, row in df.iterrows()]
    for col in _READ_COLS:
        if col in df.columns:
            df = df.drop(columns=col)
    confirmed = isinstance(reads, pd.DataFrame) and not reads.empty and "OBJECT_FQN" in reads.columns
    if confirmed and reads is not None:
        rd = reads[[c for c in ("OBJECT_FQN", *_READ_COLS) if c in reads.columns]].copy()
        rd["OBJECT_FQN"] = rd["OBJECT_FQN"].map(_text)
        rd = rd.drop_duplicates("OBJECT_FQN")
        df = df.merge(rd, on="OBJECT_FQN", how="left", indicator=True)
        hit = df.pop("_merge").eq("both")
    else:
        hit = pd.Series(False, index=df.index)
    for col in ("READ_QUERIES", "READ_USERS", "WRITE_QUERIES"):
        df[col] = pd.to_numeric(df[col], errors="coerce") if col in df.columns else float("nan")
    hit = hit & df["READ_QUERIES"].notna()                  # an unmeasured read count is not a measured 0
    for col in ("READ_QUERIES", "READ_USERS", "WRITE_QUERIES"):
        df[col] = df[col].where(hit)
    for col in ("MATCHED_BY_ID", "SHARED_DATABASE", "LAST_READ"):
        if col not in df.columns:
            df[col] = None
        df[col] = df[col].astype("object").where(hit, None)
    verdicts: list[str] = []
    for i, row in df.iterrows():
        if not hit[i]:
            verdicts.append(VERDICT_UNCONFIRMED)
        elif _known_false(row["MATCHED_BY_ID"]):
            verdicts.append(VERDICT_GONE)
        elif safe_float(row["READ_QUERIES"]) > 0:
            verdicts.append(VERDICT_KEEP)
        elif _truthy(row["SHARED_DATABASE"]):
            verdicts.append(VERDICT_SHARED)
        elif safe_float(row["MAINT_CREDITS_30D"]) <= 0:
            verdicts.append(VERDICT_NO_RECENT)
        else:
            verdicts.append(ARM_VERDICT[str(row["ARM"])])
    df["VERDICT"] = verdicts
    action = df["VERDICT"].isin(ACTION_VERDICTS)
    df["FINDING_TYPE"] = [ARM_FINDING_TYPE[str(a)] if act else None
                          for a, act in zip(df["ARM"], action, strict=True)]
    sql = [(_statements(row) if act else (None, None)) for (_, row), act in zip(df.iterrows(), action, strict=True)]
    df["REVIEW_SQL"] = [s for s, _ in sql]
    df["REVERSE_SQL"] = [rv for _, rv in sql]
    for col in VERDICT_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df["_rank"] = df["VERDICT"].map(_VERDICT_RANK).fillna(9)
    df = df.sort_values(["_rank", "EST_MONTHLY_USD", "MAINT_USD", "OBJECT_FQN"],
                        ascending=[True, False, False, True], kind="mergesort").drop(columns="_rank")
    rest = [c for c in df.columns if c not in VERDICT_COLUMNS]
    return df.reset_index(drop=True)[[*VERDICT_COLUMNS, *rest]]


def confirm_failure_note(error_kind: object, error: object = "") -> str:
    """Why the access-history confirm failed, from run()'s classified error kind (PR C review C8 / C18).

    Only an object-not-visible failure ('absent': "does not exist or not authorized") or an error that names
    the edition / an unsupported feature blames the edition and the SNOWFLAKE grant; a timeout says so and how
    to narrow the scan; anything else shows the error itself. This account already reads ACCESS_HISTORY
    daily (the object-cost loader), so a timeout or a transient fault is the likelier cause there."""
    kind = str(error_kind or "").strip().lower()
    err = " ".join(str(error or "").split())[:300]
    low = err.lower()
    if kind == "absent" or "enterprise" in low or "unsupported feature" in low:
        return ("ACCESS_HISTORY is not visible to this app (it needs Enterprise edition and IMPORTED "
                "PRIVILEGES on the SNOWFLAKE database)")
    if kind == "timeout":
        return ("the 90-day access-history check timed out; set the Database filter to narrow the shortlist, "
                "then retry")
    return "the access-history check failed" + (f": {err}" if err else "")


def object_key(fqn: object) -> str:
    """The shortlist's normalized object key, UPPER(REPLACE(OBJECT_FQN, '"', '')) (cost_sql.maintenance_on_unread
    groups on it), so a quoted or lower-case ledger TARGET_OBJECT names the same object as its verdict row."""
    return _text(fqn).replace('"', "").upper()


_BOOKED_TYPES = frozenset(ARM_FINDING_TYPE.values())


def _cell(value: object) -> str:
    """A ledger cell as text; None / NaN / pd.NA read as '' (a NULL), never the string '<NA>'."""
    try:
        if value is None or bool(pd.isna(value)):
            return ""
    except (TypeError, ValueError):
        pass
    return _text(value)


def booked_objects(ledger: pd.DataFrame | None) -> frozenset[str] | None:
    """Next-Fifty #35: the objects already booked on the Savings ledger under ANY unread-maintenance finding type
    in any state but REJECTED: the predicate book_estimated_sql's WHERE NOT EXISTS refuses a second booking on
    (a NULL STATE is not '<> REJECTED' there either), so an object the Book button would refuse is never counted
    in Addressable $/mo. Keys are object_key(TARGET_OBJECT); FINDING_TYPE / STATE compare UPPER(TRIM()).

    None / not a DataFrame -> None (unknown: the caller leaves the lever out rather than risk a double count);
    an empty frame -> frozenset() (nothing booked); a non-empty frame without TARGET_OBJECT, FINDING_TYPE or
    STATE -> None. Pure; never raises."""
    if not isinstance(ledger, pd.DataFrame):
        return None
    if ledger.empty:
        return frozenset()
    if not {"TARGET_OBJECT", "FINDING_TYPE", "STATE"}.issubset(ledger.columns):
        return None
    out: set[str] = set()
    for target, ftype, state in zip(ledger["TARGET_OBJECT"], ledger["FINDING_TYPE"], ledger["STATE"],
                                    strict=True):
        live = _cell(state).upper()
        if _cell(ftype).upper() in _BOOKED_TYPES and live and live != "REJECTED":
            key = object_key(_cell(target))
            if key:
                out.add(key)
    return frozenset(out)


def book_estimated_sql(row: Mapping[str, object], *, proof_sql: str) -> str:
    """ONE idempotent INSERT of an ESTIMATED SAVINGS_LEDGER row for a confirmed-unread action row.

    ``proof_sql`` is built by the data layer (cost_sql.unread_maintenance_proof) and stored as PROOF_SQL,
    which is what the manual Verify flow needs. Keyed on the OBJECT (WHERE NOT EXISTS: the same
    TARGET_OBJECT under ANY unread-maintenance finding type, in any state but REJECTED), because the estimate
    is the object's credits across every arm while FINDING_TYPE follows the dominant arm, which can flip as
    the pre-ALTER days roll out of the window (PR C review C6): a second click, a flipped arm or an already
    VERIFIED booking books nothing; a REJECTED one can be booked again. Every value is a sql_literal /
    sql_number at the
    V005 / V053 column widths, and the statement holds no ';' outside literals (it passes the executor
    allow-list as one INSERT into OVERWATCH's own table). ValueError unless the row is an action verdict
    with review SQL, a positive estimate, a finding type, an object and a proof query."""
    verdict = _text(row.get("VERDICT"))
    if verdict not in ACTION_VERDICTS:
        raise ValueError(f"only an action verdict is bookable, not {verdict!r}")
    review = _text(row.get("REVIEW_SQL"))
    if not review:
        raise ValueError("no review SQL for this object (its name is not a plain three-part name)")
    est = safe_float(row.get("EST_MONTHLY_USD"), default=float("nan"))
    if not (est > 0):
        raise ValueError("the estimate must be positive")
    ftype = _text(row.get("FINDING_TYPE"))
    fqn = _text(row.get("OBJECT_FQN"))
    if not ftype or not fqn:
        raise ValueError("a finding type and an object are required")
    if not _text(proof_sql):
        raise ValueError("a proof query is required")
    ledger = core_object("SAVINGS_LEDGER")
    desc = f"{verdict} on {fqn} (no reads in {UNREAD_WINDOW_DAYS}d)"
    notes = f"Review SQL: {' '.join(review.split())} | {BOOK_NOTE}"
    return (
        f"INSERT INTO {ledger} (DESCRIPTION, STATE, ESTIMATED_USD, PROOF_SQL, NOTES, FINDING_TYPE, "
        "TARGET_OBJECT)\n"
        f"SELECT {sql_literal(desc, 500)}, 'ESTIMATED', {sql_number(round(est, 2))}, "
        f"{sql_literal(proof_sql, 4000)}, {sql_literal(notes, 2000)}, {sql_literal(ftype, 40)}, "
        f"{sql_literal(fqn, 300)}\n"
        "WHERE NOT EXISTS (\n"
        f"    SELECT 1 FROM {ledger}\n"
        f"    WHERE TARGET_OBJECT = {sql_literal(fqn, 300)}\n"
        f"      AND FINDING_TYPE IN ({_BOOKED_TYPES_SQL})\n"
        "      AND STATE <> 'REJECTED'\n"
        ")"
    )
