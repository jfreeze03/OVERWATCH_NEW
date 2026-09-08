#!/usr/bin/env python3
"""Forward-generate V132: the DQ_BREACH data-quality alert (Codex R24).

The Operations data-quality panel scores each registered-product table's most recent rows-added load by
robust z (logic/dq.row_volume_anomalies) and flags spikes/drops — but nothing booked those findings as
alerts (dq.py's own docstring names the DQ_BREACH alert a deferred owner-migration half). This adds a
DQ_BREACH arm to SP_ANOMALY_SWEEP that reproduces that scoring server-side and raises one alert per
flagged table, plus the DQ_BREACH ALERT_CONFIG rule.

The arm is the DB-side twin of dq.row_volume_anomalies: per registered table, the daily rows-added series
(rows>0 only, dated-load tables excluded), the LATEST load scored against a BASELINE of its prior loads
(excludes the latest), robust z = (latest - baseline_median) / GREATEST(MAD*1.4826, 0.15*median) with the
same dispersion floor, gated on >=10 baseline loads (>=11 total) and a baseline median >= 100 rows, flagged
when |z| >= the rule threshold (3.5) in EITHER direction (spike or drop — the drop half PIPE_VOLUME_DROP's
%-collapse logic can miss). Guarded like the sweep's other ACCOUNT_USAGE arms so a TABLE_DML_HISTORY gap
can't break the cost/volume halves.

Re-derives SP_ANOMALY_SWEEP from V122 with ONLY the new arm inserted before the Cortex pre-explain block;
everything else byte-identical. Reads only internal ACCOUNT_USAGE + ENTITY_CATALOG (no external grants), so
the rule ships ENABLED. Owner applies in Snowsight after V131; the migration re-runs the sweep once so the
last 3 days' DQ anomalies populate immediately. This file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V122__anomaly_sweep_reconcile_race.sql"


def extract_procedure(text: str, sig: str) -> str:
    pattern = re.compile(rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{sig}.*?\$\$;\n", re.S)
    matches = pattern.findall(text)
    assert len(matches) == 1, f"{sig}: expected 1 proc, got {len(matches)}"
    return matches[0]


proc = extract_procedure(BASE.read_text(encoding="utf-8"), r"SP_ANOMALY_SWEEP\(")

# The DQ_BREACH arm — plain string (NOT an f-string) so the regex {8} survives literally.
DQ_ARM = """\
    -- DQ_BREACH (R24): registered-product tables whose most recent rows-added load is a robust-z
    -- outlier (spike OR drop) vs its own prior loads -- the DB-side twin of logic/dq.row_volume_anomalies
    -- (the Operations data-quality panel), now booked as alerts. Guarded so an ACCOUNT_USAGE gap can't
    -- break the sweep's cost/volume halves.
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        -- 28-day load window: MUST match the panel's product_row_volume(28) so the alert and the
        -- Operations data-quality panel score the SAME series (same baseline, same >=11-load
        -- eligibility) and can never disagree on a table -- a wider window would fire on tables the
        -- panel omits or shift the baseline median/MAD. (The panel's QUALIFY DENSE_RANK<=600 render
        -- cap is deliberately NOT mirrored: the alert has no render limit, so a real anomaly on the
        -- 601st+ registered table still pages -- more complete, never wrong.)
        WITH vol AS (
            SELECT d.DATABASE_NAME AS DB,
                   d.DATABASE_NAME || '.' || d.SCHEMA_NAME || '.' || d.TABLE_NAME AS FQN,
                   DATE(d.START_TIME) AS DAY,
                   SUM(d.ROWS_ADDED) AS ROWS_ADDED
            FROM SNOWFLAKE.ACCOUNT_USAGE.TABLE_DML_HISTORY d
            WHERE d.START_TIME >= DATEADD('day', -28, CURRENT_DATE())
              AND d.START_TIME < CURRENT_DATE()
              AND UPPER(d.DATABASE_NAME) <> 'SNOWFLAKE'
              AND NOT REGEXP_LIKE(d.TABLE_NAME, '.*_[0-9]{8}(_[0-9]+)+', 'i')
            GROUP BY 1, 2, 3
            HAVING SUM(d.ROWS_ADDED) > 0
        ),
        cat AS (
            SELECT ENTITY_TYPE, UPPER(ENTITY_KEY) AS K, DATA_PRODUCT, OWNER_NAME, CRITICALITY
            FROM DBA_MAINT_DB.OVERWATCH.ENTITY_CATALOG
            WHERE NULLIF(TRIM(DATA_PRODUCT), '') IS NOT NULL
        ),
        reg AS (
            SELECT v.FQN, v.DB, v.DAY, v.ROWS_ADDED,
                   COALESCE(om.DATA_PRODUCT, dm.DATA_PRODUCT) AS DATA_PRODUCT,
                   COALESCE(om.OWNER_NAME, dm.OWNER_NAME) AS OWNER_NAME
            FROM vol v
            LEFT JOIN cat om ON om.ENTITY_TYPE = 'OBJECT' AND om.K = UPPER(v.FQN)
            LEFT JOIN cat dm ON om.K IS NULL AND dm.ENTITY_TYPE = 'DATABASE' AND dm.K = UPPER(v.DB)
            WHERE COALESCE(om.DATA_PRODUCT, dm.DATA_PRODUCT) IS NOT NULL
        ),
        ranked AS (
            SELECT FQN, DB, DAY, ROWS_ADDED, DATA_PRODUCT, OWNER_NAME,
                   ROW_NUMBER() OVER (PARTITION BY FQN ORDER BY DAY DESC) AS RN,
                   COUNT(*) OVER (PARTITION BY FQN) AS N_LOADS
            FROM reg
        ),
        base_med AS (
            SELECT FQN, MEDIAN(ROWS_ADDED) AS MED
            FROM ranked WHERE RN > 1 GROUP BY 1
        ),
        base_mad AS (
            SELECT r.FQN, m.MED, MEDIAN(ABS(r.ROWS_ADDED - m.MED)) AS MAD_RAW
            FROM ranked r JOIN base_med m ON m.FQN = r.FQN
            WHERE r.RN > 1 GROUP BY 1, 2
        ),
        scored AS (
            SELECT l.FQN, l.DB, l.DAY, l.ROWS_ADDED AS LATEST_ROWS, l.DATA_PRODUCT, l.OWNER_NAME,
                   b.MED,
                   (l.ROWS_ADDED - b.MED)
                       / NULLIF(GREATEST(b.MAD_RAW * 1.4826, 0.15 * b.MED), 0) AS RAW_Z
            FROM ranked l
            JOIN base_mad b ON b.FQN = l.FQN
            WHERE l.RN = 1
              AND l.N_LOADS >= 11
              AND b.MED >= 100
        )
        SELECT c.RULE_ID,
               IFF(s.DB LIKE 'TRXS%', 'Trexis', 'ALFA'),
               c.SEVERITY,
               s.FQN || IFF(s.RAW_Z < 0, ' rows-added dropped to ', ' rows-added spiked to ') ||
                   s.LATEST_ROWS || ' on ' || TO_VARCHAR(s.DAY) || ' (z=' ||
                   ROUND(LEAST(99.9, GREATEST(-99.9, s.RAW_Z)), 1) || ')',
               'Registered product ' || COALESCE(s.DATA_PRODUCT, '(unknown)') || ', owner ' ||
                   COALESCE(s.OWNER_NAME, '(unassigned)') || '. Baseline median ' || ROUND(s.MED, 0) ||
                   ' rows/load over its prior loads. Robust z ' ||
                   ROUND(LEAST(99.9, GREATEST(-99.9, s.RAW_Z)), 1) || ' vs threshold ' || c.THRESHOLD_NUM ||
                   '. Investigate: Operations > Pipeline data-quality panel.',
               LEAST(99.9, GREATEST(-99.9, s.RAW_Z)),
               c.RULE_ID || '|' || s.FQN || '|' || TO_VARCHAR(s.DAY)
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
        JOIN scored s ON c.RULE_ID = 'DQ_BREACH' AND c.ENABLED
           AND ABS(s.RAW_Z) >= c.THRESHOLD_NUM
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = c.RULE_ID || '|' || s.FQN || '|' || TO_VARCHAR(s.DAY)
        );
    EXCEPTION
        WHEN OTHER THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AnomalySweep', 'dml_history_unavailable', 'TABLE_DML_HISTORY not readable',
                   'DQ_BREACH check skipped', CURRENT_ROLE();
    END;

"""

# Insert the arm after the PIPE_VOLUME_DROP block, before the Cortex pre-explain block.
ANCHOR = "    END;\n\n    -- Pre-explain fresh anomalies (guarded):"
assert proc.count(ANCHOR) == 1, f"pre-explain anchor: got {proc.count(ANCHOR)}"
proc = proc.replace(ANCHOR, "    END;\n\n" + DQ_ARM + "    -- Pre-explain fresh anomalies (guarded):", 1)

# post-conditions
assert "-- DQ_BREACH (R24)" in proc and "RULE_ID = 'DQ_BREACH'" in proc
assert proc.count("PIPE_VOLUME_DROP") >= 1 and "COST_ANOMALY_SWEEP" in proc  # nothing dropped
assert proc.count("RETURN 'anomaly sweep v3 complete'") == 1

RULE_MERGE = """\
MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t
USING (
    SELECT * FROM VALUES
        ('DQ_BREACH', 'PIPELINE', 'Registered-table rows-added is a robust-z outlier (spike or drop)', TRUE, 'MEDIUM', 3.5, 24)
    AS s(RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
) s
ON t.RULE_ID = s.RULE_ID
WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
     VALUES (s.RULE_ID, s.FAMILY, s.NAME, s.ENABLED, s.SEVERITY, s.THRESHOLD_NUM, s.WINDOW_HOURS);
"""

HEADER = """\
-- V132__dq_breach_alert.sql
--
-- DQ_BREACH data-quality alert (Codex R24). The Operations data-quality panel scores each
-- registered-product table's most recent rows-added load by robust z (logic/dq.row_volume_anomalies)
-- and flags spikes/drops, but nothing booked those findings as alerts (dq.py's docstring names DQ_BREACH
-- a deferred owner-migration half). This adds a DQ_BREACH arm to SP_ANOMALY_SWEEP that reproduces that
-- scoring server-side -- per registered table, the LATEST rows-added load scored against a BASELINE of
-- its prior loads: robust z = (latest - baseline_median) / GREATEST(MAD*1.4826, 0.15*median), gated on
-- >=10 baseline loads and a baseline median >= 100 rows, flagged when |z| >= the rule threshold (3.5) in
-- EITHER direction (the drop half PIPE_VOLUME_DROP's %-collapse logic can miss) -- and raises one alert
-- per flagged table (dedup key DQ_BREACH|<fqn>|<load day>). Plus the DQ_BREACH ALERT_CONFIG rule
-- (PIPELINE / MEDIUM / threshold 3.5). Reads only internal ACCOUNT_USAGE + ENTITY_CATALOG (no external
-- grants), so the rule ships ENABLED; the arm is exception-guarded like the sweep's other ACCOUNT_USAGE
-- arms. Re-derives SP_ANOMALY_SWEEP from V122 with only the new arm inserted; everything else
-- byte-identical. Owner applies in Snowsight after V131; the trailing CALL re-runs the sweep once so
-- current DQ anomalies (each registered table's latest load) populate immediately. This file never runs
-- from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20132, 'V132 requires V131 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 131) THEN
        RAISE not_ready;
    END IF;
END;
$$;

"""

TAIL = """
-- Re-run once now so current DQ anomalies (each registered table's latest load) and the last 3 complete
-- days' cost/volume anomalies are (re)scored under the new arm; the per-(rule, series, day) dedup makes
-- this idempotent.
CALL DBA_MAINT_DB.OVERWATCH.SP_ANOMALY_SWEEP();

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 132 AS VERSION,
       'DQ_BREACH data-quality alert (R24): SP_ANOMALY_SWEEP re-derived from V122 with a DQ_BREACH arm that scores each registered-product table''s latest rows-added load by robust z vs a baseline of its prior loads (twin of logic/dq.row_volume_anomalies: median/MAD*1.4826 with a 0.15*median dispersion floor, >=10 baseline loads, median >= 100 rows, |z| >= 3.5 either direction) and books one alert per flagged table, plus the DQ_BREACH ALERT_CONFIG rule (PIPELINE/MEDIUM/3.5). Catches volume BLOAT and thin loads the PIPE_VOLUME_DROP %-collapse rule misses. Reads only internal ACCOUNT_USAGE + ENTITY_CATALOG (ships ENABLED); arm exception-guarded; everything else in the proc byte-identical; re-runs the sweep once so current anomalies populate.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 132);
"""

out = HEADER + RULE_MERGE + "\n" + proc + TAIL

# self-assertions
assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ANOMALY_SWEEP" in out
assert "('DQ_BREACH', 'PIPELINE'" in out
assert "WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME" in out
assert "CREATE TABLE " not in out and "ALTER TABLE " not in out and "CREATE TASK" not in out
assert "CREATE OR REPLACE VIEW" not in out and "CREATE OR REPLACE FUNCTION" not in out
assert "EXCEPTION (-20132" in out and "IF (v < 131) THEN" in out
assert "SELECT 132 AS VERSION" in out and "WHERE VERSION = 132)" in out
assert "CALL DBA_MAINT_DB.OVERWATCH.SP_ANOMALY_SWEEP();" in out

target = Path(os.environ.get("V132_OUT") or (MIG / "V132__dq_breach_alert.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
