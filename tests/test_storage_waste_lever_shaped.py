"""Next-Fifty #35 storage leg on the rendered pages (AppTest, streamlit >= 1.55 like the rest of the shaped harness;
the floor leg skips these, and tests/test_storage_waste_lever.py locks the same wiring by source).

Cost ▸ Optimization & Savings ▸ Storage & waste — the storage-waste scan is OFF by default (no read on first paint);
on, its read-evidence rows get a LEVER and publish the session handoff that Idle & sizing and Proof ▸ Pipeline count in
Addressable $/mo (zero reads there). A scan without read evidence, a failed ledger read, and a booked retention keep
tables out and say so; a table counted by unread maintenance too counts once."""

from __future__ import annotations

import pandas as pd
import pytest
from test_pages_shaped import (  # noqa: F401 - _stub_shaped is the harness's autouse fixture
    _APPTEST_BUTTONGROUP_OK,
    _entry,
    _nav_to,
    _shaped_from_sql,
    _shaped_run,
    _stub_shaped,
)
from test_prc_c2_shaped import (
    _LEDGER,
    _READS,
    _SHORT,
    _addressable_rows,
    _card,
    _frames,
    _idle_and_sizing,
    _recording,
    _storage,
)

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from app.core.result import QueryResult
from app.data import insights_sql
from app.logic.savings_rollup import STORAGE_HANDOFF_KEY, UNREAD_HANDOFF_KEY

_SKIP = pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")

_RECLAIM_MARK = "AS OLDER_THAN_90D"              # only insights_sql.storage_reclaim emits it
_LEDGER_MARK = "REMEASURED_14D_MONTHLY_USD"      # mart_sql.savings_ledger
_RECLAIM_COLS = list(_shaped_from_sql(insights_sql.storage_reclaim("ALL")).df.columns)


def _row(db: str, schema: str, table: str, **kw) -> dict:
    row = {"DATABASE_NAME": db, "SCHEMA_NAME": schema, "TABLE_NAME": table, "ACTIVE_GB": 0.0,
           "TIME_TRAVEL_GB": 0.0, "FAILSAFE_GB": 5.0, "CLONE_RETAINED_GB": 0.0, "RETENTION_DAYS": 1.0,
           "RETENTION_KNOWN": True, "LAST_DML": pd.NaT, "LAST_READ": pd.NaT, "DML_STATUS": "STALE",
           "NEVER_READ": True, "SHARED_DATABASE": False, "OLDER_THAN_90D": True, "CLONE_GROUP_LIVE": 1}
    row.update(kw)
    return row


def _reclaim(*rows: dict) -> pd.DataFrame:
    return pd.DataFrame(list(rows)).reindex(columns=_RECLAIM_COLS)


# a stale unread table (Archive or drop: 1 TiB active = $23.00/mo at the $23 default), a written unread one (Cut
# retention: 102.4 GB TT x (5 - 1) / 5 = $1.84/mo), one read in 90 days (Keep), one in a shared-out database, and a
# stale unread clone source (Check clones, unpriced: another live table shares its clone group)
_RECLAIM = _reclaim(
    _row("DB", "S", "STALE_T", ACTIVE_GB=1024.0),
    _row("DB", "S", "WRITTEN_T", DML_STATUS="ACTIVE", TIME_TRAVEL_GB=102.4, RETENTION_DAYS=5.0, ACTIVE_GB=50.0,
         LAST_DML=pd.Timestamp("2026-09-20")),
    _row("DB", "S", "READ_T", NEVER_READ=False, ACTIVE_GB=300.0, LAST_READ=pd.Timestamp("2026-09-25")),
    _row("SHR", "S", "T", SHARED_DATABASE=True, ACTIVE_GB=200.0),
    _row("DB", "S", "CLONED_T", ACTIVE_GB=2048.0, CLONE_GROUP_LIVE=2),
)
_FRAMES = {_RECLAIM_MARK: _RECLAIM, _LEDGER_MARK: _LEDGER}
_ROWS = [["STORAGE", "DB.S.STALE_T", 23.0, 0.6], ["RETENTION", "DB.S.WRITTEN_T", 1.84, 0.6]]


def _texts(at) -> str:
    return (" ".join(str(e.value) for e in list(at.markdown) + list(at.caption) + list(at.warning)
                     + list(at.info) + list(at.error))
            .replace("&#x27;", "'").replace("&#39;", "'").replace("\\$", "$"))


def _proof_pipeline(at) -> AppTest:
    _nav_to(at, "Proof")
    at.session_state["decision_section"] = "Pipeline"
    at.run()
    assert not at.exception, f"proof pipeline (shaped): {at.exception}"
    assert not any("could not finish rendering" in str(getattr(e, "value", "")) for e in at.error)
    return at


@_SKIP
def test_storage_waste_scan_is_off_by_default(monkeypatch):
    sqls, _ = _recording(monkeypatch)
    at = _storage()
    assert "cost_waste_toggle" in [str(t.key) for t in at.toggle]
    assert not [s for s in sqls if "NEVER_READ" in s]                     # no read on first paint
    assert STORAGE_HANDOFF_KEY not in at.session_state


@_SKIP
def test_unread_tables_join_the_addressable_headline(monkeypatch):
    sqls, _ = _recording(monkeypatch, frames=_FRAMES)
    at = _storage({"cost_waste_toggle": True})
    handoff = at.session_state[STORAGE_HANDOFF_KEY]
    assert handoff["status"] == "confirmed" and handoff["rows"] == _ROWS and handoff["booked_excluded"] == 0
    blob = _texts(at)
    assert "2 unread table(s) join Addressable $/mo in Idle & sizing and on Proof ▸ Pipeline" in blob
    assert "LEVER says whether a table qualifies for Addressable $/mo" in blob
    assert "Archive or drop is a floor" not in blob                         # 5 rows: not the top-50 floor
    frame = next(f for f in _frames(at) if "LEVER" in f.columns)
    assert list(frame["LEVER"]) == ["Archive or drop", "Cut retention", "Keep", "Check share consumers",
                                    "Check clones"]
    assert list(frame.columns[:5]) == ["DATABASE_NAME", "SCHEMA_NAME", "TABLE_NAME", "LEVER", "EST_MONTHLY_USD"]
    value, card = _card(at, "Reclaimable $/mo (unread tables)")
    assert value == "$24.84", card
    assert [s for s in sqls if _LEDGER_MARK in s], "the booked-tables ledger read did not run"
    issued = _idle_and_sizing(at, sqls)
    blob = _texts(at)
    assert "Levers counted: idle timer + storage waste." in blob
    assert "unread maintenance (not checked this session: run the unread-maintenance scan in Storage & waste)" in blob
    rows = _addressable_rows(at)
    hit = rows[rows["Warehouse / target"].isin(["DB.S.STALE_T", "DB.S.WRITTEN_T"])]
    assert sorted(zip(hit["Source"], hit["Warehouse / target"], hit["Effort"], strict=True)) == [
        ("RETENTION", "DB.S.WRITTEN_T", "MEDIUM"), ("STORAGE", "DB.S.STALE_T", "MEDIUM")]
    # the headline read nothing new: no storage scan and no ledger read on that run
    assert not [s for s in issued if "NEVER_READ" in s or _LEDGER_MARK in s]


@_SKIP
def test_no_read_evidence_keeps_storage_out(monkeypatch):
    from app.ui.pages.cost_parts import optimize
    sqls: list[str] = []

    def _run(sql, **kwargs):
        sqls.append(sql)
        if _RECLAIM_MARK in sql:
            return QueryResult(df=pd.DataFrame(), ok=False, error_kind="timeout", source="stub",
                               error="Statement reached its statement or warehouse timeout of 180 second(s).")
        return _shaped_run(sql, **kwargs)

    monkeypatch.setattr(optimize, "run", _run)
    at = _storage({"cost_waste_toggle": True})
    blob = _texts(at)
    assert "Read evidence is unavailable: the 90-day access-history read timed out." in blob
    assert "needs Enterprise edition" not in blob
    assert "Not added to Addressable $/mo: without read evidence no table is confirmed unread." in blob
    assert at.session_state[STORAGE_HANDOFF_KEY]["status"] == "no_reads"
    assert not any("LEVER" in f.columns for f in _frames(at))              # the DML-only frame gets no LEVER
    assert not [s for s in sqls if _LEDGER_MARK in s]                       # nothing to count: no ledger read
    _idle_and_sizing(at, sqls)
    assert ("storage waste (the storage-waste scan had no read evidence, so no table is confirmed unread)"
            in _texts(at))


@_SKIP
def test_a_failed_ledger_read_keeps_storage_out_and_says_so(monkeypatch):
    from app.ui.pages.cost_parts import optimize
    sqls: list[str] = []

    def _run(sql, **kwargs):
        sqls.append(sql)
        if _LEDGER_MARK in sql:
            return QueryResult(df=pd.DataFrame(), ok=False, error="SAVINGS_LEDGER read failed", source="stub")
        if _RECLAIM_MARK in sql:
            return QueryResult(df=_RECLAIM, ok=True, source="stub")
        return _shaped_run(sql, **kwargs)

    monkeypatch.setattr(optimize, "run", _run)
    at = _storage({"cost_waste_toggle": True})
    assert any("The Savings ledger could not be read, so unread tables are not added" in str(e.value)
               for e in at.error)                                           # unavailable, never clean
    assert at.session_state[STORAGE_HANDOFF_KEY]["status"] == "ledger_failed"
    assert "unread table(s) join" not in _texts(at)
    _idle_and_sizing(at, sqls)
    assert ("storage waste (the Savings ledger could not be read, so objects already booked there could not be "
            "left out)") in _texts(at)
    assert not set(_addressable_rows(at)["Warehouse / target"]) & {"DB.S.STALE_T", "DB.S.WRITTEN_T"}


@_SKIP
def test_a_booked_retention_leaves_the_table_out(monkeypatch):
    ledger = pd.DataFrame([{"TARGET_OBJECT": "DB.S.WRITTEN_T", "FINDING_TYPE": "RETENTION", "STATE": "ESTIMATED"}])
    sqls, _ = _recording(monkeypatch, frames={_RECLAIM_MARK: _RECLAIM, _LEDGER_MARK: ledger})
    at = _storage({"cost_waste_toggle": True})
    handoff = at.session_state[STORAGE_HANDOFF_KEY]
    assert handoff["rows"] == [["STORAGE", "DB.S.STALE_T", 23.0, 0.6]] and handoff["booked_excluded"] == 1
    assert "1 already booked on the Savings ledger are left out." in _texts(at)
    _idle_and_sizing(at, sqls)
    targets = list(_addressable_rows(at)["Warehouse / target"])
    assert "DB.S.STALE_T" in targets and "DB.S.WRITTEN_T" not in targets
    assert "storage waste (1 already booked on the Savings ledger left out)" in _texts(at)


@_SKIP
def test_one_saving_per_table_with_unread_maintenance(monkeypatch):
    t1 = _reclaim(_row("DB", "S", "T1", DML_STATUS="ACTIVE", TIME_TRAVEL_GB=102.4, RETENTION_DAYS=5.0,
                       ACTIVE_GB=10.0))
    sqls, _ = _recording(monkeypatch, frames={"MAINT_CREDITS_30D": _SHORT, "OBJECTS_MODIFIED": _READS,
                                              _LEDGER_MARK: _LEDGER, _RECLAIM_MARK: t1})
    at = _storage({"cost_unread_maint_toggle": True, "cost_waste_toggle": True})
    assert at.session_state[UNREAD_HANDOFF_KEY]["rows"] == [["DB.S.T1", 36.8, 0.6]]
    assert at.session_state[STORAGE_HANDOFF_KEY]["rows"] == [["RETENTION", "DB.S.T1", 1.84, 0.6]]
    _idle_and_sizing(at, sqls)
    rows = _addressable_rows(at)
    hit = rows[rows["Warehouse / target"] == "DB.S.T1"]
    assert list(hit["Source"]) == ["UNREAD_MAINT"]                         # the larger counts once
    value, card = _card(at, "Overlaps removed")
    assert value == "1", card
    assert "Levers counted: idle timer + unread maintenance + storage waste." in _texts(at)


@_SKIP
def test_proof_pipeline_counts_storage_and_offers_its_doorway(monkeypatch):
    # (a) after the scan: counted, an OBJECT row per table, no storage doorway
    _recording(monkeypatch, frames=_FRAMES)
    at = _proof_pipeline(_storage({"cost_waste_toggle": True}))
    captions = " ".join(str(c.value) for c in at.caption).replace("\\$", "$")
    assert "Levers counted: idle timer + storage waste." in captions
    keys = [str(b.key) for b in at.button]
    assert "proof_link_storage" not in keys and "proof_link_unread" in keys
    pipe = [f for f in _frames(at) if {"SOURCE_ENTITY_TYPE", "SOURCE_ENTITY_KEY"} <= set(f.columns)]
    assert pipe, "the pipeline table did not render"
    hit = pipe[0][pipe[0]["SOURCE_ENTITY_KEY"] == "DB.S.STALE_T"]
    assert list(hit["SOURCE_ENTITY_TYPE"]) == ["OBJECT"]
    assert list(hit["TITLE"]) == ["Archive or drop unread DB.S.STALE_T"]
    written = pipe[0][pipe[0]["SOURCE_ENTITY_KEY"] == "DB.S.WRITTEN_T"]
    assert list(written["TITLE"]) == ["Cut Time Travel retention on unread DB.S.WRITTEN_T"]


@_SKIP
def test_proof_pipeline_offers_the_storage_doorway_when_unread_is_counted(monkeypatch):
    # (b) an unread handoff only: the unread lever is counted, so the storage-waste doorway takes its place
    _recording(monkeypatch, frames={"MAINT_CREDITS_30D": _SHORT, "OBJECTS_MODIFIED": _READS, _LEDGER_MARK: _LEDGER})
    at = _proof_pipeline(_storage({"cost_unread_maint_toggle": True}))
    captions = " ".join(str(c.value) for c in at.caption)
    assert ("storage waste (not checked this session: run the storage-waste scan in Cost ▸ Optimization & Savings ▸ "
            "Storage & waste)") in captions
    keys = [str(b.key) for b in at.button]
    assert "proof_link_unread" not in keys
    door = [b for b in at.button if str(b.key) == "proof_link_storage"]
    assert door and str(door[0].label) == "Check storage waste → Cost ▸ Optimization & Savings ▸ Storage & waste"
    at.session_state["opt_section"] = "Idle & sizing"
    door[0].click()
    at.run()
    assert not at.exception, f"doorway (shaped): {at.exception}"
    assert at.session_state["opt_section"] == "Storage & waste"


@_SKIP
def test_proof_pipeline_without_any_scan_offers_only_the_unread_doorway():
    # (c) neither handoff: the unread doorway already opens the same section
    at = AppTest.from_function(_entry, default_timeout=30)
    at.run()
    assert not at.exception
    at = _proof_pipeline(at)
    keys = [str(b.key) for b in at.button]
    assert "proof_link_unread" in keys and "proof_link_storage" not in keys
    captions = " ".join(str(c.value) for c in at.caption)
    assert "storage waste (not checked this session: run the storage-waste scan in Cost ▸" in captions
