"""Control Room review round 1 (cluster c06) on the rendered page (AppTest over the shaped harness, both
CI legs -- the logic locks are in tests/test_control_room_review_r1.py).

R1-201  Pulse under a Schema filter rendered the page-error panel (`act` is None, `act.ok` raised).
R1-202  yesterday's spend spike reaches the triage queue even when 10+ stronger historical spikes exist.
R1-205  a failed triage source is disclosed under a NON-empty queue too, by its kind, and the caption
        names only the sources that loaded.
R1-212  a full proposals page reads "20+", and a capped alert feed says it is capped.
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import pytest
from test_pages_shaped import (  # noqa: F401 - _stub_shaped is the harness's autouse fixture
    _entry,
    _nav_to,
    _shaped_batch,
    _shaped_run,
    _stub_shaped,
)

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from app.core.result import QueryResult
from app.logic.formulas import account_today


def _blob(at) -> str:
    return " ".join(str(m.value) for m in list(at.markdown) + list(at.caption) + list(at.warning)
                    + list(at.info) + list(at.error))


def _section(section: str, **state) -> AppTest:
    at = AppTest.from_function(_entry, default_timeout=60)
    at.run()
    assert not at.exception
    _nav_to(at, "Control Room")
    at.session_state["cr_section"] = section
    for k, v in state.items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, f"{section}: {at.exception}"
    assert not any("could not finish rendering" in str(getattr(e, "value", "")) for e in at.error), \
        f"{section} raised mid-render: {[str(e.value) for e in at.error]}"
    return at


def test_pulse_renders_under_a_schema_filter():
    at = _section("Pulse", flt_schema_contains="RAW")
    assert "14-day trend hidden under a Schema filter" in _blob(at)


def _spike_frame() -> pd.DataFrame:
    """FACT_WAREHOUSE_DAILY rows: 12 near-flat warehouses with one huge OLD spike each, and WH_PROD
    (noisy) spiking yesterday -- the 12 old ones out-|z| it, so a cap-then-filter drops it."""
    today = account_today()
    rows = []
    for w in range(12):
        old = today - timedelta(days=25 - (w % 5))
        for d in range(30, 0, -1):
            day = today - timedelta(days=d)
            rows.append({"WAREHOUSE_NAME": f"WH_OLD_{w:02d}", "DAY": day,
                         "CREDITS_TOTAL": 300.0 + ((d % 5) - 2) * 0.3 + (1200.0 if day == old else 0.0)})
    rows.extend({"WAREHOUSE_NAME": "WH_PROD", "DAY": today - timedelta(days=d),
                 "CREDITS_TOTAL": 900.0 + ((d % 5) - 2) * 30.0 + (900.0 if d == 1 else 0.0)}
                for d in range(30, 0, -1))
    return pd.DataFrame(rows)


def _triage_page(monkeypatch, *, batch_override=None, run_override=None) -> AppTest:
    from app.config import DEFAULT_SETTINGS
    from app.ui.pages import control_room

    settings = dict(DEFAULT_SETTINGS)
    settings["EXPECTED_SPIKE_CALENDAR"] = ""      # no month-end suppression: date-independent
    monkeypatch.setattr(control_room, "load_settings", lambda _p: dict(settings))

    def _run(*args, **kwargs):
        key = str(kwargs.get("key") or "")
        if run_override is not None:
            hit = run_override(key)
            if hit is not None:
                return hit
        if key.startswith("cr_wh_"):
            return QueryResult(df=_spike_frame(), ok=True, source="FACT_WAREHOUSE_DAILY")
        return _shaped_run(*args, **kwargs)

    def _batch(specs, **kwargs):
        out = _shaped_batch(specs, **kwargs)
        if batch_override is not None:
            out.update(batch_override(specs))
        return out

    monkeypatch.setattr(control_room, "run", _run)
    monkeypatch.setattr(control_room, "run_batch", _batch)
    return _section("Incidents & triage", _ow_current_role="SNOW_SYSADMINS")


def _queue_frame(at) -> pd.DataFrame:
    frames = [d.value for d in at.dataframe if {"KIND", "TITLE"} <= set(getattr(d.value, "columns", []))]
    assert frames, "the triage queue table did not render"
    return frames[0]


def test_yesterdays_spike_reaches_the_triage_queue(monkeypatch):
    at = _triage_page(monkeypatch)
    q = _queue_frame(at)
    spend = q[q["KIND"].isin(["Spend anomaly", "Spend collapse"])]
    assert len(spend) == 1 and "WH_PROD" in str(spend.iloc[0]["TITLE"]), q[["KIND", "TITLE"]]


def test_a_failed_alert_read_is_disclosed_under_a_non_empty_queue(monkeypatch):
    failed = QueryResult(ok=False, error="statement timed out", error_kind="timeout")

    def _cra_fails(specs):
        return {s["key"]: failed for s in specs if s.get("key") == "cra"}

    at = _triage_page(monkeypatch, batch_override=_cra_fails,
                      run_override=lambda key: failed if key.startswith("cr_alerts_") else None)
    q = _queue_frame(at)
    assert not q.empty and "Alert" not in set(q["KIND"])          # the queue still renders the rest
    blob = _blob(at)
    assert "Open alerts could not be read — alerts are missing from the queue" in blob
    assert "not installed" not in " ".join(str(e.value) for e in at.info if "alert" in str(e.value).lower())
    assert "Sources: task facts, spend anomalies; not loaded: alerts." in blob
    assert "Sources: alerts" not in blob
    assert "in the triage queue below" not in blob                  # the exception row stops promising it


def test_full_pages_say_they_are_capped(monkeypatch):
    def _caps(specs):
        out = {}
        for s in specs:
            if s.get("key") == "props":
                df = pd.DataFrame({"PROPOSAL_KEY": [f"FAM|ALFA|OBJECT|T{i}" for i in range(20)],
                                   "SUGGESTED_TITLE": [f"t{i}" for i in range(20)],
                                   "SEVERITY": ["HIGH"] * 20, "COMPANY": ["ALFA"] * 20})
                out["props"] = QueryResult(df=df, ok=True)
            if s.get("key") == "cra":
                df = pd.DataFrame({"EVENT_ID": [f"e{i}" for i in range(500)], "RULE_ID": ["R"] * 500,
                                   "RAISED_AT": [pd.Timestamp("2026-09-01")] * 500, "COMPANY": ["ALFA"] * 500,
                                   "SEVERITY": ["HIGH"] * 500, "TITLE": ["t"] * 500, "DETAIL": ["d"] * 500,
                                   "METRIC_VALUE": [1.0] * 500, "STATUS": ["OPEN"] * 500,
                                   "ACK_BY": [None] * 500, "ACK_AT": [None] * 500})
                out["cra"] = QueryResult(df=df, ok=True)
        return out

    at = _triage_page(monkeypatch, batch_override=_caps)
    assert any(str(e.label).startswith("Proposed incidents (20+)") for e in at.expander)
    blob = _blob(at)
    assert "Showing the 20 most severe/newest proposals; more may be open." in blob
    assert "Alert rows are capped at the 500 most severe/newest open events" in blob
