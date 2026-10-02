"""v4.606.0 holistic review (b): Brief's 'Fires' row click re-arms after Back (the R1-215 class).

The Fires table opens an alert's drawer through a hand-rolled change sentinel. It never re-armed: the drawer
navigates away, the table returns unselected, and the next click on the same row matched the stale sentinel
-- nothing opened. Rendered through the shaped AppTest harness (its autouse stub fixture is imported below),
with the table's emitted selection driven run by run; fails on the pre-fix tree (b13e87f0).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

st = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from app.core.result import QueryResult  # noqa: E402
from tests.test_pages_shaped import (  # noqa: E402, F401  (_stub_shaped: the harness's autouse stub fixture)
    _entry,
    _nav_to,
    _shaped_batch,
    _shaped_run,
    _stub_shaped,
)

_ROOT = Path(__file__).resolve().parents[1]


def _fires() -> QueryResult:
    return QueryResult(ok=True, source="ALERT_EVENTS", df=pd.DataFrame({
        "EVENT_ID": ["E0", "E1"], "RULE_ID": ["R", "R"],
        "RAISED_AT": pd.to_datetime(["2026-09-30 08:00", "2026-09-30 07:00"]), "COMPANY": ["ALL", "ALL"],
        "SEVERITY": ["CRITICAL", "HIGH"], "TITLE": ["Fire zero", "Fire one"], "DETAIL": ["", ""],
        "METRIC_VALUE": [1.0, 1.0], "STATUS": ["OPEN", "OPEN"], "ACK_BY": [None, None], "ACK_AT": [None, None]}))


def test_fires_row_reclick_after_back_opens_the_drawer_again(monkeypatch):
    from app.ui.pages import brief

    cell: dict = {"sel": None}
    opened: list = []

    def _batch(specs, **k):
        out = _shaped_batch(specs, **k)
        for s in specs:
            if str(s.get("key", "")) == "events" or str(s.get("key", "")).startswith("brief_events_"):
                out[s["key"]] = _fires()
        return out

    def _run(*a, **k):
        return _fires() if str(k.get("key", "")).startswith("brief_events_") else _shaped_run(*a, **k)

    def _table(_df, key="", **_k):
        return cell["sel"] if key == "brief_fires_sel" else None

    def _nav(page, section=None, context=None, **_k):
        opened.append((page, section, (context or {}).get("event_id")))

    monkeypatch.setattr(brief, "run_batch", _batch)
    monkeypatch.setattr(brief, "run", _run)
    monkeypatch.setattr(brief, "selectable_table", _table)
    monkeypatch.setattr(brief, "request_navigation", _nav)
    at = AppTest.from_function(_entry, default_timeout=60)
    at.run()
    assert not at.exception
    _nav_to(at, "Brief")
    at.run()
    assert not at.exception, at.exception
    assert opened == []
    cell["sel"] = 0                     # click the first fire -> its drawer opens
    at.run()
    assert opened == [("Alerts", "Open events", "E0")]
    at.run()                            # the sticky selection re-emits: no second open
    assert len(opened) == 1
    cell["sel"] = None                  # Back: the table remounts unselected
    at.run()
    cell["sel"] = 0                     # click the same fire again -> it must open again
    at.run()
    assert not at.exception, at.exception
    assert opened == [("Alerts", "Open events", "E0")] * 2     # was one: the re-click was swallowed


def test_fires_sentinel_re_arms_before_the_change_check():
    """Source-order lock: the re-arm sits before the change check (the AppTest above proves the behaviour)."""
    src = (_ROOT / "app" / "ui" / "pages" / "brief.py").read_text(encoding="utf-8")
    rearm = src.index('if _fire_sel is None:\n                # R1-215 re-arm')
    pop = src.index('st.session_state.pop("_brief_fire_sel_last", None)')
    check = src.index('if _fire_sel is not None and _fire_sel != st.session_state.get("_brief_fire_sel_last"):')
    assert rearm < pop < check
