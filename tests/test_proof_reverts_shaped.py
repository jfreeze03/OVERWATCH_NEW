"""Next-Fifty #31 on the rendered Proof page (AppTest, streamlit >= 1.55 like the rest of the shaped harness):
the "Saved to date" card renders beside the run-rate under production-SHAPED data, and a ledger whose every
verified item was later undone reads the all-reverted caption + the Reverted savings list — never "No
savings verified yet" — with the accrued card still disclosing its split."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from test_pages_shaped import (  # noqa: F401 - _stub_shaped is the harness's autouse fixture
    _APPTEST_BUTTONGROUP_OK,
    _entry,
    _nav_to,
    _shaped_batch,
    _shaped_run,
    _stub_shaped,
)

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from app.core.result import QueryResult

_SKIP = pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")


def _blob(at) -> str:
    return " ".join(str(m.value) for m in at.markdown) + " " + " ".join(str(c.value) for c in at.caption)


@_SKIP
def test_proof_renders_saved_to_date_shaped():
    at = AppTest.from_function(_entry, default_timeout=30)
    at.run()
    assert not at.exception
    _nav_to(at, "Proof")
    at.run()
    assert not at.exception, f"proof (shaped): {at.exception}"
    assert not any("could not finish rendering" in str(getattr(e, "value", "")) for e in at.error)
    blob = _blob(at)
    assert "Saved to date" in blob and "Verified savings run-rate" in blob


def _reverted_ledger() -> pd.DataFrame:
    return pd.DataFrame([{
        "ITEM_ID": "i1", "CREATED_AT": pd.Timestamp("2026-06-01 06:45"), "DESCRIPTION": "Detected SIZE change",
        "STATE": "VERIFIED", "ESTIMATED_USD": 0.0, "VERIFIED_USD": 300.0,
        "VERIFIED_AT": pd.Timestamp("2026-06-16 06:45"), "VERIFIED_BY": "AUTO:TASK_LEDGER_AUTOBOOK",
        "NOTES": "Auto-booked | measured on the full window", "FINDING_TYPE": "RESIZE", "SOURCE": "auto",
        "SUPERSEDED_BY_CHANGE_ID": None, "MEASURED_AFTER_DAYS": 14.0, "SOURCE_CHANGE_ID": "C1",
        "TARGET_OBJECT": None, "CHANGE_WAREHOUSE": "WH_A", "CHANGE_SETTING": "SIZE", "CHANGE_OLD_VALUE": "Large",
        "CHANGE_NEW_VALUE": "Small", "CHANGE_SEEN_AT": pd.Timestamp("2026-06-01 06:40", tz="America/Chicago"),
        "CHANGE_VERDICT": "IMPROVED", "TRACKING_UNTIL": date(2026, 6, 15),
        "REVERTED_AT": pd.Timestamp("2026-08-01 06:40", tz="America/Chicago"), "REVERT_CHANGE_ID": "C2",
        "REVERT_OLD_VALUE": "Small", "REVERT_NEW_VALUE": "Medium", "REVERT_KIND": "partial"}])


def _reverted_summary() -> pd.DataFrame:
    return pd.DataFrame([{
        "VERIFIED_QTD_USD": 0.0, "VERIFIED_ITEMS": 0, "VERIFIED_ACTIVE_MONTHLY_USD": 0.0, "VERIFIED_ACTIVE_ITEMS": 0,
        "ESTIMATED_OPEN_USD": 0.0, "SUPERSEDED_ITEMS": 0, "REVERTED_ACTIVE_ITEMS": 1, "REVERTED_ACTIVE_USD": 300.0,
        "SAVED_TO_DATE_USD": 610.0, "SAVED_MEASURED_USD": 140.0, "SAVED_BEFORE_REVERT_USD": 610.0,
        "SAVED_SINCE_DATE": date(2026, 6, 1)}])


@_SKIP
def test_all_reverted_ledger_reads_honestly(monkeypatch):
    import app.ui.decision_studio as ds

    def _run(*args, **kwargs):
        key = kwargs.get("key")
        if key == "decision_roi_ledger_full":
            return QueryResult(df=_reverted_ledger(), ok=True, source="t")
        if key == "proof_attribution":                       # no attribution read: labels stay NULL
            return QueryResult(df=pd.DataFrame(), ok=True, source="t")
        return _shaped_run(*args, **kwargs)

    def _batch(specs, **kwargs):
        out = _shaped_batch(specs, **kwargs)
        if "sc_quarter" in out:
            out["sc_quarter"] = QueryResult(df=_reverted_summary(), ok=True, source="t")
        return out

    monkeypatch.setattr(ds, "run", _run)
    monkeypatch.setattr(ds, "run_batch", _batch)
    at = AppTest.from_function(_entry, default_timeout=30)
    at.run()
    _nav_to(at, "Proof")
    at.run()
    assert not at.exception, f"proof (all reverted): {at.exception}"
    blob = _blob(at)
    assert "1 verified item(s), all later undone" in blob
    assert "No savings verified yet" not in blob
    assert "left the run-rate because the daily change scan saw the change undone" in blob
    assert "$610.00" in blob and "incl. $610.00 saved before a change was undone" in blob
    assert "Reverted savings — 1 change(s) undone" in [str(e.label) for e in at.expander]
    assert "1 reverted, not counted" in blob
