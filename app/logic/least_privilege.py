"""rec#24: least-privilege analysis — held grants vs. what was actually used.

Pure module (no Streamlit, no clock, no network). Consumes the grant-scope
rollup (data/security_sql.grant_scope_usage) and labels each role's per-schema
grant footprint by how much of it was exercised over the lookback window.

Two correctness traps this feature is designed AROUND (handled in the SQL, noted
here because they shape what the labels can honestly claim):

* **Role inheritance.** Access is attributed at the OBJECT level ("was this table
  touched by *any* query?"), never per-role, so a grant exercised only through a
  child role is still counted as used. This can under-report a specific unused
  grant, but it never *falsely* flags a used one — the safe direction for a
  revoke recommendation.
* **Identifier matching.** The SQL bridges grants to ACCESS_HISTORY through the
  numeric object id (TABLE_STORAGE_METRICS.ID), not the DB.SCHEMA.TABLE string,
  so mixed-case/quoted identifiers can't masquerade as "never accessed".
"""

from __future__ import annotations

import math

import pandas as pd

SCOPE_COLUMNS = (
    "ROLE_NAME",
    "DATABASE_NAME",
    "SCHEMA_NAME",
    "GRANTED_TABLES",
    "TOUCHED_TABLES",
    "UNUSED_TABLES",
    "USED_PCT",
    "VERDICT",
)

# Verdicts, ordered worst (most revocable) to best for stable sorting.
_VERDICT_RANK = {"UNUSED": 0, "OVER-BROAD": 1, "FOCUSED": 2}

_EMPTY = pd.DataFrame({name: pd.Series(dtype="object") for name in SCOPE_COLUMNS})


def classify_grant_scopes(
    frame: pd.DataFrame | None,
    min_granted: int = 4,
    narrow_ratio: float = 1.0 / 3.0,
) -> pd.DataFrame:
    """Label each (role, database, schema) grant footprint by exercised share.

    Input columns (from grant_scope_usage): ROLE_NAME, DATABASE_NAME,
    SCHEMA_NAME, GRANTED_TABLES, TOUCHED_TABLES.

    * **UNUSED** — the role touched none of the tables it is granted in this
      schema (a whole-scope revoke candidate).
    * **OVER-BROAD** — granted at least ``min_granted`` tables but exercised at
      most ``narrow_ratio`` of them (narrow the grant to the tables in use).
    * **FOCUSED** — the grant footprint is mostly used.

    Empty/None input returns the empty schema so callers render "nothing to
    review" honestly.
    """
    if frame is None or getattr(frame, "empty", True):
        return _EMPTY.copy()

    out = frame.copy()
    for col in ("GRANTED_TABLES", "TOUCHED_TABLES"):
        out[col] = pd.to_numeric(out.get(col), errors="coerce").fillna(0).astype(int)
    # Touched can never exceed granted; clamp defensively so USED_PCT stays sane
    # even if the two legs are read at slightly different instants.
    out["TOUCHED_TABLES"] = out[["TOUCHED_TABLES", "GRANTED_TABLES"]].min(axis=1)
    out["UNUSED_TABLES"] = (out["GRANTED_TABLES"] - out["TOUCHED_TABLES"]).clip(lower=0)
    out["USED_PCT"] = (
        out["TOUCHED_TABLES"] / out["GRANTED_TABLES"].replace(0, pd.NA) * 100
    ).fillna(0.0).round(1)

    def _verdict(row: pd.Series) -> str:
        granted = int(row["GRANTED_TABLES"])
        touched = int(row["TOUCHED_TABLES"])
        if granted < 1:
            return "FOCUSED"
        if touched == 0:
            return "UNUSED"
        if granted >= min_granted and touched <= math.floor(granted * narrow_ratio):
            return "OVER-BROAD"
        return "FOCUSED"

    out["VERDICT"] = out.apply(_verdict, axis=1)
    out["_RANK"] = out["VERDICT"].map(_VERDICT_RANK).fillna(len(_VERDICT_RANK))
    out = out.sort_values(
        ["_RANK", "UNUSED_TABLES", "GRANTED_TABLES"],
        ascending=[True, False, False],
    ).drop(columns="_RANK")
    return out[list(SCOPE_COLUMNS)].reset_index(drop=True)


# The data privileges the unused-grants shortlist can observe (ACCESS_HISTORY
# traces reads/writes only); mirrors security_sql._DATA_PRIVS. Any other value in
# a row is skipped rather than emitted as a REVOKE we can't stand behind.
_REVOKABLE_PRIVS = ("SELECT", "INSERT", "UPDATE", "DELETE", "REFERENCES", "TRUNCATE")


def revoke_statements(frame: pd.DataFrame | None) -> list[str]:
    """Format copy-paste REVOKE statements from the unused-table-grants shortlist.

    Input columns (from security_sql.unused_table_grants): ROLE_NAME, PRIVILEGE,
    OBJECT_NAME (a fully-qualified DB.SCHEMA.TABLE from the catalog). One statement
    per row, in the frame's own order (role then object). Rows missing a field or
    carrying an unrecognized privilege are skipped so a malformed REVOKE is never
    emitted. Identifiers pass through as the catalog reports them — this is a
    review-then-run script (the app never executes it), so the caller wraps it with
    a 'verify before revoking' header. Empty/None -> []."""
    if frame is None or getattr(frame, "empty", True):
        return []
    if not {"ROLE_NAME", "PRIVILEGE", "OBJECT_NAME"}.issubset(frame.columns):
        return []
    statements: list[str] = []
    for _, row in frame.iterrows():
        role = str(row.get("ROLE_NAME") or "").strip()
        priv = str(row.get("PRIVILEGE") or "").strip().upper()
        obj = str(row.get("OBJECT_NAME") or "").strip()
        if not role or not obj or priv not in _REVOKABLE_PRIVS:
            continue
        statements.append(f"REVOKE {priv} ON TABLE {obj} FROM ROLE {role};")
    return statements


# Per-sheet auditor recommendation for the access-review export (Sec #17). Each
# actionable sheet gets one leading RECOMMEND column; evidence-only sheets (role
# matrix, grant diff, failed logins, ...) stay raw evidence.
_ACCESS_REVIEW_RECOMMEND = {
    "unused_roles_90d": "REVOKE — role unused by any query in 90d",
    "dormant_users": "REVIEW / disable — no activity in 90d",
    "mfa_gaps_password_login": "ENABLE MFA — password login without MFA",
    "expiring_credentials_10d": "ROTATE — credential expires within 10 days",
    "break_glass_holders": "REVIEW — standing privileged access",
}


def recommend_for_sheet(sheet: str, frame: pd.DataFrame | None) -> pd.DataFrame | None:
    """Add a leading RECOMMEND column to an actionable access-review sheet so the
    auditor pack says what to DO, not just what exists (Sec #17). Evidence-only
    sheets, empty frames, and error frames pass through unchanged. Pure."""
    rec = _ACCESS_REVIEW_RECOMMEND.get(sheet)
    if (rec is None or frame is None or getattr(frame, "empty", True)
            or "ERROR" in getattr(frame, "columns", [])):
        return frame
    out = frame.copy()
    out.insert(0, "RECOMMEND", rec)
    return out


def summarize_scopes(frame: pd.DataFrame | None) -> dict[str, int]:
    """KPI counts over a classified scope frame (from classify_grant_scopes)."""
    empty = {"roles": 0, "scopes": 0, "unused": 0, "over_broad": 0, "unused_tables": 0}
    if frame is None or getattr(frame, "empty", True):
        return empty
    return {
        "roles": int(frame["ROLE_NAME"].nunique()),
        "scopes": len(frame),
        "unused": int((frame["VERDICT"] == "UNUSED").sum()),
        "over_broad": int((frame["VERDICT"] == "OVER-BROAD").sum()),
        "unused_tables": int(frame["UNUSED_TABLES"].sum()),
    }
