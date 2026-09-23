#!/usr/bin/env python3
"""Forward-generate V150: the COST_CLOUD_SVC_ANOMALY per-entity cloud-services baseline.

Phase 2 of the Cloud Services Driver Intelligence work. Replaces the fixed 10/20% cloud-services
RATIO threshold (COST_CLOUD_SVC_RATIO) as the primary "is this warehouse's cloud services a
problem" signal with a PER-WAREHOUSE robust-z baseline: a chronically compile-heavy discovery
warehouse (e.g. WH_ALFA_QA) sits in-baseline instead of re-alerting every day, while a genuine
step-change fires. The baseline is the same median/MAD modified-z engine the existing
COST_ANOMALY_SWEEP arm and app/logic/anomaly.robust_zscores use (0.6745 / 0.7979 fallback,
28-day window), sourced from MART_CLOUD_SVC_DAILY (per family x warehouse x day CS credits).

Materiality is gated on a CS-credit VOLUME floor, NOT the $50 compute floor the warehouse arm
uses -- cloud-services credits are tiny (a $50 gate ~= 13.6 credits would suppress real CS
step-changes). Every credit is GROSS USAGE (pre the account+day ~10% rebate); the alert says so.

Mirrors V133 exactly: a new self-contained scan proc (SP_SCAN_CLOUD_SVC_ANOMALY) + its
ALERT_CONFIG rule, and SP_ANOMALY_SWEEP re-derived from its CURRENT definition (V133 -- the loader
was re-derived across V012/V014/V016/V023/V076/V097/V122/V132/V133, so an older base would drop the
intervening arms) with ONE added CALL arm inside the standard per-arm EXCEPTION guard. Rides the
daily TASK_ANOMALY_SWEEP cadence, no new task. Owner applies after V149; the trailing CALL books
any current step-change immediately. This file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V133__dq_schema_drift.sql"


def extract_procedure(text: str, sig: str) -> str:
    pattern = re.compile(rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{sig}.*?\$\$;\n", re.S)
    matches = pattern.findall(text)
    assert len(matches) == 1, f"{sig}: expected 1 proc, got {len(matches)}"
    return matches[0]


proc = extract_procedure(BASE.read_text(encoding="utf-8"), r"SP_ANOMALY_SWEEP\(")

# The cloud-services anomaly arm — a thin CALL wrapped in the standard per-arm guard (V129/V133 pattern).
CS_ARM = """\
    -- COST_CLOUD_SVC_ANOMALY (Phase 2): per-warehouse cloud-services robust-z step-change vs a 28d
    -- baseline, replacing the fixed 10/20% CS-ratio threshold so a chronically compile-heavy warehouse
    -- stays in-baseline while a real spike fires. The scan lives in SP_SCAN_CLOUD_SVC_ANOMALY; this arm
    -- just CALLs it inside the standard guard, so a MART_CLOUD_SVC_DAILY issue can't break the other arms.
    BEGIN
        CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_CLOUD_SVC_ANOMALY();
    EXCEPTION
        WHEN OTHER THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AnomalySweep', 'cloud_svc_anomaly_scan_failed', SQLERRM,
                   'COST_CLOUD_SVC_ANOMALY check skipped', CURRENT_ROLE();
    END;

"""

# Insert the CS arm after the schema-drift arm (its END is now immediately before the Cortex pre-explain).
ANCHOR = "    END;\n\n    -- Pre-explain fresh anomalies (guarded):"
assert proc.count(ANCHOR) == 1, f"pre-explain anchor: got {proc.count(ANCHOR)}"
proc = proc.replace(ANCHOR, "    END;\n\n" + CS_ARM + "    -- Pre-explain fresh anomalies (guarded):", 1)

# post-conditions: the CS arm is in, nothing else changed
assert "CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_CLOUD_SVC_ANOMALY()" in proc
assert "CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_SCHEMA_DRIFT()" in proc      # V133 arm survives
assert "-- DQ_BREACH (R24)" in proc and "PIPE_VOLUME_DROP" in proc       # nothing dropped
assert proc.count("RETURN 'anomaly sweep v3 complete'") == 1

RULE_MERGE = """\
MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t
USING (
    SELECT * FROM VALUES
        ('COST_CLOUD_SVC_ANOMALY', 'COST', 'Cloud-services credits per warehouse: robust-z step-change vs the 28d baseline, replacing the fixed 10/20% ratio', TRUE, 'MEDIUM', 3.5, 24)
    AS s(RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
) s
ON t.RULE_ID = s.RULE_ID
WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
     VALUES (s.RULE_ID, s.FAMILY, s.NAME, s.ENABLED, s.SEVERITY, s.THRESHOLD_NUM, s.WINDOW_HOURS);
"""

SCAN_PROC = """\
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_SCAN_CLOUD_SVC_ANOMALY()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
-- Per-warehouse cloud-services anomaly scan (Phase 2). Books one COST_CLOUD_SVC_ANOMALY alert per
-- (warehouse, day) whose gross cloud-services credits are a robust-z outlier vs the warehouse's own
-- prior-28-day baseline. This is the PER-ENTITY replacement for the fixed 10/20% CS-ratio threshold:
-- a warehouse that is CHRONICALLY compile-heavy (high but STEADY cloud services) sits in-baseline and
-- does NOT alert, while a genuine step-change (a new chatty tool, a runaway metadata loop) fires.
--
-- Same robust median/MAD modified-z engine as the COST_ANOMALY_SWEEP warehouse/service arm and the
-- app twin app/logic/anomaly.robust_zscores (0.6745; mean-absolute-deviation / 0.7979 fallback when
-- MAD collapses to 0). Source is MART_CLOUD_SVC_DAILY (gross CS credits, per family x warehouse x day;
-- CS is USAGE, never billable -- the ~10% rebate is account+day and not warehouse-decomposable).
-- Materiality is a CS-CREDIT VOLUME floor, NOT the $50 compute floor: cloud-services credits are tiny,
-- so a dollar gate would suppress real CS step-changes. The last 3 complete days are (re)scored with a
-- per-(series,day) dedup key, so a day deleted mid-reconcile is picked up on the next run.
DECLARE
    zthr FLOAT;
    cs_floor FLOAT DEFAULT 1.0;   -- CS credits/day floor: below this, a spike is not worth paging
BEGIN
    SELECT COALESCE(MAX(THRESHOLD_NUM), 3.5) INTO :zthr
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
    WHERE RULE_ID = 'COST_CLOUD_SVC_ANOMALY' AND ENABLED;
    IF (:zthr IS NULL) THEN
        RETURN 'cloud-services anomaly scan skipped (rule disabled)';
    END IF;

    INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
        (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
    WITH series AS (
        SELECT 'CLOUD SVC ' || COALESCE(WAREHOUSE_NAME, 'NONE') AS SERIES, COMPANY, DAY,
               SUM(CS_CREDITS) AS CREDITS
        FROM DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY
        WHERE DAY >= DATEADD('day', -29, CURRENT_DATE()) AND DAY < CURRENT_DATE()
        GROUP BY 1, 2, 3
    ),
    med AS (
        SELECT SERIES, MEDIAN(CREDITS) AS MED
        FROM series GROUP BY 1
    ),
    mad AS (
        SELECT s.SERIES, m.MED, MEDIAN(ABS(s.CREDITS - m.MED)) AS MAD
        FROM series s JOIN med m ON m.SERIES = s.SERIES
        GROUP BY 1, 2
    ),
    meanad AS (
        SELECT s.SERIES, AVG(ABS(s.CREDITS - m.MED)) AS MEAN_AD
        FROM series s JOIN med m ON m.SERIES = s.SERIES
        GROUP BY 1
    ),
    active AS (
        SELECT SERIES, COUNT_IF(CREDITS > 0) AS ACTIVE_DAYS
        FROM series GROUP BY 1
    ),
    latest AS (
        SELECT s.SERIES, s.COMPANY, s.DAY, s.CREDITS, m.MED, m.MAD, a.ACTIVE_DAYS,
               IFF(m.MAD > 0, 0.6745, 0.7979) * (s.CREDITS - m.MED)
                   / NULLIF(IFF(m.MAD > 0, m.MAD, ma.MEAN_AD), 0) AS SIGNED_Z,
               ABS(IFF(m.MAD > 0, 0.6745, 0.7979) * (s.CREDITS - m.MED)
                   / NULLIF(IFF(m.MAD > 0, m.MAD, ma.MEAN_AD), 0)) AS ROBUST_Z
        FROM series s
        JOIN mad m ON m.SERIES = s.SERIES
        JOIN meanad ma ON ma.SERIES = s.SERIES
        JOIN active a ON a.SERIES = s.SERIES
        WHERE s.DAY >= DATEADD('day', -3, CURRENT_DATE())
    )
    SELECT 'COST_CLOUD_SVC_ANOMALY', l.COMPANY,
           IFF(l.ROBUST_Z >= :zthr * 2, 'HIGH', 'MEDIUM'),
           l.SERIES || IFF(l.SIGNED_Z < 0, ' cloud-services collapsed to ', ' cloud-services spiked to ') ||
               ROUND(l.CREDITS, 2) || ' credits on ' || TO_VARCHAR(l.DAY) ||
               ' (z=' || ROUND(l.SIGNED_Z, 1) || ')',
           'Median ' || ROUND(l.MED, 2) || ' CS credits/day over the prior 28d (GROSS usage, before the ' ||
               'account-level ~10% rebate). Robust z ' || ROUND(l.ROBUST_Z, 1) || ' vs threshold ' || :zthr ||
               '. This per-warehouse baseline replaces the fixed 10/20% ratio: a chronically compile-heavy ' ||
               'warehouse stays in-baseline, so this is a real step-change. Investigate: Operations > ' ||
               'Queries > cloud-services chatter by application, or Cost > Spend cloud-services health.',
           l.ROBUST_Z,
           'COST_CLOUD_SVC_ANOMALY|' || l.SERIES || '|' || TO_VARCHAR(l.DAY)
    FROM latest l
    WHERE l.SIGNED_Z IS NOT NULL AND l.ROBUST_Z >= :zthr
      AND l.ACTIVE_DAYS >= 10
      AND (
          (l.SIGNED_Z > 0 AND l.CREDITS >= :cs_floor)
          OR (l.SIGNED_Z < 0 AND l.MED >= :cs_floor)
      )
      AND NOT EXISTS (
          SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
          WHERE e.DEDUPE_KEY = 'COST_CLOUD_SVC_ANOMALY|' || l.SERIES || '|' || TO_VARCHAR(l.DAY)
      );

    RETURN 'cloud-services anomaly scan complete';
END;
$$;
"""

HEADER = """\
-- V150__cloud_svc_anomaly_baseline.sql
--
-- COST_CLOUD_SVC_ANOMALY per-entity cloud-services baseline (Cloud Services Driver Intelligence,
-- Phase 2). Replaces the fixed 10/20% cloud-services RATIO threshold (COST_CLOUD_SVC_RATIO) as the
-- primary "is this warehouse's cloud services a problem" signal with a PER-WAREHOUSE robust-z
-- baseline: a chronically compile-heavy discovery warehouse sits in-baseline instead of re-alerting
-- every day, while a genuine step-change fires. Same median/MAD modified-z engine (0.6745 / 0.7979
-- fallback, 28-day window) as the COST_ANOMALY_SWEEP arm and app/logic/anomaly.robust_zscores,
-- sourced from MART_CLOUD_SVC_DAILY (gross CS credits, per family x warehouse x day). Materiality is
-- a CS-CREDIT VOLUME floor, NOT the $50 compute floor (cloud-services credits are tiny). CS is GROSS
-- usage, never billable.
--
-- Adds SP_SCAN_CLOUD_SVC_ANOMALY + its COST_CLOUD_SVC_ANOMALY ALERT_CONFIG rule (COST / MEDIUM), and
-- a CALL arm in SP_ANOMALY_SWEEP (re-derived from V133) inside the standard per-arm EXCEPTION guard --
-- so it rides the existing daily TASK_ANOMALY_SWEEP cadence with no new task, and a failure can't
-- break the cost/volume/DQ arms. Internal mart reads only, so the rule ships ENABLED. Owner applies in
-- Snowsight after V149; the trailing CALL books any current step-change immediately. This file never
-- runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20150, 'V150 requires V149 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 149) THEN
        RAISE not_ready;
    END IF;
END;
$$;

"""

TAIL = """
-- Run the new scan once so any current step-change books immediately (the daily TASK_ANOMALY_SWEEP
-- picks it up going forward). Only the CS arm -- not the full sweep -- to avoid the Cortex pre-explain
-- AI cost at apply time; the arm is exception-guarded there and here.
CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_CLOUD_SVC_ANOMALY();

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 150 AS VERSION,
       'COST_CLOUD_SVC_ANOMALY per-entity cloud-services baseline (CS driver intelligence, Phase 2): SP_SCAN_CLOUD_SVC_ANOMALY books one alert per (warehouse, day) whose gross CS credits from MART_CLOUD_SVC_DAILY are a robust-z (0.6745 / 0.7979 fallback, 28d) outlier vs the warehouse own prior baseline -- the per-entity replacement for the fixed 10/20% CS-ratio threshold so a chronically compile-heavy warehouse stays in-baseline while a step-change fires. CS-credit VOLUME materiality floor (not the $50 compute floor; CS credits are tiny). Adds the COST_CLOUD_SVC_ANOMALY ALERT_CONFIG rule (COST / MEDIUM, ENABLED) + a CALL arm in SP_ANOMALY_SWEEP (re-derived from V133) inside the standard EXCEPTION guard, riding the existing daily TASK_ANOMALY_SWEEP cadence (no new task). Proc otherwise byte-identical. CS credits are gross usage, never billable.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 150);
"""

out = HEADER + RULE_MERGE + "\n" + SCAN_PROC + "\n" + proc + TAIL

# self-assertions
assert out.count("CREATE OR REPLACE PROCEDURE") == 2  # SP_SCAN_CLOUD_SVC_ANOMALY + SP_ANOMALY_SWEEP
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_SCAN_CLOUD_SVC_ANOMALY" in out
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ANOMALY_SWEEP" in out
assert "('COST_CLOUD_SVC_ANOMALY', 'COST'" in out
assert "WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME" in out
assert "WHEN MATCHED THEN UPDATE" not in out       # never clobber an operator-edited rule
assert "MART_CLOUD_SVC_DAILY" in out and "0.6745" in out and "0.7979" in out
assert "CREATE TABLE" not in out and "ALTER TABLE " not in out and "CREATE TASK" not in out
assert "CREATE OR REPLACE VIEW" not in out and "CREATE OR REPLACE FUNCTION" not in out
assert "SNOWFLAKE.ACCOUNT_USAGE" not in SCAN_PROC   # the new scan reads the mart, not ACCOUNT_USAGE
assert "EXCEPTION (-20150" in out and "IF (v < 149) THEN" in out
assert "SELECT 150 AS VERSION" in out and "WHERE VERSION = 150)" in out
assert "CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_CLOUD_SVC_ANOMALY();" in out

target = Path(os.environ.get("V150_OUT") or (MIG / "V150__cloud_svc_anomaly_baseline.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
