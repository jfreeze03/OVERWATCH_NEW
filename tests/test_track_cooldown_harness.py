"""Next-Fifty #46 (b) + #15, EXECUTED: the ONE Track write (fix_queue.track_entities_sql) and the triage
status read (workbench_sql.tracked_entity_actions) run in sqlite against a seeded ACTION_QUEUE.

Pins the cooldown override where it actually decides -- inside the INSERT's NOT EXISTS: a family marked
DONE inside the 90-day cooldown is re-inserted by Track all only when its measured outcome re-broke or never
held (rebroke_keys); a DROPPED family never is, an OPEN one never is, the same key under another entity type
never blocks, and a second run inserts nothing (idempotent). The Snowflake builtins are registered as Python
functions and the VALUES column list is rewritten (each rewrite asserts its fragment, so a changed builder
fails loudly). Pure: runs on the floor-compat leg.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta

import pandas as pd
from test_task_evidence_harness import _CountIf, _dateadd, _MaxBy

from app.data import workbench_sql
from app.logic.fix_queue import (
    TRIAGE_TRACK_SOURCE,
    TRIAGE_TRACK_TYPES,
    track_entities_sql,
    track_fingerprints_sql,
    with_triage_track_status,
)

_NOW = "2026-09-29 08:00:00"
_ACTOR = "'JDOE'"


def _ts(days_ago: int) -> str:
    return (datetime.strptime(_NOW, "%Y-%m-%d %H:%M:%S") - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _to_sqlite(sql: str) -> str:
    def swap(old: str, new: str) -> None:
        nonlocal sql
        assert old in sql, old
        sql = sql.replace(old, new)

    swap("DBA_MAINT_DB.OVERWATCH.", "")
    sql = sql.replace("CURRENT_TIMESTAMP()", f"'{_NOW}'")
    sql = sql.replace("::FLOAT", "").replace("::NUMBER(18,2)", "")
    m = re.search(r"FROM \(VALUES\n(?P<rows>.*?)\n\) AS v \(COMPANY, SEVERITY, TITLE, DETAIL, ENTITY_KEY, CONF, "
                  r"USD, PER\)", sql, re.S)
    if m:
        cols = ("COMPANY", "SEVERITY", "TITLE", "DETAIL", "ENTITY_KEY", "CONF", "USD", "PER")
        sel = ", ".join(f"column{i + 1} AS {c}" for i, c in enumerate(cols))
        sql = sql.replace(m.group(0), f"FROM (SELECT {sel} FROM (VALUES\n{m.group('rows')}\n)) AS v")
    assert "::" not in sql and "CURRENT_TIMESTAMP" not in sql
    return sql


def _db(rows: list[tuple]) -> sqlite3.Connection:
    """ACTION_QUEUE seeded with (entity type, key, status, decided days ago)."""
    con = sqlite3.connect(":memory:")
    con.create_function("IFF", 3, lambda c, a, b: a if c else b)
    con.create_function("DATEADD", 3, _dateadd)
    con.create_aggregate("MAX_BY", 2, _MaxBy)
    con.create_aggregate("COUNT_IF", 1, _CountIf)
    con.execute("CREATE TABLE ACTION_QUEUE (ACTION_ID INTEGER PRIMARY KEY AUTOINCREMENT, COMPANY TEXT, "
                "SEVERITY TEXT, TITLE TEXT, DETAIL TEXT, OWNER TEXT, STATUS TEXT, SOURCE TEXT, "
                "SOURCE_ENTITY_TYPE TEXT, SOURCE_ENTITY_KEY TEXT, CONFIDENCE REAL, ESTIMATED_USD REAL, "
                "PERIOD TEXT, UPDATED_BY TEXT, CREATED_AT TEXT, UPDATED_AT TEXT, COMPLETED_AT TEXT)")
    for etype, key, status, ago in rows:
        decided = _ts(ago)
        con.execute("INSERT INTO ACTION_QUEUE (COMPANY, SEVERITY, TITLE, DETAIL, OWNER, STATUS, SOURCE, "
                    "SOURCE_ENTITY_TYPE, SOURCE_ENTITY_KEY, UPDATED_BY, CREATED_AT, UPDATED_AT, COMPLETED_AT) "
                    "VALUES ('ALFA', 'LOW', 't', 'd', 'UNASSIGNED', ?, 'seed', ?, ?, 'SEED', ?, ?, ?)",
                    (status, etype, key, _ts(ago + 1), decided, decided if status in ("DONE", "DROPPED") else None))
    return con


def _item(key: str) -> dict:
    return {"COMPANY": "ALFA", "SEVERITY": "MEDIUM", "TITLE": f"Fix: {key}", "DETAIL": "d", "ENTITY_KEY": key,
            "CONFIDENCE": 0.5, "ESTIMATED_USD": None, "PERIOD": ""}


def _inserted(con: sqlite3.Connection) -> list[tuple]:
    return con.execute("SELECT SOURCE_ENTITY_TYPE, SOURCE_ENTITY_KEY, STATUS, SOURCE, UPDATED_BY, OWNER "
                       "FROM ACTION_QUEUE WHERE UPDATED_BY <> 'SEED' ORDER BY SOURCE_ENTITY_KEY").fetchall()


_SEED = [
    ("QUERY_FINGERPRINT", "FP_DONE_RB", "DONE", 10),       # done in cooldown, measured re-broke
    ("QUERY_FINGERPRINT", "FP_DONE", "DONE", 10),          # done in cooldown, held
    ("QUERY_FINGERPRINT", "FP_DROP_RB", "DROPPED", 10),    # dismissed: never lifted
    ("QUERY_FINGERPRINT", "FP_OPEN_RB", "OPEN", 3),        # open: never re-inserted
    ("QUERY_FINGERPRINT", "FP_BOTH_RB", "DONE", 5),        # done AND dismissed: the dismissal still blocks
    ("QUERY_FINGERPRINT", "FP_BOTH_RB", "DROPPED", 20),
    ("QUERY_FINGERPRINT", "fp_old", "DONE", 100),          # outside the cooldown (case-insensitive key)
    ("TASK", "FP_OTHER", "OPEN", 1),                        # same key, other type: never blocks a family
]


def test_bulk_track_all_lifts_only_the_done_cooldown_of_rebroke_families():
    con = _db(_SEED)
    keys = ["FP_DONE_RB", "FP_DONE", "FP_DROP_RB", "FP_OPEN_RB", "FP_BOTH_RB", "FP_OLD", "FP_OTHER", "FP_NEW"]
    sql = track_fingerprints_sql([_item(k) for k in keys], actor_sql=_ACTOR, bulk=True,
                                 rebroke_keys={"fp_done_rb", "FP_DROP_RB", "FP_OPEN_RB", "FP_BOTH_RB"})
    con.execute(_to_sqlite(sql))
    got = {k for _t, k, *_ in _inserted(con)}
    assert got == {"FP_DONE_RB", "FP_OLD", "FP_OTHER", "FP_NEW"}
    # every new item is an OPEN, UNASSIGNED Optimize item stamped by the viewer
    assert {r[2:] for r in _inserted(con)} == {("OPEN", "Operations > Optimize", "JDOE", "UNASSIGNED")}
    # idempotent: the same click again inserts nothing (the fresh OPEN items block)
    con.execute(_to_sqlite(sql))
    assert len(_inserted(con)) == 4


def test_without_rebroke_keys_the_cooldown_holds_for_every_decided_family():
    con = _db(_SEED)
    keys = ["FP_DONE_RB", "FP_DONE", "FP_DROP_RB", "FP_OLD"]
    con.execute(_to_sqlite(track_fingerprints_sql([_item(k) for k in keys], actor_sql=_ACTOR, bulk=True)))
    assert {k for _t, k, *_ in _inserted(con)} == {"FP_OLD"}


def test_a_single_track_ignores_the_cooldown_and_the_rebroke_keys():
    con = _db(_SEED)
    sql = track_fingerprints_sql([_item("FP_DONE"), _item("FP_DROP_RB"), _item("FP_OPEN_RB")], actor_sql=_ACTOR,
                                 bulk=False, rebroke_keys={"FP_DONE"})
    assert sql == track_fingerprints_sql([_item("FP_DONE"), _item("FP_DROP_RB"), _item("FP_OPEN_RB")],
                                         actor_sql=_ACTOR, bulk=False)
    con.execute(_to_sqlite(sql))
    assert {k for _t, k, *_ in _inserted(con)} == {"FP_DONE", "FP_DROP_RB"}      # an OPEN one still blocks


def test_triage_task_track_is_keyed_on_the_task_entity():
    con = _db([("TASK", "DB.S.DONE", "DONE", 5), ("TASK", "DB.S.OPEN", "OPEN", 1),
               ("QUERY_FINGERPRINT", "DB.S.FAM", "OPEN", 1), ("WAREHOUSE", "DB.S.OPEN_WH", "OPEN", 1)])
    items = [_item(k) for k in ("db.s.done", "DB.S.OPEN", "DB.S.FAM", "DB.S.OPEN_WH")]
    con.execute(_to_sqlite(track_entities_sql(items, entity_type="TASK", source=TRIAGE_TRACK_SOURCE,
                                              actor_sql=_ACTOR, bulk=False)))
    rows = _inserted(con)
    assert [(t, k) for t, k, *_ in rows] == [("TASK", "DB.S.FAM"), ("TASK", "DB.S.OPEN_WH"), ("TASK", "db.s.done")]
    assert {r[3] for r in rows} == {TRIAGE_TRACK_SOURCE}


def test_tracked_entity_actions_executes_into_the_triage_status():
    con = _db([("TASK", "DB.S.LOAD_A", "OPEN", 1), ("TASK", "db.s.load_a", "DONE", 40),
               ("WAREHOUSE", "WH_A", "DONE", 10), ("WAREHOUSE", "WH_B", "DROPPED", 20),
               ("WAREHOUSE", "WH_OLD", "DONE", 200),                      # outside the lookback: untracked
               ("QUERY_FINGERPRINT", "WH_C", "OPEN", 1)])                 # other type: never read here
    cur = con.execute(_to_sqlite(workbench_sql.tracked_entity_actions(TRIAGE_TRACK_TYPES)))
    tracked = pd.DataFrame(cur.fetchall(), columns=[d[0] for d in cur.description])
    assert sorted(zip(tracked["ENTITY_TYPE_U"], tracked["ENTITY_KEY_U"], strict=True)) == [
        ("TASK", "DB.S.LOAD_A"), ("WAREHOUSE", "WH_A"), ("WAREHOUSE", "WH_B")]
    load = tracked.set_index("ENTITY_KEY_U").loc["DB.S.LOAD_A"]
    assert load["OPEN_N"] == 1 and load["DONE_N"] == 1 and load["OPEN_ACTION_COMPANY"] == "ALFA"
    queue = pd.DataFrame([
        {"SEVERITY": "HIGH", "KIND": "Task failure", "ENTITY_TYPE": "TASK", "ENTITY_KEY": "DB.S.LOAD_A"},
        {"SEVERITY": "HIGH", "KIND": "Spend anomaly", "ENTITY_TYPE": "WAREHOUSE", "ENTITY_KEY": "WH_A"},
        {"SEVERITY": "HIGH", "KIND": "Spend anomaly", "ENTITY_TYPE": "WAREHOUSE", "ENTITY_KEY": "WH_B"},
        {"SEVERITY": "HIGH", "KIND": "Spend collapse", "ENTITY_TYPE": "WAREHOUSE", "ENTITY_KEY": "WH_C"},
        {"SEVERITY": "HIGH", "KIND": "Spend anomaly", "ENTITY_TYPE": "WAREHOUSE", "ENTITY_KEY": "WH_OLD"},
    ])
    out = with_triage_track_status(queue, tracked, read_ok=True)
    status = dict(zip(out["ENTITY_KEY"], out["TRACKED"], strict=True))
    assert status == {"DB.S.LOAD_A": "Tracked (open)", "WH_A": "Done", "WH_B": "Dismissed", "WH_C": "Untracked",
                      "WH_OLD": "Untracked"}
    assert out["ENTITY_KEY"].iloc[-1] == "DB.S.LOAD_A"          # the only owned row sorts last in its band
