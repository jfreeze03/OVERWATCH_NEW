"""Pure prioritization and scenario math for the Decision Studio."""

from __future__ import annotations

import math

import pandas as pd

from app.logic.formulas import safe_float


def prioritize_workloads(frame: pd.DataFrame | None, rate: float,
                         days: int) -> pd.DataFrame:
    """Rank measured query families by impact, evidence and blast-radius proxy."""
    if frame is None or frame.empty:
        return pd.DataFrame() if frame is None else frame.copy()
    out = frame.copy()
    horizon = max(1, int(days or 1))
    runs = pd.to_numeric(out.get("RUNS", 0), errors="coerce").fillna(0.0)
    fails = pd.to_numeric(out.get("FAILS", 0), errors="coerce").fillna(0.0)
    credits = pd.to_numeric(out.get("CREDITS", 0), errors="coerce").fillna(0.0)
    active_days = pd.to_numeric(out.get("ACTIVE_DAYS", 0), errors="coerce").fillna(0.0)
    users = pd.to_numeric(out.get("USERS", 0), errors="coerce").fillna(0.0)
    databases = pd.to_numeric(out.get("DATABASES", 0), errors="coerce").fillna(0.0)
    cache = pd.to_numeric(out.get("AVG_CACHE_PCT", 0), errors="coerce").fillna(0.0)
    p95 = pd.to_numeric(out.get("P95_SEC", 0), errors="coerce").fillna(0.0)

    # Decision-Studio #2: a MISSING measurement is not a measured 0. Family behavioral
    # evidence (cache %, P95, fails) is absent whenever a cost fingerprint missed the
    # family mart (join miss, or below its 2000/day cap); coercing NULL->0 made a blind
    # row look like 0% cache + instant queries and drove a "Cache this / ACT NOW" call
    # with the cache column rendered blank. Track presence from the RAW columns and never
    # recommend an action, or reach ACT NOW, from an absent measurement.
    def _present(col: str) -> pd.Series:
        if col not in out.columns:
            return pd.Series(False, index=out.index)
        return pd.to_numeric(out[col], errors="coerce").notna()
    has_cache = _present("AVG_CACHE_PCT")
    has_latency = _present("P95_SEC")
    has_fails = _present("FAILS")
    has_behavior = has_cache | has_latency | has_fails
    out["EVIDENCE_COVERAGE"] = (
        (has_cache.astype(float) + has_latency.astype(float) + has_fails.astype(float)) / 3
    ).round(2)

    out["IMPACT_USD_30D"] = (credits * max(safe_float(rate), 0.0) / horizon * 30).round(2)
    out["FAIL_PCT"] = (fails / runs.replace(0, pd.NA) * 100).fillna(0.0).round(2)
    run_evidence = (runs / 30).clip(upper=1.0)
    day_evidence = (active_days / min(horizon, 30)).clip(upper=1.0)
    cost_evidence = credits.gt(0).astype(float)
    out["CONFIDENCE"] = (
        0.35 * run_evidence + 0.35 * day_evidence + 0.30 * cost_evidence
    ).clip(0.0, 1.0).round(2)
    out["BLAST_RADIUS"] = (users + databases).round(0)
    effort_score = users + databases * 2
    out["EFFORT_PROXY"] = pd.cut(
        effort_score,
        bins=[-math.inf, 5, 15, math.inf],
        labels=["LOW", "MEDIUM", "HIGH"],
    ).astype(str)
    reliability_boost = 1.0 + (out["FAIL_PCT"] / 100).clip(upper=0.5)
    effort_divisor = 1.0 + effort_score.map(lambda value: math.log2(max(value, 0.0) + 1))
    out["PRIORITY_SCORE"] = (
        out["IMPACT_USD_30D"] * out["CONFIDENCE"] * reliability_boost / effort_divisor
    ).round(2)
    percentile = out["PRIORITY_SCORE"].rank(method="min", pct=True)
    out["LANE"] = "PLAN"
    out.loc[out["CONFIDENCE"] < 0.5, "LANE"] = "VALIDATE"
    out.loc[(percentile >= 0.8) & (out["CONFIDENCE"] >= 0.65), "LANE"] = "ACT NOW"
    # #2: with no behavioral evidence we cannot know WHAT to do, so a high-cost-but-blind
    # family must never reach ACT NOW off cost signals alone — send it to VALIDATE.
    out.loc[~has_behavior, "LANE"] = "VALIDATE"
    out["NEXT_MOVE"] = "Profile and tune"
    # #2: gate the caching and latency recommendations on the measurement actually being
    # present, so a NULL-fill (cache=0, p95=0) can no longer trigger them.
    cache_candidate = has_cache & (cache <= 25) & (runs / horizon * 30 >= 10)
    out.loc[cache_candidate, "NEXT_MOVE"] = "Cache or materialize"
    out.loc[out["FAIL_PCT"] >= 2, "NEXT_MOVE"] = "Stabilize failures"
    out.loc[(p95 < 5) & has_latency & (out["FAIL_PCT"] < 2) & ~cache_candidate, "NEXT_MOVE"] = (
        "Reduce recurrence"
    )
    out.loc[~has_behavior, "NEXT_MOVE"] = "Validate evidence"
    return out.sort_values(
        ["LANE", "PRIORITY_SCORE"],
        ascending=[True, False],
        key=lambda series: series.map({"ACT NOW": 0, "PLAN": 1, "VALIDATE": 2})
        if series.name == "LANE" else series,
    ).reset_index(drop=True)


def scenario_projection(actions: pd.DataFrame | None, *, adoption_pct: float,
                        realization_pct: float, confidence_floor: float) -> dict[str, float]:
    """Haircut and de-duplicate authored action estimates without mixing verified value."""
    if actions is None or actions.empty:
        return {
            "candidates": 0.0,
            "gross_estimate": 0.0,
            "expected_capture": 0.0,
            "low_capture": 0.0,
            "high_capture": 0.0,
        }
    view = actions.copy()
    status = view.get("STATUS", pd.Series("OPEN", index=view.index)).astype(str).str.upper()
    confidence = pd.to_numeric(
        view.get("CONFIDENCE", pd.Series(0.0, index=view.index)), errors="coerce"
    ).fillna(0.0).clip(0.0, 1.0)
    estimates = pd.to_numeric(
        view.get("ESTIMATED_USD", pd.Series(0.0, index=view.index)), errors="coerce"
    ).fillna(0.0).clip(lower=0.0)
    view = view.assign(_CONFIDENCE=confidence, _ESTIMATE=estimates)
    view = view[status.isin(("OPEN", "IN_PROGRESS")) & (confidence >= confidence_floor)]
    if view.empty:
        return scenario_projection(None, adoption_pct=adoption_pct,
                                   realization_pct=realization_pct,
                                   confidence_floor=confidence_floor)
    entity_type = view.get(
        "SOURCE_ENTITY_TYPE", pd.Series("", index=view.index)
    ).fillna("").astype(str)
    entity_key = view.get(
        "SOURCE_ENTITY_KEY", pd.Series("", index=view.index)
    ).fillna("").astype(str)
    action_id = view.get("ACTION_ID", pd.Series(view.index, index=view.index)).astype(str)
    entity_id = (entity_type.str.upper() + ":" + entity_key.str.upper()).str.strip(":")
    view["_ENTITY"] = entity_id.where(entity_id.str.len().gt(1), action_id)
    deduped = view.sort_values("_ESTIMATE", ascending=False).drop_duplicates("_ENTITY")
    gross = float(deduped["_ESTIMATE"].sum())
    adoption = max(0.0, min(safe_float(adoption_pct), 100.0)) / 100
    realization = max(0.0, min(safe_float(realization_pct), 100.0)) / 100
    expected = gross * adoption * realization
    return {
        "candidates": float(len(deduped)),
        "gross_estimate": round(gross, 2),
        "expected_capture": round(expected, 2),
        "low_capture": round(gross * adoption * max(realization - 0.2, 0.0), 2),
        "high_capture": round(min(gross, gross * adoption * min(realization + 0.2, 1.0)), 2),
    }


def slo_summary(frame: pd.DataFrame | None) -> dict[str, float]:
    if frame is None or frame.empty:
        return {"total": 0.0, "met": 0.0, "breach": 0.0, "no_data": 0.0,
                "worst_burn": 0.0}
    status = frame.get("STATUS", pd.Series("NO_DATA", index=frame.index)).astype(str).str.upper()
    burn = pd.to_numeric(
        frame.get("BURN_MULTIPLE", pd.Series(0.0, index=frame.index)), errors="coerce"
    ).fillna(0.0)
    return {
        "total": float(len(frame)),
        "met": float(status.eq("MET").sum()),
        "breach": float(status.eq("BREACH").sum()),
        "no_data": float(status.eq("NO_DATA").sum()),
        "worst_burn": round(float(burn.max()), 2),
    }
