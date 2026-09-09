"""ETL process-control readers — the customer's Informatica-orchestrated nightly cycle.

Phase 1: reference-data gap detection. The nightly load fails when a source
system emits a code that has no translation row in the reference (XLAT) table,
so this finds codes present in a *staging* table but MISSING from the reference
table — the manual morning ``MINUS`` check, generalized across the whole XLAT
code family and driven from SETTINGS config (adding a check is an Admin edit, not
a code change).

Config (Admin ▸ SETTINGS):
  ETL_REF_GAP_XLAT    the reference/translation table FQN, e.g.
                      ALFA_EDW_PRD.DB_V_PROD_BASE.TERADATA_ETL_REF_XLAT
  ETL_REF_GAP_CHECKS  one check per line (or ';'-separated); blank / '#' lines
                      are ignored:
                        <src_idntftn_nm> | <staging_fqn> | <staging_code_col>
                      e.g.
                        pc_uwissuetype.code | ALFA_EDW_PRD.DB_T_PROD_STAG.PC_UWISSUETYPE | CODE_STG

The reference table is matched on SRC_IDNTFTN_NM = <src_idntftn_nm>; its code
column is SRC_IDNTFTN_VAL (the Teradata ETL translation convention). Every
table / column is validated with safe_identifier, so a malformed config row
fails closed (its check is dropped and reported) rather than emitting unsafe
SQL. Pure module: bounded output, no Streamlit, no dollar rates.
"""

from __future__ import annotations

from dataclasses import dataclass

# The Teradata ETL translation (XLAT) table's own column convention. These are
# fixed for the reference table (the family axis is SRC_IDNTFTN_NM, the translated
# value is SRC_IDNTFTN_VAL); only the staging side varies per code type, which is
# why the staging table + column live in config while these stay constants.
XLAT_NAME_COL = "SRC_IDNTFTN_NM"
XLAT_VALUE_COL = "SRC_IDNTFTN_VAL"

MAX_CODES = 1000  # a real gap is a handful of codes; the cap only guards a misconfig


@dataclass(frozen=True)
class RefGapCheck:
    """One reference-gap check: staging code column vs the XLAT family member.

    ``name`` is the SRC_IDNTFTN_NM value (e.g. ``pc_uwissuetype.code``); it is
    both the label and the XLAT filter, so one config field drives both sides.
    ``pinned`` checks (config prefix ``*``) always show, ignoring the scope-bar
    Database filter — the operator's every-morning check stays visible whatever
    database is scoped. Every other check honors the Database filter, keyed on
    ``database`` (the first segment of its staging FQN).
    """

    name: str
    staging_fqn: str
    staging_col: str
    pinned: bool = False

    @property
    def database(self) -> str:
        """The staging table's database (first FQN segment), for the scope filter."""
        return self.staging_fqn.split(".", 1)[0].strip().upper()


def parse_ref_gap_checks(raw: object) -> tuple[list[RefGapCheck], list[str]]:
    """Parse the ETL_REF_GAP_CHECKS setting into checks + parse warnings.

    One check per entry, ``[*]<name> | <staging_fqn> | <staging_col>``. A leading
    ``*`` on the name pins the check (always shown, ignores the Database filter).
    Entries are separated by newlines OR ``;`` (so the config survives a
    single-line Admin text field); blank entries and those beginning with ``#``
    are ignored. A malformed entry (not exactly three non-empty fields) is skipped
    and named in the returned warnings — a bad row never silently drops a check
    with no signal.
    """
    text = str(raw or "").strip()
    if not text:
        return [], []
    checks: list[RefGapCheck] = []
    warnings: list[str] = []
    # ';' and newline are interchangeable entry separators.
    entries = [seg.strip() for seg in text.replace(";", "\n").splitlines()]
    for seg in entries:
        if not seg or seg.startswith("#"):
            continue
        parts = [p.strip() for p in seg.split("|")]
        if len(parts) != 3 or not all(parts):
            warnings.append(f"Ignored malformed check (need name | staging_table | code_column): {seg!r}")
            continue
        name, pinned = parts[0], False
        if name.startswith("*"):
            name, pinned = name[1:].strip(), True
        if not name:
            warnings.append(f"Ignored malformed check (empty name): {seg!r}")
            continue
        checks.append(RefGapCheck(name=name, staging_fqn=parts[1], staging_col=parts[2], pinned=pinned))
    return checks, warnings


def filter_checks_by_database(
    checks: list[RefGapCheck], database: str
) -> list[RefGapCheck]:
    """Keep checks in the scope-bar Database, plus every pinned check.

    ``database`` is the scope bar's exact database name; ``""`` (all) keeps every
    check. Pinned checks are always kept — the every-morning pc_uwissuetype check
    stays visible whatever database is scoped, per the owner's ask.
    """
    db = str(database or "").strip().upper()
    if not db:
        return list(checks)
    return [c for c in checks if c.pinned or c.database == db]


def _check_sql(check: RefGapCheck, xlat_fqn: str) -> str:
    """One check's gap subquery: staging codes MINUS the XLAT codes for its family.

    MINUS (not NOT IN) mirrors the operator's proven manual query and is NULL-safe
    — a NULL in the reference column can't swallow the whole result the way a
    NOT-IN subquery would. The outer TO_VARCHAR keeps NEW_CODE type-stable so the
    per-check subqueries UNION cleanly even when their code columns differ in type.
    Identifiers are validated (fail-closed); the family name is a quoted literal.
    """
    from app.core.sqlsafe import safe_identifier, sql_literal

    stg = safe_identifier(check.staging_fqn, allow_qualified=True)
    col = safe_identifier(check.staging_col)
    xlat = safe_identifier(xlat_fqn, allow_qualified=True)
    name_lit = sql_literal(check.name)
    return (
        f"SELECT {name_lit} AS CHECK_NAME, TO_VARCHAR(g.NEW_CODE) AS NEW_CODE FROM (\n"
        f"  SELECT s.{col} AS NEW_CODE FROM {stg} s WHERE s.{col} IS NOT NULL\n"
        f"  MINUS\n"
        f"  SELECT x.{XLAT_VALUE_COL} FROM {xlat} x WHERE x.{XLAT_NAME_COL} = {name_lit}\n"
        f") g"
    )


def reference_gap_scan(
    checks: list[RefGapCheck], xlat_fqn: str, *, max_codes: int = MAX_CODES
) -> tuple[str, list[str]]:
    """Build the UNION-ALL gap scan across all checks + per-check build errors.

    Returns ``(sql, errors)``. A check whose identifiers fail validation is
    dropped and named in ``errors`` (one bad table name never kills the whole
    scan). ``sql`` is ``""`` when no valid check survives OR the reference table
    is unset — the caller then renders a setup hint instead of running nothing.
    ORDER BY + LIMIT live IN the SQL so the row cap truncates deterministically.
    """
    if not str(xlat_fqn or "").strip() or not checks:
        return "", []
    parts: list[str] = []
    errors: list[str] = []
    for check in checks:
        try:
            parts.append(_check_sql(check, xlat_fqn))
        except ValueError as exc:
            errors.append(f"Check {check.name!r} skipped: {exc}")
    if not parts:
        return "", errors
    union = "\nUNION ALL\n".join(parts)
    sql = (
        "SELECT CHECK_NAME, NEW_CODE FROM (\n"
        f"{union}\n"
        f") ORDER BY CHECK_NAME, NEW_CODE\nLIMIT {int(max_codes)}"
    )
    return sql, errors


# --- Phase 2: workflow runtimes / status (Informatica CONTROL_STATUS) --------
# Alfa's nightly cycle is Informatica-orchestrated stored-proc CALLs, so the task
# runtimes + statuses are INVISIBLE to Snowflake's ACCOUNT_USAGE.TASK_HISTORY (which
# only records native Snowflake TASKs). CONTROL_STATUS is the only record of each
# task's start/end/status per run, keyed by RUN_ID. Config: ETL_CONTROL_STATUS_FQN.

MAX_TASKS = 1000  # a nightly run is a few hundred tasks; the cap only guards a misconfig

# TASK_STATUS interpretation (Informatica), shared by the Operations panel and the
# Brief signal so both read failures the same way. Only KNOWN failure states are a
# failure (red / a morning fire); RUNNING states are in-progress, never a failure; an
# unknown status is neither and still shows verbatim in the runtimes table.
FAILED_TASK_STATUSES = frozenset(
    {"FAILED", "ABORTED", "ERROR", "ERRORED", "STOPPED", "TERMINATED", "KILLED"}
)
RUNNING_TASK_STATUSES = frozenset(
    {"RUNNING", "STARTED", "IN PROGRESS", "IN-PROGRESS", "SCHEDULED", "QUEUED"}
)


MAX_WORKFLOWS = 200  # distinct workflows for the runtimes picker (a cycle has dozens; bounded)


def _window_clause(days: object, col: str = "TASK_START_DTTM", indent: str = "  ") -> str:
    """A ``AND <col> >= DATEADD('day', -N, now)`` line honoring the scope-bar Window.

    ``days <= 0`` (the default / 'all') returns ``""`` — no window filter — so callers
    that don't scope a window behave exactly as before. Shared by the runtimes, list, and
    drift readers so the scope bar means the same thing everywhere. A non-numeric ``days``
    (None, a stray string) is treated as unscoped, never a crash."""
    n = int(days) if isinstance(days, (int, float)) else 0
    if n <= 0:
        return ""
    return f"{indent}AND {col} >= DATEADD('day', -{n}, CURRENT_TIMESTAMP())\n"


def workflow_list_scan(control_fqn: object, *, days: object = 0, max_rows: int = MAX_WORKFLOWS) -> str:
    """Distinct workflows with a run in the Window, for the runtimes picker.

    One row per WORKFLOW_NAME: LAST_RUN_AT (newest TASK_START_DTTM) and RUNS (distinct
    RUN_IDs seen in the window), most-recent first — so the picker defaults to the workflow
    that ran most recently and only lists workflows actually active in the scoped Window.
    Fail-closed on a bad FQN. Pure: bounded output, no Streamlit."""
    from app.core.sqlsafe import safe_identifier

    fqn = str(control_fqn or "").strip()
    if not fqn:
        return ""
    try:
        tbl = safe_identifier(fqn, allow_qualified=True)
    except ValueError:
        return ""
    return (
        "SELECT WORKFLOW_NAME, MAX(TASK_START_DTTM) AS LAST_RUN_AT,\n"
        "       COUNT(DISTINCT RUN_ID) AS RUNS\n"
        f"  FROM {tbl}\n"
        "  WHERE TASK_START_DTTM IS NOT NULL AND WORKFLOW_NAME IS NOT NULL\n"
        f"{_window_clause(days)}"
        "  GROUP BY WORKFLOW_NAME\n"
        "  ORDER BY LAST_RUN_AT DESC\n"
        f"  LIMIT {int(max_rows)}"
    )


def workflow_runtimes_scan(
    control_fqn: object, *, workflow: object = "", days: object = 0, max_tasks: int = MAX_TASKS
) -> str:
    """A workflow's latest run's per-task runtimes from the Informatica CONTROL_STATUS table.

    With ``workflow`` set, isolates the newest RUN_ID FOR THAT WORKFLOW (bound as an escaped
    literal — a name is data, not an identifier) within the scoped Window; otherwise the
    globally-newest run (back-compatible default). Per-workflow matters because each RUN_ID is
    ONE workflow's execution, so the single globally-latest run only ever shows one workflow —
    the picker lets the operator see any workflow's latest run, not just whichever finished last.
    ``days`` (> 0) honors the scope-bar Window; ``0`` means all time. Returns one row per task:
    WORKFLOW_NAME, TASK_NAME, TASK_STATUS, the start/end window, and RUNTIME_SEC = end − start (a
    still-running task with a NULL end is measured to CURRENT_TIMESTAMP(), so a hung task
    surfaces). Slowest-first; the ``_SEC`` name humanizes to Hr/Min/Sec. Fail-closed on a bad
    FQN. Pure: bounded output, no Streamlit."""
    from app.core.sqlsafe import safe_identifier, sql_literal

    fqn = str(control_fqn or "").strip()
    if not fqn:
        return ""
    try:
        tbl = safe_identifier(fqn, allow_qualified=True)
    except ValueError:
        return ""
    _wf = str(workflow or "").strip()
    wf_filter = f"    AND WORKFLOW_NAME = {sql_literal(_wf)}\n" if _wf else ""
    return (
        "WITH latest AS (\n"
        f"  SELECT RUN_ID FROM {tbl}\n"
        "  WHERE TASK_START_DTTM IS NOT NULL\n"
        f"{wf_filter}"
        f"{_window_clause(days, indent='  ')}"
        "  QUALIFY ROW_NUMBER() OVER (ORDER BY TASK_START_DTTM DESC) = 1\n"
        ")\n"
        "SELECT s.WORKFLOW_NAME, s.TASK_NAME, s.TASK_STATUS,\n"
        "       s.TASK_START_DTTM, s.TASK_END_DTTM,\n"
        "       DATEDIFF('second', s.TASK_START_DTTM,\n"
        "                COALESCE(s.TASK_END_DTTM, CURRENT_TIMESTAMP())) AS RUNTIME_SEC\n"
        f"  FROM {tbl} s\n"
        "  JOIN latest l ON s.RUN_ID = l.RUN_ID\n"
        "  ORDER BY RUNTIME_SEC DESC, s.TASK_START_DTTM\n"
        f"  LIMIT {int(max_tasks)}"
    )


MAX_DRIFT_ROWS = 200     # material slowdowns are few; bounded output
DRIFT_MIN_ABS_SEC = 60   # ignore a < 1-minute absolute change (noise)
DRIFT_MIN_RATIO = 1.5    # AND require the latest to be >= 1.5x the baseline median


def workflow_runtime_drift_scan(
    control_fqn: object,
    *,
    baseline_runs: int = 5,
    min_abs_sec: int = DRIFT_MIN_ABS_SEC,
    min_ratio: float = DRIFT_MIN_RATIO,
    max_rows: int = MAX_DRIFT_ROWS,
) -> str:
    """Tasks that ran materially SLOWER than the same workflow's recent baseline.

    Ranks each workflow's runs by recency (PARTITION BY WORKFLOW_NAME) — because each
    RUN_ID is one workflow's execution and the same workflow runs on a cadence, so a
    task is compared to its OWN prior runs, never to a different workflow from the same
    night. For each (WORKFLOW_NAME, TASK_NAME) it compares the newest run's runtime to
    the MEDIAN of the baseline runs; only a material slowdown surfaces — at least
    ``min_abs_sec`` seconds AND at least ``min_ratio``x the baseline. CONTROL_STATUS holds
    the mapping + child tasks (SP_*/M_*), not a 'ROOT' total row (that lives in
    CONTROL_RUN_ID), so each drifting row is the specific child task, not the whole
    workflow. FAILED tasks are dropped from the runtime series (a crashed-short run must
    not depress the baseline). A workflow with only one
    run has no baseline and is absent by design. Fail-closed on a bad FQN. The _SEC
    columns humanize to Hr/Min/Sec; SLOWER_BY_* stay positive (a duration, not a signed
    delta). Pure: bounded output, no Streamlit."""
    from app.core.sqlsafe import safe_identifier

    fqn = str(control_fqn or "").strip()
    if not fqn:
        return ""
    try:
        tbl = safe_identifier(fqn, allow_qualified=True)
    except ValueError:
        return ""
    keep = 1 + max(1, int(baseline_runs))
    # crash-short guard: a FAILED task's truncated runtime must not depress the baseline
    # median or read as "fast" (shared set, so the panel + this never disagree).
    _failed = ", ".join(f"'{s}'" for s in sorted(FAILED_TASK_STATUSES))
    return (
        # Rank runs PER WORKFLOW: each RUN_ID is one workflow's execution and the SAME
        # workflow runs on a cadence, so a task must be compared to its OWN prior runs.
        # (Ranking globally compares different workflows from one night → empty baseline.)
        "WITH runs AS (\n"
        f"  SELECT RUN_ID, WORKFLOW_NAME, MAX(TASK_START_DTTM) AS RUN_START FROM {tbl}\n"
        "  WHERE TASK_START_DTTM IS NOT NULL AND RUN_ID IS NOT NULL\n"
        "  GROUP BY RUN_ID, WORKFLOW_NAME\n"
        # RUN_ID DESC tiebreaker so 'latest' is deterministic on a same-second RUN_START tie
        f"  QUALIFY ROW_NUMBER() OVER (PARTITION BY WORKFLOW_NAME ORDER BY RUN_START DESC, RUN_ID DESC) <= {keep}\n"
        "),\n"
        "ranked AS (\n"
        "  SELECT RUN_ID, WORKFLOW_NAME,\n"
        "         ROW_NUMBER() OVER (PARTITION BY WORKFLOW_NAME ORDER BY RUN_START DESC, RUN_ID DESC) AS RN\n"
        "  FROM runs\n"
        "),\n"
        "task_runtimes AS (\n"
        "  SELECT s.WORKFLOW_NAME, s.TASK_NAME, r.RN,\n"
        "         MAX(DATEDIFF('second', s.TASK_START_DTTM,\n"
        "             COALESCE(s.TASK_END_DTTM, CURRENT_TIMESTAMP()))) AS RUNTIME_SEC\n"
        f"  FROM {tbl} s JOIN ranked r ON s.RUN_ID = r.RUN_ID AND s.WORKFLOW_NAME = r.WORKFLOW_NAME\n"
        "  WHERE s.TASK_START_DTTM IS NOT NULL\n"
        # NULL-safe: an unknown-status task is KEPT (NOT IN is NULL-blind), matching the
        # runtimes panel's 'unknown status still shows' rule — only KNOWN failures drop.
        f"    AND (s.TASK_STATUS IS NULL OR UPPER(s.TASK_STATUS) NOT IN ({_failed}))\n"
        "  GROUP BY s.WORKFLOW_NAME, s.TASK_NAME, r.RN\n"
        "),\n"
        "latest AS (\n"
        "  SELECT WORKFLOW_NAME, TASK_NAME, RUNTIME_SEC AS LATEST_SEC\n"
        "  FROM task_runtimes WHERE RN = 1\n"
        "),\n"
        "baseline AS (\n"
        "  SELECT WORKFLOW_NAME, TASK_NAME, MEDIAN(RUNTIME_SEC) AS BASELINE_SEC,\n"
        "         COUNT(*) AS BASELINE_RUNS\n"
        "  FROM task_runtimes WHERE RN > 1\n"
        "  GROUP BY WORKFLOW_NAME, TASK_NAME\n"
        ")\n"
        # Each row is a mapping / child task (SP_*/M_*) — CONTROL_STATUS has no 'ROOT' total
        # row (that lives in CONTROL_RUN_ID), so a drifting row names the specific step.
        "SELECT l.WORKFLOW_NAME, l.TASK_NAME,\n"
        "       l.LATEST_SEC, b.BASELINE_SEC, b.BASELINE_RUNS,\n"
        "       (l.LATEST_SEC - b.BASELINE_SEC) AS SLOWER_BY_SEC,\n"
        "       ROUND(100.0 * (l.LATEST_SEC - b.BASELINE_SEC) / NULLIF(b.BASELINE_SEC, 0), 0) AS SLOWER_BY_PCT\n"
        "  FROM latest l\n"
        "  JOIN baseline b ON b.WORKFLOW_NAME = l.WORKFLOW_NAME AND b.TASK_NAME = l.TASK_NAME\n"
        f"  WHERE (l.LATEST_SEC - b.BASELINE_SEC) >= {int(min_abs_sec)}\n"
        f"    AND l.LATEST_SEC >= b.BASELINE_SEC * {float(min_ratio)}\n"
        "  ORDER BY SLOWER_BY_SEC DESC\n"
        f"  LIMIT {int(max_rows)}"
    )


# --- Phase 2b: runtime-creep forecaster (per-task runtime series) ------------
CREEP_BASELINE_RUNS = 10    # runs of history to fit the trend over (the forecaster reads a series)
MAX_HISTORY_SERIES = 500    # distinct (workflow, task) series to analyze — the REAL cap
# Row backstop, sized STRICTLY above the series bound (series × runs) so neither this LIMIT nor the
# read-layer row cap can ever bisect a task's series mid-way (which would corrupt its trend fit) —
# the whole-series QUALIFY is the only cap that actually bounds the result.
MAX_HISTORY_ROWS = MAX_HISTORY_SERIES * CREEP_BASELINE_RUNS + 500


def task_runtime_history_scan(
    control_fqn: object, *, baseline_runs: int = CREEP_BASELINE_RUNS, days: object = 0,
    max_series: int = MAX_HISTORY_SERIES, max_rows: int = MAX_HISTORY_ROWS,
) -> str:
    """Per-(workflow, task) runtime across the last N runs — the series the creep forecaster fits.

    Reuses the drift builder's per-workflow run ranking (each RUN_ID is one workflow's execution,
    so runs are ranked WITHIN a workflow), but returns the WHOLE series (one row per run, RN=1 =
    newest) rather than latest-vs-baseline — the Python Theil-Sen fit needs every point. Columns:
    WORKFLOW_NAME, TASK_NAME, RN, RUN_START, RUNTIME_SEC (end − start, a running task measured to
    now). FAILED tasks are dropped so a crashed-short run can't fake a downward blip. ``days`` (> 0)
    honors the scope-bar Window.

    The result is capped by whole SERIES (``max_series`` via DENSE_RANK), never by a flat row cap:
    a row-only LIMIT ordered by task would bisect the boundary task's series (keeping its newest
    runs, dropping its oldest) and bias its fit. A series is therefore wholly present or wholly
    absent; ``max_rows`` is a backstop sized above the series bound so it can never bind. Fail-closed
    on a bad FQN. Pure: bounded output, no Streamlit."""
    from app.core.sqlsafe import safe_identifier

    fqn = str(control_fqn or "").strip()
    if not fqn:
        return ""
    try:
        tbl = safe_identifier(fqn, allow_qualified=True)
    except ValueError:
        return ""
    keep = max(2, int(baseline_runs))
    _failed = ", ".join(f"'{s}'" for s in sorted(FAILED_TASK_STATUSES))
    return (
        "WITH runs AS (\n"
        f"  SELECT RUN_ID, WORKFLOW_NAME, MAX(TASK_START_DTTM) AS RUN_START FROM {tbl}\n"
        "  WHERE TASK_START_DTTM IS NOT NULL AND RUN_ID IS NOT NULL\n"
        f"{_window_clause(days, indent='  ')}"
        "  GROUP BY RUN_ID, WORKFLOW_NAME\n"
        f"  QUALIFY ROW_NUMBER() OVER (PARTITION BY WORKFLOW_NAME ORDER BY RUN_START DESC, RUN_ID DESC) <= {keep}\n"
        "),\n"
        "ranked AS (\n"
        "  SELECT RUN_ID, WORKFLOW_NAME, RUN_START,\n"
        "         ROW_NUMBER() OVER (PARTITION BY WORKFLOW_NAME ORDER BY RUN_START DESC, RUN_ID DESC) AS RN\n"
        "  FROM runs\n"
        ")\n"
        "SELECT s.WORKFLOW_NAME, s.TASK_NAME, r.RN, r.RUN_START,\n"
        "       MAX(DATEDIFF('second', s.TASK_START_DTTM,\n"
        "           COALESCE(s.TASK_END_DTTM, CURRENT_TIMESTAMP()))) AS RUNTIME_SEC\n"
        f"  FROM {tbl} s JOIN ranked r ON s.RUN_ID = r.RUN_ID AND s.WORKFLOW_NAME = r.WORKFLOW_NAME\n"
        "  WHERE s.TASK_START_DTTM IS NOT NULL\n"
        f"    AND (s.TASK_STATUS IS NULL OR UPPER(s.TASK_STATUS) NOT IN ({_failed}))\n"
        "  GROUP BY s.WORKFLOW_NAME, s.TASK_NAME, r.RN, r.RUN_START\n"
        # cap by WHOLE series so the row cap can never bisect a task's series mid-fit
        f"  QUALIFY DENSE_RANK() OVER (ORDER BY s.WORKFLOW_NAME, s.TASK_NAME) <= {int(max_series)}\n"
        "  ORDER BY s.WORKFLOW_NAME, s.TASK_NAME, r.RN\n"
        f"  LIMIT {int(max_rows)}"
    )


# --- Phase 2c: failure-recurrence (per-task run STATUS series) ---------------
# Unlike task_runtime_history_scan (which DROPS failed runs — they'd depress the runtime
# baseline), this KEEPS them: a failed run is the whole signal. Per (workflow, task) it emits
# the TERMINAL status of each recent run (RN=1 newest). MAX_BY(TASK_STATUS, COALESCE(end,start))
# collapses an Informatica retry to the run's LAST attempt in one aggregate — a FAILED-then-
# retried-SUCCESS run reads SUCCESS (the honest operational outcome). IS_FAILED / IS_RUNNING are
# flagged off the SHARED status sets so this, the runtimes panel, and the Brief never disagree.
FAILREC_LOOKBACK_RUNS = 20   # ~3 weeks of nightly runs — a stabler failure rate than the last few
MAX_FAILREC_ROWS = MAX_HISTORY_SERIES * FAILREC_LOOKBACK_RUNS + 500  # backstop above the series bound


def task_status_history_scan(
    control_fqn: object, *, lookback_runs: int = FAILREC_LOOKBACK_RUNS, days: object = 0,
    max_series: int = MAX_HISTORY_SERIES, max_rows: int = MAX_FAILREC_ROWS,
) -> str:
    """Per-(workflow, task) TERMINAL status across the last N runs — the series the recurrence
    estimator folds. One row per (workflow, task, run): RN (1 = newest), RUN_START, TERMINAL_STATUS,
    IS_FAILED, IS_RUNNING.

    KEEPS failed runs (the signal). MAX_BY(TASK_STATUS, COALESCE(TASK_END_DTTM, TASK_START_DTTM))
    collapses retries to the run's terminal attempt in one aggregate. Reuses the drift builder's
    per-workflow run ranking + whole-series DENSE_RANK cap so a task's status series is never bisected
    mid-streak. ``days`` (> 0) honors the scope-bar Window. Fail-closed on a bad FQN. Pure, bounded."""
    from app.core.sqlsafe import safe_identifier

    fqn = str(control_fqn or "").strip()
    if not fqn:
        return ""
    try:
        tbl = safe_identifier(fqn, allow_qualified=True)
    except ValueError:
        return ""
    keep = max(2, int(lookback_runs))
    _failed = ", ".join(f"'{s}'" for s in sorted(FAILED_TASK_STATUSES))
    _running = ", ".join(f"'{s}'" for s in sorted(RUNNING_TASK_STATUSES))
    return (
        "WITH runs AS (\n"
        f"  SELECT RUN_ID, WORKFLOW_NAME, MAX(TASK_START_DTTM) AS RUN_START FROM {tbl}\n"
        "  WHERE TASK_START_DTTM IS NOT NULL AND RUN_ID IS NOT NULL\n"
        f"{_window_clause(days, indent='  ')}"
        "  GROUP BY RUN_ID, WORKFLOW_NAME\n"
        f"  QUALIFY ROW_NUMBER() OVER (PARTITION BY WORKFLOW_NAME ORDER BY RUN_START DESC, RUN_ID DESC) <= {keep}\n"
        "),\n"
        "ranked AS (\n"
        "  SELECT RUN_ID, WORKFLOW_NAME, RUN_START,\n"
        "         ROW_NUMBER() OVER (PARTITION BY WORKFLOW_NAME ORDER BY RUN_START DESC, RUN_ID DESC) AS RN\n"
        "  FROM runs\n"
        "),\n"
        # one row per (workflow, task, run) with the run's TERMINAL status (retries collapsed).
        "per_task AS (\n"
        "  SELECT s.WORKFLOW_NAME, s.TASK_NAME, r.RN, r.RUN_START,\n"
        "         MAX_BY(s.TASK_STATUS, COALESCE(s.TASK_END_DTTM, s.TASK_START_DTTM)) AS TERMINAL_STATUS\n"
        f"  FROM {tbl} s JOIN ranked r ON s.RUN_ID = r.RUN_ID AND s.WORKFLOW_NAME = r.WORKFLOW_NAME\n"
        "  WHERE s.TASK_START_DTTM IS NOT NULL AND s.TASK_NAME IS NOT NULL\n"
        "  GROUP BY s.WORKFLOW_NAME, s.TASK_NAME, r.RN, r.RUN_START\n"
        f"  QUALIFY DENSE_RANK() OVER (ORDER BY s.WORKFLOW_NAME, s.TASK_NAME) <= {int(max_series)}\n"
        ")\n"
        "SELECT WORKFLOW_NAME, TASK_NAME, RN, RUN_START, TERMINAL_STATUS,\n"
        f"       CASE WHEN UPPER(TERMINAL_STATUS) IN ({_failed})  THEN 1 ELSE 0 END AS IS_FAILED,\n"
        f"       CASE WHEN UPPER(TERMINAL_STATUS) IN ({_running}) THEN 1 ELSE 0 END AS IS_RUNNING\n"
        "  FROM per_task\n"
        "  ORDER BY WORKFLOW_NAME, TASK_NAME, RN\n"
        f"  LIMIT {int(max_rows)}"
    )


# --- Phase 2: run inventory + parameters (CONTROL_RUN_ID / CONTROL_PARAMS) ----
MAX_RUNS = 100      # recent-run inventory cap
MAX_PARAMS = 2000   # one run's parameters (the global + per-session knobs)


def run_inventory_scan(run_id_fqn: object, *, max_runs: int = MAX_RUNS) -> str:
    """Recent ETL runs from the Informatica CONTROL_RUN_ID registry.

    One row per RUN_ID: the workflow(s) it registered, the distinct task count, the
    first/last INSERT_TS seen for the run (STARTED_AT / LAST_SEEN_AT), and RUNTIME_SEC =
    the span between them (Last Seen − Started, humanizes to Hr/Min/Sec). Newest run
    first. Fail-closed on a bad FQN. Pure: bounded output, no Streamlit."""
    from app.core.sqlsafe import safe_identifier

    fqn = str(run_id_fqn or "").strip()
    if not fqn:
        return ""
    try:
        tbl = safe_identifier(fqn, allow_qualified=True)
    except ValueError:
        return ""
    return (
        "SELECT RUN_ID,\n"
        "       LISTAGG(DISTINCT WORKFLOW_NAME, ', ') AS WORKFLOWS,\n"
        "       COUNT(DISTINCT TASK_NAME) AS TASKS,\n"
        "       MIN(INSERT_TS) AS STARTED_AT,\n"
        "       MAX(INSERT_TS) AS LAST_SEEN_AT,\n"
        "       DATEDIFF('second', MIN(INSERT_TS), MAX(INSERT_TS)) AS RUNTIME_SEC\n"
        f"  FROM {tbl}\n"
        "  WHERE RUN_ID IS NOT NULL\n"  # no phantom NULL-key run row (matches sibling builders)
        "  GROUP BY RUN_ID\n"
        "  ORDER BY STARTED_AT DESC\n"
        f"  LIMIT {int(max_runs)}"
    )


def run_params_scan(params_fqn: object, *, run_id: object = "", max_params: int = MAX_PARAMS) -> str:
    """Parameters an ETL run executed with, from CONTROL_PARAMS.

    With ``run_id`` set, returns that specific run's parameters (the value is bound as a
    SQL literal, escaped — a run id is data, not an identifier); otherwise isolates the
    newest RUN_ID (max INSERT_TS). Ordered by scope then name — the run-level knobs
    (RUN_DATE, thresholds, load indicators) and the per-session ones. Fail-closed on a
    bad FQN. Pure: bounded output, no Streamlit."""
    from app.core.sqlsafe import safe_identifier, sql_literal

    fqn = str(params_fqn or "").strip()
    if not fqn:
        return ""
    try:
        tbl = safe_identifier(fqn, allow_qualified=True)
    except ValueError:
        return ""
    _rid = str(run_id or "").strip()
    if _rid:
        where = f"  WHERE p.RUN_ID = {sql_literal(_rid)}\n"
    else:
        where = (
            "  WHERE p.RUN_ID = (\n"
            f"    SELECT RUN_ID FROM {tbl}\n"
            "    WHERE INSERT_TS IS NOT NULL AND RUN_ID IS NOT NULL\n"
            "    QUALIFY ROW_NUMBER() OVER (ORDER BY INSERT_TS DESC) = 1)\n"
        )
    return (
        "SELECT p.PARAM_NAME, p.PARAM_VALUE, p.SCOPE_TYPE, p.SCOPE_NAME\n"
        f"  FROM {tbl} p\n"
        f"{where}"
        "  ORDER BY p.SCOPE_TYPE, p.PARAM_NAME\n"
        f"  LIMIT {int(max_params)}"
    )


def run_tasks_scan(control_fqn: object, run_id: object, *, max_tasks: int = MAX_TASKS) -> str:
    """Per-task runtimes for ONE chosen ETL run, from the CONTROL_STATUS table.

    The drill-down behind the run picker: given a RUN_ID (bound as an escaped SQL literal
    — a run id is data, not an identifier), returns that run's tasks with WORKFLOW_NAME,
    TASK_NAME, TASK_STATUS, the start/end window, and RUNTIME_SEC (end − start; a running
    task measured to now), slowest first. Fail-closed on a bad FQN or an empty run id."""
    from app.core.sqlsafe import safe_identifier, sql_literal

    fqn = str(control_fqn or "").strip()
    _rid = str(run_id or "").strip()
    if not fqn or not _rid:
        return ""
    try:
        tbl = safe_identifier(fqn, allow_qualified=True)
    except ValueError:
        return ""
    return (
        "SELECT s.WORKFLOW_NAME, s.TASK_NAME, s.TASK_STATUS,\n"
        "       s.TASK_START_DTTM, s.TASK_END_DTTM,\n"
        "       DATEDIFF('second', s.TASK_START_DTTM,\n"
        "                COALESCE(s.TASK_END_DTTM, CURRENT_TIMESTAMP())) AS RUNTIME_SEC\n"
        f"  FROM {tbl} s\n"
        f"  WHERE s.RUN_ID = {sql_literal(_rid)}\n"
        "  ORDER BY RUNTIME_SEC DESC, s.TASK_START_DTTM\n"
        f"  LIMIT {int(max_tasks)}"
    )


# --- Phase 3: reconciliation DQ (RECON_MTRC_ERROR) ---------------------------
# The nightly cycle reconciles each metric's SOURCE_LAYER against its TARGET_LAYER and
# logs a RECON_MTRC_ERROR row when they don't tie out (over the recon threshold). Config:
# ETL_RECON_ERROR_FQN. NOTE this table lives in DB_T_PROD_CORE, not PUBLIC — its own grant.
RECON_LOOKBACK_DAYS = 30   # recent recon errors (covers daily + monthly reconciliations)
MAX_RECON_ROWS = 500


def recon_errors_scan(
    recon_fqn: object, *, days: int = RECON_LOOKBACK_DAYS, max_rows: int = MAX_RECON_ROWS
) -> str:
    """Recent reconciliation errors from the Informatica RECON_MTRC_ERROR table.

    Each row is a metric (MTRC) at a frequency / value-type / layer whose SOURCE_LAYER and
    TARGET_LAYER did not reconcile — a source-vs-target mismatch the nightly recon logged.
    Returns the errors loaded in the last ``days`` days, newest first. Fail-closed on a bad
    FQN. Pure: bounded output, no Streamlit."""
    from app.core.sqlsafe import safe_identifier

    fqn = str(recon_fqn or "").strip()
    if not fqn:
        return ""
    try:
        tbl = safe_identifier(fqn, allow_qualified=True)
    except ValueError:
        return ""
    return (
        "SELECT MTRC, FRQCY, VALUE_TYPE, RECON_MTRC_LAYER,\n"
        "       SOURCE_LAYER, TARGET_LAYER, SOURCE_ERROR, TARGET_ERROR, LOAD_DTTM\n"
        f"  FROM {tbl}\n"
        f"  WHERE LOAD_DTTM >= DATEADD('day', -{int(days)}, CURRENT_TIMESTAMP())\n"
        "  ORDER BY LOAD_DTTM DESC, MTRC\n"
        f"  LIMIT {int(max_rows)}"
    )


# --- Phase 3b: reconciliation RECURRENCE (which metric keeps breaking) --------
# RECON_MTRC_ERROR logs ONLY failed reconciliations — there is no "passed" row and no
# total-cycles column. So the only honest recurrence is CONDITIONAL: of the distinct CYCLES
# in which ANY check of the same FRQCY broke, on how many did THIS check break. The denominator
# is COUNT(DISTINCT cycle) PER FRQCY (a monthly break isn't diluted by nightly cohorts); the
# numerator is COUNT(DISTINCT cycle) for the check (a chatty metric can't inflate itself); a
# cycle = DATE(LOAD_DTTM). It is a failures-only PROXY for "chances to break", never a pass-rate.
RECON_RECURRENCE_LOOKBACK_DAYS = 90   # default (unscoped) — long enough for monthly cadences to recur
MAX_RECON_RECURRENCE_ROWS = 300
RECON_RECENT_K = 5                    # "recent" = broke in N of the last K cohort cycles


def recon_recurrence_scan(
    recon_fqn: object, *, days: object = 0, max_rows: int = MAX_RECON_RECURRENCE_ROWS
) -> str:
    """Which reconciliation checks keep breaking — conditional recurrence across recon cycles.

    One row per full check identity (MTRC, FRQCY, VALUE_TYPE, RECON_MTRC_LAYER): BROKEN_CYCLES (distinct
    cycles it broke), TOTAL_ERROR_CYCLES (distinct cycles ANY same-FRQCY check broke — the honest
    denominator for a failures-only table), RECURRENCE_PCT, RECENT_BROKEN of the last K cohort cycles,
    BROKE_LATEST_CYCLE, first/last broken dates, and the newest broken cycle's SOURCE→TARGET layer +
    sample errors (the 'where to fix' hop). A cycle = DATE(LOAD_DTTM); the denominator is scoped PER
    FRQCY. ``days`` (> 0) honors the scope-bar Window; unscoped defaults to a 90-day lookback so monthly
    cadences reach several cohort cycles. One-row-per-check output, so ORDER BY + LIMIT truncate
    deterministically (no series to bisect). Fail-closed on a bad FQN. Pure: bounded, no Streamlit."""
    from app.core.sqlsafe import safe_identifier

    fqn = str(recon_fqn or "").strip()
    if not fqn:
        return ""
    try:
        tbl = safe_identifier(fqn, allow_qualified=True)
    except ValueError:
        return ""
    _d = int(days) if isinstance(days, (int, float)) and int(days) > 0 else RECON_RECURRENCE_LOOKBACK_DAYS
    _k = int(RECON_RECENT_K)
    return (
        # COALESCE all three NULLABLE grain columns to '(unknown)' BEFORE any GROUP BY / equijoin —
        # a NULL grain both splits into an uncomparable pool and fails the NULL=NULL joins below.
        "WITH errs AS (\n"
        "  SELECT MTRC,\n"
        "         COALESCE(FRQCY, '(unknown)') AS FRQCY,\n"
        "         COALESCE(VALUE_TYPE, '(unknown)') AS VALUE_TYPE,\n"
        "         COALESCE(RECON_MTRC_LAYER, '(unknown)') AS RECON_MTRC_LAYER,\n"
        "         SOURCE_LAYER, TARGET_LAYER, SOURCE_ERROR, TARGET_ERROR,\n"
        "         CAST(LOAD_DTTM AS DATE) AS CYCLE_DATE, LOAD_DTTM\n"
        f"  FROM {tbl}\n"
        f"  WHERE LOAD_DTTM >= DATEADD('day', -{_d}, CURRENT_TIMESTAMP())\n"
        "    AND MTRC IS NOT NULL AND LOAD_DTTM IS NOT NULL\n"
        "),\n"
        # per-FRQCY cohort cycle sequence: newest cohort cycle = CYCLE_RN 1 (the denominator + recency axis)
        "cohort AS (\n"
        "  SELECT FRQCY, CYCLE_DATE,\n"
        "         DENSE_RANK() OVER (PARTITION BY FRQCY ORDER BY CYCLE_DATE DESC) AS CYCLE_RN\n"
        "  FROM (SELECT DISTINCT FRQCY, CYCLE_DATE FROM errs)\n"
        "),\n"
        "freq AS (\n"
        "  SELECT FRQCY, MAX(CYCLE_RN) AS TOTAL_ERROR_CYCLES FROM cohort GROUP BY FRQCY\n"
        "),\n"
        "chk_cycles AS (\n"
        "  SELECT d.MTRC, d.FRQCY, d.VALUE_TYPE, d.RECON_MTRC_LAYER, d.CYCLE_DATE, c.CYCLE_RN\n"
        "  FROM (SELECT DISTINCT MTRC, FRQCY, VALUE_TYPE, RECON_MTRC_LAYER, CYCLE_DATE FROM errs) d\n"
        "  JOIN cohort c ON c.FRQCY = d.FRQCY AND c.CYCLE_DATE = d.CYCLE_DATE\n"
        "),\n"
        "agg AS (\n"
        "  SELECT MTRC, FRQCY, VALUE_TYPE, RECON_MTRC_LAYER,\n"
        "         COUNT(*) AS BROKEN_CYCLES,\n"
        f"         SUM(CASE WHEN CYCLE_RN <= {_k} THEN 1 ELSE 0 END) AS RECENT_BROKEN,\n"
        "         MIN(CYCLE_RN) AS NEWEST_BROKEN_RN,\n"
        "         MIN(CYCLE_DATE) AS FIRST_BROKEN_ON, MAX(CYCLE_DATE) AS LAST_BROKEN_ON\n"
        "  FROM chk_cycles GROUP BY 1, 2, 3, 4\n"
        "),\n"
        # newest broken cycle's layer/error context (the 'where to fix' hop + sample errors)
        "ctx AS (\n"
        "  SELECT MTRC, FRQCY, VALUE_TYPE, RECON_MTRC_LAYER,\n"
        "         COUNT(*) AS ERROR_ROWS,\n"
        "         MAX_BY(SOURCE_LAYER, LOAD_DTTM) AS SOURCE_LAYER,\n"
        "         MAX_BY(TARGET_LAYER, LOAD_DTTM) AS TARGET_LAYER,\n"
        "         MAX_BY(SOURCE_ERROR, LOAD_DTTM) AS SOURCE_ERROR,\n"
        "         MAX_BY(TARGET_ERROR, LOAD_DTTM) AS TARGET_ERROR\n"
        "  FROM errs GROUP BY 1, 2, 3, 4\n"
        ")\n"
        "SELECT a.MTRC, a.FRQCY, a.VALUE_TYPE, a.RECON_MTRC_LAYER,\n"
        "       a.BROKEN_CYCLES, f.TOTAL_ERROR_CYCLES,\n"
        "       ROUND(100.0 * a.BROKEN_CYCLES / NULLIF(f.TOTAL_ERROR_CYCLES, 0), 0) AS RECURRENCE_PCT,\n"
        f"       a.RECENT_BROKEN, LEAST({_k}, f.TOTAL_ERROR_CYCLES) AS RECENT_WINDOW,\n"
        "       (a.NEWEST_BROKEN_RN = 1) AS BROKE_LATEST_CYCLE,\n"
        "       a.FIRST_BROKEN_ON, a.LAST_BROKEN_ON, x.ERROR_ROWS,\n"
        "       x.SOURCE_LAYER, x.TARGET_LAYER, x.SOURCE_ERROR, x.TARGET_ERROR\n"
        "  FROM agg a\n"
        "  JOIN freq f ON f.FRQCY = a.FRQCY\n"
        "  JOIN ctx  x ON x.MTRC = a.MTRC AND x.FRQCY = a.FRQCY\n"
        "               AND x.VALUE_TYPE = a.VALUE_TYPE AND x.RECON_MTRC_LAYER = a.RECON_MTRC_LAYER\n"
        "  ORDER BY RECURRENCE_PCT DESC, a.RECENT_BROKEN DESC, a.BROKEN_CYCLES DESC, x.ERROR_ROWS DESC\n"
        f"  LIMIT {int(max_rows)}"
    )


# --- Phase 4: cost attribution (CONTROL_STATUS x ACCOUNT_USAGE) ---------------
# The nightly cycle is Informatica proc CALLs, so Snowflake bills the compute to a
# WAREHOUSE, never to a task — "which task cost the most last night?" has no billed
# answer. We ATTRIBUTE it: each query's measured credits (the fair share Snowflake
# itself computes in QUERY_ATTRIBUTION_HISTORY.CREDITS_ATTRIBUTED_COMPUTE) is charged
# to the CONTROL_STATUS task whose run window CONTAINS the query AND whose task name
# appears in the query text. The query tags carry no PRCS_ID / RUN_ID (verified live —
# every tag was blank), so text + window is the only join available. Among several
# task names that match one query the LONGEST wins, so a nested task name
# (SP_D_PLCY_TSACTN_STS_CANCLTN_RSN) never double-counts its credits onto its shorter
# prefix (SP_D_PLCY_TSACTN). A query that matches NO task is kept in an '(unattributed)'
# bucket so the panel can show coverage honestly: this is a best-effort attribution
# model, not a billed invoice. Credits stay credits here (the module takes no dollar
# rate, by design); the panel converts to USD with CREDIT_PRICE_USD.
MAX_COST_ROWS = 500

# The two ACCOUNT_USAGE views the attribution joins across (already reachable from the
# Operations page). QAH is the credit source + the query-tree root; QH is the text.
_QAH_FQN = "SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY"
_QH_FQN = "SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY"
# Labels for the coverage bucket (queries in the run window that matched no task).
UNATTRIBUTED_WORKFLOW = "(unattributed)"
UNATTRIBUTED_TASK = "(window overhead / other queries)"


def run_cost_attribution_scan(
    control_fqn: object, *, run_id: object = "", max_rows: int = MAX_COST_ROWS
) -> str:
    """Attribute a run's measured Snowflake credits to its CONTROL_STATUS tasks.

    With ``run_id`` set, attributes that specific run (the id is bound as an escaped SQL
    literal — a run id is data, not an identifier); otherwise the latest run (the one
    holding the newest TASK_START_DTTM). Returns one row per task: WORKFLOW_NAME,
    TASK_NAME, MATCHED_QUERIES, and CREDITS_ATTRIBUTED (the summed fair-share compute
    credits of every query charged to it), most expensive task first. Queries that ran
    in the run window but matched no task land in one '(unattributed)' row, so the caller
    can show attributed-vs-total coverage rather than silently dropping them.

    The join: candidate queries are QUERY_ATTRIBUTION_HISTORY (credits) ⋈ QUERY_HISTORY
    (text) inside the run's [min start, max end] window; each is charged to the LONGEST
    task name whose window contains it AND whose name is in its text. Fail-closed on a bad
    or empty CONTROL_STATUS FQN. Pure: bounded output, no Streamlit, no dollar rate."""
    from app.core.sqlsafe import safe_identifier, sql_literal

    fqn = str(control_fqn or "").strip()
    if not fqn:
        return ""
    try:
        tbl = safe_identifier(fqn, allow_qualified=True)
    except ValueError:
        return ""
    _rid = str(run_id or "").strip()
    if _rid:
        run_filter = f"    AND RUN_ID = {sql_literal(_rid)}\n"
    else:
        # latest run = the RUN_ID holding the newest TASK_START_DTTM (matches the
        # runtimes / drift panels, so all three read the same 'latest run').
        run_filter = (
            "    AND RUN_ID = (\n"
            f"      SELECT RUN_ID FROM {tbl}\n"
            "      WHERE TASK_START_DTTM IS NOT NULL AND RUN_ID IS NOT NULL\n"
            "      QUALIFY ROW_NUMBER() OVER (ORDER BY TASK_START_DTTM DESC) = 1)\n"
        )
    unattr_wf = sql_literal(UNATTRIBUTED_WORKFLOW)
    unattr_task = sql_literal(UNATTRIBUTED_TASK)
    return (
        "WITH tasks AS (\n"
        "  SELECT WORKFLOW_NAME, TASK_NAME, TASK_START_DTTM,\n"
        "         COALESCE(TASK_END_DTTM, CURRENT_TIMESTAMP()) AS TASK_END\n"
        f"  FROM {tbl}\n"
        "  WHERE TASK_START_DTTM IS NOT NULL AND TASK_NAME IS NOT NULL\n"
        f"{run_filter}"
        "),\n"
        # one narrow time window bounds BOTH ACCOUNT_USAGE reads (a time-partitioned slice,
        # so the scan is cheap even though the views are account-wide + huge).
        "bounds AS (\n"
        "  SELECT MIN(TASK_START_DTTM) AS RUN_START, MAX(TASK_END) AS RUN_END FROM tasks\n"
        "),\n"
        # QAH can carry MORE THAN ONE row per QUERY_ID (a query split across warehouse
        # slices), so SUM the credits per query FIRST. Otherwise the later RN=1 — which
        # exists only to de-dup TASK matches — would also silently drop a query's other
        # credit slices and undercount the run. HAVING keeps queries whose TOTAL is > 0.
        "qah_agg AS (\n"
        "  SELECT QUERY_ID, SUM(CREDITS_ATTRIBUTED_COMPUTE) AS CREDITS\n"
        f"  FROM {_QAH_FQN}\n"
        "  WHERE START_TIME >= (SELECT RUN_START FROM bounds)\n"
        "    AND START_TIME <= (SELECT RUN_END FROM bounds)\n"
        "  GROUP BY QUERY_ID\n"
        "  HAVING SUM(CREDITS_ATTRIBUTED_COMPUTE) > 0\n"
        "),\n"
        # attach the text (QUERY_HISTORY is one row per QUERY_ID) inside the same window.
        "cand AS (\n"
        "  SELECT a.QUERY_ID, a.CREDITS, qh.QUERY_TEXT, qh.START_TIME\n"
        "  FROM qah_agg a\n"
        f"  JOIN {_QH_FQN} qh ON qh.QUERY_ID = a.QUERY_ID\n"
        "  WHERE qh.START_TIME >= (SELECT RUN_START FROM bounds)\n"
        "    AND qh.START_TIME <= (SELECT RUN_END FROM bounds)\n"
        "),\n"
        # charge each query to the LONGEST task name whose window + text both match. The
        # LEFT JOIN keeps unmatched queries (their task columns NULL) so coverage is honest;
        # NULLS LAST means a genuinely-matched query never picks the NULL fallback row.
        # CONTAINS(UPPER(...)) is a LITERAL case-insensitive substring test — unlike ILIKE
        # it does not treat the '_' in SP_*/M_* task names as a single-char wildcard, so a
        # task name can't loosely match (and mis-charge) an unrelated query's text.
        "assigned AS (\n"
        "  SELECT c.QUERY_ID, c.CREDITS, t.WORKFLOW_NAME, t.TASK_NAME,\n"
        "         ROW_NUMBER() OVER (PARTITION BY c.QUERY_ID\n"
        "           ORDER BY LENGTH(t.TASK_NAME) DESC NULLS LAST) AS RN\n"
        "  FROM cand c\n"
        "  LEFT JOIN tasks t\n"
        "    ON c.START_TIME >= t.TASK_START_DTTM AND c.START_TIME <= t.TASK_END\n"
        "   AND CONTAINS(UPPER(c.QUERY_TEXT), UPPER(t.TASK_NAME))\n"
        ")\n"
        f"SELECT COALESCE(WORKFLOW_NAME, {unattr_wf}) AS WORKFLOW_NAME,\n"
        f"       COALESCE(TASK_NAME, {unattr_task}) AS TASK_NAME,\n"
        "       COUNT(DISTINCT QUERY_ID) AS MATCHED_QUERIES,\n"
        "       ROUND(SUM(CREDITS), 4) AS CREDITS_ATTRIBUTED\n"
        "  FROM assigned\n"
        "  WHERE RN = 1\n"
        "  GROUP BY 1, 2\n"
        "  ORDER BY CREDITS_ATTRIBUTED DESC NULLS LAST\n"
        f"  LIMIT {int(max_rows)}"
    )
