"""Pure operating-workbench rules and write-statement builders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import uuid4

import pandas as pd

from app.config import core_object
from app.core.sqlsafe import sql_literal, sql_number
from app.logic.formulas import account_today, safe_float

ENTITY_TYPES = (
    "WAREHOUSE",
    "DATABASE",
    "OBJECT",
    "TASK",
    "QUERY_FINGERPRINT",
    "USER",
    "ROLE",
    "DATA_PRODUCT",
    "ALERT",
    "INCIDENT",
    "ACTION",
)
ACTION_STATUSES = ("OPEN", "IN_PROGRESS", "DONE", "DROPPED")
EXPERIMENT_STATUSES = ("PLANNED", "RUNNING", "OBSERVING", "VERIFIED", "REJECTED", "ROLLED_BACK")
CRITICALITIES = ("CRITICAL", "HIGH", "STANDARD", "LOW")
SLO_METRIC_KEYS = (
    "WAREHOUSE_SUCCESS_PCT",
    "WAREHOUSE_P95_SEC",
    "TASK_SUCCESS_PCT",
    "TASK_P95_EXEC_SEC",
    "QUERY_SUCCESS_PCT",
    "QUERY_P95_SEC",
)


def normalize_entity_type(value: object) -> str:
    kind = str(value or "").strip().upper().replace(" ", "_")
    if kind not in ENTITY_TYPES:
        raise ValueError(f"Unsupported entity type: {kind or 'blank'}")
    return kind


def _date_sql(value: date | str | None) -> str:
    if value is None or not str(value).strip():
        return "NULL"
    if isinstance(value, date):
        text = value.isoformat()
    else:
        text = str(value).strip()[:10]
        date.fromisoformat(text)
    return f"TO_DATE({sql_literal(text, 10)})"


def action_transition_sql(
    action_id: str,
    *,
    status: str = "",
    owner: str = "",
    due_date: date | str | None = None,
    defer_until: date | str | None = None,
    note: str = "",
    actor: str = "",
    request_key: str = "",
) -> str:
    aid = str(action_id or "").strip()
    if not aid:
        raise ValueError("Action ID is required")
    state = str(status or "").strip().upper()
    if state and state not in ACTION_STATUSES:
        raise ValueError(f"Unsupported action status: {state}")
    key = str(request_key or f"action:{aid}:{uuid4()}")[:160]
    return (
        f"CALL {core_object('SP_ACTION_LIFECYCLE')}("
        f"{sql_literal(aid, 80)}, {sql_literal(state, 30)}, "
        f"{sql_literal(str(owner or '').strip(), 200)}, {_date_sql(due_date)}, "
        f"{_date_sql(defer_until)}, {sql_literal(str(note or ''), 4000)}, "
        f"{sql_literal(str(actor or '').strip(), 200)}, {sql_literal(key, 160)})"
    )


def create_action_sql(
    *,
    title: str,
    detail: str,
    company: str,
    severity: str,
    owner: str,
    due_date: date | str | None,
    source: str,
    entity_type: str,
    entity_key: str,
    confidence: float | None = None,
    estimated_usd: float | None = None,
) -> str:
    clean_title = str(title or "").strip()
    if not clean_title:
        raise ValueError("Action title is required")
    sev = str(severity or "MEDIUM").strip().upper()
    if sev not in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"):
        raise ValueError(f"Unsupported severity: {sev}")
    kind = normalize_entity_type(entity_type)
    conf_sql = "NULL" if confidence is None else sql_number(max(0.0, min(float(confidence), 1.0)))
    usd_sql = "NULL" if estimated_usd is None else sql_number(max(0.0, float(estimated_usd)))
    return f"""
INSERT INTO {core_object('ACTION_QUEUE')}
    (COMPANY, SEVERITY, TITLE, DETAIL, OWNER, STATUS, DUE_DATE, SOURCE,
     SOURCE_ENTITY_TYPE, SOURCE_ENTITY_KEY, CONFIDENCE, ESTIMATED_USD)
SELECT {sql_literal(str(company or 'ALL'), 40)}, {sql_literal(sev, 20)},
       {sql_literal(clean_title, 300)}, {sql_literal(str(detail or ''), 2000)},
       {sql_literal(str(owner or 'DBA'), 200)}, 'OPEN', {_date_sql(due_date)},
       {sql_literal(str(source or 'Action Center'), 120)}, {sql_literal(kind, 40)},
       {sql_literal(str(entity_key or '').strip(), 500)}, {conf_sql}, {usd_sql}
""".strip()


def evidence_link_sql(
    *,
    from_type: str,
    from_id: str,
    to_type: str,
    to_id: str,
    relationship: str,
    label: str = "",
    query_id: str = "",
    note: str = "",
    actor: str = "",
) -> str:
    source_kind = normalize_entity_type(from_type)
    target_kind = normalize_entity_type(to_type)
    rel = str(relationship or "").strip().upper().replace(" ", "_")
    if not rel:
        raise ValueError("Evidence relationship is required")
    return f"""
INSERT INTO {core_object('EVIDENCE_LINKS')}
    (FROM_TYPE, FROM_ID, TO_TYPE, TO_ID, RELATIONSHIP, LABEL, QUERY_ID,
     EVIDENCE_NOTE, CREATED_BY)
SELECT {sql_literal(source_kind, 40)}, {sql_literal(str(from_id or '').strip(), 500)},
       {sql_literal(target_kind, 40)}, {sql_literal(str(to_id or '').strip(), 500)},
       {sql_literal(rel, 80)}, {sql_literal(str(label or ''), 500)},
       {sql_literal(str(query_id or '').strip(), 80)}, {sql_literal(str(note or ''), 4000)},
       {sql_literal(str(actor or '').strip(), 200)}
""".strip()


def entity_catalog_merge_sql(
    *,
    entity_type: str,
    entity_key: str,
    label: str,
    company: str,
    team: str,
    owner: str,
    steward: str,
    on_call: str,
    criticality: str,
    data_product: str,
    slo_name: str,
    notes: str,
    actor: str,
) -> str:
    kind = normalize_entity_type(entity_type)
    key = str(entity_key or "").strip()
    if not key:
        raise ValueError("Entity key is required")
    crit = str(criticality or "STANDARD").strip().upper()
    if crit not in CRITICALITIES:
        raise ValueError(f"Unsupported criticality: {crit}")
    values = (
        f"SELECT {sql_literal(kind, 40)} ENTITY_TYPE, {sql_literal(key, 500)} ENTITY_KEY, "
        f"{sql_literal(str(label or ''), 500)} LABEL, {sql_literal(str(company or 'ALL'), 40)} COMPANY, "
        f"{sql_literal(str(team or ''), 200)} TEAM, {sql_literal(str(owner or ''), 200)} OWNER_NAME, "
        f"{sql_literal(str(steward or ''), 200)} STEWARD, {sql_literal(str(on_call or ''), 500)} ON_CALL, "
        f"{sql_literal(crit, 20)} CRITICALITY, {sql_literal(str(data_product or ''), 300)} DATA_PRODUCT, "
        f"{sql_literal(str(slo_name or ''), 300)} SLO_NAME, {sql_literal(str(notes or ''), 4000)} NOTES, "
        f"{sql_literal(str(actor or ''), 200)} UPDATED_BY"
    )
    return f"""
MERGE INTO {core_object('ENTITY_CATALOG')} t
USING ({values}) s
   ON UPPER(t.ENTITY_TYPE) = UPPER(s.ENTITY_TYPE)
  AND UPPER(t.ENTITY_KEY) = UPPER(s.ENTITY_KEY)
WHEN MATCHED THEN UPDATE SET
    LABEL=s.LABEL, COMPANY=s.COMPANY, TEAM=s.TEAM, OWNER_NAME=s.OWNER_NAME,
    STEWARD=s.STEWARD, ON_CALL=s.ON_CALL, CRITICALITY=s.CRITICALITY,
    DATA_PRODUCT=s.DATA_PRODUCT, SLO_NAME=s.SLO_NAME, NOTES=s.NOTES,
    UPDATED_AT=CURRENT_TIMESTAMP(), UPDATED_BY=s.UPDATED_BY
WHEN NOT MATCHED THEN INSERT
    (ENTITY_TYPE, ENTITY_KEY, LABEL, COMPANY, TEAM, OWNER_NAME, STEWARD,
     ON_CALL, CRITICALITY, DATA_PRODUCT, SLO_NAME, NOTES, UPDATED_BY)
VALUES
    (s.ENTITY_TYPE, s.ENTITY_KEY, s.LABEL, s.COMPANY, s.TEAM, s.OWNER_NAME,
     s.STEWARD, s.ON_CALL, s.CRITICALITY, s.DATA_PRODUCT, s.SLO_NAME,
     s.NOTES, s.UPDATED_BY)
""".strip()


def mark_watched(frame: pd.DataFrame | None, watchlist: pd.DataFrame | None,
                 entity_type: str, key_col: str) -> pd.Series:
    """Boolean Series: is each row's ``key_col`` on the viewer's watchlist (DS #1)?

    Case-insensitive ENTITY_KEY match within ``entity_type`` (the same upper-case match
    the Entity-360 Watch button uses), so a watched query family / warehouse / product
    surfaces on the surface that lists it. Empty or absent inputs, a missing key column,
    or a watchlist without the expected columns all yield all-False — never a raise.
    """
    if frame is None:
        return pd.Series(dtype=bool)
    if frame.empty or key_col not in frame.columns:
        return pd.Series(False, index=frame.index)
    if (watchlist is None or watchlist.empty
            or not {"ENTITY_TYPE", "ENTITY_KEY"}.issubset(watchlist.columns)):
        return pd.Series(False, index=frame.index)
    kind = str(entity_type or "").strip().upper()
    scoped = watchlist[watchlist["ENTITY_TYPE"].astype(str).str.upper() == kind]
    keys = set(scoped["ENTITY_KEY"].astype(str).str.strip().str.upper())
    return frame[key_col].astype(str).str.strip().str.upper().isin(keys)


def mark_watched_pairs(frame: pd.DataFrame | None, watchlist: pd.DataFrame | None,
                       type_col: str, key_col: str) -> pd.Series:
    """Boolean Series: is each row's (``type_col``, ``key_col``) pair on the watchlist?

    Unlike ``mark_watched`` (one entity type), this matches each row's OWN entity type +
    key against the (ENTITY_TYPE, ENTITY_KEY) pairs on the watchlist — for surfaces like
    the Action Center where each row carries its source entity (DS #1). Same graceful,
    case-insensitive behavior; empty/absent inputs yield all-false.
    """
    if frame is None:
        return pd.Series(dtype=bool)
    if frame.empty or type_col not in frame.columns or key_col not in frame.columns:
        return pd.Series(False, index=frame.index)
    if (watchlist is None or watchlist.empty
            or not {"ENTITY_TYPE", "ENTITY_KEY"}.issubset(watchlist.columns)):
        return pd.Series(False, index=frame.index)
    watched = set(zip(
        watchlist["ENTITY_TYPE"].astype(str).str.strip().str.upper(),
        watchlist["ENTITY_KEY"].astype(str).str.strip().str.upper(),
        strict=False,
    ))
    pairs = zip(
        frame[type_col].astype(str).str.strip().str.upper(),
        frame[key_col].astype(str).str.strip().str.upper(),
        strict=False,
    )
    return pd.Series([pair in watched for pair in pairs], index=frame.index)


def watchlist_sql(viewer: str, entity_type: str, entity_key: str,
                  label: str = "", *, remove: bool = False) -> str:
    name = str(viewer or "").strip()
    if not name:
        raise ValueError("Viewer identity is required for a watchlist")
    kind = normalize_entity_type(entity_type)
    key = str(entity_key or "").strip()
    if not key:
        raise ValueError("Entity key is required")
    if remove:
        return (
            f"DELETE FROM {core_object('USER_WATCHLIST')} WHERE "
            f"UPPER(VIEWER_NAME)={sql_literal(name.upper(), 200)} AND "
            f"UPPER(ENTITY_TYPE)={sql_literal(kind, 40)} AND "
            f"UPPER(ENTITY_KEY)={sql_literal(key.upper(), 500)}"
        )
    return f"""
MERGE INTO {core_object('USER_WATCHLIST')} t
USING (SELECT {sql_literal(name, 200)} VIEWER_NAME, {sql_literal(kind, 40)} ENTITY_TYPE,
              {sql_literal(key, 500)} ENTITY_KEY, {sql_literal(str(label or ''), 500)} LABEL) s
ON UPPER(t.VIEWER_NAME)=UPPER(s.VIEWER_NAME)
AND UPPER(t.ENTITY_TYPE)=UPPER(s.ENTITY_TYPE)
AND UPPER(t.ENTITY_KEY)=UPPER(s.ENTITY_KEY)
WHEN MATCHED THEN UPDATE SET LABEL=s.LABEL
WHEN NOT MATCHED THEN INSERT (VIEWER_NAME, ENTITY_TYPE, ENTITY_KEY, LABEL)
VALUES (s.VIEWER_NAME, s.ENTITY_TYPE, s.ENTITY_KEY, s.LABEL)
""".strip()


def create_experiment_sql(
    *,
    action_id: str = "",
    entity_type: str,
    entity_key: str,
    title: str,
    hypothesis: str,
    baseline: str,
    target: str,
    rollback: str,
    observation_end: date | str | None,
    actor: str,
) -> str:
    kind = normalize_entity_type(entity_type)
    if not str(title or "").strip() or not str(hypothesis or "").strip():
        raise ValueError("Experiment title and hypothesis are required")
    return f"""
INSERT INTO {core_object('OPTIMIZATION_EXPERIMENTS')}
    (ACTION_ID, ENTITY_TYPE, ENTITY_KEY, TITLE, HYPOTHESIS, BASELINE_NOTE,
     TARGET_NOTE, ROLLBACK_CONDITION, OBSERVATION_END, STATUS, CREATED_BY, UPDATED_BY)
SELECT NULLIF({sql_literal(str(action_id or '').strip(), 80)}, ''), {sql_literal(kind, 40)},
       {sql_literal(str(entity_key or '').strip(), 500)}, {sql_literal(str(title), 500)},
       {sql_literal(str(hypothesis), 4000)}, {sql_literal(str(baseline or ''), 4000)},
       {sql_literal(str(target or ''), 4000)}, {sql_literal(str(rollback or ''), 4000)},
       {_date_sql(observation_end)}, 'PLANNED', {sql_literal(str(actor or ''), 200)},
       {sql_literal(str(actor or ''), 200)}
""".strip()


def update_experiment_sql(experiment_id: str, *, status: str, result: str,
                          verified_usd: float | None, actor: str) -> str:
    state = str(status or "").strip().upper()
    if state not in EXPERIMENT_STATUSES:
        raise ValueError(f"Unsupported experiment status: {state}")
    usd = "NULL" if verified_usd is None else sql_number(max(0.0, float(verified_usd)))
    return f"""
UPDATE {core_object('OPTIMIZATION_EXPERIMENTS')}
SET STATUS={sql_literal(state, 30)}, RESULT_NOTE={sql_literal(str(result or ''), 4000)},
    VERIFIED_USD={usd}, UPDATED_AT=CURRENT_TIMESTAMP(),
    UPDATED_BY={sql_literal(str(actor or ''), 200)}
WHERE EXPERIMENT_ID={sql_literal(str(experiment_id or '').strip(), 80)}
""".strip()


def create_slo_objective_sql(
    *,
    name: str,
    entity_type: str,
    entity_key: str,
    metric_key: str,
    comparator: str,
    target_value: float,
    error_budget_pct: float,
    window_days: int,
    owner: str,
    notes: str,
    actor: str,
) -> str:
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("SLO name is required")
    kind = normalize_entity_type(entity_type)
    metric = str(metric_key or "").strip().upper()
    if metric not in SLO_METRIC_KEYS:
        raise ValueError(f"Unsupported SLO metric: {metric}")
    compare = str(comparator or "").strip()
    if compare not in (">=", "<="):
        raise ValueError("SLO comparator must be >= or <=")
    horizon = max(1, min(int(window_days or 30), 400))
    return f"""
INSERT INTO {core_object('SLO_OBJECTIVES')}
    (NAME, ENTITY_TYPE, ENTITY_KEY, METRIC_KEY, COMPARATOR, TARGET_VALUE,
     ERROR_BUDGET_PCT, WINDOW_DAYS, OWNER_NAME, NOTES, UPDATED_BY)
SELECT {sql_literal(clean_name, 300)}, {sql_literal(kind, 40)},
       {sql_literal(str(entity_key or '').strip(), 500)}, {sql_literal(metric, 160)},
       {sql_literal(compare, 4)}, {sql_number(float(target_value))},
       {sql_number(max(0.01, min(float(error_budget_pct), 100.0)))}, {horizon},
       {sql_literal(str(owner or ''), 200)}, {sql_literal(str(notes or ''), 4000)},
       {sql_literal(str(actor or ''), 200)}
""".strip()


def action_summary(frame: pd.DataFrame | None) -> dict[str, float]:
    if frame is None or frame.empty:
        return {"open": 0.0, "critical_high": 0.0, "overdue": 0.0,
                "unassigned": 0.0, "estimated_usd": 0.0}
    view = frame.copy()
    status = view.get("STATUS", pd.Series("", index=view.index)).astype(str).str.upper()
    open_mask = status.isin(("OPEN", "IN_PROGRESS"))
    severity = view.get("SEVERITY", pd.Series("", index=view.index)).astype(str).str.upper()
    due = pd.to_datetime(view.get("DUE_DATE", pd.Series(pd.NaT, index=view.index)), errors="coerce")
    owner = view.get("OWNER", pd.Series("", index=view.index)).fillna("").astype(str).str.strip()
    dollars = pd.to_numeric(
        view.get("ESTIMATED_USD", pd.Series(0.0, index=view.index)), errors="coerce"
    ).fillna(0.0)
    today = pd.Timestamp(account_today())
    return {
        "open": float(open_mask.sum()),
        "critical_high": float((open_mask & severity.isin(("CRITICAL", "HIGH"))).sum()),
        "overdue": float((open_mask & due.notna() & (due.dt.normalize() < today)).sum()),
        "unassigned": float((open_mask & owner.isin(("", "UNASSIGNED"))).sum()),
        "estimated_usd": round(float(dollars[open_mask].sum()), 2),
    }


def confidence_state(value: object, *, stale: bool = False, coverage_pct: object = None) -> dict[str, str]:
    """Translate evidence quality into one reusable UI state."""
    confidence = max(0.0, min(safe_float(value), 1.0))
    coverage = None if coverage_pct is None else max(0.0, min(safe_float(coverage_pct), 100.0))
    if stale:
        return {"label": "Stale evidence", "state": "bad", "reason": "Source freshness is outside its contract."}
    if coverage is not None and coverage < 80:
        return {"label": "Low coverage", "state": "warn", "reason": f"Only {coverage:.0f}% of the expected evidence is present."}
    if confidence >= 0.8:
        return {"label": "High confidence", "state": "ok", "reason": "Evidence clears the action threshold."}
    if confidence >= 0.5:
        return {"label": "Review", "state": "warn", "reason": "Evidence supports investigation, not automatic action."}
    return {"label": "Low confidence", "state": "bad", "reason": "Recommendation should remain suppressed."}


@dataclass(frozen=True)
class InvestigationTarget:
    page: str
    section: str
    context: dict[str, str]


def investigation_target(kind: str, value: str) -> InvestigationTarget:
    """Route a typed universal-search value to its owning workbench."""
    category = str(kind or "").strip().upper().replace(" ", "_")
    target = str(value or "").strip()
    if not target:
        raise ValueError("Search value is required")
    if category == "QUERY_ID":
        return InvestigationTarget("Operations", "Queries", {"query_id": target})
    if category == "ACTION_ID":
        return InvestigationTarget("Control Room", "Action Center", {"action_id": target})
    if category == "ALERT_ID":
        return InvestigationTarget("Alerts", "Open events", {"event_id": target})
    if category == "INCIDENT_ID":
        return InvestigationTarget("Control Room", "Incidents & triage", {"incident_id": target})
    entity_kind = normalize_entity_type(category)
    return InvestigationTarget(
        "Control Room", "Entity 360", {"entity_type": entity_kind, "entity_key": target}
    )
