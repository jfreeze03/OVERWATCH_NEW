#!/usr/bin/env python3
"""Forward-generate V133: the DQ_SCHEMA_DRIFT schema-drift monitor (Codex R23, schema-drift half).

dq.py's docstring names schema-drift + null-rate monitors the deferred owner-migration halves. This adds
the schema-drift half: SP_SCAN_SCHEMA_DRIFT snapshots each catalog-registered OBJECT table's column set
(name + type) from ACCOUNT_USAGE.COLUMNS, diffs today's snapshot against the latest prior snapshot to
detect added / removed / retyped columns, and books one DQ_SCHEMA_DRIFT alert per drifted table. Metadata
only -- NO table-data scan and NO external grants (unlike the null-rate half, which does). A table with no
prior snapshot (first scan) establishes a baseline and never alerts.

Reuses the daily TASK_ANOMALY_SWEEP cadence: SP_ANOMALY_SWEEP (re-derived from V132) gains a schema-drift
arm that just CALLs SP_SCAN_SCHEMA_DRIFT() inside the same per-arm EXCEPTION guard (the V129 pattern), so a
COLUMNS/catalog issue can't break the cost/volume/DQ arms. Adds the DQ_SCHEMA_SNAPSHOT table + the
DQ_SCHEMA_DRIFT ALERT_CONFIG rule (PIPELINE / MEDIUM). Internal reads only, so the rule ships ENABLED.
Owner applies in Snowsight after V132; the trailing CALL runs the sweep once to lay down the first
baseline. This file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V132__dq_breach_alert.sql"


def extract_procedure(text: str, sig: str) -> str:
    pattern = re.compile(rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{sig}.*?\$\$;\n", re.S)
    matches = pattern.findall(text)
    assert len(matches) == 1, f"{sig}: expected 1 proc, got {len(matches)}"
    return matches[0]


proc = extract_procedure(BASE.read_text(encoding="utf-8"), r"SP_ANOMALY_SWEEP\(")

# The schema-drift arm — a thin CALL wrapped in the standard per-arm guard (V129 pattern).
DRIFT_ARM = """\
    -- DQ_SCHEMA_DRIFT (R23): schema drift on registered-product tables (columns added / removed /
    -- retyped vs the prior snapshot). The stateful snapshot + diff lives in SP_SCAN_SCHEMA_DRIFT; this
    -- arm just CALLs it inside the standard guard, so a COLUMNS/catalog issue can't break the other arms.
    BEGIN
        CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_SCHEMA_DRIFT();
    EXCEPTION
        WHEN OTHER THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AnomalySweep', 'schema_drift_scan_failed', SQLERRM,
                   'DQ_SCHEMA_DRIFT check skipped', CURRENT_ROLE();
    END;

"""

# Insert the drift arm after the DQ_BREACH arm (its END is now immediately before the Cortex pre-explain).
ANCHOR = "    END;\n\n    -- Pre-explain fresh anomalies (guarded):"
assert proc.count(ANCHOR) == 1, f"pre-explain anchor: got {proc.count(ANCHOR)}"
proc = proc.replace(ANCHOR, "    END;\n\n" + DRIFT_ARM + "    -- Pre-explain fresh anomalies (guarded):", 1)

# post-conditions
assert "CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_SCHEMA_DRIFT()" in proc
assert "-- DQ_BREACH (R24)" in proc and "PIPE_VOLUME_DROP" in proc  # nothing dropped
assert proc.count("RETURN 'anomaly sweep v3 complete'") == 1

TABLE_DDL = """\
CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.DQ_SCHEMA_SNAPSHOT (
    FQN          VARCHAR(600)  NOT NULL,
    COLUMN_NAME  VARCHAR(300)  NOT NULL,
    DATA_TYPE    VARCHAR(200)  NOT NULL,
    SNAPSHOT_DAY DATE          NOT NULL,
    SCANNED_AT   TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()
);
"""

RULE_MERGE = """\
MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t
USING (
    SELECT * FROM VALUES
        ('DQ_SCHEMA_DRIFT', 'PIPELINE', 'Registered-table schema drift (columns added / removed / retyped)', TRUE, 'MEDIUM', 1, 24)
    AS s(RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
) s
ON t.RULE_ID = s.RULE_ID
WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
     VALUES (s.RULE_ID, s.FAMILY, s.NAME, s.ENABLED, s.SEVERITY, s.THRESHOLD_NUM, s.WINDOW_HOURS);
"""

SCAN_PROC = """\
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_SCAN_SCHEMA_DRIFT()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
-- Schema-drift monitor (R23). Snapshots the current column set (name + type) of each catalog-registered
-- OBJECT table from ACCOUNT_USAGE.COLUMNS, then compares today's snapshot against the latest PRIOR
-- snapshot to detect added / removed / retyped columns, booking one DQ_SCHEMA_DRIFT alert per drifted
-- table. Metadata only -- no table-data scan, no external grants. A table with no prior snapshot (first
-- scan) establishes a baseline and never alerts. Idempotent for same-day re-runs (today's snapshot is
-- replaced, the diff is always latest-prior vs today).
--
-- SCOPE (deliberate): monitors tables registered as OBJECT entities (a specific DB.SCHEMA.TABLE carrying a
-- DATA_PRODUCT). Unlike the volume / DQ_BREACH arms it does NOT expand DATABASE-level registrations --
-- per-column daily snapshots across every table in a registered database is deferred on cost grounds.
-- Register a table as an OBJECT (Decision Studio catalog) to schema-drift-monitor it.
DECLARE
    enabled_cnt INT;
BEGIN
    SELECT COUNT(*) INTO :enabled_cnt
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
    WHERE RULE_ID = 'DQ_SCHEMA_DRIFT' AND ENABLED;
    IF (:enabled_cnt = 0) THEN
        RETURN 'schema-drift scan skipped (rule disabled)';
    END IF;

    -- 1) refresh today's snapshot from live column metadata (idempotent for same-day re-runs).
    DELETE FROM DBA_MAINT_DB.OVERWATCH.DQ_SCHEMA_SNAPSHOT WHERE SNAPSHOT_DAY = CURRENT_DATE();
    INSERT INTO DBA_MAINT_DB.OVERWATCH.DQ_SCHEMA_SNAPSHOT (FQN, COLUMN_NAME, DATA_TYPE, SNAPSHOT_DAY)
    SELECT UPPER(c.TABLE_CATALOG || '.' || c.TABLE_SCHEMA || '.' || c.TABLE_NAME),
           c.COLUMN_NAME, c.DATA_TYPE, CURRENT_DATE()
    FROM SNOWFLAKE.ACCOUNT_USAGE.COLUMNS c
    JOIN DBA_MAINT_DB.OVERWATCH.ENTITY_CATALOG e
      ON e.ENTITY_TYPE = 'OBJECT'
     AND UPPER(e.ENTITY_KEY) = UPPER(c.TABLE_CATALOG || '.' || c.TABLE_SCHEMA || '.' || c.TABLE_NAME)
     AND NULLIF(TRIM(e.DATA_PRODUCT), '') IS NOT NULL
    WHERE c.DELETED IS NULL;

    -- 2) diff today's snapshot vs the latest PRIOR snapshot per table; one alert per drifted table.
    INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
        (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
    WITH cfg AS (
        SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED AND RULE_ID = 'DQ_SCHEMA_DRIFT'
    ),
    cur AS (
        SELECT FQN, COLUMN_NAME, DATA_TYPE
        FROM DBA_MAINT_DB.OVERWATCH.DQ_SCHEMA_SNAPSHOT WHERE SNAPSHOT_DAY = CURRENT_DATE()
    ),
    prior_day AS (
        SELECT FQN, MAX(SNAPSHOT_DAY) AS PD
        FROM DBA_MAINT_DB.OVERWATCH.DQ_SCHEMA_SNAPSHOT
        WHERE SNAPSHOT_DAY < CURRENT_DATE() GROUP BY FQN
    ),
    prior AS (
        SELECT s.FQN, s.COLUMN_NAME, s.DATA_TYPE
        FROM DBA_MAINT_DB.OVERWATCH.DQ_SCHEMA_SNAPSHOT s
        JOIN prior_day p ON p.FQN = s.FQN AND p.PD = s.SNAPSHOT_DAY
    ),
    changes AS (
        SELECT c.FQN AS FQN, 'added ' || c.COLUMN_NAME AS CH
        FROM cur c
        JOIN prior_day pd ON pd.FQN = c.FQN
        LEFT JOIN prior p ON p.FQN = c.FQN AND p.COLUMN_NAME = c.COLUMN_NAME
        WHERE p.COLUMN_NAME IS NULL
        UNION ALL
        -- removed columns -- but ONLY for a table still present in today's snapshot (the "cf" guard,
        -- symmetric with the added branch's prior_day guard). Without it, a table that was DROPPED or
        -- UNREGISTERED (cur has zero rows for it, but a prior snapshot lingers under 90-day retention)
        -- would report every column as "removed" and, because the dedup key rolls by date, re-fire that
        -- spurious alert daily until the snapshot ages out. A vanished table is a freshness/existence
        -- concern (the SLA + row-volume panels own it), not schema drift.
        SELECT p.FQN, 'removed ' || p.COLUMN_NAME
        FROM prior p
        JOIN (SELECT DISTINCT FQN FROM cur) cf ON cf.FQN = p.FQN
        LEFT JOIN cur c ON c.FQN = p.FQN AND c.COLUMN_NAME = p.COLUMN_NAME
        WHERE c.COLUMN_NAME IS NULL
        UNION ALL
        SELECT c.FQN, 'retyped ' || c.COLUMN_NAME || ' (' || p.DATA_TYPE || ' -> ' || c.DATA_TYPE || ')'
        FROM cur c JOIN prior p ON p.FQN = c.FQN AND p.COLUMN_NAME = c.COLUMN_NAME
        WHERE c.DATA_TYPE <> p.DATA_TYPE
    ),
    agg AS (
        SELECT FQN, COUNT(*) AS N,
               LISTAGG(CH, ', ') WITHIN GROUP (ORDER BY CH) AS CHANGES
        FROM changes GROUP BY FQN
    )
    SELECT cfg.RULE_ID,
           IFF(SPLIT_PART(a.FQN, '.', 1) LIKE 'TRXS%', 'Trexis', 'ALFA'),
           cfg.SEVERITY,
           a.FQN || ' schema changed: ' || a.N || ' column change(s)',
           'Registered-table schema drift vs the prior snapshot: ' || LEFT(a.CHANGES, 1700) ||
               '. Confirm the change was intended (upstream DDL / migration) and update downstream consumers.',
           a.N,
           cfg.RULE_ID || '|' || a.FQN || '|' || TO_VARCHAR(CURRENT_DATE())
    FROM agg a
    CROSS JOIN cfg
    WHERE NOT EXISTS (
        SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
        WHERE e.DEDUPE_KEY = cfg.RULE_ID || '|' || a.FQN || '|' || TO_VARCHAR(CURRENT_DATE())
    );

    -- 3) retention: keep ~90 days of column snapshots.
    DELETE FROM DBA_MAINT_DB.OVERWATCH.DQ_SCHEMA_SNAPSHOT
    WHERE SNAPSHOT_DAY < DATEADD('day', -90, CURRENT_DATE());

    RETURN 'schema-drift scan complete';
END;
$$;
"""

HEADER = """\
-- V133__dq_schema_drift.sql
--
-- DQ_SCHEMA_DRIFT schema-drift monitor (Codex R23, schema-drift half). dq.py's docstring names schema-drift
-- + null-rate the deferred owner-migration halves; this ships the schema-drift half. SP_SCAN_SCHEMA_DRIFT
-- snapshots each catalog-registered OBJECT table's column set (name + type) from ACCOUNT_USAGE.COLUMNS and
-- compares today's snapshot to the latest prior snapshot, booking one DQ_SCHEMA_DRIFT alert per table whose
-- columns were added / removed / retyped. Metadata only -- NO table-data scan and NO external grants (the
-- null-rate half, deferred, needs both). A table with no prior snapshot establishes a baseline and never
-- alerts. Adds: the DQ_SCHEMA_SNAPSHOT baseline table (90-day retention), the DQ_SCHEMA_DRIFT ALERT_CONFIG
-- rule (PIPELINE / MEDIUM), and a schema-drift arm in SP_ANOMALY_SWEEP (re-derived from V132) that CALLs the
-- scan inside the standard per-arm EXCEPTION guard -- so it rides the existing daily TASK_ANOMALY_SWEEP
-- cadence with no new task, and a failure can't break the cost/volume/DQ arms. Internal reads only, so the
-- rule ships ENABLED. Owner applies in Snowsight after V132; the trailing CALL lays down the first baseline.
-- This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20133, 'V133 requires V132 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 132) THEN
        RAISE not_ready;
    END IF;
END;
$$;

"""

TAIL = """
-- Run the sweep once so SP_SCAN_SCHEMA_DRIFT lays down the first baseline snapshot (no alerts on the first
-- run -- nothing to diff against yet); subsequent daily runs diff against it.
CALL DBA_MAINT_DB.OVERWATCH.SP_ANOMALY_SWEEP();

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 133 AS VERSION,
       'DQ_SCHEMA_DRIFT schema-drift monitor (R23, schema-drift half): DQ_SCHEMA_SNAPSHOT table + SP_SCAN_SCHEMA_DRIFT (snapshots each catalog-registered OBJECT table''s columns from ACCOUNT_USAGE.COLUMNS, diffs today vs the latest prior snapshot, books one DQ_SCHEMA_DRIFT alert per table with added/removed/retyped columns; metadata only, no data scan, no external grants; first scan just baselines) + the DQ_SCHEMA_DRIFT ALERT_CONFIG rule (PIPELINE/MEDIUM) + a schema-drift arm in SP_ANOMALY_SWEEP (re-derived from V122->V132) that CALLs the scan inside the standard EXCEPTION guard, riding the existing daily TASK_ANOMALY_SWEEP cadence (no new task). Ships ENABLED; 90-day snapshot retention; proc otherwise byte-identical. The null-rate half stays deferred (it needs table-data scans + SELECT grants).' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 133);
"""

out = HEADER + TABLE_DDL + "\n" + RULE_MERGE + "\n" + SCAN_PROC + "\n" + proc + TAIL

# self-assertions
assert out.count("CREATE OR REPLACE PROCEDURE") == 2  # SP_SCAN_SCHEMA_DRIFT + SP_ANOMALY_SWEEP
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_SCAN_SCHEMA_DRIFT" in out
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ANOMALY_SWEEP" in out
assert "CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.DQ_SCHEMA_SNAPSHOT" in out
assert "('DQ_SCHEMA_DRIFT', 'PIPELINE'" in out
assert "WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME" in out
assert "ALTER TABLE " not in out and "CREATE TASK" not in out
assert "CREATE OR REPLACE VIEW" not in out and "CREATE OR REPLACE FUNCTION" not in out
assert "EXCEPTION (-20133" in out and "IF (v < 132) THEN" in out
assert "SELECT 133 AS VERSION" in out and "WHERE VERSION = 133)" in out
assert "CALL DBA_MAINT_DB.OVERWATCH.SP_ANOMALY_SWEEP();" in out

target = Path(os.environ.get("V133_OUT") or (MIG / "V133__dq_schema_drift.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
