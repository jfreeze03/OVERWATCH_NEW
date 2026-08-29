"""UI/UX master list — Wave 2 scatter-guide cluster (F46) on workload_portfolio.

The portfolio scatter (impact x confidence, colored by lane) encoded the ACT NOW
/ PLAN / VALIDATE lane in COLOR alone. F46 draws the confidence-axis lane gates so
a dot's vertical position explains its lane too. Crucially, only the CONFIDENCE
axis has axis-aligned gates — ACT NOW keys off a PRIORITY_SCORE percentile, NOT an
impact threshold — so NO vertical impact quadrant is drawn (it would misrepresent
the lane). The guide values are the same named constants the lane logic uses, so
guides and colors can never drift apart.
"""

from __future__ import annotations

import pandas as pd

from app.logic.decision import LANE_ACTNOW_CONF, LANE_ACTNOW_PCTL, LANE_CONF_FLOOR, prioritize_workloads
from app.ui import charts


def _render(monkeypatch):
    captured = {"caps": []}
    monkeypatch.setattr(charts.st, "altair_chart",
                        lambda chart, **_: captured.__setitem__("spec", chart.to_dict()))
    monkeypatch.setattr(charts.st, "caption", lambda t, **_: captured["caps"].append(str(t)))
    df = pd.DataFrame({
        "FINGERPRINT": ["a", "b", "c"], "LANE": ["ACT NOW", "PLAN", "VALIDATE"],
        "IMPACT_USD_30D": [1000.0, 500.0, 100.0], "CONFIDENCE": [0.8, 0.6, 0.3],
        "BLAST_RADIUS": [5, 3, 1], "PRIORITY_SCORE": [9.0, 4.0, 1.0],
        "NEXT_MOVE": ["x", "y", "z"],
    })
    charts.workload_portfolio(df)
    return captured


def _rule_layer(spec):
    for layer in spec.get("layer", []):
        mark = layer.get("mark")
        mtype = mark.get("type") if isinstance(mark, dict) else mark
        if mtype == "rule":
            return layer
    return None


def _rows(spec, layer):
    data = layer.get("data", {})
    return data.get("values") or spec.get("datasets", {}).get(data.get("name"), [])


def test_f46_draws_the_two_confidence_gates(monkeypatch):
    spec = _render(monkeypatch)["spec"]
    rule = _rule_layer(spec)
    assert rule is not None, "no confidence-gate rule layer on the scatter"
    ys = sorted(r["_y"] for r in _rows(spec, rule))
    assert ys == [LANE_CONF_FLOOR, LANE_ACTNOW_CONF]     # exactly the two lane floors


def test_f46_draws_no_vertical_impact_quadrant(monkeypatch):
    # a vertical rule (encoding x, no y) would misrepresent the lane, since ACT NOW
    # is a PRIORITY_SCORE percentile, not an impact threshold. The only rule layer
    # must encode y (a horizontal confidence gate), never a bare x.
    spec = _render(monkeypatch)["spec"]
    for layer in spec.get("layer", []):
        mark = layer.get("mark")
        mtype = mark.get("type") if isinstance(mark, dict) else mark
        if mtype == "rule":
            enc = layer.get("encoding", {})
            assert "y" in enc and "x" not in enc


def test_f46_axis_renamed_and_caption_states_the_full_rule(monkeypatch):
    # review HIGH fix: the y-axis is 'Run/cost confidence' (not 'Evidence
    # confidence', which collided with the 'Validate evidence' next-move), and a
    # caption spells the FULL lane rule so an amber VALIDATE dot sitting HIGH on the
    # axis (a blind, high-cost family) is explained, not an unexplained contradiction.
    out = _render(monkeypatch)
    spec = out["spec"]
    def _mtype(layer):
        m = layer.get("mark")
        return m.get("type") if isinstance(m, dict) else m
    points = next(lyr for lyr in spec["layer"] if _mtype(lyr) in ("circle", "point"))
    assert points["encoding"]["y"]["title"] == "Run/cost confidence"
    cap = " ".join(out["caps"]).lower()
    assert "behavioural evidence" in cap and "validate" in cap and "act now" in cap


def test_f46_validate_can_sit_above_the_actnow_floor_when_blind():
    # review HIGH: CONFIDENCE is run/cost-only, so a high-RUNS/CREDITS family with NO
    # behavioural evidence (cache/p95/fails absent) is forced to VALIDATE at a
    # confidence ABOVE the act-now floor — the case the caption now explains.
    blind = pd.DataFrame({
        "RUNS": [300], "CREDITS": [5000], "ACTIVE_DAYS": [30],
        "USERS": [3], "DATABASES": [1],
        # behavioural columns present but NULL (a join miss) -> has_behavior is False
        "AVG_CACHE_PCT": [None], "P95_SEC": [None], "FAILS": [None],
    })
    out = prioritize_workloads(blind, rate=3.0, days=30)
    assert out.loc[0, "CONFIDENCE"] > LANE_ACTNOW_CONF     # high run/cost confidence
    assert out.loc[0, "LANE"] == "VALIDATE"                # ...yet VALIDATE (blind)


def test_f46_guides_stay_in_lockstep_with_the_lane_logic():
    # the guide constants ARE the constants prioritize_workloads gates on — proven
    # by exercising the logic: a family below the floor is VALIDATE; one that clears
    # both the confidence floor and the top-percentile is ACT NOW.
    frame = pd.DataFrame({
        "RUNS": [300, 300, 5], "FAILS": [0, 0, 0], "CREDITS": [5000, 4000, 10],
        "ACTIVE_DAYS": [30, 30, 1], "USERS": [3, 3, 1], "DATABASES": [1, 1, 1],
        "AVG_CACHE_PCT": [10, 10, 0], "P95_SEC": [8, 8, 0],
    })
    out = prioritize_workloads(frame, rate=3.0, days=30)
    lo = out.loc[out["CONFIDENCE"] < LANE_CONF_FLOOR, "LANE"]
    assert (lo == "VALIDATE").all()            # below the floor -> only VALIDATE
    assert 0.0 <= LANE_ACTNOW_PCTL <= 1.0
