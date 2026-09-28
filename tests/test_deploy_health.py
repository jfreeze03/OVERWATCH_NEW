"""#26 Phase 1: Admin says whether OVERWATCH is deployed and running (logic.deploy_health + wiring).

Pure tests for the schema-vs-build drift, the runtime versions and every Task health status; builder
tests for the two task reads; source locks for the Admin wiring. The migration tip and APP_VERSION
are always derived (``_EXPECTED_MIGRATIONS`` / ``app.config``), never pinned.
"""

from __future__ import annotations

import datetime
import importlib.metadata
import math
import re
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from app.config import APP_VERSION
from app.core.result import QueryResult
from app.data import ops_sql
from app.logic import deploy_health as dh

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _body(src: str, fn: str) -> str:
    """The function's own text: up to the next line that starts at column 0 (def, constant, comment)."""
    return re.split(r"\n(?=\S)", src.split(f"def {fn}(", 1)[1], maxsplit=1)[0]


def _expected() -> dict:
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    return _EXPECTED_MIGRATIONS


# --------------------------------------------------------------------------- #
# Schema vs build
# --------------------------------------------------------------------------- #

def test_schema_drift_equal_sets_are_clean():
    exp = _expected()
    tip = max(exp)
    d = dh.schema_drift(range(1, tip + 1), exp)
    assert d.missing == () and d.ahead == ()
    assert d.build_tip == tip and d.db_tip == tip and d.expected_n == len(exp)
    assert dh.ahead_warning(d, APP_VERSION) == ""
    assert dh.migrations_clean_message(d) == (
        f"All {len(exp)} migrations this build expects are applied (V001–V{tip:03d}), and none newer.")


def test_schema_drift_database_ahead_of_the_build():
    exp = _expected()
    tip = max(exp)
    d = dh.schema_drift(range(1, tip + 3), exp)               # two migrations newer than this build
    assert d.ahead == (tip + 1, tip + 2) and d.missing == ()
    assert d.db_tip == tip + 2 and d.build_tip == tip
    w = dh.ahead_warning(d, APP_VERSION)
    assert w.startswith("The database has 2 migration(s) this app build does not know")
    assert f"V{tip + 1:03d}–V{tip + 2:03d}" in w
    assert f"({APP_VERSION}, built for V001–V{tip:03d})" in w
    assert "`snow streamlit deploy --replace`" in w and "The migrations themselves are not the problem." in w


def test_schema_drift_database_behind_the_build():
    exp = _expected()
    tip = max(exp)
    d = dh.schema_drift(range(1, tip - 10), exp)
    assert d.missing == tuple(range(tip - 10, tip + 1)) and d.ahead == ()
    assert dh.ahead_warning(d, APP_VERSION) == ""
    empty = dh.schema_drift([], exp)
    assert empty.db_tip is None and len(empty.missing) == len(exp)


def test_applied_versions_skips_junk():
    vals = [1, 2.0, "3", " 4 ", None, math.nan, float("inf"), "x", 5.5, -1, 0, pd.NA, pd.NaT, [7]]
    assert dh.applied_versions(vals) == {1, 2, 3, 4}
    assert dh.applied_versions(pd.Series([1.0, 2.0, None])) == {1, 2}   # shaped frames hand floats
    assert dh.applied_versions([]) == set()


def test_version_span_forms():
    assert dh.version_span([162]) == "V162"
    assert dh.version_span([163, 162]) == "V162–V163"
    assert dh.version_span([162, 170]) == "V162, V170"
    assert dh.version_span(list(range(162, 180, 2)), limit=3) == "V162, V164, V166, …"
    assert dh.version_span([]) == ""


# --------------------------------------------------------------------------- #
# Runtime
# --------------------------------------------------------------------------- #

def test_runtime_versions_has_exactly_five_labelled_entries():
    v = dh.runtime_versions()
    assert list(v) == ["Python", "Streamlit", "pandas", "Altair", "Snowpark"]
    assert re.fullmatch(r"\d+\.\d+\.\d+.*", v["Python"])
    assert v["pandas"] == pd.__version__
    cap = dh.runtime_caption(v)
    assert cap.startswith(f"Runtime: Python {v['Python']} · Streamlit ")
    assert cap.count(" · ") == 4


def test_runtime_versions_unreadable_packages_read_dash(monkeypatch):
    def _missing(_name):
        raise importlib.metadata.PackageNotFoundError(_name)
    monkeypatch.setattr(importlib.metadata, "version", _missing)
    v = dh.runtime_versions()
    assert v["Python"] != "—"
    assert [v[k] for k in ("Streamlit", "pandas", "Altair", "Snowpark")] == ["—"] * 4


# --------------------------------------------------------------------------- #
# Task health
# --------------------------------------------------------------------------- #

_KW: dict = {"expected": ops_sql.OVERWATCH_TASKS, "opt_in": ops_sql.OPT_IN_OVERWATCH_TASKS,
             "retired": ops_sql._RETIRED_OVERWATCH_TASKS,
             "suspended_ok": ops_sql.SUSPENDED_OK_OVERWATCH_TASKS,
             "result_limit": ops_sql.TASK_HISTORY_RESULT_LIMIT}
_T0 = pd.Timestamp("2026-09-28 06:00")
_HOUR = datetime.timedelta(hours=1)


def _show(overrides: dict | None = None, *, extra: tuple = (), drop: tuple = (), upper: bool = False,
          reason_col: bool = True) -> pd.DataFrame:
    """A SHOW TASKS frame: every expected task started, with per-task overrides."""
    rows = []
    for name in sorted(set(ops_sql.OVERWATCH_TASKS) - set(drop)) + list(extra):
        row = {"name": name, "state": "started", "schedule": None,
               "predecessors": '["DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY"]',
               "last_suspended_reason": None}
        if name == "TASK_LOAD_HOURLY":
            row.update(schedule="USING CRON 7 * * * * America/Chicago", predecessors="[]")
        row.update((overrides or {}).get(name, {}))
        rows.append(row)
    df = pd.DataFrame(rows)
    if not reason_col:
        df = df.drop(columns=["last_suspended_reason"])
    if upper:
        df.columns = [c.upper() for c in df.columns]
    return df


def _runs(overrides: dict | None = None, *, history_rows: float = 400.0) -> pd.DataFrame:
    """The run summary: every expected task succeeded twice in the window."""
    rows = []
    for name in sorted(ops_sql.OVERWATCH_TASKS):
        row = {"TASK_NAME": name, "SUCCEEDED_N": 2.0, "FAILED_N": 0.0, "SKIPPED_N": 0.0,
               "LAST_SUCCESS_AT": _T0, "LAST_FAILURE_AT": pd.NaT, "LAST_ERROR_MESSAGE": None,
               "HISTORY_ROWS": history_rows}
        row.update((overrides or {}).get(name, {}))
        rows.append(row)
    return pd.DataFrame(rows)


def _status(h: dh.TaskHealth, name: str) -> str:
    return str(h.rows.set_index("TASK_NAME").loc[name, "STATUS"])


def test_show_failed_is_unverifiable_never_down():
    h = dh.task_health(None, None, **_KW)
    assert h.state == "UNVERIFIABLE" and h.severity == "info"
    assert "NOT evidence the tasks are down" in h.headline
    assert h.rows.empty and list(h.rows.columns) == list(dh.TASK_HEALTH_COLUMNS)


@pytest.mark.parametrize("tasks", [pd.DataFrame(), pd.DataFrame(columns=["name", "state"]),
                                   pd.DataFrame({"state": ["started"]}), pd.DataFrame({"name": [None, ""]})])
def test_empty_show_is_not_visible_with_no_rows(tasks):
    h = dh.task_health(tasks, _runs(), **_KW)
    assert h.state == "NOT_VISIBLE" and h.severity == "info"
    assert h.rows.empty                                   # never 32 false "Not visible" rows


@pytest.mark.parametrize("upper", [False, True])
def test_all_started_and_succeeding_is_running(upper):
    h = dh.task_health(_show(upper=upper), _runs(), **_KW)
    n = len(ops_sql.OVERWATCH_TASKS)
    assert h.state == "RUNNING" and h.severity == "ok" and h.notes == ()
    assert h.headline == f"{n} of {n} OVERWATCH tasks started · 0 suspended · 0 failing (last 24h)"
    assert set(h.rows["STATUS"]) == {"Running"} and len(h.rows) == n
    assert list(h.rows.columns) == list(dh.TASK_HEALTH_COLUMNS)
    row = h.rows.set_index("TASK_NAME")
    assert row.loc["TASK_LOAD_HOURLY", "RUNS_ON"] == "USING CRON 7 * * * * America/Chicago"
    assert row.loc["TASK_QH_EXTRACT", "RUNS_ON"] == "after TASK_LOAD_HOURLY"   # bare parent name


def test_suspended_expected_task_is_bad_with_its_reason():
    h = dh.task_health(_show({"TASK_QH_EXTRACT": {"state": "suspended",
                                                  "last_suspended_reason": "SUSPENDED_DUE_TO_ERRORS"}}),
                       _runs(), **_KW)
    assert h.state == "FAILING" and h.severity == "bad"
    row = h.rows.iloc[0]                                  # bad sorts first
    assert (row["TASK_NAME"], row["STATUS"], row["NOTE"]) == (
        "TASK_QH_EXTRACT", "Suspended", "SUSPENDED_DUE_TO_ERRORS")
    assert "1 suspended" in h.headline
    # an older account's SHOW has no last_suspended_reason column: still Suspended, no note
    h2 = dh.task_health(_show({"TASK_QH_EXTRACT": {"state": "suspended"}}, reason_col=False), _runs(), **_KW)
    assert _status(h2, "TASK_QH_EXTRACT") == "Suspended"
    assert h2.rows.set_index("TASK_NAME").loc["TASK_QH_EXTRACT", "NOTE"] is None


def test_suspended_alert_notify_is_expected_warn_not_bad():
    h = dh.task_health(_show({"TASK_ALERT_NOTIFY": {"state": "suspended"}}), _runs(), **_KW)
    assert _status(h, "TASK_ALERT_NOTIFY") == "Suspended (expected)"
    assert h.state == "DEGRADED" and h.severity == "warn"
    assert "delivery integration" in h.rows.set_index("TASK_NAME").loc["TASK_ALERT_NOTIFY", "NOTE"]


def test_failing_recovered_and_skipped():
    runs = _runs({
        "TASK_LOAD_DAILY": {"FAILED_N": 2.0, "LAST_FAILURE_AT": _T0 + _HOUR,
                            "LAST_ERROR_MESSAGE": "Numeric value '$1' is not recognized"},
        "TASK_LOAD_HOURLY": {"FAILED_N": 1.0, "LAST_FAILURE_AT": _T0 - _HOUR},
        "TASK_QH_EXTRACT": {"SKIPPED_N": 3.0},
        "TASK_PURGE_FACTS": {"FAILED_N": 1.0, "LAST_SUCCESS_AT": pd.NaT,
                             "LAST_FAILURE_AT": _T0},              # never succeeded -> failing
    })
    h = dh.task_health(_show(), runs, **_KW)
    assert _status(h, "TASK_LOAD_DAILY") == "Failing"
    assert _status(h, "TASK_PURGE_FACTS") == "Failing"
    assert _status(h, "TASK_LOAD_HOURLY") == "Recovered"
    assert _status(h, "TASK_QH_EXTRACT") == "Skipped runs"
    assert h.state == "FAILING" and "2 failing (last 24h)" in h.headline
    by = h.rows.set_index("TASK_NAME")
    assert by.loc["TASK_LOAD_DAILY", "LAST_ERROR_MESSAGE"] == "Numeric value '$1' is not recognized"
    assert by.loc["TASK_LOAD_DAILY", "FAILED_N"] == 2
    # sorted by severity (bad, warn, ...) then name
    assert list(h.rows["STATUS"][:2]) == ["Failing", "Failing"]
    assert list(h.rows["TASK_NAME"][:2]) == ["TASK_LOAD_DAILY", "TASK_PURGE_FACTS"]


def test_not_visible_retired_opt_in_and_unknown_live_tasks():
    tasks = _show({"TASK_ALERT_DRILL": {"state": "suspended"}},
                  drop=("TASK_LOCK_WAIT_DAILY",),
                  extra=("TASK_BACKUP_OPERATOR", "TASK_ALERT_DRILL", "TASK_FROM_V999"))
    h = dh.task_health(tasks, _runs(), **_KW)
    assert _status(h, "TASK_LOCK_WAIT_DAILY") == "Not visible"
    assert _status(h, "TASK_BACKUP_OPERATOR") == "Retired"
    assert "not yet applied" in h.rows.set_index("TASK_NAME").loc["TASK_BACKUP_OPERATOR", "NOTE"]
    assert _status(h, "TASK_ALERT_DRILL") == "Opt-in"               # even when suspended
    assert _status(h, "TASK_FROM_V999") == "Not in this build"
    assert h.state == "DEGRADED" and h.severity == "warn"
    by = h.rows.set_index("TASK_NAME")
    assert math.isnan(by.loc["TASK_FROM_V999", "SUCCEEDED_N"])         # no run row -> NaN ('—')
    n = len(ops_sql.OVERWATCH_TASKS)
    assert h.headline.startswith(f"{n - 1} of {n} OVERWATCH tasks started")


def test_history_at_the_limit_is_never_green():
    limit = ops_sql.TASK_HISTORY_RESULT_LIMIT
    h = dh.task_health(_show(), _runs(history_rows=float(limit)), **_KW)
    assert h.state == "RUNNING" and h.severity == "info"
    assert h.notes == (dh.NOTE_HISTORY_TRUNCATED.format(limit=limit),)
    assert "10,000-row limit" in h.notes[0]
    below = dh.task_health(_show(), _runs(history_rows=float(limit - 1)), **_KW)
    assert below.severity == "ok" and below.notes == ()


def test_runs_read_failed_or_invisible_says_so_and_is_never_green():
    h = dh.task_health(_show(), None, **_KW)
    assert h.notes == (dh.NOTE_RUNS_UNVERIFIABLE,) and h.severity == "info"
    assert set(h.rows["STATUS"]) == {"Running"}
    assert h.rows["SUCCEEDED_N"].isna().all()
    # the LEFT JOIN's lone NULL-name row: HISTORY_ROWS only, never a status row
    null_only = pd.DataFrame([{"TASK_NAME": None, "SUCCEEDED_N": 0, "FAILED_N": 0, "SKIPPED_N": 0,
                               "LAST_SUCCESS_AT": None, "LAST_FAILURE_AT": None,
                               "LAST_ERROR_MESSAGE": None, "HISTORY_ROWS": 9000}])
    h2 = dh.task_health(_show(), null_only, **_KW)
    assert h2.notes == (dh.NOTE_NO_RUN_HISTORY,) and h2.severity == "info"
    assert len(h2.rows) == len(ops_sql.OVERWATCH_TASKS)
    assert not set(h2.rows["TASK_NAME"]) & {"", "NONE", "NAN"}
    # a NULL-name row next to real rows is ignored for statuses but its HISTORY_ROWS still counts
    mixed = pd.DataFrame([*_runs().to_dict("records"),
                          {**null_only.iloc[0].to_dict(),
                           "HISTORY_ROWS": float(ops_sql.TASK_HISTORY_RESULT_LIMIT)}])
    h3 = dh.task_health(_show(), mixed, **_KW)
    assert h3.notes == (dh.NOTE_HISTORY_TRUNCATED.format(limit=ops_sql.TASK_HISTORY_RESULT_LIMIT),)


@pytest.mark.parametrize("tasks, runs", [
    (pd.DataFrame({"name": [None, 3.5, ["x"], {"a": 1}], "state": [object(), None, 5, "STARTED"]}),
     pd.DataFrame({"task_name": [1, None], "failed_n": ["x", object()], "last_failure_at": ["junk", 7]})),
    (pd.DataFrame({"NAME": ["TASK_LOAD_HOURLY"], "PREDECESSORS": [{"weird": True}]}),
     pd.DataFrame({"HISTORY_ROWS": ["not a number"]})),
    (_show(), pd.DataFrame({"TASK_NAME": ["TASK_LOAD_DAILY"],
                            "LAST_SUCCESS_AT": [pd.Timestamp("2026-09-28", tz="UTC")],
                            "LAST_FAILURE_AT": ["2026-09-28 01:00:00+05:00"], "FAILED_N": [1]})),
    (_show(), "not a frame"),
])
def test_garbage_frames_never_raise(tasks, runs):
    h = dh.task_health(tasks, runs, **_KW)
    assert h.state in {"RUNNING", "DEGRADED", "FAILING", "NOT_VISIBLE", "UNVERIFIABLE"}
    assert list(h.rows.columns) == list(dh.TASK_HEALTH_COLUMNS)


def test_statuses_are_sentence_case():
    tasks = _show({"TASK_QH_EXTRACT": {"state": "suspended"}, "TASK_ALERT_NOTIFY": {"state": "suspended"},
                   "TASK_LOAD_DAILY": {"state": ""}},
                  drop=("TASK_LOCK_WAIT_DAILY",),
                  extra=("TASK_BACKUP_OPERATOR", "TASK_REFRESH_ML_FORECAST", "TASK_UNKNOWN"))
    runs = _runs({"TASK_PURGE_FACTS": {"FAILED_N": 1.0, "LAST_FAILURE_AT": _T0 + _HOUR},
                  "TASK_LOAD_HOURLY": {"FAILED_N": 1.0, "LAST_FAILURE_AT": _T0 - _HOUR},
                  "TASK_ALERT_SCAN": {"SKIPPED_N": 1.0}})
    statuses = set(dh.task_health(tasks, runs, **_KW).rows["STATUS"])
    assert statuses == {"Running", "Suspended", "Suspended (expected)", "Failing", "Recovered",
                        "Skipped runs", "Not visible", "Retired", "Opt-in", "Not in this build",
                        "State unknown"}
    for s in statuses:
        assert s[0].isupper() and s[1:] == s[1:].lower(), s


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def test_task_state_read_is_a_scoped_show():
    assert ops_sql.overwatch_task_states() == "SHOW TASKS IN SCHEMA DBA_MAINT_DB.OVERWATCH"
    assert "max_rows=0" in ops_sql.overwatch_task_states.__doc__


def test_task_run_summary_builder():
    sql = ops_sql.overwatch_task_run_summary(24)
    assert "FROM TABLE(DBA_MAINT_DB.INFORMATION_SCHEMA.TASK_HISTORY(" in sql
    assert "RESULT_LIMIT => 10000" in sql and ops_sql.TASK_HISTORY_RESULT_LIMIT == 10_000
    assert "DATEADD('hour', -24, CURRENT_TIMESTAMP())" in sql
    assert ("FROM n\nLEFT JOIN h ON h.DATABASE_NAME = 'DBA_MAINT_DB' AND h.SCHEMA_NAME = 'OVERWATCH'"
            in sql)
    assert "CONVERT_TIMEZONE('America/Chicago'" in sql and "HISTORY_ROWS" in sql
    assert "'FAILED_AND_AUTO_SUSPENDED'" in sql and "COUNT_IF(h.STATE = 'SKIPPED')" in sql
    # review r1: a CANCELLED run (operator cancel / cancelled graph run) is counted apart, never a failure
    assert "COUNT_IF(h.STATE = 'CANCELLED') AS CANCELLED_N" in sql
    assert "h.STATE IN ('FAILED', 'FAILED_AND_AUTO_SUSPENDED')" in sql and sql.count("'CANCELLED'") == 1
    assert "ACCOUNT_USAGE" not in sql
    assert "DATEADD('hour', -168," in ops_sql.overwatch_task_run_summary(999999)
    assert "DATEADD('hour', -24," in ops_sql.overwatch_task_run_summary(0)
    assert "DATEADD('hour', -1," in ops_sql.overwatch_task_run_summary(-5)


def test_task_run_summary_parses_and_is_a_canary():
    sqlglot = pytest.importorskip("sqlglot")
    expr = sqlglot.parse_one(ops_sql.overwatch_task_run_summary(24), read="snowflake")
    assert expr.named_selects == ["TASK_NAME", "SUCCEEDED_N", "FAILED_N", "SKIPPED_N", "CANCELLED_N",
                                  "LAST_SUCCESS_AT", "LAST_FAILURE_AT", "LAST_ERROR_MESSAGE", "HISTORY_ROWS"]
    from app.data.canary import CANARIES
    names = {n for n, _ in CANARIES}
    assert "ops.overwatch_task_run_summary" in names
    assert not any("overwatch_task_states" in n for n in names)     # SHOW cannot be EXPLAINed


# --------------------------------------------------------------------------- #
# Admin wiring
# --------------------------------------------------------------------------- #

def test_admin_header_warns_on_every_section():
    adm = _src("app/ui/pages/admin.py")
    ctx = _body(adm, "_context_section")
    assert "_schema_ahead_banner()" in ctx
    assert "deploy_health.runtime_caption(deploy_health.runtime_versions())" in ctx
    # outside `if ctx.usable()`: the banner and caption show even with no session-context row
    assert ctx.index("elif not ctx.ok:") < ctx.index("_schema_ahead_banner()")
    banner = _body(adm, "_schema_ahead_banner")
    assert 'key="schema_ahead_check", tier="recent"' in banner and "probe=True" in banner
    assert "st.warning(md_dollars(deploy_health.ahead_warning(drift, APP_VERSION)))" in banner
    render = _body(adm, "render")
    assert render.index("_context_section()") < render.index("lazy_sections(")


def test_admin_header_banner_fires_only_when_ahead(monkeypatch):
    from app.ui.pages import admin
    tip = max(admin._EXPECTED_MIGRATIONS)
    warned: list[str] = []
    monkeypatch.setattr(admin, "st", SimpleNamespace(warning=warned.append))

    def _banner(result: QueryResult) -> list[str]:
        warned.clear()
        monkeypatch.setattr(admin, "run", lambda *_a, **_k: result)
        admin._schema_ahead_banner()
        return list(warned)

    def _versions(vs) -> QueryResult:
        return QueryResult(df=pd.DataFrame({"VERSION": [float(v) for v in vs]}), ok=True)

    assert _banner(_versions(range(1, tip + 1))) == []                 # in step: silent
    assert _banner(_versions(range(1, tip - 3))) == []                 # behind: the tab's punch list
    ahead = _banner(_versions(range(1, tip + 2)))
    assert len(ahead) == 1 and f"({APP_VERSION}, built for V001–V{tip:03d})" in ahead[0]
    assert f"(V{tip + 1:03d})" in ahead[0]
    assert _banner(QueryResult(ok=False, error="boom")) == []           # unreadable: silent here
    assert _banner(QueryResult(df=pd.DataFrame({"X": [1]}), ok=True)) == []   # no VERSION column


def test_migrations_tab_no_longer_claims_exactly_these():
    adm = _src("app/ui/pages/admin.py")
    assert "expects exactly these" not in adm
    assert "check SHOW TASKS" not in adm and "Check SHOW TASKS" not in adm
    tab = _body(adm, "_migrations_tab")
    assert "drift = deploy_health.schema_drift(applied, _EXPECTED_MIGRATIONS)" in tab
    assert 'empty_state("clean", deploy_health.migrations_clean_message(drift))' in tab
    assert "Applied but newer than this build" in tab
    assert tab.rstrip().endswith("_task_health_panel()")


def test_task_health_panel_is_toggle_gated_and_safe():
    panel = _body(_src("app/ui/pages/admin.py"), "_task_health_panel")
    toggle = panel.index("st.toggle(")
    assert toggle < panel.index("ops_sql.overwatch_task_states()")
    assert toggle < panel.index("ops_sql.overwatch_task_run_summary(24)")
    show_call = panel[panel.index("run(ops_sql.overwatch_task_states()"):]
    assert "max_rows=0" in show_call.split("\n    runs = ", 1)[0]
    assert "st.dataframe(" not in panel and "SNOWFLAKE.ACCOUNT_USAGE" not in panel
    # ACCOUNT_USAGE is only ever NAMED as what the panel avoids ("no ACCOUNT_USAGE lag")
    assert set(re.findall(r"(\w+) ACCOUNT_USAGE", panel)) == {"no"}
    assert re.findall(r"\b\w+_sql\.\w+\(", panel) == ["ops_sql.overwatch_task_states(",
                                                      "ops_sql.overwatch_task_run_summary("]
    assert "styled_table(h.rows, height=TABLE_H_LG)" in panel
    assert "st.error(md_dollars(h.headline))" in panel and "st.warning(md_dollars(h.headline))" in panel
    assert "st.caption(md_dollars(note))" in panel
    assert 'empty_state("unavailable", h.headline, detail=tasks.error)' in panel


def test_setup_progress_counts_only_known_migrations(monkeypatch):
    from app.ui.pages import admin
    tab = _body(_src("app/ui/pages/admin.py"), "_setup_progress_tab")
    assert "drift.ahead" in tab and "len(applied)} of" not in tab
    tip = max(admin._EXPECTED_MIGRATIONS)
    tables: list[pd.DataFrame] = []

    def _run(sql, *, key, **_k):
        if key == "setup_schema_version":
            return QueryResult(df=pd.DataFrame({"VERSION": list(range(1, tip + 2))}), ok=True)
        return QueryResult(ok=True)

    monkeypatch.setattr(admin, "run", _run)
    monkeypatch.setattr(admin, "panel_help", lambda *_a, **_k: None)
    monkeypatch.setattr(admin, "empty_state", lambda *_a, **_k: None)
    monkeypatch.setattr(admin, "load_settings", lambda _p: {})
    monkeypatch.setattr(admin, "styled_table", lambda df, **_k: tables.append(df))
    monkeypatch.setattr(admin, "st", SimpleNamespace(warning=lambda *_a: None, caption=lambda *_a: None))
    admin._setup_progress_tab()
    row = tables[0].set_index("STEP").loc["Database migrations"]
    n = len(admin._EXPECTED_MIGRATIONS)
    assert row["DETAIL"] == f"{n} of {n} applied; 1 newer than this build — redeploy the app"
    assert row["STATUS"] == "Partial" and "snow streamlit deploy --replace" in row["FIX"]


# --------------------------------------------------------------------------- #
# review r1
# --------------------------------------------------------------------------- #
def test_cancel_only_runs_are_a_warning_never_failing():
    runs = _runs({"TASK_LOAD_DAILY": {"CANCELLED_N": 1.0, "LAST_ERROR_MESSAGE": None}})
    h = dh.task_health(_show(), runs, **_KW)
    assert _status(h, "TASK_LOAD_DAILY") == "Cancelled runs"
    assert h.severity == "warn" and "0 failing" in h.headline
    assert h.rows.set_index("TASK_NAME").loc["TASK_LOAD_DAILY", "CANCELLED_N"] == 1


def test_a_started_opt_in_task_is_graded_by_its_runs():
    runs = _runs()
    extra = pd.DataFrame([{"TASK_NAME": "TASK_REFRESH_ML_FORECAST", "SUCCEEDED_N": 0.0, "FAILED_N": 1.0,
                           "SKIPPED_N": 0.0, "LAST_SUCCESS_AT": pd.NaT, "LAST_FAILURE_AT": _T0,
                           "LAST_ERROR_MESSAGE": "Forecast model not found", "HISTORY_ROWS": 400.0}])
    h = dh.task_health(_show(extra=("TASK_REFRESH_ML_FORECAST",)), pd.concat([runs, extra], ignore_index=True), **_KW)
    assert _status(h, "TASK_REFRESH_ML_FORECAST") == "Failing"
    assert h.severity == "bad" and h.headline.endswith(" · 1 opt-in failing")
    assert "0 failing (last 24h)" in h.headline                 # the expected-set counter is unchanged
    assert "optional" in h.rows.set_index("TASK_NAME").loc["TASK_REFRESH_ML_FORECAST", "NOTE"]
    # suspended or clean opt-in tasks stay 'Opt-in' (ok)
    quiet = dh.task_health(_show({"TASK_ALERT_DRILL": {"state": "suspended"}}, extra=("TASK_ALERT_DRILL",)),
                           _runs(), **_KW)
    assert _status(quiet, "TASK_ALERT_DRILL") == "Opt-in" and "opt-in failing" not in quiet.headline


def test_migrations_tabs_agree_with_the_header_banner():
    adm = _src("app/ui/pages/admin.py")
    fresh = _body(adm, "_fresh_applied_versions")
    assert 'key="schema_ahead_check", tier="recent"' in fresh          # the banner's own read (cache hit)
    banner = _body(adm, "_schema_ahead_banner")
    assert 'key="schema_ahead_check", tier="recent"' in banner
    tab = _body(adm, "_migrations_tab")
    assert tab.index("applied |= _fresh_applied_versions()") < tab.index(
        "drift = deploy_health.schema_drift(applied, _EXPECTED_MIGRATIONS)")
    # the task reads do not depend on SCHEMA_VERSION: the unreadable branch still offers Task health
    unreadable = tab[tab.index("if not res.ok:"):tab.index("applied = set()")]
    assert "_task_health_panel()" in unreadable and unreadable.rstrip().endswith("return")
    setup = _body(adm, "_setup_progress_tab")
    assert setup.index("applied |= _fresh_applied_versions()") < setup.index("drift = deploy_health")


def test_setup_progress_sees_a_migration_the_metadata_read_has_not(monkeypatch):
    """The 4h metadata read still says V001..tip, the header's 5-minute read already sees tip+1."""
    from app.ui.pages import admin
    tip = max(admin._EXPECTED_MIGRATIONS)
    tables: list[pd.DataFrame] = []

    def _run(sql, *, key, **_k):
        if key == "setup_schema_version":
            return QueryResult(df=pd.DataFrame({"VERSION": list(range(1, tip + 1))}), ok=True)
        if key == "schema_ahead_check":
            return QueryResult(df=pd.DataFrame({"VERSION": list(range(1, tip + 2))}), ok=True)
        return QueryResult(ok=True)

    monkeypatch.setattr(admin, "run", _run)
    monkeypatch.setattr(admin, "panel_help", lambda *_a, **_k: None)
    monkeypatch.setattr(admin, "empty_state", lambda *_a, **_k: None)
    monkeypatch.setattr(admin, "load_settings", lambda _p: {})
    monkeypatch.setattr(admin, "styled_table", lambda df, **_k: tables.append(df))
    monkeypatch.setattr(admin, "st", SimpleNamespace(warning=lambda *_a: None, caption=lambda *_a: None))
    admin._setup_progress_tab()
    row = tables[0].set_index("STEP").loc["Database migrations"]
    assert row["STATUS"] == "Partial" and "1 newer than this build" in row["DETAIL"]
