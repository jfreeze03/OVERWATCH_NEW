"""Next-Fifty #46(d): the measured before/after behind a MANUAL savings-ledger verify.

Pure measurement, the mart builder's shape, the canaries, and the Optimize ▸ Savings ledger wiring (the
prefill sentinel, PROOF_* on the measured branch only). Runs on the floor-compat leg; the rendered
verify is in test_prc_c1_shaped.py."""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest
import sqlglot

from app.data import canary, mart_sql
from app.logic.ledger_measure import (
    MEASURED,
    NO_DATA,
    OBJECT_FINDING_TYPES,
    PROOF_RESULT_MAX_CHARS,
    TB,
    TOO_EARLY,
    ledger_basis,
    ledger_measurement,
    ledger_overlaps,
    proof_result_json,
    verify_prefill,
)
from tests._source import read

_TODAY = date(2026, 9, 29)
_BOOKED = date(2026, 9, 1)


def _wh_row(**over) -> dict:
    row = {"BASIS": "WAREHOUSE", "BOOKED_DAY": _BOOKED, "BEFORE_DAYS": 14, "MAX_AFTER_DAYS": 30,
           "BEFORE_CREDITS": 1400.0, "AFTER_CREDITS": 1350.0, "BEFORE_QUERIES": 14000.0,
           "AFTER_QUERIES": 27000.0, "LOADED_THROUGH": date(2026, 9, 28)}
    row.update(over)
    return row


def test_ledger_basis_mapping():
    for ft in ("SCHEDULE", "AUTO_SUSPEND", "STATEMENT_TIMEOUT", "RESIZE", "MAX_CLUSTERS", "resize"):
        assert ledger_basis(ft) == "WAREHOUSE", ft
    assert ledger_basis("RETENTION") == "TABLE"
    for ft in OBJECT_FINDING_TYPES:
        assert ledger_basis(ft) == "OBJECT"
    assert {"SUSPEND_RECLUSTER", "DROP_SEARCH_OPTIMIZATION", "SUSPEND_MV_REFRESH"} == OBJECT_FINDING_TYPES
    for ft in ("unclassified", "EXPERIMENT", "", None, "free text"):
        assert ledger_basis(ft) is None


def test_warehouse_measurement_math():
    m = ledger_measurement(_wh_row(), basis="WAREHOUSE", rate=3.68, storage_usd_per_tb=23.0, today=_TODAY)
    # after window: min(loaded Sep 28, yesterday Sep 28, booked+30 Oct 1) = Sep 28 -> 27 complete days
    assert m["state"] == MEASURED and m["after_days"] == 27 and m["after_end"] == date(2026, 9, 28)
    assert m["before_per_day"] == 100.0 and m["after_per_day"] == 50.0
    assert m["monthly_usd"] == round((100 - 50) * 30 * 3.68, 2) == m["prefill_usd"]
    assert m["volume_ratio"] == 1.0 and m["confounded"] is False       # 1000/day both sides


def test_warehouse_measurement_flags_volume_confounding_and_bounds_by_the_loader():
    m = ledger_measurement(_wh_row(AFTER_QUERIES=54000.0), basis="WAREHOUSE", rate=3.68,
                           storage_usd_per_tb=23.0, today=_TODAY)
    assert m["volume_ratio"] == 2.0 and m["confounded"] is True and "volume-confounded" in m["note"]
    # a stalled loader bounds the after window: never a saving read from missing days
    stalled = ledger_measurement(_wh_row(LOADED_THROUGH=date(2026, 9, 4)), basis="WAREHOUSE", rate=3.68,
                                 storage_usd_per_tb=23.0, today=_TODAY)
    assert stalled["state"] == TOO_EARLY and stalled["after_days"] == 3 and stalled["prefill_usd"] == 0.0
    # the after window never runs past booking + 30
    late = ledger_measurement(_wh_row(), basis="WAREHOUSE", rate=3.68, storage_usd_per_tb=23.0,
                              today=date(2026, 12, 1))
    assert late["after_days"] == 27 and late["after_end"] == date(2026, 9, 28)   # still loader-bounded
    capped = ledger_measurement(_wh_row(LOADED_THROUGH=date(2026, 11, 30)), basis="WAREHOUSE", rate=3.68,
                                storage_usd_per_tb=23.0, today=date(2026, 12, 1))
    assert capped["after_days"] == 30


def test_too_early_no_data_and_a_rise_prefill_zero():
    early = ledger_measurement(_wh_row(LOADED_THROUGH=date(2026, 9, 7)), basis="WAREHOUSE", rate=3.68,
                               storage_usd_per_tb=23.0, today=date(2026, 9, 8))
    assert early["state"] == TOO_EARLY and early["after_days"] == 6 and early["monthly_usd"] is None
    none = ledger_measurement(_wh_row(BEFORE_CREDITS=None), basis="WAREHOUSE", rate=3.68,
                              storage_usd_per_tb=23.0, today=_TODAY)
    assert none["state"] == NO_DATA and none["prefill_usd"] == 0.0
    rose = ledger_measurement(_wh_row(AFTER_CREDITS=5400.0), basis="WAREHOUSE", rate=3.68,
                              storage_usd_per_tb=23.0, today=_TODAY)
    assert rose["state"] == MEASURED and rose["monthly_usd"] < 0 and rose["prefill_usd"] == 0.0
    assert "ROSE" in rose["note"]
    assert ledger_measurement(_wh_row(BOOKED_DAY=pd.NaT), basis="WAREHOUSE", rate=3.68,
                              storage_usd_per_tb=23.0, today=_TODAY)["state"] == NO_DATA
    with pytest.raises(ValueError):
        ledger_measurement(_wh_row(), basis="QUERY", rate=3.68, storage_usd_per_tb=23.0, today=_TODAY)


def test_object_measurement_has_no_volume():
    m = ledger_measurement(_wh_row(BASIS="OBJECT", BEFORE_QUERIES=None, AFTER_QUERIES=None,
                                   BEFORE_CREDITS=28.0, AFTER_CREDITS=0.0),
                           basis="OBJECT", rate=3.0, storage_usd_per_tb=23.0, today=_TODAY)
    assert m["state"] == MEASURED and m["monthly_usd"] == round(2.0 * 30 * 3.0, 2)
    assert m["volume_ratio"] is None and m["confounded"] is False and "Maintenance credits" in m["note"]


def test_table_measurement():
    row = {"BASIS": "TABLE", "BOOKED_DAY": pd.Timestamp("2026-09-01"), "BEFORE_TT_BYTES": 3 * TB,
           "BEFORE_RETENTION_DAYS": 30, "BEFORE_SNAPSHOT_DAY": date(2026, 8, 31), "AFTER_TT_BYTES": 1 * TB,
           "AFTER_RETENTION_DAYS": 1, "AFTER_SNAPSHOT_DAY": date(2026, 9, 12), "LOADED_THROUGH": date(2026, 9, 12)}
    m = ledger_measurement(row, basis="TABLE", rate=3.68, storage_usd_per_tb=23.0, today=_TODAY)
    assert m["state"] == MEASURED and m["monthly_usd"] == 46.0 and m["after_days"] == 11
    assert m["before_per_day"] is None and m["before_bytes"] == 3 * TB
    unchanged = ledger_measurement({**row, "AFTER_RETENTION_DAYS": 30}, basis="TABLE", rate=3.68,
                                   storage_usd_per_tb=23.0, today=_TODAY)
    assert "check that the ALTER ran" in unchanged["note"]
    gone = ledger_measurement({**row, "BEFORE_TT_BYTES": None}, basis="TABLE", rate=3.68,
                              storage_usd_per_tb=23.0, today=_TODAY)
    assert gone["state"] == NO_DATA and "two weeks" in gone["note"]
    early = ledger_measurement({**row, "AFTER_SNAPSHOT_DAY": date(2026, 9, 4)}, basis="TABLE", rate=3.68,
                               storage_usd_per_tb=23.0, today=_TODAY)
    assert early["state"] == TOO_EARLY


def test_proof_result_json_is_bounded_json():
    m = ledger_measurement(_wh_row(), basis="WAREHOUSE", rate=3.68, storage_usd_per_tb=23.0, today=_TODAY)
    text = proof_result_json(m, target="WH_A", basis="WAREHOUSE", entered_usd=5000.0, sql_hash="abc123")
    doc = json.loads(text)
    assert doc["kind"] == "ledger_before_after" and doc["basis"] == "WAREHOUSE" and doc["target"] == "WH_A"
    assert doc["entered_usd"] == 5000.0 and doc["prefill_usd"] == m["prefill_usd"] and doc["sql_hash"] == "abc123"
    assert doc["after_end"] == "2026-09-28" and doc["state"] == MEASURED
    huge = proof_result_json({**m, "note": "x" * 50_000}, target="T" * 5000, basis="WAREHOUSE",
                             entered_usd=float("nan"), sql_hash="h")
    assert len(huge) <= PROOF_RESULT_MAX_CHARS and json.loads(huge)["entered_usd"] is None


# ---------------------------------------------------------------------------
# the mart builder
# ---------------------------------------------------------------------------

_CREDIT_COLS = ["BASIS", "BOOKED_DAY", "BEFORE_DAYS", "MAX_AFTER_DAYS", "BEFORE_CREDITS", "AFTER_CREDITS",
                "BEFORE_QUERIES", "AFTER_QUERIES", "LOADED_THROUGH"]
_TABLE_COLS = ["BASIS", "BOOKED_DAY", "BEFORE_TT_BYTES", "BEFORE_RETENTION_DAYS", "BEFORE_SNAPSHOT_DAY",
               "AFTER_TT_BYTES", "AFTER_RETENTION_DAYS", "AFTER_SNAPSHOT_DAY", "LOADED_THROUGH"]


@pytest.mark.parametrize("basis,cols", [("WAREHOUSE", _CREDIT_COLS), ("OBJECT", _CREDIT_COLS),
                                        ("TABLE", _TABLE_COLS)])
def test_ledger_before_after_shape(basis, cols):
    sql = mart_sql.ledger_before_after(basis, "db.s.t", _BOOKED)
    assert sqlglot.parse_one(sql, read="snowflake").named_selects == cols
    assert "ACCOUNT_USAGE" not in sql and "CURRENT_DATE" not in sql
    assert "CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE AS TODAY_DAY" in sql
    assert "TO_DATE('2026-09-01') AS BOOKED_DAY" in sql


def test_ledger_before_after_windows_and_keys():
    wh = mart_sql.ledger_before_after("WAREHOUSE", "wh_a", _BOOKED)
    assert "UPPER(f.WAREHOUSE_NAME) = 'WH_A'" in wh and "FACT_WAREHOUSE_DAILY" in wh
    assert "DATEADD('day', -14, w.BOOKED_DAY)" in wh and "DATEADD('day', 30, w.BOOKED_DAY)" in wh
    assert "f.DAY < w.TODAY_DAY" in wh and "m.DAY <= l.LOADED_THROUGH" in wh
    assert "MART_WAREHOUSE_EFFICIENCY_DAILY" in wh
    obj = mart_sql.ledger_before_after("OBJECT", 'DB."S".t', _BOOKED)
    assert "UPPER(REPLACE(f.OBJECT_FQN, '\"', '')) = 'DB.S.T'" in obj
    assert "f.COST_ARM IN ('CLUSTERING', 'SEARCH_OPT', 'MV_REFRESH')" in obj
    assert "NULL AS BEFORE_QUERIES" in obj and "MART_WAREHOUSE_EFFICIENCY_DAILY" not in obj
    tbl = mart_sql.ledger_before_after("TABLE", "db.s.t", _BOOKED)
    assert "UPPER(s.DATABASE_NAME || '.' || s.SCHEMA_NAME || '.' || s.TABLE_NAME) = 'DB.S.T'" in tbl
    assert "LEFT JOIN snap ON 1 = 1" in tbl                       # aggregate over w: always one row


def test_ledger_before_after_rejects_and_escapes():
    for bad in ("QUERY", "", None):
        with pytest.raises(ValueError):
            mart_sql.ledger_before_after(bad, "WH_A", _BOOKED)
    with pytest.raises(ValueError):
        mart_sql.ledger_before_after("WAREHOUSE", "  ", _BOOKED)
    from test_p4_filter_matrix import _strip_literals_and_comments
    for basis in ("WAREHOUSE", "OBJECT", "TABLE"):
        sql = mart_sql.ledger_before_after(basis, "X' OR 1=1; DROP TABLE T --ZZINJZZ", _BOOKED)
        residue = _strip_literals_and_comments(sql)
        assert "ZZINJZZ" not in residue and "DROP" not in residue and "'" not in residue


def test_canaries_registered_after_the_ledger_pair():
    names = [n for n, _ in canary.CANARIES]
    for basis in ("warehouse", "object", "table"):
        assert f"mart.ledger_before_after.{basis}" in names
    reg = dict(canary.CANARIES)
    for basis in ("warehouse", "object", "table"):
        sql = reg[f"mart.ledger_before_after.{basis}"]()
        assert sqlglot.parse_one(sql, read="snowflake").named_selects
    # the savings_ledger -> ledger_attribution adjacency lock is untouched
    assert names.index("mart.ledger_attribution") == names.index("mart.savings_ledger") + 1
    assert names.index("mart.ledger_before_after.warehouse") == names.index("mart.savings_verification_runs") + 1


# ---------------------------------------------------------------------------
# PR C review r1: the prefill decision (C12 / C17) and the same-target overlap check (C22). Pure, so the
# floor leg runs them too; the rendered proofs are in test_prc_c1_shaped.py.
# ---------------------------------------------------------------------------

_A, _B = "item-a", "item-b"


def test_verify_prefill_first_render_and_item_switch():
    first = verify_prefill(item_id=_A, target=6072.0, last=None, widget_value=None, clicked=False)
    assert first == {"write": 6072.0, "state": {"item": _A, "val": 6072.0}, "kept_edit": False}
    # another item takes its own prefill ...
    other = verify_prefill(item_id=_B, target=100.0, last=first["state"], widget_value=1234.0, clicked=False)
    assert other["write"] == 100.0 and other["state"] == {"item": _B, "val": 100.0}
    # ... or 0.0 when it has none: item A's amount never carries over to an unmeasured item
    bare = verify_prefill(item_id=_B, target=None, last=first["state"], widget_value=6072.0, clicked=False)
    assert bare["write"] == 0.0 and bare["state"] == {"item": _B, "val": 0.0}
    # no prefill ever and no widget yet: nothing written, the widget's own 0.0 default is the baseline
    none = verify_prefill(item_id=_A, target=None, last=None, widget_value=None, clicked=False)
    assert none == {"write": None, "state": {"item": _A, "val": 0.0}, "kept_edit": False}


def test_verify_prefill_rearms_when_the_widget_state_was_dropped():
    """C12: leaving the section drops the widget key; coming back must re-prefill, not render 0."""
    last = {"item": _A, "val": 6072.0}
    back = verify_prefill(item_id=_A, target=6072.0, last=last, widget_value=None, clicked=False)
    assert back["write"] == 6072.0 and back["state"] == last
    # a later measurement (read failed first, 0.0 default on screen) still prefills the untouched widget
    late = verify_prefill(item_id=_A, target=500.0, last={"item": _A, "val": 0.0}, widget_value=0.0,
                          clicked=False)
    assert late["write"] == 500.0


def test_verify_prefill_never_overwrites_an_edit():
    """C17: the measurement moves; an edited widget keeps the operator's value, an untouched one follows."""
    last = {"item": _A, "val": 6072.0}
    edited = verify_prefill(item_id=_A, target=5974.59, last=last, widget_value=1234.0, clicked=False)
    assert edited["write"] is None and edited["kept_edit"] is True and edited["state"] == last
    same = verify_prefill(item_id=_A, target=1234.0, last=last, widget_value=1234.0, clicked=False)
    assert same["write"] is None and same["kept_edit"] is False
    untouched = verify_prefill(item_id=_A, target=5974.59, last=last, widget_value=6072.0, clicked=False)
    assert untouched["write"] == 5974.59 and untouched["state"] == {"item": _A, "val": 5974.59}
    # an untouched prefill is withdrawn (to the 0.0 default) when the target goes away, e.g. an overlap
    gone = verify_prefill(item_id=_A, target=None, last=last, widget_value=6072.0, clicked=False)
    assert gone["write"] == 0.0 and gone["state"] == {"item": _A, "val": 0.0}
    steady = verify_prefill(item_id=_A, target=6072.0, last=last, widget_value=6072.0, clicked=False)
    assert steady["write"] is None and steady["state"] == last


def test_verify_prefill_never_moves_in_the_verify_click_rerun():
    """C17: the click's rerun must write what st.code showed, so the amount is frozen there."""
    last = {"item": _A, "val": 6072.0}
    for widget in (6072.0, 1234.0, None):
        frozen = verify_prefill(item_id=_A, target=5974.59, last=last, widget_value=widget, clicked=True)
        assert frozen["write"] is None and frozen["state"] == last


def _ledger(*rows: dict) -> pd.DataFrame:
    base = {"ITEM_ID": "", "CREATED_AT": pd.Timestamp("2026-09-01 09:00"), "DESCRIPTION": "d",
            "STATE": "ESTIMATED", "FINDING_TYPE": "SCHEDULE", "SOURCE": "manual", "TARGET_OBJECT": "WH_A",
            "SUPERSEDED_BY_CHANGE_ID": None, "CHANGE_WAREHOUSE": None}
    return pd.DataFrame([{**base, **r} for r in rows])


def test_ledger_overlaps_names_other_changes_inside_the_window():
    me = {"ITEM_ID": "me000000-1"}
    frame = _ledger(
        me,
        # a settled change-scan row: no TARGET_OBJECT, matched on its registry warehouse; inside the window
        {"ITEM_ID": "auto0000-1", "SOURCE": "auto", "STATE": "VERIFIED", "FINDING_TYPE": "AUTO_SUSPEND",
         "TARGET_OBJECT": None, "CHANGE_WAREHOUSE": "wh_a", "CREATED_AT": pd.Timestamp("2026-09-03 07:00")},
        # a manual row on the same warehouse 10 days BEFORE the booking: inside the 14-day before-window
        {"ITEM_ID": "man00000-1", "CREATED_AT": pd.Timestamp("2026-08-22 10:00"), "TARGET_OBJECT": '"WH_A"'},
        # excluded: another warehouse, rejected, a superseded twin, and outside the window either side
        {"ITEM_ID": "othr0000-1", "TARGET_OBJECT": "WH_B"},
        {"ITEM_ID": "rej00000-1", "STATE": "REJECTED"},
        {"ITEM_ID": "twin0000-1", "SUPERSEDED_BY_CHANGE_ID": "chg-1"},
        {"ITEM_ID": "old00000-1", "CREATED_AT": pd.Timestamp("2026-08-17 23:00")},
        {"ITEM_ID": "late0000-1", "CREATED_AT": pd.Timestamp("2026-09-20 08:00")},
    )
    ov = ledger_overlaps(frame, item_id="me000000-1", target="WH_A", booked_day=_BOOKED,
                         window_end=date(2026, 9, 15), row_cap=500)
    assert [o["item_id"] for o in ov["items"]] == ["man00000-1", "auto0000-1"] and ov["count"] == 2
    assert ov["complete"] is True and ov["start"] == date(2026, 8, 18) and ov["end"] == date(2026, 9, 15)
    assert ov["items"][1]["label"] == "auto0000 AUTO_SUSPEND (auto, VERIFIED, booked Sep 3)"
    # no after_end yet: the window runs to booking + 30 days, which takes the Sep 20 row in
    wide = ledger_overlaps(frame, item_id="me000000-1", target="WH_A", booked_day=_BOOKED)
    assert "late0000-1" in [o["item_id"] for o in wide["items"]]
    alone = ledger_overlaps(_ledger(me), item_id="me000000-1", target="WH_A", booked_day=_BOOKED)
    assert alone["count"] == 0 and alone["items"] == [] and alone["complete"] is True
    assert ledger_overlaps(None, item_id="x", target="WH_A", booked_day=_BOOKED)["count"] == 0


def test_ledger_overlaps_flags_a_cut_off_page():
    """The page is savings_ledger()'s newest-first LIMIT: full, with its oldest row inside the window, it
    cannot rule an overlap out."""
    rows = [{"ITEM_ID": f"r{i:07d}-1", "TARGET_OBJECT": "WH_Z",
             "CREATED_AT": pd.Timestamp("2026-08-25 09:00")} for i in range(3)]
    cut = ledger_overlaps(_ledger({"ITEM_ID": "me000000-1"}, *rows), item_id="me000000-1", target="WH_A",
                          booked_day=_BOOKED, row_cap=4)
    assert cut["count"] == 0 and cut["complete"] is False
    older = [*rows, {"ITEM_ID": "anc00000-1", "TARGET_OBJECT": "WH_Z",
                     "CREATED_AT": pd.Timestamp("2026-08-01 09:00")}]
    ok = ledger_overlaps(_ledger({"ITEM_ID": "me000000-1"}, *older), item_id="me000000-1", target="WH_A",
                         booked_day=_BOOKED, row_cap=5)
    assert ok["complete"] is True
    assert ledger_overlaps(_ledger({"ITEM_ID": "me000000-1"}, *rows), item_id="me000000-1", target="WH_A",
                           booked_day=_BOOKED, row_cap=500)["complete"] is True


def test_proof_result_records_overlaps_only_when_present():
    m = ledger_measurement(_wh_row(), basis="WAREHOUSE", rate=3.68, storage_usd_per_tb=23.0, today=_TODAY)
    clean = proof_result_json(m, target="WH_A", basis="WAREHOUSE", entered_usd=1.0, sql_hash="h")
    assert clean == proof_result_json(m, target="WH_A", basis="WAREHOUSE", entered_usd=1.0, sql_hash="h",
                                      overlaps={"items": [], "count": 0, "complete": True})
    doc = json.loads(proof_result_json(
        m, target="WH_A", basis="WAREHOUSE", entered_usd=1.0, sql_hash="h",
        overlaps={"items": [{"label": "auto0000 AUTO_SUSPEND (auto, VERIFIED, booked Sep 3)"}], "count": 1,
                  "complete": False}))
    assert doc["overlapping_items"] == ["auto0000 AUTO_SUSPEND (auto, VERIFIED, booked Sep 3)"]
    assert doc["overlap_check"] == "incomplete"


def test_ledger_page_rows_matches_the_builder_default():
    import inspect

    from app.ui.pages.cost_parts import optimize
    assert inspect.signature(mart_sql.savings_ledger).parameters["limit"].default == optimize._LEDGER_PAGE_ROWS
    tab = read("app/ui/pages/cost_parts/optimize.py").split("def _savings_tab(", 1)[1].split("\ndef ", 1)[0]
    assert "run(mart_sql.savings_ledger(), page=_PAGE, key=\"savings_ledger\"" in tab
    assert "ledger_overlaps(res.df, item_id=_item, target=_tgt, booked_day=_booked.date()," in tab
    assert "row_cap=_LEDGER_PAGE_ROWS)" in tab


# ---------------------------------------------------------------------------
# Optimize ▸ Savings ledger wiring (source locks; the rendered check is in test_prc_c1_shaped.py)
# ---------------------------------------------------------------------------

def _verify() -> str:
    src = read("app/ui/pages/cost_parts/optimize.py")
    return src.split('with st.expander("Verify an estimated item (proof required)"):', 1)[1].split(
        'notify(ok, msg if not ok else f"Verified savings item', 1)[0]


def test_verify_measures_after_the_pick_and_prefills_behind_a_sentinel():
    v = _verify()
    pick = v.index("row = options[chosen]")
    assert pick < v.index("ledger_basis(row.get(\"FINDING_TYPE\"))") < v.index("mart_sql.ledger_before_after(")
    assert v.index("mart_sql.ledger_before_after(") < v.index('key="ledger_verified_usd"')
    assert 'key=f"ledger_measure_{_item[:8]}", tier="recent"' in v and "probe=True" in v
    # Review C12/C17 (replaces the item|value string sentinel, which re-prefilled over an edit whenever the
    # measurement moved and never re-armed after Streamlit dropped the widget): ONE pure decision, fed the
    # widget's presence, the last amount OVERWATCH left there and the Verify-click flag, runs before the
    # widget, and it is the ONLY writer of the widget's state.
    decide = v.index("_pf = verify_prefill(")
    assert decide < v.index('st.session_state["ledger_verified_usd"] = float(_pf["write"])') \
        < v.index('key="ledger_verified_usd"')
    assert v.count('st.session_state["ledger_verified_usd"] = ') == 1
    # C12: an ABSENT key (Streamlit dropped it when the section was left) reaches the decision as None
    assert 'widget_value=st.session_state.get("ledger_verified_usd"),' in v
    assert 'else st.session_state.get("_ow_ledger_prefill")),' in v and "clicked=_clicked and not _stale_click)" in v
    assert '_clicked = bool(st.session_state.get("ledger_verify_exec"))' in v
    # review r2: a click on another item than the one whose UPDATE was painted is stale -- the prefill takes the
    # item-change rule, and the write runs only when the rebuilt statement is exactly the one painted
    assert '_stale_click = _clicked and not _same' in v
    assert 'last=({"item": "", "val": None} if _stale_click' in v
    gate = v.split('st.button("Verify savings item", key="ledger_verify_exec"):', 1)[1]
    assert gate.index("if _stale_click or update_sql != _painted:") < gate.index('elif write_gate_open("ledger_verify_exec"):')
    assert gate.index('elif write_gate_open("ledger_verify_exec"):') < gate.index("execute_statement(update_sql")
    # the painted statement is recorded only AFTER st.code painted it
    assert v.index('st.code(update_sql, language="sql")') < v.index('"ov": _ov, "update_sql": update_sql}')
    assert v.index('st.session_state["_ow_ledger_prefill"] = _pf["state"]') < v.index('key="ledger_verified_usd"')
    # C22: an overlapping change withdraws the prefill target
    assert 'target=float(_m["prefill_usd"]) if _measured and not _overlap and _m is not None else None' in v
    # C17: the click's rerun swaps in what the previous render showed BEFORE the KPIs and the proof use it
    swap = v.index('_m, _msql, _qid, _ov = _shown["m"], _shown["sql"], _shown["qid"], _shown["ov"]')
    assert swap < v.index("kpi_row(_mk)") < v.index("proof_result_json(")
    assert swap < v.index('st.session_state["_ow_ledger_shown"] = {')
    assert '"Verified USD per month (measured, post-period)"' in v and "min_value=0.0, step=50.0" in v
    assert 'st.caption("No measured basis for this item — enter the verified amount by hand.")' in v


def test_proof_columns_only_on_the_measured_branch():
    v = _verify()
    proof = v.split("_proof_set = \"\"\n", 1)[1].split("update_sql = (", 1)[0]
    assert proof.lstrip().startswith("if _measured and _m is not None:")
    for col in ("PROOF_QUERY_ID = ", "PROOF_RESULT = ", "PROOF_RUN_AT = CURRENT_TIMESTAMP()"):
        assert col in proof
        assert v.count(col) == 1
    assert "sql_literal(_qid, 80) if _qid else 'NULL'" in proof and "sql_literal(_proof_json, 16000)" in proof
    assert 'st.session_state[f"_ow_proof_qid_{_item}"] = _mres.query_id' in v
    assert "VERIFIED_BY = {identity_sql()}{_proof_set}\\n" in v
    assert "AND STATE = 'ESTIMATED';" in v and "can_verify(check)" in v
    assert 'st.button("Verify savings item", key="ledger_verify_exec")' in v


def test_savings_tab_takes_the_rate_and_settings_from_the_page():
    opt = read("app/ui/pages/cost_parts/optimize.py")
    assert "def _savings_tab(rate: float = 3.68, settings: dict | None = None) -> None:" in opt
    assert "_savings_tab(rate, settings)" in read("app/ui/pages/cost.py")
    assert ("from app.logic.formulas import account_today, format_usd, humanize_duration, md_dollars, "
            "safe_float\n") in opt
    # the measured verify adds no write gate, button, raw info/success or ACCOUNT_USAGE literal to the tab
    tab = opt.split("def _savings_tab(", 1)[1].split("\ndef ", 1)[0]
    assert tab.count("write_gate_open(") == 3 and tab.count("st.button(") == 4
    assert "ACCOUNT_USAGE" not in tab and "st.info(" not in tab and "st.success(" not in tab
    assert tab.count("methodology_note(") == 1
