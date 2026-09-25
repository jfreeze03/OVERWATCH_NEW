"""Locks for V156 — nightly ETL-cycle PUSH alerts (Next-Fifty rank 2, wave 2b).

SP_SCAN_ETL_CYCLE() reads the configured Informatica CONTROL_STATUS table once per run into the transient
ETL_CYCLE_TASKS cache (23 nights, retries collapsed to each task's terminal attempt, night key
DATE(start - 12h)) and raises PIPE_ETL_TASK_FAILED / PIPE_ETL_CYCLE_NOT_STARTED / PIPE_ETL_CYCLE_LATE itself,
each rule in its own EXCEPTION guard. New objects only (no re-derivation, so no generator): the hourly
SP_ALERT_SCAN gains the add-on CALL arm in V157. These locks pin the app parity (status words, night key,
retry collapse, settings defaults, the Overdue test, the forecaster constants), the plan corrections
(TERMINAL_START filter, GREATEST threshold floor, the term_lw regular-terminal guard, the newest-14 clean
history, CRITICAL only for an unfinished miss, lead-window-only METRIC_VALUE) and a Python model of the
[C] grading that proves an afternoon terminal re-run no longer hides the night's completion.

The validate / docs / admin pins below are asserted at the WAVE TIP (V158) and fail until the wave-2b
integration commit bumps those shared files.
"""

from __future__ import annotations

import re
import statistics
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from app.config import DEFAULT_SETTINGS
from app.data import etl_control_sql as etl
from app.logic import insights

_ROOT = Path(__file__).resolve().parents[2]
_NAME = "V156__etl_cycle_push_alerts.sql"
_MIG = (_ROOT / "snowflake" / "migrations" / _NAME).read_text(encoding="utf-8")
_PARTS = _MIG.split("$$")
_GUARD = _PARTS[1]
_BODY = _PARTS[3]                      # the SP_SCAN_ETL_CYCLE body
_HEADER = _MIG.split("\nEXECUTE IMMEDIATE\n$$\n", 1)[0]
_RULES = ("PIPE_ETL_TASK_FAILED", "PIPE_ETL_CYCLE_NOT_STARTED", "PIPE_ETL_CYCLE_LATE")
_TIP = 158                             # the wave-2b tip (V156 ETL cycle, V157 scans, V158 backups)
_FAILED_SQL = ", ".join(f"'{s}'" for s in sorted(etl.FAILED_TASK_STATUSES))


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _merge() -> str:
    return _MIG.split("MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG", 1)[1].split(";", 1)[0]


def _rule_statements() -> list[str]:
    """Every static ALERT_EVENTS / APP_ERROR_LOG INSERT and ALERT_EVENTS UPDATE in the proc body, cut at its
    real terminating ';' (quote-aware: a DETAIL literal carries a ';')."""
    from tests.test_migrations_parse import _split_statements
    starts = [m.start() for m in re.finditer(
        r"(?:INSERT INTO DBA_MAINT_DB\.OVERWATCH\.(?:ALERT_EVENTS|APP_ERROR_LOG)"
        r"|UPDATE DBA_MAINT_DB\.OVERWATCH\.ALERT_EVENTS)\b", _BODY)]
    return [_split_statements(_BODY[i:])[0] for i in starts]


def _bind(sql: str) -> str:
    sql = sql.replace(":now_ct", "'2026-09-25 06:10:00'::TIMESTAMP_NTZ")
    sql = re.sub(r":(start_wf|end_wf|emsg)\b", "'X'", sql)
    return re.sub(r":[a-z_]+\b", "420", sql)


def _render_ins_sql(fqn: str = "DB.S.CONTROL_STATUS", lookback: int = 23) -> str:
    """Evaluate the ins_sql concatenation the proc EXECUTE IMMEDIATEs (literals un-doubled)."""
    expr = _BODY.split("ins_sql := ", 1)[1].split(";\n    EXECUTE IMMEDIATE :ins_sql;", 1)[0]
    out = []
    for piece in re.findall(r"'(?:[^']|'')*'|[^|\s][^|]*?(?=\s*\|\||\s*$)", expr):
        piece = piece.strip()
        if piece.startswith("'"):
            out.append(piece[1:-1].replace("''", "'"))
        elif piece == "TRIM(:ctl_fqn)":
            out.append(fqn)
        elif piece == ":lookback_days":
            out.append(str(lookback))
        else:
            raise AssertionError(f"unexpected ins_sql piece: {piece!r}")
    return "".join(out)


_SCHEMA = {"DBA_MAINT_DB": {"OVERWATCH": {
    "ETL_CYCLE_TASKS": dict.fromkeys(
        ("CYCLE_DATE", "WORKFLOW_NAME", "TASK_NAME", "TERMINAL_STATUS", "FIRST_START", "TERMINAL_START",
         "TERMINAL_END", "SCANNED_AT"), "VARCHAR"),
    "ALERT_CONFIG": dict.fromkeys(
        ("RULE_ID", "FAMILY", "NAME", "ENABLED", "SEVERITY", "THRESHOLD_NUM", "WINDOW_HOURS",
         "AUTO_CLEAR_ENABLED", "CLEAR_THRESHOLD_NUM"), "VARCHAR"),
    "ALERT_EVENTS": dict.fromkeys(
        ("EVENT_ID", "RULE_ID", "COMPANY", "SEVERITY", "TITLE", "DETAIL", "METRIC_VALUE", "DEDUPE_KEY",
         "STATUS", "RAISED_AT", "RESOLVED_AT", "RESOLUTION_KIND"), "VARCHAR"),
    "APP_ERROR_LOG": dict.fromkeys(
        ("PAGE", "ERROR_TYPE", "ERROR_MESSAGE", "CONTEXT", "ROLE_NAME", "LOGGED_AT"), "VARCHAR"),
}}}
# the CONTROL_STATUS columns the app builders read (grounded below against etl_control_sql)
_CTL_COLUMNS = ("TASK_START_DTTM", "TASK_END_DTTM", "TASK_STATUS", "WORKFLOW_NAME", "TASK_NAME")


# ---------------------------------------------------------------------------------------------------
# 1-2. guard, version, object inventory
def test_v156_guarded_and_ordered():
    assert "not_ready EXCEPTION (-20156, 'V156 requires V155 first - apply migrations in order.');" in _GUARD
    assert "IF (v < 155) THEN" in _GUARD
    assert "SELECT 156 AS VERSION" in _MIG and "WHERE VERSION = 156)" in _MIG
    assert len(_PARTS) == 5, "exactly two $$ blocks: the guard and the SP_SCAN_ETL_CYCLE body"
    order = ["EXECUTE IMMEDIATE", "CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS",
             "MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG",
             "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_SCAN_ETL_CYCLE()",
             "INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION"]
    idx = [_MIG.index(s) for s in order]
    assert idx == sorted(idx)
    assert _MIG.startswith("-- V156__etl_cycle_push_alerts.sql\n")


def test_v156_new_objects_only_and_no_tail_call():
    assert _MIG.count("CREATE OR REPLACE") == 1
    assert ("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_SCAN_ETL_CYCLE()\nRETURNS VARCHAR\n"
            "LANGUAGE SQL\nEXECUTE AS OWNER\n") in _MIG
    for banned in ("CREATE TASK", "ALTER TASK", "ALTER TABLE", "DROP ", "SP_ALERT_SCAN()"):
        assert banned not in _MIG, banned
    assert "-- >>> derived:" not in _MIG, "a first definition carries no lineage marker"
    live = "\n".join(ln for ln in _MIG.splitlines() if not ln.lstrip().startswith("--"))
    assert not re.search(r"^\s*CALL\b", live, re.M), "no raiser CALL at apply (it would page)"
    assert not re.search(r"\bCALL\s+DBA_MAINT_DB", _BODY)
    earlier = [p.name for p in (_ROOT / "snowflake" / "migrations").glob("V*.sql")
               if p.name != _NAME and "SP_SCAN_ETL_CYCLE" in p.read_text(encoding="utf-8")
               and int(p.name[1:4]) < 156]
    assert not earlier, f"SP_SCAN_ETL_CYCLE must be a first definition: {earlier}"


def test_v156_table_carries_terminal_start():
    table = _MIG.split("CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS (", 1)[1]
    table = table.split(");", 1)[0]
    cols = [ln.split()[0] for ln in table.strip().splitlines()]
    assert cols == ["CYCLE_DATE", "WORKFLOW_NAME", "TASK_NAME", "TERMINAL_STATUS", "FIRST_START",
                    "TERMINAL_START", "TERMINAL_END", "SCANNED_AT"]
    assert "    TERMINAL_START  TIMESTAMP_NTZ,\n" in table


# ---------------------------------------------------------------------------------------------------
# 3. the one dynamic statement is allowlist-gated
def test_v156_fqn_allowlisted_and_single_dynamic_statement():
    rl = "RLIKE(TRIM(:ctl_fqn), '^[A-Za-z0-9_$]+([.][A-Za-z0-9_$]+){0,3}$')"
    assert rl in _BODY
    assert "'^[A-Za-z0-9_$]+([.][A-Za-z0-9_$]+){0,3}$'" in _read(
        "snowflake/migrations/V137__dq_recon_error_alert.sql")
    assert _BODY.count("EXECUTE IMMEDIATE") == 1
    assert _BODY.index(rl) < _BODY.index("'FROM ' || TRIM(:ctl_fqn)")
    # the FQN never reaches event text (navigate._DB_RE would turn 'DB.SCHEMA.' into a database filter)
    for stmt in _rule_statements():
        assert ":ctl_fqn" not in stmt


def test_v156_dynamic_insert_renders_parses_and_reads_only_app_columns():
    sqlglot = pytest.importorskip("sqlglot")
    from sqlglot.optimizer.qualify import qualify
    sql = _render_ins_sql()
    assert sql.startswith("INSERT INTO DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS (CYCLE_DATE, WORKFLOW_NAME, "
                          "TASK_NAME, TERMINAL_STATUS, FIRST_START, TERMINAL_START, TERMINAL_END) SELECT ")
    assert "FROM DB.S.CONTROL_STATUS WHERE " in sql and sql.endswith("GROUP BY 1, 2, 3")
    assert ("TASK_START_DTTM >= DATEADD('day', -23, "
            "CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ)") in sql
    tree = sqlglot.parse_one(sql, read="snowflake")
    schema = {**_SCHEMA, "DB": {"S": {"CONTROL_STATUS": dict.fromkeys(_CTL_COLUMNS, "VARCHAR")}}}
    qualify(tree, schema=schema, dialect="snowflake", validate_qualify_columns=True)
    # every CONTROL_STATUS column read is one the app's own builders already read
    app_sql = (etl.cycle_night_health_scan("DB.S.T", start_workflow="A")
               + etl.cycle_finish_history_scan("DB.S.T", start_workflow="A", end_workflow="B"))
    for col in _CTL_COLUMNS:
        assert col in app_sql, col


# ---------------------------------------------------------------------------------------------------
# 4-7. parity with the app's ETL semantics
def test_v156_failed_status_lists_match_the_app():
    lists = re.findall(r"IN \(('ABORTED'[^)]*)\)", _BODY)
    assert len(lists) >= 5
    for lst in lists:
        assert {s.strip().strip("'") for s in lst.split(",")} == set(etl.FAILED_TASK_STATUSES)
        assert lst == _FAILED_SQL, "same rendering as etl_control_sql's _failed IN-list"
    assert f"IN ({_FAILED_SQL})" in etl.cycle_finish_history_scan("DB.S.T", start_workflow="A", end_workflow="B")


def test_v156_night_key_and_retry_collapse_match_the_app():
    app = etl.cycle_finish_history_scan("DB.S.T", start_workflow="A", end_workflow="B")
    night = etl.cycle_night_health_scan("DB.S.T", start_workflow="A")
    for sql in (app, night):
        assert "DATE(DATEADD('hour', -12, " in sql
        assert "COALESCE(TASK_END_DTTM, TASK_START_DTTM))" in sql or "COALESCE(s.TASK_END_DTTM" in sql
    assert "MAX_BY(TASK_STATUS, COALESCE(TASK_END_DTTM, TASK_START_DTTM))" in app
    assert "MAX_BY(TASK_END_DTTM, COALESCE(TASK_END_DTTM, TASK_START_DTTM))" in app
    assert "DATE(DATEADD(''hour'', -12, TASK_START_DTTM::TIMESTAMP_NTZ))" in _BODY
    for frag in ("MAX_BY(TASK_STATUS, COALESCE(TASK_END_DTTM, TASK_START_DTTM))",
                 "MAX_BY(TASK_END_DTTM, COALESCE(TASK_END_DTTM, TASK_START_DTTM))",
                 "MAX_BY(TASK_START_DTTM, COALESCE(TASK_END_DTTM, TASK_START_DTTM))::TIMESTAMP_NTZ",
                 "MIN(TASK_START_DTTM)::TIMESTAMP_NTZ"):
        assert frag in _BODY, frag
    # running = no end and not failed (the app's N_RUNNING formula)
    assert "TERMINAL_END IS NULL\n" in _BODY and f"UPPER(t.TERMINAL_STATUS) NOT IN ({_FAILED_SQL})" in _BODY
    assert f"OR UPPER(TERMINAL_STATUS) NOT IN ({_FAILED_SQL})" in app


def test_v156_settings_defaults_match_default_settings():
    for key in ("ETL_CYCLE_START_WORKFLOW", "ETL_CYCLE_END_WORKFLOW", "ETL_SLA_TARGET_HHMM",
                "ETL_SLA_BREACH_HHMM"):
        assert f"MAX(IFF(KEY = '{key}', VALUE, NULL)), '{DEFAULT_SETTINGS[key]}')" in _BODY, key
    assert DEFAULT_SETTINGS["ETL_CONTROL_STATUS_FQN"] == ""
    assert "SELECT MAX(IFF(KEY = 'ETL_CONTROL_STATUS_FQN', VALUE, NULL)),\n" in _BODY   # no fallback FQN
    # the malformed-clock fallbacks equal insights._parse_hhmm's (7,0) / (8,0)
    assert "                      420);" in _BODY and "                      480);" in _BODY
    assert insights._parse_hhmm("x", (7, 0)) == (7, 0)


def _sql_hhmm(raw: str, fallback: int) -> int:
    """Python mirror of the proc's target_off / breach_off IFF (after the SELECT's TRIM)."""
    raw = raw.strip()
    if re.fullmatch(r"[0-9]{1,2}:[0-9]{1,2}", raw):
        h, m = (int(x) for x in raw.split(":"))
        if h <= 23 and m <= 59:
            return h * 60 + m
    return fallback


@pytest.mark.parametrize("raw", ["07:00", "7:5", " 06:30 ", "23:59", "00:00", "24:00", "07:60", "",
                                 "ab:cd", "07:00:00", "0700", "7"])
def test_v156_clock_parse_matches_insights_parse_hhmm(raw):
    h, m = insights._parse_hhmm(raw, (7, 0))
    assert _sql_hhmm(raw, 420) == h * 60 + m
    assert "RLIKE(:target_raw, '^[0-9]{1,2}:[0-9]{1,2}$')" in _BODY
    assert "breach_off := :target_off + 60;" in _BODY          # hard <= target -> target + 60 (the app)


def test_v156_clock_pinned_central():
    assert "now_ct := CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ;" in _BODY
    assert "CONVERT_TIMEZONE(''America/Chicago'', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ" in _BODY
    assert "CURRENT_DATE()" not in _BODY and "SYSDATE" not in _BODY
    # the only other CURRENT_TIMESTAMP() is the house RESOLVED_AT stamp of the retry auto-clear
    assert _BODY.count("CURRENT_TIMESTAMP()") == 3
    assert "SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'AUTO_CLEARED'" in _BODY
    assert "LOGGED_AT >= DATE_TRUNC('day', :now_ct)" in _BODY


def test_v156_not_started_is_the_app_overdue_test():
    app = etl.cycle_night_health_scan("DB.S.T", start_workflow="A")
    assert "7 * 86400 + 7200" in app
    assert etl.NIGHT_NOT_STARTED_SEC == 86400 + 7200
    assert "('PIPE_ETL_CYCLE_NOT_STARTED', 'PIPELINE'," in _merge() and "TRUE, 'HIGH', 120, 24)" in _merge()
    for frag in ("7 * 1440 + c.GRACE_MIN", "86400 + m.GRACE_MIN * 60", "DATEADD('day', -6, a.CYCLE_DATE)",
                 "DATEADD('day', -7, DATE(DATEADD('hour', -12, :now_ct)))",
                 "'PIPE_ETL_CYCLE_NOT_STARTED|' || TO_VARCHAR(DATEADD('day', 7, m.REF_NIGHT))"):
        assert frag in _BODY, frag
    assert "BETWEEN DATEADD('day', -6, a.CYCLE_DATE)" in app
    assert "AND DATEADD('day', -7, DATE(DATEADD('hour', -12, CURRENT_TIMESTAMP())))" in app


# ---------------------------------------------------------------------------------------------------
# 8-11. PIPE_ETL_CYCLE_LATE: projection constants, plan corrections, bands and keys
def test_v156_late_projection_shares_the_forecaster_constants():
    assert insights.SLA_FORECAST_MIN_RUNS == 4 and "IFF(h.N_HIST >= 4," in _BODY
    assert insights.SLA_FORECAST_FIT_NIGHTS == 14
    assert "QUALIFY ROW_NUMBER() OVER (ORDER BY n.CYCLE_DATE DESC) <= 14" in _BODY
    assert "DATEADD('day', -14, l.CYCLE_DATE)" not in _BODY, "newest 14 clean nights, not 14 calendar days"
    assert "WHERE n.CYCLE_FINISH IS NOT NULL AND n.N_FAILED = 0 AND n.N_RUNNING = 0" in _BODY
    assert "DATEADD('minute', :target_off + 1440, DATE_TRUNC('day', s.CYCLE_START))" in _BODY  # _deadline_after
    assert "GREATEST(:now_ct, DATEADD('second', ROUND(h.MED_CYCLE_SEC), n.CYCLE_START))" in _BODY
    # described honestly: a new heuristic sharing the constants, not parity with the Theil-Sen trend
    assert "new SQL heuristic sharing SLA_FORECAST_MIN_RUNS /" in _HEADER
    assert "SLA_FORECAST_FIT_NIGHTS" in _HEADER and "mirrors ETL_RECON_RESULTS" not in _MIG


def test_v156_terminal_counts_only_from_its_terminal_attempt():
    assert "JOIN starts s ON s.CYCLE_DATE = t.CYCLE_DATE AND t.TERMINAL_START >= s.CYCLE_START" in _BODY
    assert "t.FIRST_START >= s.CYCLE_START" not in _BODY
    # CYCLE_START keeps the app's MIN(starter start) (parity; the afternoon double re-run edge is documented)
    assert "SELECT CYCLE_DATE, MIN(FIRST_START) AS CYCLE_START\n" in _BODY
    assert "MIN(TASK_START_DTTM) AS CYCLE_START" in etl.cycle_finish_history_scan(
        "DB.S.T", start_workflow="A", end_workflow="B")
    assert "CYCLE_START = MIN(starter start)" in _HEADER


def test_v156_regular_terminal_guard():
    assert ("term_lw AS (\n                -- the app RAN_LAST_WEEK test for the terminal: did it dispatch on the "
            "same night last week?\n                SELECT COUNT(*) AS N\n                FROM nights n\n"
            "                JOIN latest l ON n.CYCLE_DATE = DATEADD('day', -7, l.CYCLE_DATE)\n"
            "                WHERE n.TERM_TASKS > 0\n            ),") in _BODY
    assert "CROSS JOIN term_lw tl" in _BODY
    assert "AND NOT (t.TERM_TASKS = 0 AND COALESCE(tl.N, 0) = 0)" in _BODY
    assert "RAN_LAST_WEEK" in etl.cycle_night_health_scan("DB.S.T", start_workflow="A")


def test_v156_late_severity_is_critical_only_when_unfinished():
    assert "IFF(g.BAND = 'WARN' OR g.IS_COMPLETE, g.SEVERITY, 'CRITICAL')," in _BODY
    assert "IFF(g.BAND = 'WARN', g.SEVERITY, 'CRITICAL')" not in _BODY
    assert "('PIPE_ETL_CYCLE_LATE', 'PIPELINE'," in _merge() and "TRUE, 'HIGH', 60, 24)" in _merge()


def test_v156_late_metric_value_only_for_the_lead_window_warn():
    assert ("IFF(g.BAND = 'WARN' AND NOT COALESCE(g.PROJECTED_FINISH > g.DL_H, FALSE), "
            "DATEDIFF('minute', :now_ct, g.DL_T), NULL),") in _BODY
    late = _BODY.split("-- [C] PIPE_ETL_CYCLE_LATE", 1)[1]
    assert "                   DATEDIFF('minute', :now_ct, g.DL_T),\n" not in late


def test_v156_band_keys_ride_the_supersede_sweep():
    from tests.test_alert_rule_consistency import _latest_proc_bodies
    assert "'PIPE_ETL_CYCLE_LATE|' || g.BAND || '|' || TO_VARCHAR(g.CYCLE_DATE)" in _BODY
    for band in ("EXH", "CRIT", "WARN"):
        assert f"THEN '{band}'" in _BODY
    scan = _latest_proc_bodies()["SP_ALERT_SCAN"]
    for lo, hi in (("|WARN|", "|CRIT|"), ("|CRIT|", "|EXH|"), ("|WARN|", "|EXH|")):
        assert f"REPLACE(lo.DEDUPE_KEY, '{lo}', '{hi}')" in scan


def _snooze_identity(key: str) -> str:
    """Python mirror of the V117 carry-forward identity (strip a trailing |YYYY-MM-DD)."""
    if len(key) >= 11 and key[-11] == "|":
        try:
            date.fromisoformat(key[-10:])
            return key[:-11]
        except ValueError:
            pass
    return key


def test_v156_dedupe_keys_end_in_the_night_date_for_snooze_carry_forward():
    for frag in ("'PIPE_ETL_TASK_FAILED|' || LEFT(w.WORKFLOW_NAME, 200) || '|' || TO_VARCHAR(w.CYCLE_DATE)",
                 "'PIPE_ETL_CYCLE_NOT_STARTED|' || TO_VARCHAR(DATEADD('day', 7, m.REF_NIGHT))",
                 "'PIPE_ETL_CYCLE_LATE|' || g.BAND || '|' || TO_VARCHAR(g.CYCLE_DATE)"):
        assert frag in _BODY
    from tests.test_alert_rule_consistency import _latest_proc_bodies
    assert "TRY_TO_DATE(RIGHT(s.DEDUPE_KEY, 10))" in _latest_proc_bodies()["SP_ALERT_SCAN"]
    # the playbook sentence: snoozing one night's WARN carries to later WARNs, never a CRIT/EXH
    warn = _snooze_identity("PIPE_ETL_CYCLE_LATE|WARN|2026-09-24")
    assert warn == _snooze_identity("PIPE_ETL_CYCLE_LATE|WARN|2026-09-25") == "PIPE_ETL_CYCLE_LATE|WARN"
    assert warn != _snooze_identity("PIPE_ETL_CYCLE_LATE|CRIT|2026-09-25")
    assert _snooze_identity("PIPE_ETL_CYCLE_NOT_STARTED|2026-09-25") == "PIPE_ETL_CYCLE_NOT_STARTED"
    assert _snooze_identity("PIPE_ETL_TASK_FAILED|WF_A|2026-09-25") == "PIPE_ETL_TASK_FAILED|WF_A"


# ---------------------------------------------------------------------------------------------------
# 12-15. seeds, isolation, bounds, TASK_FAILED
def test_v156_seeds_are_insert_only_and_never_auto_clear():
    merge = _merge()
    for rule in _RULES:
        assert f"('{rule}', 'PIPELINE', '" in merge
    assert "WHEN NOT MATCHED THEN INSERT" in merge and "WHEN MATCHED" not in merge
    assert "AUTO_CLEAR" not in merge and "FALSE" not in merge
    live = "\n".join(ln for ln in _MIG.splitlines() if not ln.lstrip().startswith("--"))
    assert "AUTO_CLEAR_ENABLED" not in live
    names = re.findall(r"\('PIPE_ETL_[A-Z_]+', 'PIPELINE', '((?:[^']|'')*)'", merge)
    assert len(names) == 3 and all(len(n) <= 200 for n in names)       # ALERT_CONFIG.NAME VARCHAR(200)
    sev = dict(re.findall(r"\('(PIPE_ETL_[A-Z_]+)', 'PIPELINE', '(?:[^']|'')*', TRUE, '([A-Z]+)'", merge))
    assert sev == {"PIPE_ETL_CYCLE_LATE": "HIGH", "PIPE_ETL_CYCLE_NOT_STARTED": "HIGH",
                   "PIPE_ETL_TASK_FAILED": "MEDIUM"}
    assert "TRUE, 'MEDIUM', 1, 24)" in merge


def test_v156_guard_c_sees_the_new_raiser():
    from tests.test_alert_rule_consistency import _config_enabled, _raised_rule_ids, _raiser_bodies
    assert "SP_SCAN_ETL_CYCLE" in _raiser_bodies()
    assert set(_RULES) <= _raised_rule_ids() and set(_RULES) <= _config_enabled()


def test_v156_rule_blocks_are_isolated_and_never_count_toward_the_scan_tally():
    assert _BODY.count("WHEN OTHER THEN") == 3
    assert _BODY.count("'etl_cycle_scan_failed'") == 3
    for rule in _RULES:
        assert f"'rule {rule} - other ETL cycle rules unaffected'" in _BODY
    assert "fails :=" not in _BODY and "fails INT" not in _BODY


def test_v156_titles_and_details_are_bounded():
    assert _BODY.count(", 300),") >= 3 and _BODY.count(", 2000),") >= 3
    assert "LEFT(:emsg, 2000)" in _BODY


def test_v156_correlated_subqueries_only_at_top_level():
    """Snowflake 002031: no nested scalar subquery. Every '(SELECT' opens a CTE, an EXISTS, an IN list or a
    derived table -- never a scalar in a select list or a comparison."""
    opened = 0
    for m in re.finditer(r"\(\s*SELECT\b", _BODY):
        before = _BODY[max(0, m.start() - 12):m.start()]
        assert re.search(r"\b(?:EXISTS|IN|FROM|AS)\s*$", before), _BODY[m.start() - 60:m.start() + 40]
        opened += 1
    assert opened >= 15


def test_v156_task_failed_threshold_floor_and_retry_auto_clear():
    assert "JOIN wf w ON w.FAILED_TASK_COUNT >= GREATEST(c.MIN_FAILED, 1)" in _BODY
    assert "w.FAILED_TASK_COUNT >= c.MIN_FAILED\n" not in _BODY
    assert "IFF(w.WORKFLOW_NAME = :end_wf AND c.SEVERITY IN ('LOW', 'MEDIUM'), 'HIGH', c.SEVERITY)" in _BODY
    assert "RESOLUTION_KIND = 'AUTO_CLEARED'" in _BODY and "AND STATUS = 'OPEN'" in _BODY
    assert "COALESCE(e.RESOLUTION_KIND, '') <> 'AUTO_CLEARED'" in _BODY      # a re-failure re-alerts
    assert "JOIN anchor a ON t.CYCLE_DATE >= a.CYCLE_DATE" in _BODY
    # header told the truth about TASK_FAILED on a no-starter night (spec correction)
    assert ("No starter run = no LATE for that night; TASK_FAILED still reports any workflow failure keyed "
            "after the\n--     last cycle start") in _HEADER


# ---------------------------------------------------------------------------------------------------
# 16-19. lockstep (validate / docs / admin pins at the wave tip fail until the integration commit)
def test_v156_validate_pin_at_the_wave_tip():
    val = _read("snowflake/validate.sql")
    assert f"V001..V{_TIP} applied" in val and f"VERSION BETWEEN 1 AND {_TIP}) = {_TIP}" in val


def test_v156_docs_list_the_migration():
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert _NAME in _read(rel), rel


def test_v156_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 156 in _EXPECTED_MIGRATIONS
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_" not in _EXPECTED_MIGRATIONS[156]


def test_v156_teardown_tracks_the_new_objects():
    text = _read("snowflake/teardown.sql")
    live = [ln for ln in text.splitlines() if not ln.lstrip().startswith("--")]
    assert any(ln.startswith("DROP PROCEDURE IF EXISTS DBA_MAINT_DB.OVERWATCH.SP_SCAN_ETL_CYCLE();") for ln in live)
    assert "-- DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS;" in text
    assert not any("ETL_CYCLE_TASKS" in ln for ln in live), "the cache stays a commented (data) drop"


def test_v156_rules_have_playbooks():
    from app.logic.playbooks import PLAYBOOKS, playbook_for
    banned = re.compile(r"\bretired\b|no alert fires|pages nothing|does not fire|scan arm was removed", re.I)
    for rule in _RULES:
        text = PLAYBOOKS[rule]
        assert "add one" not in playbook_for(rule) and text.startswith("**Means:**")
        assert "$" not in text and not banned.search(text), rule
    late = PLAYBOOKS["PIPE_ETL_CYCLE_LATE"]
    assert "Snoozing one night's WARN also snoozes later nights' WARN, never a CRIT/EXH." in late
    assert "Only lead-window WARN tags tune THRESHOLD_NUM" in late
    assert "no incident" in late and "CRITICAL and auto-declares an incident" in late
    assert "raised HIGH" in PLAYBOOKS["PIPE_ETL_TASK_FAILED"]


def test_v156_late_tunes_lower_is_worse():
    import pandas as pd

    from app.logic.tuning import LOWER_IS_WORSE, suggestions_by_rule
    assert "PIPE_ETL_CYCLE_LATE" in LOWER_IS_WORSE
    events = pd.DataFrame({"RULE_ID": ["PIPE_ETL_CYCLE_LATE"] * 6, "METRIC_VALUE": [50, 52, 49, 51, 50, 53],
                           "RESOLUTION_KIND": ["NOISE"] * 6})
    out = suggestions_by_rule(events, {"PIPE_ETL_CYCLE_LATE": 60.0})
    row = out[out["RULE_ID"] == "PIPE_ETL_CYCLE_LATE"].iloc[0]
    assert row["SUGGESTED_THRESHOLD"] < 60, "noise-cutting move for a <= rule is DOWNWARD (narrower lead)"


def test_v156_part_b_ddl_fragments_are_in_the_migration():
    """RUN_NEXT PART B (V156.1) reads GET_DDL('PROCEDURE', '...SP_SCAN_ETL_CYCLE()') for these fragments;
    a fragment that is not literally in the proc body would read FALSE on a correct apply."""
    for frag in ("t.TERMINAL_START >= s.CYCLE_START", "GREATEST(c.MIN_FAILED, 1)", "CROSS JOIN term_lw tl",
                 "IFF(g.BAND = 'WARN' OR g.IS_COMPLETE, g.SEVERITY, 'CRITICAL')"):
        assert _BODY.count(frag) == 1, frag


# ---------------------------------------------------------------------------------------------------
# 20-21. SQL parses; every column reference resolves
def test_v156_plain_sql_parses():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    stmts = list(_plain_statements(_MIG))
    assert len(stmts) == 3                                   # table, seed MERGE, SCHEMA_VERSION
    for statement in stmts:
        sqlglot.parse(statement, dialect="snowflake")


def test_v156_rule_sql_parses_and_every_column_resolves():
    sqlglot = pytest.importorskip("sqlglot")
    from sqlglot.optimizer.qualify import qualify
    stmts = _rule_statements()
    assert len(stmts) == 8          # 3 raises + 1 auto-clear UPDATE + 3 failure logs + 1 misconfig log
    for stmt in stmts:
        tree = sqlglot.parse_one(_bind(stmt), read="snowflake")
        qualify(tree, schema=_SCHEMA, dialect="snowflake", validate_qualify_columns=True)


# ---------------------------------------------------------------------------------------------------
# A Python model of the proc's collapse (ins_sql) and the [C] grading, pinned to the SQL fragments it
# mirrors. It proves the plan corrections behave: the afternoon terminal re-run, the regular-terminal
# guard, the severity split and the lead-window METRIC_VALUE.
_MODEL_FRAGMENTS = (
    "DATE(DATEADD(''hour'', -12, TASK_START_DTTM::TIMESTAMP_NTZ))",
    "MIN(TASK_START_DTTM)::TIMESTAMP_NTZ",
    "MAX_BY(TASK_START_DTTM, COALESCE(TASK_END_DTTM, TASK_START_DTTM))::TIMESTAMP_NTZ",
    "SELECT CYCLE_DATE, MIN(FIRST_START) AS CYCLE_START\n",
    "t.TERMINAL_START >= s.CYCLE_START",
    "QUALIFY ROW_NUMBER() OVER (ORDER BY n.CYCLE_DATE DESC) <= 14",
    "AND n.TERM_TASKS >= COALESCE(h.MIN_TERM_TASKS, 1)) AS IS_COMPLETE",
    "WHERE :now_ct < DATEADD('hour', 12, t.DL_H)",
    "AND NOT (t.TERM_TASKS = 0 AND COALESCE(tl.N, 0) = 0)",
    "WHEN IFF(t.IS_COMPLETE, t.CYCLE_FINISH, :now_ct) > t.DL_H THEN 'EXH'",
    "WHEN IFF(t.IS_COMPLETE, t.CYCLE_FINISH, :now_ct) > t.DL_T THEN 'CRIT'",
    "AND (:now_ct >= DATEADD('minute', -c.LEAD_MIN, t.DL_T)",
    "IFF(g.BAND = 'WARN' OR g.IS_COMPLETE, g.SEVERITY, 'CRITICAL')",
)
_FAILED = frozenset(etl.FAILED_TASK_STATUSES)
_S, _T = "WF_START", "WF_TERM"


def _collapse(rows):
    """ins_sql: (workflow, task, status, start, end) attempts -> one row per (night, workflow, task)."""
    groups: dict = {}
    for wf, task, status, start, end in rows:
        groups.setdefault(((start - timedelta(hours=12)).date(), wf, task), []).append((status, start, end))
    out = []
    for (night, wf, task), att in groups.items():
        term = max(att, key=lambda a: a[2] or a[1])          # MAX_BY(.., COALESCE(end, start))
        out.append({"CYCLE_DATE": night, "WORKFLOW_NAME": wf, "TASK_NAME": task, "TERMINAL_STATUS": term[0],
                    "FIRST_START": min(a[1] for a in att), "TERMINAL_START": term[1], "TERMINAL_END": term[2]})
    return out


def _late(tasks, now, *, filter_col="TERMINAL_START", lead=60, target_off=420, breach_off=480, sev="HIGH"):
    """[C] PIPE_ETL_CYCLE_LATE over the cache rows -> {band, severity, metric, ...} or None."""
    starts: dict = {}
    for t in tasks:
        if t["WORKFLOW_NAME"] == _S:
            d = t["CYCLE_DATE"]
            starts[d] = min(starts.get(d, t["FIRST_START"]), t["FIRST_START"])
    nights = {}
    for d, cs in starts.items():
        term = [t for t in tasks if t["WORKFLOW_NAME"] == _T and t["CYCLE_DATE"] == d and t[filter_col] >= cs]
        failed = sum(1 for t in term if str(t["TERMINAL_STATUS"] or "").upper() in _FAILED)
        running = sum(1 for t in term if t["TERMINAL_END"] is None
                      and str(t["TERMINAL_STATUS"] or "").upper() not in _FAILED)
        dl_t = datetime.combine(cs.date(), time()) + timedelta(minutes=target_off)
        if not dl_t > cs:
            dl_t += timedelta(days=1)
        nights[d] = {"start": cs, "finish": max((t["TERMINAL_END"] for t in term if t["TERMINAL_END"]), default=None),
                     "term": len(term), "failed": failed, "running": running, "dl_t": dl_t}
    latest = max(nights)
    last_week = nights.get(latest - timedelta(days=7))
    term_lw = 1 if last_week and last_week["term"] > 0 else 0
    clean = [d for d in sorted(nights, reverse=True) if d < latest and nights[d]["finish"] is not None
             and nights[d]["failed"] == 0 and nights[d]["running"] == 0][:14]
    med = statistics.median((nights[d]["finish"] - nights[d]["start"]).total_seconds() for d in clean) if clean else None
    min_term = min(nights[d]["term"] for d in clean) if clean else None
    n = nights[latest]
    dl_h = n["dl_t"] + timedelta(minutes=breach_off - target_off)
    complete = (n["finish"] is not None and n["failed"] == 0 and n["running"] == 0
                and n["term"] >= (min_term if min_term is not None else 1))
    proj = max(now, n["start"] + timedelta(seconds=round(med))) if len(clean) >= 4 and med is not None else None
    if not now < dl_h + timedelta(hours=12) or (n["term"] == 0 and term_lw == 0):
        return None
    ref = n["finish"] if complete else now
    if ref > dl_h:
        band = "EXH"
    elif ref > n["dl_t"]:
        band = "CRIT"
    elif not complete and (now >= n["dl_t"] - timedelta(minutes=lead) or (proj is not None and proj > dl_h)):
        band = "WARN"
    else:
        return None
    projected = proj is not None and proj > dl_h
    return {"band": band, "severity": sev if (band == "WARN" or complete) else "CRITICAL",
            "metric": (int((n["dl_t"] - now).total_seconds() // 60) if band == "WARN" and not projected else None),
            "key": f"PIPE_ETL_CYCLE_LATE|{band}|{latest.isoformat()}", "term": n["term"], "complete": complete}


def _night(d: date, *, term_start: time = time(4, 0), term_end: time | None = time(5, 0), status="SUCCEEDED",
           starter_at: time = time(22, 0), terminal: bool = True):
    """One ordinary night keyed d: the starter at d 22:00, two terminal tasks the next morning."""
    at = datetime.combine(d, starter_at)
    rows = [(_S, "s_kickoff", "SUCCEEDED", at, at + timedelta(minutes=5))]
    if terminal:
        nxt = d + timedelta(days=1)
        for task in ("s_recon_1", "s_recon_2"):
            end = datetime.combine(nxt, term_end) if term_end else None
            rows.append((_T, task, status if term_end else "RUNNING", datetime.combine(nxt, term_start), end))
    return rows


_TONIGHT = date(2026, 9, 22)          # a Tuesday night; the history is the 14 nights before it


def _history(*, skip_last_week_terminal: bool = False):
    rows = []
    for back in range(1, 15):
        d = _TONIGHT - timedelta(days=back)
        rows += _night(d, terminal=not (skip_last_week_terminal and back == 7))
    return rows


def test_v156_model_mirrors_the_sql_fragments():
    for frag in _MODEL_FRAGMENTS:
        assert frag in _BODY, frag


def test_v156_model_afternoon_terminal_rerun_does_not_suppress_completion():
    """The plan's material correction: a terminal re-run Tue 13:00 shares Tue's night key with the real
    Wed 04:00 run. Keyed on FIRST_START (13:00 < the 22:00 kickoff) the whole task dropped out of the night
    and a clean 05:00 finish raised CRIT at 07:10 (CRITICAL + an incident); keyed on the TERMINAL attempt's
    start it counts and the night is complete, so nothing is raised."""
    rerun = [(_T, task, "SUCCEEDED", datetime.combine(_TONIGHT, time(13, 0)),
              datetime.combine(_TONIGHT, time(13, 20))) for task in ("s_recon_1", "s_recon_2")]
    tasks = _collapse(_history() + rerun + _night(_TONIGHT))
    tonight = [t for t in tasks if t["CYCLE_DATE"] == _TONIGHT and t["WORKFLOW_NAME"] == _T]
    assert len(tonight) == 2 and all(t["FIRST_START"].hour == 13 and t["TERMINAL_START"].hour == 4 for t in tonight)
    now = datetime.combine(_TONIGHT + timedelta(days=1), time(7, 10))
    assert _late(tasks, now) is None
    old = _late(tasks, now, filter_col="FIRST_START")
    assert old and old["band"] == "CRIT" and old["severity"] == "CRITICAL" and old["term"] == 0


def test_v156_model_afternoon_rerun_alone_is_not_tonights_completion():
    """Before the real run starts, the afternoon re-run's terminal attempt (13:00) is before the kickoff, so the
    night reads not dispatched: quiet at 23:30, a lead-window WARN at 06:10 (METRIC_VALUE 50 min)."""
    rerun = [(_T, task, "SUCCEEDED", datetime.combine(_TONIGHT, time(13, 0)),
              datetime.combine(_TONIGHT, time(13, 20))) for task in ("s_recon_1", "s_recon_2")]
    tasks = _collapse(_history() + rerun + _night(_TONIGHT, terminal=False))
    assert _late(tasks, datetime.combine(_TONIGHT, time(23, 30))) is None
    warn = _late(tasks, datetime.combine(_TONIGHT + timedelta(days=1), time(6, 10)))
    assert warn == {"band": "WARN", "severity": "HIGH", "metric": 50, "term": 0, "complete": False,
                    "key": "PIPE_ETL_CYCLE_LATE|WARN|2026-09-22"}


def test_v156_model_weekday_only_terminal_is_not_judged():
    """term_lw: a night whose terminal has not dispatched is judged only when it dispatched on the same night
    last week (a weekday-only terminal stays quiet on its off night)."""
    now = datetime.combine(_TONIGHT + timedelta(days=1), time(7, 10))
    quiet = _collapse(_history(skip_last_week_terminal=True) + _night(_TONIGHT, terminal=False))
    assert _late(quiet, now) is None
    due = _collapse(_history() + _night(_TONIGHT, terminal=False))
    miss = _late(due, now)
    assert miss and miss["band"] == "CRIT" and miss["severity"] == "CRITICAL"


def test_v156_model_severity_split_and_lead_window_metric():
    tomorrow = _TONIGHT + timedelta(days=1)
    at = lambda h, m: datetime.combine(tomorrow, time(h, m))   # noqa: E731
    # finished 07:05 (after the 07:00 target, before the 08:00 hard deadline) -> CRIT at the rule severity
    late_done = _collapse(_history() + _night(_TONIGHT, term_start=time(6, 0), term_end=time(7, 5)))
    got = _late(late_done, at(7, 10))
    assert got and (got["band"], got["severity"], got["metric"]) == ("CRIT", "HIGH", None)
    # finished 08:30 -> EXH, still HIGH (email, no incident)
    very_late = _collapse(_history() + _night(_TONIGHT, term_start=time(6, 0), term_end=time(8, 30)))
    got = _late(very_late, at(8, 40))
    assert got and (got["band"], got["severity"]) == ("EXH", "HIGH")
    # still running -> CRITICAL at both crossings
    running = _collapse(_history() + _night(_TONIGHT, term_start=time(6, 0), term_end=None))
    assert _late(running, at(7, 10))["severity"] == "CRITICAL"
    assert _late(running, at(8, 10))["band"] == "EXH"
    # lead-window WARN carries minutes-left; a projection WARN (a 02:00 start, 7h typical) carries NULL
    lead = _late(running, at(6, 10))
    assert (lead["band"], lead["metric"]) == ("WARN", 50)
    slow = _collapse([*_history(), (_S, "s_kickoff", "SUCCEEDED", at(2, 0), at(2, 5))])   # night key = tonight
    proj = _late(slow, at(3, 10))
    assert proj and (proj["band"], proj["severity"], proj["metric"]) == ("WARN", "HIGH", None)
    # a clean on-time night raises nothing
    assert _late(_collapse(_history() + _night(_TONIGHT)), at(7, 10)) is None
