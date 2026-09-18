"""Query Optimization Intelligence — the fingerprint-grain scoring layer.

Builds on the per-query advisor (``query_advisor.advise`` — already a capped, additive,
per-driver 0-100 badness score + plain-English fixes) and the fingerprint grain
(``ops_sql.query_opportunity_fingerprints`` — one row per recurring logical query). For
each fingerprint it derives:

- **QOP** (0-100): the advisor's badness for the TYPICAL execution — honest, additive,
  per-driver-capped, and kept explainable via the finding breakdown (NO black-box index).
- **SQL_QOP**: QOP minus the queue contribution — SQL badness isolated from concurrency, so
  a query that is 3s of work behind 25s of queueing is NOT called bad SQL.
- **Primary pathology**: the dominant finding, with a CONCURRENCY STARVATION verdict when
  queueing dominates and the SQL is clean (the spec's key distinction), and COMPOUND when
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
    "queued": "Concurrency starvation",
    "zero_result": "Expensive empty result",
}
# SQL badness (QOP minus the queue driver) at/below this = the SQL is not the problem
_SQL_CLEAN_QOP = 15
_OUTPUT_COLS = ["FINGERPRINT", "SAMPLE_TEXT", "QUERY_TYPE", "WAREHOUSE_NAME", "RUNS",
                "TOTAL_EXEC_SEC", "QOP", "SQL_QOP", "OOS", "PATHOLOGY", "CONFIDENCE",
                "FIRST_ACTION", "_FINDINGS"]


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
    n_sql = sum(1 for f in findings if f.code != "queued")
    # "Concurrency starvation" ONLY when queueing dominates the score AND the SQL underneath
    # is genuinely clean — a warehouse/capacity problem, not bad SQL. A queued-top fingerprint
    # that STILL carries material SQL badness (sql_qop over the clean bar) is a SQL problem, so
    # it must be named by its SQL, never told "don't rewrite the query". (The queue finding
    # never names the fix here, so label by the dominant NON-queue driver.)
    if top.code == "queued" and sql_qop <= _SQL_CLEAN_QOP:
        return "Concurrency starvation"
    if n_sql >= 3:
        return "Compound inefficiency"
    top_sql = next((f for f in findings if f.code != "queued"), None)
    return _PATHOLOGY.get(top_sql.code, "Other") if top_sql is not None else "Concurrency starvation"


def score_opportunities(df: pd.DataFrame | None) -> tuple[pd.DataFrame, dict]:
    """Score + rank the fingerprint frame. Returns ``(ranked_frame, breakdowns)`` where
    ``breakdowns`` maps FINGERPRINT -> list of ``(title, points, detail)`` for the QOP
    explainer. Empty/None in -> empty frame + empty dict."""
    if df is None or df.empty:
        return pd.DataFrame(columns=_OUTPUT_COLS), {}
    rows: list[dict] = []
    breakdowns: dict = {}
    for _, r in df.iterrows():
        findings, qop = query_advisor.advise(r)
        # SQL badness = the non-queue drivers, capped the same way advise caps the total.
        # (Subtracting a queue finding's points from the CAPPED qop would understate this
        # when the raw point-sum already saturates at 100 — identical in the uncapped regime.)
        sql_qop = min(100, sum(f.points for f in findings if f.code != "queued"))
        top = findings[0] if findings else None
        fp = str(r.get("FINGERPRINT", ""))
        breakdowns[fp] = [(f.title, f.points, f.detail) for f in findings]
        rows.append({
            "FINGERPRINT": fp,
            "SAMPLE_TEXT": str(r.get("SAMPLE_TEXT", "")),
            "QUERY_TYPE": str(r.get("QUERY_TYPE", "")),
            "WAREHOUSE_NAME": str(r.get("WAREHOUSE_NAME", "")),
            "RUNS": int(safe_float(r.get("RUNS"))),
            "TOTAL_EXEC_SEC": safe_float(r.get("TOTAL_EXEC_SEC")),
            "QOP": int(qop),
            "SQL_QOP": int(sql_qop),
            "PATHOLOGY": _pathology(findings, sql_qop),
            "CONFIDENCE": _confidence(len(findings), safe_float(r.get("RUNS"))),
            "FIRST_ACTION": (top.detail if top is not None else "No actionable finding."),
            "_FINDINGS": len(findings),
        })
    out = pd.DataFrame(rows)
    # OOS impact leg = percentile rank of the compute footprint (bounded 0-100, winsorized by
    # construction) so one giant footprint can't dominate; combined with the QOP fraction.
    impact_pct = (out["TOTAL_EXEC_SEC"].rank(pct=True) * 100.0 if len(out) > 1
                  else pd.Series(100.0, index=out.index))
    out["OOS"] = (out["QOP"] / 100.0 * impact_pct).round(1)
    out = out.sort_values(["OOS", "QOP"], ascending=[False, False],
                          kind="stable").reset_index(drop=True)
    return out[_OUTPUT_COLS], breakdowns
