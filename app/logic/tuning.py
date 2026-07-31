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

# Rules whose condition is METRIC_VALUE <= THRESHOLD (LOW is bad), not >=.
# For these the whole suggestion must MIRROR: raising the threshold WIDENS firing,
# so the noise-cutting move is DOWNWARD. Getting this backwards produced advice
# that did the opposite of its own stated basis (audit A1, 2026-07-31) — and the
# operator pastes these into a real ALTER, so the direction has to be right.
#   SEC_CRED_EXPIRY      fires when days-until-expiry <= N
#   COST_CONTRACT_BREACH fires when projected days-left <= N
LOWER_IS_WORSE = frozenset({"SEC_CRED_EXPIRY", "COST_CONTRACT_BREACH"})


def suggest_threshold(metric_values: pd.DataFrame, current_threshold: float,
                      rule_id: str = "") -> dict:
    """One rule's suggestion from rows [METRIC_VALUE, RESOLUTION_KIND].

    Returns {ok, suggested, basis, noise_n, actioned_n}; ok=False with a
    basis explaining why when the evidence is too thin or not separable.

    ``rule_id`` selects the comparison direction (see LOWER_IS_WORSE). Omitting it
    keeps the higher-is-worse default, which is right for every other seeded rule.
    """
    import math

    current = safe_float(current_threshold)
    inverse = str(rule_id or "").strip().upper() in LOWER_IS_WORSE
    if metric_values is None or metric_values.empty:
        return {"ok": False, "basis": "No resolved events with metric values yet.",
                "noise_n": 0, "actioned_n": 0}
    frame = metric_values.copy()
    frame["METRIC_VALUE"] = frame["METRIC_VALUE"].map(safe_float)
    # A1: filter NON-FINITE only. The old `> 0` dropped 0 and negatives — which on an
    # inverse rule are the MOST actionable evidence there is (expires today / already
    # expired, contract already exhausted).
    frame = frame[frame["METRIC_VALUE"].map(lambda v: math.isfinite(safe_float(v)))]
    kinds = frame["RESOLUTION_KIND"].astype(str).str.upper()
    noise = frame[kinds == "NOISE"]["METRIC_VALUE"]
    actioned = frame[kinds == "ACTIONED"]["METRIC_VALUE"]
    n_noise, n_actioned = len(noise), len(actioned)

    if n_noise < MIN_NOISE_FOR_SUGGESTION:
        return {"ok": False, "noise_n": n_noise, "actioned_n": n_actioned,
                "basis": f"Only {n_noise} noise events — need {MIN_NOISE_FOR_SUGGESTION}+ "
                         "before a suggestion is trustworthy."}

    if n_actioned == 0:
        # Pure noise: everything this rule caught was closed as noise. Move the
        # threshold AWAY from the noise — up for >= rules, DOWN for <= rules.
        if inverse:
            suggested = round(float(noise.quantile(0.05)) * 0.90, 2)
            if current > 0 and suggested >= current:
                suggested = round(current * 0.5, 2)
            tail = "clears 95% of them (-10%)"
        else:
            suggested = round(float(noise.quantile(0.95)) * 1.10, 2)
            if current > 0 and suggested <= current:
                suggested = round(current * 1.5, 2)
            tail = "clears 95% of them (+10%)"
        return {"ok": True, "suggested": suggested, "noise_n": n_noise, "actioned_n": 0,
                "basis": f"All {n_noise} resolved events were noise; {suggested} {tail}. "
                         "If it keeps firing, consider disabling the rule."}

    if inverse:
        # Mirror image: actioned values sit BELOW the threshold, noise ABOVE it.
        keep_bound = float(actioned.quantile(KEEP_ACTIONED_SHARE))   # actioned ceiling
        noise_bound = float(noise.quantile(0.10))                    # noise floor
        separable = keep_bound < noise_bound
        bound_label = f"noise p10 ({noise_bound:.2f}) and the actioned ceiling ({keep_bound:.2f})"
    else:
        keep_bound = float(actioned.quantile(1.0 - KEEP_ACTIONED_SHARE))  # actioned floor
        noise_bound = float(noise.quantile(0.90))                         # noise ceiling
        separable = keep_bound > noise_bound
        bound_label = f"noise p90 ({noise_bound:.2f}) and the actioned floor ({keep_bound:.2f})"

    if not separable:
        return {"ok": False, "noise_n": n_noise, "actioned_n": n_actioned,
                "basis": "Noise and actioned values overlap — a threshold move can't "
                         "separate them; the rule's condition needs redesign, not tuning."}

    suggested = round((noise_bound + keep_bound) / 2.0, 2)
    if current > 0 and abs(suggested - current) / current < 0.05:
        return {"ok": False, "noise_n": n_noise, "actioned_n": n_actioned,
                "basis": "Evidence supports the current threshold (suggestion within 5%)."}
    if inverse:
        kept = float((actioned <= suggested).mean() * 100)
        cut = float((noise > suggested).mean() * 100)
    else:
        kept = float((actioned >= suggested).mean() * 100)
        cut = float((noise < suggested).mean() * 100)
    return {"ok": True, "suggested": suggested, "noise_n": n_noise, "actioned_n": n_actioned,
            "basis": f"Midpoint of {bound_label}: keeps {kept:.0f}% of actioned, "
                     f"cuts {cut:.0f}% of noise."}


def suggestions_by_rule(events: pd.DataFrame, thresholds: dict[str, float]) -> pd.DataFrame:
    """Vector version for the Rules panel: events [RULE_ID, METRIC_VALUE,
    RESOLUTION_KIND] + {rule_id: current_threshold} -> one row per rule."""
    if events is None or events.empty:
        return pd.DataFrame()
    rows = []
    for rule_id, block in events.groupby(events["RULE_ID"].astype(str)):
        # A1: rule_id selects the comparison direction — an inverse-metric rule
        # (SEC_CRED_EXPIRY, COST_CONTRACT_BREACH) must be tuned DOWNWARD.
        result = suggest_threshold(block, safe_float(thresholds.get(rule_id, 0.0)), rule_id)
        rows.append({
            "RULE_ID": rule_id,
            "CURRENT_THRESHOLD": safe_float(thresholds.get(rule_id, 0.0)),
            "SUGGESTED_THRESHOLD": result.get("suggested"),
            "NOISE_N": result.get("noise_n", 0),
            "ACTIONED_N": result.get("actioned_n", 0),
            "BASIS": result.get("basis", ""),
        })
    return pd.DataFrame(rows).sort_values("NOISE_N", ascending=False).reset_index(drop=True)
