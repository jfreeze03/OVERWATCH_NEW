"""Prove-it scorecard — does OVERWATCH work, and does it pay for itself?

The owner's gate before autonomy: hard numbers that the advising features are correct
and valuable. Nearly every input already exists as a pure function or mart builder
scattered across four pages (savings realization in Decision Studio ▸ ROI, remediation
acceptance in Admin, per-rule alert precision in Alerts, evidence coverage in DS
Portfolio). This module adds only the three aggregates none of them provided — an
account-wide alert precision roll-up, an action-acceptance rate, and the ROI multiple —
and a one-line verdict that composes the five proof signals. Pure pandas; no Streamlit,
no Snowflake. Tested in tests/test_proof.py.

A "None" ratio means NO DATA (nothing decided/resolved yet), never 0% — an honest blank
so an empty ledger doesn't read as "0% precise".
"""

from __future__ import annotations

import pandas as pd

from app.logic.formulas import safe_float


def account_precision(rule_precision_df: pd.DataFrame | None) -> dict:
    """Roll per-rule precision up to ONE fleet number: when any rule fires, how often
    is it real. ``rule_precision_df`` is mart_sql.rule_precision output (ACTIONED, NOISE,
    EXPECTED, UNTAGGED, RESOLVED_EVENTS). Precision = ACTIONED/(ACTIONED+NOISE), EXPECTED
    excluded from the denominator (a known-maintenance close is neither a hit nor noise).
    UNTAGGED_SHARE is the trust caveat — a high untagged share means the number isn't yet
    reliable (events closed without a kind)."""
    empty = {"PRECISION_PCT": None, "ACTIONED": 0, "NOISE": 0, "EXPECTED": 0,
             "UNTAGGED": 0, "RESOLVED": 0, "UNTAGGED_SHARE_PCT": 0.0, "RULES": 0}
    if rule_precision_df is None or rule_precision_df.empty:
        return empty

    def _col(name: str) -> float:
        if name not in rule_precision_df.columns:
            return 0.0
        return float(pd.to_numeric(rule_precision_df[name], errors="coerce").fillna(0).sum())

    actioned, noise = _col("ACTIONED"), _col("NOISE")
    expected, untagged = _col("EXPECTED"), _col("UNTAGGED")
    resolved = _col("RESOLVED_EVENTS") or (actioned + noise + expected + untagged)
    denom = actioned + noise
    return {
        "PRECISION_PCT": round(100.0 * actioned / denom, 1) if denom > 0 else None,
        "ACTIONED": int(actioned), "NOISE": int(noise), "EXPECTED": int(expected),
        "UNTAGGED": int(untagged), "RESOLVED": int(resolved),
        "UNTAGGED_SHARE_PCT": round(100.0 * untagged / resolved, 1) if resolved > 0 else 0.0,
        "RULES": len(rule_precision_df),
    }


def acceptance_summary(row: pd.DataFrame | pd.Series | dict | None) -> dict:
    """Of the recommendations the team DECIDED on, how many did they act on vs dismiss.
    ``row`` is the one-row mart_sql.action_acceptance output (DONE_N, DROPPED_N, OPEN_N,
    DONE_USD). Acceptance = DONE/(DONE+DROPPED); OPEN is the still-undecided backlog (not
    counted for/against). None acceptance when nothing has been decided yet."""
    if isinstance(row, pd.DataFrame):
        row = row.iloc[0].to_dict() if not row.empty else {}
    elif isinstance(row, pd.Series):
        row = row.to_dict()
    row = row or {}
    done = int(safe_float(row.get("DONE_N")))
    dropped = int(safe_float(row.get("DROPPED_N")))
    decided = done + dropped
    return {
        "DONE_N": done, "DROPPED_N": dropped, "OPEN_N": int(safe_float(row.get("OPEN_N"))),
        "DECIDED": decided,
        "ACCEPTANCE_PCT": round(100.0 * done / decided, 1) if decided > 0 else None,
        "DONE_USD": round(safe_float(row.get("DONE_USD")), 2),
    }


def roi_multiple(verified_usd: float, run_cost_usd: float) -> dict:
    """Does OVERWATCH pay for itself: verified savings as a multiple of its own run cost.
    RATIO None when run cost is unknown/zero (can't divide). PAYS = ratio >= 1."""
    verified = safe_float(verified_usd)
    run_cost = safe_float(run_cost_usd)
    ratio = round(verified / run_cost, 1) if run_cost > 0 else None
    return {
        "VERIFIED_USD": round(verified, 2), "RUN_COST_USD": round(run_cost, 2),
        "NET_USD": round(verified - run_cost, 2),
        "RATIO": ratio, "PAYS": bool(ratio is not None and ratio >= 1.0),
    }


# Trust bands for the headline verdict (uncalibrated starting points).
_MIN_PRECISION = 70.0        # below this, alerts are crying wolf too often
_MIN_REALIZATION = 60.0      # below this, estimates are not holding up
_MIN_ACCEPTANCE = 40.0       # below this, the team is ignoring the advice
_MAX_UNTAGGED_SHARE = 40.0   # above this, precision isn't trustworthy yet


def proof_verdict(roi: dict, realization_pct: float | None, acceptance_pct: float | None,
                  precision: dict) -> dict:
    """One-line headline: is OVERWATCH earning its keep? Composes the five signals into a
    level (good | watch | unproven) and the worst-first reasons. 'unproven' means not
    enough labeled evidence yet (the honest state early on), not failure."""
    reasons: list[str] = []
    level = "good"

    # Unproven: the tool can't be judged without labeled outcomes.
    if roi.get("RATIO") is None and realization_pct is None and precision.get("PRECISION_PCT") is None:
        return {"level": "unproven",
                "headline": "Not enough verified outcomes yet to prove value — "
                            "resolve alerts with a kind and verify savings to build the record.",
                "reasons": []}

    if roi.get("RATIO") is not None and not roi.get("PAYS"):
        reasons.append(f"run cost not yet covered ({roi['RATIO']:.1f}x)")
        level = "watch"
    if realization_pct is not None and realization_pct < _MIN_REALIZATION:
        reasons.append(f"low realization ({realization_pct:.0f}%)")
        level = "watch"
    prec = precision.get("PRECISION_PCT")
    if prec is not None and prec < _MIN_PRECISION:
        reasons.append(f"alert precision {prec:.0f}%")
        level = "watch"
    if acceptance_pct is not None and acceptance_pct < _MIN_ACCEPTANCE:
        reasons.append(f"team acts on only {acceptance_pct:.0f}%")
        level = "watch"
    if precision.get("UNTAGGED_SHARE_PCT", 0) > _MAX_UNTAGGED_SHARE:
        reasons.append(f"{precision['UNTAGGED_SHARE_PCT']:.0f}% of alerts unlabeled — precision not yet trustworthy")

    if level == "good":
        bits = []
        if roi.get("RATIO") is not None:
            bits.append(f"pays for itself {roi['RATIO']:.1f}x")
        if prec is not None:
            bits.append(f"{prec:.0f}% alert precision")
        if realization_pct is not None:
            bits.append(f"{realization_pct:.0f}% realization")
        headline = "OVERWATCH is earning its keep" + (" — " + ", ".join(bits) if bits else "") + "."
    else:
        headline = "OVERWATCH is providing value, but watch: " + "; ".join(reasons) + "."
    return {"level": level, "headline": headline, "reasons": reasons}
