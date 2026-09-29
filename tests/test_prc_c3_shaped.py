"""PR C slice C3 on rendered pages (AppTest, streamlit >= 1.55 like the rest of the shaped harness; the floor
leg skips these, and tests/test_triage_track.py + tests/test_optimize_queue.py lock the same wiring by source):

  (a) Action Center with *Include completed work*: completed warehouse / task items carry a measured Held?
      label, the signals read fires, and the selected DONE item shows its outcome chip + basis caption.
  (b) Control Room ▸ Incidents & triage as an operator: the unowned-first ranking caption and the
      'Track as work item' expander paint over shaped data.
  (c) Operations ▸ Optimize: a family marked done whose measured outcome never held reads "Not fixed" and
      its detail pane says Track all includes it again.
"""

from __future__ import annotations

from datetime import timedelta

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
from app.logic.formulas import account_today

_SKIP = pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")


def _blob(at) -> str:
    return " ".join(str(m.value) for m in list(at.markdown) + list(at.caption) + list(at.warning)
                    + list(at.info) + list(at.error))


def _signals(kind: str, key: str, done, before: float, after: float, col: str = "CREDITS") -> pd.DataFrame:
    today = account_today()
    rows = []
    for i in range(1, 60):
        day = today - timedelta(days=i)
        val = before if day < done else after
        rows.append({"ENTITY_TYPE": kind, "ENTITY_KEY_U": key, "DAY": day,
                     "CREDITS": val if col == "CREDITS" else None, "P95_SEC": val if col == "P95_SEC" else None,
                     "RUNS": 24.0, "FAILS": 0.0, "LOADED_THROUGH": today - timedelta(days=1)})
    return pd.DataFrame(rows)


@_SKIP
def test_action_center_completed_work_carries_held(monkeypatch):
    from app.ui import workbench

    today = account_today()
    done = today - timedelta(days=20)
    cols = ["ACTION_ID", "CREATED_AT", "COMPANY", "SEVERITY", "TITLE", "DETAIL", "OWNER", "STATUS", "DUE_DATE",
            "DEFER_UNTIL", "COMPLETED_AT", "RESOLUTION_NOTE", "SOURCE", "SOURCE_ENTITY_TYPE", "SOURCE_ENTITY_KEY",
            "CONFIDENCE", "PROOF_SQL", "ESTIMATED_USD", "PERIOD", "UPDATED_AT", "UPDATED_BY"]

    def _row(aid, status, etype, key):
        return {"ACTION_ID": aid, "CREATED_AT": pd.Timestamp(done - timedelta(days=5)), "COMPANY": "ALFA",
                "SEVERITY": "MEDIUM", "TITLE": f"Fix {key}", "DETAIL": "Daily spend $1,500 vs $900 baseline.",
                "OWNER": "UNASSIGNED", "STATUS": status, "DUE_DATE": None, "DEFER_UNTIL": None,
                "COMPLETED_AT": pd.Timestamp(done) if status == "DONE" else None, "RESOLUTION_NOTE": None,
                "SOURCE": "Control Room > Triage", "SOURCE_ENTITY_TYPE": etype, "SOURCE_ENTITY_KEY": key,
                "CONFIDENCE": None, "PROOF_SQL": None, "ESTIMATED_USD": None, "PERIOD": None,
                "UPDATED_AT": pd.Timestamp(done), "UPDATED_BY": "JDOE"}

    actions = pd.DataFrame([_row("a-wh", "DONE", "WAREHOUSE", "WH_A"), _row("a-task", "DONE", "TASK", "DB.S.T"),
                            _row("a-open", "OPEN", "WAREHOUSE", "WH_B")], columns=cols)
    signals = pd.DataFrame(_signals("WAREHOUSE", "WH_A", done, 100.0, 40.0).to_dict("records")
                           + _signals("TASK", "DB.S.T", done, 600.0, 590.0, col="P95_SEC").to_dict("records"))
    seen: list[str] = []

    def _run(*args, **kwargs):
        sql = str(args[0] if args else kwargs.get("sql", ""))
        if "WITH want AS" in sql:                                       # entity_daily_signals
            seen.append(sql)
            return QueryResult(df=signals.copy(), ok=True, source="stub")
        if "SOURCE_ENTITY_KEY, CONFIDENCE, PROOF_SQL" in sql:           # action_center
            return QueryResult(df=actions.copy(), ok=True, source="stub")
        return _shaped_run(*args, **kwargs)

    monkeypatch.setattr(workbench, "run", _run)
    at = AppTest.from_function(_entry, default_timeout=30)
    at.run()
    assert not at.exception
    _nav_to(at, "Control Room")
    at.session_state["action_include_closed"] = True
    at.session_state["_ow_md_sel_action_center"] = "a-wh"
    at.run()
    assert not at.exception, f"action center (held): {at.exception}"
    assert not any("could not finish rendering" in str(getattr(e, "value", "")) for e in at.error)
    assert len(seen) == 1 and "'WH_A'" in seen[0] and "'DB.S.T'" in seen[0] and "'WH_B'" not in seen[0]
    tables = [d.value for d in at.dataframe if "Held?" in list(getattr(d.value, "columns", []))]
    assert tables, "the Held? column did not reach the Action Center list"
    held = [str(v) for v in tables[0]["Held?"].tolist() if isinstance(v, str)]
    assert "Held 19 days" in held and "Not fixed" in held
    blob = _blob(at)
    assert "Held? measures a completed warehouse, task or query-family item" in blob
    assert "Held? Held 19 days" in blob                                  # the detail chip
    assert "Measured since it was marked done:" in blob


@_SKIP
def test_entity_360_work_and_outcomes_carries_held(monkeypatch):
    from app.ui import workbench

    today = account_today()
    done = today - timedelta(days=20)
    related = pd.DataFrame([
        {"ACTION_ID": "a-new", "CREATED_AT": pd.Timestamp(done), "SEVERITY": "MEDIUM", "TITLE": "Fix WH_A",
         "OWNER": "UNASSIGNED", "STATUS": "DONE", "DUE_DATE": None, "DEFER_UNTIL": None, "ESTIMATED_USD": None,
         "CONFIDENCE": 0.5, "UPDATED_AT": pd.Timestamp(done), "COMPLETED_AT": pd.Timestamp(done)}])
    seen: list[str] = []

    def _run(*args, **kwargs):
        sql = str(args[0] if args else kwargs.get("sql", ""))
        if "WITH want AS" in sql:
            seen.append(sql)
            return QueryResult(df=_signals("WAREHOUSE", "WH_A", done, 100.0, 40.0), ok=True, source="stub")
        if "ORDER BY IFF(UPPER(STATUS) IN ('OPEN', 'IN_PROGRESS'), 0, 1), UPDATED_AT DESC" in sql:
            return QueryResult(df=related.copy(), ok=True, source="stub")      # related_actions
        return _shaped_run(*args, **kwargs)

    monkeypatch.setattr(workbench, "run", _run)
    at = AppTest.from_function(_entry, default_timeout=30)
    at.run()
    _nav_to(at, "Control Room")
    at.session_state["cr_section"] = "Entity 360"
    at.session_state["entity_360_type"] = "WAREHOUSE"
    at.session_state["entity_360_key"] = "wh_a"
    at.run()
    assert not at.exception, f"entity 360 (held): {at.exception}"
    assert len(seen) == 1 and "'WAREHOUSE', 'WH_A'" in seen[0]
    tables = [d.value for d in at.dataframe if "Held?" in list(getattr(d.value, "columns", []))]
    assert tables and "HELD_BASIS" not in list(tables[0].columns)
    assert tables[0]["Held?"].tolist() == ["Held 19 days"]
    assert "Newest completed item — Held? Held 19 days" in _blob(at)


@_SKIP
def test_control_room_triage_ranks_unowned_first_and_offers_track():
    at = AppTest.from_function(_entry, default_timeout=60)
    at.run()
    assert not at.exception
    _nav_to(at, "Control Room")
    at.session_state["cr_section"] = "Incidents & triage"
    at.session_state["_ow_current_role"] = "SNOW_SYSADMINS"
    at.run()
    assert not at.exception, f"control room triage (shaped): {at.exception}"
    assert not any("could not finish rendering" in str(getattr(e, "value", "")) for e in at.error)
    blob = _blob(at)
    assert "Triage queue" in blob
    assert "ranked by severity, then unowned first" in blob
    assert any(str(e.label) == "Track as work item" for e in at.expander)
    assert at.selectbox(key="cr_track_pick") is not None
    assert "Track" in [str(b.label) for b in at.button]


@_SKIP
def test_optimize_done_family_that_never_held_reads_not_fixed(monkeypatch):
    from app.ui.pages.ops_parts import optimize_queue

    today = account_today()
    done = today - timedelta(days=20)
    tracked = pd.DataFrame([{"ENTITY_KEY_U": "1.0", "LATEST_ACTION_ID": "a-1", "OPEN_ACTION_ID": None,
                             "OPEN_ACTION_COMPANY": None, "ACTION_STATUS": "DONE", "ACTION_OWNER": "UNASSIGNED",
                             "OPEN_N": 0, "DROPPED_N": 0, "DONE_N": 1, "LAST_DECIDED": pd.Timestamp(done)}])
    seen: list[str] = []

    def _batch(specs, **kwargs):
        out = _shaped_batch(specs, **kwargs)
        out["ops_opt_tracked"] = QueryResult(df=tracked.copy(), ok=True, source="stub")
        return out

    def _run(*args, **kwargs):
        sql = str(args[0] if args else kwargs.get("sql", ""))
        if "WITH want AS" in sql:
            seen.append(sql)
            return QueryResult(df=_signals("QUERY_FINGERPRINT", "1.0", done, 10.0, 9.9), ok=True, source="stub")
        return _shaped_run(*args, **kwargs)

    monkeypatch.setattr(optimize_queue, "run_batch_mixed", _batch)
    monkeypatch.setattr(optimize_queue, "run", _run)
    at = AppTest.from_function(_entry, default_timeout=30)
    at.run()
    _nav_to(at, "Operations")
    at.session_state["ops_section"] = "Optimize"
    at.session_state["_ow_current_role"] = "SNOW_SYSADMINS"
    at.session_state["_ow_md_sel_ops_optimize"] = "1.0"
    at.run()
    assert not at.exception, f"operations optimize (held): {at.exception}"
    assert len(seen) == 1 and "'QUERY_FINGERPRINT', '1.0'" in seen[0]
    blob = _blob(at)
    assert "Action Center: Not fixed." in blob
    assert "Marked done, but the measured outcome says Not fixed" in blob and "Track all includes it again." in blob
    assert "Held? measures a family marked done" in blob
