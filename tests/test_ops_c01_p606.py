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
    # the line is byte-identical to c02's R1-143 fix of the same site, so the two branches merge clean
    assert "served_days(_prof, (bounds[1] - bounds[0]).days if bounds is not None else days))" in body
    assert "served_days(_prof, days)" not in body


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


def test_recon_recurrence_scan_totals_each_tier_before_the_limit():
    import sqlglot

    from app.data import etl_control_sql as etl
    rec = etl.recon_recurrence_scan(_RECON_SETTINGS["ETL_RECON_ERROR_FQN"])
    chronic, new = etl.recon_tier_predicates("r")
    assert f"SUM(CASE WHEN {chronic} THEN 1 ELSE 0 END) OVER () AS CHRONIC_CHECKS_TOTAL" in rec
    assert f"SUM(CASE WHEN {new} THEN 1 ELSE 0 END) OVER () AS NEW_CHECKS_TOTAL" in rec
    assert rec.index("CHRONIC_CHECKS_TOTAL") < rec.index("NEW_CHECKS_TOTAL") < rec.index(") r\n") \
        < rec.index("LIMIT 300")
    sqlglot.parse(rec, dialect="snowflake")


def test_recon_tier_predicates_match_the_classifier_row_by_row():
    # the SQL twin must tier every row exactly as app.logic.insights.recon_recurrence does (its
    # thresholds, its fillna(0), its CHRONIC-before-NEW ladder), or the banner and table disagree
    import itertools
    import sqlite3

    from app.data import etl_control_sql as etl
    from app.logic.insights import recon_recurrence
    grid = list(itertools.product([True, False], [None, 0, 59, 60, 100], [None, 1, 2, 3, 4],
                                  [None, 1, 2, 3, 10], [None, 0, 1, 2, 5]))
    df = pd.DataFrame([{"MTRC": f"M{i}", "BROKE_LATEST_CYCLE": latest, "RECURRENCE_PCT": pct,
                        "BROKEN_CYCLES": broken, "TOTAL_ERROR_CYCLES": total, "RECENT_BROKEN": recent}
                       for i, (latest, pct, broken, total, recent) in enumerate(grid)])
    tiered = recon_recurrence(df)
    want = dict(zip(tiered["MTRC"], tiered["TIER"], strict=True))
    chronic, new = etl.recon_tier_predicates("r")
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t (MTRC TEXT, BROKE_LATEST_CYCLE INT, RECURRENCE_PCT REAL, BROKEN_CYCLES INT,"
                " TOTAL_ERROR_CYCLES INT, RECENT_BROKEN INT)")
    con.executemany("INSERT INTO t VALUES (?,?,?,?,?,?)", [(f"M{i}", int(g[0]), *g[1:]) for i, g in enumerate(grid)])
    got = dict(con.execute(f"SELECT MTRC, CASE WHEN {chronic} THEN 'CHRONIC' WHEN {new} THEN 'NEW' ELSE '' END "
                           "FROM t r").fetchall())
    assert con.execute(f"SELECT COUNT(*) FROM t r WHERE ({chronic}) AND ({new})").fetchone() == (0,)
    mismatched = {m: (want[m], got[m]) for m in want
                  if (want[m] if want[m] in ("CHRONIC", "NEW") else "") != got[m]}
    assert not mismatched
    assert {"CHRONIC", "NEW"} <= set(want.values())          # the grid exercises both tiers


def test_recon_recurrence_banner_counts_tiers_past_the_cap(monkeypatch):
    # R1-137 review: 450 checks break in the latest cycle, the cap keeps the 300 highest-recurrence
    # (200 chronic + 100 intermittent) and evicts the 150 NEW ones -- counted from the frame the
    # banner read "(200 chronic, 0 newly-breaking)" under "the count above covers all of them".
    n = 300
    chronic_row = {"BROKEN_CYCLES": 40, "TOTAL_ERROR_CYCLES": 50, "RECURRENCE_PCT": 80, "RECENT_BROKEN": 5}
    flapping_row = {"BROKEN_CYCLES": 10, "TOTAL_ERROR_CYCLES": 50, "RECURRENCE_PCT": 20, "RECENT_BROKEN": 2}
    rows = [{"MTRC": f"M{i}", "FRQCY": "DAILY", "VALUE_TYPE": "V", "RECON_MTRC_LAYER": "L",
             **(chronic_row if i < 200 else flapping_row), "RECENT_WINDOW": 5, "BROKE_LATEST_CYCLE": True,
             "ERROR_ROWS": 10, "TOTAL_CHECKS": 450, "ACTIVE_CHECKS_TOTAL": 450,
             "CHRONIC_CHECKS_TOTAL": 200, "NEW_CHECKS_TOTAL": 150} for i in range(n)]
    ops, fake, seen = _page(monkeypatch, {"etl_recon_recurrence_0": _ok(pd.DataFrame(rows))},
                            load_settings=lambda *_a, **_k: _RECON_SETTINGS, guard=lambda *_a, **_k: True)
    ops._recon_recurrence_panel(0, pf=None)
    assert "450 metric(s) still breaking as of the latest cycle (200 chronic, 150 newly-breaking)" \
        in fake.text("error"), fake.text("error")
    cap = fake.text("caption")
    assert "300 of 450 checks" in cap and "the 300 highest-recurrence of 450" in cap, cap
    assert "every still-breaking check first" not in cap
    ((shown,),) = [(t,) for t in seen["tables"]]
    assert not {"CHRONIC_CHECKS_TOTAL", "NEW_CHECKS_TOTAL"} & set(shown.columns)


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


# ------------------------------------------------ R1-139: the ETL Window label on a calendar offset ----

def _date_anchored_clause(real):
    """The calendar-aware ``_window_clause`` shape (R1-063, cluster c10): a calendar offset anchors on
    the account DATE -- the period's first day at midnight -- and its 0 reads today only."""
    from app.data.common import account_today_sql

    def clause(days, col="TASK_START_DTTM", indent="  "):
        if getattr(days, "calendar_window", False) and int(days) >= 0:
            return f"{indent}AND {col} >= DATEADD('day', -{int(days)}, {account_today_sql()})\n"
        return real(days, col, indent)
    return clause


@pytest.mark.parametrize(("days", "today", "want"), [
    (CalendarDayOffset(0), date(2026, 9, 1), " (today)"),         # Current month on the 1st: was no label
    (CalendarDayOffset(9), date(2026, 9, 10), " (since Sep 1)"),  # Current month on Sep 10: was "(last 9d)"
    (7, date(2026, 9, 10), " (last 7d)"),
    (0, date(2026, 9, 10), ""),
])
def test_etl_window_suffix_names_a_calendar_period(monkeypatch, days, today, want):
    from app.data import etl_control_sql
    from app.ui.pages import operations as ops
    monkeypatch.setattr(ops, "account_today", lambda: today)
    monkeypatch.setattr(etl_control_sql, "_window_clause", _date_anchored_clause(etl_control_sql._window_clause))
    assert ops._etl_window_suffix(days) == want
    # the integration merge must keep THIS line (c10's review commit rewrites the same base line to
    # etl_control_sql.window_suffix, which calls a since-the-1st read 'last Nd')
    assert "_scope = _etl_window_suffix(days)" in _fn(read(_OPS), "_workflow_runtimes_panel")


@pytest.mark.parametrize("days", [CalendarDayOffset(0), CalendarDayOffset(9), 7, 0, -3, None])
def test_etl_window_suffix_never_names_a_window_the_read_skipped(monkeypatch, days):
    # R1-139 review: the label is read off the clause the readers really emit, so it is truthful with
    # or without the calendar-aware _window_clause -- an all-time read (an unscoped clause, which is
    # what a calendar day 0 got before R1-063) never shows ' (today)'.
    from app.data import etl_control_sql
    from app.data.common import account_today_sql
    from app.ui.pages import operations as ops
    monkeypatch.setattr(ops, "account_today", lambda: date(2026, 9, 10))
    clause, label = etl_control_sql._window_clause(days), ops._etl_window_suffix(days)
    if not clause:
        assert label == ""
    elif account_today_sql() in clause:
        assert label.startswith((" (today)", " (since "))
    else:
        assert label == f" (last {int(days)}d)"
    # a now-anchored read of a calendar offset (no R1-063 anchor) says 'last Nd', never 'since'
    monkeypatch.setattr(etl_control_sql, "_window_clause",
                        lambda d, *_a, **_k: "" if not d else f"  AND X >= DATEADD('day', -{int(d)}, "
                                                              "CURRENT_TIMESTAMP())\n")
    assert ops._etl_window_suffix(CalendarDayOffset(0)) == ""
    assert ops._etl_window_suffix(CalendarDayOffset(9)) == " (last 9d)"


# ------------------------------- R1-073 / R1-131: a failed concurrency read is not "nobody queueing" ----

def _opener(monkeypatch, peaks):
    from datetime import timedelta
    days = [date(2026, 9, 1) + timedelta(days=i) for i in range(30)]
    res = _ok(pd.DataFrame({"DAY": days * 2, "WAREHOUSE_NAME": ["WH_A"] * 30 + ["WH_B"] * 30,
                            "CREDITS_TOTAL": [10.0] * 60}))
    headers: list = []

    def header(title, health="", *_a, **_k):
        if title.startswith("Warehouse spend"):
            raise _Stop
        headers.append((title, health))

    ops, fake, seen = _page(monkeypatch, {}, run_batch_mixed=lambda *_a, **_k: {"res": res, "peaks": peaks},
                            load_settings=lambda *_a, **_k: {}, section_header=header)
    with pytest.raises(_Stop):
        ops._wh_activity_anomalies("ALL", 3.0)
    return headers, seen, fake


def test_wh_opener_failed_peaks_is_not_a_green_all_clear(monkeypatch):
    headers, seen, _fake = _opener(monkeypatch, _failed("timeout"))
    assert headers == [("Warehouses that need attention now", "")]            # neutral, never green
    ((kpis,),) = [(k,) for k in seen["kpis"]]
    assert {k["label"]: k["value"] for k in kpis}["Queueing"] == "—"          # no fabricated 0
    assert [k for k, _m in seen["empty"]] == ["no_data_yet"]
    assert "queueing could not be checked" in seen["empty"][0][1]


def test_wh_opener_ok_peaks_still_verifies_clean(monkeypatch):
    peaks = _ok(pd.DataFrame(columns=["WAREHOUSE_NAME", "PEAK_QUEUED"]))
    headers, seen, _fake = _opener(monkeypatch, peaks)
    assert headers == [("Warehouses that need attention now", "ok")]
    assert [k for k, _m in seen["empty"]] == ["clean"]


# ---------------------------- R1-127: Last month's zero never short-circuits the trailing 7-day scan ----

@pytest.mark.parametrize("from_mart", [True, False])
@pytest.mark.parametrize(("bounds", "days", "holds_last_7"), [
    ((date(2026, 8, 1), date(2026, 9, 1)), 31, False),     # Last month: August does not hold Sep 23-30
    ((date(2026, 9, 1), date(2026, 10, 1)), 29, True),     # Current month on Sep 30 holds the last 7 days
    (None, 30, True),                                      # a trailing window ends now
])
def test_failure_timeline_short_circuit_needs_a_window_holding_the_last_7_days(monkeypatch, bounds, days,
                                                                               holds_last_7, from_mart):
    # v4.608 R2-008 / R2-113 extended this lock: the count must also be LIVE. FACT_TASK_DAILY loads once a
    # day (~06:45 CT), so a mart zero never short-circuits the scan, whatever the window.
    short_circuit = holds_last_7 and not from_mart
    seen_kw: dict = {}

    def timeline(*_a, **kw):
        seen_kw.update(kw)
        raise _Stop

    df = pd.DataFrame({"DATABASE_NAME": ["DB"], "SCHEMA_NAME": ["S"], "TASK_NAME": ["T"], "RUNS": [100],
                       "FAILED": [0], "TOTAL_RUNS_WIN": [100], "TOTAL_FAILED_WIN": [0]})
    ops, _fake, _seen = _page(monkeypatch, {}, guard=lambda *_a, **_k: True,
                              _failure_timeline_section=timeline)

    def _run(*_a, key: str = "", **_k):
        if key.startswith("t_fact_") and not from_mart:
            return _ok(pd.DataFrame())                       # an empty mart -> the live task_runs fallback
        return _ok(df.copy())

    monkeypatch.setattr(ops, "run", _run)
    monkeypatch.setattr(ops, "account_today", lambda: date(2026, 9, 30))
    import app.logic.date_windows as dw
    monkeypatch.setattr(dw, "account_today", lambda: date(2026, 9, 30))
    with pytest.raises(_Stop):
        ops._task_health_view("ALL", days, bounds=bounds)
    assert (seen_kw["known_failures"] == 0) is short_circuit, seen_kw
    assert seen_kw["known_from_live"] is (not from_mart)


# ------------------------------ R1-128 / R1-129: Tasks ▸ SLA failed reads and capped all-clears ----

def _sla_view(monkeypatch, streak, fresh):
    ops, fake, seen = _page(monkeypatch, {}, run_batch=lambda *_a, **_k: {"streak": streak, "fresh": fresh},
                            stash_section_count=lambda *a, **_k: seen_badge.append(a[2]))
    seen_badge: list = []
    ops._task_sla_view("ALL", 14)
    return fake, seen, seen_badge


@pytest.mark.parametrize("kind", _SETUP + _FAILED)
def test_task_sla_failed_reads_render_by_kind(monkeypatch, kind):
    fake, seen, badge = _sla_view(monkeypatch, _failed(kind), _failed(kind))
    states = [k for k, _m in seen["empty"]]
    assert states == (["needs_setup"] * 2 if kind in _SETUP else ["unavailable"] * 2)
    if kind not in _SETUP:
        assert seen["detail"] == [f"boom ({kind})"] * 2
    text = fake.text("caption") + " ".join(m for _k, m in seen["empty"])
    assert "No SUCCEEDED/FAILED task runs" not in text and "Not enough scheduled history" not in text
    assert badge == []                                   # no "0" badge from a failed read


def _fresh_frame(n: int, last_mins: float, total: int) -> pd.DataFrame:
    """n nightly tasks (yard 1440 min), all on time but the last read at ``last_mins`` silence."""
    mins = [600.0] * (n - 1) + [last_mins]
    return pd.DataFrame({"DATABASE_NAME": ["DB"] * n, "SCHEMA_NAME": ["S"] * n,
                         "TASK_NAME": [f"T{i}" for i in range(n)], "MEDIAN_GAP_MIN": [1440.0] * n,
                         "LONG_GAP_MIN": [1440.0] * n, "INTERVALS": [10] * n, "MINS_SINCE_SUCCESS": mins,
                         "LAST_SUCCESS": ["2026-09-29"] * n, "TOTAL_TASKS": [total] * n})


def test_task_freshness_capped_read_is_not_green_unless_the_cut_is_proven(monkeypatch):
    streak = _ok(pd.DataFrame({"DATABASE_NAME": ["DB"], "SCHEMA_NAME": ["S"], "TASK_NAME": ["T"],
                               "SCHEDULED_TIME": ["2026-09-29"], "STATE": ["SUCCEEDED"], "ERROR_MESSAGE": [""]}))
    # 200 of 251 read; the last one read is past its yard (inside the lag, so not Late) -> unproven
    _fake, seen, badge = _sla_view(monkeypatch, streak, _ok(_fresh_frame(200, 1460.0, 251)))
    assert [k for k, _m in seen["empty"]] == ["clean", "no_data_yet"]       # streaks clean, freshness not
    assert "200 most-overdue tasks read (of 251)" in seen["empty"][1][1]
    assert badge == []
    # the last one read is inside its yard -> every task below the cut is on time too -> verified clean
    _fake, seen, badge = _sla_view(monkeypatch, streak, _ok(_fresh_frame(200, 900.0, 251)))
    assert [k for k, _m in seen["empty"]] == ["clean", "clean"] and badge == [0]


def test_task_freshness_ranks_by_silence_relative_to_the_task_yard():
    from app.data import ops_sql
    sql = ops_sql.task_freshness_sla(14, "ALL")
    assert ("ORDER BY DATEDIFF('minute', l.LAST_SUCCESS, CURRENT_TIMESTAMP())\n"
            "             / NULLIF(GREATEST(COALESCE(c.LONG_GAP_MIN, 0), c.MEDIAN_GAP_MIN), 0) DESC NULLS FIRST") in sql
    assert "COUNT(*) OVER () AS TOTAL_TASKS" in sql and sql.rstrip().endswith("LIMIT 200")


def test_task_recent_states_keeps_a_late_named_streak_under_the_cap():
    # 400 healthy tasks x 12 runs used to fill the 4,000-row cap alphabetically and drop T399's streak
    import sqlite3

    import sqlglot

    from app.data import ops_sql
    from app.logic.insights import task_failure_streaks
    lite = sqlglot.transpile(ops_sql.task_recent_states(7), read="snowflake", write="sqlite")[0]
    lite = (lite.replace("SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY", "TH")
                .replace("CURRENT_DATE", "'2026-09-30'")) + "\nLIMIT 4001"     # run()'s row-cap probe
    con = sqlite3.connect(":memory:")
    con.create_function("LEFT", 2, lambda s, n: None if s is None else str(s)[:n])
    con.execute("CREATE TABLE TH (DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME, COMPLETED_TIME, STATE, "
                "ERROR_MESSAGE)")
    rows = []
    for t in range(400):
        for r in range(12):
            ts = f"2026-09-29 {r:02d}:00:00"
            state = "FAILED" if (t == 399 and r >= 7) else "SUCCEEDED"
            rows.append(("DB", "S", f"T{t:03d}", ts, ts, state, "boom" if state == "FAILED" else None))
    con.executemany("INSERT INTO TH VALUES (?,?,?,?,?,?,?)", rows)
    try:
        cur = con.execute(lite)
    except sqlite3.OperationalError as exc:                          # dialect gap -> the SQL lock
        pytest.skip(f"sqlite cannot run the transpiled builder: {exc}")
    df = pd.DataFrame(cur.fetchall(), columns=[d[0] for d in cur.description])
    assert len(df) <= 4000 and df.iloc[0]["TASK_NAME"] == "T399"                # failing tasks lead
    got = task_failure_streaks(df)
    assert got["TASK_NAME"].tolist() == ["T399"] and int(got.iloc[0]["FAIL_STREAK"]) == 5


# --------------------------------- R1-134: the cancel-query confirm and latch scope by the full id ----

def test_cancel_query_gate_is_scoped_to_the_selected_query(monkeypatch):
    # two queries from one burst share their first 8 characters; switching rows must not keep a typed
    # CANCEL armed (fixed confirm key) or let the latch swallow the second cancel (qid[:8] latch key)
    qids = ["01bf1a2b-0000-aaaa-0000-000000008a29", "01bf1a2b-0000-aaaa-0000-000000008b31"]
    whs = _ok(pd.DataFrame({"name": ["WH_A"]}))
    rq = _ok(pd.DataFrame({"QUERY_ID": qids, "USER_NAME": ["U1", "U2"]}))
    picked = {"row": 0}
    gates: list = []

    class _St(_RecSt):
        def selectbox(self, *_a, **_k):
            return "WH_A"

    ops, _fake, _seen = _page(monkeypatch, {"emg_show_wh": whs, "emg_running_WH_A": rq},
                              snowsight_profile_column=lambda df, *_a, **_k: (df, None),
                              with_user_names=lambda df, *_a, **_k: df,
                              selectable_table=lambda *_a, **_k: picked["row"],
                              confirm_gate=lambda verb, label, *, key, **_k: gates.append(("confirm", key)),
                              write_gate_open=lambda key, **_k: gates.append(("latch", key)))
    monkeypatch.setattr(ops, "st", _St())
    for row in (0, 1):
        picked["row"] = row
        ops._emergency_extras(True)
    confirm_keys = [k for kind, k in gates if kind == "confirm"]
    assert confirm_keys == [f"emg_rq_{q}" for q in qids]               # one confirm per query, never shared


# ------------------------------------------- R1-124: change-scan verdict captions humanize durations ----

def test_verdict_detail_durations_render_in_hr_min_sec():
    # v4.606 holistic review: the helper moved to the pure app.logic.wh_change so Control Room's
    # ranked-cause Magnitude (rca.candidates_from_changes) shares it -- tests/test_rca.py locks that side
    from app.logic.wh_change import humanize_verdict_detail
    wh = ("credits/day 10.5->12.25 | p95 1800.0s->2400.0s | queue 145.00->200.00 min/d "
          "| fail 0->1.5% | 120->140 queries")
    got = humanize_verdict_detail(wh)
    assert "1800.0s" not in got and "2400.0s" not in got and "min/d" not in got
    assert got == ("credits/day 10.5->12.25 | p95 30m → 40m | queue 2h 25m → 3h 20m/day "
                   "| fail 0->1.5% | 120->140 queries")                  # non-durations untouched
    obj = "runs 10->12 | fails 0->1 | p95 ?s->95.5s | credits/call 0.0012->0.0019"
    assert humanize_verdict_detail(obj) == ("runs 10->12 | fails 0->1 | p95 ? → 1m 36s "
                                            "| credits/call 0.0012->0.0019")
    body = read(_OPS)
    assert body.count("wh_change.humanize_verdict_detail(_verdict_detail)") == 1
    assert body.count("wh_change.humanize_verdict_detail(_vd)") == 1
    assert "def _humanize_verdict_detail" not in body and "_VD_P95_RE" not in body   # no private twin


def test_workbench_recent_changes_detail_renders_hr_min_sec(monkeypatch):
    """v4.606 holistic-review follow-up: Workbench Entity 360's Recent changes table renders
    workbench_sql.entity_recent_changes, whose DETAIL is the same VERDICT_DETAIL string (raw seconds,
    'p95 1800.0s->2400.0s'). styled_table only humanizes NUMERIC duration columns, so the text column
    still read '1800.0s' while Operations and Control Room read '30m'. Driven through the real
    render_entity_360 with the tests/test_workbench_failed_reads.py recording fakes."""
    from tests.test_workbench_failed_reads import _patch_page

    detail = "credits/day 12.34->15.67 | p95 1800.0s->2400.0s | queue 145.00->200.00 min/d"
    df = pd.DataFrame([
        {"CHANGED_AT": "2026-09-20 08:00", "CHANGE": "WAREHOUSE_SIZE: MEDIUM -> LARGE",
         "CHANGED_BY": "JOE", "VERDICT": "REGRESSED", "DETAIL": detail},
        {"CHANGED_AT": "2026-09-21 08:00", "CHANGE": "AUTO_SUSPEND: 600 -> 60",
         "CHANGED_BY": "JOE", "VERDICT": "PENDING", "DETAIL": None},     # no verdict text yet
    ])
    wb, _fake, seen = _patch_page(monkeypatch, {"entity_changes_WAREHOUSE_WH_A": _ok(df)},
                                  entity_key="WH_A")
    wb.render_entity_360("ALL")
    (tbl,) = [t for t in seen["tables"] if "DETAIL" in t.columns]
    assert tbl["DETAIL"].iloc[0] == "credits/day 12.34->15.67 | p95 30m → 40m | queue 2h 25m → 3h 20m/day"
    assert "1800.0s" not in tbl["DETAIL"].iloc[0] and "min/d" not in tbl["DETAIL"].iloc[0]
    assert tbl["DETAIL"].iloc[1] is None                                  # a NULL detail stays NULL
    assert list(tbl.columns) == list(df.columns)                          # same table, same columns
    assert df["DETAIL"].iloc[0] == detail                                 # the cached frame is untouched
