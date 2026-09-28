"""Pure prioritization and scenario math: the Operations ▸ Optimize fix queue's ranking and the Proof ▸
Pipeline projection (both formerly on the Decision Studio page)."""

from __future__ import annotations

import math
from collections.abc import Iterable

import pandas as pd

from app.logic.formulas import safe_float
from app.logic.savings_rollup import SavingsOpportunity

# Lane gates for the query-family portfolio (Operations ▸ Optimize). Named so the scatter's confidence
# guide lines (F46) stay in lockstep with the lane assignment in prioritize_workloads.
# NOTE: ACT NOW keys off a PRIORITY_SCORE percentile (a composite of impact,
# confidence, reliability and effort) — NOT an impact threshold — so only the
# confidence axis has an axis-aligned gate the scatter can honestly draw.
LANE_CONF_FLOOR = 0.5     # confidence below this -> VALIDATE (evidence too thin to act)
LANE_ACTNOW_CONF = 0.65   # ACT NOW additionally requires at least this confidence...
LANE_ACTNOW_PCTL = 0.8    # ...AND a top-20% PRIORITY_SCORE percentile


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
    out.loc[out["CONFIDENCE"] < LANE_CONF_FLOOR, "LANE"] = "VALIDATE"
    out.loc[(percentile >= LANE_ACTNOW_PCTL) & (out["CONFIDENCE"] >= LANE_ACTNOW_CONF),
            "LANE"] = "ACT NOW"
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
    # A real entity needs a KEY, not just a TYPE: a blank key yields 'WAREHOUSE:' ->
    # strip(':') -> 'WAREHOUSE' (len 9), which the old len<=1 guard let through, so every
    # distinct blank-key action of that type collapsed into ONE entity and the ROI
    # candidate count / gross estimate under-counted. Gate on the key being present and
    # fall back to the (unique) ACTION_ID otherwise.
    _has_key = entity_key.str.strip().str.len().gt(0)
    view["_ENTITY"] = entity_id.where(_has_key, action_id)
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


# ACTION_QUEUE.PERIOD (V083; logic.workbench.ACTION_ESTIMATE_PERIODS) -> monthly run-rate divisor.
# ONE_TIME is kept OUT of the run-rate (reported beside it); NULL / '' / anything else is "unspecified",
# excluded from the run-rate but counted, so the projection never silently mixes time bases.
_PERIOD_MONTHLY_DIVISOR = {"MONTHLY": 1.0, "ANNUAL": 12.0}


def monthly_equivalent(frame: pd.DataFrame | None) -> tuple[pd.DataFrame, dict]:
    """Normalise authored ACTION_QUEUE estimates to a monthly run-rate (v4.597, Proof ▸ Pipeline).

    Returns ``(frame + MONTHLY_USD, summary)``. MONTHLY_USD = ESTIMATED_USD for MONTHLY, /12 for ANNUAL,
    NaN (not 0 — "—" in the grid) for ONE_TIME, unspecified PERIOD, or an unpriced row (NULL / <= 0
    estimate). ``summary`` counts every bucket so the UI can say "N open · M unpriced · K one-time":
      items, monthly_usd (the run-rate), priced_count (rows in the run-rate), annual_count,
      unpriced_count, one_time_count / one_time_usd, unspecified_count / unspecified_usd.
    Totals are over the frame PASSED IN — a LIMIT-capped action read must disclose its cap beside them."""
    summary: dict = {"items": 0, "monthly_usd": 0.0, "priced_count": 0, "annual_count": 0,
                     "unpriced_count": 0, "one_time_count": 0, "one_time_usd": 0.0,
                     "unspecified_count": 0, "unspecified_usd": 0.0}
    if frame is None or frame.empty:
        out = pd.DataFrame() if frame is None else frame.copy()
        out["MONTHLY_USD"] = pd.Series(dtype="float64")
        return out, summary
    out = frame.copy()
    usd = pd.to_numeric(out.get("ESTIMATED_USD", pd.Series(float("nan"), index=out.index)),
                        errors="coerce")
    period = (out.get("PERIOD", pd.Series("", index=out.index))
              .fillna("").astype(str).str.strip().str.upper())
    priced = usd.notna() & (usd > 0)
    divisor = period.map(_PERIOD_MONTHLY_DIVISOR)
    run_rate = priced & divisor.notna()
    out["MONTHLY_USD"] = (usd / divisor).where(run_rate).round(2)
    one_time = priced & period.eq("ONE_TIME")
    unspecified = priced & divisor.isna() & ~period.eq("ONE_TIME")
    summary.update({
        "items": len(out),
        "monthly_usd": round(float(out["MONTHLY_USD"].sum(skipna=True)), 2),
        "priced_count": int(run_rate.sum()),
        "annual_count": int((run_rate & period.eq("ANNUAL")).sum()),
        "unpriced_count": int((~priced).sum()),
        "one_time_count": int(one_time.sum()),
        "one_time_usd": round(float(usd[one_time].sum()), 2),
        "unspecified_count": int(unspecified.sum()),
        "unspecified_usd": round(float(usd[unspecified].sum()), 2),
    })
    return out, summary


_ADDRESSABLE_TITLE = {"IDLE": "Tighten auto-suspend on {target}", "RESIZE": "Right-size {target}"}


def pipeline_frame(rollup_items: Iterable[SavingsOpportunity] | None,
                   queued_df: pd.DataFrame | None) -> pd.DataFrame:
    """One scenario_projection-ready frame for Proof ▸ Pipeline (v4.597): the de-duplicated Cost ▸
    Optimize addressable rollup (savings_rollup.rollup_savings(...).items) UNION the queued ACTION_QUEUE
    rows, both on a monthly basis.

    Addressable rows are synthetic: KIND "Addressable", SOURCE_ENTITY_TYPE 'WAREHOUSE' + KEY = the
    target, STATUS OPEN, CONFIDENCE = the opportunity's 0..1 weight, ESTIMATED_USD = MONTHLY_USD = its
    $/mo, PERIOD MONTHLY. Queued rows (KIND "Queued") carry MONTHLY_USD (monthly_equivalent) AS
    ESTIMATED_USD — one-time / unspecified / unpriced rows project $0 — with the authored figure kept in
    AUTHORED_USD. scenario_projection then de-duplicates by entity (largest estimate wins), so a queued
    action on the same warehouse as an addressable opportunity is counted once. Verified savings never
    enter this frame."""
    rows = []
    for opp in rollup_items or ():
        source = str(opp.source).upper()
        target = str(opp.target)
        usd = round(safe_float(opp.monthly_usd), 2)
        rows.append({
            "ACTION_ID": f"addressable:{source}:{target}",
            "KIND": "Addressable",
            "SEVERITY": None,
            "TITLE": _ADDRESSABLE_TITLE.get(source, source.title() + " on {target}").format(target=target),
            "SOURCE": f"Cost ▸ Optimization & Savings ({source})",
            "SOURCE_ENTITY_TYPE": "WAREHOUSE",
            "SOURCE_ENTITY_KEY": target,
            "STATUS": "OPEN",
            "CONFIDENCE": max(0.0, min(safe_float(opp.confidence), 1.0)),
            "ESTIMATED_USD": usd,
            "MONTHLY_USD": usd,
            "AUTHORED_USD": None,
            "PERIOD": "MONTHLY",
            "OWNER": None,
        })
    addressable = pd.DataFrame(rows)
    if queued_df is None or queued_df.empty:
        return addressable.reset_index(drop=True)
    queued = queued_df if "MONTHLY_USD" in queued_df.columns else monthly_equivalent(queued_df)[0]
    queued = queued.copy()
    queued["KIND"] = "Queued"
    queued["AUTHORED_USD"] = queued["ESTIMATED_USD"] if "ESTIMATED_USD" in queued.columns else None
    queued["ESTIMATED_USD"] = queued["MONTHLY_USD"]
    if addressable.empty:
        return queued.reset_index(drop=True)
    # all-NA columns (e.g. SEVERITY / OWNER on the synthetic rows) are dropped before the concat and
    # restored after, so pandas never infers dtypes from all-NA blocks (deprecated behaviour).
    columns = list(dict.fromkeys([*addressable.columns, *queued.columns]))
    parts = [part.dropna(axis=1, how="all") for part in (addressable, queued)]
    return pd.concat(parts, ignore_index=True, sort=False).reindex(columns=columns)


def slo_summary(frame: pd.DataFrame | None) -> dict[str, float]:
    if frame is None or frame.empty:
        return {"total": 0.0, "met": 0.0, "breach": 0.0, "no_data": 0.0,
                "stale": 0.0, "worst_burn": 0.0, "has_burn": 0.0}
    status = frame.get("STATUS", pd.Series("NO_DATA", index=frame.index)).astype(str).str.upper()
    raw_burn = pd.to_numeric(
        frame.get("BURN_MULTIPLE", pd.Series(dtype="float64")), errors="coerce"
    )
    # #11: an objective evaluated off STALE (or NO_DATA) mart evidence is neither MET nor BREACHED —
    # its verdict is deliberately withheld. But the cockpit SQL still emits its last-known
    # BURN_MULTIPLE, so the worst-burn KPI and the reliability alarm must be scoped to the same
    # evaluated set, or a stalled loader fires a red "error-budget breach" off evidence the panel
    # elsewhere refuses to judge (ds-hunt 2026-08-30). Only MET/BREACH rows drive the burn signals.
    _evaluated = status.isin(("MET", "BREACH"))
    burn_eval = raw_burn.where(_evaluated)
    return {
        "total": float(len(frame)),
        "met": float(status.eq("MET").sum()),
        "breach": float(status.eq("BREACH").sum()),
        "no_data": float(status.eq("NO_DATA").sum()),
        # #11: objectives evaluated off stale mart evidence are neither met nor breached.
        "stale": float(status.eq("STALE").sum()),
        "worst_burn": round(float(burn_eval.fillna(0.0).max()), 2),
        # #10: error-budget burn only applies to SUCCESS_PCT objectives (NULL for
        # latency/P95). has_burn is False when no EVALUATED objective carries a burn, so the UI
        # shows "n/a" instead of a misleading 0.00x or a stale-only alarm.
        "has_burn": float(burn_eval.notna().any()),
    }
