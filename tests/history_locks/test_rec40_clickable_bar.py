"""rec40 clickable_bar_usd — unit coverage for the selection-extraction, the
NEW-click guard, and the on_select degrade path.

The only production caller (overview.py Top-cost-drivers) is gated behind a
non-empty board, which the AppTest smoke never provides, so without this file the
most logic-heavy new helper ships untested. Here we drive it directly with a
stubbed ``st`` so a regression in the version-defensive selection parse, the
sticky-guard, or the degrade branch is caught by the gating suite.
"""
from __future__ import annotations

import types

import pandas as pd

from app.ui import charts


def _df() -> pd.DataFrame:
    return pd.DataFrame({"DIMENSION": ["WH_ETL", "WH_BI"], "VALUE_USD": [900.0, 100.0]})


def _stub_st(event: object, session: dict, *, raise_on_select: bool = False) -> object:
    """A minimal stand-in for ``charts.st``: only altair_chart + session_state
    are exercised by clickable_bar_usd. When ``raise_on_select`` is set, the
    on_select call raises (a runtime without altair on_select) so the degrade
    branch's second, plain altair_chart call is what returns."""
    def altair_chart(_chart: object, **kwargs: object) -> object:
        if raise_on_select and "on_select" in kwargs:
            raise TypeError("on_select unsupported on this runtime")
        return event
    return types.SimpleNamespace(altair_chart=altair_chart, session_state=session)


def _call(monkeypatch, event, session, *, raise_on_select=False, key="t"):
    monkeypatch.setattr(charts, "st", _stub_st(event, session, raise_on_select=raise_on_select))
    return charts.clickable_bar_usd(_df(), "DIMENSION", "VALUE_USD", key=key)


def test_extracts_clicked_label_dict_shape(monkeypatch):
    # Streamlit's altair selection store: event.selection[param] = [{field: value}]
    session: dict = {}
    event = types.SimpleNamespace(selection={"pt": [{"Label": "WH_ETL"}]})
    assert _call(monkeypatch, event, session) == "WH_ETL"
    assert session["_ow_barsel_t"] == "WH_ETL"   # guard armed to the fired label


def test_extracts_clicked_label_object_and_scalar_shapes(monkeypatch):
    # selection carried as an attribute holding a list of scalars (defensive path)
    event = types.SimpleNamespace(selection=types.SimpleNamespace(pt=["WH_BI"]))
    assert _call(monkeypatch, event, {}) == "WH_BI"


def test_repeat_of_same_label_does_not_refire(monkeypatch):
    # sticky re-emit of the last fired label must return None (no bounce / no loop)
    session = {"_ow_barsel_t": "WH_ETL"}
    event = types.SimpleNamespace(selection={"pt": [{"Label": "WH_ETL"}]})
    assert _call(monkeypatch, event, session) is None


def test_empty_selection_rearms_guard(monkeypatch):
    # fresh render with no active selection (e.g. after returning to the page) pops
    # the guard so the NEXT click of the same bar counts as new, not a dead repeat
    session = {"_ow_barsel_t": "WH_ETL"}
    event = types.SimpleNamespace(selection={"pt": []})
    assert _call(monkeypatch, event, session) is None
    assert "_ow_barsel_t" not in session


def test_new_label_after_rearm_fires(monkeypatch):
    # end-to-end of the re-drill loop: same label fires again once the guard was
    # re-armed by an intervening empty render
    session: dict = {}
    fire = types.SimpleNamespace(selection={"pt": [{"Label": "WH_ETL"}]})
    empty = types.SimpleNamespace(selection={"pt": []})
    assert _call(monkeypatch, fire, session) == "WH_ETL"
    assert _call(monkeypatch, empty, session) is None       # returned to page, GC'd
    assert _call(monkeypatch, fire, session) == "WH_ETL"    # deliberate re-click fires


def test_degrades_to_plain_bar_when_on_select_unsupported(monkeypatch):
    # a runtime without altair on_select must not raise — it renders a plain,
    # non-clickable bar and returns None (the CORE value survives)
    session: dict = {}
    event = types.SimpleNamespace(selection={"pt": [{"Label": "WH_ETL"}]})
    assert _call(monkeypatch, event, session, raise_on_select=True) is None
    assert "_ow_barsel_t" not in session                    # nothing armed on the degrade path
