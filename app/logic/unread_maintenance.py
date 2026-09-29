"""Next-Fifty #30: maintenance spend on objects nobody reads. Pure module (no Streamlit, no Snowflake).

Cost ▸ Optimization & Savings ▸ Storage & waste shortlists objects paying clustering, search-optimization or
MV-refresh credits with no read credits in the object ledger (cost_sql.maintenance_on_unread), confirms each
against ACCESS_HISTORY (insights_sql.object_reads_confirm), and this module turns the two frames into one
verdict per object, review-only SQL, and an ESTIMATED savings-ledger booking.

Verdict precedence: Unconfirmed (no measured read evidence: the confirm read failed or missed the object)
> Keep (any read) > Check share consumers (its database is shared out; a consumer account's reads never
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
VERDICT_KEEP = "Keep"
VERDICT_SHARED = "Check share consumers"
VERDICT_NO_RECENT = "No recent spend"
VERDICT_UNCONFIRMED = "Unconfirmed"
ACTION_VERDICTS = frozenset(ARM_VERDICT.values())
UNREAD_WINDOW_DAYS = 90
# display order: the actions first, then what needs a person, then what is settled
_VERDICT_RANK = {**dict.fromkeys(ARM_VERDICT.values(), 0), VERDICT_SHARED: 1, VERDICT_NO_RECENT: 2,
                 VERDICT_UNCONFIRMED: 3, VERDICT_KEEP: 4}
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


def book_estimated_sql(row: Mapping[str, object], *, proof_sql: str) -> str:
    """ONE idempotent INSERT of an ESTIMATED SAVINGS_LEDGER row for a confirmed-unread action row.

    ``proof_sql`` is built by the data layer (cost_sql.unread_maintenance_proof) and stored as PROOF_SQL,
    which is what the manual Verify flow needs. Keyed on FINDING_TYPE + TARGET_OBJECT + STATE='ESTIMATED'
    (WHERE NOT EXISTS), so a second click books nothing. Every value is a sql_literal / sql_number at the
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
        f"    WHERE FINDING_TYPE = {sql_literal(ftype, 40)} AND TARGET_OBJECT = {sql_literal(fqn, 300)}\n"
        "      AND STATE = 'ESTIMATED'\n"
        ")"
    )
