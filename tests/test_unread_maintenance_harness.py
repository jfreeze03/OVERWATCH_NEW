"""Next-Fifty #30: the unread-maintenance SQL EXECUTED in sqlite, through the verdicts.

- ``cost_sql.maintenance_on_unread`` runs over a seeded FACT_OBJECT_COST_DAILY: a quoted / mixed-case
  maintenance FQN and its read-arm twin merge into ONE key (so it is not unread), a legacy QUERY_COMPUTE row
  counts as a read, a write-only object stays unread, the 1-credit floor, today excluded, the MV domain, and
  the window totals (CANDIDATES_WIN, MAINT_CREDITS_WIN) count past the LIMIT.
- ``insights_sql.object_reads_confirm`` runs with LATERAL FLATTEN rewritten to sqlite json_each: write-wins
  (a MERGE that reads its own target is its writer, not a reader), the id-or-name UNION (a read matched both
  ways counts once), the TABLES bridge skips dropped objects, the share guard, and exactly one row per FQN.
Every rewrite asserts its fragment exists, so a changed builder fails loudly. Pure: runs on the floor leg."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from app.data import cost_sql, insights_sql
from app.logic.unread_maintenance import unread_maintenance_verdicts

_TODAY = date(2026, 9, 29)
_NOW = "2026-09-29 08:00:00"


def _dateadd(unit, n, value):
    text = str(value)
    if len(text) == 10:
        d = datetime.strptime(text, "%Y-%m-%d") + timedelta(**{f"{str(unit).lower()}s": int(n)})
        return d.strftime("%Y-%m-%d")
    ts = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S") + timedelta(**{f"{str(unit).lower()}s": int(n)})
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def _split_part(text, delim, n):
    if text is None:
        return None
    parts = str(text).split(str(delim))
    n = int(n)
    return parts[n - 1] if 0 < n <= len(parts) else ""


class _CountIf:
    def __init__(self):
        self.n = 0

    def step(self, cond):
        self.n += 1 if cond else 0

    def finalize(self):
        return self.n


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.create_function("IFF", 3, lambda c, a, b: a if c else b)
    con.create_function("DATEADD", 3, _dateadd)
    con.create_function("SPLIT_PART", 3, _split_part)
    con.create_function("COMPANY_FOR_DATABASE", 1, lambda db: "Trexis" if str(db).upper() == "TRX" else "ALFA")
    con.create_aggregate("COUNT_IF", 1, _CountIf)
    return con


def _swap(sql: str, old: str, new: str) -> str:
    assert old in sql, old
    return sql.replace(old, new)


# --- step 1: the mart shortlist ------------------------------------------------------------------------

def _d(days_ago: int) -> str:
    return (_TODAY - timedelta(days=days_ago)).isoformat()


def _ledger_rows() -> list[tuple]:
    rows: list[tuple] = []

    def add(fqn, arm, credits, days_ago, company="ALFA", domain="TABLE"):
        rows.append((_d(days_ago), fqn, domain, arm, company, float(credits)))

    for k in range(10):
        add("DB.S.MyTable", "CLUSTERING", 5, 5 + k)                     # concatenated maintenance spelling
    add('DB.S."MyTable"', "QUERY_COMPUTE_READ", 0.1, 7, domain="Table")  # ACCESS_HISTORY objectName spelling
    for k in range(3):
        add("DB.S.LEGACY", "CLUSTERING", 3, 10 + k)
    add("DB.S.LEGACY", "QUERY_COMPUTE", 0.2, 20, domain="Table")         # pre-V050 role-less = a read
    for k in range(5):
        add("DB.S.WRITEONLY", "SEARCH_OPT", 2, 40 + k)                   # all outside the last 30 days
    add("DB.S.WRITEONLY", "QUERY_COMPUTE_WRITE", 1.0, 41, domain="Table")
    add("DB.S.TINY", "CLUSTERING", 0.5, 3)                               # under the 1-credit floor
    add("DB.S.OLDREAD", "CLUSTERING", 4, 8)
    add("DB.S.OLDREAD", "QUERY_COMPUTE_READ", 9.0, 100, domain="Table")  # read, but outside the 90 days
    add("DB.S.TODAY", "CLUSTERING", 10, 0)                               # today is excluded
    add("DB.S.MV1", "MV_REFRESH", 2, 4, domain="MATERIALIZED_VIEW")
    add("DB.S.MV1", "CLUSTERING", 1, 4)                                  # the clustering arm says TABLE
    add("UNATTRIBUTED", "CLUSTERING", 50, 2)
    add("TRX.S.OTHER", "CLUSTERING", 7, 6, company="Trexis")
    for i in range(60):                                                  # push the candidates past LIMIT 50
        add(f"DB.F.T{i:02d}", "CLUSTERING", 1.5, 60)
    return rows


def _shortlist(company: str = "ALL") -> pd.DataFrame:
    con = _con()
    con.execute("CREATE TABLE FACT_OBJECT_COST_DAILY (DAY TEXT, OBJECT_FQN TEXT, OBJECT_DOMAIN TEXT, "
                "COST_ARM TEXT, COMPANY TEXT, CREDITS REAL)")
    con.executemany("INSERT INTO FACT_OBJECT_COST_DAILY VALUES (?, ?, ?, ?, ?, ?)", _ledger_rows())
    sql = cost_sql.maintenance_on_unread(90, company)
    sql = _swap(sql, "DBA_MAINT_DB.OVERWATCH.FACT_OBJECT_COST_DAILY", "FACT_OBJECT_COST_DAILY")
    sql = _swap(sql, "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(", "COMPANY_FOR_DATABASE(")
    sql = _swap(sql, "CURRENT_DATE()", f"'{_TODAY.isoformat()}'")
    assert "::" not in sql and "QUALIFY" not in sql
    return pd.read_sql_query(sql, con)


@pytest.fixture(scope="module")
def shortlist() -> pd.DataFrame:
    return _shortlist()


def test_normalized_key_merges_the_read_twin(shortlist):
    keys = {str(f).upper().replace('"', "") for f in shortlist["OBJECT_FQN"]}
    assert "DB.S.MYTABLE" not in keys                  # the quoted read twin merged -> read -> not a candidate
    assert "DB.S.LEGACY" not in keys                   # legacy QUERY_COMPUTE counts as a read


def test_unread_candidates_and_their_arms(shortlist):
    full = {**{r["OBJECT_FQN"]: r for _, r in shortlist.iterrows()}}
    wo = full["DB.S.WRITEONLY"]
    assert (wo["SEARCH_OPT_CREDITS"], wo["MAINT_CREDITS"], wo["WRITE_CREDITS"]) == (10.0, 10.0, 1.0)
    assert wo["MAINT_CREDITS_30D"] == 0.0 and wo["MAINT_DAYS"] == 5
    old = full["DB.S.OLDREAD"]
    assert old["MAINT_CREDITS"] == 4.0 and old["MAINT_CREDITS_30D"] == 4.0
    mv = full["DB.S.MV1"]
    assert mv["OBJECT_DOMAIN"] == "MATERIALIZED_VIEW" and mv["MAINT_CREDITS"] == 3.0
    assert "DB.S.TINY" not in full and "DB.S.TODAY" not in full and "UNATTRIBUTED" not in full
    assert full["TRX.S.OTHER"]["COMPANY"] == "Trexis" and mv["COMPANY"] == "ALFA"
    # ordered by the last 30 complete days, then the window
    assert list(shortlist["OBJECT_FQN"][:3]) == ["TRX.S.OTHER", "DB.S.OLDREAD", "DB.S.MV1"]


def test_window_totals_count_past_the_limit(shortlist):
    assert len(shortlist) == 50
    # 60 fillers + WRITEONLY + OLDREAD + MV1 + TRX.S.OTHER, all counted before the LIMIT
    assert set(shortlist["CANDIDATES_WIN"]) == {64}
    assert set(shortlist["MAINT_CREDITS_WIN"]) == {round(60 * 1.5 + 10 + 4 + 3 + 7, 4)}
    assert set(shortlist["MAINT_CREDITS_30D_WIN"]) == {4 + 3 + 7}
    assert set(shortlist["COVERAGE_START_DAY"]) == {_d(60)}          # the window's earliest ledger day
    # the newest in-scope day (the UNATTRIBUTED row two days ago is out of scope; today never counts)
    assert set(shortlist["LEDGER_LAST_DAY"]) == {_d(3)}


def test_a_named_company_filters_rows():
    alfa = _shortlist("ALFA")
    assert "TRX.S.OTHER" not in set(alfa["OBJECT_FQN"]) and set(alfa["CANDIDATES_WIN"]) == {63}
    trx = _shortlist("Trexis")
    assert list(trx["OBJECT_FQN"]) == ["TRX.S.OTHER"]


# --- step 2: the access-history confirm (LATERAL FLATTEN -> json_each) -----------------------------------

def _obj(oid, name):
    return {"objectId": oid, "objectName": name, "objectDomain": "Table"}


def _confirm(fqns, *, history, tables, shares) -> pd.DataFrame:
    con = _con()
    con.execute("CREATE TABLE TABLES (TABLE_ID INTEGER, TABLE_CATALOG TEXT, TABLE_SCHEMA TEXT, TABLE_NAME TEXT, "
                "DELETED TEXT)")
    con.executemany("INSERT INTO TABLES VALUES (?, ?, ?, ?, ?)", tables)
    con.execute("CREATE TABLE GRANTS_TO_ROLES (NAME TEXT, GRANTED_TO TEXT, GRANTED_ON TEXT, DELETED_ON TEXT)")
    con.executemany("INSERT INTO GRANTS_TO_ROLES VALUES (?, ?, ?, ?)", shares)
    con.execute("CREATE TABLE ACCESS_HISTORY (QUERY_ID TEXT, QUERY_START_TIME TEXT, USER_NAME TEXT, "
                "BASE_OBJECTS_ACCESSED TEXT, OBJECTS_MODIFIED TEXT)")
    con.executemany("INSERT INTO ACCESS_HISTORY VALUES (?, ?, ?, ?, ?)",
                    [(q, t, u, json.dumps(r), json.dumps(w)) for q, t, u, r, w in history])
    sql = insights_sql.object_reads_confirm(fqns, 90)
    for view in ("TABLES", "GRANTS_TO_ROLES", "ACCESS_HISTORY"):
        sql = _swap(sql, f"SNOWFLAKE.ACCOUNT_USAGE.{view}", view)
    sql = _swap(sql, "CURRENT_TIMESTAMP()", f"'{_NOW}'")
    sql = _swap(sql, "FROM VALUES ", "FROM (VALUES ")
    sql = _swap(sql, "),\nids AS (", ")),\nids AS (")
    for src in ("BASE_OBJECTS_ACCESSED", "OBJECTS_MODIFIED"):
        sql = _swap(sql, f"LATERAL FLATTEN(input => ah.{src}) f", f"json_each(ah.{src}) f")
    sql = re.sub(r'f\.value:"(\w+)"', r"json_extract(f.value, '$.\1')", sql)
    sql = re.sub(r"::(VARCHAR|NUMBER|STRING)\b", "", sql)
    assert "::" not in sql and "FLATTEN" not in sql and ':"' not in sql
    return pd.read_sql_query(sql, con)


_T = "2026-09-20 10:00:00"
_HISTORY = [
    ("q1", _T, "ANA", [_obj(101, "DB.S.X")], []),                                     # a plain read by id
    ("q2", _T, "ETL", [_obj(102, "DB.S.Y")], [_obj(102, "DB.S.Y")]),                  # MERGE reads its own target
    ("q3", _T, "BOB", [_obj(None, 'DB.S."Z"')], []),                                  # name-only (no TABLES row)
    ("q4", _T, "ANA", [_obj(104, "DB.S.W"), _obj(None, "db.s.w")], []),               # id AND name in one query
    ("q5", _T, "CARL", [_obj(104, "DB.S.W")], []),
    ("q6", "2026-05-01 10:00:00", "OLD", [_obj(106, "DB.S.N")], []),                 # outside 90 days
    ("q7", _T, "ETL", [], [_obj(107, "DB.S.D_OLD")]),                                 # a dropped object's id
    ("q8", _T, "DAN", [_obj(108, "DB.S.E_OLDNAME")], []),                             # renamed: matched by id
]
_TABLES = [(101, "DB", "S", "X", None), (102, "DB", "S", "Y", None), (104, "DB", "S", "W", None),
           (106, "DB", "S", "N", None), (107, "DB", "S", "D", "2026-09-01 00:00:00"), (108, "DB", "S", "E", None),
           (200, "SHAREDB", "S", "T", None)]
_SHARES = [("SHAREDB", "SHARE", "DATABASE", None), ("OLDSHARE", "SHARE", "DATABASE", "2026-01-01 00:00:00"),
           ("DB", "ROLE", "DATABASE", None)]
_FQNS = ["DB.S.X", "DB.S.Y", "DB.S.Z", "DB.S.W", "DB.S.N", "DB.S.D", "DB.S.E", "SHAREDB.S.T", "OLDSHARE.S.T"]


@pytest.fixture(scope="module")
def reads() -> pd.DataFrame:
    return _confirm(_FQNS, history=_HISTORY, tables=_TABLES, shares=_SHARES).set_index("OBJECT_FQN")


def test_one_row_per_shortlisted_object(reads):
    assert sorted(reads.index) == sorted(_FQNS)


def test_write_wins_and_the_id_or_name_union(reads):
    assert (reads.loc["DB.S.X", "READ_QUERIES"], reads.loc["DB.S.X", "MATCHED_BY_ID"]) == (1, 1)
    assert (reads.loc["DB.S.Y", "READ_QUERIES"], reads.loc["DB.S.Y", "WRITE_QUERIES"]) == (0, 1)   # write wins
    assert (reads.loc["DB.S.Z", "READ_QUERIES"], reads.loc["DB.S.Z", "MATCHED_BY_ID"]) == (1, 0)   # by name
    assert reads.loc["DB.S.W", "READ_QUERIES"] == 2 and reads.loc["DB.S.W", "READ_USERS"] == 2     # q4 once
    assert reads.loc["DB.S.W", "LAST_READ"] == _T
    assert reads.loc["DB.S.N", "READ_QUERIES"] == 0                                              # too old
    # the id bridge finds a read under an old name, but never through a dropped TABLES row (DELETED)
    assert (reads.loc["DB.S.E", "READ_QUERIES"], reads.loc["DB.S.E", "MATCHED_BY_ID"]) == (1, 1)
    assert (reads.loc["DB.S.D", "MATCHED_BY_ID"], reads.loc["DB.S.D", "WRITE_QUERIES"]) == (0, 0)


def test_the_share_guard(reads):
    assert reads.loc["SHAREDB.S.T", "SHARED_DATABASE"] == 1
    assert reads.loc["OLDSHARE.S.T", "SHARED_DATABASE"] == 0       # a revoked share grant
    assert reads.loc["DB.S.X", "SHARED_DATABASE"] == 0            # a role grant is not a share


def test_executed_frames_through_the_verdicts(shortlist):
    short = shortlist[shortlist["OBJECT_FQN"].isin(["DB.S.WRITEONLY", "DB.S.OLDREAD", "DB.S.MV1"])]
    conf = _confirm(list(short["OBJECT_FQN"]), history=[
        ("r1", _T, "ANA", [_obj(None, "DB.S.OLDREAD")], [])], tables=[], shares=[])
    out = unread_maintenance_verdicts(short, conf, rate=3.0).set_index("OBJECT_FQN")
    assert out.loc["DB.S.OLDREAD", "VERDICT"] == "Keep"            # the ledger missed a read access history has
    assert out.loc["DB.S.WRITEONLY", "VERDICT"] == "No recent spend"
    assert out.loc["DB.S.MV1", "VERDICT"] == "Suspend MV refresh"
    assert out.loc["DB.S.MV1", "REVIEW_SQL"].startswith("ALTER MATERIALIZED VIEW DB.S.MV1 SUSPEND RECLUSTER;")
    assert out.loc["DB.S.MV1", "EST_MONTHLY_USD"] == 9.0
