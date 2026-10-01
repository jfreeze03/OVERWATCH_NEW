"""PR-1 (v4.606.0) c10-mart-sql fixes — the confirmed R1 findings on the mart / ETL / DQ / chatter
SQL builders. Each test fails on the pre-fix builder (04fd374e) and pins the corrected behaviour.

Where the defect is an EXECUTED-SQL outcome (a lossy top-N, a double-counted route-day, a stale
'still breaking' flag, an account-wide coverage remainder, a substring actor match), the real
builder SQL runs in an in-memory sqlite with a minimal dialect shim — the logic under test is
never rewritten, only the Snowflake spellings sqlite lacks.
"""

from __future__ import annotations

import contextlib
import inspect
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from app.config import DEFAULT_MAX_ROWS
from app.core.result import QueryResult
from app.data import (
    change_impact_sql,
    chargeback_sql,
    chatter_sql,
    dq_sql,
    etl_control_sql,
    etl_sql,
    graph_sql,
    mart27_sql,
    mart_sql,
)
from app.data.common import account_today_sql
from app.logic.date_windows import CalendarDayOffset
from app.logic.insights import recon_recurrence

sqlglot = pytest.importorskip("sqlglot")

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _strip_db(sql: str) -> str:
    return re.sub(r"\bDBA_MAINT_DB\.OVERWATCH\.", "", sql)


# ---------------------------------------------------------------------------
# R1-014 — company-scoped Heaviest queries off a mart that keeps a GLOBAL per-hour top-50
# ---------------------------------------------------------------------------

_SEPT = (date(2026, 9, 1), date(2026, 10, 1))


def _ops_diag_db(rows: list[tuple[str, str, str, float]]) -> sqlite3.Connection:
    """MART_OPS_DIAG_HOURLY holding what SP_LOAD_OPS_DIAG (V062) stored: (HOUR_TS, COMPANY, QUERY_ID,
    ELAPSED_SEC) TOP_ELAPSED rows, already cut to the hour's top-50 across ALL companies."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE MART_OPS_DIAG_HOURLY (HOUR_TS TEXT, KIND TEXT, COMPANY TEXT, QUERY_ID TEXT, "
                "START_TIME TEXT, USER_NAME TEXT, WAREHOUSE_NAME TEXT, WAREHOUSE_SIZE TEXT, "
                "DATABASE_NAME TEXT, QUERY_TYPE TEXT, EXECUTION_STATUS TEXT, ELAPSED_SEC REAL, "
                "QUEUED_SEC REAL, SPILL_REMOTE_GB REAL, QUERY_PREVIEW TEXT)")
    con.executemany(
        "INSERT INTO MART_OPS_DIAG_HOURLY VALUES (?, 'TOP_ELAPSED', ?, ?, ?, 'U', 'WH', 'S', 'DB', "
        "'SELECT', 'SUCCESS', ?, 0, 0, 'q')",
        [(h, c, qid, h, e) for h, c, qid, e in rows])
    # an older FAIL_FAMILY row (the mart predates the window) opens the reader's own coverage gate
    con.execute("INSERT INTO MART_OPS_DIAG_HOURLY (HOUR_TS, KIND, COMPANY) "
                "VALUES ('2026-08-31 00:00:00', 'FAIL_FAMILY', 'ALFA')")
    return con


def _top(con: sqlite3.Connection, company: str) -> pd.DataFrame:
    sql = mart27_sql.ops_diag_top_queries(30, company, 50, bounds=_SEPT)
    return pd.read_sql_query(_strip_db(sql), con)


def test_r1_014_company_read_abstains_when_a_cut_hour_could_hide_its_heaviest_query():
    # Hour H: 50 Trexis queries of 30 min filled the loader's per-hour cut, so ALFA's 25-min query
    # in that hour was never stored. ALFA's other 49 stored queries are 5 min each.
    h = "2026-09-10 03:00:00"
    rows = [(h, "Trexis", f"T{i}", 1800.0) for i in range(50)]
    rows += [(f"2026-09-{11 + i // 3:02d} {i % 3:02d}:00:00", "ALFA", f"A{i}", 300.0) for i in range(49)]
    con = _ops_diag_db(rows)
    # pre-fix this returned the 49 lighter ALFA rows as the ALFA top-50 (non-empty, so run_mart_first
    # never fell back). Now the exactness certificate fails -> ZERO rows -> the live filtered top-N.
    assert _top(con, "ALFA").empty
    # Company=ALL is exact by construction (any global top-50 member is in its hour's top-50)
    assert len(_top(con, "ALL")) == 50


def test_r1_014_company_read_serves_when_the_certificate_proves_exactness():
    # The cut hour's lightest kept row (100 s) is below ALFA's 50th-heaviest stored (300 s), so no
    # unstored query of that hour could have ranked: the mart answer is exact and is served.
    h = "2026-09-10 03:00:00"
    rows = [(h, "Trexis", f"T{i}", 100.0) for i in range(50)]
    rows += [(f"2026-09-{11 + i // 3:02d} {i % 3:02d}:00:00", "ALFA", f"A{i}", 300.0 + i) for i in range(60)]
    df = _top(_ops_diag_db(rows), "ALFA")
    assert len(df) == 50 and df["ELAPSED_SEC"].min() == 310.0


def test_r1_014_all_scope_sql_is_unchanged_and_company_sql_parses():
    all_sql = mart27_sql.ops_diag_top_queries(7, "ALL", 50)
    assert "cut_hours" not in all_sql and "NOT EXISTS" not in all_sql    # ALL keeps the exact old shape
    comp = mart27_sql.ops_diag_top_queries(7, "ALFA", 50)
    assert "cut_hours AS (" in comp and f"HAVING COUNT(*) >= {mart27_sql.OPS_DIAG_HOURLY_TOP_N}" in comp
    sqlglot.parse_one(comp, dialect="snowflake")
    # a limit past the loader's per-hour 50 is not exact even for ALL -> certified too
    assert "cut_hours" in mart27_sql.ops_diag_top_queries(7, "ALL", 100)


# ---------------------------------------------------------------------------
# R1-015 / R1-018 — served-window class: what each mart reader can honestly serve of a long Window
# ---------------------------------------------------------------------------

def test_r1_015_pattern_cost_trailing_window_stays_inside_the_v120_restamp():
    # v4.606 holistic review REVERSED R1-015's widening to 365 days. V120 fixed the loader's RUNS fan-out
    # but re-stamped only the last 90 days (CALL SP_LOAD_PATTERN_COST(90), applied 2026-09-02); older
    # MART_PATTERN_COST_DAILY rows (V047's first fill reaches ~mid-April) can still carry inflated RUNS,
    # so a 180/365d trailing read understated $/run for exactly the patterns V120 fixed, passed the run
    # floor on inflated counts, and was labelled 365d over ~5.5 months of mart. Raise
    # PATTERN_COST_MAX_DAYS only after a 365-day re-stamp CALL has run.
    for days in (180, 365):
        sql = mart27_sql.pattern_cost(days, "ALL")
        assert "WHERE p.DAY >= DATEADD('day', -90, CURRENT_DATE())" in sql
        assert f"-{days}," not in sql
        # the run floor scales with the 90 days read (15 runs), not the 365-day ask (61)
        assert "SUM(p.RUNS) >= 15" in sql
    assert mart27_sql.PATTERN_COST_MAX_DAYS == 90
    assert "bounded_days(days, PATTERN_COST_MAX_DAYS)" in inspect.getsource(mart27_sql.pattern_cost)
    # inside the cap a trailing window is read as asked
    assert "WHERE p.DAY >= DATEADD('day', -30, CURRENT_DATE())" in mart27_sql.pattern_cost(30, "ALL")
    # a calendar preset keeps reading its exact [start, end) bounds (pre-existing, unchanged here)
    ytd = mart27_sql.pattern_cost(273, "ALL", bounds=(date(2026, 1, 1), date(2026, 10, 2)))
    assert "WHERE p.DAY >= '2026-01-01' AND p.DAY < '2026-10-02'" in ytd and "DATEADD" not in ytd


def _pressure_db(hour_rows: list[tuple[str, str, str, float, float]]) -> sqlite3.Connection:
    """FACT_QUERY_HOURLY rows (HOUR_TS, COMPANY, WAREHOUSE_NAME, QUEUED_SEC_SUM, SPILL_REMOTE_GB)."""
    con = sqlite3.connect(":memory:")
    con.create_function("GREATEST", 2, max)
    con.create_function("DATEDIFF", 3, lambda _unit, a, b: (date.fromisoformat(str(b)[:10])
                                                            - date.fromisoformat(str(a)[:10])).days)
    con.execute("CREATE TABLE FACT_QUERY_HOURLY (HOUR_TS TEXT, COMPANY TEXT, WAREHOUSE_NAME TEXT, "
                "QUERY_COUNT INTEGER, QUEUED_SEC_SUM REAL, SPILL_REMOTE_GB REAL, P95_ELAPSED_SEC REAL)")
    con.executemany("INSERT INTO FACT_QUERY_HOURLY VALUES (?, ?, ?, 10, ?, ?, 1.0)", hour_rows)
    return con


def test_r1_018_pressure_reader_reports_the_facts_own_history_span():
    # v4.606 holistic review: R1-018 widened the mart leg to 365 days on the premise "the fact is
    # retained 400d" -- but retention is not coverage: FACT_QUERY_HOURLY is never backfilled, so on
    # 2026-10-01 it holds only the hours loaded since OVERWATCH began (~Jul 7) and a 365d Window
    # silently summed ~86 days. The reader now carries COVERED_DAYS, ONE scalar over the WHOLE fact:
    # not the window, the company, or the HAVING-filtered warehouses.
    sql = mart_sql.fact_warehouse_pressure(30, "ALFA", bounds=(date(2026, 9, 1), date(2026, 10, 1)))
    assert "AS COVERED_DAYS" in sql
    sqlglot.parse_one(sql, dialect="snowflake")
    con = _pressure_db([
        # the fact's first hour: another company, outside the window, a no-pressure warehouse
        ("2026-07-07 05:00:00", "TREXIS", "WH_QUIET", 0.0, 0.0),
        ("2026-09-10 03:00:00", "ALFA", "WH_ALFA", 120.0, 0.0),
        ("2026-09-11 03:00:00", "ALFA", "WH_IDLE", 0.0, 0.0),        # dropped by HAVING
    ])
    df = pd.read_sql_query(_strip_db(sql).replace(account_today_sql(), "'2026-10-01'"), con)
    assert df["WAREHOUSE_NAME"].tolist() == ["WH_ALFA"]
    # Jul 7 .. Oct 1 inclusive -- measured from the fact's first hour, which no filter above can see
    assert int(df["COVERED_DAYS"].iloc[0]) == (date(2026, 10, 1) - date(2026, 7, 7)).days + 1 == 87
    # the trailing 365d predicate is unchanged (the span limit is disclosed, not hidden behind a cap)
    assert "HOUR_TS >= DATEADD('day', -365, CURRENT_DATE())" in mart_sql.fact_warehouse_pressure(365, "ALL")


class _Cell:
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class _ContentionSt:
    def __init__(self) -> None:
        self.captions: list[str] = []
        self.session_state: dict = {}

    def columns(self, spec, *_a, **_k):
        return tuple(_Cell() for _ in spec)

    def caption(self, text, *_a, **_k):
        self.captions.append(str(text))


def _render_contention(monkeypatch, *, live: bool, days, covered: int | None = None,
                       bounds: tuple | None = None) -> tuple[list[str], list[pd.DataFrame]]:
    """Render operations._contention_tab with a pressure result stamped exactly as run_mart_first
    stamps it (components._mark_served with the days/bounds the page passed). Today = 2026-10-01."""
    from app.ui import components
    from app.ui.pages import operations as ops
    df = pd.DataFrame({"WAREHOUSE_NAME": ["WH_A"], "QUERY_COUNT": [10], "QUEUED_SEC": [50.0],
                       "SPILL_REMOTE_GB": [0.0], "P95_ELAPSED_SEC": [3.0]})
    if covered is not None:
        df["COVERED_DAYS"] = covered
    pressure = QueryResult(df=df, ok=True, source="t")

    def fake_run_mart_first(_mart, _live, *, key: str, days=None, bounds=None, **_k):
        if key.startswith("c_pressure"):
            return components._mark_served(pressure, live=live, days=days, bounds=bounds)
        return QueryResult(df=pd.DataFrame(), ok=True, source="locks")

    fake, tables = _ContentionSt(), []
    monkeypatch.setattr(ops, "st", fake)
    monkeypatch.setattr(ops, "run_mart_first", fake_run_mart_first)
    monkeypatch.setattr(ops, "guard", lambda res, *_a, **_k: res is pressure)
    monkeypatch.setattr(ops, "account_today", lambda: date(2026, 10, 1))
    monkeypatch.setattr(ops, "charts", SimpleNamespace(bar_count=lambda *_a, **_k: None))
    monkeypatch.setattr(ops, "entity_nav_table", lambda frame, *_a, **_k: tables.append(frame))
    for name in ("section_header", "result_caption", "styled_table"):
        monkeypatch.setattr(ops, name, lambda *_a, **_k: None)
    ops._contention_tab("ALL", days, bounds=bounds)
    return fake.captions, tables


def test_r1_018_contention_says_when_the_hourly_fact_holds_less_than_the_window(monkeypatch):
    # mart leg, 365d Window, fact holds 86 days: pre-fix the rows were captioned as if they covered the
    # Window (or blamed "the live fallback", which never ran). Now the fact's own span is named.
    caps, tables = _render_contention(monkeypatch, live=False, days=365, covered=86)
    (cap,) = [c for c in caps if "Covers only" in c or "Served the last" in c]
    assert cap == ("Covers only the last 86 days — the hourly fact holds 86 days of history "
                   "(it is not backfilled).")
    # the scalar feeds the caption; it is not a table column
    assert tables and "COVERED_DAYS" not in tables[0].columns
    # a fact that covers the whole window says nothing extra
    caps, _ = _render_contention(monkeypatch, live=False, days=30, covered=86)
    assert not [c for c in caps if "Covers only" in c or "Served the last" in c]


def test_r1_018_contention_live_fallback_keeps_its_90_day_caption(monkeypatch):
    caps, _ = _render_contention(monkeypatch, live=True, days=365)
    assert "Served the last 90 days — the live fallback reads at most 90." in caps
    assert not [c for c in caps if "hourly fact" in c]
    # a calendar read is unclamped on the live leg too (R1-213), so no 90-day claim there
    caps, _ = _render_contention(monkeypatch, live=True, days=CalendarDayOffset(273),
                                 bounds=(date(2026, 1, 1), date(2026, 10, 2)))
    assert not [c for c in caps if "Served the last" in c or "Covers only" in c]


def test_r1_018_contention_calendar_window_compares_against_its_first_day(monkeypatch):
    ytd = (date(2026, 1, 1), date(2026, 10, 2))
    caps, _ = _render_contention(monkeypatch, live=False, days=CalendarDayOffset(273), covered=86,
                                 bounds=ytd)
    assert "Covers only the last 86 days — the hourly fact holds 86 days of history " \
           "(it is not backfilled)." in caps
    # Last month reaches back PAST its own 30-day span: on Oct 1 it starts 31 days ago, so a fact
    # holding 30 days misses Sep 1 even though 30 == the bounds span.
    sept = (date(2026, 9, 1), date(2026, 10, 1))
    caps, _ = _render_contention(monkeypatch, live=False, days=CalendarDayOffset(30), covered=30,
                                 bounds=sept)
    assert "Covers only the last 30 days — the hourly fact holds 30 days of history " \
           "(it is not backfilled)." in caps
    caps, _ = _render_contention(monkeypatch, live=False, days=CalendarDayOffset(30), covered=31,
                                 bounds=sept)
    assert not [c for c in caps if "Covers only" in c]


def test_r1_018_ops_diag_keeps_its_disclosed_90_day_cap():
    # Deliberate: raising it would trip the coverage gate into the (also 90-capped) live scan.
    assert mart27_sql.OPS_DIAG_MAX_DAYS == 90
    for sql in (mart27_sql.ops_diag_top_queries(365), mart27_sql.ops_diag_failures(365)):
        assert "d.HOUR_TS >= DATEADD('day', -90, CURRENT_DATE())" in sql
    ops = _read("app/ui/pages/operations.py")
    assert "Heaviest queries and Failures by error read the" in ops and "last 90 days." in ops


# ---------------------------------------------------------------------------
# R1-019 — delivery SLO route failures dedupe on the ROUTE id, not the CONTEXT text
# ---------------------------------------------------------------------------

def test_r1_019_route_failures_count_one_route_day_once():
    sql = mart_sql.delivery_slo_summary(30)
    m = re.search(r"\(SELECT COUNT\(DISTINCT .*?\) AS ROUTE_FAILURES", sql, re.S)
    assert m, "ROUTE_FAILURES subquery not found"
    expr = _strip_db(m.group(0)[: -len(" AS ROUTE_FAILURES")])
    expr = expr.replace("DATEADD('day', -30, CURRENT_TIMESTAMP())", "'2026-09-01 00:00:00'")
    con = sqlite3.connect(":memory:")
    con.create_function("SPLIT_PART", 3, lambda s, d, n: (str(s).split(d) + [""] * n)[n - 1])
    con.create_function("DATE_TRUNC", 2, lambda unit, ts: str(ts)[:10])
    con.create_function("TO_VARCHAR", 1, str)
    con.execute("CREATE TABLE APP_ERROR_LOG (PAGE TEXT, ERROR_TYPE TEXT, CONTEXT TEXT, LOGGED_AT TEXT)")
    # one route (3) broken all day: the hourly drain AND the V164 escalation re-post both fail
    con.executemany("INSERT INTO APP_ERROR_LOG VALUES ('NotifyWebhook', 'route_send_failed', ?, ?)", [
        ("route 3 integration TEAMS_A - will retry next run; other routes unaffected", "2026-09-29 08:00:00"),
        ("route 3 integration TEAMS_A - will retry next run; other routes unaffected", "2026-09-29 09:00:00"),
        ("route 3 integration TEAMS_A - escalation re-post; the email leg is unaffected", "2026-09-29 09:00:00"),
        ("route 4 integration TEAMS_B - will retry next run; other routes unaffected", "2026-09-29 09:00:00"),
        ("route 3 integration TEAMS_A - will retry next run; other routes unaffected", "2026-09-30 09:00:00"),
    ])
    (n,) = con.execute(f"SELECT {expr}").fetchone()
    assert n == 3          # (route 3, 09-29), (route 4, 09-29), (route 3, 09-30) — was 4


# ---------------------------------------------------------------------------
# R1-020 — role_share mart twin drops the 'NONE' pseudo-warehouse like the live twin
# ---------------------------------------------------------------------------

def test_r1_020_role_share_excludes_the_none_warehouse_before_the_share():
    sql = mart27_sql.role_share(30, "ALL")
    scoped = sql.split("WITH scoped AS (", 1)[1].split("), shared AS", 1)[0]
    assert "UPPER(WAREHOUSE_NAME) <> 'NONE'" in scoped     # before RATIO_TO_REPORT and the LIMIT
    assert "WAREHOUSE_NAME IS NOT NULL" in chargeback_sql.role_share_within_warehouse(30, "ALL")
    # the other FACT_QUERY_ROLE_HOURLY reader keeps warehouse-less role use (it is still role use)
    assert "'NONE'" not in mart27_sql.unused_roles_via_fact(90)


# ---------------------------------------------------------------------------
# R1-041 — the DQ row-volume series must not be cut by run()'s 5,000-row default
# ---------------------------------------------------------------------------

def test_r1_041_dq_row_volume_cap_fits_the_whole_bounded_series():
    assert dq_sql.DQ_MAX_ROWS >= dq_sql.DQ_MAX_TABLES * dq_sql.DQ_WINDOW_DAYS
    assert dq_sql.DQ_MAX_ROWS > DEFAULT_MAX_ROWS
    sql = dq_sql.product_row_volume(dq_sql.DQ_WINDOW_DAYS)
    assert f"QUALIFY DENSE_RANK() OVER (ORDER BY m.FQN) <= {dq_sql.DQ_MAX_TABLES}" in sql
    ops = _read("app/ui/pages/operations.py")
    sites = [m.start() for m in re.finditer(r"dq_sql\.product_row_volume\(28\)", ops)]
    assert len(sites) == 2
    for at in sites:                                   # the run() fallback AND the run_batch spec
        assert "dq_sql.DQ_MAX_ROWS" in ops[at:at + 260], ops[at:at + 260]


def test_r1_041_run_keeps_every_row_of_a_200_table_series(monkeypatch):
    from app.core import query as q
    monkeypatch.setattr(q, "apply_query_tag", lambda *a, **k: None)
    monkeypatch.setattr(q, "apply_statement_timeout", lambda *a, **k: None)
    monkeypatch.setattr(q, "record_error", lambda *a, **k: None)
    monkeypatch.setattr(q, "_telemetry", lambda *a, **k: None)
    full = pd.DataFrame({"FQN": [f"DB.S.T{t:03d}" for t in range(200) for _ in range(28)],
                         "DAY": [d for _ in range(200) for d in range(28)],
                         "ROWS_ADDED": 10_000})

    def fetch(sql, scope, page):                       # honors the trailing LIMIT run() appends
        lim = re.search(r"LIMIT (\d+)\s*$", sql)
        return full.head(int(lim.group(1))) if lim else full

    monkeypatch.setitem(q._FETCHERS, "recent", fetch)
    sql = dq_sql.product_row_volume(28)
    capped = q.run(sql, page="T", key="dq", tier="recent")             # the old call: default cap
    assert capped.truncated and capped.df["FQN"].nunique() < 200
    res = q.run(sql, page="T", key="dq", tier="recent", max_rows=dq_sql.DQ_MAX_ROWS)
    assert not res.truncated and len(res.df) == 5600 and res.df["FQN"].nunique() == 200


# ---------------------------------------------------------------------------
# R1-055 — a statement with no SESSIONS row is not a non-self-reporting client
# ---------------------------------------------------------------------------

def test_r1_055_no_session_record_is_its_own_bucket():
    assert chatter_sql.NO_SESSION_RECORD == "(no session record)"
    by_app = chatter_sql.chatter_by_application()
    assert "COALESCE(s.APPLICATION, '(no session record)') AS APPLICATION" in by_app
    assert "COALESCE(s.APPLICATION, '(unknown)')" not in by_app
    fam = chatter_sql.chatter_families_for_application("(no session record)")
    assert "COALESCE(s.APPLICATION, '(no session record)') = '(no session record)'" in fam
    # '(unknown)' stays _APP_EXPR's: a session that reports no client program (the KPI's population)
    assert "'(unknown)')" in by_app.split("sess AS (", 1)[1]
    ops = _read("app/ui/pages/operations.py")
    assert "'(no session record)'" in ops


# ---------------------------------------------------------------------------
# R1-057 — recon 'still breaking' is anchored to the calendar, not only the error cohort
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 9, 30, 10, 0, 0)


class _MaxBy:
    def __init__(self):
        self.best = None

    def step(self, v, k):
        if k is not None and (self.best is None or k > self.best[0]):
            self.best = (k, v)

    def finalize(self):
        return None if self.best is None else self.best[1]


def _recon_db(rows: list[tuple[str, str, str]]) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.create_aggregate("MAX_BY", 2, _MaxBy)
    # v4.606 integration: R1-137 wraps the recurrence scan with SUM(IFF(...)) OVER () totals.
    con.create_function("IFF", 3, lambda c, a, b: a if c else b)
    con.create_function("DATEDIFF", 3, lambda u, a, b: (date.fromisoformat(str(b)[:10])
                                                        - date.fromisoformat(str(a)[:10])).days)
    con.execute("CREATE TABLE RECON_MTRC_ERROR (MTRC TEXT, FRQCY TEXT, VALUE_TYPE TEXT, "
                "RECON_MTRC_LAYER TEXT, SOURCE_LAYER TEXT, TARGET_LAYER TEXT, SOURCE_ERROR TEXT, "
                "TARGET_ERROR TEXT, LOAD_DTTM TEXT)")
    con.executemany("INSERT INTO RECON_MTRC_ERROR VALUES (?, ?, 'AMT', 'EDW', 'LDW', 'EDW', 's', 't', ?)",
                    rows)
    return con


def _recon_scan(con: sqlite3.Connection) -> pd.DataFrame:
    sql = etl_control_sql.recon_recurrence_scan("RECON_MTRC_ERROR", days=0)
    sql = re.sub(r"DATEADD\('day', -(\d+), CURRENT_TIMESTAMP\(\)\)",
                 lambda m: f"'{(_NOW - timedelta(days=int(m.group(1)))).isoformat(' ')}'", sql)
    sql = sql.replace(account_today_sql(), f"'{_NOW.date().isoformat()}'")
    sql = sql.replace("CAST(LOAD_DTTM AS DATE)", "date(LOAD_DTTM)").replace("LEAST(", "MIN(")
    assert "CURRENT_TIMESTAMP" not in sql and "::" not in sql
    return pd.read_sql_query(sql, con)


def test_r1_057_a_quiet_cohort_resolves_instead_of_reading_still_breaking():
    # DAILY metric M_PREM broke Sep 1-3; every recon has passed since (the table logs only failures).
    con = _recon_db([("M_PREM", "DAILY", f"2026-09-0{d} 02:00:00") for d in (1, 2, 3)]
                    + [("M_CLM", "DAILY", "2026-09-01 02:00:00")])
    scan = _recon_scan(con)
    prem = scan[scan["MTRC"] == "M_PREM"].iloc[0]
    assert not bool(prem["BROKE_LATEST_CYCLE"]) and int(prem["DAYS_SINCE_LAST_BREAK"]) == 27
    rec = recon_recurrence(scan)
    tiers = dict(zip(rec["MTRC"], rec["TIER"], strict=True))
    assert tiers == {"M_PREM": "RESOLVED", "M_CLM": "RESOLVED"}       # was CHRONIC / High


def test_r1_057_a_fresh_break_still_reads_latest_and_unknown_cadence_keeps_the_cohort_rule():
    con = _recon_db([("M_DAY", "DAILY", "2026-09-28 02:00:00"), ("M_DAY", "DAILY", "2026-09-29 02:00:00"),
                     ("M_ODD", "AD-HOC", "2026-07-15 02:00:00")])
    scan = _recon_scan(con).set_index("MTRC")
    assert bool(scan.loc["M_DAY", "BROKE_LATEST_CYCLE"])               # 1 day ago, within DAILY's 3
    assert bool(scan.loc["M_ODD", "BROKE_LATEST_CYCLE"])               # no cadence -> cohort-only


# ---------------------------------------------------------------------------
# R1-058 — ETL cost attribution's remainder is the run's warehouses, not the whole account
# ---------------------------------------------------------------------------

def _cost_db(task_a: str = "SP_A_LOAD") -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.create_function("CONTAINS", 2, lambda s, sub: int(str(sub) in str(s)))
    con.execute("CREATE TABLE CONTROL_STATUS (WORKFLOW_NAME TEXT, TASK_NAME TEXT, TASK_STATUS TEXT, "
                "TASK_START_DTTM TEXT, TASK_END_DTTM TEXT, RUN_ID TEXT)")
    con.executemany("INSERT INTO CONTROL_STATUS VALUES (?, ?, 'SUCCESS', ?, ?, ?)", [
        ("WF_B", "SP_B_LOAD", "2026-09-30 00:30:00", "2026-09-30 02:30:00", "R_B"),
        ("WF_A", task_a, "2026-09-30 01:00:00", "2026-09-30 02:00:00", "R_A"),
    ])
    con.execute("CREATE TABLE QH (QUERY_ID TEXT, QUERY_TEXT TEXT, START_TIME TEXT)")
    con.executemany("INSERT INTO QH VALUES (?, ?, ?)", [
        ("CALL_A", "CALL SP_A_LOAD()", "2026-09-30 01:05:00"),
        ("CALL_B", "CALL SP_B_LOAD()", "2026-09-30 01:10:00"),   # a concurrent workflow, other WH
        ("BI_1", "SELECT * FROM SALES", "2026-09-30 01:20:00"),  # BI, other WH
        ("ADHOC", "SELECT 1 FROM ETL_STAGE", "2026-09-30 01:30:00"),  # ad-hoc on the run's WH
    ])
    con.execute("CREATE TABLE QAH (QUERY_ID TEXT, ROOT_QUERY_ID TEXT, WAREHOUSE_NAME TEXT, START_TIME TEXT, "
                "CREDITS_ATTRIBUTED_COMPUTE REAL, CREDITS_USED_QUERY_ACCELERATION REAL)")
    con.executemany("INSERT INTO QAH VALUES (?, ?, ?, ?, ?, 0)", [
        ("CHILD_A", "CALL_A", "WH_ETL", "2026-09-30 01:06:00", 10.0),
        ("CHILD_B", "CALL_B", "WH_B", "2026-09-30 01:11:00", 30.0),
        ("BI_1", None, "WH_BI", "2026-09-30 01:20:00", 20.0),
        ("ADHOC", None, "WH_ETL", "2026-09-30 01:30:00", 5.0),
    ])
    return con


def _attribute(con: sqlite3.Connection) -> dict[str, float]:
    sql = etl_control_sql.run_cost_attribution_scan("CONTROL_STATUS", run_id="R_A")
    sql = (sql.replace("SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY", "QAH")
              .replace("SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY", "QH")
              .replace("CURRENT_TIMESTAMP()", "CURRENT_TIMESTAMP"))
    df = pd.read_sql_query(sql, con)
    return dict(zip(df["TASK_NAME"], df["CREDITS_ATTRIBUTED"], strict=True))


def test_r1_058_unattributed_holds_only_the_runs_warehouses():
    got = _attribute(_cost_db())
    # WF_A's own task is fully charged; the remainder is the ad-hoc query on WH_ETL only — not the
    # concurrent WF_B CALL (WH_B) nor the BI query (WH_BI). Was 55 (coverage 10/65 = 15%).
    assert got == {"SP_A_LOAD": 10.0, etl_control_sql.UNATTRIBUTED_TASK: 5.0}


def test_r1_058_nothing_matched_keeps_the_whole_window_so_zero_coverage_is_honest():
    got = _attribute(_cost_db(task_a="SP_NEVER_IN_TEXT"))
    assert got == {etl_control_sql.UNATTRIBUTED_TASK: 65.0}


def test_r1_058_panel_no_longer_calls_one_workflow_each_night():
    ops = _read("app/ui/pages/operations.py")
    body = ops.split("def _cost_attribution_panel(", 1)[1].split("\ndef ", 1)[0]
    assert "Each night's measured Snowflake credits" not in body
    assert "usually \"\n" not in body and "hasn't finished metering it" not in body
    assert "latest workflow run" in body


# ---------------------------------------------------------------------------
# R1-062 — serverless task days: newest first, and a cut is detectable
# ---------------------------------------------------------------------------

def test_r1_062_serverless_task_daily_keeps_newest_days_and_flags_truncation():
    from app.core.query import _with_row_cap
    sql = graph_sql.serverless_task_daily(90)
    assert "ORDER BY DAY DESC, SERVERLESS_CREDITS DESC" in sql
    assert _with_row_cap(sql, DEFAULT_MAX_ROWS) != sql     # the cap+1 canary arms -> truncated can fire
    caller = _read("app/ui/pages/cost_parts/unit_costs.py")
    # v4.606 integration: R1-061/163/167 put the kind split and the served-window label in front of the
    # guard(), so the block runs longer than the original 1,400 characters.
    block = caller.split("graph_sql.serverless_task_daily(", 1)[1][:2400]
    assert "if guard(sls," in block and "result_caption(sls)" in block


class _PanelSt:
    """The slice of streamlit the Serverless-tasks and Workflow-runtimes panels touch, recording the
    captions a viewer would see (AppTest is skipped on the floor CI leg, so these run everywhere)."""

    def __init__(self) -> None:
        self.captions: list[str] = []
        self.column_config = SimpleNamespace(NumberColumn=lambda *_a, **k: dict(k))

    def caption(self, text, *_a, **_k) -> None:
        self.captions.append(str(text))

    def markdown(self, *_a, **_k) -> None:
        return None

    def error(self, *_a, **_k) -> None:
        return None

    def expander(self, *_a, **_k):
        return contextlib.nullcontext()


def _render_serverless(monkeypatch, sls: QueryResult) -> list[tuple[str, str, str]]:
    """Run unit_costs._graphs_tab with one task-graph row (so the Serverless panel is reached) and
    the given SERVERLESS_TASK_HISTORY read; return every (kind, message, hint) empty_state got."""
    import app.ui.components as components
    import app.ui.pages.cost_parts.unit_costs as uc

    seen: list[tuple[str, str, str]] = []
    _rec = lambda kind, msg, *_a, hint="", **_k: seen.append((kind, msg, hint))  # noqa: E731
    # the panel splits a failed read on the kind itself (uc.empty_state) and lets guard() gate an
    # ok read (components.empty_state): record both
    monkeypatch.setattr(components, "empty_state", _rec)
    monkeypatch.setattr(uc, "empty_state", _rec)
    daily = pd.DataFrame({"DAY": [date(2026, 9, 30)], "PIPELINE": ["P"], "USD": [1.0], "USD_PER_RUN": [1.0]})
    summary = pd.DataFrame({"PIPELINE": ["P"], "USD": [1.0], "SUCCESS_PCT": [100.0]})
    monkeypatch.setattr(uc, "st", _PanelSt())
    monkeypatch.setattr(uc, "run_mart_first",
                        lambda *_a, **_k: QueryResult(df=pd.DataFrame({"X": [1]}), ok=True, source="t"))
    monkeypatch.setattr(uc, "graphs", SimpleNamespace(enrich_graph_daily=lambda *_a: daily,
                                                      pipeline_summary=lambda _d: summary))
    monkeypatch.setattr(uc, "charts", SimpleNamespace(daily_stacked_usd=lambda *_a, **_k: None))
    for name in ("kpi_row", "styled_table", "result_caption"):
        monkeypatch.setattr(uc, name, lambda *_a, **_k: None)
    monkeypatch.setattr(uc, "run", lambda *_a, **_k: sls)
    uc._graphs_tab("ALL", 30, 3.0)
    return seen


def test_r1_062_review_zero_serverless_rows_carry_no_access_hint(monkeypatch):
    # A successful zero-row read PROVES the role can read SERVERLESS_TASK_HISTORY, so it is the verified
    # clean row (R1-061) with no access hint under it.
    seen = _render_serverless(monkeypatch, QueryResult(df=pd.DataFrame(), ok=True, source="t"))
    assert seen == [("clean", "No serverless task credits in this scope/window.", "")]


def test_r1_062_review_a_failed_serverless_read_names_the_grant_or_the_error(monkeypatch):
    # v4.606 integration: an "Insufficient privileges" error is a setup absence (v4.605 r3), so it names
    # the grant; any other failure is unavailable with the error (R1-061 / R1-167).
    priv = QueryResult(df=pd.DataFrame(), ok=False, source="t", error_kind="privilege",
                       error="SQL access control error: Insufficient privileges")
    ((kind, msg, _hint),) = _render_serverless(monkeypatch, priv)
    assert kind == "needs_setup" and "IMPORTED PRIVILEGES" in msg
    tmo = QueryResult(df=pd.DataFrame(), ok=False, source="t", error_kind="timeout", error="timed out")
    ((kind, msg, _hint),) = _render_serverless(monkeypatch, tmo)
    assert kind == "unavailable" and "could not be read" in msg


# ---------------------------------------------------------------------------
# R1-063 — a calendar day-0 Window is 'today', never an all-time scan
# ---------------------------------------------------------------------------

def test_r1_063_calendar_day_zero_reads_today_not_all_time():
    today = f"DATEADD('day', -0, {account_today_sql()})"
    for sql in (etl_control_sql.workflow_list_scan("DB.S.CS", days=CalendarDayOffset(0)),
                etl_control_sql.task_status_history_scan("DB.S.CS", days=CalendarDayOffset(0))):
        assert f"TASK_START_DTTM >= {today}" in sql
    # a plain 0 is still 'unscoped' (the existing lock), and a mid-period calendar offset anchors on
    # the period's first day at midnight, not now-minus-N
    assert "DATEADD" not in etl_control_sql.workflow_list_scan("DB.S.CS", days=0)
    assert f"DATEADD('day', -5, {account_today_sql()})" in \
        etl_control_sql.workflow_list_scan("DB.S.CS", days=CalendarDayOffset(5))
    recon = etl_control_sql.recon_recurrence_scan("DB.S.R", days=CalendarDayOffset(0))
    assert f"LOAD_DTTM >= {today}" in recon and "-90," not in recon
    assert etl_control_sql.recon_window_phrase(CalendarDayOffset(0)) == "today"
    assert etl_control_sql.recon_window_phrase(0) == "in the last 90 days"
    assert "drift" not in inspect.getdoc(etl_control_sql._window_clause)


def test_r1_063_review_unscoped_reads_claim_no_window():
    # v4.606 integration: the label itself is operations._etl_window_suffix (R1-139, which also names a
    # mid-period calendar offset by its first day); the reads it labels stay unscoped for these inputs.
    for unscoped in (0, -3, None, "30"):
        assert etl_control_sql._window_clause(unscoped) == ""


def _render_runtimes(monkeypatch, days: object, *, tasks: pd.DataFrame | None = None):
    """Run operations._workflow_runtimes_panel against a stubbed CONTROL_STATUS: the workflow list
    is empty unless ``tasks`` is given (then one workflow whose latest run is ``tasks``)."""
    import app.ui.pages.operations as ops

    seen: list[tuple[str, str]] = []
    fake = _PanelSt()
    wf_list = (pd.DataFrame() if tasks is None
               else pd.DataFrame({"WORKFLOW_NAME": ["WF_NIGHTLY"], "LAST_RUN_AT": [datetime(2026, 10, 1, 1)],
                                  "RUNS": [1]}))

    def fake_run(_sql, *_a, key: str = "", **_k):
        df = wf_list if key.startswith("etl_wf_list") else tasks
        return QueryResult(df=df if df is not None else pd.DataFrame(), ok=True, source="t")

    monkeypatch.setattr(ops, "st", fake)
    monkeypatch.setattr(ops, "run", fake_run)
    monkeypatch.setattr(ops, "load_settings", lambda _p: {"ETL_CONTROL_STATUS_FQN": "DB.S.CONTROL_STATUS"})
    monkeypatch.setattr(ops, "empty_state", lambda kind, msg, *_a, **_k: seen.append((kind, msg)))
    for name in ("section_header", "kpi_row", "styled_table", "result_caption", "_task_evidence_drill"):
        monkeypatch.setattr(ops, name, lambda *_a, **_k: None)
    ops._workflow_runtimes_panel(days)
    return seen, fake.captions


def test_r1_063_review_runtimes_panel_says_today_on_a_calendar_day_zero(monkeypatch):
    # Current month on the 1st: the list read is today only, so the empty state must say so.
    seen, _ = _render_runtimes(monkeypatch, CalendarDayOffset(0))
    assert seen == [("no_data_yet", "No ETL runs recorded (today). Widen the scope-bar Window to see "
                                    "older runs.")]
    start = datetime(2026, 10, 1, 1)
    tasks = pd.DataFrame({"WORKFLOW_NAME": ["WF_NIGHTLY"], "TASK_NAME": ["SP_LOAD"],
                          "TASK_STATUS": ["SUCCESS"], "TASK_START_DTTM": [start],
                          "TASK_END_DTTM": [start + timedelta(minutes=5)], "RUNTIME_SEC": [300.0],
                          "RUN_ID": ["R1"]})
    seen, captions = _render_runtimes(monkeypatch, CalendarDayOffset(0), tasks=tasks)
    assert seen == []
    assert any(c.startswith("Latest run of WF_NIGHTLY (today) — 1 task(s).") for c in captions)


def test_r1_063_review_runtimes_panel_keeps_trailing_and_unscoped_labels(monkeypatch):
    seen, _ = _render_runtimes(monkeypatch, 30)
    assert seen[0][1].startswith("No ETL runs recorded (last 30d).")
    seen, _ = _render_runtimes(monkeypatch, 0)     # unscoped = all time: no window claimed
    assert seen[0][1].startswith("No ETL runs recorded. Widen")


# ---------------------------------------------------------------------------
# R1-064 — failed-runs drill elapsed time humanizes (named in milliseconds)
# ---------------------------------------------------------------------------

def test_r1_064_failed_runs_elapsed_is_an_ms_duration_column():
    from app.ui.components import _duration_unit_for_column
    sql = etl_sql.etl_failed_runs_for_pipeline("nightly_load", 30, "ALFA")
    assert "q.TOTAL_ELAPSED_TIME AS TOTAL_ELAPSED_MS," in sql
    assert "q.TOTAL_ELAPSED_TIME,\n" not in sql
    assert _duration_unit_for_column("TOTAL_ELAPSED_MS") == "ms"


# ---------------------------------------------------------------------------
# R1-067 — DEPLOY_ACTORS matches whole list members, never a substring
# ---------------------------------------------------------------------------

def _change_source(actors: str | None, changed_by: str | None) -> str:
    sql = change_impact_sql.warehouse_change_registry(90, "ALL")
    iff = re.search(r"IFF\(w\.CHANGED_BY IS NULL.*?\) AS CHANGE_SOURCE", sql, re.S).group(0)
    iff = iff[: -len(" AS CHANGE_SOURCE")]
    iff = re.sub(r"POSITION\((.+?) IN (da\.ACTORS)\)", r"instr(\2, \1)", iff, flags=re.S)
    sub = re.search(r"CROSS JOIN (\(SELECT .*?\)) da", sql, re.S).group(1)
    con = sqlite3.connect(":memory:")
    con.create_function("IFF", 3, lambda c, a, b: a if c else b)
    con.create_function("REGEXP_REPLACE", 3,
                        lambda s, pat, rep: re.sub(pat.replace("[[:space:]]", r"\s"), rep, s or ""))
    con.execute("CREATE TABLE SETTINGS (KEY TEXT, VALUE TEXT)")
    if actors is not None:
        con.execute("INSERT INTO SETTINGS VALUES ('DEPLOY_ACTORS', ?)", (actors,))
    q = f"SELECT {iff} FROM (SELECT ? AS CHANGED_BY) w CROSS JOIN {_strip_db(sub)} da"
    return con.execute(q, (changed_by,)).fetchone()[0]


def test_r1_067_deploy_actor_match_is_whole_member():
    assert _change_source("SVC_FLYWAY_PRD", "SVC_FLYWAY") == "MANUAL"          # prefix: was MANAGED
    assert _change_source("FLYWAY_SVC,TERRAFORM_SVC", "SVC") == "MANUAL"       # substring: was MANAGED
    assert _change_source("SVC_FLYWAY_PRD", "svc_flyway_prd") == "MANAGED"
    assert _change_source("FLYWAY_SVC, TERRAFORM_SVC\n", "terraform_svc") == "MANAGED"  # spaced list
    assert _change_source("", "JOE") == "MANUAL"                               # empty setting
    assert _change_source(None, "JOE") == "MANUAL"                             # no setting row
    assert _change_source("SVC_FLYWAY_PRD", None) == "UNKNOWN"


# ---------------------------------------------------------------------------
# R1-230 — the ML forecast reader keeps TODAY's row for the #24 today-remainder term
# ---------------------------------------------------------------------------

def test_r1_230_ml_forecast_reader_keeps_today():
    sql = mart_sql.ml_forecast_daily()
    assert f"WHERE TS::DATE >= {account_today_sql()}" in sql
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE FORECAST_ML_DAILY (TS TEXT, FORECAST_CREDITS REAL, LOWER_BOUND REAL, "
                "UPPER_BOUND REAL)")
    con.executemany("INSERT INTO FORECAST_ML_DAILY VALUES (?, 100, 80, 120)",
                    [((date(2026, 9, 13) + timedelta(days=i)).isoformat(),) for i in range(45)])
    run_sql = (_strip_db(sql).replace("TS::DATE", "date(TS)")
               .replace(account_today_sql(), "'2026-09-14'"))
    days = [r[0] for r in con.execute(run_sql)]
    assert days[0] == "2026-09-14"          # today's row is present for the remainder proration
