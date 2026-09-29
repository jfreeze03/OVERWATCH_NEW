"""Next-Fifty #46(d): ``mart_sql.ledger_before_after`` EXECUTED in sqlite, then measured.

The real builder SQL runs over seeded FACT_WAREHOUSE_DAILY / MART_WAREHOUSE_EFFICIENCY_DAILY /
FACT_OBJECT_COST_DAILY / MART_TABLE_STORAGE_DAILY rows, with the Snowflake builtins it uses (IFF, DATEADD,
TO_DATE, MAX_BY) registered as Python functions and the account-time "today" pinned; each rewrite asserts
its fragment exists, so a changed builder fails loudly. The executed row then goes through
``ledger_measure.ledger_measurement`` and must equal the hand calculation. Pure: floor-compat leg too."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pandas as pd
import pytest
from test_task_evidence_harness import _MaxBy

from app.data import mart_sql
from app.data.common import account_today_sql
from app.logic.ledger_measure import MEASURED, NO_DATA, TB, ledger_measurement

_BOOKED = date(2026, 8, 1)


def _d(offset: int) -> str:
    return (_BOOKED + timedelta(days=offset)).isoformat()


def _dateadd(unit, n, value):
    assert str(unit).lower() == "day"
    return None if value is None else (date.fromisoformat(str(value)[:10]) + timedelta(days=int(n))).isoformat()


def _to_sqlite(sql: str, today: date) -> str:
    def swap(old: str, new: str) -> None:
        nonlocal sql
        assert old in sql, old
        sql = sql.replace(old, new)

    swap(account_today_sql(), f"'{today.isoformat()}'")
    swap("DBA_MAINT_DB.OVERWATCH.", "")
    assert "::" not in sql and "CURRENT_TIMESTAMP" not in sql
    return sql


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.create_function("IFF", 3, lambda c, a, b: a if c else b)
    con.create_function("DATEADD", 3, _dateadd)
    con.create_function("TO_DATE", 1, lambda v: None if v is None else str(v)[:10])
    con.create_aggregate("MAX_BY", 2, _MaxBy)
    con.execute("CREATE TABLE FACT_WAREHOUSE_DAILY (DAY TEXT, WAREHOUSE_NAME TEXT, CREDITS_TOTAL REAL)")
    con.execute("CREATE TABLE MART_WAREHOUSE_EFFICIENCY_DAILY (DAY TEXT, WAREHOUSE_NAME TEXT, QUERIES REAL)")
    con.execute("CREATE TABLE FACT_OBJECT_COST_DAILY (DAY TEXT, OBJECT_FQN TEXT, COST_ARM TEXT, CREDITS REAL)")
    con.execute("CREATE TABLE MART_TABLE_STORAGE_DAILY (DAY TEXT, DATABASE_NAME TEXT, SCHEMA_NAME TEXT, "
                "TABLE_NAME TEXT, TIME_TRAVEL_BYTES REAL, RETENTION_DAYS REAL)")
    wh, eff = [], []
    for off in range(-20, 45):
        # 10 credits/day before the booking, 4/day after; the booking day itself burns 1000 (partial, skipped)
        credits = 1000.0 if off == 0 else 10.0 if off < 0 else 4.0
        wh.append((_d(off), "WH_A", credits))
        eff.append((_d(off), "WH_A", 100.0 if off < 0 else 150.0))
    wh.append((_d(5), "WH_OTHER", 99.0))
    con.executemany("INSERT INTO FACT_WAREHOUSE_DAILY VALUES (?, ?, ?)", wh)
    con.executemany("INSERT INTO MART_WAREHOUSE_EFFICIENCY_DAILY VALUES (?, ?, ?)", eff)
    obj = [(_d(off), '"DB"."S"."T"' if off % 2 else "db.s.t", "CLUSTERING", 2.0 if off < 0 else 0.5)
           for off in range(-20, 45) if off != 0]
    obj += [(_d(-3), "DB.S.T", "SERVERLESS_TASK", 500.0), (_d(3), "DB.S.OTHER", "CLUSTERING", 500.0)]
    con.executemany("INSERT INTO FACT_OBJECT_COST_DAILY VALUES (?, ?, ?, ?)", obj)
    snaps = [(_d(-16), 9 * TB, 30.0), (_d(-2), 3 * TB, 30.0), (_d(-1), 2 * TB, 30.0),
             (_d(0), 5 * TB, 30.0), (_d(4), 1.5 * TB, 1.0), (_d(9), 1 * TB, 1.0)]
    con.executemany("INSERT INTO MART_TABLE_STORAGE_DAILY VALUES (?, 'DB', 'S', 'T', ?, ?)", snaps)
    return con


def _row(basis: str, target: str, today: date) -> pd.Series:
    sql = _to_sqlite(mart_sql.ledger_before_after(basis, target, _BOOKED), today)
    df = pd.read_sql_query(sql, _db())
    assert len(df) == 1
    return df.iloc[0]


def test_warehouse_windows_exclude_the_booking_day_and_today():
    today = _BOOKED + timedelta(days=20)
    r = _row("WAREHOUSE", "wh_a", today)
    assert r["BEFORE_CREDITS"] == 14 * 10.0                       # exactly 14 days; the 1000 is skipped
    assert r["AFTER_CREDITS"] == 19 * 4.0                         # days 1..19: today (day 20) is partial
    assert r["LOADED_THROUGH"] == _d(19)                          # max loaded day before today
    assert r["BEFORE_QUERIES"] == 14 * 100.0 and r["AFTER_QUERIES"] == 19 * 150.0
    m = ledger_measurement(r, basis="WAREHOUSE", rate=3.0, storage_usd_per_tb=23.0, today=today)
    assert m["state"] == MEASURED and m["after_days"] == 19
    assert m["monthly_usd"] == round((140 / 14 - 76 / 19) * 30 * 3.0, 2) == 540.0
    assert m["volume_ratio"] == 1.5 and m["confounded"] is True


def test_warehouse_after_window_caps_at_thirty_days():
    today = _BOOKED + timedelta(days=60)
    r = _row("WAREHOUSE", "WH_A", today)
    assert r["AFTER_CREDITS"] == 30 * 4.0                         # days 1..30, not 31..44
    assert r["AFTER_QUERIES"] == 30 * 150.0
    assert r["LOADED_THROUGH"] == _d(44)
    m = ledger_measurement(r, basis="WAREHOUSE", rate=3.0, storage_usd_per_tb=23.0, today=today)
    assert m["after_days"] == 30 and m["prefill_usd"] == round((10 - 4) * 30 * 3.0, 2)


def test_volume_stops_where_the_credit_fact_stops():
    con = _db()
    con.execute("DELETE FROM FACT_WAREHOUSE_DAILY WHERE DAY > ?", (_d(10),))   # the credit loader stalled
    today = _BOOKED + timedelta(days=20)
    sql = _to_sqlite(mart_sql.ledger_before_after("WAREHOUSE", "WH_A", _BOOKED), today)
    r = pd.read_sql_query(sql, con).iloc[0]
    assert r["LOADED_THROUGH"] == _d(10)
    assert r["AFTER_CREDITS"] == 10 * 4.0 and r["AFTER_QUERIES"] == 10 * 150.0   # the same 10 days
    m = ledger_measurement(r, basis="WAREHOUSE", rate=3.0, storage_usd_per_tb=23.0, today=today)
    assert m["after_days"] == 10 and m["volume_ratio"] == 1.5


def test_object_basis_matches_quoted_and_plain_fqns_on_the_maintenance_arms_only():
    today = _BOOKED + timedelta(days=20)
    r = _row("OBJECT", "db.s.t", today)
    assert r["BEFORE_CREDITS"] == 14 * 2.0                        # the SERVERLESS_TASK 500 is not maintenance
    assert r["AFTER_CREDITS"] == 19 * 0.5 and pd.isna(r["BEFORE_QUERIES"])
    m = ledger_measurement(r, basis="OBJECT", rate=2.0, storage_usd_per_tb=23.0, today=today)
    assert m["monthly_usd"] == round((2.0 - 0.5) * 30 * 2.0, 2) and m["volume_ratio"] is None


def test_table_basis_takes_the_last_snapshot_before_and_the_latest_after():
    today = _BOOKED + timedelta(days=12)
    r = _row("TABLE", "db.s.t", today)
    assert r["BEFORE_TT_BYTES"] == 2 * TB and r["BEFORE_SNAPSHOT_DAY"] == _d(-1)   # not the booking-day 5 TB
    assert r["AFTER_TT_BYTES"] == 1 * TB and r["AFTER_SNAPSHOT_DAY"] == _d(9)
    assert r["BEFORE_RETENTION_DAYS"] == 30 and r["AFTER_RETENTION_DAYS"] == 1
    m = ledger_measurement(r, basis="TABLE", rate=3.0, storage_usd_per_tb=23.0, today=today)
    assert m["state"] == MEASURED and m["after_days"] == 9 and m["monthly_usd"] == 23.0


@pytest.mark.parametrize("basis", ["WAREHOUSE", "OBJECT", "TABLE"])
def test_an_unknown_target_still_returns_one_row_that_reads_no_data(basis):
    today = _BOOKED + timedelta(days=20)
    r = _row(basis, "NOPE" if basis == "WAREHOUSE" else "DB.S.NOPE", today)
    assert r["BOOKED_DAY"] == _BOOKED.isoformat()
    m = ledger_measurement(r, basis=basis, rate=3.0, storage_usd_per_tb=23.0, today=today)
    assert m["state"] == NO_DATA and m["prefill_usd"] == 0.0
