"""Next-Fifty #46: ``workbench_sql.entity_daily_signals`` -- the one daily mart read behind Held? (Action
Center, Entity 360, Operations > Optimize) and the task / query-family watch arms.

Shape locks (sqlglot) plus an EXECUTED sqlite harness: the real builder output runs over seeded marts, with
the Snowflake builtins registered as Python functions and the VALUES column list rewritten (each rewrite asserts
its fragment exists, so a changed builder fails loudly instead of silently testing less). Pure: runs on the
floor-compat leg.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta

import pandas as pd
import pytest

from app.data import canary, workbench_sql
from app.logic import outcomes

sqlglot = pytest.importorskip("sqlglot")

TODAY = date(2026, 9, 29)
_COLS = ["ENTITY_TYPE", "ENTITY_KEY_U", "DAY", "CREDITS", "P95_SEC", "RUNS", "FAILS", "LOADED_THROUGH"]


@pytest.fixture(autouse=True)
def _pin_today(monkeypatch):
    monkeypatch.setattr(workbench_sql, "account_today", lambda: TODAY)


def _ents(*kinds: str, start: date = TODAY - timedelta(days=40)) -> list[tuple[str, str, date]]:
    keys = {"WAREHOUSE": "wh_a", "TASK": "db.s.load_a", "QUERY_FINGERPRINT": "abc123"}
    return [(k, keys[k], start) for k in kinds]


# ------------------------------------------------------------------------------ shape ----

@pytest.mark.parametrize("kinds", [("WAREHOUSE",), ("TASK",), ("QUERY_FINGERPRINT",),
                                   ("WAREHOUSE", "TASK"), ("TASK", "QUERY_FINGERPRINT"),
                                   ("WAREHOUSE", "TASK", "QUERY_FINGERPRINT")])
def test_named_selects_and_only_the_requested_arms(kinds):
    sql = workbench_sql.entity_daily_signals(_ents(*kinds))
    assert sqlglot.parse_one(sql, read="snowflake").named_selects == _COLS
    assert ("FACT_WAREHOUSE_DAILY" in sql) == ("WAREHOUSE" in kinds)
    assert ("MART_TASK_NODE_DAILY" in sql) == ("TASK" in kinds)
    assert ("MART_PATTERN_COST_DAILY" in sql) == ("QUERY_FINGERPRINT" in kinds)
    assert ("MART_QUERY_FAMILY_DAILY" in sql) == ("QUERY_FINGERPRINT" in kinds)
    # E5: the family arm joins through the UNION of both marts' (key, day) sets -- portable, no FULL join
    assert "FULL OUTER JOIN" not in sql.upper() and "FULL JOIN" not in sql.upper()
    # every UNION member of the loaded CTE names its columns (the first member names the union's columns)
    loaded = sql.split("loaded AS (", 1)[1].split("\n)\nSELECT", 1)[0]
    assert loaded.count("AS ENTITY_TYPE") == len(kinds) and loaded.count("AS LOADED_THROUGH") == len(kinds)


def test_central_today_mart_only_and_never_the_action_queue():
    sql = workbench_sql.entity_daily_signals(_ents("WAREHOUSE", "TASK", "QUERY_FINGERPRINT"))
    assert "CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE" in sql
    assert "CURRENT_DATE" not in sql
    assert "ACCOUNT_USAGE" not in sql
    # a queue write bumps the 'queue' cache domain; naming ACTION_QUEUE would re-cold this mart read
    assert "ACTION_QUEUE" not in sql.upper()
    assert "DAY < CONVERT_TIMEZONE" in sql and "DAY <= CONVERT_TIMEZONE" not in sql
    watch = workbench_sql.entity_daily_signals(_ents("TASK"), include_today=True)
    assert "DAY <= CONVERT_TIMEZONE" in watch and "DAY < CONVERT_TIMEZONE" not in watch


def test_dedupe_cap_start_clamp_and_empty():
    ents = [("WAREHOUSE", f"wh{i}", TODAY - timedelta(days=10)) for i in range(60)]
    ents += [("warehouse", " WH0 ", TODAY - timedelta(days=30)),        # repeat: earliest start wins
             ("USER", "x", TODAY), ("TASK", "", TODAY), ("TASK", "k", None), ("TASK", "k2", pd.NaT)]
    sql = workbench_sql.entity_daily_signals(ents)
    rows = re.findall(r"\('WAREHOUSE', '(WH\d+)', '([\d-]+)'\)", sql)
    assert len(rows) == workbench_sql.SIGNAL_MAX_ENTITIES == 40
    assert rows[0] == ("WH0", (TODAY - timedelta(days=30)).isoformat())
    assert "'USER'" not in sql and "'K'" not in sql and "'TASK'" not in sql
    old = workbench_sql.entity_daily_signals([("TASK", "k", TODAY - timedelta(days=2000))])
    assert f"'{(TODAY - timedelta(days=400)).isoformat()}'" in old          # clamped to 400 days
    assert workbench_sql.entity_daily_signals([]) == ""
    assert workbench_sql.entity_daily_signals([("USER", "x", TODAY), ("ALERT", "e", TODAY)]) == ""


def test_hostile_keys_stay_inside_literals():
    evil = "x'); DROP TABLE FACT_WAREHOUSE_DAILY; --\\"
    sql = workbench_sql.entity_daily_signals([(k, evil, TODAY - timedelta(days=5))
                                              for k in ("WAREHOUSE", "TASK", "QUERY_FINGERPRINT")])
    assert len(sqlglot.parse(sql, read="snowflake")) == 1
    residue = re.sub(r"'(?:[^'\\]|\\.|'')*'", "''", sql)
    assert "DROP" not in residue.upper()


def test_canaries_are_registered_and_parse():
    reg = dict(canary.CANARIES)
    for name in ("workbench.entity_daily_signals", "workbench.tracked_entity_actions"):
        assert name in reg
        assert sqlglot.parse_one(reg[name](), read="snowflake") is not None
    assert "FACT_WAREHOUSE_DAILY" in reg["workbench.entity_daily_signals"]()


# --------------------------------------------------------------------- executed (sqlite) ----

def _least(*args):
    return None if any(a is None for a in args) else min(args)


def _to_sqlite(sql: str) -> str:
    def swap(old: str, new: str) -> None:
        nonlocal sql
        assert old in sql, old
        sql = sql.replace(old, new)

    swap("CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE", f"'{TODAY.isoformat()}'")
    swap("DBA_MAINT_DB.OVERWATCH.", "")
    sql = sql.replace("::FLOAT", "")
    m = re.search(r"FROM \(VALUES\n(?P<rows>.*?)\n    \) AS v \(ENTITY_TYPE, ENTITY_KEY_U, START_DAY\)", sql, re.S)
    assert m, "the want CTE's VALUES shape changed"
    sql = sql.replace(m.group(0), "FROM (SELECT column1 AS ENTITY_TYPE, column2 AS ENTITY_KEY_U, column3 AS START_DAY "
                                  f"FROM (VALUES\n{m.group('rows')}\n    )) AS v")
    assert "::" not in sql and "CURRENT_TIMESTAMP" not in sql
    return sql


def _day(n: int) -> str:
    return (TODAY - timedelta(days=n)).isoformat()


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.create_function("TO_DATE", 1, lambda v: v)
    con.create_function("TO_VARCHAR", 1, lambda v: None if v is None else str(v))
    con.create_function("LEAST", -1, _least)
    con.execute("CREATE TABLE FACT_WAREHOUSE_DAILY (DAY TEXT, WAREHOUSE_NAME TEXT, COMPANY TEXT, CREDITS_TOTAL REAL)")
    con.execute("CREATE TABLE MART_TASK_NODE_DAILY (DAY TEXT, DATABASE_NAME TEXT, SCHEMA_NAME TEXT, TASK_NAME TEXT, "
                "RUNS REAL, FAILED REAL, P95_EXEC_SEC REAL)")
    con.execute("CREATE TABLE MART_PATTERN_COST_DAILY (DAY TEXT, QUERY_HASH TEXT, COMPANY TEXT, "
                "CREDITS_ATTRIBUTED REAL)")
    con.execute("CREATE TABLE MART_QUERY_FAMILY_DAILY (DAY TEXT, QUERY_HASH TEXT, COMPANY TEXT, RUNS REAL, "
                "FAILS REAL, P95_S REAL)")
    for n in range(1, 60):
        # warehouse: two companies' rows on one day sum; another warehouse never leaks in
        con.execute("INSERT INTO FACT_WAREHOUSE_DAILY VALUES (?, 'WH_A', 'ALFA', ?)", (_day(n), 10.0))
        con.execute("INSERT INTO FACT_WAREHOUSE_DAILY VALUES (?, 'wh_a', 'Trexis', ?)", (_day(n), 1.0))
        con.execute("INSERT INTO FACT_WAREHOUSE_DAILY VALUES (?, 'WH_B', 'ALFA', 99.0)", (_day(n),))
        con.execute("INSERT INTO MART_TASK_NODE_DAILY VALUES (?, 'DB', 'S', 'LOAD_A', 24, ?, 60)",
                    (_day(n), 1.0 if n == 5 else 0.0))
    con.execute("INSERT INTO FACT_WAREHOUSE_DAILY VALUES (?, 'WH_A', 'ALFA', 500.0)", (TODAY.isoformat(),))
    con.execute("INSERT INTO MART_TASK_NODE_DAILY VALUES (?, 'DB', 'S', 'LOAD_A', 3, 2, 60)", (TODAY.isoformat(),))
    # the family: cost only on day 3, runs only on day 2, both on day 4; the pattern-cost mart lags (day 3 max)
    con.execute("INSERT INTO MART_PATTERN_COST_DAILY VALUES (?, 'abc123', 'ALFA', 2.5)", (_day(3),))
    con.execute("INSERT INTO MART_PATTERN_COST_DAILY VALUES (?, 'ABC123', 'Trexis', 0.5)", (_day(4),))
    con.execute("INSERT INTO MART_PATTERN_COST_DAILY VALUES (?, 'ABC123', 'ALFA', 1.0)", (_day(4),))
    con.execute("INSERT INTO MART_QUERY_FAMILY_DAILY VALUES (?, 'ABC123', 'ALFA', 10, 1, 7.5)", (_day(2),))
    con.execute("INSERT INTO MART_QUERY_FAMILY_DAILY VALUES (?, 'ABC123', 'ALFA', 20, 0, 3.0)", (_day(4),))
    con.execute("INSERT INTO MART_QUERY_FAMILY_DAILY VALUES (?, 'ABC123', 'Trexis', 5, 2, 9.0)", (_day(4),))
    con.execute("INSERT INTO MART_QUERY_FAMILY_DAILY VALUES (?, 'OTHER', 'ALFA', 5, 2, 9.0)", (_day(1),))
    return con


def _run(sql: str) -> pd.DataFrame:
    con = _db()
    cur = con.execute(_to_sqlite(sql))
    return pd.DataFrame(cur.fetchall(), columns=[d[0] for d in cur.description])


def test_executed_warehouse_arm_sums_the_day_and_bounds_by_start_and_today():
    out = _run(workbench_sql.entity_daily_signals([("WAREHOUSE", "wh_a", TODAY - timedelta(days=30))]))
    assert list(out.columns) == _COLS
    assert set(out["ENTITY_KEY_U"]) == {"WH_A"}
    assert len(out) == 30 and out["DAY"].min() == _day(30) and out["DAY"].max() == _day(1)   # today excluded
    assert out["CREDITS"].eq(11.0).all()                                                      # both rows of a day
    assert out["P95_SEC"].isna().all() and out["RUNS"].isna().all()
    assert out["LOADED_THROUGH"].eq(_day(1)).all()


def test_executed_task_arm_and_the_include_today_watch_path():
    ents = [("TASK", "db.s.load_a", TODAY - timedelta(days=10))]
    out = _run(workbench_sql.entity_daily_signals(ents))
    assert set(out["ENTITY_KEY_U"]) == {"DB.S.LOAD_A"} and len(out) == 10
    assert out.set_index("DAY").loc[_day(5), "FAILS"] == 1.0
    assert out["P95_SEC"].eq(60.0).all() and out["CREDITS"].isna().all()
    watch = _run(workbench_sql.entity_daily_signals(ents, include_today=True))
    assert watch["DAY"].max() == TODAY.isoformat() and watch.set_index("DAY").loc[TODAY.isoformat(), "FAILS"] == 2
    assert watch["LOADED_THROUGH"].eq(TODAY.isoformat()).all()


def test_executed_family_arm_keeps_days_from_either_mart_and_the_lagging_loaded_bound():
    out = _run(workbench_sql.entity_daily_signals([("QUERY_FINGERPRINT", "abc123", TODAY - timedelta(days=10))]))
    by_day = out.set_index("DAY")
    assert list(by_day.index) == [_day(4), _day(3), _day(2)]              # ordered by day, one row each
    assert by_day.loc[_day(3), "CREDITS"] == 2.5 and pd.isna(by_day.loc[_day(3), "RUNS"])     # cost-only day
    assert pd.isna(by_day.loc[_day(2), "CREDITS"]) and by_day.loc[_day(2), "RUNS"] == 10.0     # runs-only day
    assert by_day.loc[_day(4), "CREDITS"] == 1.5 and by_day.loc[_day(4), "RUNS"] == 25.0       # both marts
    assert by_day.loc[_day(4), "P95_SEC"] == 9.0 and by_day.loc[_day(4), "FAILS"] == 2.0
    # the lower of the two marts' latest day: the pattern-cost mart stopped at day 3
    assert out["LOADED_THROUGH"].eq(_day(3)).all()


def test_executed_rows_feed_action_held_end_to_end():
    ents = [("WAREHOUSE", "WH_A", TODAY - timedelta(days=40))]
    out = _run(workbench_sql.entity_daily_signals(ents))
    # a flat 11 credits/day before and after: the level never dropped -> Not fixed (lifts the cooldown)
    res = outcomes.action_held("WAREHOUSE", "wh_a", TODAY - timedelta(days=12), out, TODAY)
    assert res["state"] == outcomes.NOT_FIXED and res["after_days"] == 11
