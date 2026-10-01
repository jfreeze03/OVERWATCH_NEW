"""R1-088: Proof ▸ Pipeline discloses the priced open items its confidence floor leaves out.

scenario_projection reads a NULL CONFIDENCE as 0, so a priced AI-exception or triage item (which carries no
authored confidence) never clears any floor above 0. The floor itself is policy and stays -- an AI exception's
estimate is projected spend, not a saving -- but the page showed "Queued work $/mo $3,000.00" beside
"Eligible entities 0" and "In play: No evidence" with nothing saying an item was left out. Now the exclusion
is counted (NULL apart from an authored low confidence), named in a caption, and the empty KPIs read
"Below floor" when priced items exist under the floor.
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.logic.decision import floor_exclusions, scenario_projection
from app.ui import decision_studio as ds


def _rows(*rows: dict) -> pd.DataFrame:
    base = {"STATUS": "OPEN", "PERIOD": "MONTHLY", "SOURCE_ENTITY_TYPE": "WAREHOUSE"}
    return pd.DataFrame([{**base, **r} for r in rows])


def test_a_priced_null_confidence_item_is_excluded_and_counted_apart():
    frame = _rows({"ACTION_ID": "ai", "SOURCE_ENTITY_TYPE": "USER", "SOURCE_ENTITY_KEY": "BOB",
                   "CONFIDENCE": None, "ESTIMATED_USD": 3000.0},
                  {"ACTION_ID": "low", "SOURCE_ENTITY_KEY": "WH_LOW", "CONFIDENCE": 0.3, "ESTIMATED_USD": 40.0},
                  {"ACTION_ID": "low2", "SOURCE_ENTITY_KEY": "WH_LOW", "CONFIDENCE": 0.2, "ESTIMATED_USD": 25.0},
                  {"ACTION_ID": "ok", "SOURCE_ENTITY_KEY": "WH_OK", "CONFIDENCE": 0.8, "ESTIMATED_USD": 100.0},
                  {"ACTION_ID": "ok_low", "SOURCE_ENTITY_KEY": "WH_OK", "CONFIDENCE": 0.1, "ESTIMATED_USD": 500.0},
                  {"ACTION_ID": "unpriced", "SOURCE_ENTITY_KEY": "WH_U", "CONFIDENCE": None, "ESTIMATED_USD": None},
                  {"ACTION_ID": "done", "SOURCE_ENTITY_KEY": "WH_D", "STATUS": "DONE", "CONFIDENCE": None,
                   "ESTIMATED_USD": 900.0})
    proj = scenario_projection(frame, adoption_pct=100, realization_pct=100, confidence_floor=0.6)
    assert proj["candidates"] == 1.0                     # the floor keeps its meaning: NULL never passes it
    ex = floor_exclusions(frame, confidence_floor=0.6)
    # NULL confidence apart from an authored low one; one per entity (largest wins); an entity the projection
    # already counts (WH_OK), an unpriced row and a closed row are never "excluded"
    assert ex == {"no_conf_count": 1.0, "no_conf_usd": 3000.0, "below_floor_count": 1.0, "below_floor_usd": 40.0}
    assert floor_exclusions(frame, confidence_floor=0.0)["no_conf_count"] == 0.0
    assert floor_exclusions(None, confidence_floor=0.6)["below_floor_count"] == 0.0


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _render(monkeypatch, frame: pd.DataFrame) -> tuple[dict, list[str]]:
    from types import SimpleNamespace
    caps: list[str] = []
    kpis: list = []
    state: dict = {}
    fake = SimpleNamespace(session_state=state, columns=lambda n: [_Ctx() for _ in range(n)],
                           slider=lambda _l, *_a, key="", **_k: state[key], button=lambda *_a, **_k: False,
                           caption=lambda t, *_a, **_k: caps.append(str(t)))
    monkeypatch.setattr(ds, "st", fake)
    monkeypatch.setattr(ds, "kpi_row", lambda items, *_a, **_k: kpis.append(items))
    defaults = {"adoption": 60, "adoption_help": "", "realization": 70, "realization_help": "",
                "conf_floor": 0.6, "conf_help": ""}
    ds._pipeline_projection.__wrapped__(frame, defaults)       # the fragment body, outside a script run
    (row,) = kpis
    return {k["label"]: k["value"] for k in row}, caps


def test_pipeline_says_below_floor_and_names_the_excluded_items(monkeypatch):
    frame = _rows({"ACTION_ID": "ai", "SOURCE_ENTITY_TYPE": "USER", "SOURCE_ENTITY_KEY": "BOB",
                   "CONFIDENCE": None, "ESTIMATED_USD": 3000.0})
    kpis, caps = _render(monkeypatch, frame)
    assert kpis["Eligible entities"] == "0"
    assert kpis["In play $/mo"] == "Below floor" and kpis["Expected capture $/mo"] == "Below floor"
    (cap,) = caps
    assert "1 priced open item(s)" in cap and "3,000.00/mo" in cap and "no authored confidence" in cap


@pytest.mark.parametrize("frame", [pd.DataFrame(), _rows({"ACTION_ID": "u", "SOURCE_ENTITY_KEY": "W",
                                                          "CONFIDENCE": None, "ESTIMATED_USD": None})])
def test_nothing_priced_under_the_floor_still_reads_no_evidence(monkeypatch, frame):
    kpis, caps = _render(monkeypatch, frame)
    assert kpis["In play $/mo"] == "No evidence" and caps == []
