"""Query Optimization Intelligence — the fingerprint-grain scoring layer.

Builds on the per-query advisor (``query_advisor.advise`` — already a capped, additive,
per-driver 0-100 badness score + plain-English fixes) and the fingerprint grain
(``ops_sql.query_opportunity_fingerprints`` — one row per recurring logical query). For
each fingerprint it derives:

- **QOP** (0-100): the advisor's badness for the TYPICAL execution — honest, additive,
  per-driver-capped, and kept explainable via the finding breakdown (NO black-box index).
- **SQL_QOP**: QOP minus the queue contribution — SQL badness isolated from concurrency, so
  a query that is 3s of work behind 25s of queueing is NOT called bad SQL.
- **Primary pathology**: the dominant finding, with a CONCURRENCY STARVATION (overload) or
  COLD-START WAIT (resume/provisioning) verdict when queueing dominates and the SQL is clean
  (the spec's key distinction), and COMPOUND when
  three or more independent SQL problems co-occur.
- **OOS** (0-100): inefficiency x how much the fingerprint actually runs. impact = the
  PERCENTILE RANK of the total compute footprint (winsorized by construction), so a
  moderately-bad query run 20k times outranks a catastrophic one run once, WITHOUT a naive
  multiply letting one extreme dominate. OOS = QOP-fraction x impact-percentile.
- **Confidence** (0-100): corroborating findings + sample size — separate from severity.

Pure pandas; no Streamlit, no Snowflake. Every score is reconstructable from the returned
columns plus the per-fingerprint finding breakdown.
"""

from __future__ import annotations

import pandas as pd

from app.logic import query_advisor
from app.logic.formulas import safe_float

# an advisor finding code -> a human pathology label
_PATHOLOGY = {
    "remote_spill": "Spill (memory)",
    "local_spill": "Spill (memory)",
    "poor_pruning": "Pruning failure",
    "cold_scan": "Scan amplification",
    "compile_bound": "Compilation heavy",
    "metadata_chatter": "Metadata chatter",
    "queued": "Concurrency starvation",
    "cold_start": "Cold-start wait",
    "zero_result": "Expensive empty result",
}
# Capacity findings (the warehouse, not the SQL): excluded from SQL badness and never named as the
# SQL driver. cold_start = the resume/provisioning share of the wait dominates (Next-Fifty #17).
_CAPACITY_CODES = frozenset({"queued", "cold_start"})
# SQL badness (QOP minus the queue driver) at/below this = the SQL is not the problem
_SQL_CLEAN_QOP = 15
# The fingerprint grain feeds advise AVG(remote spill): a rare 1-in-N spill averages to
# ~0, so require a meaningful average before firing advise's "ran out of memory" finding
# (on a single QUERY_HISTORY row the >0 default is correct; here it over-fires). Bug-hunt R1.
_FINGERPRINT_REMOTE_SPILL_FLOOR_GB = 0.5
_OUTPUT_COLS = ["FINGERPRINT", "SAMPLE_TEXT", "QUERY_TYPE", "WAREHOUSE_NAME", "RUNS",
                "TOTAL_EXEC_SEC", "QOP", "SQL_QOP", "OOS", "PATHOLOGY", "CONFIDENCE",
                "FIRST_ACTION", "LAST_SEEN", "_FINDINGS"]


def _confidence(n_findings: int, runs: float) -> int:
    """More corroborating findings + a bigger sample => higher confidence. Separate from
    severity: a single-signal or tiny-sample diagnosis is honestly less certain."""
    base = {0: 40, 1: 62}.get(n_findings, min(90, 62 + 12 * (n_findings - 1)))
    if runs < 5:
        base -= 20   # a handful of executions is a weak basis for a verdict
    return int(max(30, min(95, base)))


def _pathology(findings: list, sql_qop: int) -> str:
    if not findings:
        return "None"
    top = findings[0]
    n_sql = sum(1 for f in findings if f.code not in _CAPACITY_CODES)
    # "Concurrency starvation" ONLY when queueing dominates the score AND the SQL underneath
    # is genuinely clean — a warehouse/capacity problem, not bad SQL. A queued-top fingerprint
    # that STILL carries material SQL badness (sql_qop over the clean bar) is a SQL problem, so
    # it must be named by its SQL, never told "don't rewrite the query". (The queue finding
    # never names the fix here, so label by the dominant NON-queue driver.)
    if top.code in _CAPACITY_CODES and sql_qop <= _SQL_CLEAN_QOP:
        return _PATHOLOGY[top.code]
    if n_sql >= 3:
        return "Compound inefficiency"
    top_sql = next((f for f in findings if f.code not in _CAPACITY_CODES), None)
    return (_PATHOLOGY.get(top_sql.code, "Other") if top_sql is not None
            else _PATHOLOGY.get(top.code, "Concurrency starvation"))


def _lead_finding(findings: list, sql_qop: int):
    """The finding whose fix to LEAD with — mirrors _pathology's label choice so the
    'first fix' (and the drill's top row) never disagrees with the pathology. A
    queued-TOP fingerprint that still carries material SQL badness leads with its
    dominant SQL driver, never the queue finding (which says 'don't rewrite the query')."""
    if not findings:
        return None
    top = findings[0]
    if top.code in _CAPACITY_CODES and sql_qop > _SQL_CLEAN_QOP:
        top_sql = next((f for f in findings if f.code not in _CAPACITY_CODES), None)
        if top_sql is not None:
            return top_sql
    return top


def score_opportunities(df: pd.DataFrame | None) -> tuple[pd.DataFrame, dict]:
    """Score + rank the fingerprint frame. Returns ``(ranked_frame, breakdowns)`` where
    ``breakdowns`` maps FINGERPRINT -> list of ``(title, points, detail)`` for the QOP
    explainer. Empty/None in -> empty frame + empty dict."""
    if df is None or df.empty:
        return pd.DataFrame(columns=_OUTPUT_COLS), {}
    rows: list[dict] = []
    breakdowns: dict = {}
    for _, r in df.iterrows():
        findings, qop = query_advisor.advise(
            r, remote_spill_floor_gb=_FINGERPRINT_REMOTE_SPILL_FLOOR_GB)
        # SQL badness = the non-queue drivers, capped the same way advise caps the total.
        # (Subtracting a queue finding's points from the CAPPED qop would understate this
        # when the raw point-sum already saturates at 100 — identical in the uncapped regime.)
        sql_qop = min(100, sum(f.points for f in findings if f.code not in _CAPACITY_CODES))
        lead = _lead_finding(findings, sql_qop)
        fp = str(r.get("FINGERPRINT", ""))
        # Lead the breakdown with the fix-to-take (so the drill's "top row is the first fix"
        # caption is honest) — the rest stay in points order for the additive-score story.
        _ordered = ([lead, *(f for f in findings if f is not lead)] if lead is not None
                    else findings)
        breakdowns[fp] = [(f.title, f.points, f.detail) for f in _ordered]
        rows.append({
            "FINGERPRINT": fp,
            "SAMPLE_TEXT": str(r.get("SAMPLE_TEXT", "")),
            "QUERY_TYPE": str(r.get("QUERY_TYPE", "")),
            "WAREHOUSE_NAME": str(r.get("WAREHOUSE_NAME", "")),
            "RUNS": int(safe_float(r.get("RUNS"))),
            "TOTAL_EXEC_SEC": safe_float(r.get("TOTAL_EXEC_SEC")),
            "TOTAL_COMPILE_SEC": safe_float(r.get("TOTAL_COMPILE_SEC")),
            "QOP": int(qop),
            "SQL_QOP": int(sql_qop),
            "PATHOLOGY": _pathology(findings, sql_qop),
            "CONFIDENCE": _confidence(len(findings), safe_float(r.get("RUNS"))),
            "FIRST_ACTION": (lead.detail if lead is not None else "No actionable finding."),
            "LAST_SEEN": r.get("LAST_SEEN"),   # passthrough for the panel's "Last seen" column
            "_FINDINGS": len(findings),
        })
    out = pd.DataFrame(rows)
    # OOS impact leg = percentile rank of the FOOTPRINT (bounded 0-100, winsorized by construction)
    # so one giant footprint can't dominate; combined with the QOP fraction. The footprint is the
    # GREATER of the compute (exec-seconds) and the compile-seconds percentile: a metadata /
    # compile-dominated family does ~no warehouse execution, so ranking it by exec-seconds alone
    # (the old leg) buried it — its compile-seconds footprint is the honest "how much work" axis.
    # Guarded so a frame with no compile footprint (older callers / tests) keeps the exec-only leg.
    if len(out) > 1:
        impact_pct = out["TOTAL_EXEC_SEC"].rank(pct=True) * 100.0
        if "TOTAL_COMPILE_SEC" in out.columns and out["TOTAL_COMPILE_SEC"].sum() > 0:
            compile_pct = out["TOTAL_COMPILE_SEC"].rank(pct=True) * 100.0
            impact_pct = pd.concat([impact_pct, compile_pct], axis=1).max(axis=1)
    else:
        impact_pct = pd.Series(100.0, index=out.index)
    out["OOS"] = (out["QOP"] / 100.0 * impact_pct).round(1)
    out = out.sort_values(["OOS", "QOP"], ascending=[False, False],
                          kind="stable").reset_index(drop=True)
    return out[_OUTPUT_COLS], breakdowns
