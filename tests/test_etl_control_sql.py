"""ETL reference-data gap monitor (Phase 1) — parser + scan builder.

Covers the config parser (entry separators, malformed rows), the UNION-ALL
MINUS scan, the dormant/empty paths, and injection fail-closed behavior.
"""

from __future__ import annotations

from app.data import etl_control_sql as etl

_XLAT = "ALFA_EDW_PRD.DB_V_PROD_BASE.TERADATA_ETL_REF_XLAT"
_STG = "ALFA_EDW_PRD.DB_T_PROD_STAG.PC_UWISSUETYPE"
_ONE = f"pc_uwissuetype.code | {_STG} | CODE_STG"


# --- parse_ref_gap_checks ---------------------------------------------------

def test_parse_single_check() -> None:
    checks, warns = etl.parse_ref_gap_checks(_ONE)
    assert warns == []
    assert len(checks) == 1
    c = checks[0]
    assert c.name == "pc_uwissuetype.code"
    assert c.staging_fqn == _STG
    assert c.staging_col == "CODE_STG"


def test_parse_multiple_entries_newline_and_semicolon() -> None:
    raw = f"{_ONE}\nother.code | DB.SCH.T2 | VAL_STG ; third.code | DB.SCH.T3 | C3"
    checks, warns = etl.parse_ref_gap_checks(raw)
    assert warns == []
    assert [c.name for c in checks] == ["pc_uwissuetype.code", "other.code", "third.code"]


def test_parse_skips_blank_and_comment_lines() -> None:
    raw = f"# a header comment\n\n{_ONE}\n   \n# trailing note"
    checks, warns = etl.parse_ref_gap_checks(raw)
    assert warns == []
    assert len(checks) == 1


def test_parse_malformed_entry_warns_not_raises() -> None:
    raw = f"{_ONE}\nbroken row without pipes\nmissing.col | DB.SCH.T |"
    checks, warns = etl.parse_ref_gap_checks(raw)
    assert len(checks) == 1  # only the valid one survives
    assert len(warns) == 2   # both malformed rows are reported
    assert all("malformed" in w.lower() for w in warns)


def test_parse_empty_is_empty() -> None:
    for raw in ("", "   ", None):
        checks, warns = etl.parse_ref_gap_checks(raw)
        assert checks == [] and warns == []


def test_parse_pin_marker_strips_star_and_flags() -> None:
    checks, warns = etl.parse_ref_gap_checks(f"*{_ONE}")
    assert warns == []
    assert checks[0].pinned is True
    assert checks[0].name == "pc_uwissuetype.code"  # the '*' is not part of the name


def test_parse_star_only_name_is_malformed() -> None:
    checks, warns = etl.parse_ref_gap_checks(f"* | {_STG} | CODE_STG")
    assert checks == []
    assert len(warns) == 1


def test_check_database_is_first_fqn_segment() -> None:
    check = etl.parse_ref_gap_checks(_ONE)[0][0]
    assert check.database == "ALFA_EDW_PRD"
    assert check.pinned is False


# --- filter_checks_by_database ----------------------------------------------

def _mixed_checks() -> list[etl.RefGapCheck]:
    raw = (
        f"*{_ONE}\n"                                         # pinned, ALFA_EDW_PRD
        "other.code | OTHER_DB.SCH.T2 | VAL_STG\n"           # OTHER_DB
        "third.code | ALFA_EDW_PRD.SCH.T3 | C3")             # ALFA_EDW_PRD
    return etl.parse_ref_gap_checks(raw)[0]


def test_filter_empty_database_keeps_all() -> None:
    checks = _mixed_checks()
    assert etl.filter_checks_by_database(checks, "") == checks
    assert etl.filter_checks_by_database(checks, "   ") == checks


def test_filter_scopes_to_database_but_keeps_pinned() -> None:
    checks = _mixed_checks()
    kept = etl.filter_checks_by_database(checks, "OTHER_DB")
    names = {c.name for c in kept}
    # pinned pc_uwissuetype survives despite being in a different database
    assert "pc_uwissuetype.code" in names
    assert "other.code" in names          # matches the scoped database
    assert "third.code" not in names      # different database, not pinned


def test_filter_is_case_insensitive() -> None:
    checks = _mixed_checks()
    kept = etl.filter_checks_by_database(checks, "alfa_edw_prd")
    names = {c.name for c in kept}
    assert names == {"pc_uwissuetype.code", "third.code"}


# --- reference_gap_scan -----------------------------------------------------

def test_scan_builds_minus_union_with_cap_and_order() -> None:
    checks, _ = etl.parse_ref_gap_checks(
        f"{_ONE}\nother.code | ALFA_EDW_PRD.DB_T_PROD_STAG.OTHER | VAL_STG")
    sql, errs = etl.reference_gap_scan(checks, _XLAT)
    assert errs == []
    # both checks present, joined by UNION ALL
    assert sql.count("UNION ALL") == 1
    assert sql.count("MINUS") == 2
    # the family name drives both the label literal and the XLAT filter
    assert "'pc_uwissuetype.code'" in sql
    assert f"{etl.XLAT_NAME_COL} = 'pc_uwissuetype.code'" in sql
    assert etl.XLAT_VALUE_COL in sql
    assert _STG in sql and _XLAT in sql
    # deterministic truncation: ORDER BY + LIMIT are IN the SQL, not left to pandas
    assert "ORDER BY CHECK_NAME, NEW_CODE" in sql
    assert f"LIMIT {etl.MAX_CODES}" in sql


def test_scan_matches_the_manual_morning_query_shape() -> None:
    # The proven manual check: staging code column MINUS the XLAT values for its family.
    checks, _ = etl.parse_ref_gap_checks(_ONE)
    sql, _ = etl.reference_gap_scan(checks, _XLAT)
    assert "SELECT s.CODE_STG AS NEW_CODE" in sql
    assert f"FROM {_STG} s WHERE s.CODE_STG IS NOT NULL" in sql
    assert f"SELECT x.{etl.XLAT_VALUE_COL} FROM {_XLAT} x" in sql


def test_scan_empty_when_unconfigured() -> None:
    checks, _ = etl.parse_ref_gap_checks(_ONE)
    # no reference table -> nothing to run
    assert etl.reference_gap_scan(checks, "") == ("", [])
    # no checks -> nothing to run
    assert etl.reference_gap_scan([], _XLAT) == ("", [])


def test_scan_drops_and_reports_unsafe_identifier() -> None:
    # A hostile staging FQN must fail closed: its check is dropped, not emitted.
    bad = etl.RefGapCheck(name="evil", staging_fqn="T; DROP TABLE X", staging_col="C")
    good = etl.parse_ref_gap_checks(_ONE)[0][0]
    sql, errs = etl.reference_gap_scan([good, bad], _XLAT)
    assert len(errs) == 1
    assert "evil" in errs[0]
    assert "DROP" not in sql.upper()  # the unsafe fragment never reaches the SQL
    assert "MINUS" in sql             # the good check still builds


def test_scan_all_unsafe_yields_no_sql() -> None:
    bad = etl.RefGapCheck(name="x", staging_fqn="a b c", staging_col="1col")
    sql, errs = etl.reference_gap_scan([bad], _XLAT)
    assert sql == ""
    assert len(errs) == 1


# --- workflow_runtimes_scan (Phase 2: CONTROL_STATUS) -----------------------

_CTRL = "ALFA_EDW_PRD.PUBLIC.CONTROL_STATUS"


def test_workflow_runtimes_scan_basic() -> None:
    sql = etl.workflow_runtimes_scan(_CTRL)
    assert _CTRL in sql
    # latest run only, per-task runtime, running task measured to now, slowest first
    assert "QUALIFY ROW_NUMBER() OVER (ORDER BY TASK_START_DTTM DESC) = 1" in sql
    assert "AS RUNTIME_SEC" in sql                      # _SEC name -> Hr/Min/Sec humanize
    assert "COALESCE(s.TASK_END_DTTM, CURRENT_TIMESTAMP())" in sql
    assert "ORDER BY RUNTIME_SEC DESC" in sql
    assert "LIMIT" in sql


def test_workflow_runtimes_scan_dormant_when_unset() -> None:
    assert etl.workflow_runtimes_scan("") == ""
    assert etl.workflow_runtimes_scan(None) == ""
    assert etl.workflow_runtimes_scan("   ") == ""


def test_workflow_runtimes_scan_injection_fail_closed() -> None:
    # a hostile / malformed FQN must yield no SQL, never an unsafe fragment
    assert etl.workflow_runtimes_scan("T; DROP TABLE X") == ""
    assert etl.workflow_runtimes_scan("a b c") == ""


def test_workflow_runtimes_scan_default_has_no_workflow_or_window_filter() -> None:
    # back-compat: the no-arg form (Brief signal + basic call) is unchanged — no per-workflow
    # filter, no window filter, so existing callers keep the global-latest-run behavior.
    sql = etl.workflow_runtimes_scan(_CTRL)
    assert "WORKFLOW_NAME =" not in sql
    assert "DATEADD" not in sql


def test_workflow_runtimes_scan_per_workflow_binds_a_literal() -> None:
    sql = etl.workflow_runtimes_scan(_CTRL, workflow="WF_SP_SEMANTIC_PC")
    # the chosen workflow filters the latest-run CTE as an escaped literal (data, not identifier)
    assert "AND WORKFLOW_NAME = 'WF_SP_SEMANTIC_PC'" in sql
    # a hostile workflow name stays inside the literal (single-quote doubled)
    assert "'x'' OR ''1''=''1'" in etl.workflow_runtimes_scan(_CTRL, workflow="x' OR '1'='1")


def test_workflow_runtimes_scan_collapses_retries_and_drops_null_start() -> None:
    sql = etl.workflow_runtimes_scan(_CTRL)
    # ONE row per (workflow, task): MAX_BY collapses an Informatica retry to the terminal attempt, so
    # a FAILED-then-retried-SUCCESS task reads SUCCESS (matches the failure-recurrence panel + Brief).
    assert "MAX_BY(s.TASK_STATUS, COALESCE(s.TASK_END_DTTM, s.TASK_START_DTTM)) AS TASK_STATUS" in sql
    assert "GROUP BY s.WORKFLOW_NAME, s.TASK_NAME" in sql
    # outer NULL-start filter so a queued-but-unstarted task can't sort to the top under DESC
    assert "WHERE s.TASK_START_DTTM IS NOT NULL AND s.TASK_NAME IS NOT NULL" in sql


def test_workflow_runtimes_scan_honors_window() -> None:
    # days > 0 bounds the run to the scope-bar Window; days <= 0 adds no window filter
    assert "TASK_START_DTTM >= DATEADD('day', -7, CURRENT_TIMESTAMP())" in \
        etl.workflow_runtimes_scan(_CTRL, days=7)
    assert "DATEADD" not in etl.workflow_runtimes_scan(_CTRL, days=0)
    assert "DATEADD" not in etl.workflow_runtimes_scan(_CTRL, days=-3)


def test_workflow_list_scan_basic_and_window() -> None:
    sql = etl.workflow_list_scan(_CTRL, days=14)
    assert _CTRL in sql
    assert "MAX(TASK_START_DTTM) AS LAST_RUN_AT" in sql
    assert "COUNT(DISTINCT RUN_ID) AS RUNS" in sql
    assert "GROUP BY WORKFLOW_NAME" in sql and "ORDER BY LAST_RUN_AT DESC" in sql
    assert "TASK_START_DTTM >= DATEADD('day', -14, CURRENT_TIMESTAMP())" in sql
    # fail-closed on a bad FQN; no window filter when unscoped
    assert etl.workflow_list_scan("") == "" and etl.workflow_list_scan("a b c") == ""
    assert "DATEADD" not in etl.workflow_list_scan(_CTRL)


def test_workflow_runtimes_and_list_parse() -> None:
    import pytest
    sqlglot = pytest.importorskip("sqlglot")
    sqlglot.parse(etl.workflow_runtimes_scan(_CTRL, workflow="WF_X", days=7), dialect="snowflake")
    sqlglot.parse(etl.workflow_list_scan(_CTRL, days=30), dialect="snowflake")


def test_task_status_sets_are_disjoint_and_shared() -> None:
    # the panel + the Brief signal both read these, so a status is never BOTH a
    # failure and a running state (that would make the two surfaces disagree)
    assert etl.FAILED_TASK_STATUSES and etl.RUNNING_TASK_STATUSES
    assert not (etl.FAILED_TASK_STATUSES & etl.RUNNING_TASK_STATUSES)
    assert "FAILED" in etl.FAILED_TASK_STATUSES and "ABORTED" in etl.FAILED_TASK_STATUSES
    assert "RUNNING" in etl.RUNNING_TASK_STATUSES
    assert "SUCCEEDED" not in etl.FAILED_TASK_STATUSES  # success is never a failure


# --- workflow_runtime_drift_scan (Phase 2: run-over-run drift) ---------------

def test_drift_scan_basic() -> None:
    sql = etl.workflow_runtime_drift_scan(_CTRL, baseline_runs=5)
    assert _CTRL in sql
    # ranked PER WORKFLOW (a task vs its OWN prior runs, not other workflows same night);
    # RUN_ID DESC tiebreaker makes 'latest' deterministic on a same-second tie
    assert "QUALIFY ROW_NUMBER() OVER (PARTITION BY WORKFLOW_NAME ORDER BY RUN_START DESC, RUN_ID DESC) <= 6" in sql
    assert "MEDIAN(RUNTIME_SEC) AS BASELINE_SEC" in sql
    assert "WHERE RN = 1" in sql and "WHERE RN > 1" in sql
    # crash-short: KNOWN-FAILED tasks dropped so a crash can't depress the baseline, but a
    # NULL/unknown status is KEPT (matches the runtimes panel), so the NOT IN is NULL-guarded
    assert "s.TASK_STATUS IS NULL OR UPPER(s.TASK_STATUS) NOT IN (" in sql and "'FAILED'" in sql
    # matched per task on workflow+task; biggest slowdown first
    assert "b.WORKFLOW_NAME = l.WORKFLOW_NAME AND b.TASK_NAME = l.TASK_NAME" in sql
    assert "ORDER BY SLOWER_BY_SEC DESC" in sql
    # SLOWER_BY_* (not DELTA_*) so the _SEC humanizes as a duration, not a signed delta
    assert "SLOWER_BY_SEC" in sql and "SLOWER_BY_PCT" in sql
    assert "DELTA" not in sql


def test_drift_scan_materiality_gate_in_sql() -> None:
    # both the absolute-seconds AND the ratio gate live in the SQL (deterministic,
    # not a post-filter), and divide-by-zero on a 0-second baseline is guarded
    sql = etl.workflow_runtime_drift_scan(_CTRL, min_abs_sec=60, min_ratio=1.5)
    assert "l.LATEST_SEC - b.BASELINE_SEC) >= 60" in sql
    assert "l.LATEST_SEC >= b.BASELINE_SEC * 1.5" in sql
    assert "NULLIF(b.BASELINE_SEC, 0)" in sql


def test_drift_scan_fail_closed() -> None:
    assert etl.workflow_runtime_drift_scan("") == ""
    assert etl.workflow_runtime_drift_scan(None) == ""
    assert etl.workflow_runtime_drift_scan("T; DROP TABLE X") == ""
    assert etl.workflow_runtime_drift_scan("a b c") == ""


def test_task_runtime_history_scan_basic_and_window() -> None:
    sql = etl.task_runtime_history_scan(_CTRL, baseline_runs=10, days=30)
    assert _CTRL in sql
    # per-workflow run ranking (the same foundation as the drift builder), whole series kept
    assert "PARTITION BY WORKFLOW_NAME ORDER BY RUN_START DESC, RUN_ID DESC" in sql
    assert "AS RN" in sql and "AS RUNTIME_SEC" in sql
    # one row per (workflow, task, run), ordered so the forecaster reads a clean per-task series
    assert "ORDER BY s.WORKFLOW_NAME, s.TASK_NAME, r.RN" in sql
    # cap by WHOLE series (DENSE_RANK), not a flat row cap: a row LIMIT ordered by task would
    # bisect the boundary task's series and bias its fit. The row LIMIT is only a backstop, sized
    # strictly above the series bound so it can never bind.
    assert "QUALIFY DENSE_RANK() OVER (ORDER BY s.WORKFLOW_NAME, s.TASK_NAME) <=" in sql
    assert etl.MAX_HISTORY_ROWS > etl.MAX_HISTORY_SERIES * etl.CREEP_BASELINE_RUNS
    # FAILED tasks dropped (a crashed-short run must not fake a downward blip); window honored
    assert "NOT IN (" in sql and "'FAILED'" in sql
    assert "TASK_START_DTTM >= DATEADD('day', -30, CURRENT_TIMESTAMP())" in sql


def test_task_runtime_history_scan_fail_closed() -> None:
    assert etl.task_runtime_history_scan("") == ""
    assert etl.task_runtime_history_scan(None) == ""
    assert etl.task_runtime_history_scan("T; DROP TABLE X") == ""
    assert etl.task_runtime_history_scan("a b c") == ""
    # no window filter when unscoped
    assert "DATEADD" not in etl.task_runtime_history_scan(_CTRL)


def test_task_runtime_history_scan_parses() -> None:
    import pytest
    sqlglot = pytest.importorskip("sqlglot")
    sqlglot.parse(etl.task_runtime_history_scan(_CTRL, days=14), dialect="snowflake")
    sqlglot.parse(etl.task_runtime_history_scan(_CTRL), dialect="snowflake")


def test_drift_scan_parses() -> None:
    import pytest
    sqlglot = pytest.importorskip("sqlglot")
    sqlglot.parse(etl.workflow_runtime_drift_scan(_CTRL), dialect="snowflake")


# --- run_inventory_scan / run_params_scan (CONTROL_RUN_ID / CONTROL_PARAMS) ---

_RUNID = "ALFA_EDW_PRD.PUBLIC.CONTROL_RUN_ID"
_PARAMS = "ALFA_EDW_PRD.PUBLIC.CONTROL_PARAMS"


def test_run_inventory_scan_basic() -> None:
    sql = etl.run_inventory_scan(_RUNID)
    assert _RUNID in sql
    assert "GROUP BY RUN_ID" in sql
    assert "COUNT(DISTINCT TASK_NAME) AS TASKS" in sql
    assert "MIN(INSERT_TS) AS STARTED_AT" in sql and "MAX(INSERT_TS) AS LAST_SEEN_AT" in sql
    # calculated runtime (Last Seen - Started); _SEC name humanizes to Hr/Min/Sec
    assert "DATEDIFF('second', MIN(INSERT_TS), MAX(INSERT_TS)) AS RUNTIME_SEC" in sql
    assert "WHERE RUN_ID IS NOT NULL" in sql  # no phantom NULL-key run (matches siblings)
    assert "ORDER BY STARTED_AT DESC" in sql and "LIMIT" in sql


def test_run_params_scan_latest_run_only() -> None:
    sql = etl.run_params_scan(_PARAMS)
    assert _PARAMS in sql
    # no run_id -> latest run only (newest INSERT_TS), its params by scope then name
    assert "QUALIFY ROW_NUMBER() OVER (ORDER BY INSERT_TS DESC) = 1" in sql
    assert "p.PARAM_NAME, p.PARAM_VALUE, p.SCOPE_TYPE, p.SCOPE_NAME" in sql
    assert "ORDER BY p.SCOPE_TYPE, p.PARAM_NAME" in sql


def test_run_params_scan_specific_run_binds_a_literal() -> None:
    sql = etl.run_params_scan(_PARAMS, run_id="abc-123")
    # a chosen run filters to that RUN_ID as an escaped literal, NOT the latest CTE
    assert "WHERE p.RUN_ID = 'abc-123'" in sql
    assert "QUALIFY" not in sql
    # a hostile run id is escaped (single-quote doubled) so it stays one string literal
    evil = etl.run_params_scan(_PARAMS, run_id="x' OR '1'='1")
    assert "'x'' OR ''1''=''1'" in evil  # doubled quotes = no break-out of the literal


def test_run_tasks_scan_basic_and_bound() -> None:
    sql = etl.run_tasks_scan(_CTRL, "run-42")
    assert _CTRL in sql
    assert "WHERE s.RUN_ID = 'run-42'" in sql
    assert "AS RUNTIME_SEC" in sql and "COALESCE(s.TASK_END_DTTM, CURRENT_TIMESTAMP())" in sql
    assert "ORDER BY RUNTIME_SEC DESC" in sql
    # fail-closed: bad FQN or empty run id -> no SQL
    assert etl.run_tasks_scan("", "r") == ""
    assert etl.run_tasks_scan(_CTRL, "") == ""
    assert etl.run_tasks_scan("a b c", "r") == ""
    # hostile run id escaped
    assert "'r'' OR 1=1--'" in etl.run_tasks_scan(_CTRL, "r' OR 1=1--")


def test_inventory_and_params_fail_closed() -> None:
    for fn in (etl.run_inventory_scan, etl.run_params_scan):
        assert fn("") == ""
        assert fn(None) == ""
        assert fn("T; DROP TABLE X") == ""
        assert fn("a b c") == ""


def test_inventory_params_tasks_parse() -> None:
    import pytest
    sqlglot = pytest.importorskip("sqlglot")
    sqlglot.parse(etl.run_inventory_scan(_RUNID), dialect="snowflake")
    sqlglot.parse(etl.run_params_scan(_PARAMS), dialect="snowflake")
    sqlglot.parse(etl.run_params_scan(_PARAMS, run_id="abc"), dialect="snowflake")
    sqlglot.parse(etl.run_tasks_scan(_CTRL, "abc"), dialect="snowflake")


# --- recon_errors_scan (Phase 3: RECON_MTRC_ERROR) ---------------------------

_RECON = "ALFA_EDW_PRD.DB_T_PROD_CORE.RECON_MTRC_ERROR"


def test_recon_errors_scan_basic() -> None:
    sql = etl.recon_errors_scan(_RECON, days=30)
    assert _RECON in sql
    assert "SOURCE_LAYER, TARGET_LAYER, SOURCE_ERROR, TARGET_ERROR" in sql
    # recent-window (lookback) + newest first
    assert "WHERE LOAD_DTTM >= DATEADD('day', -30, CURRENT_TIMESTAMP())" in sql
    assert "ORDER BY LOAD_DTTM DESC, MTRC" in sql
    assert "LIMIT" in sql


def test_recon_errors_scan_fail_closed() -> None:
    assert etl.recon_errors_scan("") == ""
    assert etl.recon_errors_scan(None) == ""
    assert etl.recon_errors_scan("T; DROP TABLE X") == ""
    assert etl.recon_errors_scan("a b c") == ""


def test_recon_errors_scan_parses() -> None:
    import pytest
    sqlglot = pytest.importorskip("sqlglot")
    sqlglot.parse(etl.recon_errors_scan(_RECON), dialect="snowflake")


# --- run_cost_attribution_scan (Phase 4: CONTROL_STATUS x ACCOUNT_USAGE) ------

def test_cost_attribution_scan_latest_run() -> None:
    sql = etl.run_cost_attribution_scan(_CTRL)
    assert _CTRL in sql
    # joins the two ACCOUNT_USAGE views: credits (QAH) + text (QH)
    assert "SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY" in sql
    assert "SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY" in sql
    # QAH can have >1 row per QUERY_ID (a query split across warehouse slices): SUM the
    # credits per query FIRST so the RN=1 task-dedup can't drop a query's other slices.
    assert "SUM(CREDITS_ATTRIBUTED_COMPUTE) AS CREDITS" in sql
    assert "GROUP BY QUERY_ID" in sql
    assert "HAVING SUM(CREDITS_ATTRIBUTED_COMPUTE) > 0" in sql
    assert "ON qh.QUERY_ID = a.QUERY_ID" in sql
    # no run_id -> latest run (newest TASK_START_DTTM), same rule the sibling panels use
    assert "QUALIFY ROW_NUMBER() OVER (ORDER BY TASK_START_DTTM DESC) = 1" in sql
    # the substring guard: charge each query to the LONGEST matching task name (so a nested
    # task name never double-counts onto its shorter prefix)
    assert "ORDER BY LENGTH(t.TASK_NAME) DESC NULLS LAST" in sql
    assert "WHERE RN = 1" in sql
    # window containment + LITERAL text match are the whole join (tags are empty here).
    # CONTAINS(UPPER(...)) not ILIKE: the '_' in SP_*/M_* task names must NOT act as a
    # single-char wildcard and loosely match (mis-charge) an unrelated query.
    assert "c.START_TIME >= t.TASK_START_DTTM AND c.START_TIME <= t.TASK_END" in sql
    assert "CONTAINS(UPPER(c.QUERY_TEXT), UPPER(t.TASK_NAME))" in sql
    assert "ILIKE" not in sql  # no wildcard-bearing operator on the task-name match
    # honest coverage: unmatched queries kept via LEFT JOIN into one labelled bucket
    assert "LEFT JOIN tasks t" in sql
    assert etl.UNATTRIBUTED_WORKFLOW in sql and etl.UNATTRIBUTED_TASK in sql
    # credits summed then reported; most-expensive task first; bounded
    assert "SUM(CREDITS)" in sql and "AS CREDITS_ATTRIBUTED" in sql
    assert "ORDER BY CREDITS_ATTRIBUTED DESC NULLS LAST" in sql and "LIMIT" in sql


def test_cost_attribution_scan_specific_run_binds_a_literal() -> None:
    sql = etl.run_cost_attribution_scan(_CTRL, run_id="dd1ecdac-ea2e")
    # a chosen run filters to that RUN_ID as an escaped literal, NOT the latest CTE
    assert "AND RUN_ID = 'dd1ecdac-ea2e'" in sql
    assert "QUALIFY" not in sql
    # a hostile run id is escaped (single-quote doubled) so it stays one string literal
    evil = etl.run_cost_attribution_scan(_CTRL, run_id="x' OR '1'='1")
    assert "'x'' OR ''1''=''1'" in evil


def test_cost_attribution_scan_fail_closed() -> None:
    assert etl.run_cost_attribution_scan("") == ""
    assert etl.run_cost_attribution_scan(None) == ""
    assert etl.run_cost_attribution_scan("T; DROP TABLE X") == ""
    assert etl.run_cost_attribution_scan("a b c") == ""


def test_cost_attribution_scan_parses() -> None:
    import pytest
    sqlglot = pytest.importorskip("sqlglot")
    sqlglot.parse(etl.run_cost_attribution_scan(_CTRL), dialect="snowflake")
    sqlglot.parse(etl.run_cost_attribution_scan(_CTRL, run_id="abc-123"), dialect="snowflake")


# --- task_status_history_scan (Phase 2c: failure-recurrence series) ----------

def test_task_status_history_scan_basic() -> None:
    sql = etl.task_status_history_scan(_CTRL, lookback_runs=20, days=14)
    assert _CTRL in sql
    assert "PARTITION BY WORKFLOW_NAME ORDER BY RUN_START DESC, RUN_ID DESC" in sql
    # KEEPS failed runs (the signal); terminal status via MAX_BY collapses an Informatica retry
    assert "MAX_BY(s.TASK_STATUS, COALESCE(s.TASK_END_DTTM, s.TASK_START_DTTM)) AS TERMINAL_STATUS" in sql
    assert "AS IS_FAILED" in sql and "AS IS_RUNNING" in sql
    assert "'FAILED'" in sql and "'RUNNING'" in sql   # both shared status sets inlined
    # whole-series cap so the row cap can't bisect a task's status series mid-streak; window honored
    assert "QUALIFY DENSE_RANK() OVER (ORDER BY s.WORKFLOW_NAME, s.TASK_NAME) <=" in sql
    assert "TASK_START_DTTM >= DATEADD('day', -14, CURRENT_TIMESTAMP())" in sql


def test_task_status_history_scan_fail_closed_and_parse() -> None:
    assert etl.task_status_history_scan("") == ""
    assert etl.task_status_history_scan(None) == ""
    assert etl.task_status_history_scan("a b c") == ""
    assert "DATEADD" not in etl.task_status_history_scan(_CTRL)   # no window filter when unscoped
    import pytest
    sqlglot = pytest.importorskip("sqlglot")
    sqlglot.parse(etl.task_status_history_scan(_CTRL, days=7), dialect="snowflake")


# --- recon_recurrence_scan (Phase 3b: reconciliation recurrence) -------------

def test_recon_recurrence_scan_basic() -> None:
    sql = etl.recon_recurrence_scan(_RECON, days=45)
    assert _RECON in sql
    # a cycle = DATE(LOAD_DTTM); the denominator is a per-FRQCY cohort (never global)
    assert "CAST(LOAD_DTTM AS DATE) AS CYCLE_DATE" in sql
    assert "DENSE_RANK() OVER (PARTITION BY FRQCY ORDER BY CYCLE_DATE DESC)" in sql
    # COALESCE all THREE nullable grain columns before any GROUP BY / equijoin (NULL=NULL trap)
    assert "COALESCE(FRQCY, '(unknown)')" in sql
    assert "COALESCE(VALUE_TYPE, '(unknown)')" in sql
    assert "COALESCE(RECON_MTRC_LAYER, '(unknown)')" in sql
    # conditional recurrence fraction + the layer hop context
    assert "AS RECURRENCE_PCT" in sql and "AS TOTAL_ERROR_CYCLES" in sql
    assert "MAX_BY(SOURCE_LAYER, LOAD_DTTM)" in sql
    assert "WHERE LOAD_DTTM >= DATEADD('day', -45, CURRENT_TIMESTAMP())" in sql


def test_recon_recurrence_scan_default_window_and_fail_closed() -> None:
    # unscoped -> the 90-day default (so monthly cadences recur), NOT the raw panel's 30
    assert "-90, CURRENT_TIMESTAMP()" in etl.recon_recurrence_scan(_RECON)
    assert etl.recon_recurrence_scan("") == ""
    assert etl.recon_recurrence_scan(None) == ""
    assert etl.recon_recurrence_scan("T; DROP TABLE X") == ""
    assert etl.recon_recurrence_scan("a b c") == ""


def test_recon_recurrence_scan_parses() -> None:
    import pytest
    sqlglot = pytest.importorskip("sqlglot")
    sqlglot.parse(etl.recon_recurrence_scan(_RECON, days=90), dialect="snowflake")
    sqlglot.parse(etl.recon_recurrence_scan(_RECON), dialect="snowflake")


# --- cycle_finish_history_scan (Phase 5: SLA finish forecast) ----------------

def test_cycle_finish_history_scan_basic() -> None:
    sql = etl.cycle_finish_history_scan(_CTRL, start_workflow="WF_START", end_workflow="WF_END", days=14)
    assert _CTRL in sql
    # night-key groups the ~22:00 start and the early-AM finish into ONE cycle
    assert "DATE(DATEADD('hour', -12, TASK_START_DTTM)) AS CYCLE_DATE" in sql
    # starter -> CYCLE_START (MIN start); terminal -> CYCLE_FINISH (latest terminal-attempt end)
    assert "MIN(TASK_START_DTTM) AS CYCLE_START" in sql
    assert "MAX(TERMINAL_END) AS CYCLE_FINISH" in sql
    # retries collapsed to each task's TERMINAL attempt before counting failures (a retried-to-success
    # terminal task must NOT count as a failed night)
    assert "MAX_BY(TASK_STATUS, COALESCE(TASK_END_DTTM, TASK_START_DTTM)) AS TERMINAL_STATUS" in sql
    assert "AS N_FAILED" in sql and "AS N_RUNNING" in sql
    # anchor workflow names bound as escaped literals (data, not identifiers)
    assert "WORKFLOW_NAME = 'WF_START'" in sql and "WORKFLOW_NAME = 'WF_END'" in sql
    assert "CURRENT_TIMESTAMP() AS SNAPSHOT_TS" in sql
    assert "TASK_START_DTTM >= DATEADD('day', -14, CURRENT_TIMESTAMP())" in sql


def test_cycle_finish_history_scan_fail_closed_and_injection() -> None:
    # needs BOTH anchor workflows AND a valid FQN
    assert etl.cycle_finish_history_scan(_CTRL, start_workflow="", end_workflow="WF_END") == ""
    assert etl.cycle_finish_history_scan(_CTRL, start_workflow="WF_START", end_workflow="") == ""
    assert etl.cycle_finish_history_scan("", start_workflow="A", end_workflow="B") == ""
    assert etl.cycle_finish_history_scan("a b c", start_workflow="A", end_workflow="B") == ""
    # a hostile workflow name stays inside the literal (single-quote doubled)
    evil = etl.cycle_finish_history_scan(_CTRL, start_workflow="x' OR '1'='1", end_workflow="B")
    assert "'x'' OR ''1''=''1'" in evil


def test_cycle_finish_history_scan_parses() -> None:
    import pytest
    sqlglot = pytest.importorskip("sqlglot")
    sqlglot.parse(etl.cycle_finish_history_scan(_CTRL, start_workflow="A", end_workflow="B", days=7),
                  dialect="snowflake")
    sqlglot.parse(etl.cycle_finish_history_scan(_CTRL, start_workflow="A", end_workflow="B"),
                  dialect="snowflake")
