"""Decision Studio ROI fixes (v4.592.0), from the owner's screenshots of 2026-09-24.

D1 realization read '— nothing verified yet' beside $1,864.51 verified (autobook books no estimate); D2 the
lever table printed 'None' (a caller printf overrode the house em-dash); D3 the monthly chart was one dot
(no calendar, current month dropped); D4 the sums of MONTHLY savings were labelled 'all time'; and the
'Healthy' banner claimed realization and follow-through nothing measured."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

import app.logic.actions as actions_mod
from app.logic.actions import savings_month_calendar
from app.logic.proof import proof_verdict
from app.ui import components

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_every_table_asks_the_grid_for_the_em_dash(monkeypatch):
    # Streamlit's grid draws a NULL with its own placeholder (default 'None') before any Styler text, so the
    # house '—' must be the grid's placeholder - on every render path (plain, selectable)
    calls: list = []
    monkeypatch.setattr(components.st, "dataframe", lambda data, **kw: calls.append(kw))
    df = pd.DataFrame({"LEVER": ["RESIZE", "AUTO_SUSPEND"], "REALIZATION_PCT": [None, 88.0]})
    cfg = {"REALIZATION_PCT": st.column_config.NumberColumn("Realization %", format="%.0f%%")}
    components._render_table(df, height=None, size_note=False, column_config=cfg)
    components._render_table(df, height=None, size_note=False, column_config=cfg, key="k", selectable=True)
    assert calls and all(kw.get("placeholder") == "—" for kw in calls)
    # the caller's printf is left to Streamlit (one renderer for every row count, JS rounding)
    assert calls[0]["column_config"]["REALIZATION_PCT"]["type_config"]["format"] == "%.0f%%"


def test_old_runtime_without_placeholder_still_renders(monkeypatch):
    seen: list = []

    def _old(data, **kw):
        if "placeholder" in kw:
            raise TypeError("dataframe() got an unexpected keyword argument 'placeholder'")
        seen.append(kw)

    monkeypatch.setattr(components.st, "dataframe", _old)
    components._render_table(pd.DataFrame({"A": [1.0, None]}), height=None, size_note=False, column_config=None)
    assert len(seen) == 1 and "placeholder" not in seen[0]


def test_month_calendar_zero_fills_and_keeps_the_month_to_date(monkeypatch):
    monkeypatch.setattr(actions_mod, "account_now", lambda: datetime(2026, 9, 24, 9, 0))
    ledger = pd.DataFrame({
        "STATE": ["VERIFIED", "VERIFIED", "VERIFIED", "ESTIMATED"],
        "VERIFIED_AT": ["2026-08-01", "2026-09-10", "2026-09-20", None],
        "VERIFIED_USD": [683.0, 1000.0, 181.51, 999.0],
    })
    cal = savings_month_calendar(ledger, 12)
    assert len(cal) == 12 and list(cal.columns) == ["MONTH", "MONTH_LABEL", "VERIFIED_USD", "PARTIAL"]
    assert cal["MONTH"].iloc[0] == "2025-10" and cal["MONTH"].iloc[-1] == "2026-09"   # chronological
    last = cal.iloc[-1]
    assert last["MONTH_LABEL"] == "Sep 2026 (MTD)" and bool(last["PARTIAL"]) and last["VERIFIED_USD"] == 1181.51
    aug = cal[cal["MONTH"] == "2026-08"].iloc[0]
    assert aug["MONTH_LABEL"] == "Aug 2026" and aug["VERIFIED_USD"] == 683.0 and not bool(aug["PARTIAL"])
    assert float(cal["VERIFIED_USD"].sum()) == 1864.51                   # nothing hidden
    assert (cal["VERIFIED_USD"] == 0).sum() == 10                         # zero months are real bars
    assert savings_month_calendar(None).empty and savings_month_calendar(pd.DataFrame()).empty


def test_roi_labels_are_a_monthly_run_rate():
    ds = _src("app/ui/decision_studio.py")
    body = ds.split("def _roi(", 1)[1].split("\ndef ", 1)[0]
    assert '"label": "Verified savings (all time)"' not in body and "Verified savings run-rate" in body
    assert "Added this quarter" in body and '"method": "measured"' in body
    assert "}/mo** of active savings run-rate" in body and "totals['verified_active_usd']" in body
    assert "older item(s) no longer counted" in body
    assert "no up-front estimate on the" in body
    assert 'charts.monthly_bars_usd(month_df, "MONTH_LABEL", "VERIFIED_USD"' in body
    assert "Experiments (below)" not in ds                               # stale copy


def test_verdict_names_only_measured_facts():
    roi = {"RATIO": 3.2, "PAYS": True}
    v = proof_verdict(roi, None, None, {"PRECISION_PCT": None, "UNTAGGED_SHARE_PCT": 0})
    assert v["level"] == "good" and "earning its keep" in v["headline"]
    assert "pays for itself 3.2x" in v["headline"]
    assert "not yet measured: realization, alert precision, team follow-through" in v["headline"]
    no_cost = proof_verdict({"RATIO": None, "PAYS": False}, 95.0, None, {"PRECISION_PCT": None, "UNTAGGED_SHARE_PCT": 0})
    assert "not yet measured: ROI multiple (run cost)" in no_cost["headline"]
    full = proof_verdict(roi, 95.0, 80.0, {"PRECISION_PCT": 90.0, "UNTAGGED_SHARE_PCT": 0})
    assert "not yet measured" not in full["headline"] and "the team acts on 80%" in full["headline"]
    ds = _src("app/ui/decision_studio.py")
    verdict = ds.split("def decision_verdict(", 1)[1].split("\ndef ", 1)[0]
    assert "savings realize, alerts stay precise" not in verdict and 'proof["headline"]' in verdict


def test_realization_counts_split_auto_from_hand_verified():
    from app.logic.actions import ledger_totals
    ledger = pd.DataFrame({
        "STATE": ["VERIFIED"] * 4,
        "ESTIMATED_USD": [0.0, None, 100.0, 0.0],
        "VERIFIED_USD": [500.0, 250.0, 10.0, 1000.0],
        "VERIFIED_AT": ["2026-09-01", "2026-09-02", "2026-09-03", "2024-01-01"],
        "SOURCE_CHANGE_ID": ["CHG1", None, None, "CHG2"],
    })
    t = ledger_totals(ledger)
    assert t["verified_no_estimate_count"] == 3 and t["verified_no_estimate_auto_count"] == 2
    assert t["verified_active_count"] == 3                       # the 2024 item is past the active window
    ds = _src("app/ui/decision_studio.py")
    assert "auto-measured, " in ds and "verified by hand)" in ds and "not in the ratio" in ds
    assert "every verified item was auto-measured" not in ds
    assert "} verified this quarter\"" not in ds and "/mo added this quarter" in ds
    assert "/mo added this quarter" in _src("app/ui/pages/brief.py")


def test_operations_points_catalog_edits_at_entity_360():
    ops = _src("app/ui/pages/operations.py")
    assert "in Decision Studio" not in ops and "Control Room ▸ Entity 360" in ops
