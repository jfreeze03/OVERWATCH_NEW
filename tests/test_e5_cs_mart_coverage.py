"""v4.608 (PR-3, cluster e5-cost) R2-012: MART_CLOUD_SVC_DAILY holds statements only from its first load.

SP_LOAD_CLOUD_SVC_MART (V055) merges the last 2 days of the 72 h extract and nothing backfills the mart, so its
history starts the day it was first loaded (and again after a rebuild drops it). On a 90/180/365-day or
Current-year window the Spend panels summed ~65 days under the full label:

  * cloud_svc_billed_families compared statement credits on those days with metering over EVERY complete day
    of the window, so the 'N% of metered' sub-line read ~27% low on Last 90 days (43% for a 3-of-7 day mart
    whose statements are 100% of metering), and METERED_DAYS / the billing-basis note counted days with no
    statements;
  * cs_by_query_type_mart / cloud_svc_top_shapes / cloud_svc_by_user carried no COVERED_DAYS, so the
    'Scanned Nd of the window' caption never fired on the mart leg (and, once it can, must not blame the live
    fallback for a short mart).

Also the R2-056 regression: on the 1st of the month (Current month) the statement-type caption claims no scan cap.
Executed in sqlite with the shims of tests/test_cs_billed_families_harness.py; nothing opens a Snowflake session.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pandas as pd
import pytest

from app.core.result import QueryResult
from app.data import mart_sql
from app.logic import cs_driver
from tests.test_cs_billed_families_harness import (
    TODAY,
    _CountIf,
    _greatest,
    _least,
    _MaxBy,
    _regexp_instr,
    _to_sqlite,
)

RATE = 3.68


def _db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.create_function("LEAST", -1, _least)
    c.create_function("GREATEST", -1, _greatest)
    c.create_function("IFF", 3, lambda cond, a, b: a if cond else b)
    c.create_function("REGEXP_INSTR", 2, _regexp_instr)
    c.create_aggregate("COUNT_IF", 1, _CountIf)
    c.create_aggregate("MAX_BY", 2, _MaxBy)
    c.executescript("""
    CREATE TABLE FACT_METERING_DAILY(DAY TEXT, SERVICE_TYPE TEXT, CREDITS_COMPUTE REAL,
        CREDITS_CLOUD_SVCS REAL, CREDITS_ADJUSTMENT REAL);
    CREATE TABLE MART_CLOUD_SVC_DAILY(DAY TEXT, COMPANY TEXT, WAREHOUSE_NAME TEXT, USER_NAME TEXT,
        ROLE_NAME TEXT, QUERY_TYPE TEXT, QUERY_PARAMETERIZED_HASH TEXT, SAMPLE_TEXT TEXT, RUNS INT,
        CS_CREDITS REAL, EXEC_SEC_SUM REAL, COMPILE_SEC_SUM REAL);
    CREATE TABLE MART_QUERY_FAMILY_DAILY(DAY TEXT, COMPANY TEXT, QUERY_HASH TEXT, TOTAL_ELAPSED_SEC REAL,
        TOTAL_EXEC_SEC REAL, RUNS INT);
    CREATE TABLE FACT_APP_COST_DAILY(DAY TEXT, COMPANY TEXT, USER_NAME TEXT, APPLICATION TEXT, QUERIES INT);
    """)
    return c


def _query(c: sqlite3.Connection, sql: str, days: int) -> pd.DataFrame:
    cur = c.execute(_to_sqlite(sql, days))
    return pd.DataFrame(cur.fetchall(), columns=[d[0] for d in cur.description])


_WINDOW = [f"2026-09-{d:02d}" for d in range(19, 27)]     # 7 complete days + TODAY (the in-progress day)
assert _WINDOW[-1] == TODAY


def _young_mart(c: sqlite3.Connection) -> None:
    """Metering on all 7 complete days (10 CS, billed 7 each); the statement mart started on 09-23, so its
    statements cover only the last 3 complete days (100% of those days' metering) plus today's partial day."""
    for d in _WINDOW[:-1]:
        c.execute("INSERT INTO FACT_METERING_DAILY VALUES (?,?,?,?,?)", (d, "WAREHOUSE_METERING", 100.0, 10.0, -3.0))
    c.execute("INSERT INTO FACT_METERING_DAILY VALUES (?,?,?,?,?)", (TODAY, "WAREHOUSE_METERING", 40.0, 4.0, -4.0))
    for d, cs in (("2026-09-23", 10.0), ("2026-09-24", 10.0), ("2026-09-25", 10.0), (TODAY, 4.0)):
        c.execute("INSERT INTO MART_CLOUD_SVC_DAILY VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (d, "ALFA", "WH_A", "SVC", "R", "SELECT", "H1", "select 1", 100, cs, 1.0, 0.1))


def test_billed_families_compare_metering_only_on_days_the_statement_mart_holds():
    c = _db()
    _young_mart(c)
    df = _query(c, mart_sql.cloud_svc_billed_families(7), 7)
    row = df.iloc[0]
    assert row["METERED_DAYS"] == 3                              # pre-fix: 7 (four days with no statements)
    assert row["METERED_CS_CREDITS"] == pytest.approx(30.0)      # pre-fix: 70
    assert row["COVERED_DAYS"] == 4                              # 09-23..09-25 + today
    _, s = cs_driver.billed_family_view(df, RATE)
    share = (s["scope_cs"] - s["unmetered_cs"]) / s["metered_cs"] * 100      # spend.py's sub-line, verbatim
    assert share == pytest.approx(100.0)                         # pre-fix: 43% ("most CS is on no statement")
    head, _ = cs_driver.billing_basis_note(s, RATE)
    assert "all 3 complete metered days" in head                 # pre-fix: "all 7"


def test_billed_families_floor_is_account_wide_not_the_scope():
    """The floor is the mart's first day for the whole account: a company whose statements start later still
    compares the account's metering from the mart's first day (billing is an account fact)."""
    sql = mart_sql.cloud_svc_billed_families(30, "Trexis", "WH_T")
    bill = sql.split("WITH bill AS (", 1)[1].split("\n),", 1)[0]
    assert "x.DAY >= (SELECT MIN(m0.DAY) FROM DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY m0)" in bill
    assert "Trexis" not in bill and "WH_T" not in bill


def test_cs_by_query_type_mart_carries_account_wide_covered_days():
    c = _db()
    for d in ("2026-09-24", "2026-09-25"):
        c.execute("INSERT INTO MART_CLOUD_SVC_DAILY VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (d, "ALFA", "WH_A", "U", "R", "SHOW", "H", "show tables", 10, 1.0, 0.0, 0.0))
    for d in ("2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25"):
        c.execute("INSERT INTO MART_CLOUD_SVC_DAILY VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (d, "Trexis", "WH_T", "U", "R", "SELECT", "H2", "select 2", 10, 2.0, 0.0, 0.0))
    df = _query(c, mart_sql.cs_by_query_type_mart(7, "ALFA"), 7)
    assert list(df.columns) == ["QUERY_TYPE", "QUERIES", "CS_CREDITS", "CS_CREDITS_PER_1K", "COVERED_DAYS"]
    assert df["QUERY_TYPE"].tolist() == ["SHOW"]                 # the scope still filters the rows ...
    assert df["COVERED_DAYS"].iloc[0] == 4                       # ... but coverage is the loader's, not ALFA's


@pytest.mark.parametrize("builder", [mart_sql.cloud_svc_top_shapes, mart_sql.cloud_svc_by_user])
def test_drill_readers_add_covered_days_only_on_request(builder):
    plain = builder(90, "ALFA", "WH_A")
    assert "COVERED_DAYS" not in plain                           # the alert evidence pack keeps its shape
    cov = builder(90, "ALFA", "WH_A", coverage=True)
    assert "COUNT(DISTINCT c0.DAY)" in cov and "AS COVERED_DAYS" in cov
    assert cov.replace(", (SELECT COUNT(DISTINCT c0.DAY) FROM DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY c0 "
                       "WHERE c0.DAY >= DATEADD('day', -90, CURRENT_DATE())) AS COVERED_DAYS", "") == plain


# ---------------------------------------------------------------- rendered Spend panels (AppTest) ----
_TYPES = pd.DataFrame({"QUERY_TYPE": ["SHOW", "DESCRIBE"], "QUERIES": [900, 400],
                       "CS_CREDITS": [3.0, 1.0], "CS_CREDITS_PER_1K": [3.33, 2.5]})


def _render_spend(monkeypatch, *, days, bounds, mart_frame: pd.DataFrame | None, drill_frame=None):
    """The real Spend tab with one ELEVATED warehouse (so the statement-type panel paints). The cs_types mart leg
    answers ``mart_frame`` (or fails -> the live twin answers); ``drill_frame`` opens the shape/user drill."""
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    from app.ui import components
    from app.ui.pages.cost_parts import spend

    components._MART_FAIL_BACKOFF.clear()
    empty = QueryResult(df=pd.DataFrame(), ok=True, source="stub")
    tables: list[list[str]] = []

    def _run(sql, *_a, **kwargs):
        key = str(kwargs.get("key", ""))
        if key.startswith("cs_types_") and key.endswith("_fact"):
            if mart_frame is not None:
                return QueryResult(df=mart_frame.copy(), ok=True, source="mart stub")
            return QueryResult(df=pd.DataFrame(), ok=False, error="mart down")
        if key.startswith("cs_types_"):
            return QueryResult(df=_TYPES.copy(), ok=True, source="live stub")
        if drill_frame is not None and key.startswith(("cs_shapes_", "cs_users_")):
            return QueryResult(df=drill_frame.copy(), ok=True, source="drill stub")
        return empty

    real_styled = spend.styled_table

    def _styled(df, *a, **k):
        tables.append([str(c) for c in getattr(df, "columns", [])])
        return real_styled(df, *a, **k)

    monkeypatch.setattr("app.core.query.run", _run)
    monkeypatch.setattr(spend, "run", _run)
    monkeypatch.setattr(spend, "styled_table", _styled)
    monkeypatch.setattr(spend, "with_user_names", lambda df, *_a, **_k: df)
    monkeypatch.setattr(spend, "run_batch", lambda specs, **_k: {s["key"]: empty for s in specs})
    monkeypatch.setattr(spend, "load_settings", lambda *_a, **_k: {})
    monkeypatch.setattr(spend, "can_open", lambda _page: True)
    monkeypatch.setattr(spend, "request_navigation", lambda *_a, **_k: None)
    metering = QueryResult(df=pd.DataFrame({
        "DAY": [date(2026, 9, 29)], "SERVICE_TYPE": ["WAREHOUSE_METERING"], "CREDITS_USED": [100.0],
        "CREDITS_BILLED": [100.0], "CREDITS_ADJUSTMENT": [0.0]}), ok=True, source="metering stub")
    csr = QueryResult(df=pd.DataFrame({
        "WAREHOUSE_NAME": ["WH_A"], "COMPANY": ["ALFA"], "COMPUTE_CREDITS": [70.0],
        "CLOUD_SVC_CREDITS": [30.0], "TOTAL_CREDITS": [100.0], "CLOUD_SVC_PCT": [30.0],
        "STATUS": ["ELEVATED"]}), ok=True, source="csr stub")
    monkeypatch.setattr(spend, "_E5_TEST_ARGS", {
        "days": days, "bounds": bounds,
        "pre": {"metering_res": metering, "csr_res": csr, "coco_res": empty, "allin_res": empty,
                "napp_res": empty, "csfam_res": empty}}, raising=False)

    def _app():
        from app.ui.pages.cost_parts import spend as _spend
        _a = _spend._E5_TEST_ARGS
        _spend._spend_tab("ALL", _a["days"], 3.0, 3.0, bounds=_a["bounds"], **_a["pre"])

    try:
        at = AppTest.from_function(_app, default_timeout=60)
        if drill_frame is not None:
            at.session_state[f"cs_drill_toggle_ALL_{int(days)}"] = True
        at.run()
    finally:
        components._MART_FAIL_BACKOFF.clear()
    assert not at.exception, at.exception
    caps = [str(c.value) for c in at.caption]
    cs_caps = [c for c in caps if "Metadata storms show up here" in c]
    assert len(cs_caps) == 1, "the statement-type panel did not render"
    return cs_caps[0], caps, tables


def test_a_short_statement_mart_names_its_days_not_the_live_cap(monkeypatch):
    """Last 90 days, the mart answers with 65 covered days: the caption says the MART holds 65 of the 90 days
    (pre-fix: no COVERED_DAYS reached it; with one it read 'Scanned 65d ... (the live fallback caps its scan)')."""
    cap, _, tables = _render_spend(monkeypatch, days=90, bounds=None,
                                   mart_frame=_TYPES.assign(COVERED_DAYS=65))
    assert "The statement mart holds 65 of this window's 90 days" in cap
    assert "live fallback caps" not in cap and "Scanned" not in cap
    assert ["QUERY_TYPE", "QUERIES", "CS_CREDITS", "CS_CREDITS_PER_1K"] in tables     # COVERED_DAYS not shown


def test_a_full_statement_mart_and_the_live_clamp_keep_their_captions(monkeypatch):
    cap, _, _ = _render_spend(monkeypatch, days=90, bounds=None, mart_frame=_TYPES.assign(COVERED_DAYS=91))
    assert "statement mart holds" not in cap and "Scanned" not in cap
    cap, _, _ = _render_spend(monkeypatch, days=365, bounds=None, mart_frame=None)
    assert "Scanned 90d of the 365d window (the live fallback caps its scan)." in cap


@pytest.mark.parametrize("mart_up", [True, False])
def test_first_of_the_month_claims_no_scan_cap(monkeypatch, mart_up):
    """R2-056 (fixed by R1-213's span compare, locked here): Current month on Oct 1 is day OFFSET 0 with a 1-day
    span; neither leg capped anything, so no 'Scanned 1d of the 0d window' text."""
    from app.logic.date_windows import resolve_window_days, window_bounds
    today = date(2026, 10, 1)
    days, bounds = resolve_window_days("CURRENT_MONTH", today=today), window_bounds("CURRENT_MONTH", today=today)
    assert int(days) == 0 and bounds == (date(2026, 10, 1), date(2026, 10, 2))
    cap, _, _ = _render_spend(monkeypatch, days=days, bounds=bounds, mart_frame=_TYPES if mart_up else None)
    assert "Scanned" not in cap and "statement mart holds" not in cap


def test_the_shape_drill_says_how_many_days_the_mart_holds(monkeypatch):
    drill = pd.DataFrame({"QUERY_PARAMETERIZED_HASH": ["H"], "QUERY_TYPE": ["SHOW"], "SAMPLE_TEXT": ["show x"],
                          "RUNS": [10], "CS_CREDITS": [1.0], "CS_CREDITS_PER_1K": [100.0], "AVG_EXEC_S": [0.1],
                          "AVG_CACHE_PCT": [90], "USER_NAME": ["U"], "ROLE_NAME": ["R"], "COVERED_DAYS": [40]})
    _, caps, tables = _render_spend(monkeypatch, days=180, bounds=None, mart_frame=_TYPES.assign(COVERED_DAYS=181),
                                    drill_frame=drill)
    assert any("The statement mart holds 40 of this window's 180 days" in c
               and "the shape and user rankings" in c for c in caps), caps
    assert not any("COVERED_DAYS" in cols for cols in tables)


def test_the_billed_panel_says_how_many_days_the_mart_holds(monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    from app.ui.pages.cost_parts import spend

    c = _db()
    _young_mart(c)
    df = _query(c, mart_sql.cloud_svc_billed_families(7), 7)
    monkeypatch.setattr(spend, "_E5_BILLED", QueryResult(df=df, ok=True, source="billed stub"), raising=False)

    def _app():
        from app.ui.pages.cost_parts import spend as _spend
        _spend._cs_billed_families_panel("ALL", 7, 3.68, "", prefetched=_spend._E5_BILLED)

    at = AppTest.from_function(_app, default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    caps = " ".join(str(x.value) for x in at.caption)
    assert "The statement mart holds 4 of this window's 7 days" in caps
    assert "the metered comparison cover those days only" in caps
    tiles = " ".join(str(m.value) for m in at.markdown)
    assert "= 100% of" in tiles                                  # pre-fix data: "= 43% of"
