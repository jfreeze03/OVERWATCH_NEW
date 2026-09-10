"""ETL reconciliation-recurrence classifier (insights.recon_recurrence).

Covers the tier ladder (CHRONIC / NEW / INTERMITTENT / RESOLVED), the LOW_CONFIDENCE
thin-denominator gate, the RANK_SCORE ordering, RESOLVED demotion, and empty-in.
"""

from __future__ import annotations

import pandas as pd

from app.logic.insights import recon_recurrence


def _row(mtrc: str, *, broken: int, total: int, pct: float, latest: bool,
         recent: int = 1, frqcy: str = "DAILY") -> dict:
    return {
        "MTRC": mtrc, "FRQCY": frqcy, "VALUE_TYPE": "AMT", "RECON_MTRC_LAYER": "EDW",
        "BROKEN_CYCLES": broken, "TOTAL_ERROR_CYCLES": total, "RECURRENCE_PCT": pct,
        "RECENT_BROKEN": recent, "RECENT_WINDOW": 5, "BROKE_LATEST_CYCLE": latest,
        "FIRST_BROKEN_ON": "2026-08-01", "LAST_BROKEN_ON": "2026-09-01", "ERROR_ROWS": broken,
        "SOURCE_LAYER": "LDW", "TARGET_LAYER": "EDW", "SOURCE_ERROR": "x", "TARGET_ERROR": "y",
    }


def test_chronic_when_recurs_often_with_enough_history() -> None:
    out = recon_recurrence(pd.DataFrame([_row("M_A", broken=8, total=10, pct=80, latest=True)]))
    r = out.iloc[0]
    assert r["TIER"] == "CHRONIC"
    assert r["SEVERITY"] == "High"
    assert not bool(r["LOW_CONFIDENCE"])


def test_thin_denominator_is_not_chronic() -> None:
    # 1/1 = 100% is arithmetically chronic but operationally a one-off -> NOT chronic
    out = recon_recurrence(pd.DataFrame([_row("M_B", broken=1, total=1, pct=100, latest=True)]))
    r = out.iloc[0]
    assert bool(r["LOW_CONFIDENCE"])
    assert r["TIER"] != "CHRONIC"
    assert r["TIER"] == "NEW"          # broke in only the last cycle -> a fresh regression


def test_resolved_when_not_broken_in_latest_cycle() -> None:
    # broke chronically earlier but not in the latest cycle -> demoted, not top of the "likely next" list
    out = recon_recurrence(pd.DataFrame([_row("M_C", broken=9, total=10, pct=90, latest=False)]))
    r = out.iloc[0]
    assert r["TIER"] == "RESOLVED"
    assert r["SEVERITY"] == "Low"


def test_intermittent_flapping() -> None:
    out = recon_recurrence(pd.DataFrame([_row("M_D", broken=4, total=10, pct=40, latest=True)]))
    assert out.iloc[0]["TIER"] == "INTERMITTENT"


def test_new_tier_requires_recency() -> None:
    # broke twice — once long ago and once on the latest cycle (RECENT_BROKEN=1 < BROKEN_CYCLES=2):
    # a low break count alone must NOT read as a fresh regression; recency gates it to INTERMITTENT.
    out = recon_recurrence(pd.DataFrame([_row("M_OLD", broken=2, total=8, pct=25, latest=True, recent=1)]))
    assert out.iloc[0]["TIER"] == "INTERMITTENT"


def test_ranking_active_chronic_above_resolved() -> None:
    rows = [
        _row("M_RESOLVED", broken=9, total=10, pct=90, latest=False),   # RESOLVED, sinks
        _row("M_CHRONIC", broken=8, total=10, pct=80, latest=True, recent=5),  # active chronic, tops
    ]
    out = recon_recurrence(pd.DataFrame(rows))
    assert list(out["MTRC"]) == ["M_CHRONIC", "M_RESOLVED"]
    assert out.iloc[0]["RANK_SCORE"] > out.iloc[1]["RANK_SCORE"]


def test_severity_leads_ranking_over_rank_score() -> None:
    # a High CHRONIC (lower RANK_SCORE) must outrank a Medium NEW (higher RANK_SCORE) as the worst row
    rows = [
        _row("M_NEW", broken=2, total=4, pct=50, latest=True, recent=2),      # NEW, Medium, higher score
        _row("M_CHRONIC", broken=6, total=9, pct=67, latest=True, recent=1),  # CHRONIC, High, lower score
    ]
    out = recon_recurrence(pd.DataFrame(rows))
    assert out.iloc[0]["MTRC"] == "M_CHRONIC"
    assert out.iloc[0]["SEVERITY"] == "High"
    # confirm the Medium NEW really has the higher RANK_SCORE, so severity (not score) drove the order
    _new = out[out["MTRC"] == "M_NEW"].iloc[0]
    _chr = out[out["MTRC"] == "M_CHRONIC"].iloc[0]
    assert _new["RANK_SCORE"] > _chr["RANK_SCORE"]


def test_empty_and_malformed_in_empty_out() -> None:
    assert recon_recurrence(pd.DataFrame()).empty
    assert recon_recurrence(pd.DataFrame({"X": [1]})).empty
    assert recon_recurrence(None).empty  # type: ignore[arg-type]
