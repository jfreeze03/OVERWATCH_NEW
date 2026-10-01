"""Next-Fifty #31 on the rendered Proof page (AppTest over the shaped harness, both CI legs):
the "Saved to date" card renders beside the run-rate under production-SHAPED data, and a ledger whose every
verified item was later undone reads the all-reverted caption + the Reverted savings list — never "No
savings verified yet" — with the accrued card still disclosing its split.

Review r1 (tests/test_saved_to_date.py also locks the same wiring by source):
  F25 — the shaped render asserts text ONLY the Saved to date card emits (its title, its "accrued" chip, its
        delta), so deleting the card fails it; the run-rate card's help also names "Saved to date".
  F2/F6/F21 — a $0 accrual beside a verified run-rate reads "nothing accrued yet", not "nothing verified yet".
  F3/F5 — an old kept item + a recent undone one names the undone item, not only the age.
  F7 — the reverted count in the run-rate delta is the uncapped SQL figure; the caption points at the
       Reverted savings list only when it is there, and the list's title says "newest ledger rows" when the
       ledger frame is truncated."""

from __future__ import annotations

from datetime import date

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


def _blob(at) -> str:
    return " ".join(str(m.value) for m in at.markdown) + " " + " ".join(str(c.value) for c in at.caption)


def test_proof_renders_saved_to_date_shaped():
    at = AppTest.from_function(_entry, default_timeout=30)
    at.run()
    assert not at.exception
    _nav_to(at, "Proof")
    at.run()
    assert not at.exception, f"proof (shaped): {at.exception}"
    assert not any("could not finish rendering" in str(getattr(e, "value", "")) for e in at.error)
    blob = _blob(at)
    # F25: only the card itself emits these (the run-rate card's help ALSO says "Saved to date", so a bare
    # substring passed with the card deleted)
    card = _card(blob, "Saved to date")
    assert 'ow-src-badge--method">accrued</span>' in card
    # the shaped summary types SAVED_TO_DATE_USD as a date (the DATE token) -> $0 accrued, while the shaped
    # VERIFIED_ACTIVE_ITEMS counts a verified item: the neutral zero state, never "nothing verified yet"
    assert "$0.00" in card and "nothing accrued yet" in card and "nothing verified yet" not in card
    assert "Verified savings run-rate" in blob


def _card(blob: str, label: str) -> str:
    """One KPI card's rendered HTML (metric_card_html), from its title to the next card's title."""
    head = f'ow-card__title">{label}<'
    assert head in blob, f"no {label!r} card rendered"
    return blob.split(head, 1)[1].split('ow-card__title">', 1)[0]


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


def _render(monkeypatch, ledger: pd.DataFrame, summary: pd.DataFrame, *, truncated: bool = False):
    import app.ui.decision_studio as ds

    def _run(*args, **kwargs):
        key = kwargs.get("key")
        if key == "decision_roi_ledger_full":
            return QueryResult(df=ledger, ok=True, source="t", truncated=truncated)
        if key == "proof_attribution":
            return QueryResult(df=pd.DataFrame(), ok=True, source="t")
        return _shaped_run(*args, **kwargs)

    def _batch(specs, **kwargs):
        out = _shaped_batch(specs, **kwargs)
        if "sc_quarter" in out:
            out["sc_quarter"] = QueryResult(df=summary, ok=True, source="t")
        return out

    monkeypatch.setattr(ds, "run", _run)
    monkeypatch.setattr(ds, "run_batch", _batch)
    at = AppTest.from_function(_entry, default_timeout=30)
    at.run()
    _nav_to(at, "Proof")
    at.run()
    assert not at.exception, f"proof: {at.exception}"
    return at


def _old_kept_row() -> dict:
    # hand-verified 15 months ago and never undone: still verified, no longer in the run-rate
    return {"ITEM_ID": "old", "CREATED_AT": pd.Timestamp("2025-05-20 10:00"), "DESCRIPTION": "Schedule",
            "STATE": "VERIFIED", "ESTIMATED_USD": 0.0, "VERIFIED_USD": 80.0,
            "VERIFIED_AT": pd.Timestamp("2025-06-01 10:00"), "VERIFIED_BY": "JDOE", "NOTES": "booked from Optimize",
            "FINDING_TYPE": "SCHEDULE", "SOURCE": "manual", "SUPERSEDED_BY_CHANGE_ID": None,
            "SOURCE_CHANGE_ID": None, "TARGET_OBJECT": "WH_S", "REVERTED_AT": None}


def test_old_item_plus_recent_undone_item_names_the_undo(monkeypatch):
    # F3/F5: verified_count (kept) = 1 old item, and the one item verified inside the window was undone
    ledger = pd.DataFrame([*_reverted_ledger().to_dict("records"), _old_kept_row()])
    blob = _blob(_render(monkeypatch, ledger, _reverted_summary()))
    assert ("1 older verified item(s) no longer count toward the run-rate (verified more than 12 months ago), "
            "and every item verified in the last 12 months (1) was later undone (see Reverted savings below)"
            ) in blob
    assert "none verified in the last" not in blob


def test_reverted_count_is_the_sql_figure_and_the_pointer_needs_the_list(monkeypatch):
    # F7: the uncapped SQL says 2 undone items left the run-rate; the (capped) ledger frame holds none of them.
    # The delta carries the SQL count, and the caption does not point at a list that is not there.
    summary = _reverted_summary().assign(REVERTED_ACTIVE_ITEMS=2, REVERTED_ACTIVE_USD=450.0)
    at = _render(monkeypatch, pd.DataFrame([_old_kept_row()]), summary, truncated=True)
    blob = _blob(at)
    assert "2 reverted, not counted" in blob
    assert ("2 verified saving(s) (\\$450.00/mo) left the run-rate because the daily change scan saw the "
            "change undone.") in blob                               # md_dollars escapes the $
    assert "see Reverted savings below" not in blob
    assert not [e for e in at.expander if str(e.label).startswith("Reverted savings")]


def test_truncated_ledger_qualifies_the_reverted_list(monkeypatch):
    # F7: the list reads the row-capped frame -> its title says "newest ledger rows" when truncated
    at = _render(monkeypatch, _reverted_ledger(), _reverted_summary(), truncated=True)
    labels = [str(e.label) for e in at.expander]
    assert "Reverted savings — 1 change(s) undone (newest ledger rows)" in labels
    assert "left the run-rate because the daily change scan saw the change undone — see Reverted savings below" \
        in _blob(at)
