"""Next-Fifty #33: ``ops_sql.warehouse_timeout_tail`` EXECUTED in sqlite over a seeded QUERY_HISTORY.

The real builder SQL runs with the Snowflake builtins it uses registered as Python functions
(APPROX_PERCENTILE as an EXACT linear percentile, COUNT_IF, IFF, DATEADD) and the company UDF as a name
rule; each rewrite asserts its fragment exists, so a changed builder fails loudly instead of testing less.
Pure: no Snowflake, runs on the floor-compat leg."""

from __future__ import annotations

import sqlite3

import pandas as pd
from test_task_evidence_harness import _CountIf, _dateadd, _regexp_substr

from app.data import ops_sql
from app.logic.stmt_timeout import CAP_LADDER_S

_NOW = "2026-09-28 08:00:00"
_TIMEOUT_MSG = "Statement reached its statement or warehouse timeout of 3,600 second(s) and was canceled."
_TIMEOUT_600 = "Statement reached its statement or warehouse timeout of 600 second(s) and was canceled."


def _try_to_number(value):
    """TRY_TO_NUMBER: NULL for NULL or unparseable text, else the number."""
    try:
        f = float(str(value))
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else f


class _Percentile:
    """APPROX_PERCENTILE(x, p) as the exact linear-interpolated percentile of the non-NULL inputs."""

    def __init__(self):
        self.values: list[float] = []
        self.p = 0.5

    def step(self, value, p):
        self.p = float(p)
        if value is not None:
            self.values.append(float(value))

    def finalize(self):
        return None if not self.values else float(pd.Series(self.values).quantile(self.p))


def _company(name):
    n = str(name or "").upper()
    return "ALFA" if n.startswith("WH_ALFA") else "Trexis" if n.startswith("WH_TRX") else "UNKNOWN"


def _to_sqlite(sql: str) -> str:
    def swap(old: str, new: str) -> None:
        nonlocal sql
        assert old in sql, old
        sql = sql.replace(old, new)

    swap("SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY", "QUERY_HISTORY")
    swap("CURRENT_TIMESTAMP()", f"'{_NOW}'")
    swap("DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(", "COMPANY_FOR_WAREHOUSE(")
    swap(" ILIKE ", " LIKE ")                         # sqlite LIKE is case-insensitive for ASCII
    assert "CURRENT_TIMESTAMP" not in sql and "::" not in sql
    return sql


def _db(rows: list[dict]) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.create_function("IFF", 3, lambda c, a, b: a if c else b)
    con.create_function("DATEADD", 3, _dateadd)
    con.create_function("COMPANY_FOR_WAREHOUSE", 1, _company)
    con.create_function("REGEXP_SUBSTR", 6, _regexp_substr)
    con.create_function("TRY_TO_NUMBER", 1, _try_to_number)
    con.create_aggregate("COUNT_IF", 1, _CountIf)
    con.create_aggregate("APPROX_PERCENTILE", 2, _Percentile)
    con.execute("CREATE TABLE QUERY_HISTORY (WAREHOUSE_NAME TEXT, START_TIME TEXT, EXECUTION_STATUS TEXT, "
                "ERROR_MESSAGE TEXT, TOTAL_ELAPSED_TIME REAL, EXECUTION_TIME REAL)")
    con.executemany("INSERT INTO QUERY_HISTORY VALUES (?, ?, ?, ?, ?, ?)",
                    [(r["wh"], r.get("start", "2026-09-20 10:00:00"), r.get("status", "SUCCESS"),
                      r.get("msg"), float(r["ms"]), float(r.get("exec_ms", r["ms"]))) for r in rows])
    return con


def _seed() -> list[dict]:
    rows = []
    # WH_ALFA_ETL: 200 completed runs of 1..200 s, two of them long (1000 s, 5000 s)
    rows += [{"wh": "WH_ALFA_ETL", "ms": s * 1000} for s in range(1, 199)]
    rows += [{"wh": "WH_ALFA_ETL", "ms": 1_000_000}, {"wh": "WH_ALFA_ETL", "ms": 5_000_000}]
    # a timed-out statement (counted), a plain failure (not), and a success too old for the window
    rows += [{"wh": "WH_ALFA_ETL", "ms": 3_600_000, "status": "FAIL", "msg": _TIMEOUT_MSG},
             {"wh": "WH_ALFA_ETL", "ms": 9_000_000, "status": "FAIL", "msg": "SQL compilation error"},
             {"wh": "WH_ALFA_ETL", "ms": 99_000_000, "start": "2026-08-01 10:00:00"}]
    # WH_TRX_BI: 3 runs, one of them cancelled by a timeout (case differs: ILIKE)
    rows += [{"wh": "WH_TRX_BI", "ms": 2000}, {"wh": "WH_TRX_BI", "ms": 4000},
             {"wh": "WH_TRX_BI", "ms": 7_200_000, "status": "INCIDENT", "msg": _TIMEOUT_MSG.upper()}]
    rows += [{"wh": None, "ms": 5000}]                     # no warehouse: never a row
    # a completed statement is never a timeout, whatever text rides along (pins the status predicate)
    rows += [{"wh": "WH_TRX_BI", "ms": 3000, "msg": _TIMEOUT_MSG}]
    # #33 D5: WH_ALFA_ETL also had a cancel at a lower (600 s) ceiling -- a session value, say
    rows += [{"wh": "WH_ALFA_ETL", "ms": 600_000, "status": "FAIL", "msg": _TIMEOUT_600}]
    return rows


def _run(company: str = "ALL", days: int = 30) -> pd.DataFrame:
    sql = ops_sql.warehouse_timeout_tail(days, company)
    return pd.read_sql_query(_to_sqlite(sql), _db(_seed())).set_index("WAREHOUSE_NAME")


def test_tail_counts_completed_statements_and_the_timeout_token_only():
    df = _run()
    assert set(df.index) == {"WH_ALFA_ETL", "WH_TRX_BI"}
    etl = df.loc["WH_ALFA_ETL"]
    assert etl["COMPLETED_RUNS"] == 200                            # SUCCESS only; the 90-day-old run is out
    assert etl["TIMEOUT_CANCELLED_RUNS"] == 2                      # the compilation error is not a timeout
    assert df.loc["WH_TRX_BI", "TIMEOUT_CANCELLED_RUNS"] == 1       # the token matches case-insensitively
    assert etl["MAX_ELAPSED_SEC"] == 5000.0                        # completed statements only
    expect_p99 = pd.Series([float(s) for s in range(1, 199)] + [1000.0, 5000.0]).quantile(0.99)
    assert abs(etl["P99_ELAPSED_SEC"] - expect_p99) < 1e-6
    assert list(df.columns[:2]) == ["COMPANY", "COMPLETED_RUNS"] and etl["COMPANY"] == "ALFA"


def test_runs_over_each_rung_are_exact_counts():
    etl = _run().loc["WH_ALFA_ETL"]
    completed = [float(s) for s in range(1, 199)] + [1000.0, 5000.0]
    for s in CAP_LADDER_S:
        assert etl[f"RUNS_OVER_{s}"] == sum(1 for v in completed if v > s), s
    assert etl["RUNS_OVER_900"] == 2 and etl["RUNS_OVER_3600"] == 1 and etl["RUNS_OVER_7200"] == 0


def test_timeout_total_is_the_scope_sum_on_every_row():
    df = _run()
    assert (df["TIMEOUT_CANCELLED_TOTAL"] == df["TIMEOUT_CANCELLED_RUNS"].sum()).all()
    assert df["TIMEOUT_CANCELLED_TOTAL"].iloc[0] == 3


def test_fired_ceiling_is_parsed_from_the_cancel_message():
    """#33 D5: N from 'timeout of N second(s)', thousands comma stripped ('3,600' -> 3600), over cancels only:
    the compilation failure (9,000 s elapsed) and the completed statement carrying the timeout text never count."""
    df = _run()
    etl = df.loc["WH_ALFA_ETL"]
    assert (etl["TIMEOUT_FIRED_MIN_SEC"], etl["TIMEOUT_FIRED_MAX_SEC"]) == (600, 3600)
    # the upper-cased message still COUNTS (ILIKE) but, like the W5b probe's case-sensitive regex, yields no
    # ceiling: 'Fired at' shows the dash for it rather than a guessed value
    bi = df.loc["WH_TRX_BI"]
    assert bi["TIMEOUT_CANCELLED_RUNS"] == 1 and pd.isna(bi["TIMEOUT_FIRED_MIN_SEC"])
    assert pd.isna(bi["TIMEOUT_FIRED_MAX_SEC"])


def _impact(wh: str, target: int = 3600, rows: list[dict] | None = None) -> pd.Series:
    sql = ops_sql.warehouse_timeout_impact(wh, target, 30)
    for old, new in (("SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY", "QUERY_HISTORY"),
                     ("CURRENT_TIMESTAMP()", f"'{_NOW}'")):
        assert old in sql, old
        sql = sql.replace(old, new)
    assert "CURRENT_TIMESTAMP" not in sql and "::" not in sql
    out = pd.read_sql_query(sql, _db(_seed() if rows is None else rows))
    assert len(out) == 1
    return out.iloc[0]


def test_impact_counts_one_warehouse_completed_statements_on_elapsed_and_execution_time():
    """#33 D1: the drawer's one-warehouse impact read. Elapsed includes queue time (an upper bound); the
    execution-time count shows how much of it is queue."""
    rows = [{"wh": "WH_TRXS_TRANSFORM", "ms": 9_245_000, "exec_ms": 5_000_000},     # over 1h on both
            {"wh": "WH_TRXS_TRANSFORM", "ms": 4_000_000, "exec_ms": 1_000_000},     # over only by queueing
            {"wh": "WH_TRXS_TRANSFORM", "ms": 100_000},
            {"wh": "WH_TRXS_TRANSFORM", "ms": 7_200_000, "status": "FAIL", "msg": _TIMEOUT_MSG},  # not completed
            {"wh": "WH_TRXS_TRANSFORM", "ms": 99_000_000, "start": "2026-08-01 10:00:00"},       # out of window
            {"wh": "WH_OTHER", "ms": 50_000_000}]                                                 # another warehouse
    r = _impact("WH_TRXS_TRANSFORM", rows=rows)
    assert (r["COMPLETED_RUNS"], r["OVER_TARGET_RUNS"], r["EXEC_OVER_TARGET_RUNS"]) == (3, 2, 1)
    assert r["MAX_ELAPSED_SEC"] == 9245.0
    assert _impact("wh_trxs_transform", rows=rows)["OVER_TARGET_RUNS"] == 2           # name match is upper-cased
    # nothing ran: still exactly one row (no GROUP BY) with a NULL longest. Snowflake's COUNT_IF over no rows is
    # 0; this sqlite shim yields NULL, which stmt_timeout.parse_timeout_impact reads as UNKNOWN (the safe side)
    idle = _impact("WH_NOTHING_RAN", rows=rows)
    assert pd.isna(idle["MAX_ELAPSED_SEC"])


def test_a_named_company_filters_the_outer_query():
    alfa = _run("ALFA")
    assert list(alfa.index) == ["WH_ALFA_ETL"]
    assert alfa["TIMEOUT_CANCELLED_TOTAL"].iloc[0] == 2              # the total follows the scope
    assert list(_run("Trexis").index) == ["WH_TRX_BI"]
    assert _run("UNKNOWN").empty


def test_rows_order_longest_run_first():
    assert list(_run().index) == ["WH_ALFA_ETL", "WH_TRX_BI"]
