"""Next-Fifty #15: 'Track as work item' from Control Room triage.

Task-failure and warehouse-spend rows can be tracked into Action Center through the ONE shared Track write
(fix_queue.track_entities_sql -- Optimize's statement, keyed on the entity); alerts never can (D2: an
ACTION_QUEUE item has no lifecycle link to ALERT_EVENTS). The triage table gains TRACKED, Ack by and ACK_AT
and ranks unowned rows first within each severity -- re-sorted BEFORE the table is built, so a clicked row
maps back to the row it shows. The panel test runs through AppTest.from_function with no ButtonGroup, so it
also runs on the streamlit 1.52.2 floor leg (where a re-ordered selectbox re-creates its widget).
"""

from __future__ import annotations

import inspect
import re

import pandas as pd
import pytest

from app import companies
from app.core.query import _statement_allowed
from app.data import canary, workbench_sql
from app.logic import fix_queue
from app.logic.actions import triage_queue
from app.logic.fix_queue import (
    TRACK_ENTITY_TYPE,
    TRACK_SOURCE,
    TRIAGE_TRACK_SOURCE,
    TRIAGE_TRACK_TYPES,
    track_entities_sql,
    track_fingerprints_sql,
    triage_track_item,
    triage_track_label,
    triage_track_options,
    triage_track_row,
    with_triage_track_status,
)
from app.logic.workbench import ENTITY_TYPES
from tests._source import read

sqlglot = pytest.importorskip("sqlglot")

_CR = "app/ui/pages/control_room.py"


def _body(src: str, name: str) -> str:
    return src.split(f"def {name}(", 1)[1].split("\ndef ", 1)[0]


def _alerts() -> pd.DataFrame:
    return pd.DataFrame([
        {"SEVERITY": "HIGH", "TITLE": "acked", "DETAIL": "", "RAISED_AT": "2026-09-28 01:00", "EVENT_ID": "E1",
         "RULE_ID": "R", "STATUS": "ack", "ACK_BY": " JDOE ", "ACK_AT": pd.Timestamp("2026-09-28 02:00")},
        {"SEVERITY": "HIGH", "TITLE": "open", "DETAIL": "", "RAISED_AT": "2026-09-28 00:00", "EVENT_ID": "E2",
         "RULE_ID": "R", "STATUS": "OPEN", "ACK_BY": float("nan"), "ACK_AT": pd.NaT},
        {"SEVERITY": "CRITICAL", "TITLE": "crit acked", "DETAIL": "", "RAISED_AT": "2026-09-28 00:00",
         "EVENT_ID": "E3", "RULE_ID": "R", "STATUS": "ACK", "ACK_BY": "X", "ACK_AT": pd.Timestamp("2026-09-28")},
    ])


def _tasks() -> pd.DataFrame:
    return pd.DataFrame([
        {"DATABASE_NAME": "DB", "SCHEMA_NAME": "S", "TASK_NAME": "LOAD_A", "FAILED": 4, "LAST_ERROR": "boom",
         "DAY": "2026-09-28"},
        {"DATABASE_NAME": "DB", "SCHEMA_NAME": "", "TASK_NAME": "NOSCHEMA", "FAILED": 1, "LAST_ERROR": "",
         "DAY": "2026-09-28"},
    ])


def _anomalies() -> list[dict]:
    return [{"label": "WH_A", "value": 2000.0, "z": 9.0, "excess_usd": 900.0, "day": "2026-09-27"},
            {"label": "WH_B", "value": 1500.0, "z": 8.0, "excess_usd": 600.0, "day": "2026-09-27"},
            {"label": "WH_C", "value": 1.0, "z": -9.0, "excess_usd": -500.0, "day": "2026-09-27"}]


def _tracked(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame([{"ENTITY_TYPE_U": t, "ENTITY_KEY_U": k, "LATEST_ACTION_ID": f"id-{k}",
                          "OPEN_ACTION_ID": f"open-{k}" if o else None, "OPEN_ACTION_COMPANY": "ALFA" if o else None,
                          "ACTION_STATUS": s, "ACTION_OWNER": "UNASSIGNED", "OPEN_N": o, "DROPPED_N": d,
                          "DONE_N": 1 if s == "DONE" else 0, "LAST_DECIDED": None} for t, k, s, o, d in rows])


# ---------------------------------------------------------------------------- triage_queue ----

def test_triage_queue_carries_alert_ack_state():
    q = triage_queue(_alerts(), None, None)
    by = q.set_index("TITLE")
    assert by.loc["acked", "STATUS"] == "ACK" and by.loc["acked", "ACK_BY"] == "JDOE"
    assert by.loc["acked", "ACK_AT"] == "2026-09-28 02:00:00"                  # Arrow-safe text
    assert by.loc["open", "ACK_BY"] is None and by.loc["open", "ACK_AT"] is None
    no_status = triage_queue(_alerts().drop(columns=["STATUS", "ACK_BY", "ACK_AT"]), None, None)
    assert no_status["STATUS"].eq("OPEN").all() and no_status["ACK_BY"].isna().all()
    mixed = triage_queue(_alerts(), _tasks(), _anomalies())
    other = mixed[mixed["KIND"] != "Alert"]
    assert other["ACK_BY"].isna().all() and other["ACK_AT"].isna().all() and other["STATUS"].eq("").all()
    pa = pytest.importorskip("pyarrow")
    pa.Table.from_pandas(mixed[["ACK_BY", "ACK_AT", "RAISED_AT"]])            # one type per column


def test_triage_queue_carries_entity_identity_and_keeps_its_order():
    q = triage_queue(_alerts(), _tasks(), _anomalies())
    ident = {(r["KIND"], r["TITLE"]): (r["ENTITY_TYPE"], r["ENTITY_KEY"]) for _, r in q.iterrows()}
    assert ident[("Task failure", "DB.S.LOAD_A failed 4x")] == ("TASK", "DB.S.LOAD_A")
    assert ident[("Task failure", "DB.NOSCHEMA failed 1x")] == ("", "")            # not trackable
    assert ident[("Spend anomaly", "WH_A daily spend z=+9.0")] == ("WAREHOUSE", "WH_A")
    assert ident[("Spend collapse", "WH_C daily spend collapsed z=-9.0")] == ("WAREHOUSE", "WH_C")
    assert all(v == ("", "") for (k, _t), v in ident.items() if k == "Alert")
    # the sort is unchanged: severity, then dollars at risk, then KIND
    assert q["SEVERITY"].tolist()[0] == "CRITICAL"
    assert q["TITLE"].tolist()[1:3] == ["WH_A daily spend z=+9.0", "WH_B daily spend z=+8.0"]


# --------------------------------------------------------------------- status + ranking ----

def _ranked(read_ok: bool = True, tracked: pd.DataFrame | None = None) -> pd.DataFrame:
    q = triage_queue(_alerts(), None, _anomalies()[:2])
    t = tracked if tracked is not None else _tracked([("WAREHOUSE", "WH_A", "OPEN", 1, 0)])
    return with_triage_track_status(q, t, read_ok=read_ok)


def test_unowned_first_within_severity_keeps_dollar_order():
    before = triage_queue(_alerts(), None, _anomalies()[:2])
    assert before["TITLE"].tolist() == ["crit acked", "WH_A daily spend z=+9.0", "WH_B daily spend z=+8.0",
                                        "acked", "open"]
    out = _ranked()
    # CRITICAL first; then the unowned HIGH rows in their previous relative order; then the owned ones
    assert out["TITLE"].tolist() == ["crit acked", "WH_B daily spend z=+8.0", "open",
                                     "WH_A daily spend z=+9.0", "acked"]
    by = out.set_index("TITLE")
    assert by.loc["WH_B daily spend z=+8.0", "TRACKED"] == "Untracked"
    assert by.loc["WH_A daily spend z=+9.0", "TRACKED"] == "Tracked (open)"
    assert by.loc["WH_A daily spend z=+9.0", "TRACKED_ACTION_ID"] == "open-WH_A"
    assert by.loc["WH_A daily spend z=+9.0", "TRACKED_COMPANY"] == "ALFA"
    assert by.loc["acked", "TRACKED"] is None and by.loc["open", "TRACKED_ACTION_ID"] == ""
    assert out.index.tolist() == list(range(len(out)))                         # positional mapping


def test_failed_tracked_read_is_unknown_and_never_demotes():
    out = _ranked(read_ok=False, tracked=pd.DataFrame())
    assert out["TITLE"].tolist() == ["crit acked", "WH_A daily spend z=+9.0", "WH_B daily spend z=+8.0",
                                     "open", "acked"]                          # only the ACKed alert moved
    by = out.set_index("TITLE")
    assert by.loc["WH_A daily spend z=+9.0", "TRACKED"] == "Unknown" and by.loc["open", "TRACKED"] is None
    assert out["TRACKED_ACTION_ID"].eq("").all() and out["TRACKED_COMPANY"].eq("").all()


def test_dismissed_and_done_show_and_sort_as_unowned():
    t = _tracked([("WAREHOUSE", "WH_A", "DONE", 0, 0), ("warehouse", "wh_b", "DROPPED", 0, 1)])
    out = _ranked(tracked=t)
    by = out.set_index("TITLE")
    assert by.loc["WH_A daily spend z=+9.0", "TRACKED"] == "Done"
    assert by.loc["WH_B daily spend z=+8.0", "TRACKED"] == "Dismissed"          # case-insensitive match
    assert out["TITLE"].tolist()[1:3] == ["WH_A daily spend z=+9.0", "WH_B daily spend z=+8.0"]
    # a same-key item of ANOTHER type never marks the row tracked (keyed on type AND key)
    other = _ranked(tracked=_tracked([("TASK", "WH_A", "OPEN", 1, 0)]))
    assert other.set_index("TITLE").loc["WH_A daily spend z=+9.0", "TRACKED"] == "Untracked"


def test_with_triage_track_status_empty_passthrough():
    assert with_triage_track_status(pd.DataFrame(), None, read_ok=True).empty
    assert with_triage_track_status(pd.DataFrame(), None, read_ok=False).empty


# ------------------------------------------------------------------------------ the item ----

def test_alerts_are_never_trackable():
    q = with_triage_track_status(triage_queue(_alerts(), _tasks(), _anomalies()), None, read_ok=True)
    alert = q[q["KIND"] == "Alert"].iloc[0].to_dict()
    assert triage_track_item(alert, "ALL") is None
    # queue order (dollars at risk inside HIGH), de-duplicated; the schema-less task is not offered
    assert triage_track_options(q) == ["WAREHOUSE|WH_A", "WAREHOUSE|WH_B", "WAREHOUSE|WH_C", "TASK|DB.S.LOAD_A"]
    for kind in ("ALERT", "INCIDENT", "", "USER", "QUERY"):
        with pytest.raises(ValueError):
            track_entities_sql([{"ENTITY_KEY": "E1"}], entity_type=kind, source=TRIAGE_TRACK_SOURCE,
                               actor_sql="CURRENT_USER()", bulk=False)
    # the trackable set is exactly the Optimize family + the two triage types, all real Entity 360 types
    assert {TRACK_ENTITY_TYPE, "TASK", "WAREHOUSE"} == fix_queue._TRACKABLE_TYPES
    assert set(TRIAGE_TRACK_TYPES) | {TRACK_ENTITY_TYPE} <= set(ENTITY_TYPES)
    assert triage_track_options(pd.DataFrame([{"KIND": "Alert"}])) == []
    assert triage_track_options(None) == []


def test_triage_track_item_maps_company_severity_title():
    row = {"ENTITY_TYPE": "TASK", "ENTITY_KEY": "ALFA_EDW_PRD.S.LOAD_A", "KIND": "Task failure",
           "SEVERITY": "HIGH", "TITLE": "ALFA_EDW_PRD.S.LOAD_A failed 4x", "DETAIL": "boom",
           "RAISED_AT": "2026-09-28"}
    item = triage_track_item(row, "ALL")
    assert item is not None
    assert item["COMPANY"] == "ALFA" and item["SEVERITY"] == "MEDIUM"
    assert item["TITLE"] == "Task failure: ALFA_EDW_PRD.S.LOAD_A"
    assert item["DETAIL"].startswith("ALFA_EDW_PRD.S.LOAD_A failed 4x. boom. Triage severity HIGH, raised 2026-09-28.")
    assert item["ESTIMATED_USD"] is None and item["CONFIDENCE"] is None and item["PERIOD"] == ""
    assert triage_track_item({**row, "SEVERITY": "CRITICAL"}, "ALL")["SEVERITY"] == "MEDIUM"
    assert triage_track_item({**row, "SEVERITY": "MEDIUM"}, "ALL")["SEVERITY"] == "LOW"
    assert triage_track_item(row, "Trexis")["COMPANY"] == "Trexis"                # the scope wins
    wh = {"ENTITY_TYPE": "WAREHOUSE", "KIND": "Spend anomaly", "SEVERITY": "HIGH"}
    assert triage_track_item({**wh, "ENTITY_KEY": "WH_ALFA_X"}, "ALL")["COMPANY"] == "ALFA"
    assert triage_track_item({**wh, "ENTITY_KEY": companies.TREXIS_WAREHOUSES[0]}, "ALL")["COMPANY"] == "Trexis"
    assert triage_track_item({**wh, "ENTITY_KEY": "SOMETHING_ELSE"}, "ALL")["COMPANY"] == "ALL"
    long = triage_track_item({**row, "DETAIL": "x" * 5000, "ENTITY_KEY": "K" * 600}, "ALL")
    assert len(long["TITLE"]) <= 300 and len(long["DETAIL"]) <= 1000 and len(long["ENTITY_KEY"]) <= 500
    assert long["DETAIL"].endswith("unpriced (a triage row carries no savings estimate).")
    assert triage_track_item({**row, "ENTITY_KEY": ""}, "ALL") is None


def _task_sql(bulk: bool = False, evil: str = "") -> str:
    row = {"ENTITY_TYPE": "TASK", "ENTITY_KEY": evil or "DB.S.LOAD_A", "KIND": "Task failure",
           "SEVERITY": "HIGH", "TITLE": evil or "t", "DETAIL": evil or "d", "RAISED_AT": ""}
    item = triage_track_item(row, evil or "ALL")
    return track_entities_sql([item], entity_type="TASK", source=TRIAGE_TRACK_SOURCE,
                              actor_sql="'JDOE'", bulk=bulk)


def test_track_entities_sql_task_and_warehouse_shape():
    for kind in ("TASK", "WAREHOUSE"):
        item = {"ENTITY_KEY": "K1", "COMPANY": "ALFA", "SEVERITY": "MEDIUM", "TITLE": "t", "DETAIL": "d",
                "CONFIDENCE": None, "ESTIMATED_USD": None, "PERIOD": ""}
        sql = track_entities_sql([item], entity_type=kind.lower(), source=TRIAGE_TRACK_SOURCE,
                                 actor_sql="'JDOE'", bulk=False)
        assert _statement_allowed(sql) == (True, "")
        parsed = sqlglot.parse(sql, read="snowflake")
        assert len(parsed) == 1 and parsed[0].key == "insert"
        assert sql.startswith("INSERT INTO DBA_MAINT_DB.OVERWATCH.ACTION_QUEUE")
        assert f"UPPER(q.SOURCE_ENTITY_TYPE) = '{kind}'" in sql
        assert "'UNASSIGNED', 'OPEN', 'Control Room > Triage'" in sql and f"'{kind}', v.ENTITY_KEY" in sql
        assert "'K1', NULL, NULL, '')" in sql                                   # unpriced
        assert "NULLIF(v.PER, ''), 'JDOE'" in sql                               # UPDATED_BY = the viewer
        assert "q.TITLE" not in sql and "q.COMPANY" not in sql and "DROPPED" not in sql
    evil = "x'); DROP TABLE ACTION_QUEUE; --\\"
    sql = _task_sql(evil=evil)
    assert _statement_allowed(sql) == (True, "")
    residue = re.sub(r"'(?:[^'\\]|\\.|'')*'", "''", sql)
    assert "DROP" not in residue.upper()


def test_one_write_path():
    items = [{"COMPANY": "ALFA", "SEVERITY": "MEDIUM", "TITLE": "t", "DETAIL": "d", "ENTITY_KEY": "FP1",
              "CONFIDENCE": 0.5, "ESTIMATED_USD": 12.3, "PERIOD": "MONTHLY"}]
    for bulk in (True, False):
        assert track_fingerprints_sql(items, actor_sql="'J'", bulk=bulk) == track_entities_sql(
            items, entity_type="QUERY_FINGERPRINT", source="Operations > Optimize", actor_sql="'J'", bulk=bulk)
    assert TRACK_SOURCE == "Operations > Optimize"
    # normalize-and-compare: a TASK statement differs from the family one ONLY in its type and source literals
    fam = track_entities_sql(items, entity_type="QUERY_FINGERPRINT", source=TRACK_SOURCE, actor_sql="'J'",
                             bulk=False)
    task = track_entities_sql(items, entity_type="TASK", source=TRIAGE_TRACK_SOURCE, actor_sql="'J'", bulk=False)
    assert task.replace("'TASK'", "'QUERY_FINGERPRINT'").replace(f"'{TRIAGE_TRACK_SOURCE}'",
                                                                  f"'{TRACK_SOURCE}'") == fam
    assert read("app/logic/fix_queue.py").count("INSERT INTO") == 1
    cr = read(_CR)
    assert "INSERT INTO {core_object('ACTION_QUEUE')}" not in cr
    assert ('track_entities_sql([_item], entity_type=str(_item["ENTITY_TYPE"]), source=TRIAGE_TRACK_SOURCE,'
            in cr)


def test_tracked_entity_actions_builder():
    sql = workbench_sql.tracked_entity_actions()
    assert sqlglot.parse_one(sql, read="snowflake").named_selects == [
        "ENTITY_TYPE_U", "ENTITY_KEY_U", "LATEST_ACTION_ID", "OPEN_ACTION_ID", "OPEN_ACTION_COMPANY",
        "ACTION_STATUS", "ACTION_OWNER", "OPEN_N", "DROPPED_N", "DONE_N", "LAST_DECIDED"]
    assert "UPPER(q.SOURCE_ENTITY_TYPE) IN ('TASK', 'WAREHOUSE')" in sql
    assert "GROUP BY UPPER(q.SOURCE_ENTITY_TYPE), UPPER(q.SOURCE_ENTITY_KEY)" in sql
    assert workbench_sql.tracked_entity_actions(("warehouse", "task", "TASK")) == sql      # sorted + deduped
    assert "-365," in workbench_sql.tracked_entity_actions(lookback_days=99999)
    with pytest.raises(ValueError):
        workbench_sql.tracked_entity_actions(())
    assert "company" not in inspect.signature(workbench_sql.tracked_entity_actions).parameters
    assert "ACCOUNT_USAGE" not in sql
    reg = dict(canary.CANARIES)
    assert sqlglot.parse_one(reg["workbench.tracked_entity_actions"](), read="snowflake") is not None
    # the Optimize read is untouched (its column list is locked in test_optimize_queue)
    assert "ENTITY_TYPE_U" not in workbench_sql.tracked_actions()


def test_triage_track_label_is_a_pure_function_of_the_option():
    assert triage_track_label("TASK|DB.S.T") == "Task · DB.S.T"
    assert triage_track_label("WAREHOUSE|WH") == "Warehouse · WH"
    code = inspect.getsource(triage_track_label).split('"""', 2)[-1]
    for token in ("humanize_", "format_usd", "z=", "SEVERITY", "TRACKED"):
        assert token not in code, token
    q = pd.DataFrame([{"ENTITY_TYPE": "TASK", "ENTITY_KEY": "A.B.C", "TITLE": "first"},
                      {"ENTITY_TYPE": "task", "ENTITY_KEY": "A.B.C", "TITLE": "dupe"}])
    assert triage_track_options(q) == ["TASK|A.B.C"]
    assert triage_track_row(q, "TASK|A.B.C")["TITLE"] == "first"          # bound by identity, first match
    assert triage_track_row(q, "TASK|NOPE") is None


# ------------------------------------------------------------------------ source locks ----

def test_control_room_track_is_gated_latched_and_previewed():
    src = read(_CR)
    body = _body(src, "_triage_track_panel")
    assert body.count('write_gate_open(f"cr_track:{_pick}")') == 1
    assert body.count('stamp_write(f"cr_track:{_pick}", ok)') == 1
    gate = body.index('write_gate_open(f"cr_track:{_pick}")')
    assert 'can_write and st.button("Track"' in body[max(0, gate - 220):gate]
    assert body.index("st.code(_sql") < body.index('st.button("Track"')
    stamp = body.index('stamp_write(f"cr_track:{_pick}", ok)')
    assert body.index("execute_statement(_sql.strip()") < stamp < body.index("st.rerun()", stamp)
    assert "bulk=False" in body and "source=TRIAGE_TRACK_SOURCE" in body
    assert 'st.caption("Read-only — an operator can track this into Action Center.")' in body
    # pick memory: remembered outside the widget, seeds index, saved after the widget
    prev = body.index('_prev = st.session_state.get("cr_track_last")')
    idx = body.index("_idx = _opts.index(_prev) if _prev in _opts else 0")
    pick = body.index('_pick = st.selectbox("Task or warehouse", _opts, index=_idx, key="cr_track_pick",')
    save = body.index('st.session_state["cr_track_last"] = _pick')
    assert prev < idx < pick < save
    assert "format_func=triage_track_label" in body[pick:save]
    assert "triage_track_row(queue, str(_pick))" in body                  # bound by identity, not position
    # the gate call site passes the VIEWER's operator check (owner's-rights SiS: never CURRENT_ROLE())
    assert "_triage_track_panel(queue, company, can_write=_is_op, tracked_ok=_tracked_ok)" in src
    assert "_is_op = is_operator()" in src


def test_triage_tracked_read_is_gated_before_display():
    src = read(_CR)
    tri = src.split('section_header("Triage queue")', 1)[1].split('elif section == "Timeline & movers":', 1)[0]
    q = tri.index("queue = triage_queue(")
    gate = tri.index("if triage_track_options(queue):")
    rd = tri.index("run(workbench_sql.tracked_entity_actions(TRIAGE_TRACK_TYPES)")
    resort = tri.index("queue = with_triage_track_status(queue, _tracked_df, read_ok=_tracked_ok)")
    assert q < gate < rd < resort
    assert 'key="cr_triage_tracked", tier="recent"' in tri[rd:resort]
    assert "run_batch" not in tri[rd:resort] and "probe" not in tri[rd:resort]
    # re-sorted BEFORE the display frame, the AGE frame and the positional click closure are built
    for later in ("_disp = [c for c in (", 'queue.assign(AGE=queue["RAISED_AT"].map(lambda t: humanize_age(t, _now)))',
                  "def _open_triage(", "selectable_nav_table(_qdisp[_disp]"):
        assert resort < tri.index(later), later
    assert '("SEVERITY", "KIND", "TRACKED", "DATABASE", "TITLE", "DETAIL", "SOURCE",' in tri
    assert 'with_user_names(_qdisp, _PAGE, user_col="ACK_BY", display_col="Ack by")' in tri
    assert '_disp = [*_disp, "Ack by", "ACK_AT"]' in tri
    assert "ranked by severity, then unowned first" in tri


def test_the_open_triage_closure_indexes_the_resorted_queue():
    """Probe for the F51 bind-by-identity class: the frame the table shows and the frame the click indexes are
    the same re-sorted `queue` (attach_display_name preserves row order)."""
    from app.logic.directory import attach_display_name
    ranked = _ranked()
    shown = attach_display_name(ranked.assign(AGE="1h ago"), {"JDOE": "Jane Doe"}, user_col="ACK_BY",
                                display_col="Ack by")
    assert shown["TITLE"].tolist() == ranked["TITLE"].tolist()
    for i in range(len(ranked)):          # a click on shown row i opens ranked row i
        assert shown.iloc[i]["TITLE"] == ranked.iloc[i]["TITLE"]
    closure = read(_CR).split("def _open_triage(", 1)[1].split("request_navigation(", 1)[0]
    assert "_qr = queue.iloc[int(_i)]" in closure


# ------------------------------------------------------------- panel (AppTest, floor too) ----

WRITES: list[str] = []


def _panel_app():
    import pandas as _pd
    import streamlit as _st

    from app.ui.pages import control_room as _cr
    rows = [
        {"SEVERITY": "HIGH", "KIND": "Task failure", "ENTITY_TYPE": "TASK", "ENTITY_KEY": "DB.S.LOAD_A",
         "TITLE": "DB.S.LOAD_A failed 4x", "DETAIL": "boom", "RAISED_AT": "2026-09-28",
         "TRACKED": _st.session_state.get("_task_status", "Untracked"), "TRACKED_ACTION_ID": "a-1",
         "TRACKED_COMPANY": "ALFA"},
        {"SEVERITY": "HIGH", "KIND": "Spend anomaly", "ENTITY_TYPE": "WAREHOUSE", "ENTITY_KEY": "WH_B",
         "TITLE": "WH_B daily spend z=+8.0", "DETAIL": "Daily spend $1,500 vs robust baseline.",
         "RAISED_AT": "2026-09-27", "TRACKED": "Untracked", "TRACKED_ACTION_ID": "", "TRACKED_COMPANY": ""},
        {"SEVERITY": "HIGH", "KIND": "Alert", "ENTITY_TYPE": "", "ENTITY_KEY": "", "TITLE": "a", "DETAIL": "",
         "RAISED_AT": "", "TRACKED": None, "TRACKED_ACTION_ID": "", "TRACKED_COMPANY": ""},
    ]
    if _st.session_state.get("_alerts_only"):
        rows = rows[2:]
    if _st.session_state.get("_reorder"):
        rows = [rows[1], rows[0], *rows[2:]]
    _cr._triage_track_panel(_pd.DataFrame(rows), "ALL", can_write=bool(_st.session_state.get("_op", True)),
                            tracked_ok=True)


@pytest.fixture
def _panel(monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    import app.core.identity as identity
    import app.core.query as query
    import app.ui.components as components

    WRITES.clear()

    def _record_write(sql, *, page=""):
        WRITES.append(sql)
        return True, "ok"

    monkeypatch.setattr(query, "execute_statement", _record_write)
    monkeypatch.setattr(identity, "viewer_name", lambda: "JDOE")
    monkeypatch.setattr(components, "log_ui_event", lambda *a, **k: None)
    return AppTest.from_function(_panel_app, default_timeout=30)


def test_track_panel_apptest(_panel):
    at = _panel
    at.run()
    assert not at.exception, at.exception
    sb = at.selectbox(key="cr_track_pick")
    assert list(sb.options) == ["Task · DB.S.LOAD_A", "Warehouse · WH_B"]      # alerts never offered
    sb.set_value("WAREHOUSE|WH_B").run()
    assert at.selectbox(key="cr_track_pick").value == "WAREHOUSE|WH_B"
    # a re-sort re-orders the options (on 1.52.2 that re-creates the widget): the pick survives
    at.session_state["_reorder"] = True
    at.run()
    assert not at.exception, at.exception
    assert at.selectbox(key="cr_track_pick").value == "WAREHOUSE|WH_B"
    track = [b for b in at.button if b.label == "Track"]
    assert len(track) == 1
    track[0].click().run()
    assert not at.exception, at.exception
    assert len(WRITES) == 1
    assert "'Control Room > Triage'" in WRITES[0] and "'JDOE'" in WRITES[0] and "'WH_B'" in WRITES[0]
    assert "UPPER(q.SOURCE_ENTITY_TYPE) = 'WAREHOUSE'" in WRITES[0]
    at.run()                                                                       # a plain rerun: no 2nd write
    assert len(WRITES) == 1
    assert any("INSERT INTO" in str(c.value) for c in at.code)                     # the SQL is shown


def test_track_panel_open_item_and_read_only(_panel):
    at = _panel
    at.session_state["_task_status"] = "Tracked (open)"
    at.run()
    assert not at.exception, at.exception
    labels = [str(b.label) for b in at.button]
    assert "Open in Action Center →" in labels and "Track" not in labels
    captions = " ".join(str(c.value) for c in at.caption)
    assert "Already tracked" in captions
    at.session_state["_task_status"] = "Untracked"
    at.session_state["_op"] = False
    at.run()
    assert not at.exception, at.exception
    assert "Track" not in [str(b.label) for b in at.button]
    assert "Read-only — an operator can track this into Action Center." in " ".join(str(c.value)
                                                                                     for c in at.caption)
    assert not at.code and not WRITES


def test_track_panel_renders_nothing_for_an_alerts_only_queue(_panel):
    at = _panel
    at.session_state["_alerts_only"] = True
    at.run()
    assert not at.exception, at.exception
    assert not at.expander and not at.selectbox
