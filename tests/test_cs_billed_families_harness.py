"""Executed harness for mart_sql.cloud_svc_billed_families (v4.595): the REAL builder SQL runs in sqlite
(table FQNs and CURRENT_DATE substituted; LEAST / GREATEST / IFF / COUNT_IF / MAX_BY / REGEXP_INSTR shimmed
with Snowflake's NULL semantics) over seeded marts, then through cs_driver.billed_family_view.

Locks the billing arithmetic end to end — the string locks in test_cs_billed_families_sql.py cannot:
the in-progress metering day is never priced (review r1 F1), the sleep-polling total is capped per day as
a GROUP (r1 F2), an all-unmetered sleep total renders unpriced not $0 (r1 F11), and a statement that only
MENTIONS SYSTEM$WAIT is never flagged (r1 F3/F8).
"""

from __future__ import annotations

import re
import sqlite3

import pandas as pd
import pytest

from app.data import mart_sql
from app.logic import cs_driver

TODAY = "2026-09-26"
RATE = 3.68


def _to_sqlite(sql: str, days: int) -> str:
    sql = re.sub(r"\b[A-Z_]+\.[A-Z_]+\.((?:FACT|MART)_[A-Z_]+)", r"\1", sql)
    sql = sql.replace(f"DATEADD('day', -{days}, CURRENT_DATE())", f"date('{TODAY}', '-{days} day')")
    sql = sql.replace("[[:space:]]", r"\s")
    assert "CURRENT_DATE" not in sql and "DBA_MAINT_DB" not in sql
    return sql


def _least(*a):
    return None if any(x is None for x in a) else min(a)


def _greatest(*a):
    return None if any(x is None for x in a) else max(a)


def _regexp_instr(s, p):
    if s is None:
        return None
    m = re.search(p, s)
    return m.start() + 1 if m else 0


class _CountIf:
    def __init__(self):
        self.n = 0

    def step(self, c):
        if c:
            self.n += 1

    def finalize(self):
        return self.n


class _MaxBy:
    def __init__(self):
        self.best = None
        self.v = None

    def step(self, v, k):
        if k is not None and (self.best is None or k > self.best):
            self.best, self.v = k, v

    def finalize(self):
        return self.v


@pytest.fixture()
def db():
    c = sqlite3.connect(":memory:")
    c.create_function("LEAST", -1, _least)
    c.create_function("GREATEST", -1, _greatest)
    c.create_function("IFF", 3, lambda cond, a, b: a if cond else b)
    c.create_function("REGEXP_INSTR", 2, _regexp_instr)
    c.create_aggregate("COUNT_IF", 1, _CountIf)
    c.create_aggregate("MAX_BY", 2, _MaxBy)
    c.executescript("""
    CREATE TABLE FACT_METERING_DAILY(DAY TEXT, SERVICE_TYPE TEXT, CREDITS_COMPUTE REAL,
        CREDITS_CLOUD_SVCS REAL, CREDITS_ADJUSTMENT REAL);
    CREATE TABLE MART_CLOUD_SVC_DAILY(DAY TEXT, COMPANY TEXT, WAREHOUSE_NAME TEXT, USER_NAME TEXT,
        ROLE_NAME TEXT, QUERY_TYPE TEXT, QUERY_PARAMETERIZED_HASH TEXT, SAMPLE_TEXT TEXT, RUNS INT,
        CS_CREDITS REAL, EXEC_SEC_SUM REAL, COMPILE_SEC_SUM REAL);
    CREATE TABLE MART_QUERY_FAMILY_DAILY(DAY TEXT, COMPANY TEXT, QUERY_HASH TEXT, TOTAL_ELAPSED_SEC REAL,
        TOTAL_EXEC_SEC REAL, RUNS INT);
    CREATE TABLE FACT_APP_COST_DAILY(DAY TEXT, COMPANY TEXT, USER_NAME TEXT, APPLICATION TEXT, QUERIES INT);
    """)
    yield c
    c.close()


def _run(c, days: int = 7) -> pd.DataFrame:
    cur = c.execute(_to_sqlite(mart_sql.cloud_svc_billed_families(days), days))
    return pd.DataFrame(cur.fetchall(), columns=[d[0] for d in cur.description])


def _fam(c, day, h, text, cs, runs, wh="WH_TRXS_TRANSFORM", user="SYSTEM", qtype="CALL", compile_s=0.07):
    c.execute("INSERT INTO MART_CLOUD_SVC_DAILY VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              (day, "ALFA", wh, user, "R", qtype, h, text, runs, cs, 0.0, compile_s * runs))


def _meter(c, day, cs, adj):
    c.execute("INSERT INTO FACT_METERING_DAILY VALUES (?,?,?,?,?)", (day, "WAREHOUSE_METERING", 100.0, cs, adj))


_DAYS = [f"2026-09-{d:02d}" for d in range(19, 27)]     # 7 complete days + today (the in-progress UTC day)


def test_in_progress_metering_day_is_never_priced(db):
    for d in _DAYS[:-1]:
        _meter(db, d, 31.0, -16.0)                      # complete days: billed 15 (above the allowance)
        _fam(db, d, "H_CTM", "select system$wait(10)", 4.1, 2667, wh="WH_ALFA_TRANSFORM_PRD", user="CTM",
             qtype="SELECT")
    _meter(db, _DAYS[-1], 11.0, -11.0)                  # newest row = partial day, billed 0 so far
    _fam(db, _DAYS[-1], "H_CTM", "select system$wait(10)", 3.9, 2540, wh="WH_ALFA_TRANSFORM_PRD", user="CTM",
         qtype="SELECT")
    df = _run(db)
    row = df.iloc[0]
    assert row["METERED_DAYS"] == 7                      # the newest (in-progress) row is excluded ...
    assert row["UNDER_ALLOWANCE_DAYS"] == 0              # ... so it never reads as "under the allowance"
    assert row["LAST_METERED_DAY"] == _DAYS[-2]
    assert row["BILLED_CS_CREDITS"] == pytest.approx(4.1 * 7)
    assert row["SCOPE_UNMETERED_CS_CREDITS"] == pytest.approx(3.9)   # the in-progress day: unpriced, disclosed
    _, s = cs_driver.billed_family_view(df, RATE)
    assert s["sleep_usd"] == pytest.approx(4.1 * 7 * RATE)
    head, _ = cs_driver.billing_basis_note(s, RATE)
    assert "on all 7 complete metered days" in head and "left unpriced" in head


def test_sleep_total_is_capped_per_day_as_a_group(db):
    d = _DAYS[-2]
    _meter(db, d, 25.0, -20.0)                          # the account billed 5 CS credits that day
    _meter(db, _DAYS[-1], 1.0, 0.0)                     # newest row (excluded as in progress)
    for h, text, cs, runs, user, qtype in (
            ("H_CTM", "select system$wait(10)", 4.1, 2667, "CTM", "SELECT"),
            ("H_W30", "CALL SYSTEM$WAIT(30)", 2.3, 500, "SYSTEM", "CALL"),
            ("H_W60", "CALL SYSTEM$WAIT(60)", 1.5, 158, "SYSTEM", "CALL"),
            ("H_W1200", "CALL SYSTEM$WAIT(1200)", 0.3, 2, "SYSTEM", "CALL")):
        _fam(db, d, h, text, cs, runs, user=user, qtype=qtype)
    df = _run(db)
    # per-family marginal: each alone is under the day's 5 billed credits, so each is 1:1 ...
    assert df.set_index("QUERY_PARAMETERIZED_HASH")["BILLED_CS_CREDITS"].to_dict() == pytest.approx(
        {"H_CTM": 4.1, "H_W30": 2.3, "H_W60": 1.5, "H_W1200": 0.3})
    # ... but together they are 8.2 against 5 billed: stopping them all saves at most 5
    assert df["SLEEP_BILLED_CS_CREDITS_ALL"].iloc[0] == pytest.approx(5.0)
    assert df["SLEEP_CS_CREDITS_ALL"].iloc[0] == pytest.approx(8.2)
    _, s = cs_driver.billed_family_view(df, RATE)
    assert s["sleep_usd"] == pytest.approx(5.0 * RATE)


def test_all_unmetered_sleep_is_unpriced_not_zero(db):
    _meter(db, _DAYS[0], 31.0, -16.0)                   # a complete day with no sleep on it
    _meter(db, _DAYS[-1], 11.0, -11.0)                  # newest row (in progress)
    _fam(db, _DAYS[0], "H_BIG", "select a from big_table", 9.0, 50, wh="WH_Q", user="U", qtype="SELECT")
    _fam(db, _DAYS[-1], "H_CTM", "select system$wait(10)", 3.9, 2540, user="CTM", qtype="SELECT")
    df = _run(db)
    assert pd.isna(df["SLEEP_BILLED_CS_CREDITS_ALL"].iloc[0])
    view, s = cs_driver.billed_family_view(df, RATE)
    assert s["metered_days"] == 1 and s["sleep_families"] == 1
    assert pd.isna(s["sleep_usd"])                       # the card renders "—", never $0.00
    ctm = view[view["DRIVER_CLASS"] == cs_driver.SLEEP_POLLING].iloc[0]
    assert pd.isna(ctm["BILLED_CS_USD"])


def test_mentions_are_not_flagged_and_sleeps_ride_past_the_top_n(db):
    d = _DAYS[-2]
    _meter(db, d, 200.0, -10.0)
    _meter(db, _DAYS[-1], 1.0, 0.0)
    for i in range(mart_sql.CS_BILLED_TOP_N + 5):         # 55 heavier ordinary families
        _fam(db, d, f"H{i:03d}", f"select col{i} from t{i}", 2.0 + i / 100, 100, wh="WH_Q", user="U",
             qtype="SELECT", compile_s=0.9)
    _fam(db, d, "H_DIAG", "select query_text from qh where query_text ilike '%system$wait(%'", 0.5, 6,
         wh="WH_Q", user="ANALYST", qtype="SELECT")
    _fam(db, d, "H_CMT", "select 1 -- system$wait(10)", 0.4, 30, wh="WH_Q", user="ANALYST", qtype="SELECT")
    _fam(db, d, "H_LOOP", "BEGIN LOOP CALL SYSTEM$WAIT(60); END LOOP; END;", 0.3, 4)
    _fam(db, d, "H_W30", "CALL SYSTEM$WAIT(30)", 0.2, 500)   # a real sleep, ranked below the top 50
    # a multi-statement parent whose first statement is the sleep: its child (H_W30-like) carries it
    _fam(db, d, "H_MS", "select system$wait(30); call dw.load_orders()", 0.1, 5, qtype="MULTI_STATEMENT")
    df = _run(db)
    flagged = set(df.loc[df["SLEEP_FLAG"] == 1, "QUERY_PARAMETERIZED_HASH"])
    assert flagged == {"H_W30"}
    assert "H_W30" in set(df["QUERY_PARAMETERIZED_HASH"])          # pulled in past the top-N cut
    assert not {"H_DIAG", "H_CMT", "H_LOOP", "H_MS"} & set(df["QUERY_PARAMETERIZED_HASH"])  # below the cut
    assert df["SLEEP_FAMILIES_ALL"].iloc[0] == 1
    assert df["SCOPE_CS_CREDITS"].iloc[0] == pytest.approx(
        sum(2.0 + i / 100 for i in range(mart_sql.CS_BILLED_TOP_N + 5)) + 0.5 + 0.4 + 0.3 + 0.2 + 0.1)
    # the compile-ranking blind spot: only the families averaging <= 0.5 s compile
    assert df["LOW_COMPILE_CS_CREDITS_ALL"].iloc[0] == pytest.approx(0.5 + 0.4 + 0.3 + 0.2 + 0.1)
    view, s = cs_driver.billed_family_view(df, RATE)
    assert s["sleep_families"] == 1 and s["sleep_hours_complete"] is True
    assert list(view["DRIVER_CLASS"]).count(cs_driver.SLEEP_POLLING) == 1
