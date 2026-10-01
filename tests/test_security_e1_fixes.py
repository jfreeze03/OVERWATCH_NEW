"""Security round-2 fixes (cluster e1): coverage, KPI semantics, failed-read honesty, cache tiers, canaries.

Each test drives the real builder (executed through sqlglot -> SQLite where the arithmetic matters) or the
real page/panel function with recording fakes, and fails on the pre-fix tree (e504d089):

* R2-006  CHANGE RISK coverage accepts the extract loader's own STATUS stamp ('loader'), so it can be COMPLETE.
* R2-032  Trust Center 'Worsening scanners' counts REGRESSED scanners only, never a brand-new one.
* R2-054  Egress vs-prior on the 1st of the period (CalendarDayOffset(0)) is a 1-day span, not 30.
* R2-069  every ACCESS_HISTORY column the app reads is covered by a FAIL-on-drift canary.
* R2-079  a failed 90-day posture read renders 'unavailable' instead of vanishing.
* R2-080  the governance score names the read(s) that actually served it.
* R2-081  the service-account split note is worded by the failed read's KIND.
* R2-082  unload KPIs summed from the capped feed say they are lower bounds.
* R2-101  the admin-grant timing check reads on the 1h tier and names its source on the clean verdict.
* R2-102  SHOW SHARES / SHOW GRANTS TO SHARE read on the 5-minute tier.
* leads   the legacy FACT_LOGIN_DAILY gate counts complete days only; the live DDL feed shares the fact's clock.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest
import sqlglot

from app.core.result import QueryResult
from app.data import canary, security_sql
from app.logic.date_windows import CalendarDayOffset
from tests._source import ROOT

_SETUP = ("absent", "privilege", "unknown_function")
_FAILED = ("missing_column", "timeout", "other")


def _ok(df: pd.DataFrame | None = None, *, source: str = "") -> QueryResult:
    return QueryResult(df=pd.DataFrame() if df is None else df, ok=True, source=source)


def _failed(kind: str, *, source: str = "") -> QueryResult:
    return QueryResult(df=pd.DataFrame(), ok=False, error=f"boom ({kind})", error_kind=kind, source=source)


# ============================================================ fakes for page / panel renders ====

class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def __getattr__(self, _name):
        return lambda *_a, **_k: None


class _FakeSt:
    """Just enough streamlit: records captions; toggles are on, buttons are off, the rest no-ops."""

    def __init__(self):
        self.captions: list[str] = []
        self.session_state: dict = {}

    def caption(self, text="", *_a, **_k):
        self.captions.append(str(text))

    def toggle(self, *_a, **_k):
        return True

    def button(self, *_a, **_k):
        return False

    def expander(self, *_a, **_k):
        return _Ctx()

    def __getattr__(self, _name):
        return lambda *_a, **_k: None


def _harness(monkeypatch, module, results: dict, **extra):
    """Patch ``module``'s streamlit + render helpers with recorders. ``results`` maps a run() key PREFIX to a
    result (longest prefix wins); an unmatched key reads ok + empty. Every run() call's kwargs are kept."""
    fake = _FakeSt()
    seen: dict = {"runs": [], "empty": [], "detail": [], "kpis": [], "tables": [], "captions": fake.captions,
                  "result_captions": []}

    def fake_run(sql, *_a, key: str = "", **kw):
        seen["runs"].append({"key": key, "sql": sql, **kw})
        hits = sorted((p for p in results if key.startswith(p)), key=len, reverse=True)
        return results[hits[0]] if hits else _ok()

    def fake_empty(kind, msg, *_a, **k):
        seen["empty"].append((kind, msg))
        seen["detail"].append(k.get("detail"))

    patches = {
        "st": fake, "run": fake_run, "empty_state": fake_empty,
        "section_header": lambda *_a, **_k: None, "panel_help": lambda *_a, **_k: None,
        "result_caption": lambda res, *_a, **_k: seen["result_captions"].append(res),
        "kpi_row": lambda items, *_a, **_k: seen["kpis"].append(items),
        "styled_table": lambda df, *_a, **_k: seen["tables"].append(df),
        "entity_nav_table": lambda df, *_a, **_k: seen["tables"].append(df),
        "guard": lambda res, *_a, **_k: bool(res.ok and not res.empty),
        "with_user_names": lambda df, *_a, **_k: df.assign(USER=df.get("USER_NAME")),
        "snowsight_profile_column": lambda df, *_a, **_k: (df, {}),
        "methodology_note": lambda *_a, **_k: None,
        **extra,
    }
    for name, value in patches.items():
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value)
    return fake, seen


def _sec():
    import app.ui.pages.security as sec
    return sec


def _sc():
    import app.ui.security_center as sc
    return sc


def _kpi(seen: dict, label: str) -> dict:
    for row in seen["kpis"]:
        for item in row:
            if item.get("label") == label:
                return item
    raise AssertionError(f"no KPI {label!r} in {seen['kpis']}")


def _run_kw(seen: dict, key_prefix: str) -> dict:
    (call,) = [c for c in seen["runs"] if c["key"].startswith(key_prefix)]
    return call


# ===================================================== R2-006: CHANGE RISK coverage can be COMPLETE ====

_NOW = datetime(2026, 10, 1, 12, 0, 0)


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _coverage(fresh_rows: list[tuple[str, datetime, str]]) -> pd.DataFrame:
    """Execute security_domain_coverage() on SQLite at a pinned clock against SOURCE_FRESHNESS_STATE rows."""
    sql = (security_sql.security_domain_coverage()
           .replace("DATEADD('hour', -3, CURRENT_TIMESTAMP())", f"'{_ts(_NOW - timedelta(hours=3))}'")
           .replace("DATEADD('day', -2, CURRENT_DATE())", f"'{(_NOW.date() - timedelta(days=2)).isoformat()}'")
           .replace("DBA_MAINT_DB.OVERWATCH.", ""))
    assert "CURRENT_TIMESTAMP" not in sql and "CURRENT_DATE" not in sql     # every clock is pinned
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE SOURCE_FRESHNESS_STATE (SOURCE_NAME, SNAPSHOT_TS, STATUS)")
    con.executemany("INSERT INTO SOURCE_FRESHNESS_STATE VALUES (?, ?, ?)",
                    [(name, _ts(ts), status) for name, ts, status in fresh_rows])
    con.execute("CREATE TABLE MART_SECURITY_POSTURE_DAILY (DAY, METRIC, COMPANY, VALUE)")
    cur = con.execute(sqlglot.transpile(sql, read="snowflake", write="sqlite")[0])
    return pd.DataFrame(cur.fetchall(), columns=[c[0] for c in cur.description])


def _change_risk(frame: pd.DataFrame) -> str:
    return str(frame.loc[frame["DOMAIN"] == "CHANGE RISK", "COVERAGE"].iloc[0])


def _loader_rows(extract_age: timedelta | None, extract_status: str = "loader") -> list[tuple[str, datetime, str]]:
    rows = [("FACT_SECURITY_CHANGE", _NOW - timedelta(minutes=10), "OK"),
            ("SECURITY_TRUST_SNAPSHOT", _NOW - timedelta(minutes=10), "OK")]
    if extract_age is not None:
        rows.append(("OW_QH_EXTRACT", _NOW - extract_age, extract_status))
    return rows


def test_change_risk_is_complete_when_the_extract_stamp_is_the_loaders_own():
    """SP_LOAD_QH_EXTRACT stamps STATUS 'loader' (never 'OK'), so the old 'OK'-only test kept CHANGE RISK
    STALE forever: the domain could never score, and 'Who changed what' always paid the live fallback."""
    assert _change_risk(_coverage(_loader_rows(timedelta(minutes=5)))) == "COMPLETE"
    # the r31 guard is intact: a stalled or missing extract still keeps the fact from reading COMPLETE
    assert _change_risk(_coverage(_loader_rows(timedelta(hours=4)))) == "STALE"
    assert _change_risk(_coverage(_loader_rows(None))) == "STALE"
    # TRUST CENTER keeps its own 'OK' contract (its loader stamps 'OK')
    trust = _coverage(_loader_rows(timedelta(minutes=5)))
    assert trust.loc[trust["DOMAIN"] == "TRUST CENTER", "COVERAGE"].iloc[0] == "COMPLETE"


def test_a_complete_change_risk_domain_can_reach_act():
    from app.logic.security import domain_posture
    coverage = _coverage(_loader_rows(timedelta(minutes=5)))
    exceptions = pd.DataFrame([{"DOMAIN": "CHANGE RISK", "SEVERITY": "CRITICAL", "IMPACT_COUNT": 3}])
    (change,) = [p for p in domain_posture(exceptions, coverage) if p.domain == "CHANGE RISK"]
    assert change.coverage == "COMPLETE" and change.score is not None and change.state == "Act"


def _latest_qh_extract_definer() -> tuple[str, str]:
    """(file name, body) of the newest migration that CREATEs SP_LOAD_QH_EXTRACT."""
    hits = []
    for path in sorted((ROOT / "snowflake" / "migrations").glob("V*.sql"),
                       key=lambda p: int(re.match(r"V(\d+)", p.name).group(1))):
        text = path.read_text(encoding="utf-8")
        m = re.search(r"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.SP_LOAD_QH_EXTRACT\b", text)
        if m:
            hits.append((path.name, text[m.start():text.index("$$;", text.index("$$", m.start()) + 2)]))
    assert hits, "no SP_LOAD_QH_EXTRACT definer found"
    return hits[-1]


def test_accepted_extract_status_is_the_one_the_latest_definer_writes():
    """A re-derivation that changes the stamp must break here, not silently make CHANGE RISK unscoreable."""
    name, body = _latest_qh_extract_definer()
    merges = [m.start() for m in re.finditer(r"MERGE INTO DBA_MAINT_DB\.OVERWATCH\.SOURCE_FRESHNESS_STATE", body)]
    stamp = [body[i:body.index(";", i)] for i in merges if "'OW_QH_EXTRACT'" in body[i:body.index(";", i)]]
    assert len(stamp) == 1, name
    written = set(re.findall(r"STATUS = '([^']*)'", stamp[0]))
    written |= set(re.findall(r"VALUES \([^)]*,\s*'([^']*)'\)", stamp[0]))
    assert written and written <= set(security_sql.QHX_FRESH_STATUSES), (name, written)
    # the stamp is committed-only (inside IF (ok)), which is what makes a fresh SNAPSHOT_TS proof enough
    gate = body.index("IF (ok) THEN")
    assert gate < body.index(stamp[0]) < body.index("END IF", gate)
    qhx = security_sql.security_domain_coverage().split("qhx AS (", 1)[1].split("\n)", 1)[0]
    for status in written:
        assert f"'{status}'" in qhx.split("SELECT", 1)[1]


# ===================================================== R2-032: Worsening scanners = REGRESSED only ====

def _delta_row(scanner: str, current: int, prior: int | None, state: str, severity: str = "HIGH") -> dict:
    return {"SCANNER_ID": scanner, "SCANNER_NAME": scanner, "SEVERITY": severity, "CURRENT_COUNT": current,
            "PRIOR_COUNT": prior, "COUNT_DELTA": current - (prior or 0), "CHANGE_STATE": state,
            "SCANNED_AT": "2026-10-01", "SNAPSHOT_DAY": "2026-10-01"}


def _trust_tab(monkeypatch, rows: list[dict]) -> dict:
    sec = _sec()
    covered = _ok(pd.DataFrame([{"DOMAIN": "TRUST CENTER", "COVERAGE": "COMPLETE"}]))
    _, seen = _harness(monkeypatch, sec, {"trust_center_delta": _ok(pd.DataFrame(rows)),
                                          "sec_trust_coverage": covered})
    sec._trust_center_tab()
    return seen


def test_new_scanners_are_not_worsening(monkeypatch):
    """First snapshot day / a newly enabled scanner package: PRIOR_COUNT is NULL, COUNT_DELTA = CURRENT_COUNT."""
    seen = _trust_tab(monkeypatch, [_delta_row("A", 3, 3, "UNCHANGED"), _delta_row("B", 7, None, "NEW"),
                                    _delta_row("C", 2, None, "NEW"), _delta_row("D", 0, None, "NEW")])
    tile = _kpi(seen, "Worsening scanners")
    assert tile["value"] == "0"                                   # was 2 (every NEW scanner with entities)
    assert "+2 new scanners with entities at risk" in tile["sub"]
    assert "new, not worsening" in tile["help"]


def test_a_regressed_scanner_is_worsening(monkeypatch):
    seen = _trust_tab(monkeypatch, [_delta_row("A", 5, 3, "REGRESSED"), _delta_row("B", 1, 4, "IMPROVED")])
    tile = _kpi(seen, "Worsening scanners")
    assert tile["value"] == "1" and tile["sub"] == ""


# ===================================================== R2-054: day-0 offset is a 1-day egress span ====

def test_egress_baseline_on_the_first_of_the_period_is_one_day():
    sql = security_sql.egress_baseline(CalendarDayOffset(0))       # the 1st under Current month / Current year
    assert "DATEADD('day', -1, CURRENT_TIMESTAMP())" in sql
    assert "DATEADD('day', -2, CURRENT_TIMESTAMP())" in sql
    assert "1 AS BASELINE_DAYS" in sql
    assert "-30," not in sql and "-60," not in sql                 # was September vs August under an October label
    # the same as day 2 of the period, and the explicit default is still 30
    assert "1 AS BASELINE_DAYS" in security_sql.egress_baseline(CalendarDayOffset(1))
    assert "30 AS BASELINE_DAYS" in security_sql.egress_baseline()
    assert "30 AS BASELINE_DAYS" in security_sql.egress_baseline(None)  # type: ignore[arg-type]


# ===================================================== R2-069: ACCESS_HISTORY drift FAILs in the canary ====

_ACCESS_HISTORY_COLUMNS = ("QUERY_START_TIME", "BASE_OBJECTS_ACCESSED", "OBJECTS_MODIFIED",
                           "DIRECT_OBJECTS_ACCESSED", "USER_NAME", "QUERY_ID")


def _access_history_columns_read(sql: str) -> set[str]:
    """ACCESS_HISTORY columns a statement reads, through the alias it gives the view (or unqualified)."""
    cols: set[str] = set()
    for m in re.finditer(r"ACCOUNT_USAGE\.ACCESS_HISTORY(?:\s+(?:AS\s+)?([A-Za-z_]\w*))?", sql):
        alias = m.group(1)
        prefix = rf"\b{alias}\." if alias and alias.upper() not in ("WHERE", "JOIN", "ON", "GROUP") else r"(?<![\w.])"
        cols |= {c for c in _ACCESS_HISTORY_COLUMNS if re.search(prefix + c + r"\b", sql)}
    return cols


def test_every_access_history_column_the_app_reads_has_a_fail_on_drift_canary():
    from app.data import graph_sql, insights_sql, workbench_sql
    readers = (security_sql.access_evidence_days(), security_sql.grant_scope_usage(90),
               security_sql.unused_table_grants(90), graph_sql.object_blast_consumers(("DB.S.T",)),
               workbench_sql.product_consumer_reads(30, "ALFA"), insights_sql.object_reads_confirm(("DB.S.T",), 7))
    app_reads = set().union(*(_access_history_columns_read(sql) for sql in readers))
    assert {"QUERY_START_TIME", "BASE_OBJECTS_ACCESSED", "OBJECTS_MODIFIED", "DIRECT_OBJECTS_ACCESSED",
            "USER_NAME"} <= app_reads
    canaried = {name: fn() for name, fn in canary.CANARIES if "ACCOUNT_USAGE.ACCESS_HISTORY" in fn()}
    assert canaried, "no canary reads ACCESS_HISTORY"                # was: none, by a stale Standard-edition rule
    covered = set().union(*(_access_history_columns_read(sql) for sql in canaried.values()))
    assert app_reads <= covered, app_reads - covered
    for name, sql in canaried.items():
        assert name not in canary.EXPECTED_GAPS, name               # Enterprise account: absence is a FAIL
        sqlglot.parse_one(sql, read="snowflake")


# ===================================================== R2-079: failed posture read is not silent ====

@pytest.mark.parametrize("kind", _SETUP + _FAILED)
def test_posture_trend_panel_renders_a_failed_read_by_its_kind(monkeypatch, kind):
    sec = _sec()
    _, seen = _harness(monkeypatch, sec, {})
    sec._posture_trend_panel(_failed(kind))
    ((state, msg),) = seen["empty"]
    if kind in _SETUP:
        assert state == "needs_setup"
    else:
        assert state == "unavailable" and "could not be read" in msg and seen["detail"] == [f"boom ({kind})"]


def test_posture_trend_panel_says_an_empty_mart_has_no_history_yet(monkeypatch):
    sec = _sec()
    _, seen = _harness(monkeypatch, sec, {})
    sec._posture_trend_panel(_ok())
    assert seen["empty"] == [("no_data_yet", "Posture trend unlocks after the daily posture loader has run "
                                             "(MART_SECURITY_POSTURE_DAILY is empty).")]


# ===================================================== R2-080: governance names the path that served ====

_POSTURE_SRC = "MART_SECURITY_POSTURE_DAILY (daily post-06:45 snapshot, 90d shared)"
_COUNTS_SRC = "USERS + CREDENTIALS + GRANTS_TO_USERS (live fallback)"
_SHOW_WH_SRC = "SHOW WAREHOUSES (pre-V041 fallback)"


def _gov(monkeypatch, results: dict) -> dict:
    sec = _sec()
    _, seen = _harness(monkeypatch, sec, results, load_settings=lambda *_a, **_k: {})
    sec._governance_score_panel()
    return seen


def _source_captions(seen: dict) -> list[str]:
    return [c for c in seen["captions"] if "Source: " in c]


def test_live_fallback_score_names_the_fallback_not_the_failed_mart(monkeypatch):
    counts = _ok(pd.DataFrame([{"MFA_GAP_USERS": None, "EXPIRED_CREDENTIALS": 2, "EXPIRING_CREDENTIALS": 1,
                                "BREAKGLASS_GRANTS_30D": 0}]), source=_COUNTS_SRC)
    whs = _ok(pd.DataFrame([{"name": "WH_A", "auto_suspend": 60}, {"name": "WH_B", "auto_suspend": 0}]),
              source=_SHOW_WH_SRC)
    seen = _gov(monkeypatch, {"gov_posture": _failed("timeout", source=_POSTURE_SRC), "gov_counts": counts,
                              "gov_show_wh": whs})
    (caption,) = _source_captions(seen)
    assert caption.startswith("Incomplete: MFA gaps")                 # the ceiling caption still leads
    assert _COUNTS_SRC in caption and _SHOW_WH_SRC in caption
    assert "MART_SECURITY_POSTURE_DAILY" not in caption               # the read that FAILED is never credited


def test_complete_score_names_its_source(monkeypatch):
    posture = pd.DataFrame([{"DAY": "2026-09-30", "METRIC": m, "COMPANY": "ALL", "VALUE": v}
                            for m, v in (("MFA_GAP_USERS", 1), ("EXPIRED_CRED", 0), ("EXPIRING_CRED_10D", 0),
                                         ("BREAKGLASS_GRANTS_30D", 0), ("WH_NO_AUTOSUSPEND", 0))])
    seen = _gov(monkeypatch, {"gov_posture": _ok(posture, source=_POSTURE_SRC)})
    assert _source_captions(seen) == [f"Source: {_POSTURE_SRC}."]   # was: no source line at all
    assert [c["key"] for c in seen["runs"]] == ["gov_posture"]


# ===================================================== R2-081: service-account split note by kind ====

@pytest.mark.parametrize("kind", _SETUP + _FAILED)
def test_service_split_note_follows_the_failure_kind(monkeypatch, kind):
    sec = _sec()
    _, seen = _harness(monkeypatch, sec, {"service_users": _failed(kind)})
    names, error_kind = sec._service_user_names()
    assert names is None and error_kind == kind
    ranked = pd.DataFrame([{"USER_NAME": "U1", "ACCOUNT_BAND": "Person", "SEVERITY": "High"}])
    sec._banded_user_tables(ranked, ["SEVERITY", "USER_NAME"], key="k", service_names=names,
                            service_error_kind=error_kind)
    (note,) = [c for c in seen["captions"] if c.startswith("Service-account split unavailable")]
    if kind in _SETUP or kind == "missing_column":
        assert "USERS.TYPE isn't readable here" in note
    else:                                                             # a transient failure is not an absence
        assert "USERS.TYPE" not in note and "read failed" in note


def test_service_split_has_no_note_when_the_read_served(monkeypatch):
    sec = _sec()
    _, seen = _harness(monkeypatch, sec, {"service_users": _ok(pd.DataFrame({"USER_NAME": ["SVC_ETL"]}))})
    names, error_kind = sec._service_user_names()
    assert names == {"SVC_ETL"} and error_kind == ""
    ranked = pd.DataFrame([{"USER_NAME": "SVC_ETL", "ACCOUNT_BAND": "Service", "SEVERITY": "Low"}])
    sec._banded_user_tables(ranked, ["SEVERITY", "USER_NAME"], key="k", service_names=names)
    assert not [c for c in seen["captions"] if "split unavailable" in c]


# ===================================================== R2-082: capped-feed fallback is a lower bound ====

def _unload_feed(n: int) -> pd.DataFrame:
    return pd.DataFrame([{"DAY": "2026-09-30", "USER_NAME": f"U{i % 10}", "ROLE_NAME": "R", "UNLOADS": 1,
                          "GB_OUT": 1.0, "LAST_UNLOAD": "2026-09-30 01:00:00", "TARGET_LOCATION": "@s",
                          "SAMPLE_TARGET": "COPY INTO @s", "QUERY_ID": f"q{i}"} for i in range(n)])


def _egress(monkeypatch, feed_rows: int) -> dict:
    sec = _sec()
    _, seen = _harness(monkeypatch, sec, {"unload_tot_": _failed("timeout"),
                                          "unload_": _ok(_unload_feed(feed_rows))})
    sec._egress_tab("ALFA", 7)
    return seen


def test_unload_kpis_from_a_full_capped_feed_are_lower_bounds(monkeypatch):
    seen = _egress(monkeypatch, security_sql.UNLOAD_FEED_LIMIT)
    for label in ("Unload runs", "GB written out", "Users unloading"):
        tile = _kpi(seen, label)
        assert str(tile["value"]).startswith("≥ ") and "Lower bound" in tile["help"], label
    assert _kpi(seen, "Unload runs")["value"] == f"≥ {security_sql.UNLOAD_FEED_LIMIT}"
    assert any("Window totals unavailable (boom (timeout))" in c and "lower bounds" in c for c in seen["captions"])


def test_unload_kpis_below_the_cap_are_exact(monkeypatch):
    seen = _egress(monkeypatch, 12)
    assert _kpi(seen, "Unload runs")["value"] == "12" and not _kpi(seen, "Unload runs")["help"]
    assert not [c for c in seen["captions"] if "Window totals unavailable" in c]


def test_unload_feed_limit_is_the_builders_cap():
    assert f"LIMIT {security_sql.UNLOAD_FEED_LIMIT}\n" in security_sql.unload_activity(7, "ALFA")


# ===================================================== R2-101 / R2-102: cache tiers ====

def test_admin_grant_timing_check_reads_hourly_and_names_its_clean_source(monkeypatch):
    sc = _sc()
    _, seen = _harness(monkeypatch, sc, {"sec_admin_grant_ctx_": _ok()})
    sc.render_admin_grant_anomalies("ALL")
    call = _run_kw(seen, "sec_admin_grant_ctx_")
    assert call["tier"] == "hourly"                                    # was the 4h metadata tier
    assert [k for k, _ in seen["empty"]] == ["clean"]
    assert len(seen["result_captions"]) == 1                          # the clean verdict names its read


def test_share_inventory_and_drill_read_on_the_recent_tier(monkeypatch):
    sec = _sec()
    shares = _ok(pd.DataFrame([{"kind": "OUTBOUND", "name": "S1", "to": "ACCT_A,ACCT_B",
                                "listing_global_name": ""}]))
    _, seen = _harness(monkeypatch, sec, {"sec_shares": shares, "sec_share_grants_": _ok()},
                       selectable_table=lambda *_a, **_k: 0)
    sec._exposure_tab()
    assert _run_kw(seen, "sec_shares")["tier"] == "recent"            # SHOW SHARES is real time
    assert _run_kw(seen, "sec_share_grants_")["tier"] == "recent"


# ===================================================== leads ====

def _legacy_coverage(present: set[date], today: date) -> SimpleNamespace:
    sql = (security_sql.login_fact_coverage(30)
           .replace("DATEADD('day', -30, CURRENT_DATE())", f"'{(today - timedelta(days=30)).isoformat()}'")
           .replace("CURRENT_DATE()", f"'{today.isoformat()}'")
           .replace("DBA_MAINT_DB.OVERWATCH.", ""))
    assert "CURRENT_" not in sql and "DATEADD" not in sql
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE FACT_LOGIN_DAILY (DAY, LOAD_TS)")
    con.executemany("INSERT INTO FACT_LOGIN_DAILY VALUES (?, ?)", [(d.isoformat(), "x") for d in sorted(present)])
    cur = con.execute(sqlglot.transpile(sql, read="snowflake", write="sqlite")[0])
    frame = pd.DataFrame(cur.fetchall(), columns=[c[0] for c in cur.description])
    return SimpleNamespace(ok=True, df=frame, usable=lambda: not frame.empty)


def test_legacy_login_fact_gate_rejects_one_interior_hole_with_today_loaded(monkeypatch):
    """FACT_LOGIN_DAILY's daily load writes today's partial partition; counting it let a loaded today stand
    in for one missing interior day (30 of 31), so the MFA / auth-readiness evidence passed over the hole."""
    from app.logic import security as logic
    today = date(2026, 10, 1)
    monkeypatch.setattr(logic, "account_today", lambda: today)
    dense = {today - timedelta(days=i) for i in range(40)}
    assert logic.fact_coverage_complete(_legacy_coverage(dense, today), 30)
    assert logic.fact_coverage_complete(_legacy_coverage(dense - {today}, today), 30)   # before today's load
    holed = dense - {today - timedelta(days=12)}
    assert not logic.fact_coverage_complete(_legacy_coverage(holed, today), 30)


def test_live_ddl_fallback_shares_the_fact_twins_clock():
    """The fact twin rolls from CURRENT_TIMESTAMP; the live fallback anchored on CURRENT_DATE's midnight and so
    served up to a day more under the same 'last N days' label."""
    for days in (7, 30, 90):
        live = security_sql.recent_ddl_changes(days, "ALL")
        rollup = security_sql.recent_ddl_changes_rollup(days, "ALL")
        fact = security_sql.recent_ddl_changes_fact(days, "ALL")
        assert f"f.EVENT_TS >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())" in fact
        for sql in (live, rollup):
            assert f"START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())" in sql
            assert f"START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())" not in sql
