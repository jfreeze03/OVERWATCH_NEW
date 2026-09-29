"""PR C slice C2 on the rendered pages (AppTest, streamlit >= 1.55 like the rest of the shaped harness; the floor
leg skips these, and tests/test_cycle_eta_surfaces.py + tests/test_unread_maintenance.py lock the same wiring by
source).

- #36: Operations ▸ Pipeline SLA ▸ Tonight with an in-flight cycle paints "Tonight's projected finish" (three
  tiles + the V156 disclosure) under the glance, and the cycle-timeline toggle renders the per-workflow table.
  The Brief's Nightly cycle tile shows the same projection. A failing ETA-enriched night read still renders the
  glance's whole-night roll-up (the fallback), never a blank.
- #30: Cost ▸ Optimization & Savings ▸ Storage & waste — the unread-maintenance scan is OFF by default (no read
  on first paint); on, both reads run and the panel paints; an operator can book a confirmed-unread object as an
  ESTIMATED ledger row (the INSERT reaches the statement executor)."""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import pytest
from test_pages_shaped import (  # noqa: F401 - _stub_shaped is the harness's autouse fixture
    _APPTEST_BUTTONGROUP_OK,
    _entry,
    _nav_to,
    _shaped_batch,
    _shaped_from_sql,
    _shaped_run,
    _stub_shaped,
)

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from app.config import DEFAULT_SETTINGS
from app.core.result import QueryResult
from app.data import etl_control_sql

_SKIP = pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")

_FQN = "DB.SCH.CONTROL_STATUS"
_START_WF = DEFAULT_SETTINGS["ETL_CYCLE_START_WORKFLOW"]
_END_WF = DEFAULT_SETTINGS["ETL_CYCLE_END_WORKFLOW"]
_TONIGHT = pd.Timestamp("2026-09-16")
_SNAP = pd.Timestamp("2026-09-17 01:00", tz="America/Chicago")


def _texts(at) -> str:
    return " ".join(str(e.value) for e in list(at.markdown) + list(at.caption) + list(at.warning)
                    + list(at.info) + list(at.error)).replace("&#x27;", "'").replace("&#39;", "'")


def _cycle_finish_frame() -> pd.DataFrame:
    """8 clean nights (22:00 start, 6h40m .. 7h50m) + tonight in flight since 22:30: the median 7h15m projects
    05:45; the 25th-75th percentile range is 05:28-06:02 (a 6h57m30s quartile rounds to the minute)."""
    rows = []
    for i, minutes in enumerate((400, 410, 420, 430, 440, 450, 460, 470)):
        day = _TONIGHT - timedelta(days=8 - i)
        start = day + timedelta(hours=22)
        rows.append({"CYCLE_DATE": day, "CYCLE_START": start, "CYCLE_FINISH": start + timedelta(minutes=minutes),
                     "N_FAILED": 0, "N_RUNNING": 0})
    rows.append({"CYCLE_DATE": _TONIGHT, "CYCLE_START": _TONIGHT + timedelta(hours=22, minutes=30),
                 "CYCLE_FINISH": pd.NaT, "N_FAILED": 0, "N_RUNNING": 1})
    return pd.DataFrame(rows).assign(SNAPSHOT_TS=_SNAP, RN=range(9, 0, -1))


def _night_frame() -> pd.DataFrame:
    """The whole-night roll-up with the ETA columns: the starter finished (the pace marker, 10 min late), a
    middle workflow still running, the terminal pending."""
    cols = list(_shaped_from_sql(etl_control_sql.cycle_night_health_scan(
        _FQN, start_workflow=_START_WF, end_workflow=_END_WF)).df.columns)
    base = {"CYCLE_DATE": _TONIGHT, "CYCLE_START_AT": _TONIGHT + timedelta(hours=22, minutes=30),
            "CYCLE_AGE_SEC": 9000.0, "NEXT_CYCLE_OVERDUE": 0, "SNAPSHOT_TS": _SNAP,
            "PACE_WORKFLOW_NAME": _START_WF, "PACE_LATE_SEC": 600.0,
            "PACE_END_AT": _TONIGHT + timedelta(hours=23)}
    rows = [
        {**base, "WORKFLOW_NAME": "WF_MIDDLE", "NIGHT_STATUS": "RUNNING", "TASK_COUNT": 3, "FAILED_TASK_COUNT": 0,
         "RUNNING_TASK_COUNT": 1, "FIRST_START_AT": _TONIGHT + timedelta(hours=23, minutes=5),
         "LAST_END_AT": pd.NaT, "NIGHTS_RAN_COUNT": 14, "TYPICAL_OFFSET_SEC": 1800.0, "START_OFFSET_SEC": 2100.0,
         "END_OFFSET_SEC": None, "TYPICAL_END_OFFSET_SEC": 14000.0, "END_NIGHTS_COUNT": 13},
        {**base, "WORKFLOW_NAME": _END_WF, "NIGHT_STATUS": "PENDING", "TASK_COUNT": 0, "FAILED_TASK_COUNT": 0,
         "RUNNING_TASK_COUNT": 0, "FIRST_START_AT": pd.NaT, "LAST_END_AT": pd.NaT, "NIGHTS_RAN_COUNT": 14,
         "TYPICAL_OFFSET_SEC": 21000.0, "START_OFFSET_SEC": None, "END_OFFSET_SEC": None,
         "TYPICAL_END_OFFSET_SEC": 25000.0, "END_NIGHTS_COUNT": 14},
        {**base, "WORKFLOW_NAME": _START_WF, "NIGHT_STATUS": "OK", "TASK_COUNT": 2, "FAILED_TASK_COUNT": 0,
         "RUNNING_TASK_COUNT": 0, "FIRST_START_AT": _TONIGHT + timedelta(hours=22, minutes=30),
         "LAST_END_AT": _TONIGHT + timedelta(hours=23), "NIGHTS_RAN_COUNT": 14, "TYPICAL_OFFSET_SEC": 0.0,
         "START_OFFSET_SEC": 0.0, "END_OFFSET_SEC": 1800.0, "TYPICAL_END_OFFSET_SEC": 1200.0,
         "END_NIGHTS_COUNT": 14},
    ]
    df = pd.DataFrame(rows).reindex(columns=cols)
    df["TOTAL_WORKFLOWS"] = 3
    for col, n in (("TOTAL_FAILED_TASKS", 0), ("TOTAL_FAILED_WF", 0), ("TOTAL_MISSING_WF", 0),
                   ("TOTAL_RUNNING_WF", 1), ("TOTAL_PENDING_WF", 1)):
        df[col] = n
    return df


def _etl_configured(monkeypatch, *, fail_enriched: bool = False) -> list[str]:
    """ETL configured on every page; the shared night read (attention.run) returns the crafted in-flight night
    (or FAILS for the enriched SQL when ``fail_enriched``), the cycle-finish reads return the in-flight history."""
    from app.ui import attention, components
    from app.ui.pages import brief, control_room, operations

    settings = dict(DEFAULT_SETTINGS)
    settings.update({"_source": "stub", "ETL_CONTROL_STATUS_FQN": _FQN})
    for mod in (brief, control_room, operations, components):
        monkeypatch.setattr(mod, "load_settings", lambda _page: dict(settings))
    finish_sql = etl_control_sql.cycle_finish_history_scan(_FQN, start_workflow=_START_WF, end_workflow=_END_WF)
    keys: list[str] = []

    def _attn_run(sql, **kwargs):
        keys.append(kwargs.get("key", ""))
        if "pace AS (" in sql:
            if fail_enriched:
                return QueryResult(df=pd.DataFrame(), ok=False, error="invalid identifier 'PACE_LATE_SEC'",
                                   error_kind="missing_column", source="stub")
            return QueryResult(df=_night_frame(), ok=True, source="stub")
        if sql == finish_sql:
            return QueryResult(df=_cycle_finish_frame(), ok=True, source="stub")
        if "NIGHT_STATUS" in sql:          # the pre-#36 fallback roll-up
            return QueryResult(df=_night_frame().drop(columns=[
                "START_OFFSET_SEC", "END_OFFSET_SEC", "TYPICAL_END_OFFSET_SEC", "END_NIGHTS_COUNT",
                "PACE_WORKFLOW_NAME", "PACE_LATE_SEC", "PACE_END_AT"]), ok=True, source="stub")
        return _shaped_run(sql, **kwargs)

    def _ops_batch(specs, **kwargs):
        out = _shaped_batch(specs, **kwargs)
        if "cycle_finish" in out:
            out["cycle_finish"] = QueryResult(df=_cycle_finish_frame(), ok=True, source="stub")
        return out

    monkeypatch.setattr(attention, "run", _attn_run)
    monkeypatch.setattr(attention, "record_error", lambda *a, **k: "stub")
    monkeypatch.setattr(operations, "run_batch", _ops_batch)
    return keys


def _tonight(at_state: dict | None = None) -> AppTest:
    at = AppTest.from_function(_entry, default_timeout=30)
    at.run()
    assert not at.exception
    _nav_to(at, "Operations")
    at.session_state["ops_section"] = "Pipeline SLA"
    for k, v in (at_state or {}).items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, f"tonight (shaped): {at.exception}"
    assert not any("could not finish rendering" in str(getattr(e, "value", "")) for e in at.error)
    return at


@_SKIP
def test_tonight_projected_finish_and_timeline_render_shaped(monkeypatch):
    _etl_configured(monkeypatch)
    at = _tonight()
    blob = _texts(at)
    assert "Tonight at a glance" in blob
    assert "Tonight's projected finish" in blob, "the ETA section did not paint"
    assert "Projected finish" in blob and "Pace so far" in blob and "Projected vs 07:00" in blob
    assert "~05:45" in blob and "usual range 05:28–06:02" in blob                         # the projection and its range
    assert "How this differs from the PIPE_ETL_CYCLE_LATE alert" in blob
    assert "Cycle timeline" in blob
    toggles = [str(t.key) for t in at.toggle]
    assert "ops_cycle_timeline_toggle" in toggles
    before = len(at.dataframe)
    at.toggle(key="ops_cycle_timeline_toggle").set_value(True)
    at.run()
    assert not at.exception, f"cycle timeline (shaped): {at.exception}"
    assert not any("could not finish rendering" in str(getattr(e, "value", "")) for e in at.error)
    assert len(at.dataframe) == before + 1
    frames = [d.value for d in at.dataframe]
    assert any("TIMELINE_NOTE" in getattr(f, "columns", []) for f in frames), "the timeline table did not render"


@_SKIP
def test_failed_eta_columns_keep_the_glance(monkeypatch):
    keys = _etl_configured(monkeypatch, fail_enriched=True)
    at = _tonight()
    blob = _texts(at)
    assert "Tonight at a glance" in blob and "Running" in blob          # the whole-night roll-up survived
    assert "attn_cycle_night_base" in keys                              # ... through the pre-#36 fallback
    assert "Cycle timeline" not in blob                                 # no offsets -> the timeline hides
    assert "Pace so far" in blob and "pace unavailable from tonight's roll-up" in blob   # ETA without pace
    assert "no upstream workflow has finished yet" not in blob                         # unknown, not "not yet"


@_SKIP
def test_brief_tile_shows_the_projection(monkeypatch):
    _etl_configured(monkeypatch)
    at = AppTest.from_function(_entry, default_timeout=30)
    at.run()
    assert not at.exception
    _nav_to(at, "Brief")
    at.run()
    assert not at.exception, f"brief (shaped): {at.exception}"
    assert "projected ~05:45 (05:28–06:02)" in _texts(at)


# --- #30: Storage & waste --------------------------------------------------------------------------------

def _storage(at_state: dict | None = None) -> AppTest:
    at = AppTest.from_function(_entry, default_timeout=30)
    at.run()
    assert not at.exception
    _nav_to(at, "Cost Intelligence")
    at.session_state["cost_section"] = "Optimization & Savings"
    at.session_state["opt_section"] = "Storage & waste"
    for k, v in (at_state or {}).items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, f"storage & waste (shaped): {at.exception}"
    assert not any("could not finish rendering" in str(getattr(e, "value", "")) for e in at.error)
    return at


def _recording(monkeypatch, *, frames: dict | None = None) -> tuple[list[str], list[str]]:
    from app.ui.pages.cost_parts import optimize
    sqls: list[str] = []
    writes: list[str] = []

    def _run(sql, **kwargs):
        sqls.append(sql)
        for marker, df in (frames or {}).items():
            if marker in sql:
                return QueryResult(df=df, ok=True, source="stub")
        return _shaped_run(sql, **kwargs)

    def _record_write(sql, **_kwargs):
        writes.append(sql)
        return True, "stubbed"

    monkeypatch.setattr(optimize, "run", _run)
    monkeypatch.setattr(optimize, "execute_statement", _record_write)
    return sqls, writes


@_SKIP
def test_unread_maintenance_is_off_by_default_and_renders_shaped(monkeypatch):
    sqls, _ = _recording(monkeypatch)
    at = _storage()
    assert "cost_unread_maint_toggle" in [str(t.key) for t in at.toggle]
    assert not [s for s in sqls if "MAINT_CREDITS_30D" in s or "OBJECTS_MODIFIED" in s]   # nothing on first paint
    at.toggle(key="cost_unread_maint_toggle").set_value(True)
    at.run()
    assert not at.exception, f"unread maintenance (shaped): {at.exception}"
    assert not any("could not finish rendering" in str(getattr(e, "value", "")) for e in at.error)
    assert [s for s in sqls if "MAINT_CREDITS_30D" in s], "the mart shortlist did not run"
    assert [s for s in sqls if "OBJECTS_MODIFIED" in s], "the access-history confirm did not run"
    blob = _texts(at)
    assert "Maintenance on objects nobody reads" in blob and "Mart shortlist" in blob


_SHORT = pd.DataFrame([{
    "OBJECT_FQN": "DB.S.T1", "OBJECT_DOMAIN": "TABLE", "COMPANY": "ALFA", "CLUSTERING_CREDITS": 30.0,
    "SEARCH_OPT_CREDITS": 0.0, "MV_REFRESH_CREDITS": 0.0, "MAINT_CREDITS": 30.0, "MAINT_CREDITS_30D": 10.0,
    "WRITE_CREDITS": 0.0, "FIRST_MAINT_DAY": pd.Timestamp("2026-07-10"), "LAST_MAINT_DAY": pd.Timestamp("2026-09-27"),
    "MAINT_DAYS": 60, "COVERAGE_START_DAY": pd.Timestamp("2026-07-01"), "LEDGER_LAST_DAY": pd.Timestamp("2026-09-27"),
    "CANDIDATES_WIN": 1, "MAINT_CREDITS_WIN": 30.0, "MAINT_CREDITS_30D_WIN": 10.0}])
_READS = pd.DataFrame([{"OBJECT_FQN": "DB.S.T1", "MATCHED_BY_ID": True, "SHARED_DATABASE": False,
                        "READ_QUERIES": 0, "READ_USERS": 0, "LAST_READ": None, "WRITE_QUERIES": 2}])


@_SKIP
def test_operator_books_a_confirmed_unread_object(monkeypatch):
    _, writes = _recording(monkeypatch, frames={"MAINT_CREDITS_30D": _SHORT, "OBJECTS_MODIFIED": _READS})
    at = _storage({"cost_unread_maint_toggle": True, "unread_maint_sel_last": "DB.S.T1",
                   "_ow_current_role": "SNOW_SYSADMINS"})
    codes = [str(c.value) for c in at.code]
    assert "ALTER TABLE DB.S.T1 SUSPEND RECLUSTER;" in codes           # the bulk review + the selected row
    assert any(c.startswith("INSERT INTO DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER") for c in codes)
    blob = _texts(at)
    assert "Also written by 2 queries in 90 days" in blob and "Book only after the ALTER above has run" in blob
    book = [b for b in at.button if str(b.label) == "Book estimated saving"]
    assert book, [str(b.label) for b in at.button]
    book[0].click()
    at.run()
    assert not at.exception, f"booking (shaped): {at.exception}"
    assert len(writes) == 1 and "'ESTIMATED'" in writes[0] and "'SUSPEND_RECLUSTER'" in writes[0]
    assert "WHERE NOT EXISTS" in writes[0]
