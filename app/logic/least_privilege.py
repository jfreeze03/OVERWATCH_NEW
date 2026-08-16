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
