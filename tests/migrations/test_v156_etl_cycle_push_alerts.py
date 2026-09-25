"""Locks for V156 — nightly ETL-cycle PUSH alerts (Next-Fifty rank 2, wave 2b).

SP_SCAN_ETL_CYCLE() reads the configured Informatica CONTROL_STATUS table once per run into the transient
ETL_CYCLE_TASKS cache (the 23 newest WHOLE nights, retries collapsed to each task's terminal attempt, night key
DATE(start - 12h), FIRST_OK_END = the first clean finish after the night's last kickoff) and raises
PIPE_ETL_TASK_FAILED / PIPE_ETL_CYCLE_NOT_STARTED / PIPE_ETL_CYCLE_LATE itself, each rule in its own EXCEPTION
guard. New objects only (no re-derivation, so no generator): the hourly SP_ALERT_SCAN gains the add-on CALL arm
in V157. These locks pin the app parity (status words, night key, retry collapse, settings defaults, the Overdue
test, the forecaster constants), the plan corrections (TERMINAL_START filter, GREATEST threshold floor, the
term_lw regular-terminal guard, the newest-14 clean history, CRITICAL only for an unfinished miss,
lead-window-only METRIC_VALUE), the review fixes (FIRST_OK_END bounded by the night's first AND last kickoff,
auto-clear only on a FINISHED clean final attempt, whole-night cache) and Python models of the collapse, the [A]
raise / auto-clear, the [B] Overdue test and the [C] grading. The one dynamic statement (rendered) and the three
rule INSERTs are each pinned WHOLE against a test-side golden -- every CTE compared to its own closing paren,
then the outer select -- so no clause a model encodes can be edited, dropped or appended to without a failing
test, and a model and the SQL cannot drift apart silently.

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
_INS_SPLIT = ";\n    EXECUTE IMMEDIATE :ins_sql USING (start_wf);"


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _strip_comments(sql: str) -> str:
    """Drop '--' line comments outside single-quoted literals (a doubled '' toggles twice, so it is safe)."""
    out: list[str] = []
    i, n, quoted = 0, len(sql), False
    while i < n:
        c = sql[i]
        if not quoted and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "'":
            quoted = not quoted
        out.append(c)
        i += 1
    return "".join(out)


def _norm(sql: str) -> str:
    """Whitespace-normalized, comment-free SQL: one space between tokens, none inside '( ' / ' )'."""
    s = re.sub(r"\s+", " ", _strip_comments(sql)).strip()
    return re.sub(r"\s+\)", ")", re.sub(r"\(\s+", "(", s))


_NBODY = _norm(_BODY)


def _header_text() -> str:
    """The migration header as one line of prose (the '-- ' prefixes and line wraps collapsed)."""
    return " ".join(ln[2:].strip() for ln in _HEADER.splitlines() if ln.startswith("--"))


def _once(frag: str) -> None:
    """The fragment occurs EXACTLY once in the normalized proc body (a mutation of it cannot hide elsewhere)."""
    assert _NBODY.count(_norm(frag)) == 1, frag


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


def _raise_statements() -> list[str]:
    raises = [s for s in _rule_statements() if s.startswith("INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS")]
    assert len(raises) == 3
    return raises                      # [A] TASK_FAILED, [B] NOT_STARTED, [C] LATE, in body order


def _auto_clear_statement() -> str:
    (upd,) = [s for s in _rule_statements() if s.startswith("UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS")]
    return upd


def _bind(sql: str) -> str:
    sql = sql.replace(":now_ct", "'2026-09-25 06:10:00'::TIMESTAMP_NTZ")
    sql = re.sub(r":(start_wf|end_wf|emsg)\b", "'X'", sql)
    return re.sub(r":[a-z_]+\b", "420", sql)


def _lookback() -> int:
    """The proc's shipped DECLARE default (the render and the cache model read THIS, never a test value)."""
    found = re.findall(r"\n    lookback_days INT DEFAULT (\d+);\n", _BODY)
    assert len(found) == 1 and _BODY.count("lookback_days INT") == 1
    return int(found[0])


def _render_ins_sql(fqn: str = "DB.S.CONTROL_STATUS") -> str:
    """Evaluate the ins_sql concatenation the proc EXECUTE IMMEDIATEs (literals un-doubled), with the shipped
    lookback_days DEFAULT."""
    lookback = _lookback()
    expr = _BODY.split("ins_sql := ", 1)[1].split(_INS_SPLIT, 1)[0]
    out = []
    for piece in re.findall(r"'(?:[^']|'')*'|[^|\s][^|]*?(?=\s*\|\||\s*$)", expr):
        piece = piece.strip()
        if piece.startswith("'"):
            out.append(piece[1:-1].replace("''", "'"))
        elif piece == "TRIM(:ctl_fqn)":
            out.append(fqn)
        elif piece == ":lookback_days":
            out.append(str(lookback))
        elif piece == "(:lookback_days + 1)":
            out.append(str(lookback + 1))
        else:
            raise AssertionError(f"unexpected ins_sql piece: {piece!r}")
    return "".join(out)


# The rendered dynamic INSERT, as a test-side golden: the INSERT column order <-> select-list order, the NOT NULL
# filter, the whole-night cut, the bound starter name and FIRST_OK_END bounded by the START of an attempt vs
# the night's last kickoff (CYC_LAST_START, MAX starter start) are all in it.
_INS_SQL = (
    "INSERT INTO DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS "
    "(CYCLE_DATE, WORKFLOW_NAME, TASK_NAME, TERMINAL_STATUS, FIRST_START, TERMINAL_START, TERMINAL_END, FIRST_OK_END) "
    "WITH src AS (SELECT DATE(DATEADD('hour', -12, TASK_START_DTTM::TIMESTAMP_NTZ)) AS CD, "
    "LEFT(TO_VARCHAR(WORKFLOW_NAME), 500) AS WF, LEFT(TO_VARCHAR(TASK_NAME), 500) AS TN, "
    "TASK_STATUS, TASK_START_DTTM, TASK_END_DTTM "
    "FROM DB.S.CONTROL_STATUS "
    "WHERE TASK_START_DTTM IS NOT NULL AND WORKFLOW_NAME IS NOT NULL AND TASK_NAME IS NOT NULL "
    "AND TASK_START_DTTM >= DATEADD('day', -24, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ) "
    "AND DATE(DATEADD('hour', -12, TASK_START_DTTM::TIMESTAMP_NTZ)) > DATEADD('day', -23, "
    "DATE(DATEADD('hour', -12, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ)))), "
    "cs AS (SELECT CD AS CS_NIGHT, "
    "MAX(TASK_START_DTTM) AS CYC_LAST_START FROM src WHERE WF = ? GROUP BY CD) "
    "SELECT CD, WF, TN, "
    "LEFT(TO_VARCHAR(MAX_BY(TASK_STATUS, COALESCE(TASK_END_DTTM, TASK_START_DTTM))), 100), "
    "MIN(TASK_START_DTTM)::TIMESTAMP_NTZ, "
    "MAX_BY(TASK_START_DTTM, COALESCE(TASK_END_DTTM, TASK_START_DTTM))::TIMESTAMP_NTZ, "
    "MAX_BY(TASK_END_DTTM, COALESCE(TASK_END_DTTM, TASK_START_DTTM))::TIMESTAMP_NTZ, "
    "MIN(IFF(TASK_END_DTTM IS NOT NULL "
    f"AND COALESCE(UPPER(TASK_STATUS), '') NOT IN ({_FAILED_SQL}) "
    "AND TASK_START_DTTM >= CYC_LAST_START, TASK_END_DTTM, NULL))::TIMESTAMP_NTZ "
    "FROM src LEFT JOIN cs ON cs.CS_NIGHT = src.CD "
    "GROUP BY CD, WF, TN"
)

_CACHE_COLUMNS = ("CYCLE_DATE", "WORKFLOW_NAME", "TASK_NAME", "TERMINAL_STATUS", "FIRST_START", "TERMINAL_START",
                  "TERMINAL_END", "FIRST_OK_END", "SCANNED_AT")
_SCHEMA = {"DBA_MAINT_DB": {"OVERWATCH": {
    "ETL_CYCLE_TASKS": dict.fromkeys(_CACHE_COLUMNS, "VARCHAR"),
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
    assert cols == list(_CACHE_COLUMNS)
    assert "    TERMINAL_START  TIMESTAMP_NTZ,\n" in table
    assert "    FIRST_OK_END    TIMESTAMP_NTZ,\n" in table          # nullable: no clean in-cycle finish yet


# ---------------------------------------------------------------------------------------------------
# 3. the one dynamic statement is allowlist-gated, and the starter name is bound as data
def test_v156_fqn_allowlisted_and_single_dynamic_statement():
    rl = "RLIKE(TRIM(:ctl_fqn), '^[A-Za-z0-9_$]+([.][A-Za-z0-9_$]+){0,3}$')"
    assert rl in _BODY
    assert "'^[A-Za-z0-9_$]+([.][A-Za-z0-9_$]+){0,3}$'" in _read(
        "snowflake/migrations/V137__dq_recon_error_alert.sql")
    assert _BODY.count("EXECUTE IMMEDIATE") == 1
    assert _BODY.index(rl) < _BODY.index("'FROM ' || TRIM(:ctl_fqn)")
    # the starter name reaches the dynamic statement ONLY as a bind value (one '?', USING (start_wf))
    assert _BODY.count("EXECUTE IMMEDIATE :ins_sql USING (start_wf);") == 1
    expr = _BODY.split("ins_sql := ", 1)[1].split(_INS_SPLIT, 1)[0]
    assert ":start_wf" not in expr and ":end_wf" not in expr and _render_ins_sql().count("?") == 1
    # the FQN never reaches event text (navigate._DB_RE would turn 'DB.SCHEMA.' into a database filter)
    for stmt in _rule_statements():
        assert ":ctl_fqn" not in stmt


def test_v156_dynamic_insert_renders_parses_and_reads_only_app_columns():
    sqlglot = pytest.importorskip("sqlglot")
    from sqlglot import exp
    from sqlglot.optimizer.qualify import qualify
    assert _lookback() == 23, "the proc's shipped lookback DEFAULT (term_lw and NOT_STARTED need >= 14 nights)"
    sql = _render_ins_sql()
    assert sql == _INS_SQL
    assert ("TASK_START_DTTM >= DATEADD('day', -24, "
            "CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ)") in sql   # prunes only
    assert ("AND DATE(DATEADD('hour', -12, TASK_START_DTTM::TIMESTAMP_NTZ)) > DATEADD('day', -23, DATE(DATEADD("
            "'hour', -12, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ))))") in sql
    tree = sqlglot.parse_one(sql, read="snowflake")
    # the INSERT column list zipped with the SELECT projections (a swapped line reverts a plan correction)
    cols = [c.name for c in tree.this.expressions]
    assert cols == list(_CACHE_COLUMNS[:-1])
    got = dict(zip(cols, tree.expression.expressions, strict=True))
    want = {
        "CYCLE_DATE": "CD", "WORKFLOW_NAME": "WF", "TASK_NAME": "TN",
        "TERMINAL_STATUS": "LEFT(TO_VARCHAR(MAX_BY(TASK_STATUS, COALESCE(TASK_END_DTTM, TASK_START_DTTM))), 100)",
        "FIRST_START": "MIN(TASK_START_DTTM)::TIMESTAMP_NTZ",
        "TERMINAL_START": "MAX_BY(TASK_START_DTTM, COALESCE(TASK_END_DTTM, TASK_START_DTTM))::TIMESTAMP_NTZ",
        "TERMINAL_END": "MAX_BY(TASK_END_DTTM, COALESCE(TASK_END_DTTM, TASK_START_DTTM))::TIMESTAMP_NTZ",
        "FIRST_OK_END": (f"MIN(IFF(TASK_END_DTTM IS NOT NULL AND COALESCE(UPPER(TASK_STATUS), '') NOT IN "
                         f"({_FAILED_SQL}) AND TASK_START_DTTM >= CYC_LAST_START, "
                         "TASK_END_DTTM, NULL))::TIMESTAMP_NTZ"),
    }
    for col, src in want.items():
        assert got[col] == sqlglot.parse_one(src, read="snowflake"), col
    ctes = {c.alias: c.this for c in tree.find_all(exp.CTE)}
    assert set(ctes) == {"src", "cs"}
    src_proj = {p.alias: p.this for p in ctes["src"].expressions if p.alias}
    assert src_proj["CD"] == sqlglot.parse_one("DATE(DATEADD('hour', -12, TASK_START_DTTM::TIMESTAMP_NTZ))",
                                               read="snowflake")
    # cs: the night's LAST kickoff (MAX) over the bound starter's attempts, and nothing else (no second bind:
    # the starter-IS-terminal case is deliberately not special-cased, see the header)
    cs_proj = {p.alias: p.this for p in ctes["cs"].expressions}
    assert cs_proj == {"CS_NIGHT": sqlglot.parse_one("CD", read="snowflake"),
                       "CYC_LAST_START": sqlglot.parse_one("MAX(TASK_START_DTTM)", read="snowflake")}
    assert ctes["cs"].args["where"].this == sqlglot.parse_one("WF = ?", read="snowflake")
    schema = {**_SCHEMA, "DB": {"S": {"CONTROL_STATUS": dict.fromkeys(_CTL_COLUMNS, "VARCHAR")}}}
    qualify(sqlglot.parse_one(sql.replace("?", "'X'"), read="snowflake"), schema=schema, dialect="snowflake",
            validate_qualify_columns=True)
    # every CONTROL_STATUS column read is one the app's own builders already read
    app_sql = (etl.cycle_night_health_scan("DB.S.T", start_workflow="A")
               + etl.cycle_finish_history_scan("DB.S.T", start_workflow="A", end_workflow="B"))
    for col in _CTL_COLUMNS:
        assert col in app_sql, col


# ---------------------------------------------------------------------------------------------------
# 4-7. parity with the app's ETL semantics
def test_v156_failed_status_lists_match_the_app():
    lists = re.findall(r"(NOT )?IN \(('ABORTED'[^)]*)\)", _BODY)
    # FAILED_TASK_COUNT, LISTAGG, auto-clear HAVING, [C] N_FAILED (IN) and [C] N_RUNNING (NOT IN), in order
    assert [bool(neg) for neg, _ in lists] == [False, False, False, False, True]
    for _, lst in lists:
        assert {s.strip().strip("'") for s in lst.split(",")} == set(etl.FAILED_TASK_STATUSES)
        assert lst == _FAILED_SQL, "same rendering as etl_control_sql's _failed IN-list"
    # the dynamic FIRST_OK_END clean test is the NOT IN twin (a NULL status with an end reads clean, as [C])
    assert _render_ins_sql().count(f"COALESCE(UPPER(TASK_STATUS), '') NOT IN ({_FAILED_SQL})") == 1
    assert f"IN ({_FAILED_SQL})" in etl.cycle_finish_history_scan("DB.S.T", start_workflow="A", end_workflow="B")


def test_v156_night_key_and_retry_collapse_match_the_app():
    app = etl.cycle_finish_history_scan("DB.S.T", start_workflow="A", end_workflow="B")
    night = etl.cycle_night_health_scan("DB.S.T", start_workflow="A")
    for sql in (app, night):
        assert "DATE(DATEADD('hour', -12, " in sql
        assert "COALESCE(TASK_END_DTTM, TASK_START_DTTM))" in sql or "COALESCE(s.TASK_END_DTTM" in sql
    assert "MAX_BY(TASK_STATUS, COALESCE(TASK_END_DTTM, TASK_START_DTTM))" in app
    assert "MAX_BY(TASK_END_DTTM, COALESCE(TASK_END_DTTM, TASK_START_DTTM))" in app
    assert "DATE(DATEADD(''hour'', -12, TASK_START_DTTM::TIMESTAMP_NTZ)) AS CD" in _BODY
    for frag in ("MAX_BY(TASK_STATUS, COALESCE(TASK_END_DTTM, TASK_START_DTTM))",
                 "MAX_BY(TASK_END_DTTM, COALESCE(TASK_END_DTTM, TASK_START_DTTM))",
                 "MAX_BY(TASK_START_DTTM, COALESCE(TASK_END_DTTM, TASK_START_DTTM))::TIMESTAMP_NTZ",
                 "MIN(TASK_START_DTTM)::TIMESTAMP_NTZ"):
        assert _BODY.count(frag) == 1, frag
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


def _clock_iff(var: str, raw: str, fallback: int) -> str:
    return (f"{var} := IFF(RLIKE(:{raw}, '^[0-9]{{1,2}}:[0-9]{{1,2}}$') "
            f"AND TRY_TO_NUMBER(SPLIT_PART(:{raw}, ':', 1)) <= 23 "
            f"AND TRY_TO_NUMBER(SPLIT_PART(:{raw}, ':', 2)) <= 59, "
            f"TRY_TO_NUMBER(SPLIT_PART(:{raw}, ':', 1)) * 60 + TRY_TO_NUMBER(SPLIT_PART(:{raw}, ':', 2)), "
            f"{fallback});")


def test_v156_clock_parse_sql_is_the_mirrored_expression():
    """The SQL clock parse _sql_hhmm mirrors, as exact text: hours = part 1 * 60, minutes = part 2, the <= 23 /
    <= 59 bounds, the 07:00 / 08:00 fallbacks and the hard-deadline floor."""
    _once(_clock_iff("target_off", "target_raw", 420))
    _once(_clock_iff("breach_off", "breach_raw", 480))
    _once("IF (:breach_off <= :target_off) THEN breach_off := :target_off + 60; END IF;")
    assert _NBODY.index(_norm(_clock_iff("breach_off", "breach_raw", 480))) < _NBODY.index(
        "IF (:breach_off <= :target_off)")


def _sql_hhmm(raw: str, fallback: int) -> int:
    """Python mirror of the proc's target_off / breach_off IFF (after the SELECT's TRIM), pinned as exact text
    by test_v156_clock_parse_sql_is_the_mirrored_expression."""
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
    # the dynamic INSERT's two clock reads (prefilter + whole-night cut), both Central wall-clock NTZ
    assert _BODY.count("CONVERT_TIMEZONE(''America/Chicago'', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ") == 2
    assert "CURRENT_DATE()" not in _BODY and "SYSDATE" not in _BODY
    # the only other CURRENT_TIMESTAMP() is the house RESOLVED_AT stamp of the retry auto-clear
    assert _BODY.count("CURRENT_TIMESTAMP()") == 4
    assert "SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'AUTO_CLEARED'" in _BODY
    assert "LOGGED_AT >= DATE_TRUNC('day', :now_ct)" in _BODY


_B_MISSED = """
    missed AS (
        SELECT c.RULE_ID, c.SEVERITY, c.GRACE_MIN, a.CYCLE_START_AT AS LAST_START,
               MAX(IFF(DATEADD('minute', 7 * 1440 + c.GRACE_MIN, p.CYCLE_START_AT) < :now_ct,
                       p.CYCLE_DATE, NULL)) AS REF_NIGHT,
               MAX(IFF(DATEADD('minute', 7 * 1440 + c.GRACE_MIN, p.CYCLE_START_AT) < :now_ct,
                       p.CYCLE_START_AT, NULL)) AS REF_START
        FROM cfg c
        CROSS JOIN anchor a
        JOIN cyc p
          ON p.CYCLE_DATE BETWEEN DATEADD('day', -6, a.CYCLE_DATE)
                              AND DATEADD('day', -7, DATE(DATEADD('hour', -12, :now_ct)))
        GROUP BY c.RULE_ID, c.SEVERITY, c.GRACE_MIN, a.CYCLE_START_AT
    )"""
_B_FIRES = """
    FROM missed m
    WHERE m.REF_NIGHT IS NOT NULL
      AND DATEDIFF('second', m.LAST_START, :now_ct) > 86400 + m.GRACE_MIN * 60
    ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)"""


def test_v156_not_started_is_the_app_overdue_test():
    app = etl.cycle_night_health_scan("DB.S.T", start_workflow="A")
    assert "7 * 86400 + 7200" in app
    assert etl.NIGHT_NOT_STARTED_SEC == 86400 + 7200
    assert "('PIPE_ETL_CYCLE_NOT_STARTED', 'PIPELINE'," in _merge() and "TRUE, 'HIGH', 120, 24)" in _merge()
    for frag in ("7 * 1440 + c.GRACE_MIN", "86400 + m.GRACE_MIN * 60", "DATEADD('day', -6, a.CYCLE_DATE)",
                 "DATEADD('day', -7, DATE(DATEADD('hour', -12, :now_ct)))",
                 "'PIPE_ETL_CYCLE_NOT_STARTED|' || TO_VARCHAR(DATEADD('day', 7, m.REF_NIGHT))"):
        assert frag in _BODY, frag
    # the comparisons themselves (the model below encodes exactly these), not just the constants
    for frag in (_B_MISSED, _B_FIRES,
                 "cyc AS (SELECT CYCLE_DATE, MIN(FIRST_START) AS CYCLE_START_AT FROM "
                 "DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS WHERE WORKFLOW_NAME = :start_wf GROUP BY CYCLE_DATE)",
                 "anchor AS (SELECT CYCLE_DATE, CYCLE_START_AT FROM cyc "
                 "QUALIFY ROW_NUMBER() OVER (ORDER BY CYCLE_DATE DESC) = 1)"):
        _once(frag)
    assert "BETWEEN DATEADD('day', -6, a.CYCLE_DATE)" in app
    assert "AND DATEADD('day', -7, DATE(DATEADD('hour', -12, CURRENT_TIMESTAMP())))" in app


# ---------------------------------------------------------------------------------------------------
# 8-11. PIPE_ETL_CYCLE_LATE: projection constants, plan corrections, bands and keys
def test_v156_late_projection_shares_the_forecaster_constants():
    assert insights.SLA_FORECAST_MIN_RUNS == 4 and "IFF(h.N_HIST >= 4," in _BODY
    assert insights.SLA_FORECAST_FIT_NIGHTS == 14
    assert "QUALIFY ROW_NUMBER() OVER (ORDER BY n.CYCLE_DATE DESC) <= 14" in _BODY
    assert "DATEADD('day', -14, l.CYCLE_DATE)" not in _BODY, "newest 14 clean nights, not 14 calendar days"
    # the whole hist derived table: PRIOR nights only (a partial tonight in hist would set MIN_TERM_TASKS=1)
    _once("FROM (SELECT n.* FROM nights n JOIN latest l ON n.CYCLE_DATE < l.CYCLE_DATE "
          "WHERE n.CYCLE_FINISH IS NOT NULL AND n.N_FAILED = 0 AND n.N_RUNNING = 0 "
          "QUALIFY ROW_NUMBER() OVER (ORDER BY n.CYCLE_DATE DESC) <= 14) n")
    assert "DATEADD('minute', :target_off + 1440, DATE_TRUNC('day', s.CYCLE_START))" in _BODY  # _deadline_after
    assert "GREATEST(:now_ct, DATEADD('second', ROUND(h.MED_CYCLE_SEC), n.CYCLE_START))" in _BODY
    # described honestly: a new heuristic sharing the constants, not parity with the Theil-Sen trend
    assert "new SQL heuristic sharing SLA_FORECAST_MIN_RUNS /" in _HEADER
    assert "SLA_FORECAST_FIT_NIGHTS" in _HEADER and "mirrors ETL_RECON_RESULTS" not in _MIG


def test_v156_terminal_counts_only_from_its_terminal_attempt():
    _once("JOIN starts s ON s.CYCLE_DATE = t.CYCLE_DATE "
          "AND (t.FIRST_OK_END IS NOT NULL OR t.TERMINAL_START >= s.CYCLE_START)")
    assert "t.FIRST_START >= s.CYCLE_START" not in _BODY
    # a task done at its first clean in-cycle finish: finish from FIRST_OK_END, never counted failed / running
    _once("MAX(COALESCE(t.FIRST_OK_END, t.TERMINAL_END)) AS CYCLE_FINISH")
    _once(f"COUNT_IF(t.FIRST_OK_END IS NULL AND UPPER(t.TERMINAL_STATUS) IN ({_FAILED_SQL})) AS N_FAILED")
    _once("COUNT_IF(t.FIRST_OK_END IS NULL AND t.TERMINAL_END IS NULL AND (t.TERMINAL_STATUS IS NULL "
          f"OR UPPER(t.TERMINAL_STATUS) NOT IN ({_FAILED_SQL}))) AS N_RUNNING")
    # FIRST_OK_END counts only an attempt that STARTED at/after the night's LAST kickoff (MAX starter start),
    # not a naive MIN and not an end bound: an attempt started before the real kickoff never counts, even one
    # still running across it (review round 2: a 21:30-22:10 afternoon terminal vs a 22:00 kickoff)
    rendered = _render_ins_sql()
    assert rendered.count("cs AS (SELECT CD AS CS_NIGHT, "
                          "MAX(TASK_START_DTTM) AS CYC_LAST_START FROM src WHERE WF = ? GROUP BY CD)") == 1
    assert rendered.count("AND TASK_START_DTTM >= CYC_LAST_START, TASK_END_DTTM, NULL))::TIMESTAMP_NTZ") == 1
    assert "TASK_END_DTTM >= CYC_LAST_START" not in rendered and "CYC_START" not in rendered.replace(
        "CYC_LAST_START", "")
    # CYCLE_START keeps the app's MIN(starter start) (parity; the afternoon double re-run edge is documented)
    assert "SELECT CYCLE_DATE, MIN(FIRST_START) AS CYCLE_START\n" in _BODY
    assert "MIN(TASK_START_DTTM) AS CYCLE_START" in etl.cycle_finish_history_scan(
        "DB.S.T", start_workflow="A", end_workflow="B")
    hdr = _header_text()
    assert "CYCLE_START = MIN(starter start) is the afternoon run" in hdr
    assert ("clean finish after the night's last kickoff (FIRST_OK_END: the attempt must START at/after it), so a "
            "re-run of it after the night finished") in hdr
    # the known edges say what the round-2 bound leaves: loud, never silent, including starter = terminal
    for edge in ("FIRST_OK_END must START at/after the night's LAST starter start",
                 "a next-morning re-run of the STARTER", "a starter re-run alone stays quiet",
                 "When ETL_CYCLE_START_WORKFLOW = ETL_CYCLE_END_WORKFLOW every task is a starter task",
                 "Both fail LOUD, by choice",
                 "once the real kickoff runs, an afternoon attempt that started before it never counts as "
                 "FIRST_OK_END",
                 "SILENT residual 1: a real cycle that hangs before its terminal dispatches stays quiet",
                 "SILENT residual 2: an afternoon-chain terminal attempt that STARTS after the real kickoff"):
        assert edge in hdr, edge
    for path in (f"snowflake/migrations/{_NAME}", "CHANGELOG.md", "README.md", "RUNBOOK.md"):
        text = _read(path)
        for claim in ("never hides the real", "never hides a real", "the one SILENT residual"):
            assert claim not in text, (path, claim)     # two silent residuals exist: no doc may promise otherwise
    assert "SILENT residual 1:" in hdr and "SILENT residual 2:" in hdr
    assert "LATE reads the night complete all night" not in hdr               # the round-1 edge is gone


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


def test_v156_late_band_order_parsed_from_the_sql():
    """Structural: the [C] graded CASE, parsed by sqlglot, tests EXH (vs DL_H) before CRIT (vs DL_T) before the
    unfinished-only WARN -- the order the _late model applies."""
    sqlglot = pytest.importorskip("sqlglot")
    from sqlglot import exp
    late = _raise_statements()[2]
    tree = sqlglot.parse_one(_bind(late), read="snowflake")
    (graded,) = [c for c in tree.find_all(exp.CTE) if c.alias == "graded"]
    (case,) = list(graded.this.find_all(exp.Case))
    ifs = case.args["ifs"]
    assert [i.args["true"].name for i in ifs] == ["EXH", "CRIT", "WARN"]
    assert [i.this.expression.sql() for i in ifs[:2]] == ["t.DL_H", "t.DL_T"]
    assert all(isinstance(i.this, exp.GT) for i in ifs[:2])
    warn = ifs[2].this
    assert isinstance(warn, exp.And) and warn.this.sql() == "NOT t.IS_COMPLETE"
    assert case.args.get("default") is None


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
    # 3 LEFT(:emsg, 2000) handlers + the misconfig log + the 3 rule DETAILs; 3 rule TITLEs
    assert _BODY.count(", 2000),") == 7 and _BODY.count(", 300),") == 3
    assert _BODY.count("LEFT(:emsg, 2000)") == 3
    for stmt in _raise_statements():
        assert stmt.count(", 300),") == 1 and stmt.count(", 2000),") == 1, stmt[:120]


def test_v156_correlated_subqueries_only_at_top_level():
    """Snowflake 002031: no nested scalar subquery. Every '(SELECT' opens a CTE, an EXISTS, an IN list or a
    derived table -- never a scalar in a select list or a comparison."""
    opened = 0
    for m in re.finditer(r"\(\s*SELECT\b", _BODY):
        before = _BODY[max(0, m.start() - 12):m.start()]
        assert re.search(r"\b(?:EXISTS|IN|FROM|AS)\s*$", before), _BODY[m.start() - 60:m.start() + 40]
        opened += 1
    assert opened >= 15


_A_AUTO_CLEAR = f"""
    UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
       SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'AUTO_CLEARED'
     WHERE RULE_ID = 'PIPE_ETL_TASK_FAILED'
       AND STATUS = 'OPEN'
       AND DEDUPE_KEY IN (
           SELECT 'PIPE_ETL_TASK_FAILED|' || LEFT(t.WORKFLOW_NAME, 200) || '|' || TO_VARCHAR(t.CYCLE_DATE)
           FROM DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS t
           GROUP BY t.WORKFLOW_NAME, t.CYCLE_DATE
           HAVING COUNT_IF(UPPER(t.TERMINAL_STATUS) IN ({_FAILED_SQL})) = 0
              AND COUNT_IF(t.TERMINAL_END IS NULL) = 0
       );"""


def test_v156_task_failed_threshold_floor_and_retry_auto_clear():
    assert "JOIN wf w ON w.FAILED_TASK_COUNT >= GREATEST(c.MIN_FAILED, 1)" in _BODY
    assert "w.FAILED_TASK_COUNT >= c.MIN_FAILED\n" not in _BODY
    assert "IFF(w.WORKFLOW_NAME = :end_wf AND c.SEVERITY IN ('LOW', 'MEDIUM'), 'HIGH', c.SEVERITY)" in _BODY
    assert "COALESCE(e.RESOLUTION_KIND, '') <> 'AUTO_CLEARED'" in _BODY      # a re-failure re-alerts
    # tonight's anchor: the NEWEST starter night (MAX), else the newest night; failures from it forward only
    _once("anchor AS (SELECT COALESCE(MAX(IFF(WORKFLOW_NAME = :start_wf, CYCLE_DATE, NULL)), MAX(CYCLE_DATE)) "
          "AS CYCLE_DATE FROM DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS)")
    _once("JOIN anchor a ON t.CYCLE_DATE >= a.CYCLE_DATE")
    # the whole auto-clear: OPEN-only, zero final-attempt failures AND every final attempt FINISHED
    _once(_A_AUTO_CLEAR)
    assert _norm(_auto_clear_statement()) + ";" == _norm(_A_AUTO_CLEAR)       # the statement is exactly that
    # key parity: the auto-clear rebuilds EXACTLY the key the raise wrote (w. -> t.)
    raise_a = _raise_statements()[0]
    (ins_key,) = re.findall(r"'PIPE_ETL_TASK_FAILED\|' \|\| [^\n]*", raise_a)
    (upd_key,) = re.findall(r"'PIPE_ETL_TASK_FAILED\|' \|\| [^\n]*", _auto_clear_statement())
    assert ins_key.replace("w.", "t.") == upd_key
    assert _BODY.index("n_failed := SQLROWCOUNT;") < _BODY.index("UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS")
    # header told the truth about TASK_FAILED on a no-starter night (spec correction)
    assert ("No starter run = no LATE for that night; TASK_FAILED still reports any workflow failure keyed "
            "after the\n--     last cycle start") in _HEADER
    assert "a retry still running keeps the event OPEN" in _HEADER


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
    # the hung-step triage query counts 'still running' the way [C] does (a done task's re-run is not hung)
    assert "AND TERMINAL_END IS NULL AND FIRST_OK_END IS NULL ORDER BY" in late
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
    a fragment that is not literally in the proc body would read FALSE on a correct apply. Quote-free, so
    GET_DDL's re-quoting of the body can never change the match (the house grid precedent)."""
    for frag in ("t.TERMINAL_START >= s.CYCLE_START", "GREATEST(c.MIN_FAILED, 1)", "CROSS JOIN term_lw tl",
                 "OR g.IS_COMPLETE, g.SEVERITY", "NOT COALESCE(g.PROJECTED_FINISH > g.DL_H, FALSE)",
                 "MAX(COALESCE(t.FIRST_OK_END, t.TERMINAL_END))", "COUNT_IF(t.TERMINAL_END IS NULL) = 0",
                 "USING (start_wf)", "MAX(TASK_START_DTTM) AS CYC_LAST_START",
                 "TASK_START_DTTM >= CYC_LAST_START"):
        assert "'" not in frag and _BODY.count(frag) == 1, frag


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
# 22. the three rule INSERTs pinned WHOLE (review round 2). A fragment lock stops at the last clause it names,
#     so a filter APPENDED after it survived (e.g. 'WHERE e.CYCLE_DATE IS NOT NULL' after the nights LEFT JOIN
#     turns it back into the inner join that drops a hung night). Each statement is split into its CTEs, each
#     compared to its OWN closing paren, then the INSERT head and the outer select: the whole statement.
def _with_parts(stmt: str) -> tuple[str, list[tuple[str, str]], str]:
    """Normalized 'INSERT ... WITH a AS (...), b AS (...) SELECT ...' -> (head, [(cte, 'cte AS (...)')], tail),
    each CTE cut at ITS matching ')' (quote-aware: a '(' or ')' inside a literal never counts)."""
    s = _norm(stmt)
    head, rest = s.split(" WITH ", 1)
    ctes: list[tuple[str, str]] = []
    while True:
        m = re.match(r"(\w+) AS \(", rest)
        assert m, rest[:80]
        depth, quoted, i = 0, False, m.end() - 1
        while True:
            c = rest[i]
            if c == "'":
                quoted = not quoted
            elif not quoted and c == "(":
                depth += 1
            elif not quoted and c == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        ctes.append((m.group(1), rest[:i + 1]))
        rest = rest[i + 1:]
        if not rest.startswith(", "):
            return head, ctes, rest.strip()
        rest = rest[2:]


# [A] PIPE_ETL_TASK_FAILED raise: the reviewed statement, comments stripped (compared whitespace-normalized)
_A_RAISE = f"""
INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
    (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
WITH cfg AS (
    SELECT RULE_ID, SEVERITY, COALESCE(THRESHOLD_NUM, 1) AS MIN_FAILED
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
    WHERE RULE_ID = 'PIPE_ETL_TASK_FAILED' AND ENABLED
),
anchor AS (
    SELECT COALESCE(MAX(IFF(WORKFLOW_NAME = :start_wf, CYCLE_DATE, NULL)), MAX(CYCLE_DATE)) AS CYCLE_DATE
    FROM DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS
),
wf AS (
    SELECT t.CYCLE_DATE, t.WORKFLOW_NAME,
           COUNT(*) AS TASK_COUNT,
           COUNT_IF(UPPER(t.TERMINAL_STATUS) IN ({_FAILED_SQL})) AS FAILED_TASK_COUNT,
           LISTAGG(IFF(UPPER(t.TERMINAL_STATUS) IN ({_FAILED_SQL}),
                       t.TASK_NAME || ' (' || t.TERMINAL_STATUS || ')', NULL), ', ')
               WITHIN GROUP (ORDER BY t.TASK_NAME) AS FAILED_TASKS
    FROM DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS t
    JOIN anchor a ON t.CYCLE_DATE >= a.CYCLE_DATE
    GROUP BY t.CYCLE_DATE, t.WORKFLOW_NAME
)
SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
FROM (
SELECT c.RULE_ID, 'ALL',
       IFF(w.WORKFLOW_NAME = :end_wf AND c.SEVERITY IN ('LOW', 'MEDIUM'), 'HIGH', c.SEVERITY),
       LEFT(w.WORKFLOW_NAME || ': ' || w.FAILED_TASK_COUNT || ' of ' || w.TASK_COUNT
            || ' task(s) failed in the ' || TO_VARCHAR(w.CYCLE_DATE) || ' nightly cycle', 300),
       LEFT('Final attempt failed (Informatica retries collapse to the last attempt): '
            || LEFT(w.FAILED_TASKS, 1500) || '. '
            || IFF(w.WORKFLOW_NAME = :end_wf,
                   'This is the cycle TERMINAL workflow - the cycle cannot finish until it is re-run. ', '')
            || 'Informatica may also have notified. Triage: Operations > Pipeline SLA > Tonight at a glance. '
            || 'Auto-clears if a retry succeeds.', 2000),
       w.FAILED_TASK_COUNT,
       'PIPE_ETL_TASK_FAILED|' || LEFT(w.WORKFLOW_NAME, 200) || '|' || TO_VARCHAR(w.CYCLE_DATE)
FROM cfg c
JOIN wf w ON w.FAILED_TASK_COUNT >= GREATEST(c.MIN_FAILED, 1)
) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
WHERE NOT EXISTS (
    SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
    WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
      AND COALESCE(e.RESOLUTION_KIND, '') <> 'AUTO_CLEARED'
)"""

# [B] PIPE_ETL_CYCLE_NOT_STARTED raise: the reviewed statement, comments stripped (compared whitespace-normalized)
_B_RAISE = """
INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
    (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
WITH cfg AS (
    SELECT RULE_ID, SEVERITY, GREATEST(ROUND(COALESCE(THRESHOLD_NUM, 120)), 0) AS GRACE_MIN
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
    WHERE RULE_ID = 'PIPE_ETL_CYCLE_NOT_STARTED' AND ENABLED
),
cyc AS (
    SELECT CYCLE_DATE, MIN(FIRST_START) AS CYCLE_START_AT
    FROM DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS
    WHERE WORKFLOW_NAME = :start_wf
    GROUP BY CYCLE_DATE
),
anchor AS (
    SELECT CYCLE_DATE, CYCLE_START_AT
    FROM cyc
    QUALIFY ROW_NUMBER() OVER (ORDER BY CYCLE_DATE DESC) = 1
),
missed AS (
    SELECT c.RULE_ID, c.SEVERITY, c.GRACE_MIN, a.CYCLE_START_AT AS LAST_START,
           MAX(IFF(DATEADD('minute', 7 * 1440 + c.GRACE_MIN, p.CYCLE_START_AT) < :now_ct,
                   p.CYCLE_DATE, NULL)) AS REF_NIGHT,
           MAX(IFF(DATEADD('minute', 7 * 1440 + c.GRACE_MIN, p.CYCLE_START_AT) < :now_ct,
                   p.CYCLE_START_AT, NULL)) AS REF_START
    FROM cfg c
    CROSS JOIN anchor a
    JOIN cyc p
      ON p.CYCLE_DATE BETWEEN DATEADD('day', -6, a.CYCLE_DATE)
                          AND DATEADD('day', -7, DATE(DATEADD('hour', -12, :now_ct)))
    GROUP BY c.RULE_ID, c.SEVERITY, c.GRACE_MIN, a.CYCLE_START_AT
)
SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
FROM (
SELECT m.RULE_ID, 'ALL', m.SEVERITY,
       LEFT('Nightly ETL cycle did not start: ' || :start_wf || ' has no run for the '
            || TO_VARCHAR(DATEADD('day', 7, m.REF_NIGHT)) || ' night', 300),
       LEFT('It kicked off at ' || TO_VARCHAR(m.REF_START, 'HH24:MI') || ' on the same night last week; '
            || 'now ' || FLOOR(DATEDIFF('minute', DATEADD('day', 7, m.REF_START), :now_ct) / 60) || 'h '
            || MOD(DATEDIFF('minute', DATEADD('day', 7, m.REF_START), :now_ct), 60) || 'm past that (grace '
            || m.GRACE_MIN || ' min). Last cycle start ' || TO_VARCHAR(m.LAST_START, 'YYYY-MM-DD HH24:MI')
            || ' (Central). Nothing downstream loads until the starter runs - check the Informatica '
            || 'scheduler / integration service. A planned no-run night (holiday, freeze) = resolve as '
            || 'EXPECTED. Operations > Pipeline SLA > Tonight at a glance shows the same Cycle start: Overdue.', 2000),
       DATEDIFF('minute', DATEADD('day', 7, m.REF_START), :now_ct),
       'PIPE_ETL_CYCLE_NOT_STARTED|' || TO_VARCHAR(DATEADD('day', 7, m.REF_NIGHT))
FROM missed m
WHERE m.REF_NIGHT IS NOT NULL
  AND DATEDIFF('second', m.LAST_START, :now_ct) > 86400 + m.GRACE_MIN * 60
) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
WHERE NOT EXISTS (
    SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
    WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
)"""

# [C] PIPE_ETL_CYCLE_LATE raise (every clause the _late model encodes): the reviewed statement, comments stripped (compared whitespace-normalized)
_C_RAISE = f"""
INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
    (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
WITH cfg AS (
    SELECT RULE_ID, SEVERITY, GREATEST(ROUND(COALESCE(THRESHOLD_NUM, 60)), 0) AS LEAD_MIN
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
    WHERE RULE_ID = 'PIPE_ETL_CYCLE_LATE' AND ENABLED
),
starts AS (
    SELECT CYCLE_DATE, MIN(FIRST_START) AS CYCLE_START
    FROM DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS
    WHERE WORKFLOW_NAME = :start_wf
    GROUP BY CYCLE_DATE
),
ends AS (
    SELECT t.CYCLE_DATE,
           COUNT(*) AS TERM_TASKS,
           MAX(COALESCE(t.FIRST_OK_END, t.TERMINAL_END)) AS CYCLE_FINISH,
           COUNT_IF(t.FIRST_OK_END IS NULL
                    AND UPPER(t.TERMINAL_STATUS) IN ({_FAILED_SQL})) AS N_FAILED,
           COUNT_IF(t.FIRST_OK_END IS NULL
                    AND t.TERMINAL_END IS NULL
                    AND (t.TERMINAL_STATUS IS NULL
                         OR UPPER(t.TERMINAL_STATUS) NOT IN ({_FAILED_SQL}))) AS N_RUNNING
    FROM DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS t
    JOIN starts s ON s.CYCLE_DATE = t.CYCLE_DATE
                 AND (t.FIRST_OK_END IS NOT NULL OR t.TERMINAL_START >= s.CYCLE_START)
    WHERE t.WORKFLOW_NAME = :end_wf
    GROUP BY t.CYCLE_DATE
),
nights AS (
    SELECT s.CYCLE_DATE, s.CYCLE_START, e.CYCLE_FINISH,
           COALESCE(e.TERM_TASKS, 0) AS TERM_TASKS,
           COALESCE(e.N_FAILED, 0) AS N_FAILED,
           COALESCE(e.N_RUNNING, 0) AS N_RUNNING,
           IFF(DATEADD('minute', :target_off, DATE_TRUNC('day', s.CYCLE_START)) > s.CYCLE_START,
               DATEADD('minute', :target_off, DATE_TRUNC('day', s.CYCLE_START)),
               DATEADD('minute', :target_off + 1440, DATE_TRUNC('day', s.CYCLE_START))) AS DL_T
    FROM starts s
    LEFT JOIN ends e ON e.CYCLE_DATE = s.CYCLE_DATE
),
latest AS (
    SELECT MAX(CYCLE_DATE) AS CYCLE_DATE FROM nights
),
term_lw AS (
    SELECT COUNT(*) AS N
    FROM nights n
    JOIN latest l ON n.CYCLE_DATE = DATEADD('day', -7, l.CYCLE_DATE)
    WHERE n.TERM_TASKS > 0
),
hist AS (
    SELECT MEDIAN(DATEDIFF('second', n.CYCLE_START, n.CYCLE_FINISH)) AS MED_CYCLE_SEC,
           MIN(n.TERM_TASKS) AS MIN_TERM_TASKS,
           COUNT(*) AS N_HIST
    FROM (
        SELECT n.*
        FROM nights n
        JOIN latest l ON n.CYCLE_DATE < l.CYCLE_DATE
        WHERE n.CYCLE_FINISH IS NOT NULL AND n.N_FAILED = 0 AND n.N_RUNNING = 0
        QUALIFY ROW_NUMBER() OVER (ORDER BY n.CYCLE_DATE DESC) <= 14
    ) n
),
tonight AS (
    SELECT n.CYCLE_DATE, n.CYCLE_START, n.CYCLE_FINISH, n.TERM_TASKS, n.N_FAILED, n.N_RUNNING,
           n.DL_T,
           DATEADD('minute', :breach_off - :target_off, n.DL_T) AS DL_H,
           h.MED_CYCLE_SEC, h.N_HIST,
           (n.CYCLE_FINISH IS NOT NULL AND n.N_FAILED = 0 AND n.N_RUNNING = 0
            AND n.TERM_TASKS >= COALESCE(h.MIN_TERM_TASKS, 1)) AS IS_COMPLETE,
           IFF(h.N_HIST >= 4,
               GREATEST(:now_ct, DATEADD('second', ROUND(h.MED_CYCLE_SEC), n.CYCLE_START)),
               NULL) AS PROJECTED_FINISH
    FROM nights n
    JOIN latest l ON n.CYCLE_DATE = l.CYCLE_DATE
    CROSS JOIN hist h
),
graded AS (
    SELECT t.CYCLE_DATE, t.CYCLE_START, t.CYCLE_FINISH, t.TERM_TASKS, t.N_FAILED, t.N_RUNNING,
           t.DL_T, t.DL_H, t.MED_CYCLE_SEC, t.N_HIST, t.IS_COMPLETE, t.PROJECTED_FINISH,
           c.RULE_ID, c.SEVERITY,
           CASE
               WHEN IFF(t.IS_COMPLETE, t.CYCLE_FINISH, :now_ct) > t.DL_H THEN 'EXH'
               WHEN IFF(t.IS_COMPLETE, t.CYCLE_FINISH, :now_ct) > t.DL_T THEN 'CRIT'
               WHEN NOT t.IS_COMPLETE
                    AND (:now_ct >= DATEADD('minute', -c.LEAD_MIN, t.DL_T)
                         OR t.PROJECTED_FINISH > t.DL_H) THEN 'WARN'
           END AS BAND
    FROM tonight t
    CROSS JOIN cfg c
    CROSS JOIN term_lw tl
    WHERE :now_ct < DATEADD('hour', 12, t.DL_H)
      AND NOT (t.TERM_TASKS = 0 AND COALESCE(tl.N, 0) = 0)
)
SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
FROM (
SELECT g.RULE_ID, 'ALL',
       IFF(g.BAND = 'WARN' OR g.IS_COMPLETE, g.SEVERITY, 'CRITICAL'),
       LEFT(CASE g.BAND
                WHEN 'EXH' THEN IFF(g.IS_COMPLETE,
                    'Nightly ETL cycle finished ' || TO_VARCHAR(g.CYCLE_FINISH, 'HH24:MI')
                        || ', past the ' || TO_VARCHAR(g.DL_H, 'HH24:MI') || ' hard deadline',
                    'Nightly ETL cycle past the ' || TO_VARCHAR(g.DL_H, 'HH24:MI')
                        || ' hard deadline and still not finished')
                WHEN 'CRIT' THEN IFF(g.IS_COMPLETE,
                    'Nightly ETL cycle finished ' || TO_VARCHAR(g.CYCLE_FINISH, 'HH24:MI')
                        || ', after the ' || TO_VARCHAR(g.DL_T, 'HH24:MI') || ' target',
                    'Nightly ETL cycle missed the ' || TO_VARCHAR(g.DL_T, 'HH24:MI')
                        || ' target and is still not finished')
                ELSE IFF(g.PROJECTED_FINISH > g.DL_H,
                    'Nightly ETL cycle projected to finish ~' || TO_VARCHAR(g.PROJECTED_FINISH, 'HH24:MI')
                        || ', past the ' || TO_VARCHAR(g.DL_H, 'HH24:MI') || ' hard deadline',
                    'Nightly ETL cycle not finished ' || DATEDIFF('minute', :now_ct, g.DL_T)
                        || ' min before the ' || TO_VARCHAR(g.DL_T, 'HH24:MI') || ' target')
            END || ' (' || TO_VARCHAR(g.CYCLE_DATE) || ' night)', 300),
       LEFT('Cycle started ' || TO_VARCHAR(g.CYCLE_START, 'YYYY-MM-DD HH24:MI') || ' (' || :start_wf
            || '). Terminal ' || :end_wf || ': '
            || CASE WHEN g.TERM_TASKS = 0 THEN 'not dispatched yet'
                    WHEN g.N_FAILED > 0 THEN g.N_FAILED || ' task(s) FAILED - re-run it'
                    WHEN g.N_RUNNING > 0 THEN g.N_RUNNING || ' task(s) still running'
                    WHEN g.IS_COMPLETE THEN 'finished ' || TO_VARCHAR(g.CYCLE_FINISH, 'YYYY-MM-DD HH24:MI')
                    ELSE 'only ' || g.TERM_TASKS || ' task(s) dispatched so far' END
            || '. Typical cycle '
            || COALESCE(FLOOR(g.MED_CYCLE_SEC / 3600) || 'h ' || MOD(FLOOR(g.MED_CYCLE_SEC / 60), 60)
                        || 'm over ' || g.N_HIST || ' clean night(s)', 'unknown (short history)')
            || '. Target ' || TO_VARCHAR(g.DL_T, 'HH24:MI') || ', hard deadline '
            || TO_VARCHAR(g.DL_H, 'HH24:MI') || ' (Central). Operations > Pipeline SLA > Tonight: '
            || 'SLA finish forecast + Tonight at a glance.', 2000),
       IFF(g.BAND = 'WARN' AND NOT COALESCE(g.PROJECTED_FINISH > g.DL_H, FALSE), DATEDIFF('minute', :now_ct, g.DL_T), NULL),
       'PIPE_ETL_CYCLE_LATE|' || g.BAND || '|' || TO_VARCHAR(g.CYCLE_DATE)
FROM graded g
WHERE g.BAND IS NOT NULL
) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
WHERE NOT EXISTS (
    SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
    WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
)"""

_RAISE_GOLDENS = dict(zip(_RULES, (_A_RAISE, _B_RAISE, _C_RAISE), strict=True))     # body order [A], [B], [C]


@pytest.mark.parametrize("rule", _RULES)
def test_v156_rule_inserts_are_pinned_whole(rule):
    stmt = _raise_statements()[_RULES.index(rule)]
    assert f"'{rule}|' ||" in stmt, "the statement for this rule (its dedupe key)"
    head, ctes, tail = _with_parts(stmt)
    want_head, want_ctes, want_tail = _with_parts(_RAISE_GOLDENS[rule])
    assert [n for n, _ in ctes] == [n for n, _ in want_ctes], rule
    for (name, got), (_, want) in zip(ctes, want_ctes, strict=True):
        assert got == want, f"{rule}: CTE {name} changed (compared whole, to its closing paren)"
    assert head == want_head, rule
    assert tail == want_tail, f"{rule}: the outer select changed"
    assert _norm(stmt) == _norm(_RAISE_GOLDENS[rule])


# ---------------------------------------------------------------------------------------------------
# Python models of the proc's collapse (ins_sql, pinned by the _INS_SQL golden), the [A] raise / auto-clear,
# the [B] Overdue test and the [C] grading. Every [C] clause the _late model encodes is pinned below as EXACT
# normalized text occurring once, so the model cannot pass while the SQL drifts: the CASE order, the deadline
# math, the terminal-workflow filter, the prior-night history join and the IS_COMPLETE expression.
_MODEL_FRAGMENTS = (
    # [C] starts / ends: the cycle start, the terminal filter, FIRST_OK_END and the retry-collapsed health
    "starts AS (SELECT CYCLE_DATE, MIN(FIRST_START) AS CYCLE_START FROM DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS "
    "WHERE WORKFLOW_NAME = :start_wf GROUP BY CYCLE_DATE)",
    "ends AS (SELECT t.CYCLE_DATE, COUNT(*) AS TERM_TASKS, "
    "MAX(COALESCE(t.FIRST_OK_END, t.TERMINAL_END)) AS CYCLE_FINISH, "
    f"COUNT_IF(t.FIRST_OK_END IS NULL AND UPPER(t.TERMINAL_STATUS) IN ({_FAILED_SQL})) AS N_FAILED, "
    "COUNT_IF(t.FIRST_OK_END IS NULL AND t.TERMINAL_END IS NULL AND (t.TERMINAL_STATUS IS NULL "
    f"OR UPPER(t.TERMINAL_STATUS) NOT IN ({_FAILED_SQL}))) AS N_RUNNING "
    "FROM DBA_MAINT_DB.OVERWATCH.ETL_CYCLE_TASKS t "
    "JOIN starts s ON s.CYCLE_DATE = t.CYCLE_DATE AND (t.FIRST_OK_END IS NOT NULL OR t.TERMINAL_START >= s.CYCLE_START) "
    "WHERE t.WORKFLOW_NAME = :end_wf GROUP BY t.CYCLE_DATE)",
    "t.TERMINAL_START >= s.CYCLE_START",
    # nights: the LEFT JOIN that keeps a hung (terminal-never-dispatched) night, and the deadline math
    "LEFT JOIN ends e ON e.CYCLE_DATE = s.CYCLE_DATE",
    "IFF(DATEADD('minute', :target_off, DATE_TRUNC('day', s.CYCLE_START)) > s.CYCLE_START,",
    "IFF(DATEADD('minute', :target_off, DATE_TRUNC('day', s.CYCLE_START)) > s.CYCLE_START, "
    "DATEADD('minute', :target_off, DATE_TRUNC('day', s.CYCLE_START)), "
    "DATEADD('minute', :target_off + 1440, DATE_TRUNC('day', s.CYCLE_START))) AS DL_T",
    "nights AS (SELECT s.CYCLE_DATE, s.CYCLE_START, e.CYCLE_FINISH, COALESCE(e.TERM_TASKS, 0) AS TERM_TASKS, "
    "COALESCE(e.N_FAILED, 0) AS N_FAILED, COALESCE(e.N_RUNNING, 0) AS N_RUNNING, ",
    "DATEADD('minute', :breach_off - :target_off, n.DL_T) AS DL_H",
    # tonight = the newest starter night; history = the newest 14 PRIOR clean nights
    "latest AS (SELECT MAX(CYCLE_DATE) AS CYCLE_DATE FROM nights)",
    "SELECT MAX(CYCLE_DATE) AS CYCLE_DATE FROM nights",
    "JOIN latest l ON n.CYCLE_DATE < l.CYCLE_DATE",
    "SELECT n.* FROM nights n JOIN latest l ON n.CYCLE_DATE < l.CYCLE_DATE "
    "WHERE n.CYCLE_FINISH IS NOT NULL AND n.N_FAILED = 0 AND n.N_RUNNING = 0 "
    "QUALIFY ROW_NUMBER() OVER (ORDER BY n.CYCLE_DATE DESC) <= 14",
    "QUALIFY ROW_NUMBER() OVER (ORDER BY n.CYCLE_DATE DESC) <= 14",
    "hist AS (SELECT MEDIAN(DATEDIFF('second', n.CYCLE_START, n.CYCLE_FINISH)) AS MED_CYCLE_SEC, "
    "MIN(n.TERM_TASKS) AS MIN_TERM_TASKS, COUNT(*) AS N_HIST FROM (",
    "(n.CYCLE_FINISH IS NOT NULL AND n.N_FAILED = 0 AND n.N_RUNNING = 0 "
    "AND n.TERM_TASKS >= COALESCE(h.MIN_TERM_TASKS, 1)) AS IS_COMPLETE",
    "IFF(h.N_HIST >= 4, GREATEST(:now_ct, DATEADD('second', ROUND(h.MED_CYCLE_SEC), n.CYCLE_START)), NULL) "
    "AS PROJECTED_FINISH FROM nights n JOIN latest l ON n.CYCLE_DATE = l.CYCLE_DATE CROSS JOIN hist h",
    # graded: the whole CASE, in order, and the window / regular-terminal filter
    "CASE WHEN IFF(t.IS_COMPLETE, t.CYCLE_FINISH, :now_ct) > t.DL_H THEN 'EXH' "
    "WHEN IFF(t.IS_COMPLETE, t.CYCLE_FINISH, :now_ct) > t.DL_T THEN 'CRIT' "
    "WHEN NOT t.IS_COMPLETE AND (:now_ct >= DATEADD('minute', -c.LEAD_MIN, t.DL_T) "
    "OR t.PROJECTED_FINISH > t.DL_H) THEN 'WARN' END AS BAND",
    "OR t.PROJECTED_FINISH > t.DL_H)",
    "WHERE :now_ct < DATEADD('hour', 12, t.DL_H) AND NOT (t.TERM_TASKS = 0 AND COALESCE(tl.N, 0) = 0))",
    "IFF(g.BAND = 'WARN' OR g.IS_COMPLETE, g.SEVERITY, 'CRITICAL')",
    "IFF(g.BAND = 'WARN' AND NOT COALESCE(g.PROJECTED_FINISH > g.DL_H, FALSE), DATEDIFF('minute', :now_ct, g.DL_T), "
    "NULL)",
    "FROM graded g WHERE g.BAND IS NOT NULL",
)
_FAILED = frozenset(etl.FAILED_TASK_STATUSES)
_S, _T = "WF_START", "WF_TERM"


def _status(t) -> str:
    return str(t["TERMINAL_STATUS"] or "").upper()


def _in_cache(rows, now, *, whole_nights: bool = True):
    """ins_sql WHERE over (workflow, task, status, start, end) attempts: the one-day-wider timestamp prefilter
    AND the night-key cut (the newest lookback_days nights). whole_nights=False = the pre-fix timestamp cut."""
    lb = _lookback()
    if not whole_nights:
        return [r for r in rows if r[3] >= now - timedelta(days=lb)]
    key_now = (now - timedelta(hours=12)).date()
    return [r for r in rows if r[3] >= now - timedelta(days=lb + 1)
            and (r[3] - timedelta(hours=12)).date() > key_now - timedelta(days=lb)]


def _as_of(rows, now):
    """What CONTROL_STATUS holds at `now`: an attempt not yet started is absent; one still in flight has no end
    and a RUNNING status."""
    out = []
    for wf, task, status, start, end in rows:
        if start > now:
            continue
        if end is not None and end > now:
            status, end = "RUNNING", None
        out.append((wf, task, status, start, end))
    return out


def _collapse(rows, *, bound: str = "kickoff", start_wf: str = _S):
    """ins_sql: attempts -> one row per (night, workflow, task). FIRST_OK_END = the earliest clean (ended, not a
    failed status; NULL status reads clean) finish among the attempts that STARTED at/after the night's LAST
    kickoff (cs.CYC_LAST_START = MAX starter start); no starter run that night -> NULL (a NULL bound never
    compares true). Superseded bounds, kept to prove each review round's case: bound="end" = round 2 (started
    at/after the FIRST kickoff and ENDED at/after the last), bound="first" = round 1 (the first-kickoff bound
    alone), bound="none" = the naive unbounded MIN. start_wf is the bound starter name (the '?' of cs)."""
    assert bound in ("kickoff", "end", "first", "none")
    groups: dict = {}
    for wf, task, status, start, end in rows:
        groups.setdefault(((start - timedelta(hours=12)).date(), wf, task), []).append((status, start, end))
    cyc_first: dict = {}
    cyc_last: dict = {}
    for (night, wf, _task), att in groups.items():
        if wf == start_wf:
            lo, hi = min(a[1] for a in att), max(a[1] for a in att)
            cyc_first[night] = min(cyc_first.get(night, lo), lo)
            cyc_last[night] = max(cyc_last.get(night, hi), hi)
    out = []
    for (night, wf, task), att in groups.items():
        term = max(att, key=lambda a: a[2] or a[1])          # MAX_BY(.., COALESCE(end, start))
        cs, cl = cyc_first.get(night), cyc_last.get(night)
        if bound == "kickoff":
            fits = [cl is not None and a[1] >= cl for a in att]
        elif bound == "end":
            fits = [cs is not None and a[1] >= cs and a[2] is not None and a[2] >= cl for a in att]
        elif bound == "first":
            fits = [cs is not None and a[1] >= cs for a in att]
        else:
            fits = [True for _a in att]
        ok = [a[2] for a, fit in zip(att, fits, strict=True)
              if fit and a[2] is not None and str(a[0] or "").upper() not in _FAILED]
        out.append({"CYCLE_DATE": night, "WORKFLOW_NAME": wf, "TASK_NAME": task, "TERMINAL_STATUS": term[0],
                    "FIRST_START": min(a[1] for a in att), "TERMINAL_START": term[1], "TERMINAL_END": term[2],
                    "FIRST_OK_END": min(ok, default=None)})
    return out


def _late(tasks, now, *, filter_col="TERMINAL_START", first_ok=True, lead=60, target_off=420, breach_off=480,
          sev="HIGH", start_wf=_S, end_wf=_T):
    """[C] PIPE_ETL_CYCLE_LATE over the cache rows -> {band, severity, metric, ...} or None. first_ok=False (with
    filter_col) models the SQL before the FIRST_OK_END fix; start_wf / end_wf are :start_wf / :end_wf."""
    starts: dict = {}
    for t in tasks:
        if t["WORKFLOW_NAME"] == start_wf:
            d = t["CYCLE_DATE"]
            starts[d] = min(starts.get(d, t["FIRST_START"]), t["FIRST_START"])
    nights = {}
    for d, cs in starts.items():
        def done(t):
            return first_ok and t["FIRST_OK_END"] is not None
        term = [t for t in tasks if t["WORKFLOW_NAME"] == end_wf and t["CYCLE_DATE"] == d
                and (done(t) or t[filter_col] >= cs)]
        live = [t for t in term if not done(t)]
        failed = sum(1 for t in live if _status(t) in _FAILED)
        running = sum(1 for t in live if t["TERMINAL_END"] is None and _status(t) not in _FAILED)
        ends = [t["FIRST_OK_END"] if done(t) else t["TERMINAL_END"] for t in term]
        dl_t = datetime.combine(cs.date(), time()) + timedelta(minutes=target_off)
        if not dl_t > cs:
            dl_t += timedelta(days=1)
        nights[d] = {"start": cs, "finish": max((e for e in ends if e), default=None),
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


def _key_a(wf: str, night: date) -> str:
    return f"PIPE_ETL_TASK_FAILED|{wf[:200]}|{night.isoformat()}"


def _auto_cleared(tasks, *, finished_only: bool = True) -> set:
    """[A] auto-clear IN-list: the (workflow, night) keys with zero final-attempt failures and (finished_only)
    every final attempt ended. finished_only=False = the pre-fix HAVING."""
    groups: dict = {}
    for t in tasks:
        groups.setdefault((t["WORKFLOW_NAME"], t["CYCLE_DATE"]), []).append(t)
    return {_key_a(wf, d) for (wf, d), ts in groups.items()
            if not any(_status(t) in _FAILED for t in ts)
            and (not finished_only or all(t["TERMINAL_END"] is not None for t in ts))}


def _task_failed_scan(tasks, events: list, *, finished_only: bool = True) -> list:
    """One hourly [A]: raise per (workflow, night >= anchor) with >= 1 final-attempt failure unless a
    non-AUTO_CLEARED event already carries the key, THEN auto-clear the OPEN ones. Returns the minted keys."""
    starter = [t["CYCLE_DATE"] for t in tasks if t["WORKFLOW_NAME"] == _S]
    anchor = max(starter) if starter else max(t["CYCLE_DATE"] for t in tasks)
    failing = {(t["WORKFLOW_NAME"], t["CYCLE_DATE"]) for t in tasks
               if t["CYCLE_DATE"] >= anchor and _status(t) in _FAILED}
    minted = []
    for wf, d in sorted(failing):
        key = _key_a(wf, d)
        if not any(e["key"] == key and e["kind"] != "AUTO_CLEARED" for e in events):
            events.append({"key": key, "status": "OPEN", "kind": None})
            minted.append(key)
    cleared = _auto_cleared(tasks, finished_only=finished_only)
    for e in events:
        if e["status"] == "OPEN" and e["key"] in cleared:
            e["status"], e["kind"] = "RESOLVED", "AUTO_CLEARED"
    return minted


def _not_started(tasks, now, *, grace=120):
    """[B] PIPE_ETL_CYCLE_NOT_STARTED over the cache rows (cyc / anchor / missed / the outer WHERE)."""
    cyc: dict = {}
    for t in tasks:
        if t["WORKFLOW_NAME"] == _S:
            d = t["CYCLE_DATE"]
            cyc[d] = min(cyc.get(d, t["FIRST_START"]), t["FIRST_START"])
    if not cyc:
        return None
    anchor = max(cyc)
    lo, hi = anchor - timedelta(days=6), (now - timedelta(hours=12)).date() - timedelta(days=7)
    window = {d: s for d, s in cyc.items() if lo <= d <= hi}
    if not window:
        return None                    # the inner JOIN cyc p leaves no missed row
    due = {d: s for d, s in window.items() if s + timedelta(minutes=7 * 1440 + grace) < now}
    if not due or not (now - cyc[anchor]).total_seconds() > 86400 + grace * 60:
        return None
    ref_night, ref_start = max(due), max(due.values())
    return {"key": f"PIPE_ETL_CYCLE_NOT_STARTED|{(ref_night + timedelta(days=7)).isoformat()}",
            "metric": int((now - (ref_start + timedelta(days=7))).total_seconds() // 60)}


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


def _at(h: int, m: int) -> datetime:
    """A clock time on the morning after _TONIGHT (where the terminal runs and the deadlines fall)."""
    return datetime.combine(_TONIGHT + timedelta(days=1), time(h, m))


def test_v156_model_mirrors_the_sql_fragments():
    for frag in _MODEL_FRAGMENTS:
        _once(frag)
    # the ends CTE filters the TERMINAL workflow (the model's `_T`), never the starter
    ends = _NBODY.split("ends AS (", 1)[1].split("nights AS (", 1)[0]
    assert ends.count("WHERE t.WORKFLOW_NAME = :end_wf") == 1 and ":start_wf" not in ends
    # every [C] CTE the _late model encodes is pinned WHOLE, to its own closing paren, so a filter appended
    # after its last fragment (review round 2: nights / tonight / hist) cannot survive
    got, want = dict(_with_parts(_raise_statements()[2])[1]), dict(_with_parts(_C_RAISE)[1])
    for name in ("starts", "ends", "nights", "latest", "term_lw", "hist", "tonight", "graded"):
        assert got[name] == want[name], name
    assert got["nights"].endswith(" AS DL_T FROM starts s LEFT JOIN ends e ON e.CYCLE_DATE = s.CYCLE_DATE)")
    assert got["hist"].endswith(" QUALIFY ROW_NUMBER() OVER (ORDER BY n.CYCLE_DATE DESC) <= 14) n)")
    assert got["tonight"].endswith(" AS PROJECTED_FINISH FROM nights n JOIN latest l ON n.CYCLE_DATE = l.CYCLE_DATE "
                                   "CROSS JOIN hist h)")
    assert got["latest"] == "latest AS (SELECT MAX(CYCLE_DATE) AS CYCLE_DATE FROM nights)"
    assert got["graded"].endswith(" WHERE :now_ct < DATEADD('hour', 12, t.DL_H) "
                                  "AND NOT (t.TERM_TASKS = 0 AND COALESCE(tl.N, 0) = 0))")
    # the collapse the model's _collapse mirrors
    assert _render_ins_sql() == _INS_SQL


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
    assert all(t["FIRST_OK_END"] == _at(5, 0) for t in tonight)     # the 13:20 finish is before the kickoff
    now = _at(7, 10)
    assert _late(tasks, now) is None
    assert _late(tasks, now, first_ok=False) is None                 # the TERMINAL_START filter alone holds too
    old = _late(tasks, now, filter_col="FIRST_START", first_ok=False)
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


@pytest.mark.parametrize("outcome", ["RUNNING", "SUCCEEDED", "FAILED"])
def test_v156_model_next_morning_rerun_does_not_regrade_an_on_time_night(outcome):
    """Review finding #1: the night finished 05:30 (on time). An operator re-runs s_recon_2 07:05-07:20 and
    s_recon_1 at 09:30 (still running / finished 09:50 / failed 09:50); both attempts key to the same night
    (09:30 - 12h = 21:30 the night before). Graded on each task's LATEST attempt that paged a false CRIT at
    07:10 and EXH at 10:10; FIRST_OK_END keeps each task's first clean in-cycle finish, so nothing is raised."""
    end = None if outcome == "RUNNING" else _at(9, 50)
    rows = (_history() + _night(_TONIGHT, term_start=time(4, 0), term_end=time(5, 30))
            + [(_T, "s_recon_2", "SUCCEEDED", _at(7, 5), _at(7, 20)), (_T, "s_recon_1", outcome, _at(9, 30), end)])
    for now in (_at(6, 10), _at(7, 10), _at(10, 10), _at(11, 10)):
        tasks = _collapse(_as_of(rows, now))
        assert _late(tasks, now) is None, now
        tonight = [t for t in tasks if t["CYCLE_DATE"] == _TONIGHT and t["WORKFLOW_NAME"] == _T]
        assert [t["FIRST_OK_END"] for t in tonight] == [_at(5, 30)] * 2
    early = _late(_collapse(_as_of(rows, _at(7, 10))), _at(7, 10), first_ok=False)
    assert early and (early["band"], early["severity"]) == ("CRIT", "CRITICAL")
    late = _late(_collapse(_as_of(rows, _at(10, 10))), _at(10, 10), first_ok=False)
    assert late and late["band"] == "EXH"


def test_v156_model_first_ok_end_is_bounded_by_the_cycle_start():
    """The naive MIN of any clean finish would let the afternoon re-run (13:00-13:20, before the 22:00 kickoff)
    complete the night and silence a real miss; bounded by the cycle start it does not. And an in-cycle failure
    whose retry is still running the next morning is NOT done: EXH CRITICAL at 10:10."""
    rerun = [(_T, task, "SUCCEEDED", datetime.combine(_TONIGHT, time(13, 0)),
              datetime.combine(_TONIGHT, time(13, 20))) for task in ("s_recon_1", "s_recon_2")]
    rows = _history() + rerun + _night(_TONIGHT, term_start=time(4, 0), term_end=None)   # real run hung
    for bound in ("kickoff", "end", "first"):          # the round-1 first-kickoff bound already held this case
        miss = _late(_collapse(rows, bound=bound), _at(7, 10))
        assert miss and (miss["band"], miss["severity"]) == ("CRIT", "CRITICAL"), bound
    assert _late(_collapse(rows, bound="none"), _at(7, 10)) is None, "the naive MIN silences the real miss"
    failed = (_history() + _night(_TONIGHT, term_start=time(4, 0), term_end=time(4, 30), status="FAILED")
              + [(_T, "s_recon_1", "RUNNING", _at(9, 30), None)])
    got = _late(_collapse(_as_of(failed, _at(10, 10))), _at(10, 10))
    assert got and (got["band"], got["severity"]) == ("EXH", "CRITICAL")


def _afternoon_chain(d: date = _TONIGHT, *, kick: time = time(13, 0), term: tuple = (time(15, 0), time(15, 30))):
    """The WHOLE chain re-run after noon on d, keyed to night d like the real 22:00 kickoff (the ops response to
    a failed previous night): the starter at `kick` (5 min), then both terminal tasks over `term`, clean."""
    k0 = datetime.combine(d, kick)
    return [(_S, "s_kickoff", "SUCCEEDED", k0, k0 + timedelta(minutes=5))] + [
        (_T, task, "SUCCEEDED", datetime.combine(d, term[0]), datetime.combine(d, term[1]))
        for task in ("s_recon_1", "s_recon_2")]


# the real terminal run (both tasks, 04:00 start) of the night after an afternoon full-chain re-run:
# (status, end), and what [C] raises at each hourly scan as (band, severity), None = nothing
_REAL_RUNS = {
    "HUNG": (("RUNNING", None), {"06:10": ("WARN", "HIGH"), "07:10": ("CRIT", "CRITICAL"),
                                 "08:10": ("EXH", "CRITICAL"), "08:40": ("EXH", "CRITICAL")}),
    "FAILED": (("FAILED", time(4, 30)), {"06:10": ("WARN", "HIGH"), "07:10": ("CRIT", "CRITICAL"),
                                         "08:10": ("EXH", "CRITICAL"), "08:40": ("EXH", "CRITICAL")}),
    "LATE": (("SUCCEEDED", time(8, 30)), {"06:10": ("WARN", "HIGH"), "07:10": ("CRIT", "CRITICAL"),
                                          "08:10": ("EXH", "CRITICAL"), "08:40": ("EXH", "HIGH")}),
    "ON_TIME": (("SUCCEEDED", time(5, 0)), {}),
}


@pytest.mark.parametrize("real", sorted(_REAL_RUNS))
def test_v156_model_double_rerun_never_silences_the_real_run(real):
    """Review round 2, finding 1: bounded only by the night's FIRST kickoff (MIN starter start = the 13:00
    afternoon re-run), the 15:30 afternoon finish was each terminal task's FIRST_OK_END, so LATE read the night
    complete all night and a hung, failed or late REAL terminal run raised nothing (no CRITICAL, no incident).
    Bounded by the LAST kickoff too (15:30 < the real 22:00 kickoff) the afternoon finish never counts: quiet
    before the real run, then the real run is judged."""
    (status, end), want = _REAL_RUNS[real]
    rows = _history() + _afternoon_chain() + _night(_TONIGHT, term_start=time(4, 0), term_end=end, status=status)
    scans = {"20:00": datetime.combine(_TONIGHT, time(20, 0)), "23:30": datetime.combine(_TONIGHT, time(23, 30)),
             "06:10": _at(6, 10), "07:10": _at(7, 10), "08:10": _at(8, 10), "08:40": _at(8, 40)}
    got, old = {}, {}
    for label, now in scans.items():
        snap = _as_of(rows, now)
        new, first_only = _late(_collapse(snap), now), _late(_collapse(snap, bound="first"), now)
        got[label] = new and (new["band"], new["severity"])
        old[label] = first_only and (first_only["band"], first_only["severity"])
    assert got == {label: want.get(label) for label in scans}
    assert set(old.values()) == {None}, "the round-1 first-kickoff bound alone silences the real run"
    # the cache row itself: the afternoon finish is never FIRST_OK_END once the real kickoff ran
    tonight = [t for t in _collapse(_as_of(rows, _at(8, 40))) if t["CYCLE_DATE"] == _TONIGHT and t["WORKFLOW_NAME"] == _T]
    ok_end = {"HUNG": None, "FAILED": None, "LATE": _at(8, 30), "ON_TIME": _at(5, 0)}[real]
    assert [t["FIRST_OK_END"] for t in tonight] == [ok_end, ok_end]
    before = _collapse(_as_of(rows, datetime.combine(_TONIGHT, time(20, 0))))
    assert [t["FIRST_OK_END"] for t in before if t["CYCLE_DATE"] == _TONIGHT and t["WORKFLOW_NAME"] == _T] == [
        datetime.combine(_TONIGHT, time(15, 30))] * 2        # before the real kickoff it is the night's finish



@pytest.mark.parametrize("real", sorted(_REAL_RUNS))
def test_v156_model_afternoon_chain_straddling_the_kickoff_never_silences_the_real_run(real):
    """Review round 2 re-review: the afternoon chain's terminal ran 21:30-22:10 across the real 22:00 kickoff.
    Bounded by the END (round 2) its 22:10 clean finish was FIRST_OK_END and a hung, failed or late REAL
    terminal raised nothing; bounded by the attempt's START (it began 21:30, before the kickoff) it never counts."""
    (status, end), want = _REAL_RUNS[real]
    rows = (_history() + _afternoon_chain(kick=time(15, 0), term=(time(21, 30), time(22, 10)))
            + _night(_TONIGHT, term_start=time(4, 0), term_end=end, status=status))
    scans = {"20:00": datetime.combine(_TONIGHT, time(20, 0)), "23:30": datetime.combine(_TONIGHT, time(23, 30)),
             "06:10": _at(6, 10), "07:10": _at(7, 10), "08:10": _at(8, 10), "08:40": _at(8, 40)}
    got, old = {}, {}
    for label, now in scans.items():
        snap = _as_of(rows, now)
        new, end_bound = _late(_collapse(snap), now), _late(_collapse(snap, bound="end"), now)
        got[label] = new and (new["band"], new["severity"])
        old[label] = end_bound and (end_bound["band"], end_bound["severity"])
    assert got == {label: want.get(label) for label in scans}
    assert set(old.values()) == {None}, "the round-2 end bound silences the real run"
    tonight = [t for t in _collapse(_as_of(rows, _at(8, 40))) if t["CYCLE_DATE"] == _TONIGHT and t["WORKFLOW_NAME"] == _T]
    ok_end = {"HUNG": None, "FAILED": None, "LATE": _at(8, 30), "ON_TIME": _at(5, 0)}[real]
    assert [t["FIRST_OK_END"] for t in tonight] == [ok_end, ok_end]


def test_v156_model_late_afternoon_chain_is_the_documented_silent_residual():
    """The residual the header names SILENT: an afternoon chain whose terminal STARTS after the real kickoff
    (16:00 chain, terminal 22:30-23:10 vs the 22:00 kickoff) looks exactly like a real early run, so its clean
    finish is FIRST_OK_END and a hung real terminal raises no LATE. Locked so a change here is deliberate."""
    rows = (_history() + _afternoon_chain(kick=time(16, 0), term=(time(22, 30), time(23, 10)))
            + _night(_TONIGHT, term_start=time(4, 0), term_end=None))
    for now in (_at(6, 10), _at(7, 10), _at(8, 40)):
        assert _late(_collapse(_as_of(rows, now)), now) is None, now
    hung = _late(_collapse(_as_of(rows, _at(7, 10))), _at(7, 10), first_ok=False)
    assert hung and (hung["band"], hung["severity"]) == ("CRIT", "CRITICAL"), "latest-attempt grading would page"



def test_v156_model_hang_before_dispatch_after_afternoon_chain_is_the_documented_silent_residual():
    """SILENT residual 1 (header): after a 13:00 afternoon re-run of the whole chain (terminal 15:00-15:30), the
    real 22:00 cycle hangs BEFORE its terminal dispatches. The afternoon attempt is never FIRST_OK_END, but
    CYCLE_START is the afternoon MIN, so its latest attempt still reads the night complete: no LATE. Without the
    afternoon chain the same night pages CRIT CRITICAL at 07:10. Locked so a change here is deliberate."""
    hung = _night(_TONIGHT, terminal=False)
    for now in (_at(6, 10), _at(7, 10), _at(8, 40)):
        assert _late(_collapse(_as_of(_history() + _afternoon_chain() + hung, now)), now) is None, now
    alone = _late(_collapse(_history() + hung), _at(7, 10))
    assert alone and (alone["band"], alone["severity"]) == ("CRIT", "CRITICAL")


def test_v156_model_next_morning_starter_rerun_is_the_documented_loud_residual():
    """The known edge the last-kickoff bound leaves (header): a STARTER re-run the next morning (09:00, keyed to
    the night) is the night's last kickoff, so the 05:30 on-time finish no longer counts as FIRST_OK_END. Alone
    it changes nothing (each terminal task's latest attempt is still its clean 04:00-05:30 run); with a terminal
    re-run alongside, the night re-grades on that re-run -- LOUD (EXH: CRITICAL while it runs, HIGH once it
    ends clean), never silent."""
    on_time = _history() + _night(_TONIGHT, term_end=time(5, 30))
    starter = [(_S, "s_kickoff", "SUCCEEDED", _at(9, 0), _at(9, 5))]
    for now in (_at(9, 40), _at(10, 10), _at(11, 10)):
        tasks = _collapse(_as_of(on_time + starter, now))
        assert _late(tasks, now) is None, now
        assert [t["FIRST_OK_END"] for t in tasks
                if t["CYCLE_DATE"] == _TONIGHT and t["WORKFLOW_NAME"] == _T] == [None, None], now
    rerun = [*on_time, *starter, (_T, "s_recon_1", "SUCCEEDED", _at(9, 30), _at(9, 50))]
    running = _late(_collapse(_as_of(rerun, _at(9, 40))), _at(9, 40))
    assert running and (running["band"], running["severity"]) == ("EXH", "CRITICAL")
    done = _late(_collapse(_as_of(rerun, _at(10, 10))), _at(10, 10))
    assert done and (done["band"], done["severity"]) == ("EXH", "HIGH")


_ONE = "WF_ONE"


def _one_workflow(rows):
    """The same attempts under ETL_CYCLE_START_WORKFLOW = ETL_CYCLE_END_WORKFLOW (one workflow is the cycle)."""
    return [(_ONE, task, status, start, end) for _wf, task, status, start, end in rows]


def test_v156_model_starter_is_terminal_fails_loud_not_silent():
    """The start = end decision (header known edges), locked. With one workflow as both starter and terminal,
    every task start is a kickoff, so the last-kickoff bound voids a finished night on ANY next-morning re-run
    of one of its tasks (round 1's page comes back for this configuration: EXH CRITICAL while the re-run runs).
    The alternative -- the first-kickoff bound alone when start = end -- keeps that night quiet but lets an
    afternoon re-run of the workflow complete the night and silence a hung real run. The SQL takes the loud
    side: no special case (cs binds only the starter; locked in the render test)."""
    def late(rows, now, bound="kickoff"):
        return _late(_collapse(_as_of(rows, now), bound=bound, start_wf=_ONE), now, start_wf=_ONE, end_wf=_ONE)
    hist = _one_workflow(_history())
    normal = hist + _one_workflow(_night(_TONIGHT))
    for now in (_at(6, 10), _at(7, 10), _at(10, 10)):
        assert late(normal, now) is None, now
    # an afternoon re-run of the whole workflow, then the real run hangs: CRITICAL at 07:10, never silent ...
    hung = hist + _one_workflow(_afternoon_chain() + _night(_TONIGHT, term_end=None))
    got = late(hung, _at(7, 10))
    assert got and (got["band"], got["severity"]) == ("CRIT", "CRITICAL")
    assert late(hung, _at(7, 10), bound="first") is None, "the rejected special case silences the hung run"
    # ... at the documented price: a next-morning re-run of a task of a night that finished on time pages
    rerun = hist + _one_workflow([*_night(_TONIGHT, term_end=time(5, 30)),
                                  (_T, "s_recon_1", "RUNNING", _at(9, 30), None)])
    loud = late(rerun, _at(10, 10))
    assert loud and (loud["band"], loud["severity"]) == ("EXH", "CRITICAL")
    assert late(rerun, _at(10, 10), bound="first") is None


def test_v156_model_weekday_only_terminal_is_not_judged():
    """term_lw: a night whose terminal has not dispatched is judged only when it dispatched on the same night
    last week (a weekday-only terminal stays quiet on its off night)."""
    now = _at(7, 10)
    quiet = _collapse(_history(skip_last_week_terminal=True) + _night(_TONIGHT, terminal=False))
    assert _late(quiet, now) is None
    due = _collapse(_history() + _night(_TONIGHT, terminal=False))
    miss = _late(due, now)
    assert miss and miss["band"] == "CRIT" and miss["severity"] == "CRITICAL"


def test_v156_model_severity_split_and_lead_window_metric():
    at = _at
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


def test_v156_model_partial_tonight_never_enters_the_history():
    """hist reads PRIOR nights only (n.CYCLE_DATE < latest): tonight with one terminal task finished and the
    other not dispatched is incomplete (the usual count is 2), so 06:10 raises the lead-window WARN."""
    rows = _history() + _night(_TONIGHT)[:2]            # the starter + s_recon_1 only
    got = _late(_collapse(rows), _at(6, 10))
    assert got and (got["band"], got["term"], got["complete"]) == ("WARN", 1, False)


def test_v156_model_task_failed_retry_in_flight_keeps_the_event_open():
    """Review finding #2: a terminal task fails 02:00-02:05, a retry runs 02:40-04:05 and fails, a second retry
    runs 04:20-05:00 clean. Clearing on 'no failed final attempt' resolved the event at 03:10 while the retry was
    only RUNNING, so the failed retry minted a second event (and a second HIGH email). Clearing only once every
    final attempt FINISHED clean keeps ONE event open until 05:10."""
    rows = [*_night(_TONIGHT, terminal=False),
            (_T, "s_recon_1", "FAILED", _at(2, 0), _at(2, 5)),
            (_T, "s_recon_1", "FAILED", _at(2, 40), _at(4, 5)),
            (_T, "s_recon_1", "SUCCEEDED", _at(4, 20), _at(5, 0))]
    key = _key_a(_T, _TONIGHT)
    for finished_only, mints in ((True, 1), (False, 2)):
        events: list = []
        minted, states = [], {}
        for now in (_at(2, 10), _at(3, 10), _at(4, 10), _at(4, 30), _at(5, 10)):
            minted += _task_failed_scan(_collapse(_as_of(rows, now)), events, finished_only=finished_only)
            states[now] = [e["status"] for e in events]
        assert minted.count(key) == mints, finished_only
        if finished_only:
            assert states[_at(3, 10)] == ["OPEN"] and states[_at(4, 30)] == ["OPEN"]
            assert states[_at(5, 10)] == ["RESOLVED"] and events[0]["kind"] == "AUTO_CLEARED"


def test_v156_model_cache_holds_whole_nights_so_an_old_failure_never_auto_clears():
    """Review finding #3: an OPEN (MEDIUM, not emailed) TASK_FAILED event for night N, where WF_X's a_load
    failed at 02:00 and b_load succeeded at 03:00 and nothing was re-run. A timestamp-only lookback cut slid
    between 02:00 and 03:00 about 23 days later, so the cached night held only b_load and the auto-clear
    resolved the event as 'retry recovered'. Cut by night key, a night is cached whole or not at all."""
    n = date(2026, 8, 20)
    nxt = n + timedelta(days=1)
    rows = [(_S, "s_kickoff", "SUCCEEDED", datetime.combine(n, time(22, 0)), datetime.combine(n, time(22, 5))),
            ("WF_X", "a_load", "FAILED", datetime.combine(nxt, time(2, 0)), datetime.combine(nxt, time(2, 5))),
            ("WF_X", "b_load", "SUCCEEDED", datetime.combine(nxt, time(3, 0)), datetime.combine(nxt, time(3, 10)))]
    key = _key_a("WF_X", n)
    by_night: dict = {}
    for r in rows:
        by_night.setdefault((r[3] - timedelta(hours=12)).date(), set()).add(r)
    old_cleared = []
    for hour in range(72):
        now = datetime.combine(n + timedelta(days=22), time(0, 10)) + timedelta(hours=hour)
        cached = _in_cache(rows, now)
        for night in {(r[3] - timedelta(hours=12)).date() for r in cached}:
            assert by_night[night] <= set(cached), (now, night)          # whole night or nothing
        assert key not in _auto_cleared(_collapse(cached)), now
        if key in _auto_cleared(_collapse(_in_cache(rows, now, whole_nights=False))):
            old_cleared.append(now)
    assert old_cleared == [datetime.combine(n + timedelta(days=24), time(2, 10))]
    # the cut keeps exactly lookback_days nights: N is cached through the scan before N + 23d 12:00
    assert _in_cache(rows, datetime.combine(n + timedelta(days=23), time(11, 10)))
    assert not _in_cache(rows, datetime.combine(n + timedelta(days=23), time(12, 10)))


def test_v156_model_not_started_is_the_overdue_test():
    """[B]: a holiday tonight (the starter ran every night before) fires once the starter is silent past 24h +
    grace AND last week's same-night kickoff + 7 days + grace has passed; a normal night never fires."""
    holiday = _collapse(_history())                           # no starter run on _TONIGHT
    assert _not_started(holiday, datetime.combine(_TONIGHT, time(23, 59))) is None      # silent 25h59m
    got = _not_started(holiday, _at(0, 10))                   # silent 26h10m; last week's 22:00 + 7d + 2h passed
    assert got == {"key": "PIPE_ETL_CYCLE_NOT_STARTED|2026-09-22", "metric": 130}
    assert _not_started(holiday, _at(0, 10), grace=180) is None
    normal = _collapse(_history() + _night(_TONIGHT, terminal=False))
    assert _not_started(normal, _at(0, 10)) is None
    assert _not_started(normal, _at(9, 10)) is None
