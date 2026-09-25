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


def test_null_printf_columns_detects_only_nullable_printf_numbers():
    df = pd.DataFrame({"REALIZATION_PCT": [90.0, float("nan")], "VERIFIED_USD": [1.0, 2.0], "LEVER": ["A", "B"]})
    cfg = {"REALIZATION_PCT": st.column_config.NumberColumn("Realization %", format="%.0f%%"),
           "VERIFIED_USD": st.column_config.NumberColumn("Verified $", format="$%.2f"),   # no NULLs
           "LEVER": st.column_config.TextColumn("Lever")}
    assert components._nullable_printf_columns(df, cfg) == {"REALIZATION_PCT": "%.0f%%"}
    named = {"REALIZATION_PCT": st.column_config.NumberColumn("R", format="percent")}   # not printf
    assert components._nullable_printf_columns(df, named) == {}


def test_render_table_shows_the_em_dash_for_a_caller_printf_null(monkeypatch):
    seen: dict = {}

    def _capture(data, **kw):
        seen["data"], seen["cfg"] = data, kw.get("column_config")

    monkeypatch.setattr(components.st, "dataframe", _capture)
    df = pd.DataFrame({"LEVER": ["RESIZE", "AUTO_SUSPEND"], "REALIZATION_PCT": [None, 88.0]})
    components._render_table(df, height=None, size_note=False, column_config={
        "REALIZATION_PCT": st.column_config.NumberColumn("Realization %", format="%.0f%%")})
    cfg = seen["cfg"]["REALIZATION_PCT"]
    assert cfg["label"] == "Realization %"                   # the caller's label survives
    assert cfg["type_config"]["format"] is None              # its printf no longer overrides the cell
    html = seen["data"].to_html()
    assert "—" in html and "88%" in html and ">None<" not in html


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
    assert "}/mo** of savings run-rate across" in body
    assert "auto-measured — no up-front estimate" in body
    assert 'charts.monthly_bars_usd(month_df, "MONTH_LABEL", "VERIFIED_USD"' in body
    assert "Experiments (below)" not in ds                               # stale copy


def test_verdict_names_only_measured_facts():
    roi = {"RATIO": 3.2, "PAYS": True}
    v = proof_verdict(roi, None, None, {"PRECISION_PCT": None, "UNTAGGED_SHARE_PCT": 0})
    assert v["level"] == "good" and "earning its keep" in v["headline"]
    assert "pays for itself 3.2x" in v["headline"]
    assert "not yet measured: realization, alert precision, team follow-through" in v["headline"]
    full = proof_verdict(roi, 95.0, 80.0, {"PRECISION_PCT": 90.0, "UNTAGGED_SHARE_PCT": 0})
    assert "not yet measured" not in full["headline"] and "the team acts on 80%" in full["headline"]
    ds = _src("app/ui/decision_studio.py")
    verdict = ds.split("def decision_verdict(", 1)[1].split("\ndef ", 1)[0]
    assert "savings realize, alerts stay precise" not in verdict and 'proof["headline"]' in verdict


def test_operations_points_catalog_edits_at_entity_360():
    ops = _src("app/ui/pages/operations.py")
    assert "in Decision Studio" not in ops and "Control Room ▸ Entity 360" in ops
