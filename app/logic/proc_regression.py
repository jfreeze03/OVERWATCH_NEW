"""Stored-procedure regression classifier (pure, tested, zero AI cost).

From one ``ops_sql.proc_regression`` result row — this window's success-only p95
vs the prior equal-length window — classify how a proc's runtime moved and score
it for ranking. Deterministic, no Cortex. Mirrors ``query_advisor.advise``'s
Mapping-in / structured-out contract and ``insights.takeover_severity``'s
annotate-a-frame shape.

The verdict vocabulary is chosen so the shared status palette colors it for free:
'Regressed' -> red and 'Improved' -> green via ``status_colors._VERDICTS``, and the
``P95_DELTA_PCT`` column sign-colors (up = worse = red) via ``is_delta_column``.
"""

from __future__ import annotations

from collections.abc import Mapping

import pandas as pd

from .formulas import safe_float

# A proc needs at least this many calls in BOTH windows before a p95 delta is
# trustworthy — a p95 over two or three calls is one slow run, not a trend. The
# SQL HAVING enforces the same floor; keep them in sync at the call site.
MIN_CALLS = 5

# p95-growth thresholds (percent change vs the prior window).
REGRESSED_PCT = 50.0    # p95 grew by at least this -> a real regression
SLOWER_PCT = 20.0       # p95 grew by at least this -> worth watching
IMPROVED_PCT = -20.0    # p95 shrank by at least this -> genuinely faster
# a fail-rate jump (percentage points) that turns a p95 "win" into a false one:
# a proc that now errors out fast looks faster but is actually broken.
FAIL_JUMP_PCT = 10.0

# severity vocabulary matches the app's SEVERITY column + STATUS_COLOR_MAP.
_SEV_RANK = {"High": 0, "Medium": 1, "Low": 2}


def _f(row: Mapping[str, object], col: str, default: float = 0.0) -> float:
    return safe_float(row.get(col), default)


def classify(row: Mapping[str, object]) -> tuple[str, str, int]:
    """Return (verdict, severity, score) for one proc_regression row.

    ``verdict`` is a human label ('Regressed' | 'Slower' | 'Stable' | 'Improved'
    | 'Faster but failing'); ``severity`` is 'High' | 'Medium' | 'Low'; ``score``
    is 0-100 for ranking within a severity band.
    """
    p95_delta = _f(row, "P95_DELTA_PCT")
    fail_jump = _f(row, "CUR_FAIL_PCT") - _f(row, "PRIOR_FAIL_PCT")
    failing = fail_jump >= FAIL_JUMP_PCT

    # A big slowdown is a regression on its own, whatever the failure rate did.
    if p95_delta >= REGRESSED_PCT:
        return ("Regressed", "High", int(min(100, 50 + p95_delta / 2)))
    # Slower AND failing more is strictly worse than either alone — escalate to
    # High rather than demote it to 'Slower' (which would drop the failure spike).
    if p95_delta > 0 and failing:
        return ("Regressed", "High", int(min(100, 50 + p95_delta / 2 + fail_jump / 2)))
    # p95 flat-or-down BUT failures spiked: 'faster' only because it now errors
    # out. Reached only when p95_delta <= 0, so the 'Faster' label is accurate.
    if failing:
        return ("Faster but failing", "High", int(min(100, 40 + fail_jump)))
    # A moderate slowdown with no failure spike.
    if p95_delta >= SLOWER_PCT:
        return ("Slower", "Medium", int(min(100, 20 + p95_delta)))
    if p95_delta <= IMPROVED_PCT:
        return ("Improved", "Low", 0)
    return ("Stable", "Low", 0)


def annotate(df: pd.DataFrame) -> pd.DataFrame:
    """Add VERDICT / SEVERITY / SCORE columns to a proc_regression frame, sorted
    worst-first (High severity, then biggest score). Empty-in -> empty-out; never
    raises."""
    if df is None or getattr(df, "empty", True):
        return pd.DataFrame() if df is None else df
    out = df.copy()
    verdicts, severities, scores = [], [], []
    for _, r in out.iterrows():
        verdict, severity, score = classify(r)
        verdicts.append(verdict)
        severities.append(severity)
        scores.append(score)
    out["VERDICT"] = verdicts
    out["SEVERITY"] = severities
    out["SCORE"] = scores
    out["_o"] = out["SEVERITY"].map(_SEV_RANK).fillna(3)
    out = (out.sort_values(["_o", "SCORE"], ascending=[True, False])
              .drop(columns="_o").reset_index(drop=True))
    return out
