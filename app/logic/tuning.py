"""Alert-threshold tuning from resolution evidence (pure, tested).

Turns the V021 precision data into advice: given the METRIC_VALUEs of a
rule's resolved events split by kind, suggest a threshold that would have
suppressed most NOISE while keeping the ACTIONED alerts. Suggestions are
advice with a stated basis — the operator still applies them through the
existing generate-only SQL flow.
"""

from __future__ import annotations

import pandas as pd

from .formulas import safe_float

MIN_NOISE_FOR_SUGGESTION = 5
KEEP_ACTIONED_SHARE = 0.90  # a suggestion must keep >= 90% of actioned alerts


def suggest_threshold(metric_values: pd.DataFrame, current_threshold: float) -> dict:
    """One rule's suggestion from rows [METRIC_VALUE, RESOLUTION_KIND].

    Returns {ok, suggested, basis, noise_n, actioned_n}; ok=False with a
    basis explaining why when the evidence is too thin or not separable.
    """
    current = safe_float(current_threshold)
    if metric_values is None or metric_values.empty:
        return {"ok": False, "basis": "No resolved events with metric values yet.",
                "noise_n": 0, "actioned_n": 0}
    frame = metric_values.copy()
    frame["METRIC_VALUE"] = frame["METRIC_VALUE"].map(safe_float)
    frame = frame[frame["METRIC_VALUE"] > 0]
    kinds = frame["RESOLUTION_KIND"].astype(str).str.upper()
    noise = frame[kinds == "NOISE"]["METRIC_VALUE"]
    actioned = frame[kinds == "ACTIONED"]["METRIC_VALUE"]
    n_noise, n_actioned = len(noise), len(actioned)

    if n_noise < MIN_NOISE_FOR_SUGGESTION:
        return {"ok": False, "noise_n": n_noise, "actioned_n": n_actioned,
                "basis": f"Only {n_noise} noise events — need {MIN_NOISE_FOR_SUGGESTION}+ "
                         "before a suggestion is trustworthy."}

    if n_actioned == 0:
        # Pure noise: everything this rule caught was closed as noise.
        suggested = round(float(noise.quantile(0.95)) * 1.10, 2)
        if current > 0 and suggested <= current:
            suggested = round(current * 1.5, 2)
        return {"ok": True, "suggested": suggested, "noise_n": n_noise, "actioned_n": 0,
                "basis": f"All {n_noise} resolved events were noise; {suggested} clears 95% "
                         "of them (+10%). If it keeps firing, consider disabling the rule."}

    keep_floor = float(actioned.quantile(1.0 - KEEP_ACTIONED_SHARE))
    noise_p90 = float(noise.quantile(0.90))
    if keep_floor <= noise_p90:
        return {"ok": False, "noise_n": n_noise, "actioned_n": n_actioned,
                "basis": "Noise and actioned values overlap — a threshold move can't "
                         "separate them; the rule's condition needs redesign, not tuning."}

    suggested = round((noise_p90 + keep_floor) / 2.0, 2)
    if current > 0 and abs(suggested - current) / current < 0.05:
        return {"ok": False, "noise_n": n_noise, "actioned_n": n_actioned,
                "basis": "Evidence supports the current threshold (suggestion within 5%)."}
    kept = float((actioned >= suggested).mean() * 100)
    cut = float((noise < suggested).mean() * 100)
    return {"ok": True, "suggested": suggested, "noise_n": n_noise, "actioned_n": n_actioned,
            "basis": f"Midpoint of noise p90 ({noise_p90:.2f}) and the actioned floor "
                     f"({keep_floor:.2f}): keeps {kept:.0f}% of actioned, cuts {cut:.0f}% of noise."}


def suggestions_by_rule(events: pd.DataFrame, thresholds: dict[str, float]) -> pd.DataFrame:
    """Vector version for the Rules panel: events [RULE_ID, METRIC_VALUE,
    RESOLUTION_KIND] + {rule_id: current_threshold} -> one row per rule."""
    if events is None or events.empty:
        return pd.DataFrame()
    rows = []
    for rule_id, block in events.groupby(events["RULE_ID"].astype(str)):
        result = suggest_threshold(block, safe_float(thresholds.get(rule_id, 0.0)))
        rows.append({
            "RULE_ID": rule_id,
            "CURRENT_THRESHOLD": safe_float(thresholds.get(rule_id, 0.0)),
            "SUGGESTED_THRESHOLD": result.get("suggested"),
            "NOISE_N": result.get("noise_n", 0),
            "ACTIONED_N": result.get("actioned_n", 0),
            "BASIS": result.get("basis", ""),
        })
    return pd.DataFrame(rows).sort_values("NOISE_N", ascending=False).reset_index(drop=True)
