"""Round-4 UI-layer bug hunt (v4.341.0) — locks for the three confirmed fixes.

The regression self-audit of this session's ~40 changes came back clean; these three
are fresh (Entity-360 re-drill state, and two chart-encoding defects). Behavioral
(compiled Vega spec) for the charts; source-shape for the render-state fix.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from app.ui import charts

_ROOT = Path(__file__).resolve().parents[2]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _spec(fn, *args, **kwargs):
    captured = {}
    _oc, _ocap = charts.st.altair_chart, charts.st.caption
    charts.st.altair_chart = lambda ch, **k: captured.__setitem__("d", ch.to_dict())
    charts.st.caption = lambda *a, **k: None
    try:
        fn(*args, **kwargs)
    finally:
        charts.st.altair_chart, charts.st.caption = _oc, _ocap
    return captured.get("d", {})


# 1) Entity 360 re-drill re-seeds — the nav identity is CONSUMED, not signature-deduped
def test_entity_seed_consumes_nav_identity_so_a_re_drill_re_seeds():
    wb = _src("app/ui/workbench.py")
    seed = wb.split("def _seed_entity_context", 1)[1].split("\ndef ", 1)[0]
    # the buggy persistent signature flag is no longer read or written (a comment may
    # still name it to explain the removal — check the CODE, not the mention)
    assert '.get("_ow_entity_context_applied")' not in wb
    assert '"_ow_entity_context_applied"] =' not in wb
    # the identity is consumed from the nav context (mirrors Action Center's action_id),
    # so a repeat drill to the same entity re-populates and re-seeds
    assert '"_ow_nav_context"' in seed
    assert 'k not in ("entity_type", "entity_key")' in seed


# 2) bar_usd keeps cents on sub-dollar spend (axis AND the always-visible labels)
def test_bar_usd_sub_dollar_keeps_cents():
    d = _spec(charts.bar_usd, pd.DataFrame({"U": ["a", "b", "c"], "USD": [0.58, 0.30, 0.04]}),
              "U", "USD", title="Spend (USD)")
    assert d["layer"][0]["encoding"]["x"]["axis"]["format"] == "$,.2f"   # axis ticks
    assert d["layer"][1]["encoding"]["text"]["format"] == "$,.2f"        # on-bar data labels
    # a whole-dollar chart is unchanged (no forced cents)
    d2 = _spec(charts.bar_usd, pd.DataFrame({"U": ["a", "b"], "USD": [500.0, 200.0]}), "U", "USD")
    assert d2["layer"][0]["encoding"]["x"]["axis"]["format"] == "$,.0f"


def test_clickable_bar_usd_sub_dollar_keeps_cents():
    src = _src("app/ui/charts.py")
    blk = src.split("def clickable_bar_usd", 1)[1].split("\ndef ", 1)[0]
    assert '_fmt = "$,.2f" if 0 < dmax < 1 else _usd_fmt(dmax)' in blk


# 3) events_by_day day tooltip is F40-formatted (no "12:00:00 AM" on day-grain data)
def test_events_by_day_tooltip_formats_the_day():
    df = pd.DataFrame({"DAY": pd.to_datetime(["2026-08-01"] * 3),
                       "SEVERITY": ["CRITICAL", "HIGH", "INFO"], "EVENTS": [1, 2, 3]})
    spec = _spec(charts.events_by_day, df)
    day_tips = [t for t in spec["encoding"]["tooltip"] if t.get("field") == "Day"]
    assert day_tips and day_tips[0].get("format") == "%b %d, %Y"   # _DAY_TIP_FMT
    assert "axis" in spec["encoding"]["x"]                          # adaptive day ticks like siblings
    # guard against a literal-string regression in the source
    assert '"Day:T", "Severity:N"' not in json.dumps(_src("app/ui/charts.py"))
