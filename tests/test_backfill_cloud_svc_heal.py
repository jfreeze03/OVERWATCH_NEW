"""backfill_365.sql heals MART_CLOUD_SVC_DAILY's history (V166 round, HEAL-CS-MART; the R2-012 follow-up).

SP_LOAD_CLOUD_SVC_MART (V055) merges only the last 2 days of the 72h extract, so the cloud-services statement mart
starts at its first load, and again after a teardown drops it; the app discloses the short coverage (R2-012) but
nothing ever filled it. backfill_365.sql now carries one plain INSERT arm, right after the FACT_QUERY_DAILY arm and
OUTSIDE the task-suspend window (it reads ACCOUNT_USAGE, not the extract):
  * placement -- a top-level statement between the FACT_QUERY_DAILY INSERT and ``SET backfill_started``, so the
    suspend-window locks (tests/test_backfill_suspend_window.py) are untouched;
  * parity -- normalize-and-compare against the LATEST SP_LOAD_CLOUD_SVC_MART MERGE source: exactly three deltas
    (Central-pinned DAY, QUERY_HISTORY for the extract, the 364-day only-older-days window);
  * time zone -- the pin is formulas.ACCOUNT_TIMEZONE and the arm has no bare DATE(START_TIME) / CURRENT_DATE();
  * executed -- the arm itself in sqlite: only days before the mart's first day, the loader's grain and defaults,
    CS = 0 rows out, idempotent, an empty mart stops short of the loader's 3 days, and a shared day equals what the
    loader's own source SELECT produces.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests._source import read
from tests.test_backfill_suspend_window import _latest_proc, _statements

_BF = read("snowflake/backfill_365.sql")
_HEAD = "INSERT INTO DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY"
_CT = ZoneInfo("America/Chicago")
# the heal window (the test's own copy): the three deltas' right-hand sides
_HEAL_WINDOW = """WHERE START_TIME >= DATEADD('day', -366, CURRENT_TIMESTAMP())
      AND CONVERT_TIMEZONE('America/Chicago', START_TIME)::DATE
          >= DATEADD('day', -364, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE)
      AND CONVERT_TIMEZONE('America/Chicago', START_TIME)::DATE
          < COALESCE((SELECT MIN(m.DAY) FROM DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY m),
                     DATEADD('day', -2, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE))"""


def _heal() -> str:
    hits = [s for s in _statements(_BF) if s.startswith(_HEAD)]
    assert len(hits) == 1, len(hits)
    return hits[0]


def _norm(sql: str) -> str:
    return " ".join(re.sub(r"--[^\n]*", "", sql).split())


def _loader_source() -> str:
    body = _latest_proc("SP_LOAD_CLOUD_SVC_MART")
    return body[body.index("USING (\n") + len("USING (\n"):body.index("\n    ) s\n")]


def test_heal_sits_after_the_query_fact_and_outside_the_suspend_window():
    stmts = _statements(_BF)
    at = [i for i, s in enumerate(stmts) if s.startswith(_HEAD)]
    qd = [i for i, s in enumerate(stmts) if s.startswith("INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY")]
    started = stmts.index("SET backfill_started = CURRENT_TIMESTAMP()")
    assert len(at) == 1 and len(qd) == 1
    assert qd[0] + 1 == at[0] < started                    # right after FACT_QUERY_DAILY, before the window opens
    assert stmts[started + 1] == "ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY SUSPEND"


def test_heal_is_the_loaders_aggregation_with_exactly_three_deltas():
    src = _norm(_loader_source())
    old_window = _norm("WHERE START_TIME >= DATEADD('day', -2, CURRENT_DATE())")
    assert src.count("DATE(START_TIME) AS DAY") == 1 and src.count("DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT") == 1
    assert src.count(old_window) == 1
    want = (src.replace("DATE(START_TIME) AS DAY", "CONVERT_TIMEZONE('America/Chicago', START_TIME)::DATE AS DAY")
            .replace("DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT", "SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY")
            .replace(old_window, _norm(_HEAL_WINDOW)))
    heal = _heal()
    select = _norm(heal[heal.index("SELECT g.DAY,"):])
    assert select == want
    # teeth: a changed default key, a dropped CS filter, a widened window -- each breaks the comparison
    for old, new in (("'NONE'", "'NULL'"), ("AND COALESCE(CREDITS_USED_CLOUD_SERVICES, 0) > 0", ""),
                     ("-364", "-365")):
        assert _norm(heal.replace(old, new, 1))[_norm(heal).index("SELECT g.DAY,"):] != want, old
    # the column list is the mart's (V055) insert list, in the loader's order
    cols = _norm(heal[:heal.index("SELECT g.DAY,")])
    assert cols == _norm(f"{_HEAD} (DAY, COMPANY, WAREHOUSE_NAME, USER_NAME, ROLE_NAME, QUERY_TYPE, "
                         "QUERY_PARAMETERIZED_HASH, SAMPLE_TEXT, RUNS, CS_CREDITS, EXEC_SEC_SUM, COMPILE_SEC_SUM, "
                         "CACHE_PCT_SUM)")


def test_heal_pins_the_account_timezone():
    from app.logic.formulas import ACCOUNT_TIMEZONE
    code = re.sub(r"--[^\n]*", "", _heal())
    zones = set(re.findall(r"CONVERT_TIMEZONE\('([^']+)'", code))
    assert zones == {ACCOUNT_TIMEZONE}
    assert "DATE(START_TIME)" not in code.replace("::DATE", "") and "CURRENT_DATE()" not in code
    assert "COALESCE((SELECT MIN(DAY)" not in code         # history_locks/test_v016 counts the dailies' guard (6)


# ---------------------------------------------------------------------------------------------------------------
# Executed in sqlite: the arm itself, START_TIME stored in UTC, the Central pin shimmed with zoneinfo.
# ---------------------------------------------------------------------------------------------------------------
_NOW_UTC = datetime(2026, 9, 30, 15, 0, tzinfo=UTC)                      # 10:00 CDT, 2026-09-30
_TODAY_CT = _NOW_UTC.astimezone(_CT).date()
_FMT = "%Y-%m-%d %H:%M:%S"


def _ct_date(v: str | None) -> str | None:
    if v is None:
        return None
    if len(v) == 10:
        return v
    return datetime.strptime(v, _FMT).replace(tzinfo=UTC).astimezone(_CT).date().isoformat()


def _dateadd(unit: str, n: int, v: str) -> str:
    assert unit == "day"
    if len(v) == 10:
        return (date.fromisoformat(v) + timedelta(days=n)).isoformat()
    return (datetime.strptime(v, _FMT) + timedelta(days=n)).strftime(_FMT)


class _AnyValue:
    def __init__(self):
        self.v = None

    def step(self, x):
        if self.v is None:
            self.v = x

    def finalize(self):
        return self.v


def _db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.create_function("CT_DATE", 1, _ct_date)
    c.create_function("DATEADD", 3, _dateadd)
    c.create_function("LEFT", 2, lambda s, n: None if s is None else s[:n])
    c.create_function("COMPANY_FOR_WAREHOUSE", 1, lambda w: "ALFA" if w and w.startswith("WH_ALFA") else "UNKNOWN")
    c.create_aggregate("ANY_VALUE", 1, _AnyValue)
    c.execute("""CREATE TABLE QUERY_HISTORY(START_TIME TEXT, WAREHOUSE_NAME TEXT, USER_NAME TEXT, ROLE_NAME TEXT,
        QUERY_TYPE TEXT, QUERY_PARAMETERIZED_HASH TEXT, QUERY_TEXT TEXT, CREDITS_USED_CLOUD_SERVICES REAL,
        EXECUTION_TIME REAL, COMPILATION_TIME REAL, PERCENTAGE_SCANNED_FROM_CACHE REAL)""")
    c.execute("""CREATE TABLE MART_CLOUD_SVC_DAILY(DAY TEXT, COMPANY TEXT, WAREHOUSE_NAME TEXT, USER_NAME TEXT,
        ROLE_NAME TEXT, QUERY_TYPE TEXT, QUERY_PARAMETERIZED_HASH TEXT, SAMPLE_TEXT TEXT, RUNS INT, CS_CREDITS REAL,
        EXEC_SEC_SUM REAL, COMPILE_SEC_SUM REAL, CACHE_PCT_SUM REAL)""")
    return c


def _sqlite(sql: str) -> str:
    now = _NOW_UTC.strftime(_FMT)
    sql = sql.replace("DBA_MAINT_DB.OVERWATCH.", "").replace("SNOWFLAKE.ACCOUNT_USAGE.", "")
    sql = sql.replace("CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE", f"'{_TODAY_CT}'")
    sql = re.sub(r"CONVERT_TIMEZONE\('America/Chicago', (\w+)\)::DATE", r"CT_DATE(\1)", sql)
    sql = sql.replace("CURRENT_TIMESTAMP()", f"'{now}'")
    assert "::" not in sql and "CONVERT_TIMEZONE" not in sql and "CURRENT_" not in sql, sql
    return sql


def _q(c, utc: datetime, cs: float, wh: str | None = "WH_ALFA_TRANSFORM", user: str | None = "ETL",
       h: str | None = "H1", text: str = "select 1", exec_ms: float = 2000, comp_ms: float = 500, cache: float = 10):
    c.execute("INSERT INTO QUERY_HISTORY VALUES (?,?,?,?,?,?,?,?,?,?,?)",
              (utc.strftime(_FMT), wh, user, "R", "SELECT", h, text, cs, exec_ms, comp_ms, cache))


def _seed(c) -> None:
    for back in range(1, 11):                                # 10 Central days of history, 18:00 UTC each
        _q(c, _NOW_UTC - timedelta(days=back, hours=-3), 0.01 * back)
        _q(c, _NOW_UTC - timedelta(days=back, hours=-3), 0.0, h="H_ZERO")          # CS = 0: never a mart row
    # 2026-09-25 03:00 UTC is still 2026-09-24 in Central: the pin puts it on the Central day
    _q(c, datetime(2026, 9, 25, 3, 0, tzinfo=UTC), 0.5, wh=None, user=None, h=None, text="x" * 300)


def _rows(c, where: str = "") -> list[tuple]:
    return c.execute(f"SELECT * FROM MART_CLOUD_SVC_DAILY {where} ORDER BY 1, 3, 4, 7").fetchall()


def test_heal_inserts_only_older_days_with_the_loaders_grain_and_is_idempotent():
    c = _db()
    _seed(c)
    first = (_TODAY_CT - timedelta(days=3)).isoformat()
    c.execute("INSERT INTO MART_CLOUD_SVC_DAILY VALUES (?, 'ALFA', 'WH_ALFA_TRANSFORM', 'ETL', 'R', 'SELECT', 'H1', "
              "'select 1', 1, 0.03, 2, 0.5, 10)", (first,))
    c.execute(_sqlite(_heal()))
    healed = _rows(c, f"WHERE DAY < '{first}'")
    assert healed and all(r[0] < first for r in healed)
    assert _rows(c, f"WHERE DAY >= '{first}'") == [(first, "ALFA", "WH_ALFA_TRANSFORM", "ETL", "R", "SELECT", "H1",
                                                       "select 1", 1, 0.03, 2, 0.5, 10)]   # the loader's rows untouched
    assert not [r for r in healed if r[6] == "H_ZERO"]                         # CS = 0 excluded
    odd = [r for r in healed if r[2] == "NONE"]
    assert odd == [("2026-09-24", "UNKNOWN", "NONE", "UNKNOWN", "R", "SELECT", "n/a", "x" * 160, 1, 0.5, 2.0, 0.5,
                    10.0)]                                                     # Central day + the loader's defaults
    day = next(r for r in healed if r[2] == "WH_ALFA_TRANSFORM" and r[0] == "2026-09-26")
    assert day[8:] == (1, pytest.approx(0.04), 2.0, 0.5, 10.0)                # RUNS, CS, exec/compile s, cache sum
    before = _rows(c)
    c.execute(_sqlite(_heal()))
    assert _rows(c) == before                                                  # a re-run inserts nothing


def test_heal_on_an_empty_mart_stops_short_of_the_loaders_days():
    c = _db()
    _seed(c)
    c.execute(_sqlite(_heal()))
    days = {r[0] for r in _rows(c)}
    assert days and max(days) < (_TODAY_CT - timedelta(days=2)).isoformat()


def test_heal_rows_equal_the_loaders_own_source_on_a_shared_day():
    """The loader's MERGE source (its session runs in Central, so DATE(START_TIME) is the Central day) over the same
    QUERY_HISTORY rows gives the same row for a day both could write."""
    c = _db()
    _seed(c)
    c.execute("INSERT INTO MART_CLOUD_SVC_DAILY (DAY) VALUES (?)", (_TODAY_CT.isoformat(),))   # heal through D-1
    c.execute(_sqlite(_heal()))
    shared = (_TODAY_CT - timedelta(days=2)).isoformat()
    healed = _rows(c, f"WHERE DAY = '{shared}'")
    src = re.sub(r"--[^\n]*", "", _loader_source())
    src = src.replace("DATE(START_TIME) AS DAY", "CT_DATE(START_TIME) AS DAY")
    src = src.replace("WHERE START_TIME >= DATEADD('day', -2, CURRENT_DATE())", "WHERE 1 = 1")
    src = _sqlite(src.replace("DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT", "QUERY_HISTORY"))
    loader = sorted(r for r in c.execute(src).fetchall() if r[0] == shared)
    assert healed and sorted(healed) == loader
