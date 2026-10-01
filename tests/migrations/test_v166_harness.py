"""Executed models for V166 -- the loaders' OWN statements in sqlite, plus pure-Python replays of the task schedules.

  * R2-007 -- SP_LOAD_SECURITY_FACTS' d<=3 arm: the lo_ts SELECT, the DELETE and the INSERT bound, read from the V166
    file (and V105's DELETE / INSERT bound from V105's file) and run in sqlite over a toy extract and fact, across the
    scenarios the item names (a normal run, a swallowed extract failure across midnight, a 90-day-wide extract, manual
    DAYS_BACK 0 / 1 / 2, an empty extract). Checked against a pure model: V166 never loses or duplicates a row; V105
    loses rows in every widened-extract case.
  * R2-009 -- the V166 storage arm and the repair MERGE (as UPDATE ... FROM) over a toy DATABASE_STORAGE_USAGE_HISTORY
    with re-created databases: the name-day holds the SUM of its DATABASE_ID rows (= the live twin's basis), V101's
    AVG halves it (teeth), the repair touches only multi-ID name-days inside the view's 365 days, leaves LOAD_TS alone,
    adds or removes no row, and is idempotent.
  * R2-011 -- the daily CALL(3) schedule replayed with failed runs; whether a failure rolls back is read from each
    proc's text (V077 / V046 vs V166). V166: one or two consecutive failures leave no hole; V077: one failure loses
    day D-3 for good. Three or more consecutive failures still leave a stale day with the wrap (the owner-question
    catch-up floor, R2-011 Delta C, is not in V166).
  * C10 -- a keep-alive session opened 9 days before its query: resolved by V166's last reload of that day, relabelled
    '(unknown)' by V077's; the pads are parsed from the procs.
  * the owner REPAIR's gap-depth query (R166.4 / R166.5) agrees with the calendar-gap grid's MAX(DAYS_BACK_TO_HEAL).
Clocks are Central wall-clock (the account TIMEZONE); timestamps are 'YYYY-MM-DD HH:MM:SS' text.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from tests._source import read

_ROOT = Path(__file__).resolve().parents[2]
_MIG = read("snowflake/migrations/V166__fact_loader_window_integrity.sql")
_V105 = read("snowflake/migrations/V105__change_risk_create_or_replace_destructive.sql")
_V101 = read("snowflake/migrations/V101__fact_task_daily_retry_collapse.sql")
_V077 = read("snowflake/migrations/V077__app_cost_ledger.sql")
_V046 = read("snowflake/migrations/V046__storage_truth.sql")
_TS = "%Y-%m-%d %H:%M:%S"


def _proc(text: str, name: str) -> str:
    s = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    o = text.index("$$", s)
    return text[s:text.index("$$;", o + 2) + 3]


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


# ============================================================================================================
# sqlite shims (Snowflake semantics for the pieces these statements use; anything else fails closed)
# ============================================================================================================
def _parse(v: str) -> datetime:
    return datetime.strptime(v, _TS) if len(v) > 10 else datetime.strptime(v, "%Y-%m-%d")


def _dateadd(unit: str, n: int, v: str | None) -> str | None:
    if v is None:
        return None
    step = {"day": timedelta(days=1), "hour": timedelta(hours=1)}[unit]
    out = _parse(v) + n * step
    return out.strftime(_TS) if len(v) > 10 or unit == "hour" else out.strftime("%Y-%m-%d")


def _datediff(unit: str, a: str | None, b: str | None) -> int | None:
    assert unit == "day"
    if a is None or b is None:
        return None
    return (_parse(b).date() - _parse(a).date()).days


def _greatest(*a):
    return None if any(x is None for x in a) else max(a)


def _ts(v: str | None) -> str | None:
    """::TIMESTAMP_LTZ of a DATE -> its midnight."""
    return None if v is None else _parse(v).strftime(_TS)


def _db(today: str) -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.create_function("DATEADD", 3, _dateadd)
    c.create_function("DATEDIFF", 3, _datediff)
    c.create_function("GREATEST", -1, _greatest)
    c.create_function("TS", 1, _ts)
    c.create_function("COMPANY_FOR_DATABASE", 1, lambda n: "ALFA" if n and n.startswith("ALFA") else "UNKNOWN")
    c.execute("CREATE TABLE _clock(TODAY TEXT)")
    c.execute("INSERT INTO _clock VALUES (?)", (today,))
    return c


def _to_sqlite(sql: str, today: str, binds: dict[str, str]) -> str:
    sql = sql.replace("DBA_MAINT_DB.OVERWATCH.", "").replace("SNOWFLAKE.ACCOUNT_USAGE.", "")
    sql = sql.replace("CURRENT_DATE()", f"'{today}'")
    sql = re.sub(r"(?<![:\w]):([A-Za-z_]\w*)", lambda m: binds[m.group(1)], sql)
    sql = re.sub(r"(DATEADD\([^()]*\))::TIMESTAMP_LTZ", r"TS(\1)", sql)
    sql = re.sub(r"('[0-9-]{10}(?: [0-9:]{8})?')::DATE", lambda m: f"'{m.group(1)[1:11]}'", sql)
    assert "::" not in sql and "DBA_MAINT_DB" not in sql and "SNOWFLAKE." not in sql, sql
    assert not re.search(r"(?<![:\w]):[A-Za-z_]", sql), sql
    return sql


# ============================================================================================================
# R2-007 -- the d<=3 change reload
# ============================================================================================================
_SEC166 = _proc(_MIG, "SP_LOAD_SECURITY_FACTS(DAYS_BACK FLOAT)")
_SEC105 = _proc(_V105, "SP_LOAD_SECURITY_FACTS(DAYS_BACK FLOAT)")
_CLAMP = "    d := GREATEST(1, LEAST(COALESCE(DAYS_BACK, 3), 180))::INT;\n"


def _d3(p: str) -> str:
    return _between(p, "    IF (d <= 3) THEN\n", "\n    ELSE\n")


def _arm_sql(p: str) -> tuple[str | None, str, str]:
    """(lo_ts SELECT or None, DELETE statement, the INSERT's START_TIME predicate) from a proc's d<=3 arm."""
    arm = _d3(p)
    lo = None
    if "lo_ts := (" in arm:
        lo = _between(arm, "lo_ts := (", ");\n")[len("lo_ts := ("):]
    delete = _between(arm, "        DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE", ";\n").strip()
    pred = re.search(r"FROM DBA_MAINT_DB\.OVERWATCH\.OW_QH_EXTRACT\n\s+(WHERE START_TIME >= [^\n]+)\n", arm).group(1)
    return lo, delete, pred


def _clamp(days_back: float | None) -> int:
    assert _SEC166.count(_CLAMP) == 1 and _SEC105.count(_CLAMP) == 1          # the clamp is unchanged
    return int(max(1, min(3 if days_back is None else days_back, 180)))


def _run_reload(p: str, now: datetime, days_back: float | None, extract: list[datetime],
                fact: list[datetime]) -> list[str]:
    """Run the proc's d<=3 DELETE + INSERT (its own text) in sqlite; return the fact's EVENT_TS after."""
    d = _clamp(days_back)
    assert d <= 3
    today = now.date().isoformat()
    c = _db(today)
    c.execute("CREATE TABLE OW_QH_EXTRACT(QUERY_ID TEXT, START_TIME TEXT, EXECUTION_STATUS TEXT)")
    c.execute("CREATE TABLE FACT_SECURITY_CHANGE(QUERY_ID TEXT, EVENT_TS TEXT)")
    c.executemany("INSERT INTO OW_QH_EXTRACT VALUES (?, ?, 'SUCCESS')", [(t.strftime(_TS), t.strftime(_TS)) for t in extract])
    c.executemany("INSERT INTO FACT_SECURITY_CHANGE VALUES (?, ?)", [(t.strftime(_TS), t.strftime(_TS)) for t in fact])
    lo_sql, delete, pred = _arm_sql(p)
    binds = {"d": str(d)}
    if lo_sql is not None:
        lo_ts = c.execute(_to_sqlite(lo_sql, today, binds)).fetchone()[0]
        binds["lo_ts"] = "NULL" if lo_ts is None else f"'{lo_ts}'"
    c.execute(_to_sqlite(delete, today, binds))
    c.execute(_to_sqlite("INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE (QUERY_ID, EVENT_TS) "
                         f"SELECT QUERY_ID, START_TIME FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT {pred} "
                         "AND EXECUTION_STATUS = 'SUCCESS'", today, binds))
    return [r[0] for r in c.execute("SELECT EVENT_TS FROM FACT_SECURITY_CHANGE ORDER BY 1")]


def _model(now: datetime, days_back: float | None, ext_min: datetime | None, new: bool) -> tuple[datetime | None,
                                                                                              datetime | None]:
    """(delete-from, insert-from) under V105 (new=False) or V166 (new=True); None = matches nothing."""
    d = _clamp(days_back)
    midnight = datetime.combine(now.date() - timedelta(days=d), datetime.min.time())
    if not new:
        return ext_min, (midnight if ext_min is not None else None)
    lo = None if ext_min is None else max(ext_min, midnight)
    return lo, lo


_D = datetime(2026, 10, 1)
_SCENARIOS = [
    # (label, now, DAYS_BACK, extract first row (None = empty), V105 loses rows)
    ("normal 14:20 run", _D.replace(hour=14, minute=20), 3, _D.replace(hour=14, minute=20) - timedelta(hours=72), False),
    ("extract failure across midnight", _D.replace(minute=12), 3, _D - timedelta(days=4) + timedelta(hours=23, minutes=7),
     True),
    ("90-day-wide extract (backfill_365)", _D.replace(hour=10, minute=7), 3,
     _D.replace(hour=10, minute=7) - timedelta(days=90), True),
    ("manual DAYS_BACK 0 (clamped to 1)", _D.replace(hour=14, minute=20), 0,
     _D.replace(hour=14, minute=20) - timedelta(hours=72), True),
    ("manual DAYS_BACK 1", _D.replace(hour=14, minute=20), 1, _D.replace(hour=14, minute=20) - timedelta(hours=72), True),
    ("manual DAYS_BACK 2", _D.replace(hour=14, minute=20), 2, _D.replace(hour=14, minute=20) - timedelta(hours=72), True),
    ("empty extract", _D.replace(hour=14, minute=20), 3, None, False),
]


def _events(now: datetime) -> list[datetime]:
    """One admitted DDL event every 30 minutes for 100 days before ``now``."""
    t, out = now - timedelta(days=100), []
    while t < now:
        out.append(t)
        t += timedelta(minutes=30)
    return out


@pytest.mark.parametrize(("label", "now", "days_back", "ext_min", "old_loses"), _SCENARIOS, ids=[s[0] for s in _SCENARIOS])
def test_r2_007_v166_never_loses_a_row_and_v105_did(label, now, days_back, ext_min, old_loses):
    truth = _events(now)
    extract = [] if ext_min is None else [t for t in truth if t >= ext_min]
    want = sorted(t.strftime(_TS) for t in truth)
    new = _run_reload(_SEC166, now, days_back, extract, truth)
    old = _run_reload(_SEC105, now, days_back, extract, truth)
    # V166: every row survives exactly once (delete == re-insert window)
    assert new == want, label
    # V105: the rows in [extract first row, midnight(today - d)) are deleted and never re-read
    lost = sorted(set(want) - set(old))
    assert bool(lost) is old_loses, (label, lost[:3])
    assert len(old) == len(set(old)), label                         # never a duplicate either way
    # the sqlite run agrees with the pure model of each version's bounds
    for is_new, got in ((True, new), (False, old)):
        dlo, ilo = _model(now, days_back, ext_min, is_new)
        kept = [t for t in truth if dlo is None or t < dlo]
        added = [t for t in extract if ilo is not None and t >= ilo]
        assert sorted(t.strftime(_TS) for t in kept + added) == got, (label, is_new)


def test_r2_007_midnight_case_loses_exactly_the_pre_midnight_slice_under_v105():
    now, ext_min = _D.replace(minute=12), _D - timedelta(days=4) + timedelta(hours=23, minutes=7)
    truth = _events(now)
    extract = [t for t in truth if t >= ext_min]
    old = set(_run_reload(_SEC105, now, 3, extract, truth))
    lost = sorted(t for t in truth if t.strftime(_TS) not in old)
    midnight = datetime.combine(now.date() - timedelta(days=3), datetime.min.time())
    assert lost and all(ext_min <= t < midnight for t in lost)


# ============================================================================================================
# R2-009 -- SUM per name-day + the bounded repair
# ============================================================================================================
_DAILY166 = _proc(_MIG, "SP_LOAD_DAILY_FACTS()")
_DAILY101 = _proc(_V101, "SP_LOAD_DAILY_FACTS()")
_TODAY = "2026-10-01"
_TB = 1024 ** 4


def _storage_db() -> sqlite3.Connection:
    c = _db(_TODAY)
    c.execute("CREATE TABLE DATABASE_STORAGE_USAGE_HISTORY(USAGE_DATE TEXT, DATABASE_ID INT, DATABASE_NAME TEXT, "
              "AVERAGE_DATABASE_BYTES REAL, AVERAGE_FAILSAFE_BYTES REAL)")
    c.execute("CREATE TABLE FACT_STORAGE_DAILY(DAY TEXT, DATABASE_NAME TEXT, COMPANY TEXT, DB_BYTES REAL, "
              "FAILSAFE_BYTES REAL, LOAD_TS TEXT DEFAULT 'LOADED')")
    rows = [
        # ALFA_EDW re-created on 2026-09-20: the dropped ID keeps Time Travel / fail-safe bytes that day
        ("2026-09-20", 1, "ALFA_EDW", 2 * _TB, 1 * _TB), ("2026-09-20", 2, "ALFA_EDW", 2 * _TB, 0.0),
        ("2026-09-21", 2, "ALFA_EDW", 2 * _TB, 0.0),                                  # single ID afterwards
        ("2026-09-20", 7, "TRXS_RAW", 5 * _TB, 0.5 * _TB),                            # never re-created
        ("2025-09-01", 3, "ALFA_OLD", 4 * _TB, 0.0), ("2025-09-01", 4, "ALFA_OLD", 4 * _TB, 0.0),   # > 365 days
    ]
    c.executemany("INSERT INTO DATABASE_STORAGE_USAGE_HISTORY VALUES (?, ?, ?, ?, ?)", rows)
    return c


def _storage_insert(p: str) -> str:
    return _between(p, "    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY", ";\n")


def _run_loader(c: sqlite3.Connection, p: str, lo: str = "2025-08-01") -> dict[tuple[str, str], tuple]:
    c.execute(_to_sqlite(_storage_insert(p), _TODAY, {"lo_storage": f"'{lo} 00:00:00'"}))
    return {(r[0], r[1]): r[2:] for r in c.execute(
        "SELECT DAY, DATABASE_NAME, COMPANY, DB_BYTES, FAILSAFE_BYTES, LOAD_TS FROM FACT_STORAGE_DAILY")}


def _repair_sql() -> str:
    """The V166 repair MERGE, translated mechanically to sqlite's UPDATE ... FROM (fails closed on anything else)."""
    merge = _between(_MIG, "MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY t\n", ";\n")
    m = re.fullmatch(r"MERGE INTO (\S+) t\nUSING \(\n(.*)\n\) s\nON (.*)\nWHEN MATCHED THEN UPDATE SET (.*)", merge, re.S)
    assert m, merge
    target, src, on, sets = m.groups()
    assert "WHEN" not in sets and "\n" not in on
    return _to_sqlite(f"UPDATE {target} AS t SET {sets} FROM (\n{src}\n) AS s WHERE {on}", _TODAY, {})


def _live(c: sqlite3.Connection) -> dict[tuple[str, str], tuple[float, float]]:
    """The live twin's basis: SUM of the raw per-ID rows per name-day (cost_sql.storage_by_database_calendar_live)."""
    return {(r[0], r[1]): (r[2], r[3]) for r in c.execute(
        "SELECT USAGE_DATE, DATABASE_NAME, SUM(AVERAGE_DATABASE_BYTES), SUM(AVERAGE_FAILSAFE_BYTES) "
        "FROM DATABASE_STORAGE_USAGE_HISTORY GROUP BY 1, 2")}


def test_r2_009_loader_writes_the_sum_and_v101_wrote_the_average():
    c = _storage_db()
    new = _run_loader(c, _DAILY166)
    live = _live(c)
    for key, (_company, db_bytes, fs_bytes, _ts) in new.items():
        assert (db_bytes, fs_bytes) == live[key], key                    # the mart equals the live twin
    assert new[("2026-09-20", "ALFA_EDW")][:3] == ("ALFA", 4 * _TB, 1 * _TB)
    old_c = _storage_db()
    old = _run_loader(old_c, _DAILY101)
    assert old[("2026-09-20", "ALFA_EDW")][1:3] == (2 * _TB, 0.5 * _TB)  # teeth: AVG halved the re-created day
    assert old[("2026-09-20", "TRXS_RAW")] == new[("2026-09-20", "TRXS_RAW")]   # single-ID days never differed


def test_r2_009_repair_rewrites_only_multi_id_name_days_inside_the_view():
    c = _storage_db()
    _run_loader(c, _DAILY101)                                            # the pre-V166 fact (AVG)
    c.execute("UPDATE FACT_STORAGE_DAILY SET LOAD_TS = 'LOAD-' || DAY")  # distinct, so a touch would show
    before = {(r[0], r[1]): r[2:] for r in c.execute("SELECT * FROM FACT_STORAGE_DAILY")}
    c.execute(_repair_sql())
    after = {(r[0], r[1]): r[2:] for r in c.execute("SELECT * FROM FACT_STORAGE_DAILY")}
    assert set(after) == set(before)                                     # no row added or removed
    changed = {k for k in after if after[k] != before[k]}
    assert changed == {("2026-09-20", "ALFA_EDW")}                       # the multi-ID name-day only
    assert after[("2026-09-20", "ALFA_EDW")] == ("ALFA", 4 * _TB, 1 * _TB, "LOAD-2026-09-20")   # LOAD_TS kept
    assert after[("2025-09-01", "ALFA_OLD")] == before[("2025-09-01", "ALFA_OLD")]   # past the 365-day bound
    # it now equals what the V166 loader writes for that day, and a second run changes nothing
    fresh = _storage_db()
    assert _run_loader(fresh, _DAILY166)[("2026-09-20", "ALFA_EDW")][:3] == after[("2026-09-20", "ALFA_EDW")][:3]
    c.execute(_repair_sql())
    assert {(r[0], r[1]): r[2:] for r in c.execute("SELECT * FROM FACT_STORAGE_DAILY")} == after


# ============================================================================================================
# R2-011 -- the daily CALL(3) schedule with failed runs
# ============================================================================================================
def _rolls_back(p: str) -> bool:
    """Read from the proc text: the DELETE and the INSERT sit inside BEGIN TRANSACTION .. COMMIT and the handler
    ROLLBACKs (V166), or the DELETE autocommits on its own (V077 / V046)."""
    if "BEGIN TRANSACTION;" not in p:
        return False
    txn = _between(p, "BEGIN TRANSACTION;", "    COMMIT;\n")
    handler = _between(p, "    EXCEPTION\n", "    END;\n")
    return "DELETE FROM" in txn and "INSERT INTO" in txn and "ROLLBACK;" in handler


def _replay(rolls_back: bool, failed: set[int], last: int = 30, days_back: int = 3) -> dict[int, str]:
    """{day: 'complete' | 'partial'} after daily runs 1..last; a run on day r reloads [r - days_back, r] and a day
    loaded on itself is partial (still in progress). A failed run deletes its window unless it rolls back."""
    fact: dict[int, str] = {}
    for r in range(1, last + 1):
        window = range(max(1, r - days_back), r + 1)
        if r in failed:
            if not rolls_back:
                for d in window:
                    fact.pop(d, None)
            continue
        for d in window:
            fact[d] = "complete" if d < r else "partial"
    return fact


def _damage(fact: dict[int, str], last: int = 30) -> tuple[list[int], list[int]]:
    missing = [d for d in range(1, last) if d not in fact]
    stale = [d for d in range(1, last) if fact.get(d) == "partial"]
    return missing, stale


@pytest.mark.parametrize("name", ["SP_LOAD_APP_COST(DAYS_BACK FLOAT)", "SP_LOAD_STORAGE_TRUTH(DAYS_BACK FLOAT)"])
def test_r2_011_one_or_two_failures_no_longer_leave_a_hole(name):
    new, old = _proc(_MIG, name), _proc(_V077 if "APP_COST" in name else _V046, name)
    assert "    lo := DATEADD('day', -GREATEST(COALESCE(:DAYS_BACK, 3), 1)::INT, CURRENT_DATE());\n" in new
    assert _rolls_back(new) and not _rolls_back(old)
    assert _damage(_replay(_rolls_back(old), set())) == ([], [])          # no failure: nothing to see either way
    # V077 / V046: one failed run on day 20 loses day 17 for good; two lose 17 and 18
    assert _damage(_replay(_rolls_back(old), {20})) == ([17], [])
    assert _damage(_replay(_rolls_back(old), {20, 21})) == ([17, 18], [])
    # V166: the failed run rolls back to the previous fill; the next run's window covers what it would have
    assert _damage(_replay(_rolls_back(new), {20})) == ([], [])
    assert _damage(_replay(_rolls_back(new), {20, 21})) == ([], [])
    # the documented limit (R2-011 Delta C, an owner question): 3+ consecutive failures leave one stale day
    assert _damage(_replay(_rolls_back(new), {20, 21, 22})) == ([], [19])


# ============================================================================================================
# C10 -- the session lookback on the LAST reload of a day
# ============================================================================================================
def _pad(p: str) -> int:
    pads = re.findall(r"WHERE CREATED_ON >= DATEADD\('day', -(\d+), :lo\)", p)
    assert len(pads) == 1
    return int(pads[0])


def _resolved_on_each_load(created: int, query_day: int, pad: int, days_back: int = 3) -> list[bool]:
    """Day ``query_day`` is reloaded by the runs on query_day .. query_day + days_back; the run on r sets
    lo = r - days_back and resolves sessions CREATED_ON >= lo - pad."""
    return [created >= (r - days_back) - pad for r in range(query_day, query_day + days_back + 1)]


def test_c10_a_keep_alive_session_stays_resolved_on_the_last_reload():
    new, old = _proc(_MIG, "SP_LOAD_APP_COST(DAYS_BACK FLOAT)"), _proc(_V077, "SP_LOAD_APP_COST(DAYS_BACK FLOAT)")
    assert (_pad(old), _pad(new)) == (7, 30)
    q = 40
    # opened 9 days before the query: V077 resolves it on the first load, then relabels it '(unknown)'
    assert _resolved_on_each_load(q - 9, q, _pad(old)) == [True, True, False, False]
    assert _resolved_on_each_load(q - 9, q, _pad(new)) == [True] * 4
    # the last reload is the narrowest, so it decides the label; V166 resolves anything opened <= 30 days before
    assert all(_resolved_on_each_load(q - k, q, _pad(new))[-1] for k in range(31))
    assert not _resolved_on_each_load(q - 31, q, _pad(new))[-1]


# ============================================================================================================
# The owner REPAIR's gap depth == the calendar-gap grid's MAX(DAYS_BACK_TO_HEAL)
# ============================================================================================================
@pytest.fixture(scope="module")
def repair_text(tmp_path_factory) -> str:
    tmp = tmp_path_factory.mktemp("v166_repair")
    env = {k: v for k, v in os.environ.items() if not k.endswith("_OUT")}
    env.update(V166_OUT=str(tmp / "m.sql"), REPAIR_OUT=str(tmp / "r.sql"))
    res = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v166.py")], env=env, cwd=tmp,
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return (tmp / "r.sql").read_text(encoding="utf-8")


def _grid_depth(days: list[str], today: str) -> int:
    """The PREFLIGHT / R166.3 grid: every calendar day missing between MIN(DAY) and MAX(DAY), DAYS_BACK_TO_HEAL =
    today - day + 1; the heal needs the largest."""
    present = {date.fromisoformat(d) for d in days}
    if not present:
        return 0
    lo, hi, t = min(present), max(present), date.fromisoformat(today)
    missing = [lo + timedelta(i) for i in range((hi - lo).days + 1) if lo + timedelta(i) not in present]
    return max(((t - d).days + 1 for d in missing), default=0)


@pytest.mark.parametrize("days", [
    [f"2026-09-{d:02d}" for d in range(1, 31)],                                   # no gap
    [f"2026-09-{d:02d}" for d in range(1, 31) if d != 17],                        # one hole
    [f"2026-09-{d:02d}" for d in range(1, 31) if d not in (5, 6, 22)],            # two holes; the oldest decides
    ["2026-07-01", "2026-09-30"],                                                 # a long hole
    [],                                                                           # an empty fact
])
def test_repair_gap_depth_matches_the_grid(repair_text, days):
    block = _between(repair_text, "    SELECT COALESCE(MAX(DATEDIFF(", ";\n")
    sel = block.replace(" INTO :n", "")
    for fact in ("FACT_APP_COST_DAILY", "FACT_STORAGE_ACCOUNT_DAILY"):
        assert f"FROM DBA_MAINT_DB.OVERWATCH.{fact})) d" in repair_text
    c = _db(_TODAY)
    c.execute("CREATE TABLE FACT_STORAGE_ACCOUNT_DAILY(DAY TEXT)")
    c.executemany("INSERT INTO FACT_STORAGE_ACCOUNT_DAILY VALUES (?)", [(d,) for d in days])
    got = c.execute(_to_sqlite(sel, _TODAY, {})).fetchone()[0]
    assert got == _grid_depth(days, _TODAY)


# ============================================================================================================
# The owner R166.2 security heal, walked statement by statement over every path (review r1)
# ============================================================================================================
_HOURLY = "DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY"
_OK_VERDICT = "security facts loaded 180d"
_RETURN_PARTS = r"'(?:[^']|'')*'|COALESCE\(rv, '[^']*'\)|\w+"


class _Raised(Exception):
    pass


def _units(code: str) -> list[str]:
    """The block's statements in order: an 'IF (...) THEN' line is one unit, otherwise lines join until one ends in
    ';'. Comment lines are dropped."""
    units: list[str] = []
    buf: list[str] = []
    for raw in code.splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("--"):
            continue
        if not buf and re.fullmatch(r"IF \(.*\) THEN", ln):
            units.append(ln)
            continue
        buf.append(ln)
        if ln.endswith(";"):
            units.append(" ".join(buf))
            buf = []
    assert not buf, buf
    return units


def _concat(expr: str, env: dict) -> str:
    """A RETURN expression: string literals, rv / emsg, COALESCE(rv, 'literal'), joined by ||. Nothing else."""
    parts = re.findall(_RETURN_PARTS, expr)
    assert re.sub(r"\s+", "", "||".join(parts)) == re.sub(r"\s+", "", expr), expr    # nothing unmodelled
    out = []
    for part in parts:
        if part.startswith("'"):
            out.append(part[1:-1].replace("''", "'"))
        elif part.startswith("COALESCE("):
            out.append(env["rv"] if env["rv"] is not None else re.search(r"'([^']*)'", part).group(1))
        else:
            assert part in ("rv", "emsg"), part
            out.append(env[part])
    return "".join(out)


def _cond(c: str, env: dict) -> bool:
    if c == "running > 0":
        return env["running"] > 0
    if c == "called":
        return env["called"]
    m = re.fullmatch(r"rv IS NULL OR NOT \(rv = '([^']*)'\)", c)
    assert m, c                                               # an unmodelled condition fails closed
    return env["rv"] is None or env["rv"] != m.group(1)


def _step(u: str, env: dict, fail_at: str | None) -> None:
    if u == f"ALTER TASK IF EXISTS {_HOURLY} SUSPEND;":
        if fail_at == "suspend":
            raise _Raised
        env["graph"] = "suspended"
    elif u == f"ALTER TASK IF EXISTS {_HOURLY} RESUME;":
        env["graph"], env["dependents"] = "started", False
    elif u == f"SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('{_HOURLY}');":
        env["dependents"] = True
    elif u.startswith("SELECT COUNT(*) INTO :running FROM TABLE(DBA_MAINT_DB.INFORMATION_SCHEMA.CURRENT_TASK_GRAPHS("):
        if fail_at == "probe":
            raise _Raised
        env["running"] = env["in_flight"]
    elif u == "called := TRUE;":
        env["called"] = True
    elif u == "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_SECURITY_FACTS(180);":
        # the invariant the block exists for: the loader runs only with the root suspended and no run in flight
        assert env["graph"] == "suspended" and env["running"] == 0 and env["in_flight"] == 0
        env["calls"] += 1
        if fail_at == "call":
            raise _Raised
    elif u == "SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));":
        env["rv"] = env["verdict"]
    elif u.startswith("INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG "):
        env["log"].append(re.search(r"SELECT 'OwnerRepair', '(\w+)'", u).group(1))
    elif u == "emsg := SQLERRM;":
        env["emsg"] = "boom"
    else:
        raise AssertionError(f"unmodelled statement: {u[:80]}")


def _walk(units: list[str], env: dict, fail_at: str | None) -> str | None:
    skip = 0
    for u in units:
        if skip:
            skip += u.startswith("IF (")
            skip -= u == "END IF;"
            continue
        if u.startswith("IF ("):
            skip = 0 if _cond(u[4:u.rindex(") THEN")], env) else 1
        elif u.startswith("RETURN "):
            return _concat(u[len("RETURN "):-1], env)
        elif u != "END IF;":
            _step(u, env, fail_at)
    return None


def _run_heal(block: str, *, in_flight: int = 0, fail_at: str | None = None, verdict: str = _OK_VERDICT) -> dict:
    code = block[block.index("\nBEGIN\n") + len("\nBEGIN\n"):]
    body, _, handler = code.partition("\nEXCEPTION\n")
    handler = handler[handler.index("WHEN OTHER THEN") + len("WHEN OTHER THEN"):handler.rindex("\nEND;")]
    env = {"graph": "started", "dependents": True, "in_flight": in_flight, "running": None, "called": False,
           "rv": None, "verdict": verdict, "emsg": None, "calls": 0, "log": []}
    try:
        pane = _walk(_units(body), env, fail_at)
    except _Raised:
        pane = _walk(_units(handler), env, None)
    env["pane"] = pane
    return env


@pytest.fixture(scope="module")
def heal_block(repair_text) -> str:
    from tests.test_backfill_suspend_window import _statements
    blocks = [s for s in _statements(repair_text) if "SP_LOAD_SECURITY_FACTS(180);" in s]
    assert len(blocks) == 1
    return blocks[0]


_NOTE_FRAGS = ("FACT_SECURITY_CHANGE", "FACT_SECURITY_LOGIN_DAILY", "re-run this block")
_HEAL_PATHS = [
    ("a graph run still in flight", {"in_flight": 1}, "WAIT: ", False, [], 0),
    ("the heal succeeds", {}, f"ok: SP_LOAD_SECURITY_FACTS(180) -> {_OK_VERDICT}", False, [], 1),
    ("an unexpected verdict", {"verdict": "security facts loaded 90d"},
     "FAILED: SP_LOAD_SECURITY_FACTS(180) -> security facts loaded 90d", True, ["repair_verdict_failed"], 1),
    ("the CALL fails part-way", {"fail_at": "call"}, "FAILED: SP_LOAD_SECURITY_FACTS(180) - boom", True,
     ["repair_call_failed"], 1),
    ("the SUSPEND fails", {"fail_at": "suspend"}, "FAILED: SP_LOAD_SECURITY_FACTS(180) - boom", False,
     ["repair_call_failed"], 0),
    ("the in-flight probe fails", {"fail_at": "probe"}, "FAILED: SP_LOAD_SECURITY_FACTS(180) - boom", False,
     ["repair_call_failed"], 0),
]


@pytest.mark.parametrize(("label", "kw", "pane_start", "noted", "log", "calls"), _HEAL_PATHS,
                         ids=[p[0] for p in _HEAL_PATHS])
def test_security_heal_paths(heal_block, label, kw, pane_start, noted, log, calls):
    """Every path the block can catch ends with the hourly graph RESUMEd and its dependents enabled; the loader runs
    only with the root suspended and no run in flight; the emptied-facts note rides exactly the FAILED panes of a
    CALL that started (a failure before it changed nothing). A timeout or a Stop is not catchable: the standalone
    RESUME pair after the block covers it (test_v166_repair_security_heal_holds_the_hourly_graph)."""
    env = _run_heal(heal_block, **kw)
    assert env["pane"] is not None and env["pane"].startswith(pane_start), (label, env["pane"])
    assert env["graph"] == "started" and env["dependents"], (label, env["graph"])
    assert all(f in env["pane"] for f in _NOTE_FRAGS) is noted, (label, env["pane"])
    assert env["log"] == log and env["calls"] == calls, label


_NL = chr(10)
_HEAL_MUTATIONS = [
    ("the WAIT branch forgets the RESUME",
     f"    IF (running > 0) THEN{_NL}        ALTER TASK IF EXISTS {_HOURLY} RESUME;{_NL}",
     f"    IF (running > 0) THEN{_NL}"),
    ("the note is never armed", f"    called := TRUE;{_NL}", ""),
    ("the note is armed before the SUSPEND", f"    ALTER TASK IF EXISTS {_HOURLY} SUSPEND;{_NL}",
     f"    called := TRUE;{_NL}    ALTER TASK IF EXISTS {_HOURLY} SUSPEND;{_NL}"),
    ("no in-flight refusal", f"    IF (running > 0) THEN{_NL}", f"    IF (running < 0) THEN{_NL}"),
    ("the handler forgets the RESUME",
     f"        ALTER TASK IF EXISTS {_HOURLY} RESUME;{_NL}"
     f"        SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('{_HOURLY}');{_NL}        IF (called) THEN{_NL}",
     f"        IF (called) THEN{_NL}"),
]


@pytest.mark.parametrize(("label", "old", "new"), _HEAL_MUTATIONS, ids=[m[0] for m in _HEAL_MUTATIONS])
def test_security_heal_walk_has_teeth(heal_block, label, old, new):
    assert heal_block.count(old) == 1, label
    mutated = heal_block.replace(old, new)
    caught = [kw for kw in ({"in_flight": 1}, {}, {"verdict": "x"}, {"fail_at": "call"}, {"fail_at": "suspend"})
              if _heal_walk_breaks(mutated, kw)]
    assert caught, label


def _heal_walk_breaks(block: str, kw: dict) -> bool:
    """True when the walk under ``kw`` violates an invariant test_security_heal_paths holds the shipped block to."""
    try:
        env = _run_heal(block, **kw)
    except AssertionError:                     # the CALL ran unsuspended / mid-run, or an unmodelled statement
        return True
    noted = env["pane"] is not None and all(f in env["pane"] for f in _NOTE_FRAGS)
    return (env["graph"] != "started" or not env["dependents"]
            or (kw.get("fail_at") == "call" and not noted)
            or (kw.get("fail_at") == "suspend" and noted))
