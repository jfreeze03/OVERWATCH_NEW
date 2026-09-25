#!/usr/bin/env python3
"""Forward-generate V158: operator-data backups rotate DAILY into dated generations (Next-Fifty #32).

Before: SP_BACKUP_OPERATOR_TABLES (current definer V089) overwrote ONE ``<T>_BAK_LAST`` clone per
operator table every Sunday. A bad bulk edit noticed a day later could lose up to a week, and after the
next Sunday the only backup already held the corruption.

After (plan section 1 "V157 -- rank 32", renumbered V158 because wave 2a inserted V155):
  * a dedicated TRANSIENT schema DBA_MAINT_DB.OVERWATCH_BAK (decision O-12). roles.sql grants FUTURE
    TABLES only in OVERWATCH, so generations there cause no grant/revoke churn (recent_grant_changes,
    the RCA feed, unused-grant candidates) and survive a DROP of the OVERWATCH schema;
  * every day at 05:10 Central (O-13) each of the 25 V089 tables is cloned to an immutable TRANSIENT
    ``OVERWATCH_BAK.<T>_OWBAK_D<yyyymmdd>`` (Central day); on Sundays a ``_OWBAK_W<yyyymmdd>`` is cloned
    from that D generation inside OVERWATCH_BAK, and V089's own ``<T>_BAK_LAST`` statement runs unchanged
    (so the OVERWATCH-side cadence and grant churn stay weekly, exactly as today);
  * backup-vs-source row counts land in the new OPERATOR_BACKUP_LOG;
  * a rank-based prune keeps SETTINGS BACKUP_KEEP_DAILY (14) / BACKUP_KEEP_WEEKLY (8) per table and kind
    (floors 7 / 4, ceilings 60 / 52), only for TRANSIENT base tables in OVERWATCH_BAK whose WHOLE name is
    one of the 25 + ``_OWBAK_[DW]`` + 8 digits, dated before today, re-checked right before each DROP;
  * SOURCE_FRESHNESS_STATE 'OPERATOR_BACKUP_DAILY' (DAILY in the name = the shared 30h cadence rule)
    advances only on a run with zero clone failures, Central-pinned.

The prune's daily DROPs would score DESTRUCTIVE 100 / CRITICAL in FACT_SECURITY_CHANGE, so the SAME file
re-derives V_SECURITY_EXCEPTION_QUEUE from V151 (its current definer) with ONE carve-out clause keyed on
USER_NAME 'SYSTEM' (probe F5 default) AND the exact generated DROP text. The first prune happens 15 days
after apply, in the same file as the carve-out, so there is never a flood window.

Two re-derived objects, both with count == 1 anchors and markers (tests/test_proc_lineage.py):
  * the proc keeps V089's prefix through ``    i INT;\\n`` byte-identical (signature, EXECUTE AS OWNER,
    the 25-name array, the declarations) and V089's ``_BAK_LAST`` statement block verbatim (re-indented);
  * the view is V151's byte-for-byte plus the one clause (the V158 test folds it back out).
This generator stays self-contained (no app import), per house style.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE_PROC = MIG / "V089__backup_transient_clone.sql"
BASE_VIEW = MIG / "V151__security_change_risk_identity_policy_drops.sql"

# The 25 operator tables, in V089's array order (asserted against the carried literal below).
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

# ------------------------------------------------------------------------------------------------
# 1. SP_BACKUP_OPERATOR_TABLES (base V089)
# ------------------------------------------------------------------------------------------------
_procs = re.findall(
    r"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.SP_BACKUP_OPERATOR_TABLES\(\).*?\n\$\$;\n",
    BASE_PROC.read_text(encoding="utf-8"), re.S)
assert len(_procs) == 1, f"SP_BACKUP_OPERATOR_TABLES (V089): expected 1 definition, got {len(_procs)}"
base_proc = _procs[0]

# Byte-identical carry: signature, EXECUTE AS OWNER, the 25-name ARRAY and the 4 declarations.
assert base_proc.count("BEGIN\n") == 2, "V089 has one outer and one per-table BEGIN"
carried = base_proc.split("BEGIN\n", 1)[0]
assert carried.endswith("    i INT;\n"), "the V089 carry must end at the last declaration"
assert tuple(re.findall(r"'([A-Z_]+)'", carried.split("tables ARRAY DEFAULT [", 1)[1].split("]", 1)[0])) \
    == TABLES, "TABLES drifted from V089's array literal"

# V089's TRANSIENT *_BAK_LAST statement (with its explanatory comment), carried verbatim: it moves one
# block deeper (inside IF (is_sunday) THEN), so only the leading indentation changes.
V089_BAK_LAST = (
    "            -- V089: TRANSIENT target -- a transient source (ALERT_EVENTS,\n"
    "            -- ACTION_QUEUE, ...) cannot clone into a PERMANENT table\n"
    "            -- (\"Transient object cannot be cloned to a permanent object\"),\n"
    "            -- which failed those backups every run. TRANSIENT works for both\n"
    "            -- transient and permanent sources and needs no Fail-safe.\n"
    "            EXECUTE IMMEDIATE 'CREATE OR REPLACE TRANSIENT TABLE DBA_MAINT_DB.OVERWATCH.' || :tname ||\n"
    "                              '_BAK_LAST CLONE DBA_MAINT_DB.OVERWATCH.' || :tname;\n"
)
assert base_proc.count(V089_BAK_LAST) == 1, "V089 _BAK_LAST anchor must occur exactly once"
BAK_LAST_SUNDAY = "".join("        " + ln + "\n" for ln in V089_BAK_LAST.splitlines())

NEW_DECLS = """\
    -- V158: dated generations in DBA_MAINT_DB.OVERWATCH_BAK, keep-count prune, row-count log,
    -- freshness stamp.
    failed INT DEFAULT 0;          -- clone failures: they hold the freshness stamp
    missing INT DEFAULT 0;         -- source table absent on this install: a skip, never a failure
    pruned INT DEFAULT 0;
    prune_failed INT DEFAULT 0;
    present INT DEFAULT 0;
    keep_d FLOAT DEFAULT 14;
    keep_w FLOAT DEFAULT 8;
    day_ct DATE;
    gen_d VARCHAR;
    gen_w VARCHAR;
    is_sunday BOOLEAN DEFAULT FALSE;
    run_id VARCHAR;
    prune_re VARCHAR;
    pname VARCHAR;
    total_rows NUMBER(38,0) DEFAULT 0;
    fstatus VARCHAR;
    res RESULTSET;
"""

BODY = """\
BEGIN
    run_id := UUID_STRING();
    -- Retention from SETTINGS (Admin-editable; seeded 14 / 8 above). A missing or non-numeric value
    -- falls back to the default; the floors (7 daily / 4 weekly) mean no value can prune the history
    -- away, and the ceilings (60 / 52) bound storage.
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'BACKUP_KEEP_DAILY', VALUE, NULL))), 14),
           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'BACKUP_KEEP_WEEKLY', VALUE, NULL))), 8)
      INTO :keep_d, :keep_w
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;
    keep_d := LEAST(GREATEST(ROUND(keep_d), 7), 60);
    keep_w := LEAST(GREATEST(ROUND(keep_w), 4), 52);

    -- Generation key = the America/Chicago calendar day (TIMEZONE STANDARD), never the session zone.
    day_ct := CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE;
    gen_d := 'D' || TO_CHAR(day_ct, 'YYYYMMDD');
    gen_w := 'W' || TO_CHAR(day_ct, 'YYYYMMDD');
    is_sunday := (DAYOFWEEKISO(day_ct) = 7);
    -- The ONLY names the prune may ever touch: one of the 25 + _OWBAK_ + D/W + 8 digits. REGEXP_LIKE
    -- anchors the whole name, so a manual <T>_BAK_<yyyymmdd> DR clone can never match.
    prune_re := '(' || ARRAY_TO_STRING(:tables, '|') || ')_OWBAK_[DW][0-9]{8}';

    FOR i IN 0 TO ARRAY_SIZE(:tables) - 1 DO
        tname := GET(:tables, i)::VARCHAR;
        -- Source present on this install? Fails OPEN: if the metadata probe itself errors, the clone
        -- is attempted exactly as V089 did (a missing source then lands as clone_failed).
        present := 1;
        BEGIN
            SELECT COUNT(*) INTO :present
              FROM DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES
             WHERE TABLE_SCHEMA = 'OVERWATCH' AND TABLE_NAME = :tname
               AND TABLE_TYPE = 'BASE TABLE';
        EXCEPTION
            WHEN OTHER THEN
                present := 1;
        END;
        IF (present = 0) THEN
            missing := missing + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
                (RUN_ID, GENERATION, SOURCE_TABLE, ACTION, DETAIL)
            SELECT :run_id, :gen_d, :tname, 'SKIPPED_MISSING', 'source table absent on this install';
        ELSE
            BEGIN
                -- V158: the daily generation, immutable once taken (IF NOT EXISTS), in the dedicated
                -- TRANSIENT schema OVERWATCH_BAK (no FUTURE grants there, so no grant churn).
                EXECUTE IMMEDIATE 'CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH_BAK.' || :tname ||
                                  '_OWBAK_' || :gen_d || ' CLONE DBA_MAINT_DB.OVERWATCH.' || :tname;
                IF (is_sunday) THEN
                    -- Sundays: the weekly generation, cloned from today's daily one inside OVERWATCH_BAK,
                    -- and the V089 *_BAK_LAST pointer in OVERWATCH, still weekly (statement unchanged).
                    EXECUTE IMMEDIATE 'CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH_BAK.' || :tname ||
                                      '_OWBAK_' || :gen_w || ' CLONE DBA_MAINT_DB.OVERWATCH_BAK.' || :tname ||
                                      '_OWBAK_' || :gen_d;
__BAK_LAST__                END IF;
                done := done + 1;
            EXCEPTION
                WHEN OTHER THEN
                    emsg := SQLERRM;
                    failed := failed + 1;
                    INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                        (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                    SELECT 'BackupOperatorTables', 'clone_failed', LEFT(:emsg, 2000),
                           'table ' || :tname || ' generation ' || :gen_d || ' (OPERATOR_BACKUP_DAILY)', CURRENT_ROLE();
                    INSERT INTO DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
                        (RUN_ID, GENERATION, SOURCE_TABLE, ACTION, DETAIL)
                    SELECT :run_id, :gen_d, :tname, 'CLONE_FAILED', LEFT(:emsg, 1000);
            END;
        END IF;
    END FOR;

    -- Row counts: INFORMATION_SCHEMA metadata only, no table scan. One CLONED row per OVERWATCH_BAK
    -- generation taken today (the daily D; on Sundays also the weekly W), backup vs source, so a
    -- restore can pick any kept generation on evidence (the Sunday *_BAK_LAST pointer is not logged).
    -- Isolated: a failure here never blocks the prune, the freshness stamp or the RETURN.
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
            (RUN_ID, GENERATION, SOURCE_TABLE, BACKUP_TABLE, ACTION, ROW_COUNT, SOURCE_ROW_COUNT, BYTES)
        SELECT :run_id, :gen_d, s.TABLE_NAME, b.TABLE_NAME, 'CLONED', b.ROW_COUNT, s.ROW_COUNT, b.BYTES
        FROM DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES b
        JOIN DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES s
          ON s.TABLE_SCHEMA = 'OVERWATCH' AND s.TABLE_TYPE = 'BASE TABLE'
         AND b.TABLE_NAME = s.TABLE_NAME || '_OWBAK_' || :gen_d
        WHERE b.TABLE_SCHEMA = 'OVERWATCH_BAK'
          AND REGEXP_LIKE(b.TABLE_NAME, :prune_re);
        IF (is_sunday) THEN
            -- The weekly generation outlives its D twin (kept in weeks, not days), so it gets its own
            -- CLONED row: once the D generation is pruned, the log still names a table that exists.
            INSERT INTO DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
                (RUN_ID, GENERATION, SOURCE_TABLE, BACKUP_TABLE, ACTION, ROW_COUNT, SOURCE_ROW_COUNT, BYTES)
            SELECT :run_id, :gen_w, s.TABLE_NAME, b.TABLE_NAME, 'CLONED', b.ROW_COUNT, s.ROW_COUNT, b.BYTES
            FROM DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES b
            JOIN DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES s
              ON s.TABLE_SCHEMA = 'OVERWATCH' AND s.TABLE_TYPE = 'BASE TABLE'
             AND b.TABLE_NAME = s.TABLE_NAME || '_OWBAK_' || :gen_w
            WHERE b.TABLE_SCHEMA = 'OVERWATCH_BAK'
              AND REGEXP_LIKE(b.TABLE_NAME, :prune_re);
        END IF;
        -- The freshness ROW_COUNT is the daily generation's rows only (never doubled on a Sunday).
        SELECT COALESCE(SUM(ROW_COUNT), 0) INTO :total_rows
          FROM DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
         WHERE RUN_ID = :run_id AND ACTION = 'CLONED' AND GENERATION = :gen_d;
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'BackupOperatorTables', 'backup_log_failed', LEFT(:emsg, 2000),
                   'row-count log ' || :gen_d || ' (OPERATOR_BACKUP_DAILY): generations taken, counts not logged', CURRENT_ROLE();
    END;

    -- Prune: keep the newest keep_d daily / keep_w weekly generations per table and kind (rank-based,
    -- so a paused task never empties the history; today's generation is never a candidate). Only
    -- TRANSIENT base tables in DBA_MAINT_DB.OVERWATCH_BAK whose whole name matches prune_re, and the
    -- regex is re-checked right before each DROP. Isolated like the log above.
    BEGIN
        res := (
            SELECT g.TABLE_NAME
            FROM (
                SELECT TABLE_NAME,
                       LEFT(TABLE_NAME, LENGTH(TABLE_NAME) - 16) AS BASE_NAME,
                       SUBSTR(TABLE_NAME, LENGTH(TABLE_NAME) - 8, 1) AS GEN_KIND,
                       TRY_TO_DATE(RIGHT(TABLE_NAME, 8), 'YYYYMMDD') AS GEN_DAY
                FROM DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES
                WHERE TABLE_CATALOG = 'DBA_MAINT_DB'
                  AND TABLE_SCHEMA = 'OVERWATCH_BAK'
                  AND TABLE_TYPE = 'BASE TABLE'
                  AND IS_TRANSIENT = 'YES'
                  AND REGEXP_LIKE(TABLE_NAME, :prune_re)
            ) g
            WHERE g.GEN_DAY IS NOT NULL
            QUALIFY g.GEN_DAY < :day_ct
                AND ROW_NUMBER() OVER (PARTITION BY g.BASE_NAME, g.GEN_KIND ORDER BY g.GEN_DAY DESC)
                    > IFF(g.GEN_KIND = 'D', :keep_d, :keep_w)
            ORDER BY g.TABLE_NAME
        );
        LET c_prune CURSOR FOR res;
        FOR r IN c_prune DO
            pname := r.TABLE_NAME;
            IF (REGEXP_LIKE(pname, prune_re)) THEN   -- second check right before the DROP
                BEGIN
                    EXECUTE IMMEDIATE 'DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH_BAK.' || :pname;
                    pruned := pruned + 1;
                    INSERT INTO DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
                        (RUN_ID, GENERATION, SOURCE_TABLE, BACKUP_TABLE, ACTION)
                    SELECT :run_id, RIGHT(:pname, 9), LEFT(:pname, LENGTH(:pname) - 16), :pname, 'PRUNED';
                EXCEPTION
                    WHEN OTHER THEN
                        emsg := SQLERRM;
                        prune_failed := prune_failed + 1;
                        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                            (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                        SELECT 'BackupOperatorTables', 'backup_prune_failed', LEFT(:emsg, 2000),
                               'table ' || :pname || ' (OPERATOR_BACKUP_DAILY)', CURRENT_ROLE();
                        INSERT INTO DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
                            (RUN_ID, GENERATION, SOURCE_TABLE, BACKUP_TABLE, ACTION, DETAIL)
                        SELECT :run_id, RIGHT(:pname, 9), LEFT(:pname, LENGTH(:pname) - 16), :pname,
                               'PRUNE_FAILED', LEFT(:emsg, 1000);
                END;
            END IF;
        END FOR;
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            prune_failed := prune_failed + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'BackupOperatorTables', 'backup_prune_failed', LEFT(:emsg, 2000),
                   'prune scan ' || :gen_d || ' (OPERATOR_BACKUP_DAILY): every generation kept this run', CURRENT_ROLE();
    END;

    -- The log trims itself (SP_PURGE_FACTS is untouched).
    DELETE FROM DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG
     WHERE LOGGED_AT < DATEADD('day', -400, CURRENT_TIMESTAMP());

    -- Freshness (V068 idiom, Central-pinned). LAST_LOAD_TS advances only on a run with zero clone
    -- failures (V066 #11), so a failing or suspended backup goes stale and the dead-man paths fire.
    -- The name carries DAILY, so every name-based cadence rule judges it at 30h.
    fstatus := 'backup ' || gen_d || ': ' || done || ' cloned, ' || failed || ' failed, ' ||
               missing || ' missing, ' || pruned || ' pruned, ' || prune_failed || ' prune failed';
    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'OPERATOR_BACKUP_DAILY' AS SOURCE_NAME,
               IFF(:failed = 0 AND :done > 0, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ, NULL) AS RUN_TS,
               :total_rows AS ROW_COUNT,
               LEFT(:fstatus, 400) AS STATUS
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = COALESCE(s.RUN_TS, t.LAST_LOAD_TS),
        ROW_COUNT = s.ROW_COUNT,
        SNAPSHOT_TS = CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ,
        GENERATION = COALESCE(t.GENERATION, 0) + 1, STATUS = s.STATUS
    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, SNAPSHOT_TS, GENERATION, STATUS)
    VALUES (s.SOURCE_NAME, s.RUN_TS, s.ROW_COUNT,
            CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ, 1, s.STATUS);

    IF (failed > 0) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
            (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
        SELECT 'BackupOperatorTables', 'backup_incomplete', LEFT(:fstatus, 2000),
               'OPERATOR_BACKUP_DAILY ' || :gen_d || ': freshness stamp held until a clean run; see OPERATOR_BACKUP_LOG',
               CURRENT_ROLE();
    END IF;

    RETURN 'cloned ' || :done || ' operator table(s) to OVERWATCH_BAK.*_OWBAK_' || :gen_d ||
           IFF(:is_sunday, ' (+ weekly ' || :gen_w || ' and *_BAK_LAST)', '') || '; ' || :failed || ' failed, ' ||
           :missing || ' missing, ' || :pruned || ' pruned, ' || :prune_failed || ' prune failed';
END;
$$;
"""
assert BODY.count("__BAK_LAST__") == 1
proc = carried + NEW_DECLS + BODY.replace("__BAK_LAST__", BAK_LAST_SUNDAY, 1)

# proc post-conditions
assert proc.startswith(carried)
assert proc.count("CREATE OR REPLACE TRANSIENT TABLE DBA_MAINT_DB.OVERWATCH.' || :tname") == 1
assert proc.count("EXECUTE IMMEDIATE 'DROP") == 1
assert proc.count("'DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH_BAK.' || :pname") == 1
assert "CALL " not in proc and "$$" not in proc[proc.index("$$") + 2:-4]
_sunday = proc[proc.index("                IF (is_sunday) THEN\n"):proc.index("                END IF;\n")]
assert BAK_LAST_SUNDAY in _sunday, "_BAK_LAST must sit inside IF (is_sunday)"
assert "'_OWBAK_' || :gen_w || ' CLONE" in _sunday, "the W generation is cloned on Sundays only"
assert "'_OWBAK_' || :gen_d || ' CLONE" not in _sunday, "the D generation is cloned every day"
_log = proc[proc.index("    -- Row counts:"):proc.index("    -- Prune:")]
assert _log.count("'CLONED', b.ROW_COUNT, s.ROW_COUNT, b.BYTES") == 2
_log_sunday = _log[_log.index("        IF (is_sunday) THEN\n"):_log.index("        END IF;\n")]
assert "SELECT :run_id, :gen_w, s.TABLE_NAME" in _log_sunday and ":gen_d" not in _log_sunday, \
    "the W CLONED row is logged on Sundays only, keyed on gen_w"
assert "WHERE RUN_ID = :run_id AND ACTION = 'CLONED' AND GENERATION = :gen_d;" in _log, \
    "the freshness ROW_COUNT sums the daily generation only"

# ------------------------------------------------------------------------------------------------
# 2. V_SECURITY_EXCEPTION_QUEUE (base V151): + the OVERWATCH_BAK backup-prune carve-out
# ------------------------------------------------------------------------------------------------
_views = re.findall(
    r"CREATE OR REPLACE VIEW DBA_MAINT_DB\.OVERWATCH\.V_SECURITY_EXCEPTION_QUEUE AS.*?;\n",
    BASE_VIEW.read_text(encoding="utf-8"), re.S)
assert len(_views) == 1, f"V_SECURITY_EXCEPTION_QUEUE (V151): expected 1 view, got {len(_views)}"
view = _views[0]

VIEW_ANCHOR = "'DROP STORAGE LIFECYCLE POLICY %', 'DROP BACKUP POLICY %')))\n), open_actions AS ("
# Probe F5 default: task-run statements are recorded as USER_NAME 'SYSTEM'. If F5 shows otherwise, key
# the second line on the task-owner ROLE_NAME instead (UPPER(COALESCE(ROLE_NAME, '')) = '<role>').
CARVE_OUT = (
    "      -- V158 (Next-Fifty #32): the backup-generation prune of OVERWATCH itself (task-run as SYSTEM,\n"
    "      -- the exact generated DROP only). A human DROP of a backup or any other drop still surfaces.\n"
    "      AND NOT (CHANGE_KIND = 'DESTRUCTIVE'\n"
    "               AND UPPER(COALESCE(USER_NAME, '')) = 'SYSTEM'\n"
    "               AND REGEXP_LIKE(COALESCE(QUERY_PREVIEW, ''),\n"
    "                   'DROP TABLE IF EXISTS DBA_MAINT_DB[.]OVERWATCH_BAK[.][A-Z0-9_]+_OWBAK_[DW][0-9]{8}'))"
)
assert ";" not in CARVE_OUT
assert "'" not in "".join(ln for ln in CARVE_OUT.splitlines() if ln.strip().startswith("--"))
assert view.count(VIEW_ANCHOR) == 1, "V151 view anchor must occur exactly once"
view = view.replace(
    VIEW_ANCHOR,
    VIEW_ANCHOR.replace("\n), open_actions AS (", "\n" + CARVE_OUT + "\n), open_actions AS ("),
    1)
assert view.count("AND NOT (CHANGE_KIND = 'DESTRUCTIVE'") == 2      # V151's exclusion + the carve-out
assert view.count("_OWBAK_") == 1

# ------------------------------------------------------------------------------------------------
# 3. the file
# ------------------------------------------------------------------------------------------------
HEADER = """\
-- V158__operator_backup_generations.sql
--
-- Next-Fifty #32: operator-data backups rotate DAILY into dated generations instead of one weekly
-- *_BAK_LAST overwrite (a bad bulk edit noticed a day later could lose up to a week; after the next
-- Sunday the only backup already held the corruption).
--
-- * New TRANSIENT schema DBA_MAINT_DB.OVERWATCH_BAK (owner-only). roles.sql grants FUTURE TABLES
--   only in OVERWATCH, so generations here add no grant/revoke churn to OVERWATCH (recent grant
--   changes, the RCA feed, unused-grant candidates, tag coverage) and survive a drop of OVERWATCH.
-- * SP_BACKUP_OPERATOR_TABLES re-derived from V089 (its current definer): every day at 05:10 Central
--   each of the 25 operator tables is cloned to an immutable TRANSIENT
--   OVERWATCH_BAK.<T>_OWBAK_D<yyyymmdd> (Central day). Sundays also clone <T>_OWBAK_W<yyyymmdd> from
--   that D generation and run the V089 <T>_BAK_LAST statement unchanged (still weekly). A table
--   missing on this install is a logged skip, not a failure.
-- * Backup-vs-source row counts go to the new OPERATOR_BACKUP_LOG, one CLONED row per generation (the
--   daily D; Sundays also the weekly W). The proc trims the log at 400 days.
-- * Prune: newest SETTINGS BACKUP_KEEP_DAILY (14) / BACKUP_KEEP_WEEKLY (8) generations per table and
--   kind (floors 7 / 4, ceilings 60 / 52). Only TRANSIENT base tables in OVERWATCH_BAK whose whole
--   name is one of the 25 + _OWBAK_[DW] + 8 digits, dated before today, re-checked before each DROP.
--   The manual <T>_BAK_<yyyymmdd> DR clones (teardown.sql B0, rebuild/00) use a different token.
-- * SOURCE_FRESHNESS_STATE 'OPERATOR_BACKUP_DAILY' (30h cadence by name) advances only on a run with
--   zero clone failures, so a failing or suspended backup goes stale and the dead-man paths fire.
-- * TASK_BACKUP_OPERATOR was created IF NOT EXISTS (V015), so its schedule moves in place
--   (SUSPEND / SET SCHEDULE / RESUME) from Sunday 05:40 to daily 05:10 Central.
-- * V_SECURITY_EXCEPTION_QUEUE re-derived from V151 (its current definer) with ONE carve-out: the
--   prune DROPs score DESTRUCTIVE 100 / CRITICAL, so a DROP run by the task (USER_NAME SYSTEM) whose
--   statement is exactly the generated OVERWATCH_BAK generation DROP leaves the CHANGE RISK queue.
--   The first prune happens 15 days after apply, so the carve-out is always in place first.
--
-- Restore = INSERT OVERWRITE as the table-owner role (RUNBOOK section 16): a TRANSIENT backup cannot
-- CLONE back into a permanent table, and a CLONE restore would re-apply the schema FUTURE grants.
-- The tail starts the first generation through the TASK (asynchronous; it runs as SYSTEM, inside the
-- carve-out, keyed on the Central day whatever the worksheet zone). Nothing is pruned on day 1.
-- DR replay (schema gone, or a factory reset): apply V001..V157, restore the operator tables
-- (SETTINGS first), THEN this file. Its tail backs up whatever the tables hold and prunes with the
-- retention SETTINGS holds (RUNBOOK section 16 step 3).
-- Owner applies in Snowsight after V157. This file never runs from the app.
-- Rollback: V089:27-71 proc, ALTER TASK ... SET SCHEDULE = 'USING CRON 40 5 * * 0 America/Chicago',
-- and the V151 view.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20158, 'V158 requires V157 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 157) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- Dedicated backup schema (decision O-12). TRANSIENT: generations need no Fail-safe. No grants: the
-- proc owner (the role applying this file) owns every generation and runs every restore.
CREATE TRANSIENT SCHEMA IF NOT EXISTS DBA_MAINT_DB.OVERWATCH_BAK COMMENT = 'OVERWATCH operator-data backup generations (V158). Owner-only; never dropped by teardown.';

-- Retention (decision O-13), Admin-editable within 7-60 / 4-52. WHEN NOT MATCHED only: an
-- operator's edited value is never overwritten.
MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS t
USING (
    SELECT * FROM VALUES
        ('BACKUP_KEEP_DAILY', '14'),
        ('BACKUP_KEEP_WEEKLY', '8')
    AS s(KEY, VALUE)
) s
ON t.KEY = s.KEY
WHEN NOT MATCHED THEN INSERT (KEY, VALUE) VALUES (s.KEY, s.VALUE);

-- Backup ledger (operator history: teardown lists it commented, never a live drop).
-- ACTION: CLONED / SKIPPED_MISSING / CLONE_FAILED / PRUNED / PRUNE_FAILED.
CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG (
    RUN_ID           VARCHAR(64)   NOT NULL,
    GENERATION       VARCHAR(16)   NOT NULL,
    SOURCE_TABLE     VARCHAR(256)  NOT NULL,
    BACKUP_TABLE     VARCHAR(256),
    ACTION           VARCHAR(20)   NOT NULL,
    ROW_COUNT        NUMBER(38,0),
    SOURCE_ROW_COUNT NUMBER(38,0),
    BYTES            NUMBER(38,0),
    DETAIL           VARCHAR(1000),
    LOGGED_AT        TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()
);

-- >>> derived:SP_BACKUP_OPERATOR_TABLES  (from V089; daily dated generations in OVERWATCH_BAK + keep-count prune + row-count log + freshness stamp, V158)
"""

TASK = """
-- Daily cadence (decision O-13): 05:10 Central rides the hourly chain's warm WH_ALFA_ADMIN
-- (TASK_LOAD_HOURLY :07) and runs ahead of the 06:30+ daily batch.
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR SUSPEND;
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR
    SET SCHEDULE = 'USING CRON 10 5 * * * America/Chicago';
ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR RESUME;

-- >>> derived:V_SECURITY_EXCEPTION_QUEUE  (from V151; + OVERWATCH_BAK backup-prune carve-out, V158)
"""

TAIL_TASK = """
-- First generation now, through the TASK: it runs as SYSTEM like every scheduled run (inside the
-- carve-out above), exercises the real task path, and keys the generation on the Central day
-- whatever the worksheet zone. Asynchronous: read TASK_HISTORY / OPERATOR_BACKUP_LOG in PART B.
EXECUTE TASK DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR;

"""

DESCRIPTION = (
    "Operator-data backups rotate daily (Next-Fifty #32): new TRANSIENT schema DBA_MAINT_DB.OVERWATCH_BAK "
    "(owner-only, no FUTURE grants, so no grant churn in OVERWATCH). SP_BACKUP_OPERATOR_TABLES re-derived "
    "from V089 clones the 25 operator tables every day at 05:10 Central to immutable TRANSIENT "
    "OVERWATCH_BAK.<T>_OWBAK_D<yyyymmdd> generations (Sundays also _OWBAK_W, cloned from the D generation, "
    "and V089''s <T>_BAK_LAST statement, still weekly), logs backup vs source row counts to the new "
    "OPERATOR_BACKUP_LOG, prunes to SETTINGS BACKUP_KEEP_DAILY 14 / BACKUP_KEEP_WEEKLY 8 (floors 7/4, only "
    "TRANSIENT OVERWATCH_BAK tables whose whole name matches the generation pattern, dated before today) "
    "and stamps SOURCE_FRESHNESS_STATE OPERATOR_BACKUP_DAILY only on a run with zero clone failures. "
    "TASK_BACKUP_OPERATOR moved from Sunday 05:40 to daily 05:10 via SUSPEND/SET SCHEDULE/RESUME. "
    "V_SECURITY_EXCEPTION_QUEUE re-derived from V151 with one carve-out: the task''s own generation-prune "
    "DROP (USER_NAME SYSTEM, exact generated statement) leaves the CHANGE RISK queue. Restore = INSERT "
    "OVERWRITE as the table-owner role. Tail: EXECUTE TASK (first generation)."
)
assert "'" not in DESCRIPTION.replace("''", "")
assert len(DESCRIPTION.replace("''", "'")) <= 4000

SCHEMA_VERSION = (
    "INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)\n"
    "SELECT 158 AS VERSION,\n"
    f"       '{DESCRIPTION}' AS DESCRIPTION\n"
    "WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 158);\n"
)

out = HEADER + proc + TASK + view + TAIL_TASK + SCHEMA_VERSION

# file post-conditions
assert out.count("CREATE OR REPLACE PROCEDURE") == 1 and out.count("CREATE OR REPLACE VIEW") == 1
assert "CREATE TASK" not in out and "CALL " not in out
assert out.count("EXECUTE TASK DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR;") == 1
assert out.index("CREATE TRANSIENT SCHEMA") < out.index("MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS") \
    < out.index("CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG") \
    < out.index("CREATE OR REPLACE PROCEDURE") < out.index("    SET SCHEDULE = 'USING CRON 10 5 ") \
    < out.index("CREATE OR REPLACE VIEW") < out.index("\nEXECUTE TASK ") < out.index("SELECT 158 AS VERSION")
assert "EXCEPTION (-20158" in out and "IF (v < 157) THEN" in out
assert "WHERE VERSION = 158)" in out

target = Path(os.environ.get("V158_OUT") or (MIG / "V158__operator_backup_generations.sql"))
target.write_text(out, encoding="utf-8", newline="\n")
print(f"wrote {target} ({len(out):,} chars)")
