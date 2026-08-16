"""rec#27: incident routing — turn a classified task failure into a routed incident.

Pure module (no Streamlit, no clock, no network). The plumbing already exists but
sits disconnected: insights.classify_task_error names the error family and
build_failure_timeline marks root-cause vs cascade, yet nothing attaches a first
response or an owner. This stitches them: each root-cause failure gets a
first-response remediation keyed to its error family and an owner/on-call resolved
from ENTITY_CATALOG (the task's own TASK entry, else the database it lives in), so
an incident arrives with a name and a next move attached.

The write halves — opening a routed ACTION_QUEUE item / Teams mention and escalating
to a secondary when an ack times out against an on-call rotation — are the deferred
owner-migration part (they need an ON_CALL_SCHEDULE table this app can't create).
"""

from __future__ import annotations

import pandas as pd

INCIDENT_COLUMNS = (
    "ENTITY",
    "ERROR_FAMILY",
    "FAILURES",
    "LAST_FAILED",
    "SEVERITY",
    "OWNER",
    "ON_CALL",
    "ROUTED_TO",
    "REMEDIATION",
)

# First-response remediation per error family (families from insights._ERROR_FAMILIES).
REMEDIATION_BY_FAMILY: dict[str, str] = {
    "Permission / auth": "Grant the missing privilege to the task's role (SHOW GRANTS), or restore the object's grant; then resume.",
    "Concurrency / live version": "A prior version is still live — commit/complete it or serialize the task, then resume.",
    "Session not set up": "The task session has no database/schema/warehouse/role — set them in the task definition (or role defaults); resume.",
    "Metadata not ready": "An upstream object isn't ready — add a stream/task dependency or a wait, then resume.",
    "Missing object": "A referenced object was dropped/renamed — repoint or recreate it (check Security > Changes for recent DDL); resume.",
    "Timeout / cancelled": "Hit the statement/warehouse timeout — check the query profile for a runaway/looping step first; if it's genuinely heavy, raise STATEMENT_TIMEOUT_IN_SECONDS or size the warehouse up, then retry.",
    "Resource / memory": "Out of memory/quota — size up or split the workload; check the query profile for the spilling step.",
    "Data quality": "A value/constraint/conversion failed — inspect the offending rows upstream (the row-volume DQ panel flags load anomalies).",
    "Syntax / SQL": "Compilation/syntax error — the task body is broken (likely a recent change); diff vs the prior version (Change impact) and fix forward.",
    "No error text": "No error text captured — open the task's run history for the full message (Operations > Tasks).",
    "Other": "No recognized error family — open the task's run history for the full message and triage manually.",
}
_DEFAULT_REMEDIATION = REMEDIATION_BY_FAMILY["Other"]

# Matches the app's own CRITICALITIES vocabulary (workbench.py): CRITICAL / HIGH /
# STANDARD / LOW, with STANDARD the catalog default. Unknown/unregistered -> STANDARD.
_CRIT_RANK = {"CRITICAL": 3, "HIGH": 2, "STANDARD": 1, "LOW": 0}
_RANK_LABEL = {3: "CRITICAL", 2: "HIGH", 1: "STANDARD", 0: "LOW"}
_SEV_ORDER = {"CRITICAL": 0, "HIGH": 1, "STANDARD": 2, "LOW": 3}

_EMPTY = pd.DataFrame({name: pd.Series(dtype="object") for name in INCIDENT_COLUMNS})


def _catalog_lookup(catalog: pd.DataFrame | None) -> dict[tuple[str, str], tuple[str, str, str]]:
    """(ENTITY_TYPE, UPPER ENTITY_KEY) -> (owner, on_call, criticality)."""
    out: dict[tuple[str, str], tuple[str, str, str]] = {}
    if catalog is None or getattr(catalog, "empty", True):
        return out
    for _, row in catalog.iterrows():
        key = str(row.get("ENTITY_KEY") or "").strip().upper()
        if not key:
            continue
        etype = str(row.get("ENTITY_TYPE") or "").strip().upper()
        out[(etype, key)] = (
            str(row.get("OWNER_NAME") or "").strip(),
            str(row.get("ON_CALL") or "").strip(),
            str(row.get("CRITICALITY") or "").strip(),
        )
    return out


def _resolve_owner(lookup: dict, database: str, schema: str, task: str) -> tuple[str, str, str]:
    """Most-specific catalog match for a failing TASK: its own TASK entry
    (DB.SCH.TASK) wins, then an OBJECT entry of the same name (tolerating a
    mis-registration), then the coarse DATABASE owner. There is no SCHEMA entity
    type in this catalog, so it isn't probed."""
    fqn = f"{database}.{schema}.{task}".upper()
    candidates = (
        ("TASK", fqn),
        ("OBJECT", fqn),
        ("DATABASE", database.upper()),
    )
    for cand in candidates:
        if cand in lookup:
            return lookup[cand]
    return ("", "", "")


def _severity(criticality: str, is_root: bool) -> str:
    rank = _CRIT_RANK.get(str(criticality or "").strip().upper(), 1)  # unknown -> MEDIUM
    if not is_root:
        rank = max(0, rank - 1)   # a cascade is one notch below its owner's criticality
    return _RANK_LABEL[rank]


def route_incidents(
    failures: pd.DataFrame | None,
    catalog: pd.DataFrame | None,
    root_only: bool = True,
) -> pd.DataFrame:
    """Route classified task failures to owners with a first-response remediation.

    ``failures`` is the build_failure_timeline output (DATABASE_NAME, SCHEMA_NAME,
    TASK_NAME, ERROR_FAMILY, ROLE_IN_GRAPH, QUERY_START_TIME). One incident per
    failing task (collapsed to its most recent failure, with a FAILURES count).
    ``root_only`` keeps root-cause failures when that distinction is present, so
    cascades don't each page an owner. Returns most-severe first.
    """
    if failures is None or getattr(failures, "empty", True):
        return _EMPTY.copy()

    fdf = failures.copy()
    for col in ("DATABASE_NAME", "SCHEMA_NAME", "TASK_NAME", "ERROR_FAMILY", "ROLE_IN_GRAPH"):
        if col not in fdf.columns:
            fdf[col] = ""
        fdf[col] = fdf[col].fillna("").astype(str)
    fdf["QUERY_START_TIME"] = pd.to_datetime(fdf.get("QUERY_START_TIME"), errors="coerce")

    if root_only and (fdf["ROLE_IN_GRAPH"] == "Root cause").any():
        fdf = fdf[fdf["ROLE_IN_GRAPH"] == "Root cause"]
    if fdf.empty:
        return _EMPTY.copy()

    fdf["ENTITY"] = (fdf["DATABASE_NAME"] + "." + fdf["SCHEMA_NAME"] + "." + fdf["TASK_NAME"])
    # stable sort so the representative (most recent) row is deterministic on ties
    fdf = fdf.sort_values("QUERY_START_TIME", ascending=False, kind="stable")
    counts = fdf.groupby("ENTITY").size().to_dict()
    representative = fdf.drop_duplicates("ENTITY", keep="first")  # most recent per task

    lookup = _catalog_lookup(catalog)
    rows: list[dict] = []
    for _, row in representative.iterrows():
        owner, on_call, criticality = _resolve_owner(
            lookup, row["DATABASE_NAME"], row["SCHEMA_NAME"], row["TASK_NAME"])
        family = row["ERROR_FAMILY"] or "Other"
        is_root = row["ROLE_IN_GRAPH"] != "Cascade"
        last_failed = row["QUERY_START_TIME"]
        rows.append({
            "ENTITY": row["ENTITY"],
            "ERROR_FAMILY": family,
            "FAILURES": int(counts.get(row["ENTITY"], 1)),
            "LAST_FAILED": "" if pd.isna(last_failed) else last_failed.isoformat(sep=" ", timespec="minutes"),
            "SEVERITY": _severity(criticality, is_root),
            "OWNER": owner,
            "ON_CALL": on_call,
            "ROUTED_TO": on_call or owner or "unassigned",
            "REMEDIATION": REMEDIATION_BY_FAMILY.get(family, _DEFAULT_REMEDIATION),
        })

    out = pd.DataFrame(rows, columns=list(INCIDENT_COLUMNS))
    out["_SEV"] = out["SEVERITY"].map(_SEV_ORDER).fillna(len(_SEV_ORDER))
    out = out.sort_values(["_SEV", "FAILURES", "ENTITY"], ascending=[True, False, True],
                          kind="stable").drop(columns="_SEV")
    return out.reset_index(drop=True)


def summarize_incidents(frame: pd.DataFrame | None) -> dict[str, int]:
    """KPI counts over a routed incident frame (from route_incidents)."""
    empty = {"incidents": 0, "critical": 0, "routed": 0, "unrouted": 0}
    if frame is None or getattr(frame, "empty", True):
        return empty
    return {
        "incidents": len(frame),
        "critical": int(frame["SEVERITY"].isin(["CRITICAL", "HIGH"]).sum()),
        "routed": int((frame["ROUTED_TO"] != "unassigned").sum()),
        "unrouted": int((frame["ROUTED_TO"] == "unassigned").sum()),
    }
