"""Control Room review round 1 (cluster c06) -- behavioural locks that need no AppTest runtime.

R1-060  the auto-investigation reads the change registries ANCHORED on the incident onset (nearest first),
        so post-onset churn can no longer push the pre-onset trigger past the LIMIT 200.
R1-202  anomaly_summary cuts to a day window BEFORE its top-10, so stronger historical spikes / collapses
        cannot evict the current spike (triage) or the onset spike (auto-investigation).
R1-203  the unscored-warehouse disclosure counts with flag_anomalies' real 10-active-day gate.
R1-204  the freshness board says "not installed" only for a true absence; a failed read is unavailable.
R1-210  day replay renders a failed read as unavailable, and a day with no loaded facts as no_data_yet.

The rendered-page locks (Pulse under a Schema filter, the triage disclosure) are in
tests/test_control_room_review_r1_shaped.py.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
import sqlglot

from app.core.result import QueryResult
from app.data import change_impact_sql
from app.logic.anomaly import (
    ANOMALY_MIN_ACTIVE_DAYS,
    ANOMALY_MIN_USD,
    anomaly_summary,
    flag_anomalies,
    unscorable_groups,
)
from app.logic.formulas import account_today
from app.ui.pages import control_room

_TODAY = account_today()


def _wiggle(i: int, base: float, amp: float) -> float:
    """A deterministic series with MAD == amp (deviations -2..2 x amp around the base)."""
    return base + ((i % 5) - 2) * amp


def _crowded_frame(spike_day: date, *, n_old: int = 12) -> pd.DataFrame:
    """30 complete days. n_old near-flat warehouses each carry ONE huge old spike (|z| in the
    thousands), WH_PROD is noisy (MAD 100) and spikes +3000 on ``spike_day`` (z ~ 20)."""
    rows = []
    days = [_TODAY - timedelta(days=d) for d in range(30, 0, -1)]
    for w in range(n_old):
        old = _TODAY - timedelta(days=25 - (w % 5))
        for i, d in enumerate(days):
            rows.append({"WAREHOUSE_NAME": f"WH_OLD_{w:02d}", "DAY": d,
                         "USD": _wiggle(i, 1000.0, 1.0) + (4000.0 if d == old else 0.0)})
    for i, d in enumerate(days):
        rows.append({"WAREHOUSE_NAME": "WH_PROD", "DAY": d,
                     "USD": _wiggle(i, 3000.0, 100.0) + (3000.0 if d == spike_day else 0.0)})
    return pd.DataFrame(rows)


# ------------------------------------------------------------------------------------------ R1-202 ----

def test_a_current_spike_is_not_evicted_by_stronger_historical_ones():
    yday = _TODAY - timedelta(days=1)
    flagged = flag_anomalies(_crowded_frame(yday), "USD", group_col="WAREHOUSE_NAME",
                             min_value=ANOMALY_MIN_USD, min_active_days=ANOMALY_MIN_ACTIVE_DAYS)
    # the pre-fix shape: cap first, filter after -> yesterday's spike is gone
    capped = [a for a in anomaly_summary(flagged, "WAREHOUSE_NAME", "USD")
              if pd.Timestamp(a["day"]).date() == yday]
    assert capped == []
    # the fix: the day window is applied BEFORE the top-10
    hits = anomaly_summary(flagged, "WAREHOUSE_NAME", "USD", day_from=yday, day_to=yday)
    assert [h["label"] for h in hits] == ["WH_PROD"] and hits[0]["z"] > 3.5


@pytest.mark.parametrize("as_type", ["date", "datetime64", "iso"])
def test_the_day_window_matches_every_day_dtype(as_type):
    # review note on R1-202: a str() compare of a datetime64 DAY ('... 00:00:00') against an
    # astype(str) DAY ('YYYY-MM-DD') silently matched nothing. The window compares DATES.
    yday = _TODAY - timedelta(days=1)
    frame = _crowded_frame(yday, n_old=2)
    if as_type == "datetime64":
        frame["DAY"] = pd.to_datetime(frame["DAY"])
    elif as_type == "iso":
        frame["DAY"] = frame["DAY"].map(lambda d: d.isoformat())
    flagged = flag_anomalies(frame, "USD", group_col="WAREHOUSE_NAME")
    hits = anomaly_summary(flagged, "WAREHOUSE_NAME", "USD", day_from=pd.Timestamp(yday), day_to=yday)
    assert [h["label"] for h in hits] == ["WH_PROD"]
    # no window -> unchanged behaviour (strongest first, every day)
    assert len(anomaly_summary(flagged, "WAREHOUSE_NAME", "USD")) == 3


# ------------------------------------------------------------------------------------------ R1-203 ----

def _days_frame(name: str, usd: list[float]) -> pd.DataFrame:
    days = [_TODAY - timedelta(days=len(usd) - i) for i in range(len(usd))]
    return pd.DataFrame({"WAREHOUSE_NAME": name, "DAY": days, "USD": usd})


def test_unscorable_counts_with_the_real_active_day_gate():
    frame = pd.concat([
        _days_frame("WH_STEADY", [500.0] * 30),                       # scorable
        _days_frame("WH_NEW", [368.0] * 8 + [3680.0]),                # 9 active days: unscorable
        _days_frame("WH_SPARSE", [0.0] * 22 + [400.0] * 7 + [3680.0]),  # 8 non-zero of 30: unscorable
        _days_frame("WH_IDLE", [0.0] * 25 + [12.0] * 5),              # never material: hides nothing
    ])
    assert ANOMALY_MIN_ACTIVE_DAYS == 10       # the caption builds its number from this constant
    assert unscorable_groups(frame, "USD", "WAREHOUSE_NAME") == 2
    # and those two really cannot be flagged by the page's own call
    flagged = flag_anomalies(frame, "USD", group_col="WAREHOUSE_NAME",
                             min_value=ANOMALY_MIN_USD, min_active_days=ANOMALY_MIN_ACTIVE_DAYS)
    assert not flagged.loc[flagged["WAREHOUSE_NAME"].isin(["WH_NEW", "WH_SPARSE"]), "IS_ANOMALY"].any()
    # the pre-fix count (<5 distinct days) saw none of them
    assert int((frame.groupby("WAREHOUSE_NAME")["DAY"].nunique() < 5).sum()) == 0
    assert unscorable_groups(pd.DataFrame(), "USD", "WAREHOUSE_NAME") == 0


def test_triage_caption_states_the_real_gate():
    src = Path(control_room.__file__).read_text(encoding="utf-8")
    assert "A warehouse needs {ANOMALY_MIN_ACTIVE_DAYS}+ active (non-zero-spend)" in src
    assert "5+ complete days of history" not in src
    assert "_thin_warehouses = unscorable_groups(_wh_complete," in src


# ------------------------------------------------------------------------------------------ R1-060 ----

_ONSET = datetime(2026, 9, 10, 14, 5, 7)


def test_the_registries_can_read_a_window_anchored_on_onset():
    for sql, col in ((change_impact_sql.change_registry(30, "ALFA", onset=_ONSET), "CHANGE_SEEN_AT"),
                     (change_impact_sql.warehouse_change_registry(30, "ALL", onset=_ONSET), "w.CHANGE_SEEN_AT")):
        seen = f"CONVERT_TIMEZONE('America/Chicago', {col})::TIMESTAMP_NTZ"
        lit = "'2026-09-10 14:05:07'::TIMESTAMP_NTZ"
        assert (f"{seen} BETWEEN DATEADD('day', -{change_impact_sql.ONSET_LEAD_DAYS}, {lit}) "
                f"AND DATEADD('day', {change_impact_sql.ONSET_AFTER_DAYS}, {lit})") in sql
        assert f"ORDER BY ABS(DATEDIFF('second', {seen}, {lit}))" in sql      # the cap keeps the nearest
        assert "COUNT(*) OVER () AS TOTAL_CHANGES" in sql                     # the backstop caption's total
        assert "DATEADD('day', -30, CURRENT_TIMESTAMP())" not in sql
        sqlglot.parse_one(sql, read="snowflake")
    # an aware onset converts to account time; garbage never reaches the SQL
    aware = pd.Timestamp("2026-09-10 19:05:07", tz="UTC")
    assert "'2026-09-10 14:05:07'::TIMESTAMP_NTZ" in change_impact_sql.change_registry(30, onset=aware)
    with pytest.raises(ValueError):
        change_impact_sql.change_registry(30, onset="x'; DROP TABLE t; --")
    # without onset the builders are unchanged (newest first from now)
    assert "ORDER BY CHANGE_SEEN_AT DESC" in change_impact_sql.change_registry(30, "ALFA")
    assert "TOTAL_CHANGES" not in change_impact_sql.warehouse_change_registry(90)


class _Rec:
    def __init__(self):
        self.calls: list[tuple] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


@pytest.fixture
def _rca_page(monkeypatch):
    """_auto_investigation with its reads and renders stubbed; returns (specs, out) recorders."""
    import streamlit as st

    from app.ui import ai_panel

    specs: list[dict] = []
    out: dict[str, list] = {"banner": [], "caption": [], "rows": []}
    feeds: dict[str, QueryResult] = {}

    def _batch(spec_list, **_kw):
        specs.extend(spec_list)
        return {s["key"]: feeds.get(s["key"], QueryResult(df=pd.DataFrame(), ok=True)) for s in spec_list}

    monkeypatch.setattr(control_room, "run_batch", _batch)
    monkeypatch.setattr(control_room, "section_header", lambda *a, **k: None)
    monkeypatch.setattr(control_room, "load_settings", lambda _p: {})
    monkeypatch.setattr(control_room, "styled_table", lambda df, **k: out["rows"].append(df))
    monkeypatch.setattr(ai_panel, "ai_evaluation_panel", lambda **k: None)
    for fn in ("info", "warning", "error"):
        monkeypatch.setattr(st, fn, lambda text, *a, **k: out["banner"].append(str(text)))
    monkeypatch.setattr(st, "caption", lambda text, *a, **k: out["caption"].append(str(text)))
    return specs, out, feeds


def test_auto_investigation_anchors_the_registry_reads_on_onset(_rca_page):
    specs, _out, _feeds = _rca_page
    onset = pd.Timestamp(datetime.combine(_TODAY - timedelta(days=20), datetime.min.time())) + timedelta(hours=9)
    control_room._auto_investigation(pd.Series({"STARTED_AT": onset, "INCIDENT_ID": "i-1"}), "ALL", 3.0)
    by_key = {s["key"]: s["sql"] for s in specs}
    lit = f"'{onset.strftime('%Y-%m-%d %H:%M:%S')}'::TIMESTAMP_NTZ"
    for key in ("ai_obj", "ai_wh"):
        assert f"BETWEEN DATEADD('day', -3, {lit})" in by_key[key], key
        assert "ORDER BY ABS(DATEDIFF(" in by_key[key], key


def test_auto_investigation_captions_a_cut_registry(_rca_page):
    _specs, out, feeds = _rca_page
    onset = pd.Timestamp(_TODAY - timedelta(days=2)) + timedelta(hours=9)
    feeds["ai_obj"] = QueryResult(df=pd.DataFrame({
        "OBJECT_NAME": [f"DB.S.SP_{i}" for i in range(200)],
        "CHANGE_SEEN_AT": [onset - timedelta(minutes=i) for i in range(200)],
        "TOTAL_CHANGES": [251] * 200}), ok=True)
    control_room._auto_investigation(pd.Series({"STARTED_AT": onset, "INCIDENT_ID": "i-2"}), "ALL", 3.0)
    assert any("Ranked from the 200 of 251 object changes nearest onset" in c for c in out["caption"])


def test_auto_investigation_keeps_the_onset_spike_among_stronger_old_ones(_rca_page):
    # R1-202's RCA twin: 12 stronger historical spikes used to take every top-10 slot, so the
    # spike the day before onset never became a candidate.
    _specs, out, feeds = _rca_page
    spike_day = _TODAY - timedelta(days=2)
    onset = pd.Timestamp(spike_day + timedelta(days=1)) + timedelta(hours=9)
    frame = _crowded_frame(spike_day)
    feeds["ai_whd"] = QueryResult(df=frame.assign(CREDITS_TOTAL=frame["USD"] / 3.0).drop(columns="USD"), ok=True)
    control_room._auto_investigation(pd.Series({"STARTED_AT": onset, "INCIDENT_ID": "i-3"}), "ALL", 3.0)
    assert out["rows"], out["banner"]
    titles = out["rows"][0]["Hypothesis"].tolist()
    assert any("WH_PROD" in t for t in titles), titles
    assert not any("WH_OLD_" in t for t in titles), titles


# ------------------------------------------------------------------------------------------ R1-204 ----

@pytest.mark.parametrize("kind,state", [("absent", "needs_setup"), ("privilege", "needs_setup"),
                                        ("timeout", "unavailable"), ("missing_column", "unavailable"),
                                        ("other", "unavailable")])
def test_freshness_board_splits_a_failed_read_by_kind(monkeypatch, kind, state):
    seen: list[tuple] = []
    monkeypatch.setattr(control_room, "run_mart_first",
                        lambda *a, **k: QueryResult(ok=False, error=f"boom ({kind})", error_kind=kind))
    monkeypatch.setattr(control_room, "section_header", lambda *a, **k: None)
    monkeypatch.setattr(control_room, "empty_state", lambda k, msg, **kw: seen.append((k, msg, kw)))
    control_room._freshness_board()
    assert [s[0] for s in seen] == [state]
    if state == "unavailable":
        assert "could not be read" in seen[0][1] and seen[0][2]["detail"] == f"boom ({kind})"
        assert seen[0][2]["action_label"] == "Full freshness table → Admin"   # the Admin doorway survives
    else:
        assert "not installed yet" in seen[0][1]


# ------------------------------------------------------------------------------------------ R1-210 ----

def _replay(monkeypatch, results: dict[str, QueryResult]) -> list[tuple]:
    seen: list[tuple] = []
    monkeypatch.setattr(control_room, "section_header", lambda *a, **k: None)
    monkeypatch.setattr(control_room, "load_settings", lambda _p: {})
    monkeypatch.setattr(control_room, "filters", lambda: {"company": "ALL"})
    monkeypatch.setattr(control_room, "run_batch_mixed", lambda specs, **k: dict(results))
    monkeypatch.setattr(control_room, "empty_state", lambda k, msg, **kw: seen.append((k, msg, kw)))
    monkeypatch.setattr(control_room, "guard", lambda *a, **k: False)
    control_room._day_replay()
    return seen


_KEYS = ("mv", "act", "tf", "al", "ddl", "gr")


def test_day_replay_renders_an_all_failed_read_as_unavailable(monkeypatch):
    seen = _replay(monkeypatch, {k: QueryResult(ok=False, error="timed out", error_kind="timeout") for k in _KEYS})
    assert seen[0][0] == "unavailable" and "could not be read" in seen[0][1]
    assert seen[0][2]["detail"] == "timed out"
    assert not any("No telemetry loaded" in s[1] for s in seen)


def test_day_replay_all_absent_is_needs_setup(monkeypatch):
    seen = _replay(monkeypatch, {k: QueryResult(ok=False, error="does not exist", error_kind="absent")
                                 for k in _KEYS})
    assert [s[0] for s in seen] == ["needs_setup"]


def test_day_replay_a_day_with_no_loaded_facts_is_not_a_green_quiet_day(monkeypatch):
    # day_activity returns ONE aggregate row even over no data (all NULL counts)
    act = pd.DataFrame({"QUERY_COUNT": [None], "FAILED_COUNT": [None], "QUEUED_SEC": [None],
                        "SPILL_GB": [None], "BASELINE_QUERIES": [None], "BASELINE_FAILED": [None]})
    res = {k: QueryResult(df=pd.DataFrame(), ok=True) for k in _KEYS}
    res["act"] = QueryResult(df=act, ok=True)
    seen = _replay(monkeypatch, res)
    assert [s[0] for s in seen] == ["no_data_yet"] and "No telemetry loaded" in seen[0][1]
