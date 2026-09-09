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


def workflow_runtimes_scan(control_fqn: object, *, max_tasks: int = MAX_TASKS) -> str:
    """Latest run's per-task runtimes from the Informatica CONTROL_STATUS table.

    Isolates the most recent RUN_ID (the run holding the newest TASK_START_DTTM) and
    returns one row per task: WORKFLOW_NAME, TASK_NAME, TASK_STATUS, the start/end
    window, and RUNTIME_SEC = end − start (a still-running task with a NULL end is
    measured to CURRENT_TIMESTAMP(), so a hung task surfaces). Ordered slowest-first
    so the long pole leads; the ``_SEC`` column name humanizes to Hr/Min/Sec in the
    shared table machinery. The table FQN is validated with ``safe_identifier``
    (fail-closed): an unset or malformed FQN returns ``""`` and the caller renders a
    setup hint rather than running unsafe SQL. Pure: bounded output, no Streamlit."""
    from app.core.sqlsafe import safe_identifier

    fqn = str(control_fqn or "").strip()
    if not fqn:
        return ""
    try:
        tbl = safe_identifier(fqn, allow_qualified=True)
    except ValueError:
        return ""
    return (
        "WITH latest AS (\n"
        f"  SELECT RUN_ID FROM {tbl}\n"
        "  WHERE TASK_START_DTTM IS NOT NULL\n"
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
    """Tasks in the latest run that ran materially SLOWER than their recent baseline.

    Ranks runs by recency, takes the newest as 'latest' and the next ``baseline_runs``
    as the comparison window, and for each task matched on (WORKFLOW_NAME, TASK_NAME)
    compares the latest runtime to the MEDIAN of its baseline runtimes. Only a material
    slowdown surfaces — at least ``min_abs_sec`` seconds AND at least ``min_ratio``x the
    baseline — so a 2s→4s task never fires while a 5m→15m one does. Biggest slowdown
    first. Runtime per (task, run) is MAX-collapsed (defensive against a dup task row);
    a task with no baseline history (a brand-new task) is absent by design. Fail-closed
    on a bad FQN. The _SEC columns humanize to Hr/Min/Sec; SLOWER_BY_* stay positive so
    they read as a duration, not a signed delta. Pure: bounded output, no Streamlit."""
    from app.core.sqlsafe import safe_identifier

    fqn = str(control_fqn or "").strip()
    if not fqn:
        return ""
    try:
        tbl = safe_identifier(fqn, allow_qualified=True)
    except ValueError:
        return ""
    keep = 1 + max(1, int(baseline_runs))
    return (
        "WITH runs AS (\n"
        f"  SELECT RUN_ID, MAX(TASK_START_DTTM) AS RUN_START FROM {tbl}\n"
        "  WHERE TASK_START_DTTM IS NOT NULL AND RUN_ID IS NOT NULL\n"
        "  GROUP BY RUN_ID\n"
        f"  QUALIFY ROW_NUMBER() OVER (ORDER BY RUN_START DESC) <= {keep}\n"
        "),\n"
        "ranked AS (\n"
        "  SELECT RUN_ID, ROW_NUMBER() OVER (ORDER BY RUN_START DESC) AS RN FROM runs\n"
        "),\n"
        "task_runtimes AS (\n"
        "  SELECT s.WORKFLOW_NAME, s.TASK_NAME, r.RN,\n"
        "         MAX(DATEDIFF('second', s.TASK_START_DTTM,\n"
        "             COALESCE(s.TASK_END_DTTM, CURRENT_TIMESTAMP()))) AS RUNTIME_SEC\n"
        f"  FROM {tbl} s JOIN ranked r ON s.RUN_ID = r.RUN_ID\n"
        "  WHERE s.TASK_START_DTTM IS NOT NULL\n"
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
