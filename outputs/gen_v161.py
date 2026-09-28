#!/usr/bin/env python3
"""Forward-generate V161: retire the scheduled operator-data backups (owner decision 2026-09-28).

V158 (Next-Fifty #32) made TASK_BACKUP_OPERATOR clone the 25 operator tables every day into dated TRANSIENT
generations in their own schema, DBA_MAINT_DB.OVERWATCH_BAK, next to V089's weekly ``<T>_BAK_LAST`` copies in
OVERWATCH, with a row-count ledger (OPERATOR_BACKUP_LOG), two retention SETTINGS and a dead-man freshness row
(OPERATOR_BACKUP_DAILY). The owner chose to remove scheduled backups entirely: recovery is Snowflake Time Travel
plus the manual ``<T>_BAK_<yyyymmdd>`` clones taken before a risky change (teardown.sql B0,
snowflake/rebuild/00_backup_operator_data.sql), which stay.

V161, in order:
  0. ``USE SCHEMA DBA_MAINT_DB.OVERWATCH``: SP_LOAD_SECURITY_FACTS records each statement's CURRENT database and
     schema, so the footprint below is only deterministic from there (review r1);
  1. version guard (V160 first);
  2. read-only preflight: DROP SCHEMA cascades, so stop if OVERWATCH_BAK holds a table/view V158 (or a partial
     V161) did not put there, or any stage, sequence, file format, function, procedure, pipe, task, stream or
     alert;
  3. suspend TASK_BACKUP_OPERATOR, then wait (up to ~10 minutes) while a run is in flight -- V158's own tail
     EXECUTE TASK starts one on a replay -- and stop only if it is still running: an in-flight run's freshness
     MERGE could re-create the dead-man row after the DELETE below;
  4. drop the task and the proc; move OPERATOR_BACKUP_LOG and the 25 ``<T>_BAK_LAST`` copies into OVERWATCH_BAK
     (falling back to a DROP) and drop the schema once. Every drop is existence-gated: a no-op DROP IF EXISTS
     still succeeds, and SP_LOAD_SECURITY_FACTS scores every DROP by a SNOW_ACCOUNTADMINS worksheet CRITICAL, so a
     re-run must add nothing. The move-then-one-drop shape leaves 3 CRITICAL CHANGE RISK rows (task, proc,
     schema; 4 if the PERMANENT ledger cannot move into the transient schema) instead of ~29; the RENAMEs score
     as ALTERs (MEDIUM), below the queue;
  5. delete the BACKUP_KEEP_* SETTINGS rows and, last, the OPERATOR_BACKUP_DAILY freshness row, then close any
     open OPS_PIPELINE_DEGRADED stale event for it as EXPECTED;
  6. re-derive V_SECURITY_EXCEPTION_QUEUE from V158 minus its backup-prune carve-out (the result is V151's view
     byte for byte: nothing is left for the carve-out to match).

Names come only from hard-coded arrays (the 25 of V158:98-107); nothing is matched by pattern, so the manual
``<T>_BAK_<yyyymmdd>`` clones in OVERWATCH survive. With PREFLIGHT_OUT set, also writes PREFLIGHT_V161.sql
(read-only); with PART_B_OUT set, the read-only PART B verify grid. Owner applies in Snowsight after V160.
This file never runs from the app. Do NOT import outputs/gen_v158.py: it rewrites V158 at import time.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
V151 = MIG / "V151__security_change_risk_identity_policy_drops.sql"
V158 = MIG / "V158__operator_backup_generations.sql"

# The 25 operator tables, verbatim from V158's proc (V158:98-107; the same list as V075/V089).
TABLES = (
    "SETTINGS", "COMPANY_SCOPE", "ALERT_CONFIG", "ALERT_EVENTS",
    "ALERT_AUDIT", "ACTION_QUEUE", "SAVINGS_LEDGER", "DEPARTMENT_MAP",
    "ALERT_ROUTES", "REMEDIATION_LOG", "USER_PREFS",
    "OBJECT_CHANGE_REGISTRY", "WAREHOUSE_CHANGE_REGISTRY",
    "WAREHOUSE_CONFIG_SNAPSHOT", "PIPELINE_SLA_CONFIG", "DAILY_DIGEST",
    "DEPT_BUDGETS", "INCIDENTS", "INCIDENT_MEMBERS", "ACTION_ACTIVITY",
    "EVIDENCE_LINKS", "ENTITY_CATALOG", "USER_WATCHLIST",
    "OPTIMIZATION_EXPERIMENTS", "SLO_OBJECTIVES",
)
_v158 = V158.read_text(encoding="utf-8")
_v158_list = re.search(r"tables ARRAY DEFAULT \[(.*?)\];", _v158, re.S)
assert _v158_list, "V158's tables array not found"
assert tuple(re.findall(r"'(\w+)'", _v158_list.group(1))) == TABLES, "TABLES must equal V158:98-107 exactly"
assert len(TABLES) == 25 and len(set(TABLES)) == 25

BAK_LAST = tuple(f"{t}_BAK_LAST" for t in TABLES)
# What V161 moves into OVERWATCH_BAK before the one DROP SCHEMA: the ledger, then the 25 weekly copies.
MOVE = ("OPERATOR_BACKUP_LOG", *BAK_LAST)
# V158's prune regex (V158:154) over the same 25: the generation names the preflight accepts.
GEN_RE = "(" + "|".join(TABLES) + ")_OWBAK_[DW][0-9]{8}"


def _sql_array(names: tuple[str, ...], indent: str) -> str:
    """A Snowflake ARRAY literal, four names per line (V158's layout)."""
    rows = [", ".join(f"'{n}'" for n in names[i:i + 4]) for i in range(0, len(names), 4)]
    return "[\n" + ",\n".join(indent + r for r in rows) + "\n" + indent[:-4] + "]"


def _in_list(names: tuple[str, ...], indent: str) -> str:
    rows = [", ".join(f"'{n}'" for n in names[i:i + 4]) for i in range(0, len(names), 4)]
    return (",\n" + indent).join(rows)


# ------------------------------------------------------------------------------------------------
# 1. V_SECURITY_EXCEPTION_QUEUE: V158's view minus its backup-prune carve-out (= V151's view)
# ------------------------------------------------------------------------------------------------
_VIEW_RE = re.compile(r"CREATE OR REPLACE VIEW DBA_MAINT_DB\.OVERWATCH\.V_SECURITY_EXCEPTION_QUEUE AS.*?;\n", re.S)
_views158 = _VIEW_RE.findall(_v158)
_views151 = _VIEW_RE.findall(V151.read_text(encoding="utf-8"))
assert len(_views158) == 1 and len(_views151) == 1, "expected exactly one V_SECURITY_EXCEPTION_QUEUE per file"
CARVE_OUT = (
    "      -- V158 (Next-Fifty #32): the backup-generation prune of OVERWATCH itself (task-run as SYSTEM,\n"
    "      -- the exact generated DROP only). A human DROP of a backup or any other drop still surfaces.\n"
    "      AND NOT (CHANGE_KIND = 'DESTRUCTIVE'\n"
    "               AND UPPER(COALESCE(USER_NAME, '')) = 'SYSTEM'\n"
    "               AND REGEXP_LIKE(COALESCE(QUERY_PREVIEW, ''),\n"
    "                   'DROP TABLE IF EXISTS DBA_MAINT_DB[.]OVERWATCH_BAK[.][A-Z0-9_]+_OWBAK_[DW][0-9]{8}'))"
)
assert _views158[0].count("\n" + CARVE_OUT) == 1, "V158's carve-out block not found exactly once"
view = _views158[0].replace("\n" + CARVE_OUT, "", 1)
assert view == _views151[0], "V158's view minus the carve-out must be V151's view byte for byte"
assert "OVERWATCH_BAK" not in view and "_OWBAK_" not in view
assert view.count("AND NOT (CHANGE_KIND = 'DESTRUCTIVE'") == 1      # V151's TF_* / PUBLIC exclusion stays
assert "'DROP BACKUP POLICY %'" in view and "LIKE 'TF~_%' ESCAPE '~'" in view

VIEW_MARKER = ("-- >>> derived:V_SECURITY_EXCEPTION_QUEUE  (from V158; - the OVERWATCH_BAK backup-prune carve-out, "
               "leaving V151's view text, V161)\n")

# ------------------------------------------------------------------------------------------------
# 2. the file
# ------------------------------------------------------------------------------------------------
HEADER = """\
-- V161__retire_operator_backups.sql
--
-- Retire the scheduled operator-data backups (owner decision 2026-09-28). V158 (Next-Fifty #32) made
-- TASK_BACKUP_OPERATOR clone the 25 operator tables every day at 05:10 Central into dated TRANSIENT
-- generations in their own schema, DBA_MAINT_DB.OVERWATCH_BAK, next to V089's weekly <T>_BAK_LAST copies in
-- OVERWATCH, with a row-count ledger (OPERATOR_BACKUP_LOG), SETTINGS BACKUP_KEEP_DAILY / BACKUP_KEEP_WEEKLY
-- and the dead-man freshness row OPERATOR_BACKUP_DAILY. All of it goes. Recovery from here on is Snowflake
-- Time Travel (INSERT OVERWRITE ... AT / BEFORE, UNDROP) plus the manual <T>_BAK_<yyyymmdd> clones taken
-- before a risky change (teardown.sql B0, snowflake/rebuild/00_backup_operator_data.sql), which stay.
-- Retention: no migration sets DATA_RETENTION_TIME_IN_DAYS, so the account default applies; the TRANSIENT
-- operator tables (ALERT_EVENTS, ACTION_QUEUE, ...) keep at most 1 day of Time Travel and no Fail-safe.
--
-- Order (every drop existence-gated, so a re-run adds nothing):
-- * The session is pinned to DBA_MAINT_DB.OVERWATCH: SP_LOAD_SECURITY_FACTS records each statement's CURRENT
--   database/schema, so the footprint below holds only from here (from DBA_MAINT_DB.PUBLIC the V151 view hides
--   the DROPs; a *PROD* current database raises the RENAMEs to HIGH). Every name is fully qualified anyway.
-- * Preflight: DROP SCHEMA cascades, so V161 stops (-20611) if OVERWATCH_BAK holds anything V158 did not put
--   there that the preflight can see: a table or view that is not a V158 generation, a moved <T>_BAK_LAST or
--   OPERATOR_BACKUP_LOG, or any stage, sequence, file format, function, procedure, pipe, task, stream or alert.
--   PREFLIGHT_V161.sql lists the same; read it before applying.
-- * TASK_BACKUP_OPERATOR is suspended, then V161 waits (up to ~10 minutes, SYSTEM$WAIT 15 s between checks)
--   while a run is in flight -- a replay's V158 tail starts one seconds earlier -- because its freshness MERGE
--   could re-create the dead-man row after the DELETE below. Still running after that, V161 stops (-20612):
--   re-run it once the run ends. Avoid applying between about 05:05 and 05:20 Central. (The one-off wait is
--   far below COST_SLEEP_POLLING's 5-of-7-days bar.)
-- * The task and SP_BACKUP_OPERATOR_TABLES are dropped. OPERATOR_BACKUP_LOG and the 25 <T>_BAK_LAST copies
--   are moved into OVERWATCH_BAK (a DROP if the move fails) and the schema is dropped once, with every
--   generation in it. Names come only from the hard-coded 25 of V158; nothing is dropped by pattern, so the
--   manual <T>_BAK_<yyyymmdd> clones in OVERWATCH are never touched.
-- * SETTINGS BACKUP_KEEP_* and, last, SOURCE_FRESHNESS_STATE OPERATOR_BACKUP_DAILY are deleted (nothing
--   re-creates the row once the proc is gone), and any open OPS_PIPELINE_DEGRADED stale event for it is
--   closed as EXPECTED.
-- * V_SECURITY_EXCEPTION_QUEUE is re-derived from V158 without its backup-prune carve-out, which leaves V151's
--   view text. No prune has run before 2026-10-03 (BACKUP_KEEP_DAILY 7) / 2026-10-10 (the default 14), so
--   applied before then the carve-out never matched a row and the queue does not change.
--
-- Security footprint (disclosed; nothing emails or pages): applied as SNOW_ACCOUNTADMINS, the three DROPs
-- (task, proc, schema) land in FACT_SECURITY_CHANGE as DESTRUCTIVE / CRITICAL, so the Security page shows
-- CHANGE RISK "Act" for 7 days. The RENAMEs score as ALTERs (MEDIUM), below the queue. A move that falls back
-- to a DROP adds one more CRITICAL row: expect that for OPERATOR_BACKUP_LOG, the one PERMANENT table, if
-- Snowflake refuses to move it into the transient schema (4 rows then). Never delete these rows.
--
-- Rolling back V161: within the dropped schema's retention (at most 1 day for a transient schema; SHOW
-- PARAMETERS LIKE 'DATA_RETENTION_TIME_IN_DAYS' IN DATABASE DBA_MAINT_DB), UNDROP SCHEMA DBA_MAINT_DB.OVERWATCH_BAK
-- FIRST: it brings back the generations, the ledger and the weekly copies, and V158's CREATE ... IF NOT EXISTS
-- would otherwise take the name. Then re-run V015's TASK_BACKUP_OPERATOR block (lines 61-67; not the whole
-- file, which would re-create the retired MART_SPEND_ROLLUP_DT) and V158 in full, and redeploy app 4.597.0
-- (4.598 hides the task from Tasks > SLA and has no BACKUP_KEEP_* editors). Past that window the dropped
-- generations are gone for good.
-- Owner applies in Snowsight after V160, as the role that applied V158 (it owns OVERWATCH_BAK and the task).
-- This file never runs from the app.

USE SCHEMA DBA_MAINT_DB.OVERWATCH;

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20161, 'V161 requires V160 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 160) THEN
        RAISE not_ready;
    END IF;
END;
$$;
"""

PREFLIGHT_BLOCK = f"""
-- 1. Preflight (read-only): DROP SCHEMA cascades, so stop before anything changes if OVERWATCH_BAK holds a
--    table or view that is not a V158 generation, a moved <T>_BAK_LAST or OPERATOR_BACKUP_LOG, or any stage,
--    sequence, file format, function, procedure, pipe, task, stream or alert. Gated on the schema existing
--    (SHOW ... IN SCHEMA errors on a missing one), so a re-run after the drop passes straight through.
EXECUTE IMMEDIATE
$$
DECLARE
    foreign_n NUMBER DEFAULT 0;
    n NUMBER DEFAULT 0;
    bak_schema BOOLEAN DEFAULT FALSE;
    foreign_objects EXCEPTION (-20611, 'V161 stopped before any change: DBA_MAINT_DB.OVERWATCH_BAK holds objects V158 did not create. PREFLIGHT_V161.sql (P1, P2) lists them; move them out, then re-run V161.');
BEGIN
    SELECT COUNT(*) > 0 INTO :bak_schema
      FROM DBA_MAINT_DB.INFORMATION_SCHEMA.SCHEMATA
     WHERE SCHEMA_NAME = 'OVERWATCH_BAK';
    IF (bak_schema) THEN
        SELECT (SELECT COUNT(*) FROM DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES
                 WHERE TABLE_SCHEMA = 'OVERWATCH_BAK'
                   AND NOT (TABLE_TYPE = 'BASE TABLE'
                            AND (REGEXP_LIKE(TABLE_NAME, '{GEN_RE}')
                                 OR TABLE_NAME IN ({_in_list(MOVE, "                                                   ")}))))
             + (SELECT COUNT(*) FROM DBA_MAINT_DB.INFORMATION_SCHEMA.STAGES WHERE STAGE_SCHEMA = 'OVERWATCH_BAK')
             + (SELECT COUNT(*) FROM DBA_MAINT_DB.INFORMATION_SCHEMA.SEQUENCES WHERE SEQUENCE_SCHEMA = 'OVERWATCH_BAK')
             + (SELECT COUNT(*) FROM DBA_MAINT_DB.INFORMATION_SCHEMA.FILE_FORMATS WHERE FILE_FORMAT_SCHEMA = 'OVERWATCH_BAK')
             + (SELECT COUNT(*) FROM DBA_MAINT_DB.INFORMATION_SCHEMA.FUNCTIONS WHERE FUNCTION_SCHEMA = 'OVERWATCH_BAK')
             + (SELECT COUNT(*) FROM DBA_MAINT_DB.INFORMATION_SCHEMA.PROCEDURES WHERE PROCEDURE_SCHEMA = 'OVERWATCH_BAK')
             + (SELECT COUNT(*) FROM DBA_MAINT_DB.INFORMATION_SCHEMA.PIPES WHERE PIPE_SCHEMA = 'OVERWATCH_BAK')
          INTO :foreign_n;
        SHOW TASKS IN SCHEMA DBA_MAINT_DB.OVERWATCH_BAK;
        SELECT COUNT(*) INTO :n FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
        foreign_n := foreign_n + n;
        SHOW STREAMS IN SCHEMA DBA_MAINT_DB.OVERWATCH_BAK;
        SELECT COUNT(*) INTO :n FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
        foreign_n := foreign_n + n;
        SHOW ALERTS IN SCHEMA DBA_MAINT_DB.OVERWATCH_BAK;
        SELECT COUNT(*) INTO :n FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
        foreign_n := foreign_n + n;
    END IF;
    IF (foreign_n > 0) THEN
        RAISE foreign_objects;
    END IF;
END;
$$;
"""

SUSPEND = """
-- 2. Stop the 05:10 run (a standalone root: no AFTER dependents, V015:61-67).
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR SUSPEND;
"""

IN_FLIGHT_BLOCK = """
-- 3. In-flight guard, AFTER the suspend (a started task always shows its next run as SCHEDULED in the future):
--    wait while a run executes or is due now, so its freshness MERGE cannot re-create the row deleted below.
--    A replay's V158 tail starts a run seconds before this; it normally ends within the wait. A SCHEDULED row
--    more than 30 minutes overdue is stale, not in flight, and never blocks.
EXECUTE IMMEDIATE
$$
DECLARE
    running NUMBER DEFAULT 0;
    backup_in_flight EXCEPTION (-20612, 'V161 stopped: a TASK_BACKUP_OPERATOR run was still in flight after about 10 minutes. The task is suspended; re-run V161 once the run ends.');
BEGIN
    FOR attempt IN 1 TO 40 DO
        SELECT COUNT(*) INTO :running
          FROM TABLE(DBA_MAINT_DB.INFORMATION_SCHEMA.TASK_HISTORY(
                   SCHEDULED_TIME_RANGE_START => DATEADD('hour', -6, CURRENT_TIMESTAMP()),
                   TASK_NAME => 'TASK_BACKUP_OPERATOR'))
         WHERE DATABASE_NAME = 'DBA_MAINT_DB' AND SCHEMA_NAME = 'OVERWATCH'
           AND (STATE = 'EXECUTING'
                OR (STATE = 'SCHEDULED'
                    AND SCHEDULED_TIME <= CURRENT_TIMESTAMP()
                    AND SCHEDULED_TIME >= DATEADD('minute', -30, CURRENT_TIMESTAMP())));
        IF (running = 0) THEN
            BREAK;
        END IF;
        SELECT SYSTEM$WAIT(15);
    END FOR;
    IF (running > 0) THEN
        RAISE backup_in_flight;
    END IF;
END;
$$;
"""

RETIRE_BLOCK = f"""
-- 4. Retire the objects. Existence-gated: a no-op DROP IF EXISTS still succeeds and is scored as a DROP, so a
--    re-run must issue none. The ledger and the 25 weekly copies move into OVERWATCH_BAK so ONE DROP SCHEMA
--    removes them with every generation (3 CRITICAL CHANGE RISK rows: task, proc, schema); a move that fails
--    falls back to a DROP of that one table (one more CRITICAL row; likeliest for the PERMANENT ledger).
EXECUTE IMMEDIATE
$$
DECLARE
    targets ARRAY DEFAULT {_sql_array(MOVE, "        ")};
    found_names ARRAY;
    tname VARCHAR;
    n NUMBER DEFAULT 0;
    bak_schema BOOLEAN DEFAULT FALSE;
    i INT;
BEGIN
    SHOW TASKS LIKE 'TASK_BACKUP_OPERATOR' IN SCHEMA DBA_MAINT_DB.OVERWATCH;
    SELECT COUNT(*) INTO :n FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) WHERE "name" = 'TASK_BACKUP_OPERATOR';
    IF (n > 0) THEN
        DROP TASK DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR;
    END IF;

    SELECT COUNT(*) INTO :n
      FROM DBA_MAINT_DB.INFORMATION_SCHEMA.PROCEDURES
     WHERE PROCEDURE_SCHEMA = 'OVERWATCH' AND PROCEDURE_NAME = 'SP_BACKUP_OPERATOR_TABLES'
       AND ARGUMENT_SIGNATURE = '()';
    IF (n > 0) THEN
        DROP PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_BACKUP_OPERATOR_TABLES();
    END IF;

    SELECT COUNT(*) > 0 INTO :bak_schema
      FROM DBA_MAINT_DB.INFORMATION_SCHEMA.SCHEMATA
     WHERE SCHEMA_NAME = 'OVERWATCH_BAK';

    -- ONE probe for the 26 names (exact names only: never a LIKE sweep, the manual clones share OVERWATCH).
    SELECT COALESCE(ARRAY_AGG(TABLE_NAME) WITHIN GROUP (ORDER BY TABLE_NAME), ARRAY_CONSTRUCT())
      INTO :found_names
      FROM DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES
     WHERE TABLE_SCHEMA = 'OVERWATCH' AND TABLE_TYPE = 'BASE TABLE'
       AND ARRAY_CONTAINS(TABLE_NAME::VARIANT, :targets);
    IF (ARRAY_SIZE(:found_names) > 0) THEN
        FOR i IN 0 TO ARRAY_SIZE(:found_names) - 1 DO
            tname := GET(:found_names, i)::VARCHAR;
            IF (bak_schema) THEN
                BEGIN
                    EXECUTE IMMEDIATE 'ALTER TABLE DBA_MAINT_DB.OVERWATCH.' || :tname ||
                                      ' RENAME TO DBA_MAINT_DB.OVERWATCH_BAK.' || :tname;
                EXCEPTION
                    WHEN OTHER THEN
                        EXECUTE IMMEDIATE 'DROP TABLE DBA_MAINT_DB.OVERWATCH.' || :tname;
                END;
            ELSE
                EXECUTE IMMEDIATE 'DROP TABLE DBA_MAINT_DB.OVERWATCH.' || :tname;
            END IF;
        END FOR;
    END IF;

    IF (bak_schema) THEN
        DROP SCHEMA DBA_MAINT_DB.OVERWATCH_BAK CASCADE;
    END IF;
END;
$$;
"""

DATA = """
-- 5. The proc's SETTINGS (its only reader was the proc, V158:140-145).
DELETE FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
 WHERE KEY IN ('BACKUP_KEEP_DAILY', 'BACKUP_KEEP_WEEKLY');

-- 6. The dead-man freshness row, LAST: its only writer (the proc) is gone, so nothing re-creates it, and no
--    expected-source registry alerts on its absence (V043's retire idiom).
DELETE FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
 WHERE SOURCE_NAME = 'OPERATOR_BACKUP_DAILY';

-- 7. Close any open stale event for it (key shape V157 / V160: RULE|STALE|SOURCE|day); SNOOZED too, or the hourly
--    wake step would reopen an event no scan can close again (V157's retire idiom). ALERT_EVENTS history stays.
UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
   SET STATUS = 'RESOLVED', RESOLUTION_KIND = 'EXPECTED',
       RESOLVED_AT = CURRENT_TIMESTAMP()
 WHERE RULE_ID = 'OPS_PIPELINE_DEGRADED'
   AND STARTSWITH(DEDUPE_KEY, 'OPS_PIPELINE_DEGRADED|STALE|OPERATOR_BACKUP_DAILY|')
   AND STATUS IN ('OPEN', 'ACK', 'SNOOZED');
"""

VIEW_HEAD = """
-- 8. The Security exception queue without V158's backup-prune carve-out (nothing is left for it to match).
"""

DESCRIPTION = (
    "Scheduled operator-data backups retired (owner decision 2026-09-28): recovery is Snowflake Time Travel plus "
    "the manual <T>_BAK_<yyyymmdd> clones taken before a risky change (teardown.sql B0, rebuild/00), which stay. "
    "Suspends then drops TASK_BACKUP_OPERATOR (waiting up to ~10 minutes for an in-flight run, else stopping), "
    "drops SP_BACKUP_OPERATOR_TABLES, moves OPERATOR_BACKUP_LOG and the 25 weekly <T>_BAK_LAST copies into "
    "DBA_MAINT_DB.OVERWATCH_BAK and drops that schema once with every V158 generation (a preflight stops first if "
    "the schema holds a table, view, stage, sequence, file format, function, procedure, pipe, task, stream or "
    "alert V158 did not create; every drop is existence-gated, names come only from V158''s 25). Deletes "
    "SETTINGS BACKUP_KEEP_DAILY / BACKUP_KEEP_WEEKLY and the OPERATOR_BACKUP_DAILY freshness row, closes any open "
    "OPS_PIPELINE_DEGRADED stale event for it as EXPECTED, and re-derives V_SECURITY_EXCEPTION_QUEUE from V158 "
    "without the backup-prune carve-out (V151''s view text). Footprint (session pinned to DBA_MAINT_DB.OVERWATCH): "
    "3 CRITICAL CHANGE RISK rows (task, proc, schema) for 7 days, 4 if the permanent ledger cannot move; the "
    "renames score MEDIUM. No task created, nothing runs at apply time."
)
assert "\n" not in DESCRIPTION and "'" not in DESCRIPTION.replace("''", "")
assert len(DESCRIPTION.replace("''", "'")) <= 4000

SCHEMA_VERSION = (
    "\nINSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)\n"
    "SELECT 161 AS VERSION,\n"
    f"       '{DESCRIPTION}' AS DESCRIPTION\n"
    "WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 161);\n"
)

out = (HEADER + PREFLIGHT_BLOCK + SUSPEND + IN_FLIGHT_BLOCK + RETIRE_BLOCK + DATA + VIEW_HEAD + VIEW_MARKER + view
       + SCHEMA_VERSION)

# file post-conditions
assert out.count("CREATE OR REPLACE VIEW") == 1 and "CREATE OR REPLACE PROCEDURE" not in out
assert not re.search(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:TRANSIENT\s+)?(?:TABLE|TASK|SCHEMA|PROCEDURE|FUNCTION)\b", out)
assert "EXECUTE TASK" not in out and not re.search(r"^\s*CALL\b", out, re.M)
assert "RAISE EXCEPTION (" not in out and "DETAIL =" not in out
assert "LIKE '%_BAK" not in out and "_BAK%'" not in out           # exact names only, never a pattern sweep
assert out.count("-- >>> derived:") == 1
assert (out.index("\nUSE SCHEMA DBA_MAINT_DB.OVERWATCH;\n") < out.index("EXCEPTION (-20161")
        < out.index("EXCEPTION (-20611") < out.index("SUSPEND;")
        < out.index("EXCEPTION (-20612") < out.index("DROP TASK DBA_MAINT_DB") < out.index("DROP PROCEDURE DBA_MAINT_DB")
        < out.index("RENAME TO DBA_MAINT_DB.OVERWATCH_BAK.") < out.index("DROP SCHEMA DBA_MAINT_DB.OVERWATCH_BAK CASCADE")
        < out.index("DELETE FROM DBA_MAINT_DB.OVERWATCH.SETTINGS")
        < out.index("DELETE FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE")
        < out.index("UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS") < out.index("CREATE OR REPLACE VIEW")
        < out.index("SELECT 161 AS VERSION"))
assert "$_" not in out and "$v161$" not in out                     # plain $$ only (V089 lesson)

target = Path(os.environ.get("V161_OUT") or (MIG / "V161__retire_operator_backups.sql"))
target.write_text(out, encoding="utf-8", newline="\n")
print(f"wrote {target} ({len(out):,} chars)")

# ------------------------------------------------------------------------------------------------
# 3. PREFLIGHT_V161.sql (read-only; run before applying, paste every grid back)
# ------------------------------------------------------------------------------------------------
PREFLIGHT = f"""\
-- PREFLIGHT_V161.sql -- READ-ONLY preview of what V161 will retire. Changes nothing; run it in Snowsight before
-- applying V161 and paste every result grid back.

-- P1. OVERWATCH_BAK inventory by kind. FOREIGN rows are objects V158 did not create: V161 stops (-20611) on them.
SELECT CASE WHEN TABLE_TYPE = 'BASE TABLE' AND REGEXP_LIKE(TABLE_NAME, '{GEN_RE}')
                 THEN 'GENERATION_' || SUBSTR(TABLE_NAME, LENGTH(TABLE_NAME) - 8, 1)
            WHEN TABLE_TYPE = 'BASE TABLE' AND TABLE_NAME IN ({_in_list(MOVE, "                                                             ")})
                 THEN 'MOVED_BY_A_PARTIAL_V161'
            ELSE 'FOREIGN' END AS KIND,
       COUNT(*) AS OBJECTS,
       MIN(TABLE_NAME) AS FIRST_NAME, MAX(TABLE_NAME) AS LAST_NAME,
       SUM(BYTES) AS BYTES
  FROM DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES
 WHERE TABLE_SCHEMA = 'OVERWATCH_BAK'
 GROUP BY 1
 ORDER BY 1;

-- P2. Non-table objects in OVERWATCH_BAK: expect every count 0 and the three SHOWs empty (V161 stops (-20611)
--     otherwise; SHOW OBJECTS would list only tables and views). The schema's owner is the role that must
--     apply V161.
SHOW SCHEMAS LIKE 'OVERWATCH_BAK' IN DATABASE DBA_MAINT_DB;
SELECT 'STAGES' AS KIND, COUNT(*) AS OBJECTS FROM DBA_MAINT_DB.INFORMATION_SCHEMA.STAGES WHERE STAGE_SCHEMA = 'OVERWATCH_BAK'
UNION ALL
SELECT 'SEQUENCES', COUNT(*) FROM DBA_MAINT_DB.INFORMATION_SCHEMA.SEQUENCES WHERE SEQUENCE_SCHEMA = 'OVERWATCH_BAK'
UNION ALL
SELECT 'FILE_FORMATS', COUNT(*) FROM DBA_MAINT_DB.INFORMATION_SCHEMA.FILE_FORMATS WHERE FILE_FORMAT_SCHEMA = 'OVERWATCH_BAK'
UNION ALL
SELECT 'FUNCTIONS', COUNT(*) FROM DBA_MAINT_DB.INFORMATION_SCHEMA.FUNCTIONS WHERE FUNCTION_SCHEMA = 'OVERWATCH_BAK'
UNION ALL
SELECT 'PROCEDURES', COUNT(*) FROM DBA_MAINT_DB.INFORMATION_SCHEMA.PROCEDURES WHERE PROCEDURE_SCHEMA = 'OVERWATCH_BAK'
UNION ALL
SELECT 'PIPES', COUNT(*) FROM DBA_MAINT_DB.INFORMATION_SCHEMA.PIPES WHERE PIPE_SCHEMA = 'OVERWATCH_BAK'
ORDER BY 1;
SHOW TASKS IN SCHEMA DBA_MAINT_DB.OVERWATCH_BAK;
SHOW STREAMS IN SCHEMA DBA_MAINT_DB.OVERWATCH_BAK;
SHOW ALERTS IN SCHEMA DBA_MAINT_DB.OVERWATCH_BAK;

-- P3. The 26 names V161 moves out of OVERWATCH (expect 26 rows once V158 has run on a Sunday; IS_TRANSIENT YES
--     for the 25 weekly copies), and the manual <T>_BAK_<yyyymmdd> clones V161 must leave alone (a baseline for
--     PART B item 5).
SELECT IFF(TABLE_NAME IN ({_in_list(MOVE, "                          ")}),
           'RETIRED_BY_V161', 'MANUAL_CLONE_KEPT') AS KIND,
       COUNT(*) AS TABLES, COUNT_IF(IS_TRANSIENT = 'YES') AS TRANSIENT_TABLES,
       MIN(TABLE_NAME) AS FIRST_NAME, MAX(TABLE_NAME) AS LAST_NAME
  FROM DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES
 WHERE TABLE_SCHEMA = 'OVERWATCH' AND TABLE_TYPE = 'BASE TABLE'
   AND (TABLE_NAME IN ({_in_list(MOVE, "                        ")})
        OR REGEXP_LIKE(TABLE_NAME, '.+_BAK_[0-9]{{8}}'))
 GROUP BY 1
 ORDER BY 1;

-- P4. The retention SETTINGS, the freshness row and any open stale event V161 deletes / closes.
SELECT 'SETTING ' || KEY AS ITEM, VALUE, NULL AS NEWEST
  FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
 WHERE KEY IN ('BACKUP_KEEP_DAILY', 'BACKUP_KEEP_WEEKLY')
UNION ALL
SELECT 'FRESHNESS ' || SOURCE_NAME, STATUS, LAST_LOAD_TS::VARCHAR
  FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
 WHERE SOURCE_NAME = 'OPERATOR_BACKUP_DAILY'
UNION ALL
SELECT 'OPEN STALE EVENT ' || STATUS, COUNT(*)::VARCHAR, MAX(DEDUPE_KEY)
  FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
 WHERE RULE_ID = 'OPS_PIPELINE_DEGRADED'
   AND STARTSWITH(DEDUPE_KEY, 'OPS_PIPELINE_DEGRADED|STALE|OPERATOR_BACKUP_DAILY|')
   AND STATUS IN ('OPEN', 'ACK', 'SNOOZED')
 GROUP BY STATUS
ORDER BY 1;

-- P5. The task's recent runs and its next scheduled one: apply V161 away from a run (not ~05:05-05:20 Central).
SELECT NAME, STATE, SCHEDULED_TIME, COMPLETED_TIME, LEFT(ERROR_MESSAGE, 120) AS ERROR_MESSAGE
  FROM TABLE(DBA_MAINT_DB.INFORMATION_SCHEMA.TASK_HISTORY(
           SCHEDULED_TIME_RANGE_START => DATEADD('hour', -30, CURRENT_TIMESTAMP()),
           TASK_NAME => 'TASK_BACKUP_OPERATOR'))
 ORDER BY SCHEDULED_TIME DESC;

-- P6. LAST, on its own: the backup ledger. PRUNED / PRUNE_FAILED expected 0 (no prune has run, so the view
--     carve-out V161 removes never matched a row). After a partial V161 the ledger has moved into OVERWATCH_BAK
--     (P1 shows it), so this one statement errors; the grids above are unaffected.
SELECT ACTION, COUNT(*) AS ROWS_, MAX(LOGGED_AT) AS NEWEST
  FROM DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
 GROUP BY ACTION
 ORDER BY 1;
"""

# ------------------------------------------------------------------------------------------------
# 4. PART B verify grid (read-only; after applying V161)
# ------------------------------------------------------------------------------------------------
PART_B = f"""\
-- PART B -- V161 verify (READ-ONLY). Every CHECK should read OK; paste the grids back.
SELECT 'V161.1 SCHEMA_VERSION has 161' AS CHECK_NAME,
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 161) = 1,
           'OK', 'FAIL: V161 did not finish') AS RESULT
UNION ALL
SELECT 'V161.2 SP_BACKUP_OPERATOR_TABLES dropped',
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.INFORMATION_SCHEMA.PROCEDURES
             WHERE PROCEDURE_SCHEMA = 'OVERWATCH' AND PROCEDURE_NAME = 'SP_BACKUP_OPERATOR_TABLES') = 0,
           'OK', 'FAIL: the proc still exists')
UNION ALL
SELECT 'V161.3 OVERWATCH_BAK schema dropped',
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = 'OVERWATCH_BAK') = 0,
           'OK', 'FAIL: the schema still exists')
UNION ALL
SELECT 'V161.4 ledger + 25 weekly copies gone from OVERWATCH',
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES
             WHERE TABLE_SCHEMA = 'OVERWATCH'
               AND TABLE_NAME IN ({_in_list(MOVE, "                                  ")})) = 0,
           'OK', 'FAIL: a retired table is still in OVERWATCH')
UNION ALL
SELECT 'V161.5 manual <T>_BAK_<yyyymmdd> clones kept (compare with PREFLIGHT P3)',
       (SELECT COUNT(*)::VARCHAR || ' manual clone(s)' FROM DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES
         WHERE TABLE_SCHEMA = 'OVERWATCH' AND TABLE_TYPE = 'BASE TABLE'
           AND REGEXP_LIKE(TABLE_NAME, '.+_BAK_[0-9]{{8}}'))
UNION ALL
SELECT 'V161.6 BACKUP_KEEP_* settings deleted',
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.SETTINGS
             WHERE KEY IN ('BACKUP_KEEP_DAILY', 'BACKUP_KEEP_WEEKLY')) = 0,
           'OK', 'FAIL: a BACKUP_KEEP_* row is back (an Admin save from app 4.597 or older)')
UNION ALL
SELECT 'V161.7 OPERATOR_BACKUP_DAILY freshness row gone (re-run ~10 min later)',
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
             WHERE SOURCE_NAME = 'OPERATOR_BACKUP_DAILY') = 0,
           'OK', 'FAIL: the row is back -- an in-flight run re-created it; re-run V161')
UNION ALL
SELECT 'V161.8 no open stale event for OPERATOR_BACKUP_DAILY',
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
             WHERE RULE_ID = 'OPS_PIPELINE_DEGRADED'
               AND STARTSWITH(DEDUPE_KEY, 'OPS_PIPELINE_DEGRADED|STALE|OPERATOR_BACKUP_DAILY|')
               AND STATUS IN ('OPEN', 'ACK', 'SNOOZED')) = 0,
           'OK', 'FAIL: an open stale event remains')
UNION ALL
SELECT 'V161.9 exception-queue view has no carve-out and keeps V151',
       IFF(NOT CONTAINS(GET_DDL('VIEW', 'DBA_MAINT_DB.OVERWATCH.V_SECURITY_EXCEPTION_QUEUE'), 'OVERWATCH_BAK')
           AND CONTAINS(GET_DDL('VIEW', 'DBA_MAINT_DB.OVERWATCH.V_SECURITY_EXCEPTION_QUEUE'), 'DROP BACKUP POLICY %')
           AND CONTAINS(GET_DDL('VIEW', 'DBA_MAINT_DB.OVERWATCH.V_SECURITY_EXCEPTION_QUEUE'), 'TF~_%'),
           'OK', 'FAIL: unexpected view text')
UNION ALL
SELECT 'V161.10 the view compiles',
       (SELECT COUNT(*)::VARCHAR || ' queue row(s)' FROM DBA_MAINT_DB.OVERWATCH.V_SECURITY_EXCEPTION_QUEUE)
UNION ALL
SELECT 'V161.11 no backup errors since apply (race evidence)',
       IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
             WHERE PAGE = 'BackupOperatorTables' AND LOGGED_AT >= DATEADD('hour', -2, CURRENT_TIMESTAMP())) = 0,
           'OK', 'CHECK: a backup run overlapped the apply -- re-check V161.7');

-- V161.12 the task is gone: expect 0 rows.
SHOW TASKS LIKE 'TASK_BACKUP_OPERATOR' IN SCHEMA DBA_MAINT_DB.OVERWATCH;

-- V161.13 about 2h after apply (FACT_SECURITY_CHANGE loads hourly): the footprint. Expect 3 DESTRUCTIVE /
--         CRITICAL rows (task, proc, schema) and MEDIUM ALTER rows for the renames; a move that fell back to a
--         DROP (likeliest: the permanent OPERATOR_BACKUP_LOG) shows as one more CRITICAL row. Never delete these.
SELECT CHANGE_KIND, RISK_LEVEL, COUNT(*) AS ROWS_SINCE_APPLY, MIN(LEFT(QUERY_PREVIEW, 90)) AS EXAMPLE
  FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE
 WHERE EVENT_TS >= DATEADD('hour', -3, CURRENT_TIMESTAMP())
   AND (CONTAINS(UPPER(QUERY_PREVIEW), 'OVERWATCH_BAK')
        OR CONTAINS(UPPER(QUERY_PREVIEW), 'TASK_BACKUP_OPERATOR')
        OR CONTAINS(UPPER(QUERY_PREVIEW), 'SP_BACKUP_OPERATOR_TABLES')
        OR CONTAINS(UPPER(QUERY_PREVIEW), 'OPERATOR_BACKUP_LOG')
        OR CONTAINS(UPPER(QUERY_PREVIEW), '_BAK_LAST'))
 GROUP BY 1, 2
 ORDER BY 1, 2;
"""

_pf = os.environ.get("PREFLIGHT_OUT")
if _pf:
    Path(_pf).write_text(PREFLIGHT, encoding="utf-8", newline="\n")
    print(f"wrote PREFLIGHT {_pf}")
_pb = os.environ.get("PART_B_OUT")
if _pb:
    Path(_pb).write_text(PART_B, encoding="utf-8", newline="\n")
    print(f"wrote PART B {_pb}")
