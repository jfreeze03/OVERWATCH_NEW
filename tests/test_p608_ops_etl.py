"""PR-3 (cluster e2-ops-etl, v4.608.0) locks: Operations / ETL defects confirmed by the round-2 review.

Each test names its finding id (R2-nnn) or the merge-notes lead it closes. Render tests drive the real page
function with recording fakes (the tests/test_ops_c01_p606.py pattern); SQL locks render the builder.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from tests._source import read

_OPS = "app/ui/pages/operations.py"


def _ok(df: pd.DataFrame):
    return SimpleNamespace(ok=True, empty=df.empty, df=df, error="", error_kind="", truncated=False,
                           cache_hit=False, usable=lambda: not df.empty)


def _failed(kind: str = "timeout"):
    return SimpleNamespace(ok=False, empty=True, df=pd.DataFrame(), error=f"boom ({kind})", error_kind=kind,
                           truncated=False, cache_hit=False, usable=lambda: False)


class _RecSt:
    """A recording stand-in for streamlit: captions/markdown/code, inert inputs with scripted returns."""

    def __init__(self, *, toggles: bool = True, selectbox=None, text_input=None):
        from contextlib import contextmanager

        import streamlit as _real_st
        self.column_config = _real_st.column_config      # pure config builders, safe outside a script run
        self.calls: list[tuple[str, str]] = []
        self.session_state: dict = {}
        self._toggles = toggles
        self._selectbox = selectbox
        self._text_input = text_input or {}

        @contextmanager
        def _cm(label="", *_a, **_k):
            self.calls.append(("cm", str(label)))
            yield self
        self.expander = _cm
        self.spinner = _cm

    def __getattr__(self, name):           # any other st.* call: record and return an inert value
        def _call(*a, **_k):
            self.calls.append((name, str(a[0]) if a else ""))
            return None
        return _call

    def caption(self, text, *_a, **_k):
        self.calls.append(("caption", str(text)))

    def code(self, text, *_a, **_k):
        self.calls.append(("code", str(text)))

    def toggle(self, *_a, **_k):
        return self._toggles

    def columns(self, n, *_a, **_k):
        from contextlib import nullcontext
        return [nullcontext() for _ in range(n if isinstance(n, int) else len(n))]

    def selectbox(self, label, options, *_a, key: str = "", **_k):
        self.calls.append(("selectbox", f"{key}:{list(options)}"))
        if callable(self._selectbox):
            return self._selectbox(label, list(options), key)
        return next(iter(options), None)

    def text_input(self, label, *_a, key: str = "", **_k):
        self.calls.append(("text_input", key))
        return self._text_input.get(key, "")

    def number_input(self, *_a, value=0.0, **_k):
        return value

    def radio(self, _label, options, *_a, **_k):
        return next(iter(options))

    def button(self, *_a, **_k):
        return False

    def text(self, kind: str) -> str:
        return "\n".join(t for k, t in self.calls if k == kind)


def _page(monkeypatch, run_fn, *, st=None, **extra):
    """Patch operations' st / run / empty_state / render helpers with recorders."""
    from app.ui.pages import operations as ops
    fake = st or _RecSt()
    seen: dict = {"runs": [], "empty": [], "detail": [], "kpis": [], "tables": [], "headers": []}

    def fake_run(sql, *_a, key: str = "", **kw):
        seen["runs"].append(key)
        return run_fn(sql, key=key, **kw)

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


# ------------------------------- R2-008 / R2-113: a daily mart's zero is not a verified 7-day all-clear ----

def test_failure_timeline_scans_live_when_the_zero_came_from_the_daily_mart(monkeypatch):
    # FACT_TASK_DAILY loads once a day (~06:45 CT): its 0 says nothing about a 09:30 failure. The section
    # used to render a green 'clean' ("per the hourly task mart") and never issue the live scan.
    ops, _fake, seen = _page(monkeypatch, lambda *_a, **_k: _failed("timeout"))
    ops._failure_timeline_section("ALL", known_failures=0.0, known_from_live=False)
    assert seen["runs"] == ["t_rca_ALL"]                                  # the live 7-day scan ran
    assert [k for k, _m in seen["empty"]] == ["unavailable"]              # ...and its failure shows
    assert all(k != "clean" for k, _m in seen["empty"])


def test_failure_timeline_live_zero_still_skips_the_scan(monkeypatch):
    # the perf short-circuit survives for a count as fresh as the scan (the live task_runs fallback)
    ops, _fake, seen = _page(monkeypatch, lambda *_a, **_k: pytest.fail("the live zero must skip the scan"))
    ops._failure_timeline_section("ALL", known_failures=0.0, known_from_live=True)
    assert seen["runs"] == []
    assert seen["empty"] == [("clean", "No task failures in the last 7 days for this scope.")]


def test_task_health_mart_zero_reaches_the_live_failure_scan(monkeypatch):
    # end to end: the Tasks landing (7d, mart usable, TOTAL_FAILED_WIN = 0) must issue the t_rca_ read
    mart = pd.DataFrame({"DATABASE_NAME": ["DB"], "SCHEMA_NAME": ["S"], "TASK_NAME": ["TASK_X"],
                         "RUNS": [24], "FAILED": [0], "TOTAL_RUNS_WIN": [168], "TOTAL_FAILED_WIN": [0]})
    failure = pd.DataFrame({
        "DATABASE_NAME": ["DB"], "SCHEMA_NAME": ["S"], "TASK_NAME": ["TASK_X"], "ROOT_TASK_ID": ["r1"],
        "GRAPH_RUN_GROUP_ID": ["g1"], "QUERY_START_TIME": pd.to_datetime(["2026-10-01 09:30"]),
        "RUN_SEC": [4.0], "ERROR_CODE": ["003001"], "ERROR_MESSAGE": ["Insufficient privileges"],
        "TOTAL_FAILURES_WIN": [1], "REPEAT_FAILURE": [0]})

    class _Done(Exception):
        pass

    def run_fn(_sql, *, key, **_k):
        if key.startswith("t_fact_"):
            return _ok(mart.copy())
        if key.startswith("t_rca_"):
            return _ok(failure.copy())
        raise AssertionError(key)

    def stop(*_a, **_k):
        raise _Done

    ops, _fake, seen = _page(monkeypatch, run_fn, guard=lambda *_a, **_k: True, _incident_routing_panel=stop)
    with pytest.raises(_Done):                       # stop after the timeline body rendered
        ops._task_health_view("ALL", 7)
    assert seen["runs"] == ["t_fact_ALL_7", "t_rca_ALL"]
    assert ("Failure root-cause timeline (7d)", "warn") in seen["headers"]
    assert all(k != "clean" for k, _m in seen["empty"])
    assert "hourly task mart" not in read(_OPS)


# ------------- PR-1 lead: Built-in objectives' "Tasks on cadence" uses the Tasks ▸ SLA cap rule ----

def _fresh_frame(n: int, total: int, *, last_mins: float = 10.0) -> pd.DataFrame:
    """task_freshness_sla rows: ``n`` on-time tasks (cadence 60 min), TOTAL_TASKS = ``total``; the last row
    read has been silent ``last_mins`` minutes (inside its 60-minute yard unless larger)."""
    mins = [10.0] * (n - 1) + [last_mins]
    return pd.DataFrame({"DATABASE_NAME": ["DB"] * n, "SCHEMA_NAME": ["S"] * n,
                         "TASK_NAME": [f"T{i:03d}" for i in range(n)], "MEDIAN_GAP_MIN": [60.0] * n,
                         "LONG_GAP_MIN": [60.0] * n, "INTERVALS": [20] * n, "MINS_SINCE_SUCCESS": mins,
                         "TOTAL_TASKS": [total] * n})


@pytest.mark.parametrize(("rows", "total", "last_mins", "health", "delta_tail"), [
    (200, 200, 10.0, "ok", ""),                       # exactly 200 tasks: the whole set, not a cap
    (200, 350, 10.0, "ok", " · 200 of 350 read"),     # capped, but the cut is proven: still green
    (200, 350, 70.0, "", " · 200 of 350 read"),       # capped and the last one read is past its gap
])
def test_tasks_on_cadence_tile_withholds_green_only_for_an_unproven_cut(monkeypatch, rows, total, last_mins,
                                                                        health, delta_tail):
    fresh = _fresh_frame(rows, total, last_mins=last_mins)
    ops, fake, seen = _page(monkeypatch, lambda *_a, **_k: pytest.fail("no single read here"),
                            run_batch=lambda *_a, **_k: {"fresh": _ok(fresh)},
                            load_settings=lambda *_a, **_k: {})
    ops._builtin_objectives_panel({}, "ALL", 14)
    assert seen["headers"] == [("Built-in objectives", health)]
    ((_cyc, cad),) = seen["kpis"]
    assert cad["severity"] == health
    assert cad["delta"] == "0 late · 0 stale" + delta_tail
    proven = "every task below the read is on time too" in fake.text("caption")
    assert proven is (total > rows and health == "ok")


# -------------------- R2-099: the posture verdict and script say their SHOW values may be up to 4h old ----

def test_posture_script_and_kpi_disclose_the_cached_show_values(monkeypatch):
    # a stale cached (172800, '') tuple reads Uncapped and scripts an ALTER that would LOOSEN a newer 1800s cap;
    # the panel never said the values could be hours old
    tail = pd.DataFrame({"WAREHOUSE_NAME": ["WH_X"], "COMPLETED_RUNS": [500], "P99_ELAPSED_SEC": [2000.0],
                         "MAX_ELAPSED_SEC": [2500.0], "TIMEOUT_CANCELLED_RUNS": [0],
                         "TIMEOUT_CANCELLED_TOTAL": [0]})
    show_wh = pd.DataFrame({"name": ["WH_X"]})
    show_param = pd.DataFrame({"key": ["STATEMENT_TIMEOUT_IN_SECONDS"], "value": ["172800"], "level": [""]})

    def run_fn(_sql, *, key, **_k):
        if key.startswith("ops_wh_timeout_tail_"):
            return _ok(tail.copy())
        if key == "jump_wh":
            return _ok(show_wh.copy())
        return _ok(show_param.copy())

    ops, fake, _seen = _page(monkeypatch, run_fn, md_dollars=lambda s: s)
    ops._stmt_timeout_posture_panel("ALL", 30)
    script = fake.text("code")
    assert "ALTER WAREHOUSE WH_X SET STATEMENT_TIMEOUT_IN_SECONDS" in script        # the Uncapped row is scripted
    assert script.startswith("-- Values read from SHOW PARAMETERS cached for up to 4h.")
    assert "SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN WAREHOUSE <name>" in script
    assert ("Timeout values come from SHOW PARAMETERS cached for up to 4h (Refresh data in the sidebar re-reads "
            "them): a cap set since that read still shows as Uncapped here.") in fake.text("caption")


# ----------------------- R2-100: a warehouse created since the cached SHOW WAREHOUSES can still be targeted ----

def test_emergency_lever_takes_a_typed_warehouse_over_the_cached_list(monkeypatch):
    fake = _RecSt(text_input={"emg_wh_txt": " wh_adhoc "})
    ops, fake, _seen = _page(monkeypatch, lambda *_a, **_k: _ok(pd.DataFrame({"name": ["WH_A"]})), st=fake)
    ops._emergency_tab(False)                                  # viewer: the preview shows the statement
    assert "selectbox" in [k for k, _t in fake.calls]           # the cached list is still offered
    assert fake.text("code") == "ALTER WAREHOUSE WH_ADHOC SUSPEND;"


def test_emergency_lever_keeps_the_pick_when_nothing_is_typed(monkeypatch):
    ops, fake, _seen = _page(monkeypatch, lambda *_a, **_k: _ok(pd.DataFrame({"name": ["WH_A"]})))
    ops._emergency_tab(False)
    assert fake.text("code") == "ALTER WAREHOUSE WH_A SUSPEND;"
    assert ("text_input", "emg_wh_txt") in fake.calls            # the type-in box is always there


def test_kill_switch_inspects_a_typed_warehouse(monkeypatch):
    fake = _RecSt(text_input={"emg_rq_wh_txt": "wh_adhoc"})

    def run_fn(sql, *, key, **_k):
        if key == "emg_show_wh":
            return _ok(pd.DataFrame({"name": ["WH_A"]}))
        assert "WAREHOUSE_NAME => 'WH_ADHOC'" in sql
        return _ok(pd.DataFrame())

    ops, fake, seen = _page(monkeypatch, run_fn, st=fake)
    ops._emergency_extras(True)
    assert seen["runs"] == ["emg_show_wh", "emg_running_WH_ADHOC"]
    assert seen["empty"] == [("clean", "Nothing running or queued right now.")]


# ------------------------- R2-109: a ref-gap check the PIPE_REF_GAP alert would drop is named, not silent ----

def _latest_definer(proc: str) -> str:
    """The text of the newest migration that CREATEs ``proc`` (the current definition)."""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[1] / "snowflake" / "migrations"
    hits = sorted((int(re.match(r"V(\d+)__", p.name).group(1)), p) for p in root.glob("V*__*.sql")
                  if f"PROCEDURE DBA_MAINT_DB.OVERWATCH.{proc}(" in p.read_text(encoding="utf-8"))
    assert hits, proc
    return hits[-1][1].read_text(encoding="utf-8")


def test_alert_name_allowlist_matches_the_current_sp_scan_ref_gaps():
    import re

    from app.data import etl_control_sql as etl
    body = _latest_definer("SP_SCAN_REF_GAPS")
    (pattern,) = re.findall(r"RLIKE\(nm_clean, '([^']*)'\)", body)
    assert pattern == etl.ALERT_NAME_PATTERN


@pytest.mark.parametrize("name", ["cc_coverage(type).code", "pc#type.code", "p+c", "P&C.code", "code$x",
                                  "naïve.code", "o'brien"])
def test_parse_warns_when_the_alert_would_skip_a_check(name):
    from app.data import etl_control_sql as etl
    checks, warns = etl.parse_ref_gap_checks(f"{name} | DB.SCH.T | CODE")
    assert [c.name for c in checks] == [name]                 # still scanned by the panel
    assert not checks[0].alerted
    assert warns == [f"Check {name!r} is scanned here, but the daily PIPE_REF_GAP alert skips it: alerted "
                     "check names may use only letters, digits, spaces and - _ . : /."]


@pytest.mark.parametrize("name", ["pc_uwissuetype.code", "a-b/c:d e.f", "*pinned.code"])
def test_parse_stays_quiet_for_names_the_alert_scans(name):
    from app.data import etl_control_sql as etl
    checks, warns = etl.parse_ref_gap_checks(f"{name} | DB.SCH.T | CODE")
    assert warns == [] and checks[0].alerted


def test_ref_gap_caption_names_the_checks_the_alert_skips(monkeypatch):
    gaps = pd.DataFrame({"CHECK_NAME": ["p+c", "pc_uwissuetype.code"], "NEW_CODE": ["X1", "X2"]})
    raw = "p+c | DB.SCH.T | CODE\npc_uwissuetype.code | DB.SCH.T2 | CODE"
    ops, fake, _seen = _page(monkeypatch, lambda *_a, **_k: _ok(gaps.copy()), guard=lambda *_a, **_k: True,
                             load_settings=lambda *_a, **_k: {"ETL_REF_GAP_XLAT": "DB.SCH.XLAT",
                                                              "ETL_REF_GAP_CHECKS": raw})
    ops._reference_gap_panel()
    captions = fake.text("caption")
    assert "⚠ Check 'p+c' is scanned here, but the daily PIPE_REF_GAP alert skips it" in captions
    assert ("The daily PIPE_REF_GAP alert (once installed) pages on the same gap, except for 'p+c', whose "
            "name falls outside the alert's allowed characters (see the warning above).") in captions


# ------------- R2-063 / R2-065 / R2-066 / R2-067: the Operations / ETL readers have a drift alarm ----

def _new_canaries() -> dict:
    """name -> (the exact builder text the registry must render, tokens only that read covers)."""
    from datetime import timedelta

    from app.data import chatter_sql, graph_sql, insights_sql, ops_sql
    from app.logic.formulas import account_today
    _ident = {"user_contains": "X", "database": "DBA_MAINT_DB", "schema_contains": "X"}
    return {
        "ops.operator_stats_summary": (ops_sql.operator_stats_summary(1, "ALFA", "WH", **_ident),
                                       ("FACT_QUERY_OPERATOR_STATS_DAILY", "USER_NAME", "DATABASE_NAME",
                                        "SCHEMA_NAME", "REMOTE_SPILL_GB")),
        "ops.operator_problem_board": (ops_sql.operator_problem_board(1, "ALFA", "WH", **_ident),
                                       ("FACT_QUERY_OPERATOR_STATS_DAILY", "USER_NAME", "ROW_MULTIPLE")),
        "ops.operator_anatomy": (ops_sql.operator_anatomy("canary-probe"),
                                 ("FACT_QUERY_OPERATOR_STATS_DAILY", "PARENT_OPERATOR_ID")),
        "ops.table_pruning_candidates": (ops_sql.table_pruning_candidates(1, "ALFA"),
                                         ("TABLE_PRUNING_HISTORY",)),
        "ops.query_opportunity_fingerprints": (ops_sql.query_opportunity_fingerprints(1, "ALFA"),
                                               ("BYTES_SPILLED_TO_LOCAL_STORAGE",)),
        "ops.running_queries": (ops_sql.running_queries("WH_ALFA_ADMIN"), ("QUERY_HISTORY_BY_WAREHOUSE",)),
        "insights.query_insights_feed": (insights_sql.query_insights_feed(1),
                                         ("ACCOUNT_USAGE.QUERY_INSIGHTS", "INSIGHT_TYPE_ID")),
        "insights.table_retention_live": (insights_sql.table_retention_live("DBA_MAINT_DB", "OVERWATCH",
                                                                            "SETTINGS"),
                                          ("INFORMATION_SCHEMA.TABLES", "RETENTION_TIME")),
        "insights.table_storage_breakdown": (insights_sql.table_storage_breakdown("ALFA"),
                                             ("RETAINED_FOR_CLONE_BYTES",)),
        "insights.query_detail": (insights_sql.query_detail(
            "00000000-0000-0000-0000-000000000000", (account_today() - timedelta(days=1)).isoformat()),
            ("BYTES_SPILLED_TO_LOCAL_STORAGE", "START_TIME >= DATEADD('day', -1,")),
        "graph.object_dependency_edges": (graph_sql.object_dependency_edges(1),
                                          ("ACCOUNT_USAGE.OBJECT_DEPENDENCIES", "REFERENCING_OBJECT_DOMAIN")),
        "chatter.by_application": (chatter_sql.chatter_by_application(1), ("IS_CLIENT_GENERATED_STATEMENT",)),
        "chatter.families_for_application": (chatter_sql.chatter_families_for_application("canary-probe", 1),
                                              ("ACCOUNT_USAGE.SESSIONS",)),
    }


def test_operations_and_etl_readers_are_registered_canaries():
    import sqlglot

    from app.data.canary import CANARIES, EXPECTED_GAPS
    reg = dict(CANARIES)
    rendered = "\n".join(b() for _n, b in CANARIES)
    for name, (sql, tokens) in _new_canaries().items():
        assert name in reg, name
        assert reg[name]() == sql, name
        sqlglot.parse_one(sql, read="snowflake")
        for token in tokens:
            assert token in sql, (name, token)
        # only QUERY_INSIGHTS (an optional view) may read GAP; every other absence is a FAIL
        assert (name in EXPECTED_GAPS) is (name == "insights.query_insights_feed"), name
    for token in ("FACT_QUERY_OPERATOR_STATS_DAILY", "TABLE_PRUNING_HISTORY", "QUERY_HISTORY_BY_WAREHOUSE",
                  "BYTES_SPILLED_TO_LOCAL_STORAGE", "ACCOUNT_USAGE.QUERY_INSIGHTS", "RETAINED_FOR_CLONE_BYTES",
                  "ACCOUNT_USAGE.OBJECT_DEPENDENCIES", "IS_CLIENT_GENERATED_STATEMENT"):
        assert token in rendered, token


def test_canary_fails_the_drift_the_probe_reads_never_log(monkeypatch):
    from tests.test_probe_read_honesty import _run_canary_tab
    names = list(_new_canaries())
    drift = _run_canary_tab(monkeypatch, dict.fromkeys(names, "missing_column"))
    assert {drift[n] for n in names} == {"FAIL"}
    absent = _run_canary_tab(monkeypatch, dict.fromkeys(names, "absent"))
    assert {n: absent[n] for n in names} == {n: ("GAP" if n == "insights.query_insights_feed" else "FAIL")
                                             for n in names}


# Builders from this cluster's data modules that a page reads with probe=True (a probe read logs neither an
# absent object nor a missing column) and that are NOT canaried, each with the reason. Anything else read with
# probe=True must be registered: house law 4, and the only drift alarm such a read has.
_PROBE_EXEMPT = {
    "ops_sql.overwatch_task_states": "SHOW TASKS: SHOW cannot be EXPLAINed (its run-summary twin is canaried)",
    "ops_sql.warehouse_stmt_timeout_sql": "SHOW PARAMETERS: SHOW cannot be EXPLAINed",
    "ops_sql.account_stmt_timeout_sql": "SHOW PARAMETERS: SHOW cannot be EXPLAINed",
    "graph_sql.object_blast_consumers": "ACCESS_HISTORY reader: its canary coverage is R2-069 (security cluster)",
}


def test_every_probe_read_of_an_ops_etl_builder_is_canaried_or_exempt():
    import ast
    import pathlib

    from app.data import canary
    mods = ("ops_sql", "insights_sql", "graph_sql", "chatter_sql", "etl_control_sql")
    canary_src = read("app/data/canary.py")
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    probes: set[str] = set()
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and node.args and any(
                    k.arg == "probe" and isinstance(k.value, ast.Constant) and k.value.value is True
                    for k in node.keywords)):
                continue
            first = node.args[0]
            if (isinstance(first, ast.Call) and isinstance(first.func, ast.Attribute)
                    and isinstance(first.func.value, ast.Name) and first.func.value.id in mods):
                probes.add(f"{first.func.value.id}.{first.func.attr}")
    assert "ops_sql.operator_stats_summary" in probes and "graph_sql.object_dependency_edges" in probes
    missing = sorted(p for p in probes if p not in _PROBE_EXEMPT and f"{p}(" not in canary_src)
    assert not missing, f"probe=True reads with no canary: {missing}"
    assert len(canary.CANARIES) == len(dict(canary.CANARIES))          # names stay unique


# ----------- PR-1 lead: a 0.95 queue peak no longer rounds up to the Warehouses opener's 1.0 floor ----

def test_concurrency_peak_is_truncated_not_rounded_to_the_queue_floor():
    from app.data import ops_sql
    from app.logic.anomaly import warehouse_attention_ranking
    sql = ops_sql.warehouse_concurrency_peaks(14, "ALL")
    assert "TRUNC(MAX(AVG_QUEUED_LOAD), 1) AS PEAK_QUEUED" in sql
    assert "ROUND(MAX(AVG_QUEUED_LOAD)" not in sql
    # MAX(AVG_QUEUED_LOAD) = 0.97 now reads 0.9 (ROUND gave 1.0) and stays out of the queue signal
    peaks = pd.DataFrame({"WAREHOUSE_NAME": ["WH_A", "WH_B"], "PEAK_QUEUED": [0.9, 1.0],
                          "QUEUED_INTERVALS": [3, 3]})
    ranked = warehouse_attention_ranking(pd.DataFrame(), peaks)
    assert ranked["WAREHOUSE_NAME"].tolist() == ["WH_B"]


# --------------- PR-1 lead: the SLA forecast windows only the registered tables' DML (perf, same result) ----

def _sqlite_sla_forecast(sql: str):
    import math
    import re
    import sqlite3
    from datetime import datetime

    sql = sql.replace("SNOWFLAKE.ACCOUNT_USAGE.", "").replace("DBA_MAINT_DB.OVERWATCH.", "")
    sql = re.sub(r"DATEADD\('day', -14, CURRENT_TIMESTAMP\(\)\)", "'2026-09-17 00:00:00'", sql)
    assert "CURRENT_TIMESTAMP" not in sql

    def _minutes(_unit, a, b):
        if a is None or b is None:
            return None
        f = "%Y-%m-%d %H:%M:%S"
        return int((datetime.strptime(b, f) - datetime.strptime(a, f)).total_seconds() // 60)

    class _Pct:
        def __init__(self):
            self.v: list = []

        def step(self, x, q=0.5):
            if x is not None:
                self.v.append(x)
                self.q = q

        def finalize(self):
            if not self.v:
                return None
            s = sorted(self.v)
            k = (len(s) - 1) * self.q
            lo, hi = math.floor(k), math.ceil(k)
            return s[lo] + (s[hi] - s[lo]) * (k - lo)

    class _Median(_Pct):
        def step(self, x):
            super().step(x, 0.5)

    con = sqlite3.connect(":memory:")
    con.create_function("DATEDIFF", 3, _minutes)
    con.create_aggregate("MEDIAN", 1, _Median)
    con.create_aggregate("APPROX_PERCENTILE", 2, _Pct)
    con.execute("CREATE TABLE PIPELINE_SLA_CONFIG (DATABASE_NAME, SCHEMA_NAME, TABLE_NAME, ENABLED)")
    con.execute("CREATE TABLE PIPELINE_SLA_STATUS (DATABASE_NAME, SCHEMA_NAME, TABLE_NAME, OWNER, "
                "MAX_AGE_HOURS, HOURS_SINCE, SLA_MET)")
    con.execute("CREATE TABLE TABLE_DML_HISTORY (DATABASE_NAME, SCHEMA_NAME, TABLE_NAME, START_TIME)")
    con.executemany("INSERT INTO PIPELINE_SLA_CONFIG VALUES (?,?,?,?)",
                    [("db", "s", "t1", 1), ("DB", "S", "T2", 1), ("DB", "S", "OFF", 0)])
    con.executemany("INSERT INTO PIPELINE_SLA_STATUS VALUES (?,?,?,?,?,?,?)",
                    [("db", "s", "t1", "o", 6, 1.0, 1), ("DB", "S", "T2", "o", 6, 2.0, 1)])
    dml = [("DB", "S", "T1", f"2026-09-30 0{h}:00:00") for h in range(5)]          # hourly: gaps of 60
    dml += [("DB", "S", "OTHER", f"2026-09-30 00:{m:02d}:00") for m in range(0, 60, 5)]   # unregistered
    dml += [("DB", "S", "OFF", "2026-09-30 01:00:00"), ("DB", "S", "OFF", "2026-09-30 03:00:00")]
    con.executemany("INSERT INTO TABLE_DML_HISTORY VALUES (?,?,?,?)", dml)
    cur = con.execute(sql)
    return pd.DataFrame(cur.fetchall(), columns=[d[0] for d in cur.description])


def test_sla_forecast_cadence_reads_only_registered_tables():
    import sqlglot
    from sqlglot import exp

    from app.data import insights_sql
    sql = insights_sql.pipeline_sla_forecast(14)
    tree = sqlglot.parse_one(sql, read="snowflake")
    ctes = {c.alias: c.this for c in tree.find_all(exp.CTE)}
    assert "PIPELINE_SLA_CONFIG" in ctes["reg"].sql() and "ENABLED" in ctes["reg"].sql()
    joined = {t.name for t in ctes["intervals"].find_all(exp.Table)}
    assert joined == {"TABLE_DML_HISTORY", "reg"}           # the window function never sees other tables
    out = _sqlite_sla_forecast(sql).set_index("TABLE_NAME")
    assert out.loc["t1", "MEDIAN_GAP_MIN"] == 60 and out.loc["t1", "REFRESHES"] == 4
    assert pd.isna(out.loc["T2", "MEDIAN_GAP_MIN"])          # registered, never refreshed: NULL cadence
    assert set(out.index) == {"t1", "T2"}


# ------------- PR-1 lead (c10): what the ETL run panels read under Last month is in the scope contract ----

def test_pipeline_contract_says_the_etl_panels_read_a_span_ending_now():
    import re

    from app.data import etl_control_sql as etl
    from app.logic.date_windows import CalendarDayOffset
    contract = read(_OPS).split('"Pipeline SLA": {', 1)[1].split("},", 1)[0]
    joined = re.sub(r'"\s*\n\s*"', "", contract)
    assert ("The ETL run panels (workflow runtimes, failure recurrence, runtime creep, recon recurrence) judge "
            "the newest run, so they read the Window as a span ending now: under Last month, that many days back "
            "from today, never the closed month; each caption names the span it read.") in joined
    # ...which is what the readers do with Last month's offset (its span, a calendar offset) on Oct 15
    sep = CalendarDayOffset(30)
    assert "DATEADD('day', -30, CONVERT_TIMEZONE(" in etl._window_clause(sep)
    from datetime import date
    assert etl.calendar_window_phrase(sep, today=date(2026, 10, 15)) == "since Sep 15"
