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
#   PIPE_ETL_CYCLE_LATE  fires when minutes-left-to-the-SLA-target <= N (V156 lead window; METRIC_VALUE is
#                        written only for a lead-window WARN, so projection / CRIT / EXH never skew it)
LOWER_IS_WORSE = frozenset({"SEC_CRED_EXPIRY", "COST_CONTRACT_BREACH", "PIPE_ETL_CYCLE_LATE"})

# Rules raised once per occurrence whose arm ignores THRESHOLD_NUM and writes a CONSTANT METRIC_VALUE, so a
# threshold suggested from their metric values would be meaningless (and the operator would paste it into an
# ALTER that changes nothing):
#   SEC_ADMIN_GRANT  one event per direct admin-role grant to a user (V162 arm [27], METRIC_VALUE 1)
NO_THRESHOLD_RULES = frozenset({"SEC_ADMIN_GRANT"})
NO_THRESHOLD_BASIS = "Raised once per grant; this rule has no threshold."


def suggest_threshold(metric_values: pd.DataFrame, current_threshold: float,
                      rule_id: str = "") -> dict:
    """One rule's suggestion from rows [METRIC_VALUE, RESOLUTION_KIND].

    Returns {ok, suggested, basis, noise_n, actioned_n}; ok=False with a
    basis explaining why when the evidence is too thin or not separable.

    ``rule_id`` selects the comparison direction (see LOWER_IS_WORSE). Omitting it
    keeps the higher-is-worse default, which is right for every other seeded rule.
    A NO_THRESHOLD_RULES rule always returns ok=False (it has no threshold to tune).
    """
    import math

    current = safe_float(current_threshold)
    rid = str(rule_id or "").strip().upper()
    inverse = rid in LOWER_IS_WORSE
    required = {"METRIC_VALUE", "RESOLUTION_KIND"}
    if rid in NO_THRESHOLD_RULES:
        # V162: nothing to tune -- report the resolution counts, never a threshold.
        n_noise = n_actioned = 0
        if metric_values is not None and "RESOLUTION_KIND" in getattr(metric_values, "columns", ()):
            kinds = metric_values["RESOLUTION_KIND"].astype(str).str.upper()
            n_noise, n_actioned = int((kinds == "NOISE").sum()), int((kinds == "ACTIONED").sum())
        return {"ok": False, "basis": NO_THRESHOLD_BASIS, "noise_n": n_noise, "actioned_n": n_actioned}
    if metric_values is None or metric_values.empty or not required.issubset(metric_values.columns):
        return {"ok": False, "basis": "No resolved events with metric values yet.",
                "noise_n": 0, "actioned_n": 0}
    frame = metric_values.copy()
    numeric = pd.to_numeric(frame["METRIC_VALUE"], errors="coerce")
    # A1: filter NON-FINITE only. The old `> 0` dropped 0 and negatives — which on an
    # inverse rule are the MOST actionable evidence there is (expires today / already
    # expired, contract already exhausted).
    # Preserve real zero/negative evidence, but reject null, non-numeric, and
    # infinite values before coercion. safe_float() intentionally maps all of
    # those values to 0, which would turn corrupt rows into genuine evidence.
    valid = numeric.notna() & numeric.map(lambda value: math.isfinite(float(value)))
    frame = frame.loc[valid].copy()
    frame["METRIC_VALUE"] = numeric.loc[valid].astype(float)
    if frame.empty:
        return {"ok": False, "basis": "No valid resolved metric values yet.",
                "noise_n": 0, "actioned_n": 0}
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
        # "tagged resolutions with a metric value": the rows here are only the ACTIONED/NOISE closes
        # that carry a METRIC_VALUE -- untagged, EXPECTED and NULL-metric closes are not counted, so
        # "All N resolved events" overstated the evidence (R1-123).
        return {"ok": True, "suggested": suggested, "noise_n": n_noise, "actioned_n": 0,
                "basis": f"All {n_noise} tagged resolutions with a metric value were noise; "
                         f"{suggested} {tail}. If it keeps firing, consider disabling the rule."}

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
    RESOLUTION_KIND] + {rule_id: current_threshold} -> one row per rule.

    D7 / R1-123: mart_sql.rule_metric_kinds also carries UNTAGGED_N per rule (closes with no
    resolution kind, constant within a rule). When present it rides through as a column, and a
    suggestion whose untagged closes outnumber its tagged ones says so in its BASIS -- 5 tagged and
    300 untagged closes must not read as authoritative as 305 tagged ones. The math is unchanged."""
    if events is None or events.empty:
        return pd.DataFrame()
    has_untagged = "UNTAGGED_N" in events.columns
    rows = []
    for rule_id, block in events.groupby(events["RULE_ID"].astype(str)):
        # A1: rule_id selects the comparison direction — an inverse-metric rule
        # (SEC_CRED_EXPIRY, COST_CONTRACT_BREACH) must be tuned DOWNWARD.
        result = suggest_threshold(block, safe_float(thresholds.get(rule_id, 0.0)), rule_id)
        noise_n, actioned_n = result.get("noise_n", 0), result.get("actioned_n", 0)
        basis = str(result.get("basis", ""))
        row = {
            "RULE_ID": rule_id,
            "CURRENT_THRESHOLD": safe_float(thresholds.get(rule_id, 0.0)),
            "SUGGESTED_THRESHOLD": result.get("suggested"),
            "NOISE_N": noise_n,
            "ACTIONED_N": actioned_n,
            "BASIS": basis,
        }
        if has_untagged:
            untagged = int(safe_float(block["UNTAGGED_N"].iloc[0]))
            row["UNTAGGED_N"] = untagged
            if result.get("ok") and untagged > noise_n + actioned_n:
                row["BASIS"] = (f"{basis} Caveat: {untagged} more closed untagged; tag them "
                                "before trusting this.")
        rows.append(row)
    return pd.DataFrame(rows).sort_values("NOISE_N", ascending=False).reset_index(drop=True)
