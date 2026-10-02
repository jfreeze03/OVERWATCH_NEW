"""Executed harness for V167: the reconcile sweep, the two COVERAGE_FROM stamps, the atomic pattern reload and
the in-migration twin DELETE, run in an in-memory sqlite.

Every statement is the migration's OWN text (cut out of V167 by anchors, never a hand-written twin), translated
minimally -- FQNs dropped, the clock pinned (CURRENT_DATE() / CURRENT_TIMESTAMP() become literals, the account
day is Central), ::TIMESTAMP_NTZ / ::VARIANT dropped, ARRAY_CONTAINS(x, SPLIT(:v, ' ')) / IFF / LEAST / GREATEST
(NULL-propagating, like Snowflake) / DATEADD / DATE_TRUNC / COUNT_IF / LISTAGG / ANY_VALUE / HLL_* and
COMPANY_FOR_WAREHOUSE shimmed -- and the translator fails closed on any Snowflake-only token it does not know.
A MERGE is applied the way Snowflake does it: its USING source is executed, then the WHEN MATCHED SET list and
the WHEN NOT MATCHED VALUES list (both the migration's text) run against that source.

Timestamps are Central wall clock 'YYYY-MM-DD HH:MM:SS' (the account TIMEZONE is Central); a DAY is 'YYYY-MM-DD'.

Covers: R2-018 (a failed arm keeps its rows; vanished keys are swept; an empty extract sweeps nothing; the V064
DELETE-first shape loses the edge for good), R1-016 (the AI stamp: first d=3, a 365 reload, later d=3 runs, a
half AI load, the posture row), R2-010 (a remap re-stamps instead of adding a twin; legitimate multi-company rows
stay; a failed INSERT leaves the previous fill; V120's MERGE reproduces the twin), PATTERN-RESTAMP (the NULL-safe
LEAST stamp) and the twin DELETE (with the PREFLIGHT P167.1 count equal to what it removes).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta

import pytest

from tests._source import ROOT, read

_MIG = read("snowflake/migrations/V167__mart_loader_edges_ai_coverage_pattern_reload.sql")
_V064 = read("snowflake/migrations/V064__webhook_drain_watermarks_alert_burn_telemetry.sql")
_V120 = read("snowflake/migrations/V120__pattern_cost_runs_fanout_fix.sql")
_TODAY = date(2026, 9, 24)
_NOW = "2026-09-24 06:50:00"           # the nightly reconcile / pattern task run, Central
_TS = "%Y-%m-%d %H:%M:%S"


def _proc(text: str, name: str) -> str:
    s = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    o = text.index("$$", s)
    return text[s:text.index("$$;", o + 2) + 3]


def _between(text: str, start: str, end: str, *, inclusive: bool = False) -> str:
    i = text.index(start)
    j = text.index(end, i)
    return text[i:j + (len(end) if inclusive else 0)]


_M = _proc(_MIG, "SP_LOAD_MARTS_V27(")
_R = _proc(_MIG, "SP_NIGHTLY_RECONCILE(")
_P = _proc(_MIG, "SP_LOAD_PATTERN_COST(")


# ============================================================================================================
# translation + shims
# ============================================================================================================
_UNKNOWN = ("::", "QUALIFY", "FLATTEN", "MIN_BY", "MAX_BY", "APPROX_PERCENTILE", "CONVERT_TIMEZONE", "TO_VARCHAR",
            "RESULT_SCAN", "VALUES (")


def _values_cte(sql: str) -> str:
    """FROM VALUES ('a', 'b'), ... AS srcmap(C1, C2)  ->  FROM (SELECT 'a' AS C1, 'b' AS C2 UNION ALL ...)."""
    m = re.search(r"FROM VALUES\s*\n(.*?)AS srcmap\((\w+), (\w+)\)", sql, re.S)
    if not m:
        return sql
    pairs = re.findall(r"\('(\w+)', '(\w+)'\)", m.group(1))
    c1, c2 = m.group(2), m.group(3)
    sel = " UNION ALL ".join(f"SELECT '{a}' AS {c1}, '{b}' AS {c2}" for a, b in pairs)
    return sql[:m.start()] + f"FROM ({sel})" + sql[m.end():]


def _lite(sql: str, today: date = _TODAY, now: str = _NOW) -> str:
    out = re.sub(r"\bDBA_MAINT_DB\.OVERWATCH\.|\bSNOWFLAKE\.ACCOUNT_USAGE\.", "", sql)
    out = re.sub(r"--[^\n]*", "", out)
    out = out.replace("CURRENT_DATE()", f"'{today.isoformat()}'").replace("CURRENT_TIMESTAMP()", f"'{now}'")
    out = out.replace("::TIMESTAMP_NTZ", "").replace("::VARIANT", "")
    out = re.sub(r"ARRAY_CONTAINS\(([^,()]+), SPLIT\((:\w+), ' '\)\)", r"ARRAY_CONTAINS_SPLIT(\1, \2)", out)
    out = re.sub(r"CAST\(([\w.]+) AS DATE\)", r"DATE(\1)", out)
    out = out.replace("LISTAGG(", "group_concat(").replace("ANY_VALUE(", "MAX(")
    out = _values_cte(out)
    for tok in _UNKNOWN:
        assert tok not in out, f"untranslated Snowflake token {tok!r} in: {out[:300]}"
    return out


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, _TS) if len(ts) > 10 else datetime.strptime(ts, "%Y-%m-%d")


def _dateadd(unit: str, n: int, x: str | None) -> str | None:
    if x is None or n is None:
        return None
    step = {"day": timedelta(days=1), "hour": timedelta(hours=1), "minute": timedelta(minutes=1)}[unit.lower()]
    v = _parse(x) + int(n) * step
    return v.date().isoformat() if len(x) == 10 and unit.lower() == "day" else v.strftime(_TS)


def _date_trunc(unit: str, x: str | None) -> str | None:
    if x is None:
        return None
    assert unit.lower() == "hour"
    return _parse(x).replace(minute=0, second=0).strftime(_TS)


def _least(*a):
    return None if any(v is None for v in a) else min(a)


def _greatest(*a):
    return None if any(v is None for v in a) else max(a)


class _CountIf:
    def __init__(self) -> None:
        self.n = 0

    def step(self, v) -> None:
        self.n += 1 if v else 0

    def finalize(self) -> int:
        return self.n


class _HllAccumulate:
    def __init__(self) -> None:
        self.s: set[str] = set()

    def step(self, v) -> None:
        if v is not None:
            self.s.add(str(v))

    def finalize(self) -> str:
        return json.dumps(sorted(self.s))


class _HllCombine(_HllAccumulate):
    def step(self, v) -> None:
        if v is not None:
            self.s |= set(json.loads(v))


_COMPANY: dict[str, str] = {}


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:", isolation_level=None)     # explicit BEGIN / COMMIT / ROLLBACK, as the proc
    con.create_function("IFF", 3, lambda c, a, b: a if c else b)
    con.create_function("LEAST", -1, _least)
    con.create_function("GREATEST", -1, _greatest)
    con.create_function("DATEADD", 3, _dateadd)
    con.create_function("DATE_TRUNC", 2, _date_trunc)
    con.create_function("ARRAY_CONTAINS_SPLIT", 2,
                        lambda tok, s: None if s is None else int(tok in str(s).split(" ")))
    con.create_function("COMPANY_FOR_WAREHOUSE", 1, lambda w: _COMPANY.get(w, "UNKNOWN"))
    con.create_function("HLL_ESTIMATE", 1, lambda v: None if v is None else len(json.loads(v)))
    con.create_aggregate("COUNT_IF", 1, _CountIf)
    con.create_aggregate("HLL_ACCUMULATE", 1, _HllAccumulate)
    con.create_aggregate("HLL_COMBINE", 1, _HllCombine)
    return con


def _merge(con: sqlite3.Connection, merge: str, params: dict, target: str = "SOURCE_FRESHNESS_STATE") -> None:
    """Apply a MERGE INTO <target> t USING (<src>) s ON t.SOURCE_NAME = s.SOURCE_NAME ... the Snowflake way."""
    src = _between(merge, "USING (\n", "\n        ) s\n" if "\n        ) s\n" in merge else "\n    ) s\n")
    src = src[len("USING (\n"):]
    assert "ON t.SOURCE_NAME = s.SOURCE_NAME" in merge
    set_list = _between(merge, "WHEN MATCHED THEN UPDATE SET ", "WHEN NOT MATCHED")[len("WHEN MATCHED THEN UPDATE SET "):]
    ins = re.search(r"WHEN NOT MATCHED THEN INSERT \(([^)]*)\)\s*VALUES \((.*)\);\s*$", merge, re.S)
    cols, vals = ins.group(1), ins.group(2)
    con.execute("DROP TABLE IF EXISTS s")
    con.execute(f"CREATE TEMP TABLE s AS {_lite(src)}", params)
    matched = {r[0] for r in con.execute(f"SELECT s.SOURCE_NAME FROM s JOIN {target} t ON t.SOURCE_NAME = s.SOURCE_NAME")}
    con.execute(f"UPDATE {target} AS t SET {_lite(set_list)} FROM s WHERE t.SOURCE_NAME = s.SOURCE_NAME", params)
    unmatched = [r[0] for r in con.execute("SELECT SOURCE_NAME FROM s")]
    for name in unmatched:
        if name not in matched:
            con.execute(f"INSERT INTO {target} ({cols}) SELECT {_lite(vals)} FROM s WHERE s.SOURCE_NAME = :_n",
                        {**params, "_n": name})


def _freshness_table(con: sqlite3.Connection) -> None:
    con.execute("CREATE TABLE SOURCE_FRESHNESS_STATE (SOURCE_NAME TEXT PRIMARY KEY, LAST_LOAD_TS TEXT, ROW_COUNT INT, "
                "SNAPSHOT_TS TEXT, GENERATION INT, STATUS TEXT, COVERAGE_FROM TEXT)")


# ============================================================================================================
# R2-018 -- SP_NIGHTLY_RECONCILE's mark-and-sweep
# ============================================================================================================
_TABLES = {"MART_WAREHOUSE_EFFICIENCY_DAILY": ("DAY", "WAREHOUSE_NAME"), "MART_TASK_GRAPH_DAILY": ("DAY", "PIPELINE"),
           "FACT_QUERY_ROLE_HOURLY": ("HOUR_TS", "ROLE_NAME"), "FACT_QUERY_SCHEMA_HOURLY": ("HOUR_TS", "SCHEMA_NAME")}
_RECON_START = "2026-09-24 06:45:00"
_ALL_TOKENS = "MARTS OK (HOURLY, 3d): wh_eff qfam role_hr schema_hr tagcov alloc alloc_xdim graphs task_node timeline "


def _sweeps() -> list[str]:
    body = _R[_R.index("    -- V167 (R2-018) MARK-AND-SWEEP."):_R.index("    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_OPS_DIAG(3);")]
    body = re.sub(r"--[^\n]*", "", body)                         # the prose carries ';' -- statements first
    stmts = [s.strip() + ";" for s in body.split(";") if "DELETE FROM" in s]
    assert len(stmts) == 4
    return stmts


def _recon_db(extract_start: str | None = "2026-09-21 06:52:10") -> sqlite3.Connection:
    con = _connect()
    for t, (k, n) in _TABLES.items():
        con.execute(f"CREATE TABLE {t} ({k} TEXT, {n} TEXT, LOAD_TS TEXT)")
    con.execute("CREATE TABLE OW_QH_EXTRACT (START_TIME TEXT)")
    if extract_start:
        con.executemany("INSERT INTO OW_QH_EXTRACT VALUES (?)", [(extract_start,), ("2026-09-24 06:40:00",)])
    old, new = "2026-09-23 06:50:00", "2026-09-24 06:55:00"
    rows = {
        # (key, name, LOAD_TS): D-4 (outside the window), D-3 (the left edge), D-1, with stale and fresh stamps
        "MART_WAREHOUSE_EFFICIENCY_DAILY": [("2026-09-20", "WH_A", old), ("2026-09-21", "WH_A", new),
                                            ("2026-09-21", "WH_GONE", old), ("2026-09-23", "WH_A", new)],
        "MART_TASK_GRAPH_DAILY": [("2026-09-20", "P", old), ("2026-09-21", "P", new), ("2026-09-21", "PHANTOM", old)],
        "FACT_QUERY_ROLE_HOURLY": [("2026-09-21 05:00:00", "R", old), ("2026-09-21 06:00:00", "R", old),
                                   ("2026-09-21 07:00:00", "R", old), ("2026-09-21 08:00:00", "R", new)],
        "FACT_QUERY_SCHEMA_HOURLY": [("2026-09-21 07:00:00", "S_GONE", old), ("2026-09-22 01:00:00", "S", new)],
    }
    for t, rs in rows.items():
        con.executemany(f"INSERT INTO {t} VALUES (?, ?, ?)", rs)
    return con


def _sweep(con: sqlite3.Connection, rv: str | None) -> None:
    for stmt in _sweeps():
        con.execute(_lite(stmt), {"recon_start": _RECON_START, "rv": rv})


def _keys(con: sqlite3.Connection, table: str) -> set[tuple[str, str]]:
    k, n = _TABLES[table]
    return {(a, b) for a, b in con.execute(f"SELECT {k}, {n} FROM {table}")}


def test_r2_018_sweep_removes_only_unrestamped_keys_inside_each_window():
    con = _recon_db()
    _sweep(con, _ALL_TOKENS)
    assert _keys(con, "MART_WAREHOUSE_EFFICIENCY_DAILY") == {("2026-09-20", "WH_A"), ("2026-09-21", "WH_A"),
                                                              ("2026-09-23", "WH_A")}     # WH_GONE swept
    assert _keys(con, "MART_TASK_GRAPH_DAILY") == {("2026-09-20", "P"), ("2026-09-21", "P")}
    # hour grain: the bound is GREATEST(D-3, ext_lo_hour) = 2026-09-21 07:00 (06:52:10 rounds UP to the first
    # whole extract hour), so 05:00 / 06:00 -- below the arms' own reload edge -- are never touched
    assert _keys(con, "FACT_QUERY_ROLE_HOURLY") == {("2026-09-21 05:00:00", "R"), ("2026-09-21 06:00:00", "R"),
                                                     ("2026-09-21 08:00:00", "R")}
    assert _keys(con, "FACT_QUERY_SCHEMA_HOURLY") == {("2026-09-22 01:00:00", "S")}


def test_r2_018_a_failed_arm_keeps_its_rows():
    con = _recon_db()
    failed = _ALL_TOKENS.replace("wh_eff ", "").replace("graphs ", "").replace("role_hr ", "").replace("schema_hr ", "")
    before = {t: _keys(con, t) for t in _TABLES}
    _sweep(con, "MARTS WITH ERRORS: 4 required, 0 optional " + failed[len("MARTS OK "):])
    assert {t: _keys(con, t) for t in _TABLES} == before                  # stale but present: no hole
    con2 = _recon_db()
    _sweep(con2, None)                                                   # no verdict at all -> nothing swept
    assert {t: _keys(con2, t) for t in _TABLES} == before


def test_r2_018_an_empty_extract_sweeps_no_hour():
    con = _recon_db(extract_start=None)
    before = {t: _keys(con, t) for t in ("FACT_QUERY_ROLE_HOURLY", "FACT_QUERY_SCHEMA_HOURLY")}
    _sweep(con, _ALL_TOKENS)
    assert {t: _keys(con, t) for t in before} == before
    assert ("2026-09-21", "WH_GONE") not in _keys(con, "MART_WAREHOUSE_EFFICIENCY_DAILY")   # day sweeps unaffected


def test_r2_018_v064_delete_first_lost_the_edge_for_good():
    """Teeth: V064's up-front DELETEs (its own text) + a failed reload arm leave D-3 empty; under V167 the same
    failure leaves D-3 as it was."""
    con = _recon_db()
    v064 = _proc(_V064, "SP_NIGHTLY_RECONCILE(")
    for t in _TABLES:
        stmt = _between(v064, f"DELETE FROM DBA_MAINT_DB.OVERWATCH.{t}\n", ";", inclusive=True)
        con.execute(_lite(stmt))
    # the reload's wh_eff arm failed: nothing re-inserts the left edge
    assert not [k for k in _keys(con, "MART_WAREHOUSE_EFFICIENCY_DAILY") if k[0] >= "2026-09-21"]
    con2 = _recon_db()
    _sweep(con2, _ALL_TOKENS.replace("wh_eff ", ""))
    assert ("2026-09-21", "WH_A") in _keys(con2, "MART_WAREHOUSE_EFFICIENCY_DAILY")


# ============================================================================================================
# R1-016 -- the AI COVERAGE_FROM stamp inside SP_LOAD_MARTS_V27's DAILY freshness MERGE
# ============================================================================================================
def _daily_merge() -> str:
    daily = _M.split("    IF (UPPER(:SCOPE) = 'DAILY') THEN\n", 1)[1]
    return _between(daily, "        MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t",
                    "s.STATUS,\n                IFF(s.SOURCE_NAME = 'FACT_AI_USAGE_DAILY', DATEADD('day', -:d + 1, "
                    "CURRENT_DATE()), NULL));\n", inclusive=True)


def _ai_db() -> sqlite3.Connection:
    con = _connect()
    _freshness_table(con)
    con.execute("CREATE TABLE MART_SOURCE_FRESHNESS (SOURCE_NAME TEXT, LAST_LOAD_TS TEXT, ROW_COUNT INT)")
    con.executemany("INSERT INTO MART_SOURCE_FRESHNESS VALUES (?, ?, ?)",
                    [("FACT_AI_USAGE_DAILY", "2026-09-24 06:40:00", 10), ("MART_SECURITY_POSTURE_DAILY",
                                                                           "2026-09-24 06:40:00", 7)])
    return con


def _daily_run(con: sqlite3.Connection, d: int, loaded: str, today: date = _TODAY) -> None:
    merge = _daily_merge()
    con.execute("DROP TABLE IF EXISTS s")
    params = {"d": d, "loaded": loaded}
    src = _between(merge, "USING (\n", "\n        ) s\n")[len("USING (\n"):]
    set_list = _between(merge, "WHEN MATCHED THEN UPDATE SET ", "WHEN NOT MATCHED")[len("WHEN MATCHED THEN UPDATE SET "):]
    ins = re.search(r"WHEN NOT MATCHED THEN INSERT \(([^)]*)\)\s*VALUES \((.*)\);\s*$", merge, re.S)
    con.execute(f"CREATE TEMP TABLE s AS {_lite(src, today)}", params)
    matched = {r[0] for r in con.execute("SELECT s.SOURCE_NAME FROM s JOIN SOURCE_FRESHNESS_STATE t "
                                         "ON t.SOURCE_NAME = s.SOURCE_NAME")}
    con.execute(f"UPDATE SOURCE_FRESHNESS_STATE AS t SET {_lite(set_list, today)} FROM s "
                "WHERE t.SOURCE_NAME = s.SOURCE_NAME", params)
    for (name,) in con.execute("SELECT SOURCE_NAME FROM s").fetchall():
        if name not in matched:
            con.execute(f"INSERT INTO SOURCE_FRESHNESS_STATE ({ins.group(1)}) SELECT {_lite(ins.group(2), today)} "
                        "FROM s WHERE s.SOURCE_NAME = :_n", {**params, "_n": name})


def _stamp(con: sqlite3.Connection, name: str = "FACT_AI_USAGE_DAILY") -> str | None:
    row = con.execute("SELECT COVERAGE_FROM FROM SOURCE_FRESHNESS_STATE WHERE SOURCE_NAME = ?", (name,)).fetchone()
    return None if row is None else row[0]


def test_r1_016_stamp_takes_the_deepest_whole_day_both_ai_arms_loaded():
    con = _ai_db()
    both = "posture ai_code ai_functions "
    _daily_run(con, 3, both)                                             # first DAILY run after the apply
    assert _stamp(con) == (_TODAY - timedelta(days=2)).isoformat()
    _daily_run(con, 365, both)                                           # the owner's DAILY 365 reload
    assert _stamp(con) == (_TODAY - timedelta(days=364)).isoformat()
    later = _TODAY + timedelta(days=1)
    _daily_run(con, 3, both, today=later)                                # the next daily task run never narrows it
    assert _stamp(con) == (_TODAY - timedelta(days=364)).isoformat()
    _daily_run(con, 400, "posture ai_code ", today=later)                # a half AI load stamps nothing
    assert _stamp(con) == (_TODAY - timedelta(days=364)).isoformat()
    assert _stamp(con, "MART_SECURITY_POSTURE_DAILY") is None            # the posture row never carries one
    gen = con.execute("SELECT GENERATION FROM SOURCE_FRESHNESS_STATE WHERE SOURCE_NAME = 'MART_SECURITY_POSTURE_DAILY'")
    assert gen.fetchone()[0] == 4                                        # the rest of the MERGE still stamps


def test_r1_016_a_first_half_load_inserts_no_row_and_a_null_stamp_heals():
    con = _ai_db()
    _daily_run(con, 3, "posture ai_code ")
    assert con.execute("SELECT COUNT(*) FROM SOURCE_FRESHNESS_STATE WHERE SOURCE_NAME = 'FACT_AI_USAGE_DAILY'"
                       ).fetchone()[0] == 0
    con.execute("INSERT INTO SOURCE_FRESHNESS_STATE (SOURCE_NAME, GENERATION) VALUES ('FACT_AI_USAGE_DAILY', 9)")
    _daily_run(con, 3, "posture ai_code ai_functions ")                  # a pre-V167 row (NULL stamp) gets one
    assert _stamp(con) == (_TODAY - timedelta(days=2)).isoformat()


# ============================================================================================================
# R2-010 + PATTERN-RESTAMP -- SP_LOAD_PATTERN_COST's atomic window replace and its stamp
# ============================================================================================================
def _pattern_db() -> sqlite3.Connection:
    con = _connect()
    _freshness_table(con)
    con.execute("CREATE TABLE MART_PATTERN_COST_DAILY (DAY TEXT NOT NULL, QUERY_HASH TEXT NOT NULL, "
                "COMPANY TEXT NOT NULL, DATABASE_NAME TEXT NOT NULL, RUNS INT, CREDITS_ATTRIBUTED REAL, "
                "USERS_HLL TEXT, LOAD_TS TEXT NOT NULL DEFAULT (LOAD_NOW()))")
    con.execute("CREATE UNIQUE INDEX pk ON MART_PATTERN_COST_DAILY (DAY, QUERY_HASH, COMPANY, DATABASE_NAME)")
    con.execute("CREATE TABLE QUERY_HISTORY (QUERY_ID TEXT, START_TIME TEXT, QUERY_PARAMETERIZED_HASH TEXT, "
                "WAREHOUSE_NAME TEXT, DATABASE_NAME TEXT, USER_NAME TEXT)")
    con.execute("CREATE TABLE QUERY_ATTRIBUTION_HISTORY (QUERY_ID TEXT, START_TIME TEXT, "
                "CREDITS_ATTRIBUTED_COMPUTE REAL, CREDITS_USED_QUERY_ACCELERATION REAL)")
    qh, qah = [], []
    for k in range(5):                                       # 5 days x (WH_X x 2 queries, WH_T x 1 query) on H1/DB1
        day = (_TODAY - timedelta(days=k)).isoformat()
        for i, (wh, user, cr) in enumerate((("WH_X", "U1", 1.0), ("WH_X", "U2", 2.0), ("WH_T", "U3", 4.0))):
            qid = f"q{k}{i}"
            qh.append((qid, f"{day} 03:0{i}:00", "H1", wh, "DB1", user))
            qah.append((qid, f"{day} 03:0{i}:00", cr, 0.0))
            qah.append((qid, f"{day} 04:0{i}:00", 0.5, None))   # an hour-spanning query: 2 QAH rows, 1 run
    con.executemany("INSERT INTO QUERY_HISTORY VALUES (?, ?, ?, ?, ?, ?)", qh)
    con.executemany("INSERT INTO QUERY_ATTRIBUTION_HISTORY VALUES (?, ?, ?, ?)", qah)
    return con


_LOAD_NOW = ["2026-09-24 06:50:00"]


def _pattern_statements() -> tuple[str, str, str]:
    assert "    lo := DATEADD('day', -1 * :DAYS_BACK, CURRENT_DATE());\n" in _P
    delete = _between(_P, "    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY\n", ";", inclusive=True)
    insert = _between(_P, "    INSERT INTO DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY\n",
                      "GROUP BY 1, 2, 3, 4;", inclusive=True)
    fresh = _between(_P, "    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t",
                     "'loader', s.COVERAGE_FROM);", inclusive=True)
    txn = _between(_P, "    BEGIN TRANSACTION;", "    COMMIT;", inclusive=True)
    assert delete in txn and insert in txn                    # both statements are inside the one transaction
    assert "        ROLLBACK;\n        RAISE;\n" in _P[_P.index("EXCEPTION   -- V167"):]
    return delete, insert, fresh


def _load(con: sqlite3.Connection, days_back: int | None, now: str = "2026-09-24 06:50:00",
          today: date = _TODAY) -> None:
    """CALL SP_LOAD_PATTERN_COST(days_back): BEGIN TRANSACTION; DELETE; INSERT; COMMIT; then the freshness
    MERGE -- an error inside the transaction ROLLBACKs and re-raises (the proc's handler)."""
    delete, insert, fresh = _pattern_statements()
    _LOAD_NOW[0] = now
    lo = None if days_back is None else (today - timedelta(days=days_back)).isoformat()
    con.execute("BEGIN")
    try:
        con.execute(_lite(delete, today, now), {"lo": lo})
        con.execute(_lite(insert, today, now), {"lo": lo})
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    _merge(con, fresh, {"lo": lo})


def _v120_load(con: sqlite3.Connection, days_back: int, now: str = "2026-09-24 06:50:00") -> None:
    """Teeth: V120's MERGE (its own USING source, keyed on COMPANY) as an upsert on the same 4-column key."""
    v120 = _proc(_V120, "SP_LOAD_PATTERN_COST(")
    src = _between(v120, "    USING (\n", "\n    ) s\n    ON t.DAY")[len("    USING (\n"):]
    assert "ON t.DAY = s.DAY AND t.QUERY_HASH = s.QUERY_HASH AND t.COMPANY = s.COMPANY" in v120
    _LOAD_NOW[0] = now
    sql = _lite(src).replace("DATEADD('day', -1 * :DAYS_BACK, '2026-09-24')", ":lo")
    con.execute(f"INSERT INTO MART_PATTERN_COST_DAILY (DAY, QUERY_HASH, COMPANY, DATABASE_NAME, RUNS, "
                f"CREDITS_ATTRIBUTED, USERS_HLL) SELECT * FROM ({sql}) WHERE true "
                "ON CONFLICT (DAY, QUERY_HASH, COMPANY, DATABASE_NAME) DO UPDATE SET RUNS = excluded.RUNS, "
                "CREDITS_ATTRIBUTED = excluded.CREDITS_ATTRIBUTED, USERS_HLL = excluded.USERS_HLL, "
                f"LOAD_TS = '{now}'", {"lo": (_TODAY - timedelta(days=days_back)).isoformat()})


@pytest.fixture(autouse=True)
def _fresh_company_map():
    _COMPANY.clear()
    yield


def _pattern_conn() -> sqlite3.Connection:
    con = _pattern_db()
    con.create_function("LOAD_NOW", 0, lambda: _LOAD_NOW[0])
    return con


def _rows(con: sqlite3.Connection) -> list[tuple]:
    return con.execute("SELECT DAY, COMPANY, RUNS, ROUND(CREDITS_ATTRIBUTED, 4) FROM MART_PATTERN_COST_DAILY "
                       "ORDER BY DAY, COMPANY").fetchall()


def _all_scope_credits(con: sqlite3.Connection) -> float:
    return round(con.execute("SELECT SUM(CREDITS_ATTRIBUTED) FROM MART_PATTERN_COST_DAILY").fetchone()[0], 4)


def test_r2_010_a_remap_restamps_instead_of_adding_a_twin():
    con = _pattern_conn()
    _COMPANY.update({"WH_T": "TREXIS"})                          # WH_X unmapped -> UNKNOWN
    _load(con, 3, now="2026-09-23 06:50:00")
    truth = _all_scope_credits(con)
    assert truth == round(4 * (1.5 + 2.5 + 4.5), 4)               # D-3..D: 4 days, QAH pre-aggregated per query
    _COMPANY["WH_X"] = "ALFA"                                    # Cost > Unmapped entities > Apply mapping
    _load(con, 3)
    assert _all_scope_credits(con) == truth                      # the ALL scope sums each truth once
    groups = con.execute("SELECT DAY, COUNT(*), GROUP_CONCAT(COMPANY) FROM MART_PATTERN_COST_DAILY "
                         "GROUP BY DAY").fetchall()
    assert all(n == 2 and set(c.split(",")) == {"ALFA", "TREXIS"} for _, n, c in groups), groups
    # RUNS count queries, not attribution rows (V120's fix carried byte-identical): 2 on WH_X, 1 on WH_T per day
    assert {(c, r) for _, c, r, _ in _rows(con)} == {("ALFA", 2), ("TREXIS", 1)}


def test_r2_010_v120_merge_reproduces_the_twin():
    con = _pattern_conn()
    _COMPANY.update({"WH_T": "TREXIS"})
    _v120_load(con, 3, now="2026-09-23 06:50:00")
    truth = _all_scope_credits(con)
    _COMPANY["WH_X"] = "ALFA"
    _v120_load(con, 3)
    assert _all_scope_credits(con) > truth                       # UNKNOWN twin kept beside the new ALFA rows
    assert {c for _, c, _, _ in _rows(con)} == {"UNKNOWN", "ALFA", "TREXIS"}


def test_r2_010_a_failed_insert_keeps_the_previous_fill():
    con = _pattern_conn()
    _load(con, 3)
    before = _rows(con)

    def boom(w):
        raise RuntimeError("COMPANY_FOR_WAREHOUSE unavailable")

    con.create_function("COMPANY_FOR_WAREHOUSE", 1, boom)
    with pytest.raises(sqlite3.OperationalError):
        _load(con, 3)
    assert _rows(con) == before                                  # ROLLBACK undid the DELETE


def test_pattern_restamp_coverage_from_is_the_deepest_reload_null_safe():
    con = _pattern_conn()
    _load(con, 3)
    assert _stamp(con, "MART_PATTERN_COST_DAILY") == (_TODAY - timedelta(days=3)).isoformat()
    _load(con, 364)
    assert _stamp(con, "MART_PATTERN_COST_DAILY") == (_TODAY - timedelta(days=364)).isoformat()
    _load(con, 3)                                                # the daily CALL(3) never narrows it
    _load(con, None)                                             # a NULL DAYS_BACK keeps it (and loads nothing)
    assert _stamp(con, "MART_PATTERN_COST_DAILY") == (_TODAY - timedelta(days=364)).isoformat()
    row = con.execute("SELECT STATUS, GENERATION FROM SOURCE_FRESHNESS_STATE "
                      "WHERE SOURCE_NAME = 'MART_PATTERN_COST_DAILY'").fetchone()
    assert row == ("loader", 4)
    # a pre-V167 row with no stamp: the first atomic run stamps it
    con.execute("UPDATE SOURCE_FRESHNESS_STATE SET COVERAGE_FROM = NULL")
    _load(con, 3)
    assert _stamp(con, "MART_PATTERN_COST_DAILY") == (_TODAY - timedelta(days=3)).isoformat()


# ============================================================================================================
# the in-migration twin DELETE (+ PREFLIGHT P167.1 previews exactly what it removes)
# ============================================================================================================
def _twin_delete() -> str:
    stmt = _between(_MIG, "DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY t\nUSING (", ";\n", inclusive=True)
    using = _between(stmt, "USING (\n", ") g\n")[len("USING (\n"):]
    where = stmt[stmt.index(") g\nWHERE ") + len(") g\nWHERE "):].rstrip().rstrip(";")
    # sqlite has no DELETE ... USING: the same USING source and WHERE, as a rowid semi-join
    return (f"DELETE FROM MART_PATTERN_COST_DAILY WHERE rowid IN (SELECT t.rowid FROM MART_PATTERN_COST_DAILY t "
            f"JOIN ({_lite(using)}) g ON {_lite(where)})")


@pytest.fixture(scope="module")
def preflight(tmp_path_factory) -> str:
    tmp = tmp_path_factory.mktemp("v167_pre")
    env = {k: v for k, v in os.environ.items() if not k.upper().endswith("_OUT")}
    env.update(V167_OUT=str(tmp / "m.sql"), PREFLIGHT_OUT=str(tmp / "pre.sql"))
    res = subprocess.run([sys.executable, str(ROOT / "outputs" / "gen_v167.py")], env=env, cwd=tmp,
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return (tmp / "pre.sql").read_text(encoding="utf-8")


def test_twin_delete_removes_exactly_the_stale_company_rows(preflight):
    con = _pattern_conn()
    old, new, near = "2026-07-13 07:00:00", "2026-09-02 09:00:00", "2026-09-02 08:55:00"
    con.executemany("INSERT INTO MART_PATTERN_COST_DAILY VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
        ("2026-07-03", "H1", "ALFA", "DB1", 5, 10.0, "[]", old),        # stale pre-V044 residual twin -> removed
        ("2026-07-03", "H1", "UNKNOWN", "DB1", 5, 10.0, "[]", new),     # the V120 re-stamp's row -> kept
        ("2026-07-04", "H1", "ALFA", "DB1", 2, 3.0, "[]", new),         # same-stamp two-company group -> kept
        ("2026-07-04", "H1", "TREXIS", "DB1", 1, 1.0, "[]", new),
        ("2026-07-05", "H1", "ALFA", "DB1", 9, 9.0, "[]", old),         # single-row group -> kept
        ("2026-07-06", "H1", "ALFA", "DB1", 1, 1.0, "[]", near),        # 5 minutes apart: one load -> kept
        ("2026-07-06", "H1", "TREXIS", "DB1", 1, 1.0, "[]", new),
        ("2026-07-03", "H2", "ALFA", "DB1", 1, 2.0, "[]", old),         # another hash, single row -> kept
    ])
    p1 = preflight[preflight.index("-- P167.1"):preflight.index("-- P167.2")]
    p1 = p1[p1.index("SELECT"):]
    preview = con.execute(_lite(p1)).fetchone()
    before = con.execute("SELECT COUNT(*) FROM MART_PATTERN_COST_DAILY").fetchone()[0]
    con.execute(_twin_delete())
    after = con.execute("SELECT COUNT(*) FROM MART_PATTERN_COST_DAILY").fetchone()[0]
    assert before - after == 1 == preview[0] and preview[1] == 10.0
    assert ("2026-07-03", "ALFA", 5, 10.0) not in _rows(con)
    assert ("2026-07-03", "UNKNOWN", 5, 10.0) in _rows(con)
    con.execute(_twin_delete())                                  # idempotent: a re-run removes nothing
    assert con.execute("SELECT COUNT(*) FROM MART_PATTERN_COST_DAILY").fetchone()[0] == after


def test_twin_delete_after_an_atomic_reload_finds_nothing():
    """After V167 every day a run covers is replaced whole, so a re-applied file's DELETE is a no-op."""
    con = _pattern_conn()
    _COMPANY.update({"WH_T": "TREXIS"})
    _load(con, 4, now="2026-09-22 06:50:00")
    _COMPANY["WH_X"] = "ALFA"
    _load(con, 2, now="2026-09-24 06:50:00")                      # D-2..D re-stamped, D-4..D-3 keep the old stamp
    rows = _rows(con)
    con.execute(_twin_delete())
    assert _rows(con) == rows
