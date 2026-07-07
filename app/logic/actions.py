"""Action-queue ranking and the savings ledger state machine.

The savings ledger is the app's differentiator: estimated savings and verified
savings are separate states, and nothing reaches VERIFIED without a proof
query and a verified dollar amount.
"""

from __future__ import annotations

import pandas as pd

SEVERITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
OPEN_STATUSES = ("OPEN", "IN_PROGRESS")

LEDGER_ESTIMATED = "ESTIMATED"
LEDGER_VERIFIED = "VERIFIED"
LEDGER_REJECTED = "REJECTED"
LEDGER_STATES = (LEDGER_ESTIMATED, LEDGER_VERIFIED, LEDGER_REJECTED)


def rank_actions(df: pd.DataFrame, limit: int = 25) -> pd.DataFrame:
    """Rank open actions: severity, then overdue-ness, then age.

    Expects columns SEVERITY, STATUS, DUE_DATE, CREATED_AT (extra columns pass
    through untouched). Non-open rows are dropped.
    """
    if df is None or df.empty:
        return pd.DataFrame()
    view = df.copy()
    view["STATUS"] = view.get("STATUS", pd.Series(dtype=str)).astype(str).str.upper()
    view = view[view["STATUS"].isin(OPEN_STATUSES)].copy()
    if view.empty:
        return view
    view["_SEV"] = view.get("SEVERITY", "").astype(str).str.upper().map(SEVERITY_RANK).fillna(9)
    due = pd.to_datetime(view.get("DUE_DATE"), errors="coerce")
    created = pd.to_datetime(view.get("CREATED_AT"), errors="coerce")
    now = pd.Timestamp.now()
    view["_OVERDUE"] = (due.notna() & (due < now)).astype(int)
    view["_AGE_H"] = ((now - created).dt.total_seconds() / 3600).fillna(0).clip(lower=0)
    view = view.sort_values(["_SEV", "_OVERDUE", "_AGE_H"], ascending=[True, False, False])
    return view.drop(columns=["_SEV", "_OVERDUE", "_AGE_H"]).head(limit)


def can_verify(row: dict) -> tuple[bool, str]:
    """Gate for ESTIMATED -> VERIFIED: needs proof SQL and a verified amount."""
    state = str(row.get("STATE", "")).upper()
    if state != LEDGER_ESTIMATED:
        return False, f"Only {LEDGER_ESTIMATED} items can be verified (state={state or 'missing'})."
    proof = str(row.get("PROOF_SQL", "") or "").strip()
    if not proof:
        return False, "A proof query is required before verification."
    try:
        verified = float(str(row.get("VERIFIED_USD")))
    except (TypeError, ValueError):
        return False, "A numeric verified USD amount is required."
    if verified < 0:
        return False, "Verified USD cannot be negative."
    return True, ""


def ledger_totals(df: pd.DataFrame) -> dict:
    """Estimated vs verified totals; never mixes the two."""
    if df is None or df.empty or "STATE" not in df.columns:
        return {"estimated_usd": 0.0, "verified_usd": 0.0, "estimated_count": 0, "verified_count": 0}
    view = df.copy()
    view["STATE"] = view["STATE"].astype(str).str.upper()
    est = view[view["STATE"] == LEDGER_ESTIMATED]
    ver = view[view["STATE"] == LEDGER_VERIFIED]
    est_usd = pd.to_numeric(est.get("ESTIMATED_USD"), errors="coerce").fillna(0).sum()
    ver_usd = pd.to_numeric(ver.get("VERIFIED_USD"), errors="coerce").fillna(0).sum()
    return {
        "estimated_usd": round(float(est_usd), 2),
        "verified_usd": round(float(ver_usd), 2),
        "estimated_count": len(est),
        "verified_count": len(ver),
    }


def triage_queue(
    alerts: pd.DataFrame | None,
    task_failures: pd.DataFrame | None,
    anomalies: list[dict] | None,
) -> pd.DataFrame:
    """Merge alert events, task failures, and spend anomalies into one ranked
    morning-triage queue for the Control Room."""
    rows: list[dict] = []
    if alerts is not None and not alerts.empty:
        for _, r in alerts.iterrows():
            rows.append({
                "SEVERITY": str(r.get("SEVERITY", "MEDIUM")).upper(),
                "KIND": "Alert",
                "DATABASE": "",
                "TITLE": str(r.get("TITLE", "Alert event")),
                "DETAIL": str(r.get("DETAIL", ""))[:220],
                "SOURCE": "ALERT_EVENTS",
                "RAISED_AT": r.get("RAISED_AT"),
            })
    if task_failures is not None and not task_failures.empty:
        for _, r in task_failures.iterrows():
            failed = int(float(r.get("FAILED", 0) or 0))
            if failed <= 0:
                continue
            database = str(r.get("DATABASE_NAME", "") or "")
            schema = str(r.get("SCHEMA_NAME", "") or "")
            qualified = ".".join(p for p in (database, schema) if p)
            rows.append({
                "SEVERITY": "HIGH" if failed >= 3 else "MEDIUM",
                "KIND": "Task failure",
                "DATABASE": database,
                "TITLE": f"{qualified + '.' if qualified else ''}{r.get('TASK_NAME', 'task')} failed {failed}x",
                "DETAIL": str(r.get("LAST_ERROR", "") or "")[:220],
                "SOURCE": "FACT_TASK_DAILY",
                "RAISED_AT": r.get("DAY"),
            })
    rows.extend({
        "SEVERITY": "HIGH" if abs(a.get("z", 0)) >= 5 else "MEDIUM",
        "KIND": "Spend anomaly",
        "DATABASE": "",
        "TITLE": f"{a.get('label', 'warehouse')} daily spend z={a.get('z', 0):+.1f}",
        "DETAIL": f"Daily spend ${a.get('value', 0):,.0f} vs robust baseline.",
        "SOURCE": "FACT_WAREHOUSE_DAILY",
        "RAISED_AT": None,
    } for a in anomalies or [])
    if not rows:
        return pd.DataFrame()
    queue = pd.DataFrame(rows)
    # Sources mix timestamps, dates, and None here; Arrow serialization in
    # st.dataframe requires one type, so render RAISED_AT as text.
    queue["RAISED_AT"] = queue["RAISED_AT"].map(
        lambda v: "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)
    )
    queue["_SEV"] = queue["SEVERITY"].map(SEVERITY_RANK).fillna(9)
    queue = queue.sort_values(["_SEV", "KIND"]).drop(columns=["_SEV"]).reset_index(drop=True)
    return queue
