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
  R1-148  the storage-growth tile names the window the SQL serves (R1-048) and its totals are uncapped;
  R1-170  (twin, fix-up) Remediation's tighten guard, estimate and autobook decision, and the resize lever's
          current size, read ONE warehouse's SHOW row on the live tier, never the 4 h 'jump_wh' cache;
          (v4.606 integration, fails on 8c0eac76) that read is the alert drawer's builder
          (recheck_sql.warehouse_settings_sql) through ONE parser (insights.show_warehouse_settings), so a listed
          NULL timer is the never-suspend 0 here too (R1-071), and the resize receipt names only what was booked;
  R1-044  (fix-up) a long legal FQN (3 x 255 characters) keeps its whole match key;
  R1-017  (sibling, fix-up) a failed experiments read is named on the proven-fix transfer panel.

AppTests reuse the shaped page harness (tests/test_pages_shaped.py); nothing opens a Snowflake session."""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta

import pandas as pd
import pytest
import sqlglot
from test_pages_shaped import (  # noqa: F401 - _stub_shaped is the harness's autouse fixture
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

@pytest.mark.parametrize("kind", ["timeout", "missing_column"])
def test_failed_verified_wins_read_is_unavailable_not_none_yet(monkeypatch, kind):
    def hook(_sql, kw):
        return _failed(kind) if str(kw.get("key", "")).startswith("opt_verified_wins") else None

    at = _cost_page(monkeypatch, "Idle & sizing", run_hook=hook)
    text = _texts(at)
    assert "No verified savings yet" not in text
    assert "Verified wins (SAVINGS_LEDGER x WAREHOUSE_CHANGE_REGISTRY) could not be read" in " ".join(
        str(e.value) for e in at.error)


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


def test_table_tco_keeps_the_whole_of_a_long_legal_name():
    """Three 255-character identifiers make a legal 767-character FQN. The key was capped at 600 characters
    (sql_literal truncates silently), so it never matched and the drill read 0 reads for a read table."""
    parts = ("D" * 255, "S" * 255, "T" * 255)
    fqn = ".".join(parts)
    sql = insights_sql.table_tco(*parts, 30)
    assert sql.count(f"= '{fqn}'") == 2                                  # pre-fix: a 600-character prefix
    sqlglot.parse_one(sql, read="snowflake")


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


def test_repeat_scan_normalizes_current_year_by_its_span(monkeypatch):
    seen: list[str] = []
    at = _repeat_page(monkeypatch, window="CURRENT_YEAR", today=date(2026, 9, 30), n=3, span=273, seen=seen)
    assert seen and "HAVING COUNT(*) >= 91" in seen[0]               # pre-fix: >= 30 (the clamped 90 days)
    table = _repeat_table(at)
    assert list(table["RUNS_PER_30D"]) == [30.0] * 3                  # 273 runs over 273 days; pre-fix 91.0


def test_repeat_scan_first_of_month_prefilter_is_one_day(monkeypatch):
    seen: list[str] = []
    _repeat_page(monkeypatch, window="CURRENT_MONTH", today=date(2026, 10, 1), n=1, span=1, seen=seen)
    assert seen and "HAVING COUNT(*) >= 2" in seen[0]                 # pre-fix: >= 10 (the 30-day fallback)


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


# ---- R1-170 (twin): Remediation & ledger reads the setting it is about to change LIVE ----------------------

_REMED_IDLE = pd.DataFrame({"WAREHOUSE_NAME": ["WH_X"], "COMPANY": ["ALFA"], "METERED_HOURS": [300.0],
                            "IDLE_HOURS": [200.0], "TOTAL_CREDITS": [300.0], "IDLE_CREDITS": [200.0]})


def _tighten_page(monkeypatch, *, cached: float | None, live, executed: list, seen: list) -> AppTest:
    """Remediation & ledger, 'Tighten auto-suspend to 60s' on WH_X, as an operator. ``cached`` is the AUTO_SUSPEND
    the shared 4 h 'jump_wh' SHOW WAREHOUSES entry still holds; ``live`` is the one-warehouse SHOW LIKE read's
    frame (or a failed QueryResult)."""
    def hook(sql, kw):
        if sql.startswith("SHOW WAREHOUSES LIKE"):
            seen.append((sql, dict(kw)))
            return live if isinstance(live, QueryResult) else _ok(live)
        if sql.startswith("SHOW WAREHOUSES"):
            return _ok(pd.DataFrame({"name": ["WH_X"], "size": ["X-Small"], "auto_suspend": [cached]}))
        return None

    def mart_hook(_mart, _live, kw):
        return _ok(_REMED_IDLE) if str(kw.get("key", "")).startswith("remed_idle") else None

    return _cost_page(monkeypatch, "Remediation & ledger", run_hook=hook, mart_hook=mart_hook, operator=True,
                      executed=executed)


def _execute_tighten(at) -> None:
    at.text_input(key="remed_confirm").input("WH_X").run()
    at.button(key="remed_btn").click().run()
    assert not at.exception


def test_tighten_guard_reads_the_timer_live_not_the_stale_cache(monkeypatch):
    """The cache still says 600 s; a DBA has since set 30 s. Pre-fix the plan generated SET = 60 (raising the
    timer) behind an Execute that logged a positive saving. The live row decides: no ALTER, nothing to execute.
    LIKE 'WH_X' also returns WHAX (the '_' wildcard): only the exact-name row counts."""
    executed: list[str] = []
    seen: list = []
    live = pd.DataFrame({"name": ["WHAX", "WH_X"], "auto_suspend": [600, 30]})
    at = _tighten_page(monkeypatch, cached=600, live=live, executed=executed, seen=seen)
    code = "\n".join(str(c.value) for c in at.code)
    assert "SET AUTO_SUSPEND" not in code                               # pre-fix: SET AUTO_SUSPEND = 60
    assert "WH_X is already at AUTO_SUSPEND=30s" in " ".join(str(i.value) for i in at.info)
    assert not any(t.key == "remed_confirm" for t in at.text_input)
    assert executed == []
    # ONE warehouse, on the 30 s live tier, unlogged when absent (probe), every row (no row-cap rewrite)
    assert seen, "no live SHOW WAREHOUSES LIKE read"
    sql, kw = seen[-1]
    assert sql == "SHOW WAREHOUSES LIKE 'WH_X'"
    assert (kw.get("tier"), kw.get("key"), kw.get("max_rows"), kw.get("probe")) == (
        "live", "remed_suspend_WH_X", 0, True)


def test_a_failed_live_read_generates_no_alter_even_when_the_cache_says_600(monkeypatch):
    executed: list[str] = []
    at = _tighten_page(monkeypatch, cached=600, live=_failed("timeout"), executed=executed, seen=[])
    assert "SET AUTO_SUSPEND" not in "\n".join(str(c.value) for c in at.code)
    assert "Current AUTO_SUSPEND could not be verified" in " ".join(str(w.value) for w in at.warning)
    assert not any(t.key == "remed_confirm" for t in at.text_input)
    assert executed == []


def test_a_live_600_tightens_where_the_stale_cache_said_30(monkeypatch):
    """The other direction: the cache says 30 s, but the timer was loosened to 600 s since. The live value
    generates the tighten (pre-fix: 'already at 30s', nothing offered), and 600 -> 60 is a downward change the
    daily scan books, so the Execute logs it without a second ledger row."""
    executed: list[str] = []
    at = _tighten_page(monkeypatch, cached=30, live=pd.DataFrame({"name": ["WH_X"], "auto_suspend": [600]}),
                       executed=executed, seen=[])
    assert "ALTER WAREHOUSE WH_X SET AUTO_SUSPEND = 60;" in "\n".join(str(c.value) for c in at.code)
    _execute_tighten(at)
    assert executed[0] == "ALTER WAREHOUSE WH_X SET AUTO_SUSPEND = 60;"
    assert any("REMEDIATION_LOG" in s for s in executed)
    assert not any("SAVINGS_LEDGER" in s for s in executed)


def test_a_live_never_suspend_timer_is_booked_by_the_app(monkeypatch):
    """The cache says 600 s, the live timer is 0 (never suspend). The scan does not book enabling a timer, so the
    app must: the autobook decision follows the live value (pre-fix: the cached 600 read as autobooked, and the
    ESTIMATED ledger row was never booked)."""
    executed: list[str] = []
    at = _tighten_page(monkeypatch, cached=600, live=pd.DataFrame({"name": ["WH_X"], "auto_suspend": [0]}),
                       executed=executed, seen=[])
    _execute_tighten(at)
    assert executed[0] == "ALTER WAREHOUSE WH_X SET AUTO_SUSPEND = 60;"
    booked = [s for s in executed if s.startswith("INSERT INTO DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER")]
    assert len(booked) == 1 and "'AUTO_SUSPEND', 'WH_X'" in booked[0]


def test_a_live_null_timer_is_never_suspend_and_gets_the_enable_a_timer_alter(monkeypatch):
    """R1-071 on the shared parser: SHOW lists WH_X with a NULL auto_suspend (it never suspends). Pre-merge the
    Remediation guard's own parser read that NULL as unknown -- a false 'could not be verified' and no ALTER --
    while the alert drawer and Optimize ▸ Idle read it as the known never-suspend 0. It is that 0 here too: the
    timer is enabled, and since the scan never books enabling a timer, the app books the ESTIMATED row."""
    executed: list[str] = []
    at = _tighten_page(monkeypatch, cached=600, live=pd.DataFrame({"name": ["WH_X"], "auto_suspend": [None]}),
                       executed=executed, seen=[])
    assert "could not be verified" not in " ".join(str(w.value) for w in at.warning)
    assert "ALTER WAREHOUSE WH_X SET AUTO_SUSPEND = 60;" in "\n".join(str(c.value) for c in at.code)
    _execute_tighten(at)
    assert executed[0] == "ALTER WAREHOUSE WH_X SET AUTO_SUSPEND = 60;"
    booked = [s for s in executed if s.startswith("INSERT INTO DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER")]
    assert len(booked) == 1 and "'AUTO_SUSPEND', 'WH_X'" in booked[0]


def test_show_warehouse_settings_parse():
    """The ONE parser of a live one-warehouse SHOW read (insights.show_warehouse_settings), shared with the
    alert drawer: the exact-name row only, case-insensitive; a listed NULL timer is the known never-suspend 0."""
    from app.logic.insights import auto_suspend_in_force, show_warehouse_settings

    near = pd.DataFrame({"NAME": ["WHAX", "wh_x"], "AUTO_SUSPEND": [600, 45], "SIZE": ["Large", "Medium"]})
    got = show_warehouse_settings(near, "WH_X")
    assert (got.listed, got.auto_suspend, got.auto_suspend_known, got.size) == (True, 45.0, True, "Medium")
    assert auto_suspend_in_force(near, "WH_X") == (True, 45.0)
    for df in (pd.DataFrame({"name": ["WHAX"], "auto_suspend": [600], "size": ["Large"]}), None, pd.DataFrame(),
               pd.DataFrame({"auto_suspend": [600]})):
        got = show_warehouse_settings(df, "WH_X")
        assert (got.listed, got.auto_suspend, got.auto_suspend_known, got.size) == (False, None, False, "")
        assert auto_suspend_in_force(df, "WH_X") == (False, None)
    nulls = pd.DataFrame({"name": ["WH_X"], "auto_suspend": [None], "size": [None]})
    assert auto_suspend_in_force(nulls, "WH_X") == (True, 0.0)              # R1-071: never suspends
    assert show_warehouse_settings(nulls, "WH_X").size == ""                # a NULL size is unknown, never 'None'
    bare = pd.DataFrame({"name": ["WH_X"]})                                 # SHOW without either column
    assert auto_suspend_in_force(bare, "WH_X") == (False, None) and show_warehouse_settings(bare, "WH_X").size == ""
    assert auto_suspend_in_force(pd.DataFrame({"name": ["WH_X"], "auto_suspend": ["?"]}), "WH_X") == (False, None)


def test_the_shared_live_settings_builder_is_one_validated_warehouse():
    from app.data import recheck_sql

    assert recheck_sql.warehouse_settings_sql("WH_ALFA_BI_PRD") == "SHOW WAREHOUSES LIKE 'WH_ALFA_BI_PRD'"
    assert recheck_sql.warehouse_settings_sql(" wh_x ") == "SHOW WAREHOUSES LIKE 'wh_x'"
    for bad in ("", "WH; DROP TABLE X", "WH'X", None):
        assert recheck_sql.warehouse_settings_sql(bad) is None  # type: ignore[arg-type]


def test_one_builder_and_one_parser_serve_every_live_settings_read():
    """v4.606 integration: c02 (Optimize) and c03 (Alerts) each added a one-warehouse SHOW builder and an
    exact-name parser. Folded to ONE of each so the two tighten guards (and the resize lever) cannot drift --
    the copies had already diverged on a listed NULL timer (R1-071)."""
    from tests._source import read

    opt = read("app/ui/pages/cost_parts/optimize.py")
    al = read("app/ui/pages/alerts.py")
    assert not hasattr(insights_sql, "warehouse_settings_live_sql")
    for src in (opt, al):
        assert "recheck_sql.warehouse_settings_sql(" in src
        assert "def _exact_show_row(" not in src and "def _auto_suspend_in_force(" not in src
    helper = opt.split("def _live_warehouse_settings(", 1)[1].split("\ndef ", 1)[0]
    assert "return show_warehouse_settings(res.df if res.ok else None, warehouse)" in helper
    assert 'tier="live"' in helper and "max_rows=0" in helper and "probe=True" in helper
    assert "auto_suspend_in_force(" in al


def test_the_resize_estimate_uses_the_size_in_force_now(monkeypatch):
    """The profile was mapped from the cached 'jump_wh' read (Large); a DBA has since resized to Medium. A pick
    of SMALL is ONE step down, not two: the caption names the live size and says the cached one differed."""
    from test_cluster_cap_shaped import _page, _pane, _pick

    import app.ui.pages.cost_parts.optimize as opt

    at, _seen = _page(monkeypatch, check=True, select="WH_LOW", size="Large")
    cached_run = opt.run
    live_reads: list[str] = []

    def _run(*args, **kwargs):
        sql = str(args[0] if args else kwargs.get("sql", ""))
        if sql.startswith("SHOW WAREHOUSES LIKE"):
            live_reads.append(sql)
            return _ok(pd.DataFrame({"name": ["WH_LOW"], "size": ["Medium"], "auto_suspend": [300]}))
        return cached_run(*args, **kwargs)

    monkeypatch.setattr(opt, "run", _run)
    _pick(at).select("SMALL").run()
    assert not at.exception
    _code, text = _pane(at)
    assert "resizing LARGE → SMALL" not in text                         # pre-fix: priced two steps from LARGE
    assert "resizing MEDIUM → SMALL" in text
    assert ("SHOW WAREHOUSES now reports MEDIUM (the profile above read LARGE from a cached read): the estimate "
            "below uses the size in force now.") in text
    assert live_reads and set(live_reads) == {"SHOW WAREHOUSES LIKE 'WH_LOW'"}


def test_a_failed_live_size_read_projects_and_books_no_resize_saving(monkeypatch):
    from test_cluster_cap_shaped import _page, _pane, _pick, _recording_writes

    import app.ui.pages.cost_parts.optimize as opt

    at, _seen = _page(monkeypatch, check=True, select="WH_LOW", size="Large")
    cached_run = opt.run

    def _run(*args, **kwargs):
        sql = str(args[0] if args else kwargs.get("sql", ""))
        return _failed("timeout") if sql.startswith("SHOW WAREHOUSES LIKE") else cached_run(*args, **kwargs)

    monkeypatch.setattr(opt, "run", _run)
    writes = _recording_writes(monkeypatch)
    receipts = _recording_receipts(monkeypatch)
    _pick(at).select("SMALL").run()
    assert not at.exception
    _code, text = _pane(at)
    assert "Projected saving" not in text                               # pre-fix: priced from the cached LARGE
    assert "Current warehouse size unavailable (SHOW WAREHOUSES) — no saving booked automatically" in text
    at.text_input(key="sizing_confirm").input("WH_LOW").run()
    at.button(key="sizing_btn").click().run()
    assert not at.exception
    assert "ALTER WAREHOUSE WH_LOW SET WAREHOUSE_SIZE = 'SMALL';" in writes
    logged = [w for w in writes if "REMEDIATION_LOG" in w]
    assert len(logged) == 1 and "WAREHOUSE_SIZE = ''SMALL'';', 0.0, 'EXECUTED'" in logged[0]   # est 0
    assert not any("SAVINGS_LEDGER" in w for w in writes)
    # c02 recheck: the receipt follows the (absent) ledger row -- pre-fix it said a saving was booked
    assert receipts == [(True, "Resized WH_LOW to SMALL; no saving was booked: the current size could not be "
                               "verified.")]


def _recording_receipts(monkeypatch) -> list[tuple[bool, str]]:
    """Every notify() receipt the Cost ▸ Optimize page raises, as (ok, message)."""
    import app.ui.pages.cost_parts.optimize as opt

    receipts: list[tuple[bool, str]] = []
    monkeypatch.setattr(opt, "notify", lambda ok, msg: receipts.append((ok, msg)))
    return receipts


_SCAN_BOOKS_RESIZE = ("The daily change scan books this resize to the Savings ledger and settles it against 14 days "
                      "of measured actuals — the app logs the estimate to REMEDIATION_LOG instead of booking a second "
                      "ledger row.")
_APP_BOOKS_RESIZE = ("The daily change scan cannot rank a 5XLARGE warehouse, so it never books this resize: on Execute "
                     "the app books this estimate as an ESTIMATED Savings ledger row — verify it there.")


@pytest.mark.parametrize(("live_size", "pick", "receipt", "ledger_rows", "caption"), [
    # a downsize from a size the change scan ranks: the scan books it, the app does not
    ("Large", "SMALL", "the daily change scan books and settles the measured saving.", 0, _SCAN_BOOKS_RESIZE),
    # an UPSIZE from a ranked size: the scan books only a downsize (V153), so no saving is promised
    ("Medium", "LARGE", "no saving was booked (not a downsize from the current size).", 0,
     "Resizing UP MEDIUM → LARGE raises cost — no saving booked."),
    # a downsize from a size the scan cannot rank (5X-Large): the app books the ESTIMATED row, and says so --
    # BEFORE Execute too (f2 fix-up: the pane promised the scan would book it, then the app booked a row)
    ("5X-Large", "XXLARGE", "booked an estimated saving — verify it on the Savings ledger.", 1, _APP_BOOKS_RESIZE),
])
def test_the_resize_receipt_says_only_what_was_booked(monkeypatch, live_size, pick, receipt, ledger_rows, caption):
    from test_cluster_cap_shaped import _page, _pane, _pick, _recording_writes

    import app.ui.pages.cost_parts.optimize as opt

    at, _seen = _page(monkeypatch, check=True, select="WH_LOW", size=live_size)
    cached_run = opt.run

    def _run(*args, **kwargs):
        sql = str(args[0] if args else kwargs.get("sql", ""))
        if sql.startswith("SHOW WAREHOUSES LIKE"):
            return _ok(pd.DataFrame({"name": ["WH_LOW"], "size": [live_size], "auto_suspend": [300]}))
        return cached_run(*args, **kwargs)

    monkeypatch.setattr(opt, "run", _run)
    writes = _recording_writes(monkeypatch)
    receipts = _recording_receipts(monkeypatch)
    _pick(at).select(pick).run()
    _code, text = _pane(at)
    assert caption in text
    # the scan's promise is made only where the scan books the resize (a ranked size, a downsize)
    assert (_SCAN_BOOKS_RESIZE in text) is (caption == _SCAN_BOOKS_RESIZE)
    at.text_input(key="sizing_confirm").input("WH_LOW").run()
    at.button(key="sizing_btn").click().run()
    assert not at.exception
    assert f"ALTER WAREHOUSE WH_LOW SET WAREHOUSE_SIZE = '{pick}';" in writes
    assert sum("SAVINGS_LEDGER" in w for w in writes) == ledger_rows
    assert receipts == [(True, f"Resized WH_LOW to {pick}; {receipt}")]


def _ledger_insert_refused(monkeypatch) -> list[str]:
    """Every statement the Execute gate runs; each succeeds except a SAVINGS_LEDGER INSERT, which execute_statement
    reports the way it reports any failure -- (False, msg), never an exception."""
    import app.ui.pages.cost_parts.optimize as opt

    writes: list[str] = []

    def _execute(sql, **_kwargs):
        writes.append(str(sql))
        if "SAVINGS_LEDGER" in str(sql):
            return False, "insufficient privileges"
        return True, "Statement executed."

    monkeypatch.setattr(opt, "execute_statement", _execute)
    return writes


def test_a_refused_resize_ledger_insert_is_never_receipted_as_booked(monkeypatch):
    """f2 fix-up: 5X-Large -> XXLARGE is a downsize the scan cannot rank, so the app INSERTs the ESTIMATED row. When
    that INSERT fails, the resize still happened but nothing was booked: the receipt says so and stays on screen
    (pre-fix: 'booked an estimated saving' off the attempt alone -- the INSERT's (ok, msg) was discarded)."""
    from test_cluster_cap_shaped import _page, _pick

    import app.ui.pages.cost_parts.optimize as opt

    at, _seen = _page(monkeypatch, check=True, select="WH_LOW", size="5X-Large")
    cached_run = opt.run

    def _run(*args, **kwargs):
        sql = str(args[0] if args else kwargs.get("sql", ""))
        if sql.startswith("SHOW WAREHOUSES LIKE"):
            return _ok(pd.DataFrame({"name": ["WH_LOW"], "size": ["5X-Large"], "auto_suspend": [300]}))
        return cached_run(*args, **kwargs)

    monkeypatch.setattr(opt, "run", _run)
    writes = _ledger_insert_refused(monkeypatch)
    receipts = _recording_receipts(monkeypatch)
    _pick(at).select("XXLARGE").run()
    at.text_input(key="sizing_confirm").input("WH_LOW").run()
    at.button(key="sizing_btn").click().run()
    assert not at.exception
    assert writes[0] == "ALTER WAREHOUSE WH_LOW SET WAREHOUSE_SIZE = 'XXLARGE';"
    assert sum("SAVINGS_LEDGER" in w for w in writes) == 1                 # attempted once, refused
    assert receipts == [(False, "Resized WH_LOW to XXLARGE, but the estimated saving could not be booked: "
                                "insufficient privileges")]
    assert not any("booked an estimated saving" in msg for _ok_flag, msg in receipts)


def test_a_refused_tighten_ledger_insert_is_never_receipted_as_booked(monkeypatch):
    """f2 fix-up, the tighten twin: a live never-suspend timer (0) is enabled to 60 s, which the scan never books, so
    the app INSERTs the ESTIMATED row. Pre-fix _book_ledger was the INSERT's gate, not its result: a refused INSERT
    was receipted 'executed and booked.'"""
    at = _tighten_page(monkeypatch, cached=600, live=pd.DataFrame({"name": ["WH_X"], "auto_suspend": [0]}),
                       executed=[], seen=[])
    writes = _ledger_insert_refused(monkeypatch)
    receipts = _recording_receipts(monkeypatch)
    _execute_tighten(at)
    assert writes[0] == "ALTER WAREHOUSE WH_X SET AUTO_SUSPEND = 60;"
    assert sum("SAVINGS_LEDGER" in w for w in writes) == 1
    assert receipts == [(False, "Tighten auto-suspend to 60s on WH_X — executed, but its estimated saving could not "
                                "be booked: insufficient privileges")]
    assert not any("and booked." in msg for _ok_flag, msg in receipts)


# ---- R1-017 (sibling): a failed experiments read is named on the proven-fix transfer panel ------------------

def _transfer_page(monkeypatch, experiments: QueryResult) -> AppTest:
    """Idle & sizing with one verified AUTO_SUSPEND win (WH_PROVEN) and one idle, settings-verified candidate
    (WH_CAND, 600 s timer) the transfer panel suggests; the OPTIMIZATION_EXPERIMENTS read is ``experiments``."""
    idle = pd.DataFrame({"WAREHOUSE_NAME": ["WH_CAND"], "COMPANY": ["ALFA"], "METERED_HOURS": [300.0],
                         "IDLE_HOURS": [200.0], "TOTAL_CREDITS": [300.0], "IDLE_CREDITS": [200.0]})
    wins = pd.DataFrame({"FIX_TYPE": ["AUTO_SUSPEND"], "TARGET_WAREHOUSE": ["WH_PROVEN"], "VERIFIED_USD": [50.0]})

    def hook(sql, kw):
        key = str(kw.get("key", ""))
        if sql.startswith("SHOW WAREHOUSES"):
            return _ok(pd.DataFrame({"name": ["WH_CAND"], "size": ["X-Small"], "auto_suspend": [600]}))
        if key.startswith("opt_verified_wins"):
            return _ok(wins)
        if key.startswith("opt_experiments"):
            return experiments
        return None

    def mart_hook(_mart, _live, kw):
        return _ok(idle) if str(kw.get("key", "")).startswith("idle_") else None

    return _cost_page(monkeypatch, "Idle & sizing", run_hook=hook, mart_hook=mart_hook)


def _transfer_rows(at) -> pd.DataFrame:
    for df in at.dataframe:
        if isinstance(df.value, pd.DataFrame) and "CANDIDATE_WAREHOUSE" in df.value.columns:
            return df.value
    raise AssertionError("the proven-fix transfer table did not render")


def test_failed_experiments_read_says_the_exclusion_was_not_checked(monkeypatch):
    at = _transfer_page(monkeypatch, _failed("timeout"))
    assert list(_transfer_rows(at)["CANDIDATE_WAREHOUSE"]) == ["WH_CAND"]
    errors = " ".join(str(e.value) for e in at.error)
    assert ("Open optimization experiments (OPTIMIZATION_EXPERIMENTS) could not be read, so a warehouse already "
            "under experiment is not excluded from these suggestions") in errors       # pre-fix: silent


def test_unreadable_experiments_table_is_a_setup_note(monkeypatch):
    at = _transfer_page(monkeypatch, _failed("absent"))
    assert list(_transfer_rows(at)["CANDIDATE_WAREHOUSE"]) == ["WH_CAND"]
    assert "OPTIMIZATION_EXPERIMENTS) aren't readable by this app" in " ".join(str(i.value) for i in at.info)
    assert "OPTIMIZATION_EXPERIMENTS) could not be read" not in " ".join(str(e.value) for e in at.error)


def test_a_clean_experiments_read_adds_no_note(monkeypatch):
    at = _transfer_page(monkeypatch, _ok(pd.DataFrame()))
    assert list(_transfer_rows(at)["CANDIDATE_WAREHOUSE"]) == ["WH_CAND"]
    assert "OPTIMIZATION_EXPERIMENTS)" not in _texts(at)
