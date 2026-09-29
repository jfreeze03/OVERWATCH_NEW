"""Next-Fifty #14 Ph1, review F11: the task-evidence SQL EXECUTED in sqlite, through the verdict.

Every EDW environment (ALFA_EDW_PRD, _SIT, _DEV, ...) deploys the same procedure names, so matching a
task's CALL by name alone let another environment's CALL of the same procedure, in the same window,
decide the verdict (a SIT failure reported as the PROD task's error). The builder now resolves each
CALL's database and keeps only the CALLs in CONTROL_STATUS's own database when any ran there.

The real ``etl_control_sql.run_task_evidence_scan`` SQL runs here with the Snowflake builtins it uses
registered as Python functions and its two QUALIFYs rewritten to filtered subqueries (each rewrite
asserts its fragment exists, so a changed builder fails loudly instead of silently testing less).
Pure: no Snowflake, runs on the floor-compat leg.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.data import etl_control_sql as etl
from app.logic.etl_evidence import task_evidence_lines

_NOW = "2026-09-28 08:00:00"
_CTRL = "ALFA_EDW_PRD.PUBLIC.CONTROL_STATUS"
_FMT = "%Y-%m-%d %H:%M:%S"


def _ts(value):
    return None if value is None else datetime.strptime(str(value)[:19], _FMT)


def _dateadd(unit, n, value):
    ts = _ts(value)
    if ts is None:
        return None
    return (ts + timedelta(**{f"{str(unit).lower()}s": int(n)})).strftime(_FMT)


def _datediff(unit, a, b):
    """Snowflake counts unit BOUNDARIES crossed: truncate both ends to the unit, then subtract."""
    ta, tb = _ts(a), _ts(b)
    if ta is None or tb is None:
        return None
    if str(unit).lower() == "minute":
        return int((tb.replace(second=0) - ta.replace(second=0)).total_seconds() // 60)
    return int((tb - ta).total_seconds())


def _regexp_substr(text, pattern, pos, occurrence, params, group):
    if text is None:
        return None
    rx = re.compile(str(pattern).replace("[[:space:]]", r"\s"))
    hits = list(rx.finditer(str(text)[int(pos) - 1:]))
    if len(hits) < int(occurrence):
        return None
    return hits[int(occurrence) - 1].group(int(group))


def _split_part(text, delim, n):
    if text is None:
        return None
    parts, n = str(text).split(str(delim)), int(n) or 1
    if abs(n) > len(parts):
        return ""
    return parts[n - 1] if n > 0 else parts[n]


class _MaxBy:
    def __init__(self):
        self.key, self.value = None, None

    def step(self, value, key):
        if key is not None and (self.key is None or key > self.key):
            self.key, self.value = key, value

    def finalize(self):
        return self.value


class _CountIf:
    def __init__(self):
        self.n = 0

    def step(self, cond):
        self.n += 1 if cond else 0

    def finalize(self):
        return self.n


def _to_sqlite(sql: str) -> str:
    def swap(old: str, new: str) -> None:
        nonlocal sql
        assert old in sql, old
        sql = sql.replace(old, new)

    for fqn in (_CTRL, "PUBLIC.CONTROL_STATUS"):
        sql = sql.replace(fqn, "CONTROL_STATUS")
    swap("SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY", "QUERY_HISTORY")
    swap("CURRENT_TIMESTAMP()", f"'{_NOW}'")
    swap("::VARCHAR", "")
    sql = re.sub(r"\bLEFT\(", "LEFT_(", sql)
    # the database preference: QUALIFY <cond> OR <window MAX> = 0 -> a filtered subquery (same alias d)
    m = re.search(r"  SELECT (?P<cols>d\.QUERY_ID(?:(?!\n\)).)*?)\n  FROM call_db d\n"
                  r"  QUALIFY (?P<cond>[^\n]*)\n\s+OR (?P<win>MAX\(IFF\([^\n]*\) OVER \(\)) = 0\n", sql, re.S)
    assert m, "the calls CTE's QUALIFY changed shape"
    sql = sql.replace(m.group(0), f"  SELECT * FROM (SELECT {m.group('cols')}, {m.group('win')} AS _W\n"
                                  f"  FROM call_db d) d\n  WHERE {m.group('cond')} OR _W = 0\n")
    # kid_err: QUALIFY ROW_NUMBER() ... = 1 -> a filtered subquery
    m = re.search(r"  SELECT (?P<cols>CALL_ID, QUERY_ID AS FAILED_CHILD_QUERY_ID[^\n]*)\n(?P<rest>  FROM child_rows\n"
                  r"  WHERE [^\n]*)\n  QUALIFY (?P<win>ROW_NUMBER\(\) OVER \([^\n]*\)) = 1\n", sql)
    assert m, "the kid_err CTE's QUALIFY changed shape"
    sql = sql.replace(m.group(0), f"  SELECT * FROM (SELECT {m.group('cols')}, {m.group('win')} AS _RN\n"
                                  f"{m.group('rest')}\n  ) WHERE _RN = 1\n")
    # sqlite before 3.39 wants a GROUP BY ahead of HAVING (CI runners vary); one group either way
    swap("  FROM tasks\n  HAVING COUNT(*) > 0\n", "  FROM tasks\n  GROUP BY NULL HAVING COUNT(*) > 0\n")
    assert "QUALIFY" not in sql and "::" not in sql and "CURRENT_TIMESTAMP" not in sql
    return sql


def _db(calls: list[dict], children: list[dict] = ()) -> sqlite3.Connection:
    """One PROD task SP_D_PLCY in run R1, 22:00-22:30 SUCCEEDED, plus the given QUERY_HISTORY CALLs and
    child statements (sparse dicts: the rest default)."""
    con = sqlite3.connect(":memory:")
    con.create_function("IFF", 3, lambda c, a, b: a if c else b)
    con.create_function("DATEADD", 3, _dateadd)
    con.create_function("DATEDIFF", 3, _datediff)
    con.create_function("REGEXP_SUBSTR", 6, _regexp_substr)
    con.create_function("SPLIT_PART", 3, _split_part)
    con.create_function("SPLIT", 2, lambda t, d: None if t is None else json.dumps(str(t).split(str(d))))
    con.create_function("ARRAY_SIZE", 1, lambda j: None if j is None else len(json.loads(j)))
    con.create_function("LEFT_", 2, lambda t, n: None if t is None else str(t)[: int(n)])
    con.create_function("POWER", 2, lambda a, b: float(a) ** float(b))
    con.create_aggregate("MAX_BY", 2, _MaxBy)
    con.create_aggregate("COUNT_IF", 1, _CountIf)
    con.execute("CREATE TABLE CONTROL_STATUS (RUN_ID TEXT, WORKFLOW_NAME TEXT, TASK_NAME TEXT, "
                "TASK_STATUS TEXT, TASK_START_DTTM TEXT, TASK_END_DTTM TEXT)")
    con.execute("INSERT INTO CONTROL_STATUS VALUES ('R1', 'WF_PLCY', 'SP_D_PLCY', 'SUCCEEDED', "
                "'2026-09-27 22:00:00', '2026-09-27 22:30:00')")
    cols = ("QUERY_ID", "SESSION_ID", "START_TIME", "END_TIME", "EXECUTION_STATUS", "ERROR_CODE",
            "ERROR_MESSAGE", "WAREHOUSE_NAME", "TOTAL_ELAPSED_TIME", "QUEUED_OVERLOAD_TIME",
            "QUEUED_PROVISIONING_TIME", "QUERY_TEXT", "DATABASE_NAME", "QUERY_TYPE", "COMPILATION_TIME",
            "EXECUTION_TIME", "BYTES_SPILLED_TO_LOCAL_STORAGE", "BYTES_SPILLED_TO_REMOTE_STORAGE")
    con.execute(f"CREATE TABLE QUERY_HISTORY ({', '.join(cols)})")
    base = {"EXECUTION_STATUS": "SUCCESS", "ERROR_CODE": None, "ERROR_MESSAGE": None, "WAREHOUSE_NAME": "WH_ETL",
            "QUEUED_OVERLOAD_TIME": 0, "QUEUED_PROVISIONING_TIME": 0, "COMPILATION_TIME": 100,
            "EXECUTION_TIME": 1000, "BYTES_SPILLED_TO_LOCAL_STORAGE": 0, "BYTES_SPILLED_TO_REMOTE_STORAGE": 0,
            "QUERY_TYPE": "CALL"}
    rows = [{**base, **c} for c in calls] + [{**base, "QUERY_TYPE": "MERGE", **k} for k in children]
    con.executemany(f"INSERT INTO QUERY_HISTORY VALUES ({', '.join('?' for _ in cols)})",
                    [tuple(r.get(c) for c in cols) for r in rows])
    return con


def _prd_call(**over) -> dict:
    return {"QUERY_ID": "q-prd", "SESSION_ID": 1, "START_TIME": "2026-09-27 22:05:00",
            "END_TIME": "2026-09-27 22:25:00", "TOTAL_ELAPSED_TIME": 1_200_000,
            "QUERY_TEXT": "call ALFA_EDW_PRD.PUBLIC.SP_D_PLCY()", "DATABASE_NAME": "ALFA_EDW_PRD", **over}


def _sit_call(**over) -> dict:
    # a non-prod cycle's CALL of the SAME procedure, inside the PROD task's window, newer and FAILED
    return {"QUERY_ID": "q-sit", "SESSION_ID": 2, "START_TIME": "2026-09-27 22:10:00",
            "END_TIME": "2026-09-27 22:12:00", "TOTAL_ELAPSED_TIME": 120_000, "EXECUTION_STATUS": "FAIL",
            "ERROR_CODE": "100132", "ERROR_MESSAGE": "SIT: table ALFA_EDW_SIT.PUBLIC.D_PLCY does not exist",
            "QUERY_TEXT": "CALL ALFA_EDW_SIT.PUBLIC.SP_D_PLCY()", "DATABASE_NAME": "ALFA_EDW_SIT", **over}


_KIDS = [{"QUERY_ID": "k-prd", "SESSION_ID": 1, "START_TIME": "2026-09-27 22:06:00", "TOTAL_ELAPSED_TIME": 900_000},
         {"QUERY_ID": "k-sit", "SESSION_ID": 2, "START_TIME": "2026-09-27 22:11:00", "TOTAL_ELAPSED_TIME": 60_000,
          "EXECUTION_STATUS": "FAIL", "ERROR_MESSAGE": "SIT child failed"}]


def _scan(con, fqn: str = _CTRL) -> pd.DataFrame:
    sql = etl.run_task_evidence_scan(fqn, task="SP_D_PLCY", run_id="R1")
    return pd.read_sql_query(_to_sqlite(sql), con)


@pytest.mark.parametrize("qualified", [True, False])
def test_prod_task_ignores_a_same_named_non_prod_call(qualified) -> None:
    """A PRD CALL and a newer, failed SIT CALL of the same procedure in the PROD task's window: only the PRD
    one is evidence. Unqualified CALLs resolve through the session's database (case-folded)."""
    if qualified:
        prd, sit = _prd_call(), _sit_call()
    else:
        prd = _prd_call(QUERY_TEXT="CALL PUBLIC.SP_D_PLCY()")                        # 2-part: session database
        sit = _sit_call(QUERY_TEXT="call sp_d_plcy()", DATABASE_NAME="alfa_edw_sit")  # 1-part: session database
    df = _scan(_db([prd, sit], _KIDS))
    assert list(df["CALL_QUERY_ID"]) == ["q-prd"]
    first = df.iloc[0]
    assert first["CALL_DATABASE"] == "ALFA_EDW_PRD" and first["CONTROL_DATABASE"] == "ALFA_EDW_PRD"
    # the uncapped window totals cover only the kept CALL (the SIT failure and its time are gone)
    assert (first["MATCHED_CALLS"], first["FAILED_CALLS"], first["ALL_CALLS_ELAPSED_MS"]) == (1, 0, 1_200_000)
    assert (first["CHILD_STATEMENTS"], first["FAILED_CHILD_STATEMENTS"]) == (1, 0)   # the SIT child is not linked
    lines = task_evidence_lines(df, task="SP_D_PLCY")
    assert [line.level for line in lines] == ["clean"], lines
    assert "SIT" not in " ".join(line.text for line in lines)


def test_only_a_non_prod_call_is_kept_and_flagged() -> None:
    """No PRD CALL in the window (the proc may live elsewhere): every name match stays, and the verdict names
    the other database instead of presenting it as this environment's evidence."""
    df = _scan(_db([_sit_call()], _KIDS))
    assert list(df["CALL_QUERY_ID"]) == ["q-sit"] and df.iloc[0]["CALL_DATABASE"] == "ALFA_EDW_SIT"
    lines = task_evidence_lines(df, task="SP_D_PLCY")
    # review r2: the database warning leads, and another environment's failure is a warning, never the error
    assert lines[0].level == "warn" and lines[0].text.startswith(
        "No CALL of SP_D_PLCY ran in ALFA_EDW_PRD (CONTROL_STATUS's database) in this window; the CALL(s) shown "
        "match by procedure name in ALFA_EDW_SIT")
    assert lines[1].level == "warn"
    assert "the Snowflake CALL of SP_D_PLCY in ALFA_EDW_SIT on WH_ETL" in lines[1].text
    assert "error" not in [line.level for line in lines]


def test_a_two_part_control_fqn_states_no_database() -> None:
    df = _scan(_db([_prd_call(), _sit_call()], _KIDS), "PUBLIC.CONTROL_STATUS")
    assert list(df["CALL_QUERY_ID"]) == ["q-sit", "q-prd"]                  # newest first, both kept
    assert set(df["CONTROL_DATABASE"]) == {""} and int(df.iloc[0]["MATCHED_CALLS"]) == 2
    assert not [line for line in task_evidence_lines(df, task="SP_D_PLCY")
                if "CONTROL_STATUS's database" in line.text]


def test_a_nested_prefix_never_matches() -> None:
    # SP_D_PLCY_TSACTN is a different procedure (the reason CONTAINS is never used)
    df = _scan(_db([_prd_call(QUERY_TEXT="CALL ALFA_EDW_PRD.PUBLIC.SP_D_PLCY_TSACTN()")]))
    assert len(df) == 1 and df.iloc[0]["CALL_QUERY_ID"] is None and int(df.iloc[0]["MATCHED_CALLS"]) == 0
