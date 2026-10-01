"""PR-1 (cluster c01-ops) locks: Operations-page defects confirmed by the 04fd374e adversarial review.

Each test names its finding id. Render tests drive the real page function with recording fakes (the
tests/test_probe_absence_split.py pattern); source locks cover the sites buried in large functions.
"""

from __future__ import annotations

import ast
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from app.logic.date_windows import CalendarDayOffset
from tests._source import read

_OPS = "app/ui/pages/operations.py"


def _fn(src: str, name: str) -> str:
    """The source of one top-level function of a module."""
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"no function {name}")


def _ok(df: pd.DataFrame, **extra):
    return SimpleNamespace(ok=True, empty=df.empty, df=df, error="", error_kind="", truncated=False,
                           usable=lambda: not df.empty, **extra)


def _failed(kind: str):
    return SimpleNamespace(ok=False, empty=True, df=pd.DataFrame(), error=f"boom ({kind})", error_kind=kind,
                           truncated=False, usable=lambda: False)


class _Stop(Exception):
    """Raised by a recording fake to end a page function once the value under test is captured."""


# ------------------------------------------------------- R1-049 / R1-013: divide by the bounds SPAN ----

def test_wasted_spend_monthlyizes_by_the_bounds_span():
    # R1-049: Current month passes a day OFFSET (Sep 2 MTD = 1) while the bounded scan covers the
    # span (Sep 1..Sep 3 exclusive = 2 days); the offset divisor doubled "Monthly-ized" on the 2nd.
    body = _fn(read(_OPS), "_queries_tab")
    assert ("_waste_served = ((bounds[1] - bounds[0]).days if bounds is not None\n"
            "                             else min(int(days), MAX_LIVE_WINDOW_DAYS))") in body
    assert "_waste_served = days if bounds is not None" not in body


def test_ops_sizing_divides_by_the_bounds_span(monkeypatch):
    # R1-013 / R1-049: the mart's COVERED_DAYS is 2 on Sep 2 under Current month; dividing by the
    # offset (1) doubled MONTHLY_USD_NOW / IDLE_MONTHLY_USD and gave ACTIVE_DAYS_PER_30D = 60.
    from app.ui.pages import operations as ops
    seen: dict = {}
    prof = _ok(pd.DataFrame({"WAREHOUSE_NAME": ["WH_A"], "COVERED_DAYS": [2]}))
    prof.df.attrs["_ow_effective_days"] = 1          # the stamp run_mart_first writes for offset 1

    def fake_size(_df, _rate, days_):
        seen["days"] = days_
        raise _Stop

    monkeypatch.setattr(ops, "st", SimpleNamespace(toggle=lambda *_a, **_k: True))
    monkeypatch.setattr(ops, "section_header", lambda *_a, **_k: None)
    monkeypatch.setattr(ops, "run_mart_first", lambda *_a, **_k: prof)
    monkeypatch.setattr(ops, "guard", lambda *_a, **_k: True)
    monkeypatch.setattr(ops, "run", lambda *_a, **_k: _ok(pd.DataFrame()))
    monkeypatch.setattr(ops, "size_recommendations", fake_size)
    with pytest.raises(_Stop):
        ops._wh_sizing_efficiency("ALL", 3.68, CalendarDayOffset(1),
                                  bounds=(date(2026, 9, 1), date(2026, 9, 3)))
    assert seen["days"] == 2
    body = _fn(read(_OPS), "_wh_sizing_efficiency")
    assert "served_days(_prof, _span)" in body and "served_days(_prof, days)" not in body


# ------------------------------------------- R1-039: proc_regression's PRIOR window per preset ----

def _split(sql: str) -> tuple[str, str]:
    """(scan-from literal, CUR-split literal) of a bounded proc_regression render."""
    import re
    scan = re.search(r"START_TIME >= '(\d{4}-\d{2}-\d{2})' AND START_TIME < '(\d{4}-\d{2}-\d{2})'", sql)
    cur = re.search(r"IFF\(START_TIME >= '(\d{4}-\d{2}-\d{2})', 'CUR', 'PRIOR'\)", sql)
    assert scan and cur, sql
    return f"{scan.group(1)}..{scan.group(2)}", cur.group(1)


@pytest.mark.parametrize(("bounds", "scan", "cur"), [
    # Last month (a whole calendar month): August vs July, unchanged
    ((date(2026, 8, 1), date(2026, 9, 1)), "2026-07-01..2026-09-01", "2026-08-01"),
    # Current month on Sep 3: Sep 1-3 vs the 3 days before (was: vs all of August)
    ((date(2026, 9, 1), date(2026, 9, 4)), "2026-08-29..2026-09-04", "2026-09-01"),
    # Current month on the 1st: today vs yesterday -- never an empty PRIOR
    ((date(2026, 9, 1), date(2026, 9, 2)), "2026-08-31..2026-09-02", "2026-09-01"),
    # Current year on Sep 30: 273 days vs the 273 before (was: nine months vs December alone)
    ((date(2026, 1, 1), date(2026, 10, 1)), "2025-04-03..2026-10-01", "2026-01-01"),
    # Current month on its last day is a whole month: the calendar month before
    ((date(2026, 9, 1), date(2026, 10, 1)), "2026-08-01..2026-10-01", "2026-09-01"),
    # a January whole month wraps the year
    ((date(2026, 1, 1), date(2026, 2, 1)), "2025-12-01..2026-02-01", "2026-01-01"),
])
def test_proc_regression_prior_window_per_preset(bounds, scan, cur):
    from app.data import ops_sql
    assert _split(ops_sql.proc_regression(int((bounds[1] - bounds[0]).days), bounds=bounds)) == (scan, cur)


def test_proc_regression_caption_names_the_prior_window():
    src = read(_OPS)
    assert "the prior equal-length window (percent change" not in src
    assert "the calendar month before \"\n                \"under Last month, else the equal-length window just before" in src


# ----------------------------------- R1-046 / R1-075 / R1-130: pipeline-SLA failed read by its kind ----

_SETUP = ("absent", "privilege", "unknown_function")
_FAILED = ("missing_column", "timeout", "other")


class _RecSt:
    """A recording stand-in for streamlit: captions/markdown/warnings, expander labels, inert inputs."""

    def __init__(self, toggles: bool = True):
        from contextlib import contextmanager
        self.calls: list[tuple[str, str]] = []
        self.session_state: dict = {}
        self._toggles = toggles

        @contextmanager
        def _expander(label="", *_a, **_k):
            self.calls.append(("expander", str(label)))
            yield self
        self.expander = _expander

    def __getattr__(self, name):           # any other st.* call: record and return an inert value
        def _call(*a, **_k):
            self.calls.append((name, str(a[0]) if a else ""))
            return None
        return _call

    def caption(self, text, *_a, **_k):
        self.calls.append(("caption", str(text)))

    def toggle(self, *_a, **_k):
        return self._toggles

    def columns(self, n, *_a, **_k):
        from contextlib import nullcontext
        return [nullcontext() for _ in range(n if isinstance(n, int) else len(n))]

    def text_input(self, *_a, **_k):
        return ""

    def number_input(self, *_a, value=0.0, **_k):
        return value

    def button(self, *_a, **_k):
        return False

    def text(self, kind: str) -> str:
        return "\n".join(t for k, t in self.calls if k == kind)


def _page(monkeypatch, results: dict, *, toggles: bool = True, **extra):
    """Patch operations' st / run / empty_state / render helpers with recorders."""
    from app.ui.pages import operations as ops
    fake = _RecSt(toggles)
    seen: dict = {"runs": [], "empty": [], "detail": [], "kpis": [], "tables": [], "headers": []}

    def fake_run(_sql, *_a, key: str = "", **_kw):
        seen["runs"].append(key)
        return results[key]

    def fake_empty(kind, msg, *_a, **k):
        seen["empty"].append((kind, msg))
        seen["detail"].append(k.get("detail"))

    monkeypatch.setattr(ops, "st", fake)
    monkeypatch.setattr(ops, "run", fake_run)
    monkeypatch.setattr(ops, "empty_state", fake_empty)
    for name, value in {"section_header": lambda title, health="", *_a, **_k: seen["headers"].append((title, health)),
                        "result_caption": lambda *_a, **_k: None, "panel_help": lambda *_a, **_k: None,
                        "kpi_row": lambda items, *_a, **_k: seen["kpis"].append(items),
                        "styled_table": lambda df, *_a, **_k: seen["tables"].append(df),
                        "entity_nav_table": lambda df, *_a, **_k: seen["tables"].append(df),
                        **extra}.items():
        monkeypatch.setattr(ops, name, value)
    return ops, fake, seen


def _stop(*_a, **_k):
    raise _Stop


@pytest.mark.parametrize("kind", _SETUP + _FAILED)
def test_pipeline_sla_failed_read_renders_by_kind(monkeypatch, kind):
    ops, fake, seen = _page(monkeypatch, {"sla_status": _failed(kind)}, _pipeline_prefetch=_stop)
    with pytest.raises(_Stop):                       # stop at the next chapter's prefetch
        ops._pipeline_data_checks(is_operator=True)
    ((state, msg),) = seen["empty"]
    if kind in _SETUP:
        assert state == "needs_setup" and "not installed yet" in msg
        assert ("expander", "Register a table") not in fake.calls
    else:
        assert state == "unavailable" and "not installed" not in msg
        assert "Pipeline SLA freshness could not be read" in msg
        assert seen["detail"] == [f"boom ({kind})"]
        # registering only MERGEs OVERWATCH's own config table, so a failed DML scan keeps it reachable
        assert ("expander", "Register a table") in fake.calls
    assert not seen["kpis"] and not seen["tables"]


def test_pipeline_sla_ok_empty_keeps_the_register_expander(monkeypatch):
    ops, fake, seen = _page(monkeypatch, {"sla_status": _ok(pd.DataFrame())}, _pipeline_prefetch=_stop)
    with pytest.raises(_Stop):
        ops._pipeline_data_checks(is_operator=False)
    assert [k for k, _m in seen["empty"]] == ["needs_setup"] and "No tables registered" in seen["empty"][0][1]
    assert ("expander", "Register a table") in fake.calls


# ------------------------------------------- R1-047: volume_deltas keeps FAILED rows above the LIMIT ----

def test_volume_deltas_ranks_by_severity_before_the_limit():
    import sqlglot

    from app.data import ops_sql
    sql = ops_sql.volume_deltas()
    assert ("ORDER BY CASE STATUS WHEN 'FAILED' THEN 0 WHEN 'WATCH' THEN 1 ELSE 2 END,\n"
            "         DROP_PCT DESC, DATABASE_NAME, SCHEMA_NAME, TABLE_NAME\nLIMIT 50") in sql
    assert "ORDER BY DROP_PCT DESC\nLIMIT 50" not in sql
    sqlglot.parse(sql, dialect="snowflake")


def test_volume_deltas_failed_row_survives_fifty_suppressed_weekday_rows():
    # The reviewer's Monday scenario run through sqlite: 60 Mon-Fri tables (yesterday = Sunday = 0
    # rows, same weekday last week = 0 -> NORMAL at DROP_PCT 100) and one daily table that fell 70%.
    import sqlite3

    import sqlglot

    from app.data import ops_sql
    lite = sqlglot.transpile(ops_sql.volume_deltas(), read="snowflake", write="sqlite")[0]
    lite = (lite.replace("SNOWFLAKE.ACCOUNT_USAGE.TABLE_DML_HISTORY", "DML")
                .replace("CURRENT_DATE", "'2026-09-28'"))
    con = sqlite3.connect(":memory:")
    con.create_function("REGEXP_LIKE", 3, lambda *_a: 0)
    con.execute("CREATE TABLE DML (DATABASE_NAME, SCHEMA_NAME, TABLE_NAME, START_TIME, ROWS_ADDED)")
    rows = []
    for d in range(2, 9):                                          # Sep 20..Sep 26 (d-8 .. d-2)
        day = f"2026-09-{28 - d:02d}"
        wd = date(2026, 9, 28 - d).weekday()
        if wd < 5:
            rows.extend(("BIZ_DB", "S", f"T{t:02d}", day + " 01:00:00", 2000) for t in range(60))
        rows.append(("DAILY_DB", "S", "FEED", day + " 01:00:00", 10000))
    rows.append(("DAILY_DB", "S", "FEED", "2026-09-27 01:00:00", 3000))   # yesterday: a 70% drop
    con.executemany("INSERT INTO DML VALUES (?,?,?,?,?)", rows)
    try:
        got = con.execute(lite).fetchall()
    except sqlite3.OperationalError as exc:                          # dialect gap -> the SQL lock above
        pytest.skip(f"sqlite cannot run the transpiled builder: {exc}")
    assert len(got) == 50
    assert got[0][2] == "FEED" and got[0][-1] == "FAILED"


# --------------------------------------------- R1-053: lock_contention clips to the 7-day CAP ----

@pytest.mark.parametrize(("days", "bounds", "start", "end"), [
    # Current month on the 1st: offset 0 used to render an EMPTY [Sep 2, Sep 2) window
    (CalendarDayOffset(0), (date(2026, 9, 1), date(2026, 9, 2)), "2026-09-01", "2026-09-02"),
    # Sep 5 (offset 4): the offset clip dropped the 1st
    (CalendarDayOffset(4), (date(2026, 9, 1), date(2026, 9, 6)), "2026-09-01", "2026-09-06"),
    # late in the month the 7-day cost cap still holds
    (CalendarDayOffset(29), (date(2026, 9, 1), date(2026, 10, 1)), "2026-09-24", "2026-10-01"),
    # Current year on Jan 1
    (CalendarDayOffset(0), (date(2026, 1, 1), date(2026, 1, 2)), "2026-01-01", "2026-01-02"),
    # Last month: unchanged (round 5)
    (31, (date(2026, 8, 1), date(2026, 9, 1)), "2026-08-25", "2026-09-01"),
])
def test_lock_contention_bounds_clip_uses_the_cap_not_the_offset(days, bounds, start, end):
    from app.data import ops_sql
    sql = ops_sql.lock_contention(min(days, 14), bounds=bounds)
    assert f"REQUESTED_AT >= '{start}' AND REQUESTED_AT < '{end}'" in sql


# ------------------------------------- R1-133: lock waits scope in SQL, before the LIMIT 50 ----

def test_lock_builders_scope_company_and_database_before_the_limit():
    import sqlglot

    from app.data import mart27_sql, ops_sql
    live = ops_sql.lock_contention(7, company="ALFA", database="TARGET_DB")
    assert "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME) = 'ALFA'" in live
    assert "UPPER(DATABASE_NAME) IN ('TARGET_DB')" in live
    assert live.index("TARGET_DB") < live.index("GROUP BY") < live.index("LIMIT 50")
    mart = mart27_sql.lock_wait_daily(14, "ALFA", database="TARGET_DB")
    assert "UPPER(c.DATABASE_NAME) IN ('TARGET_DB')" in mart
    assert mart.index("TARGET_DB") < mart.index("GROUP BY") < mart.index("LIMIT 50")
    for sql in (live, mart):
        sqlglot.parse(sql, dialect="snowflake")
    # unscoped renders keep their old shape
    assert "COMPANY_FOR_DATABASE" not in ops_sql.lock_contention(7)
    assert "UPPER(c.DATABASE_NAME) IN" not in mart27_sql.lock_wait_daily(14, "ALFA")


def test_lock_wait_daily_database_ranked_past_fifty_still_serves():
    # The reviewer's mart repro: 55 busier groups in other ALFA databases rank above TARGET_DB's 5,
    # so the account-wide LIMIT 50 used to hold none of them and the page said "no lock waits".
    import sqlite3

    import sqlglot

    from app.data import mart27_sql
    sql = mart27_sql.lock_wait_daily(14, "ALFA", database="TARGET_DB")
    lite = sqlglot.transpile(sql, read="snowflake", write="sqlite")[0]
    lite = lite.replace("DBA_MAINT_DB.OVERWATCH.MART_LOCK_WAIT_DAILY", "MART").replace("CURRENT_DATE", "'2026-09-30'")
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE MART (DAY, COMPANY, DATABASE_NAME, SCHEMA_NAME, OBJECT_NAME, LOCK_TYPE, "
                "WAIT_EVENTS, ACQUIRED_WAIT_SEC, NEVER_ACQUIRED, LAST_SEEN)")
    rows = [("2026-09-29", "ALFA", f"OTHER_{i:02d}", "S", "T", "TABLE", 5, 50, 3, "2026-09-29")
            for i in range(55)]
    rows += [("2026-09-29", "ALFA", "TARGET_DB", "S", f"T{i}", "TABLE", 2, 10, 0, "2026-09-29")
             for i in range(5)]
    con.executemany("INSERT INTO MART VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    try:
        got = con.execute(lite).fetchall()
    except sqlite3.OperationalError as exc:                          # dialect gap -> the SQL lock above
        pytest.skip(f"sqlite cannot run the transpiled builder: {exc}")
    assert len(got) == 5 and {r[0] for r in got} == {"TARGET_DB"}


def test_lock_waits_page_drops_the_post_limit_pandas_seam():
    body = read(_OPS)
    assert "mart27_sql.lock_wait_daily(min(days, 14), company, bounds=bounds, database=_lock_db)" in body
    assert "ops_sql.lock_contention(min(days, 14), bounds=bounds, company=company, database=_lock_db)" in body
    assert "classify_database) == company" not in body
    assert 'st.caption("No lock waits in this company/database scope.")' not in body   # C25: no raw absence
    # the Warehouses section now declares the Database filter as panel-dependent (Lock waits applies it)
    assert '"partial": ("days", "database"),' in body



# ------------------------------------- R1-059 / R1-138: SLA finish forecast's zero-row scan ----

def test_sla_finish_forecast_zero_nights_is_setup_not_clean(monkeypatch):
    # a misnamed ETL_CYCLE_START_WORKFLOW makes the all-time scan return zero rows; that used to render
    # the green verified-clean "Checked · Clear" row and tell the Tonight tile "no judged nights yet"
    ops, _fake, seen = _page(monkeypatch, {"etl_cycle_finish": _ok(pd.DataFrame(
        columns=["CYCLE_DATE", "CYCLE_START", "CYCLE_FINISH"]))},
        load_settings=lambda *_a, **_k: {"ETL_CONTROL_STATUS_FQN": "DB.SCH.CONTROL_STATUS",
                                         "ETL_CYCLE_START_WORKFLOW": "WF_TYPO_DOES_NOT_EXIST",
                                         "ETL_CYCLE_END_WORKFLOW": "WF_BASE_GW_CLOSEOUT_CTL_DLY"})
    out = ops._sla_finish_forecast_panel(pf=None)
    assert out == {"_reason": "needs_setup"}
    ((state, msg),) = seen["empty"]
    assert state == "needs_setup" and "WF_TYPO_DOES_NOT_EXIST" in msg and "ETL_CYCLE_START_WORKFLOW" in msg
    body = _fn(read(_OPS), "_sla_finish_forecast_panel")
    assert 'kind="clean"' not in body and 'empty_state("clean"' not in body


# ------------------------------- R1-066 / R1-137: recon banners count the window, not the cap ----

_RECON_SETTINGS = {"ETL_RECON_ERROR_FQN": "ALFA_EDW_PRD.DB_T_PROD_CORE.RECON_MTRC_ERROR"}


def test_recon_scans_carry_pre_limit_window_totals():
    import sqlglot

    from app.data import etl_control_sql as etl
    errs = etl.recon_errors_scan(_RECON_SETTINGS["ETL_RECON_ERROR_FQN"])
    assert "COUNT(*) OVER () AS TOTAL_ERRORS, COUNT(DISTINCT MTRC) OVER () AS TOTAL_METRICS" in errs
    rec = etl.recon_recurrence_scan(_RECON_SETTINGS["ETL_RECON_ERROR_FQN"])
    assert "COUNT(*) OVER () AS TOTAL_CHECKS" in rec
    assert "SUM(IFF(r.BROKE_LATEST_CYCLE, 1, 0)) OVER () AS ACTIVE_CHECKS_TOTAL" in rec
    # still-breaking checks rank first, so the LIMIT evicts resolved ones before a latest-cycle break
    assert "ORDER BY r.BROKE_LATEST_CYCLE DESC, r.RECURRENCE_PCT DESC" in rec
    assert rec.index("ACTIVE_CHECKS_TOTAL") < rec.index(") r\n") < rec.index("LIMIT 300")
    for sql in (errs, rec):
        sqlglot.parse(sql, dialect="snowflake")


def test_recon_error_headline_reads_the_window_totals(monkeypatch):
    df = pd.DataFrame({"MTRC": [f"M{i % 25}" for i in range(500)], "LOAD_DTTM": ["2026-09-29"] * 500,
                       "TOTAL_ERRORS": [760] * 500, "TOTAL_METRICS": [35] * 500})
    ops, fake, seen = _page(monkeypatch, {"etl_recon_errors": _ok(df)},
                            load_settings=lambda *_a, **_k: _RECON_SETTINGS, guard=lambda *_a, **_k: True)
    ops._recon_error_panel(pf=None)
    head = fake.text("error")
    assert "760 reconciliation error(s) across 35 metric(s)" in head, head
    assert "newest 500 of 760" in fake.text("caption")
    ((shown,),) = [(t,) for t in seen["tables"]]
    assert "TOTAL_ERRORS" not in shown.columns and "TOTAL_METRICS" not in shown.columns


def test_recon_recurrence_banner_reads_the_active_total(monkeypatch):
    # 300 resolved checks fill the capped frame while 5 checks broke in the latest cycle: the banner
    # used to say "300 metric(s) ... none broke in the latest cycle"
    n = 300
    df = pd.DataFrame({"MTRC": [f"M{i}" for i in range(n)], "FRQCY": ["DAILY"] * n,
                       "VALUE_TYPE": ["V"] * n, "RECON_MTRC_LAYER": ["L"] * n,
                       "BROKEN_CYCLES": [40] * n, "TOTAL_ERROR_CYCLES": [90] * n,
                       "RECURRENCE_PCT": [44] * n, "RECENT_BROKEN": [0] * n, "RECENT_WINDOW": [5] * n,
                       "BROKE_LATEST_CYCLE": [False] * n, "ERROR_ROWS": [40] * n,
                       "TOTAL_CHECKS": [405] * n, "ACTIVE_CHECKS_TOTAL": [5] * n})
    ops, fake, _seen = _page(monkeypatch, {"etl_recon_recurrence_0": _ok(df)},
                            load_settings=lambda *_a, **_k: _RECON_SETTINGS, guard=lambda *_a, **_k: True)
    ops._recon_recurrence_panel(0, pf=None)
    assert "5 metric(s) still breaking as of the latest cycle" in fake.text("error")
    assert "none broke in the latest cycle" not in fake.text("warning")
    assert "300 of 405 checks" in fake.text("caption")


def test_ref_gap_headline_marks_a_capped_count():
    body = _fn(read(_OPS), "_reference_gap_panel")
    assert '_plus = "+" if res.truncated else ""' in body
    assert "{n_codes:,}{_plus} new code(s) across {n_types}{_plus} code type(s)" in body


# ----------------------------- R1-135: release compare keeps the regressed tasks under its cap ----

def _release_rows(n_tasks: int, regressed: str) -> list[tuple]:
    """TASK_HISTORY rows: every task runs once before and once after 2026-09-15; ``regressed`` fails
    three times after the release."""
    rows = []
    for i in range(n_tasks):
        db = "A_DB" if i < n_tasks - 50 else "ZZ_DB"
        name = f"T{i:04d}"
        rows.append((db, "S", name, "2026-09-10 01:00:00", "2026-09-10 01:01:40", "SUCCEEDED",
                     "2026-09-10 01:00:00"))
        rows.append((db, "S", name, "2026-09-18 01:00:00", "2026-09-18 01:01:40", "SUCCEEDED",
                     "2026-09-18 01:00:00"))
        if name == regressed:
            rows.extend((db, "S", name, f"2026-09-1{d} 02:00:00", f"2026-09-1{d} 02:01:00", "FAILED",
                         f"2026-09-1{d} 02:00:00") for d in (6, 7, 9))
    return rows


def test_release_task_compare_keeps_a_late_named_regression():
    import sqlite3

    import sqlglot

    from app.data import insights_sql
    from app.logic.insights import task_release_deltas
    sql = insights_sql.release_task_compare("2026-09-15", 7)
    sqlglot.parse(sql, dialect="snowflake")
    assert "COUNT(DISTINCT s.DATABASE_NAME || '.' || s.SCHEMA_NAME || '.' || s.TASK_NAME) OVER () AS TOTAL_TASKS" in sql
    assert f"<= {insights_sql.RELEASE_MAX_TASKS}" in sql and "LIMIT 1000" not in sql
    lite = sqlglot.transpile(sql, read="snowflake", write="sqlite")[0]
    # sqlite has no DISTINCT window aggregate; the total is locked on the Snowflake text above
    lite = lite.replace("COUNT(DISTINCT s.DATABASE_NAME || '.' || s.SCHEMA_NAME || '.' || s.TASK_NAME) OVER ()",
                        "-1").replace("SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY", "TH")
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE TH (DATABASE_NAME, SCHEMA_NAME, NAME, QUERY_START_TIME, COMPLETED_TIME, STATE, "
                "SCHEDULED_TIME)")
    con.executemany("INSERT INTO TH VALUES (?,?,?,?,?,?,?)", _release_rows(600, "T0590"))
    try:
        cur = con.execute(lite)
    except sqlite3.OperationalError as exc:                          # dialect gap -> the SQL locks above
        pytest.skip(f"sqlite cannot run the transpiled builder: {exc}")
    df = pd.DataFrame(cur.fetchall(), columns=[d[0] for d in cur.description])
    tasks = df.groupby(["DATABASE_NAME", "SCHEMA_NAME", "TASK_NAME"])["PERIOD"].nunique()
    assert len(tasks) == insights_sql.RELEASE_MAX_TASKS and (tasks == 2).all()   # whole tasks only
    worse = task_release_deltas(df)
    assert worse.loc[worse["GOT_WORSE"], "TASK_NAME"].tolist() == ["T0590"]


def test_release_compare_discloses_a_capped_task_set():
    body = _fn(read(_OPS), "_release_compare_tab") if "def _release_compare_tab" in read(_OPS) else read(_OPS)
    assert 'if "TOTAL_TASKS" in t_res.df.columns else len(deltas))' in body
    assert "most-regressed of {_total_tasks:,} tasks" in body
