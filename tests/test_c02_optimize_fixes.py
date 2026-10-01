"""PR-1 bug fixes, cluster c02 (Cost ▸ Optimization & Savings + insights_sql builders).

Each fix test fails on the pre-fix code (04fd374e) and passes after; the few "still" / "always" tests guard the
paths the fixes must leave unchanged:

  R1-017  a FAILED verified-wins read renders 'unavailable', never "No verified savings yet";
  R1-037  proc_cost_trend's attribution read spans the calendar bounds (Last month lost Aug 1-28);
  R1-042  a calendar-bounded LIVE idle/sizing read states the span it scanned (COVERED_DAYS), so the
          live fallback's run-rates divide Current year by 273 days, not 90 (R1-143 is the same defect);
  R1-043  task_failure_details carries its pre-LIMIT window total and keeps every task's FIRST failure
          ahead of newer repeats; Operations' "Failures (7d)" reads the total;
  R1-044  table_tco matches the quote-stripped upper-case objectName and never raises on a name;
  R1-147  the TCO drill shows "—", not a fabricated 0, when its evidence read fails;
  R1-052  PERF_FINGERPRINT_DRIFT evidence is matched by the family hash in the alert DETAIL;
  R1-054  dormant_users has no 9999 sentinel; a never-logged-in account stays High;
  R1-086  the off-hours schedule is review-only (the executor refuses its script) with a one-click booking;
  R1-113  consolidation pairs only warehouses of the SAME company (the ALL scope paired ALFA with Trexis);
  R1-142  the repeat-query scan normalizes a calendar preset by its span (R1-038 is the same defect);
  R1-144  the repeat-query tiles are window totals, not sums of the LIMIT-100 frame (R1-050/R1-072);
  R1-148  the storage-growth tile names the window the SQL serves (R1-048) and its totals are uncapped.

AppTests reuse the shaped page harness (tests/test_pages_shaped.py); nothing opens a Snowflake session."""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta

import pandas as pd
import pytest
import sqlglot
from test_pages_shaped import (  # noqa: F401 - _stub_shaped is the harness's autouse fixture
    _APPTEST_BUTTONGROUP_OK,
    _entry,
    _nav_to,
    _shaped_mart_first,
    _shaped_run,
    _stub_shaped,
)

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from app.core.result import QueryResult
from app.data import alert_evidence_sql, insights_sql
from app.logic.alert_evidence import plan_for_alert
from app.logic.date_windows import CalendarDayOffset
from app.logic.insights import dormant_severity, flag_repeat_candidates, idle_waste_summary
from app.ui import components as _components

# the REAL run_mart_first, captured before the shaped harness's autouse fixture swaps the page read stubs in
_RUN_MART_FIRST = _components.run_mart_first

_SKIP = pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")
_CY = (date(2026, 1, 1), date(2026, 10, 1))       # Current year on 2026-09-30: offset 272, span 273
_LM = (date(2026, 8, 1), date(2026, 9, 1))        # Last month on 2026-09-30


def _ok(df: pd.DataFrame) -> QueryResult:
    return QueryResult(df=df, ok=True, source="t")


def _failed(kind: str = "timeout") -> QueryResult:
    return QueryResult(df=pd.DataFrame(), ok=False, source="t", error_kind=kind,
                       error="Statement reached its statement or warehouse timeout of 180 second(s).")


def _texts(at) -> str:
    return " ".join(str(e.value) for e in list(at.markdown) + list(at.caption) + list(at.warning)
                    + list(at.info) + list(at.error))


def _cost_page(monkeypatch, section: str, *, run_hook=None, mart_hook=None, operator: bool = False,
               state: dict | None = None, executed: list | None = None) -> AppTest:
    """Cost ▸ Optimization & Savings ▸ ``section`` on shaped data. ``run_hook(sql, kwargs)`` /
    ``mart_hook(mart, live, kwargs)`` return a QueryResult to override one read, else None (shaped)."""
    import app.ui.pages.cost as cost
    import app.ui.pages.cost_parts.optimize as opt

    def _run(*args, **kwargs):
        sql = str(args[0] if args else kwargs.get("sql", ""))
        got = run_hook(sql, kwargs) if run_hook else None
        return got if got is not None else _shaped_run(*args, **kwargs)

    def _mart_first(mart, live="", **kwargs):
        got = mart_hook(str(mart), str(live), kwargs) if mart_hook else None
        return got if got is not None else _shaped_mart_first(mart, live, **kwargs)

    def _execute(sql, **_kwargs):
        if executed is not None:
            executed.append(str(sql))
        return True, "stubbed"

    monkeypatch.setattr(opt, "run", _run)
    monkeypatch.setattr(opt, "run_mart_first", _mart_first)
    monkeypatch.setattr(opt, "execute_statement", _execute)
    if operator:
        monkeypatch.setattr(cost, "_is_operator", lambda: True)
    at = AppTest.from_function(_entry, default_timeout=60)
    at.run()
    assert not at.exception
    _nav_to(at, "Cost Intelligence")
    at.session_state["cost_section"] = "Optimization & Savings"
    at.session_state["opt_section"] = section
    for k, v in (state or {}).items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, f"cost optimize {section} (shaped): {at.exception}"
    assert not any("could not finish rendering" in str(getattr(e, "value", "")) for e in at.error)
    return at


# ---- R1-017: a failed verified-wins read is 'unavailable', never "nothing verified" ----------------------

@_SKIP
@pytest.mark.parametrize("kind", ["timeout", "missing_column"])
def test_failed_verified_wins_read_is_unavailable_not_none_yet(monkeypatch, kind):
    def hook(_sql, kw):
        return _failed(kind) if str(kw.get("key", "")).startswith("opt_verified_wins") else None

    at = _cost_page(monkeypatch, "Idle & sizing", run_hook=hook)
    text = _texts(at)
    assert "No verified savings yet" not in text
    assert "Verified wins (SAVINGS_LEDGER x WAREHOUSE_CHANGE_REGISTRY) could not be read" in " ".join(
        str(e.value) for e in at.error)


@_SKIP
def test_empty_verified_wins_read_still_says_none_yet(monkeypatch):
    def hook(_sql, kw):
        return _ok(pd.DataFrame()) if str(kw.get("key", "")).startswith("opt_verified_wins") else None

    at = _cost_page(monkeypatch, "Idle & sizing", run_hook=hook)
    assert "No verified savings yet" in " ".join(str(c.value) for c in at.caption)


# ---- R1-037: the trend's attribution read honours the calendar bounds -----------------------------------

def _att_cte(sql: str) -> str:
    return sql.split("att AS (", 1)[1].split("\n)\nSELECT", 1)[0]


def test_proc_cost_trend_attribution_spans_the_bounded_month():
    att = _att_cte(insights_sql.proc_cost_trend("SP_DEMO", 31, bounds=_LM))
    assert "START_TIME >= '2026-08-01' AND START_TIME < '2026-09-01'" in att
    assert "DATEADD('day', -32, CURRENT_TIMESTAMP())" not in att
    # Current year: the whole bounded range, not the clamped 90(+1) days
    att_cy = _att_cte(insights_sql.proc_cost_trend("SP_DEMO", CalendarDayOffset(272), bounds=_CY))
    assert "START_TIME >= '2026-01-01' AND START_TIME < '2026-10-01'" in att_cy
    # ... the SAME predicate the $/call leaderboard's attribution read uses for these bounds
    assert "START_TIME >= '2026-08-01' AND START_TIME < '2026-09-01'" in insights_sql.procedure_costs_usd(
        31, bounds=_LM)
    # trailing is unchanged (the -90 clamp lock in test_proc_trend still holds)
    assert "START_TIME >= DATEADD('day', -31, CURRENT_TIMESTAMP())" in _att_cte(
        insights_sql.proc_cost_trend("SP_DEMO", 30))


def test_proc_cost_trend_bounded_drill_credits_every_call_in_sqlite():
    """The rendered Last-month SQL on sqlite (syntax shimmed only): 3 calls on Aug 5 / 15 / 30, each child
    attribution 1 credit. Pre-fix the att read began ~Aug 29, so the drill summed 1 credit, not 3."""
    sql = insights_sql.proc_cost_trend("SP_DEMO", 31, bounds=_LM)
    sql = (sql.replace("SNOWFLAKE.ACCOUNT_USAGE.", "")
              .replace("REGEXP_SUBSTR(UPPER(c.QUERY_TEXT), 'CALL[[:space:]]+([A-Z0-9_.$]+)', 1, 1, 'e', 1)",
                       "c.PROC")
              .replace("COUNT_IF(n.EXECUTION_STATUS <> 'SUCCESS')",
                       "SUM(CASE WHEN n.EXECUTION_STATUS <> 'SUCCESS' THEN 1 ELSE 0 END)")
              .replace("CURRENT_TIMESTAMP()", "'2026-09-30 10:00:00'"))
    sql = re.sub(r"DATEADD\('day', (-?\d+), '2026-09-30 10:00:00'\)",
                 lambda m: f"datetime('2026-09-30 10:00:00', '{m.group(1)} days')", sql)
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE QUERY_HISTORY (QUERY_ID TEXT, START_TIME TEXT, EXECUTION_STATUS TEXT, "
               "QUERY_TYPE TEXT, WAREHOUSE_NAME TEXT, DATABASE_NAME TEXT, SCHEMA_NAME TEXT, USER_NAME TEXT, "
               "QUERY_TEXT TEXT, PROC TEXT)")
    db.execute("CREATE TABLE QUERY_ATTRIBUTION_HISTORY (QUERY_ID TEXT, ROOT_QUERY_ID TEXT, START_TIME TEXT, "
               "CREDITS_ATTRIBUTED_COMPUTE REAL, CREDITS_USED_QUERY_ACCELERATION REAL)")
    for i, day in enumerate(("2026-08-05", "2026-08-15", "2026-08-30")):
        db.execute("INSERT INTO QUERY_HISTORY VALUES (?, ?, 'SUCCESS', 'CALL', 'WH', 'DB', 'S', 'U', "
                   "'CALL SP_DEMO()', 'SP_DEMO')", (f"c{i}", f"{day} 01:00:00"))
        db.execute("INSERT INTO QUERY_ATTRIBUTION_HISTORY VALUES (?, ?, ?, 1.0, 0)",
                   (f"k{i}", f"c{i}", f"{day} 01:05:00"))
    rows = db.execute(sql).fetchall()
    assert [r[0] for r in rows] == ["2026-08-05", "2026-08-15", "2026-08-30"]
    assert sum(r[3] for r in rows) == pytest.approx(3.0)


# ---- R1-042 / R1-143: the live fallback divides by the span it scanned ------------------------------------

def test_bounded_live_idle_and_sizing_state_their_scanned_span():
    for sql in (insights_sql.idle_warehouse_analysis(CalendarDayOffset(272), "ALL", bounds=_CY),
                insights_sql.warehouse_sizing_profile(CalendarDayOffset(272), "ALL", bounds=_CY)):
        assert "273 AS COVERED_DAYS" in sql
        assert "'2026-01-01'" in sql and "'2026-10-01'" in sql
        sqlglot.parse_one(sql, read="snowflake")
    # trailing reads really clamp, so they stay byte-identical (the clamp stamp is right there)
    assert "COVERED_DAYS" not in insights_sql.idle_warehouse_analysis(365, "ALL")
    assert "COVERED_DAYS" not in insights_sql.warehouse_sizing_profile(365, "ALL")


@pytest.mark.parametrize(("bounds", "offset", "span"), [(_CY, 272, 273),
                                                          ((date(2026, 9, 1), date(2026, 9, 3)), 1, 2)])
def test_live_fallback_run_rate_divides_by_the_bounded_span(monkeypatch, bounds, offset, span):
    """run_mart_first with the mart failing and the LIVE builder answering (Snowflake evaluates the builder's
    COVERED_DAYS literal): served_days and the projected monthly idle $ use the span, not clamp_days(offset)."""
    from app.core import query
    from app.ui import components

    live_sql = insights_sql.idle_warehouse_analysis(CalendarDayOffset(offset), "ALL", bounds=bounds)

    def fake_run(sql, **_kw):
        if sql == "MART":
            return _failed()
        df = pd.DataFrame({"WAREHOUSE_NAME": ["WH_A"], "COMPANY": ["ALFA"], "METERED_HOURS": [span * 24.0],
                           "IDLE_HOURS": [span * 12.0], "TOTAL_CREDITS": [span * 24.0],
                           "IDLE_CREDITS": [span * 12.0]})
        m = re.search(r"(\d+) AS COVERED_DAYS", sql)
        if m:
            df["COVERED_DAYS"] = int(m.group(1))
        return _ok(df)

    monkeypatch.setattr(query, "run", fake_run)
    res = _RUN_MART_FIRST("MART", live_sql, page="t", key=f"c02_idle_{span}", mart_source="m",
                          live_source="l", days=CalendarDayOffset(offset))
    assert components.served_days(res, span) == span
    # 12 idle credits/day at $3 -> $1,080 a month, whatever the window
    assert idle_waste_summary(res.df, 3.0, components.served_days(res, span))["PROJECTED_MONTHLY_USD"] == 1080.0


def test_operations_sizing_lens_divides_by_the_bounds_span():
    """The Operations ▸ Sizing sibling of the same W12 class: it divided by the day OFFSET (Current month on the
    2nd: 1 day for a 2-day read), where Cost ▸ Optimize already divides by the span."""
    from tests._source import read
    src = read("app/ui/pages/operations.py")
    assert "served_days(_prof, (bounds[1] - bounds[0]).days if bounds is not None else days)" in src
    assert "served_days(_prof, days))" not in src


# ---- R1-043: the task-failure feed keeps its window total and the onset-era failures ----------------------

def _task_feed_on_sqlite(rows: list[tuple], **kwargs) -> pd.DataFrame:
    """The rendered task_failure_details on sqlite. Syntax shims only: the table prefix, the CURRENT_DATE
    anchor (pinned to 2026-09-30), LEFT -> the plain message, and the onset DATEDIFF -> julianday seconds."""
    sql = insights_sql.task_failure_details(13, "ALL", **kwargs)
    sql = sql.replace("SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY", "TASK_HISTORY")
    sql = sql.replace("LEFT(COALESCE(ERROR_MESSAGE, ''), 300)", "COALESCE(ERROR_MESSAGE, '')")
    sql = re.sub(r"DATEADD\('day', (-?\d+), CURRENT_DATE\(\)\)",
                 lambda m: f"'{date(2026, 9, 30) + timedelta(days=int(m.group(1)))}'", sql)
    sql = re.sub(r"ABS\(DATEDIFF\('second', QUERY_START_TIME::TIMESTAMP_NTZ, '([^']+)'::TIMESTAMP_NTZ\)\)",
                 lambda m: f"ABS((julianday(QUERY_START_TIME) - julianday('{m.group(1)}')) * 86400)", sql)
    sql = sqlglot.transpile(sql, read="snowflake", write="sqlite")[0]
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE TASK_HISTORY (DATABASE_NAME TEXT, SCHEMA_NAME TEXT, NAME TEXT, ROOT_TASK_ID TEXT, "
               "GRAPH_RUN_GROUP_ID TEXT, QUERY_START_TIME TEXT, COMPLETED_TIME TEXT, SCHEDULED_TIME TEXT, "
               "STATE TEXT, ERROR_CODE TEXT, ERROR_MESSAGE TEXT)")
    db.executemany("INSERT INTO TASK_HISTORY VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    cur = db.execute(sql)
    return pd.DataFrame(cur.fetchall(), columns=[d[0].upper() for d in cur.description])


def _failures(task: str, start: datetime, every_min: int, n: int) -> list[tuple]:
    out = []
    for i in range(n):
        t = (start + timedelta(minutes=every_min * i)).strftime("%Y-%m-%d %H:%M:%S")
        out.append(("DB1", "S", task, f"r_{task}", None, t, t, t, "FAILED", "2", "noise"))
    return out


def test_task_failure_feed_keeps_the_total_and_each_tasks_first_failure_in_sqlite():
    """LOADER_X failed once, 30 min before an onset 10 days ago; an unrelated task has failed every 5 minutes
    for the last 3 days (864 rows). Pre-fix, newest-first LIMIT 500 dropped the trigger and the KPI read 500."""
    assert "COUNT(*) OVER () AS TOTAL_FAILURES_WIN" in insights_sql.task_failure_details(7, "ALL")
    rows = [("DB1", "S", "LOADER_X", "r1", None, "2026-09-20 11:30:00", "2026-09-20 11:31:00",
             "2026-09-20 11:30:00", "FAILED", "1", "boom")]
    rows += _failures("NOISY_5MIN", datetime(2026, 9, 27), 5, 864)
    out = _task_feed_on_sqlite(rows)
    assert len(out) == 500
    assert int(out["TOTAL_FAILURES_WIN"].iloc[0]) == 865
    assert "LOADER_X" in set(out["TASK_NAME"])


def test_rca_feed_keeps_the_failures_nearest_the_onset_in_sqlite():
    """The incident RCA passes its onset: a task that failed once 3 days before the onset, then every minute from
    10 minutes before it for 2 days (2,881 rows). Newest-first keeps only post-onset rows (the trigger 10 minutes
    before onset is cut, so the RCA scores the task LOW); onset-anchored keeps the rows around the onset."""
    onset = datetime(2026, 9, 25, 12, 0)
    rows = _failures("LOADER_Y", onset - timedelta(days=3), 1, 1)
    rows += _failures("LOADER_Y", onset - timedelta(minutes=10), 1, 2880)
    trigger = (onset - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    newest = _task_feed_on_sqlite(rows)
    assert trigger not in set(newest["QUERY_START_TIME"])
    near = _task_feed_on_sqlite(rows, onset=pd.Timestamp(onset))
    assert len(near) == 500 and int(near["TOTAL_FAILURES_WIN"].iloc[0]) == 2881
    assert trigger in set(near["QUERY_START_TIME"])
    # an unparseable onset keeps the newest-first order (never a broken statement)
    assert "ABS(DATEDIFF" not in insights_sql.task_failure_details(7, "ALL", onset="not a time")


def test_control_room_rca_passes_the_incident_onset():
    from tests._source import read
    src = read("app/ui/pages/control_room.py")
    assert "insights_sql.task_failure_details(_days, company, onset=onset_dt)" in src


@_SKIP
def test_operations_failures_kpi_reads_the_window_total(monkeypatch):
    import app.ui.pages.operations as ops

    frame = pd.DataFrame({
        "DATABASE_NAME": ["DB1"] * 3, "SCHEMA_NAME": ["S"] * 3, "TASK_NAME": ["T1", "T2", "T3"],
        "ROOT_TASK_ID": ["r1", "r2", "r3"], "GRAPH_RUN_GROUP_ID": [None, None, None],
        "QUERY_START_TIME": pd.to_datetime(["2026-09-29", "2026-09-28", "2026-09-27"]),
        "RUN_SEC": [1.0, 2.0, 3.0], "ERROR_CODE": ["1", "1", "1"], "ERROR_MESSAGE": ["x", "y", "z"],
        "TOTAL_FAILURES_WIN": [8717, 8717, 8717], "REPEAT_FAILURE": [0, 0, 0]})

    def _run(*args, **kwargs):
        if str(kwargs.get("key", "")).startswith("t_rca_"):
            return _ok(frame)
        return _shaped_run(*args, **kwargs)

    monkeypatch.setattr(ops, "run", _run)

    def _entry_failures():
        from app.ui.pages import operations
        operations._failure_timeline_section("ALL")

    at = AppTest.from_function(_entry_failures, default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    text = _texts(at)
    assert "8,717" in text                                            # pre-fix: the KPI read len(frame) = 3
    assert "cover 3 of 8,717 failures" in text


# ---- R1-044 / R1-147: the TCO drill's evidence ------------------------------------------------------------

def test_table_tco_matches_the_quote_stripped_upper_case_name_and_never_raises():
    sql = insights_sql.table_tco("ANALYTICS", "RAW", "MyTable", 30)
    assert sql.count("UPPER(REPLACE(f.value:\"objectName\"::STRING, '\"', '')) = 'ANALYTICS.RAW.MYTABLE'") == 2
    assert "f.value:\"objectName\"::STRING = '" not in sql
    for exotic in ("2023_ARCHIVE", "ORDERS-OLD", "Order Lines", '"Quoted"'):
        got = insights_sql.table_tco("DB", "S", exotic, 30)        # pre-fix: ValueError
        sqlglot.parse_one(got, read="snowflake")
    assert "'DB.S.QUOTED'" in insights_sql.table_tco("DB", "S", '"Quoted"', 30)


def _waste_selected(monkeypatch):
    """Select row 0 of the storage-waste table (AppTest cannot click a grid row)."""
    import app.ui.components as components

    real = components._st_dataframe

    class _Event:
        def __init__(self, rows):
            self.selection = type("S", (), {"rows": rows})()

    def _st_dataframe(data, **kwargs):
        out = real(data, **kwargs)
        if kwargs.get("key") == "waste_sel":
            return _Event([0])
        return out

    monkeypatch.setattr(components, "_st_dataframe", _st_dataframe)


@_SKIP
def test_tco_drill_failed_read_shows_a_dash_not_zero_reads(monkeypatch):
    _waste_selected(monkeypatch)
    seen: list[str] = []

    def hook(sql, kw):
        if str(kw.get("key", "")).startswith("tco_"):
            seen.append(sql)
            return _failed("timeout")
        return None

    at = _cost_page(monkeypatch, "Storage & waste", run_hook=hook, state={"cost_waste_toggle": True})
    assert seen, "the TCO drill did not run (no row selected?)"
    text = _texts(at)
    assert "Object TCO" in text
    assert "No reads in 30d" not in text and "retire-candidate" not in text
    assert "needs ACCESS_HISTORY (Enterprise)" not in text
    assert "reads and writes are unknown here, not zero" in text
    assert "the read timed out" in text
    tiles = " ".join(str(m.value) for m in at.markdown)
    assert re.search(r"Reads \(30d\).{0,400}—", tiles, re.S), "the Reads tile must show the no-value dash"


# ---- R1-052: drift evidence by the family hash ---------------------------------------------------------------

_DRIFT = "Query family p95 12.0s -> 48.0s: SELECT * FROM EDW.STG.LOADS WHERE LOAD_DATE = '2026-09-21'"
_HASH = "cbd58379a88c37ed6cc0ecfebb053b03"


def test_fingerprint_drift_evidence_matches_the_family_hash():
    plan = plan_for_alert("PERF_FINGERPRINT_DRIFT", _DRIFT, f"Hash {_HASH} | runs 140 -> 35", "2026-09-22")
    assert plan is not None and plan.family_hash == _HASH
    sql = alert_evidence_sql.build(plan)
    assert f"QUERY_PARAMETERIZED_HASH = '{_HASH}'" in sql
    assert " LIKE " not in sql                       # the one-literal title sample no longer scopes it
    assert "EXECUTION_STATUS = 'SUCCESS'" in sql      # the raiser's p95 basis
    assert _HASH in plan.scope_note
    # no hash in DETAIL: the title-sample LIKE stays the fallback
    fallback = alert_evidence_sql.build(plan_for_alert("PERF_FINGERPRINT_DRIFT", _DRIFT, "", "2026-09-22"))
    assert "QUERY_PARAMETERIZED_HASH = '" not in fallback and "ESCAPE '~'" in fallback


# ---- R1-054: no fabricated 9999 dormant days ---------------------------------------------------------------

def test_dormant_users_has_no_sentinel_and_never_logged_in_stays_high():
    sql = insights_sql.dormant_users(90, "ALL")
    assert "9999" not in sql
    assert ("DATEDIFF('day', COALESCE(U.LAST_SUCCESS_LOGIN, U.CREATED_ON), CURRENT_TIMESTAMP()) AS DAYS_DORMANT"
            in sql)
    assert "(U.LAST_SUCCESS_LOGIN IS NULL) AS NEVER_LOGGED_IN" in sql
    assert "ORDER BY NEVER_LOGGED_IN DESC, DAYS_DORMANT DESC" in sql
    ranked = dormant_severity(pd.DataFrame({
        "USER_NAME": ["NEWBIE", "OLD", "MID"], "DAYS_DORMANT": [120, 400, 120],
        "NEVER_LOGGED_IN": [True, False, False], "ROLE_COUNT": [1, 1, 1]}))
    sev = dict(zip(ranked["USER_NAME"], ranked["SEVERITY"], strict=True))
    assert sev == {"NEWBIE": "High", "OLD": "High", "MID": "Medium"}
    # an old-shape frame (no NEVER_LOGGED_IN column) still ranks by the number
    old = dormant_severity(pd.DataFrame({"USER_NAME": ["A"], "DAYS_DORMANT": [100], "ROLE_COUNT": [1]}))
    assert list(old["SEVERITY"]) == ["Medium"]


# ---- R1-086: the off-hours schedule is review-only ---------------------------------------------------------

def _quiet_profile() -> pd.DataFrame:
    """WAREHOUSE_NAME_0 (the shaped idle frame's first warehouse) burns ~1 credit/h with no queries 00-05."""
    rows = [{"WAREHOUSE_NAME": "WAREHOUSE_NAME_0", "HOUR_OF_DAY": h,
             "AVG_CREDITS": 1.0 if h < 6 else 0.5, "AVG_QUERIES": 0.0 if h < 6 else 5.0,
             "DAYS_METERED": 10.0} for h in range(24)]
    return pd.DataFrame(rows)


def _schedule_page(monkeypatch, executed: list) -> AppTest:
    def hook(_sql, kw):
        return _ok(_quiet_profile()) if str(kw.get("key", "")).startswith("remed_prof") else None

    return _cost_page(monkeypatch, "Remediation & ledger", run_hook=hook, operator=True, executed=executed,
                      state={"remed_kind": "Off-hours suspend/resume schedule"})


@_SKIP
def test_off_hours_schedule_is_review_only_with_a_one_click_booking(monkeypatch):
    executed: list[str] = []
    at = _schedule_page(monkeypatch, executed)
    code = "\n".join(str(c.value) for c in at.code)
    assert "CREATE OR REPLACE TASK DBA_MAINT_DB.OVERWATCH.OVERWATCH_SUSPEND_WAREHOUSE_NAME_0" in code
    text = _texts(at)
    assert "Review only — OVERWATCH never runs this script" in text
    # no type-to-confirm execute for the schedule (it could only ever be refused and log a FAILED row)
    assert not [t for t in at.text_input if "remed" in str(t.key)]
    books = [b for b in at.button if b.key == "remed_sched_book_btn"]
    assert len(books) == 1
    books[0].click().run()
    assert not at.exception
    assert len(executed) == 1, executed
    booked = executed[0]
    assert booked.startswith("INSERT INTO DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER")
    assert "'SCHEDULE', 'WAREHOUSE_NAME_0' WHERE NOT EXISTS" in booked
    assert "AND FINDING_TYPE = 'SCHEDULE' AND STATE <> 'REJECTED')" in booked
    assert "REMEDIATION_LOG" not in booked
    from app.core.query import _statement_allowed
    assert _statement_allowed(booked) == (True, "")


def test_the_schedule_script_is_one_the_executor_always_refuses():
    from app.core.query import _statement_allowed
    from app.logic.remediation import suspend_schedule
    allowed, why = _statement_allowed(suspend_schedule("WH_X", 22, 6))
    assert not allowed and "multi-statement" in why


# ---- R1-113: consolidation pairs only warehouses of the same company ---------------------------------------

def _consolidation_page(monkeypatch) -> AppTest:
    # WH_T (Trexis, idle $60) would pair best with WH_A1 (ALFA); WH_A2 (ALFA) is its only same-company peer.
    idle = pd.DataFrame({"WAREHOUSE_NAME": ["WH_T", "WH_A1", "WH_A2"], "COMPANY": ["Trexis", "ALFA", "ALFA"],
                         "METERED_HOURS": [300.0, 300.0, 300.0], "IDLE_HOURS": [200.0, 100.0, 150.0],
                         "TOTAL_CREDITS": [300.0, 300.0, 300.0], "IDLE_CREDITS": [200.0, 100.0, 150.0]})
    show = pd.DataFrame({"name": ["WH_T", "WH_A1", "WH_A2"], "size": ["X-Small"] * 3,
                         "auto_suspend": [600, 600, 600]})
    hours = {"WH_T": range(1, 5), "WH_A1": range(9, 18), "WH_A2": range(19, 23)}
    act = pd.DataFrame([{"WAREHOUSE_NAME": w, "HOUR_OF_DAY": h, "AVG_QUERIES": 5.0 if h in hrs else 0.0,
                         "AVG_CREDITS": 1.0, "DAYS_METERED": 10.0}
                        for w, hrs in hours.items() for h in range(24)])

    def hook(sql, kw):
        if sql.startswith("SHOW WAREHOUSES"):
            return _ok(show)
        if str(kw.get("key", "")).startswith("wh_hourly_consol"):
            return _ok(act)
        return None

    def mart_hook(_mart, _live, kw):
        return _ok(idle) if str(kw.get("key", "")).startswith("idle_") else None

    return _cost_page(monkeypatch, "Idle & sizing", run_hook=hook, mart_hook=mart_hook,
                      state={"flt_company": "ALL", "opt_consolidation_toggle": True})


@_SKIP
def test_consolidation_never_pairs_an_alfa_and_a_trexis_warehouse(monkeypatch):
    at = _consolidation_page(monkeypatch)
    tables = [df.value for df in at.dataframe
              if isinstance(df.value, pd.DataFrame) and {"Keep", "Retire"} <= set(df.value.columns)]
    assert tables, "the consolidation table did not render"
    pairs = {frozenset((r["Keep"], r["Retire"])) for _, r in tables[0].iterrows()}
    assert frozenset(("WH_T", "WH_A1")) not in pairs and frozenset(("WH_T", "WH_A2")) not in pairs
    assert pairs == {frozenset(("WH_A1", "WH_A2"))}
    assert set(tables[0]["Company"]) == {"ALFA"}           # the ALL scope names the company


# ---- R1-142 / R1-144: the repeat-query scan ------------------------------------------------------------------

def test_repeat_builder_normalizes_by_the_scanned_span_and_carries_window_totals():
    sql = insights_sql.repeat_query_fingerprints(273, "ALL", 91, bounds=_CY)
    assert "START_TIME >= '2026-01-01' AND START_TIME < '2026-10-01'" in sql
    assert "HAVING COUNT(*) >= 91" in sql
    assert "ROUND(RUNS / 273 * 30, 1) >= 10.0" in sql
    assert "ROUND(TOTAL_ELAPSED_HOURS / 273 * 30, 2) >= 0.5" in sql
    for col in ("FINGERPRINTS_WIN", "ELAPSED_HOURS_WIN", "CANDIDATES_WIN"):
        assert f"OVER () AS {col}" in sql
    tail = sql.rsplit("ORDER BY", 1)[1].strip()
    assert tail.startswith("GATE_PASS DESC") and tail.endswith("LIMIT 100")
    # trailing: the clamped days normalize (90 for a 365 pick)
    assert "ROUND(RUNS / 90 * 30, 1)" in insights_sql.repeat_query_fingerprints(365, "ALL", 30)
    sqlglot.parse_one(sql, read="snowflake")


def test_repeat_gate_in_sql_matches_flag_repeat_candidates():
    """GATE_PASS (SQL, evaluated on sqlite) and CANDIDATE (insights.flag_repeat_candidates) agree on a grid."""
    sql = insights_sql.repeat_query_fingerprints(90, "ALL", 2)
    gate = "IFF(ROUND(" + sql.split("IFF(ROUND(", 1)[1].split(" AS GATE_PASS", 1)[0]
    probe = sqlglot.transpile(f"SELECT ID, {gate} AS G FROM grid", read="snowflake", write="sqlite")[0]
    grid = pd.DataFrame([{"ID": i, "RUNS": float(runs), "TOTAL_ELAPSED_HOURS": hrs, "AVG_CACHE_PCT": cache}
                         for i, (runs, hrs, cache) in enumerate(
                             (r, h, c) for r in (5, 29, 30, 31, 300) for h in (0.5, 1.49, 1.5, 1.51, 9.0)
                             for c in (0.0, 25.0, 25.5, 80.0))])
    db = sqlite3.connect(":memory:")
    grid.to_sql("grid", db, index=False)
    sql_gate = dict(db.execute(probe).fetchall())
    flagged = flag_repeat_candidates(grid, 90)
    for _, r in flagged.iterrows():
        assert bool(sql_gate[int(r["ID"])]) == bool(r["CANDIDATE"]), dict(r)


def _repeat_frame(n: int, span: int) -> pd.DataFrame:
    return pd.DataFrame({
        "FINGERPRINT": [f"h{i}" for i in range(n)], "RUNS": [float(span)] * n, "USERS": [1.0] * n,
        "WAREHOUSES": [1.0] * n, "TOTAL_ELAPSED_HOURS": [span * 0.05] * n, "AVG_ELAPSED_SEC": [180.0] * n,
        "TOTAL_TB_SCANNED": [0.1] * n, "AVG_CACHE_PCT": [0.0] * n, "EST_CREDITS": [span * 0.1] * n,
        "QUERY_PREVIEW": ["SELECT 1"] * n, "LAST_RUN": [pd.Timestamp("2026-09-29")] * n, "GATE_PASS": [1] * n,
        "FINGERPRINTS_WIN": [400] * n, "ELAPSED_HOURS_WIN": [1261.4] * n, "CANDIDATES_WIN": [179] * n})


def _repeat_page(monkeypatch, *, window, today: date, n: int, span: int, seen: list) -> AppTest:
    import app.logic.date_windows as dw

    monkeypatch.setattr(dw, "account_today", lambda: today)

    def hook(sql, kw):
        if str(kw.get("key", "")).startswith("repeatq_"):
            seen.append(sql)
            return _ok(_repeat_frame(n, span))
        return None

    return _cost_page(monkeypatch, "Queries & patterns", run_hook=hook,
                      state={"flt_days": window, "cost_repeatq_toggle": True})


def _repeat_table(at) -> pd.DataFrame:
    for df in at.dataframe:
        v = df.value
        if isinstance(v, pd.DataFrame) and "RUNS_PER_30D" in v.columns:
            return v
    raise AssertionError("the repeat-query table did not render")


@_SKIP
def test_repeat_scan_normalizes_current_year_by_its_span(monkeypatch):
    seen: list[str] = []
    at = _repeat_page(monkeypatch, window="CURRENT_YEAR", today=date(2026, 9, 30), n=3, span=273, seen=seen)
    assert seen and "HAVING COUNT(*) >= 91" in seen[0]               # pre-fix: >= 30 (the clamped 90 days)
    table = _repeat_table(at)
    assert list(table["RUNS_PER_30D"]) == [30.0] * 3                  # 273 runs over 273 days; pre-fix 91.0


@_SKIP
def test_repeat_scan_first_of_month_prefilter_is_one_day(monkeypatch):
    seen: list[str] = []
    _repeat_page(monkeypatch, window="CURRENT_MONTH", today=date(2026, 10, 1), n=1, span=1, seen=seen)
    assert seen and "HAVING COUNT(*) >= 2" in seen[0]                 # pre-fix: >= 10 (the 30-day fallback)


@_SKIP
def test_repeat_tiles_are_window_totals_not_the_top_100(monkeypatch):
    seen: list[str] = []
    at = _repeat_page(monkeypatch, window=30, today=date(2026, 9, 30), n=100, span=30, seen=seen)
    tiles = " ".join(str(m.value) for m in at.markdown)
    assert re.search(r"Repeated fingerprints.{0,400}400", tiles, re.S)
    assert re.search(r"Materialization candidates.{0,400}179", tiles, re.S)
    assert "1261h" in tiles                                          # pre-fix: 100 x 1.5h = 150h
    assert "The table lists the top 100 of 400 repeated fingerprints" in _texts(at)


# ---- R1-048 / R1-148: storage growth names its served window ----------------------------------------------

def test_storage_growth_builder_carries_uncapped_totals_and_the_database_filter():
    sql = insights_sql.storage_growth_by_database(90, "ALL", database="alfa_edw")
    for col in ("DATABASES_WIN", "CURRENT_BYTES_WIN", "GROWTH_BYTES_WIN"):
        assert f"OVER () AS {col}" in sql
    assert "UPPER(DATABASE_NAME) = 'ALFA_EDW'" in sql
    assert "UPPER(DATABASE_NAME) =" not in insights_sql.storage_growth_by_database(90, "ALL")
    sqlglot.parse_one(sql, read="snowflake")


@_SKIP
@pytest.mark.parametrize(("window", "served"), [(365, 90), (180, 90), (30, 30)])
def test_storage_growth_tile_names_the_served_window(monkeypatch, window, served):
    seen: list[str] = []

    def hook(sql, kw):
        if str(kw.get("key", "")).startswith("storgrow_"):
            seen.append(sql)
        return None

    at = _cost_page(monkeypatch, "Storage & waste", run_hook=hook, state={"flt_days": window})
    assert seen and all(f"DATEADD('day', -{served}, CURRENT_DATE())" in s for s in seen)
    tiles = " ".join(str(m.value) for m in at.markdown)
    assert f"Growth ({served}d)" in tiles
    if window != served:
        assert f"Growth ({window}d)" not in tiles                     # pre-fix: "Growth (365d)" over 90 days


@_SKIP
def test_storage_tiles_read_the_uncapped_window_totals(monkeypatch):
    """The builder stops at the top 100 growers; the two storage tiles read its pre-LIMIT totals (150 TB held,
    12 TB net growth across every database in scope), not the sum of the rows the LIMIT kept (2 TB / 0.4 TB)."""
    tib = 1024.0 ** 4
    frame = pd.DataFrame({
        "DATABASE_NAME": ["DB_A", "DB_B"], "COMPANY": ["ALFA", "ALFA"],
        "FIRST_DAY": pd.to_datetime(["2026-07-02", "2026-07-02"]),
        "LAST_DAY": pd.to_datetime(["2026-09-29", "2026-09-29"]),
        "FIRST_BYTES": [0.8 * tib, 0.8 * tib], "LAST_BYTES": [1.0 * tib, 1.0 * tib],
        "FAILSAFE_BYTES": [0.0, 0.0], "SPAN_DAYS": [89.0, 89.0], "DAYS_OBSERVED": [90.0, 90.0],
        "SLOPE_BYTES_PER_DAY": [0.002 * tib, 0.002 * tib],
        "DATABASES_WIN": [140, 140], "CURRENT_BYTES_WIN": [150.0 * tib] * 2, "GROWTH_BYTES_WIN": [12.0 * tib] * 2})

    def hook(_sql, kw):
        return _ok(frame) if str(kw.get("key", "")).startswith("storgrow_") and "drill" not in kw["key"] else None

    at = _cost_page(monkeypatch, "Storage & waste", run_hook=hook, state={"flt_days": 90})
    tiles = " ".join(str(m.value) for m in at.markdown)
    assert re.search(r"Current storage.{0,400}150\.0 TB", tiles, re.S)          # pre-fix: 2.0 TB
    assert re.search(r"Growth \(90d\).{0,400}12\.0 TB", tiles, re.S)            # pre-fix: 0.4 TB
