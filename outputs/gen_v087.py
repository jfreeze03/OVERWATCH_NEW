#!/usr/bin/env python3
"""Forward-generate V087: security-finding -> monitored rule (CoCo Sec35).

The Security decision queue turns a posture finding into a work item, but not into
something that RE-alerts when the posture degrades again. V087 makes the SECURITY
posture family data-driven so an operator can, from a finding, generate an
ALERT_CONFIG rule that self-monitors a posture metric.

Two additive changes:
  * ALTER ALERT_CONFIG ADD METRIC_NAME -- names a MART_SECURITY_POSTURE_DAILY
    metric to watch (MFA_GAP_USERS, EXPIRED_CRED, UNUSED_ROLES_90D, ...). These
    posture metrics are COUNTS OF PROBLEMS (higher = worse), so the monitor fires
    on VALUE >= THRESHOLD_NUM -- one universal comparator, no direction column.
  * Re-derive SP_ALERT_SCAN (from V086) with ONE generic arm [21] that raises
    EVERY enabled rule carrying a METRIC_NAME: it reads the newest posture reading
    per (metric, company) and raises when the value is at/over the rule's
    threshold. Rules are OPERATOR-created (via the generate-INSERT UI on
    security_center), not seeded here, so the arm is the shared raiser for all of
    them; stale posture (newest DAY > 2 days old) never alerts; daily-deduped.

The ONLY SP_ALERT_SCAN edit vs V086 is the arm [21] spliced before the fails
self-alert plus the rule-block count 15 -> 16; reversing both reproduces the V086
body byte-for-byte. The scanner is NOT fired at apply time. Owner applies in
Snowsight after V086.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE_ALERTSCAN = MIG / "V086__alert_snooze.sql"


def extract_proc(text: str, name: str) -> str:
    pattern = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n",
        re.S,
    )
    matches = pattern.findall(text)
    assert matches, name
    return matches[-1]


# One generic posture-monitor arm. Raises any ENABLED rule with a METRIC_NAME whose
# newest posture reading is at/over the rule's threshold. Its own isolated
# BEGIN..EXCEPTION so a broken rule logs + increments `fails` (it IS a rule block).
NEW_ARM = """    -- [21] SEC_POSTURE_METRIC (V087 - CoCo Sec35: generic, data-driven posture monitor
    --      keyed by ALERT_CONFIG.METRIC_NAME; every operator-created posture-metric rule
    --      raises here, so posture self-monitors after a finding is turned into a rule.
    --      INVARIANT: every MART_SECURITY_POSTURE_DAILY metric is a problem COUNT
    --      (higher = worse), so the comparator is a fixed VALUE >= THRESHOLD_NUM, and the
    --      app builder (posture_alert_rule_sql) only creates rules for that count
    --      vocabulary. A future lower-is-worse metric would need a comparator column.)
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
            WHERE ENABLED AND COALESCE(METRIC_NAME, '') <> ''
        ),
        latest AS (
            -- newest posture reading per (metric, company)
            SELECT METRIC, COMPANY, VALUE, DAY
            FROM DBA_MAINT_DB.OVERWATCH.MART_SECURITY_POSTURE_DAILY
            QUALIFY ROW_NUMBER() OVER (PARTITION BY METRIC, COMPANY ORDER BY DAY DESC) = 1
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, m.COMPANY, c.SEVERITY,
               c.NAME || ': ' || m.METRIC || ' = ' || m.VALUE::INT
                   || ' (threshold >= ' || c.THRESHOLD_NUM || ')',
               'Security posture metric ' || m.METRIC || ' is ' || m.VALUE::INT || ' as of ' || m.DAY
                   || ', at or over its configured threshold ' || c.THRESHOLD_NUM
                   || '. Source: MART_SECURITY_POSTURE_DAILY - review in Security.',
               m.VALUE,
               c.RULE_ID || '|' || m.COMPANY || '|' || TO_VARCHAR(m.DAY)
        FROM cfg c
        JOIN latest m
          ON UPPER(m.METRIC) = UPPER(c.METRIC_NAME)
         AND m.VALUE >= c.THRESHOLD_NUM
         AND m.DAY >= DATEADD('day', -2, CURRENT_DATE())

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule posture-metric (generic) - other rules unaffected', CURRENT_ROLE();
    END;
"""

_ANCHOR = "    IF (fails > 0) THEN\n"


def derive_scan(base: Path) -> str:
    """Extract SP_ALERT_SCAN from V086, splice arm [21] + bump the count 15 -> 16."""
    proc = extract_proc(base.read_text(encoding="utf-8"), "SP_ALERT_SCAN")
    assert "METRIC_NAME" not in proc and "SEC_POSTURE_METRIC" not in proc, "arm already present"
    assert proc.count(_ANCHOR) == 1, f"expected 1 fails-block anchor, got {proc.count(_ANCHOR)}"
    proc = proc.replace(_ANCHOR, NEW_ARM + _ANCHOR, 1)
    assert proc.count("' of 15 alert rule block(s) failed this run'") == 1
    proc = proc.replace("' of 15 alert rule block(s) failed this run'",
                        "' of 16 alert rule block(s) failed this run'")
    assert proc.count("(15 - :fails) || '/15 rule blocks ok'") == 1
    proc = proc.replace("(15 - :fails) || '/15 rule blocks ok'",
                        "(16 - :fails) || '/16 rule blocks ok'")
    assert "of 15 alert" not in proc and "/15 rule blocks" not in proc and "(15 - :fails)" not in proc
    assert proc.count("-- [21] SEC_POSTURE_METRIC") == 1
    return proc


alert_scan = derive_scan(BASE_ALERTSCAN)

# Sanity: the re-derived proc kept its V086 identity (arm [20] + the [wake] step).
assert "-- [20] SEC_NEW_EXPOSURE" in alert_scan and "-- [wake] V086" in alert_scan
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()" in alert_scan

out = f"""-- V087__security_posture_rule.sql
--
-- Security finding -> monitored rule (CoCo Sec35). Make the SECURITY posture family
-- data-driven so an operator can turn a finding into a self-monitoring rule.
--
-- Two additive changes:
--   * ALTER ALERT_CONFIG ADD METRIC_NAME -- names a MART_SECURITY_POSTURE_DAILY
--     metric (MFA_GAP_USERS, EXPIRED_CRED, UNUSED_ROLES_90D, ...). Those posture
--     metrics are counts of problems (higher = worse), so the monitor fires on
--     VALUE >= THRESHOLD_NUM -- one universal comparator, no direction column.
--   * Re-derive SP_ALERT_SCAN (from V086) with ONE generic arm [21] that raises
--     every enabled rule carrying a METRIC_NAME: newest posture reading per
--     (metric, company), raised when at/over the rule's threshold. Rules are
--     operator-created via the generate-INSERT UI on Security (not seeded here);
--     stale posture (newest DAY > 2d old) never alerts; daily-deduped.
--
-- The ONLY SP_ALERT_SCAN edit vs V086 is the arm + the rule-block count 15 -> 16;
-- reversing both reproduces the V086 body byte-for-byte. The scanner is NOT fired
-- at apply time. Owner applies in Snowsight after V086.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20087, 'V087 requires V086 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 86) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- Additive: the metric a posture rule watches (NULL for the existing hardcoded rules).
ALTER TABLE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG ADD COLUMN IF NOT EXISTS METRIC_NAME VARCHAR(60);

-- >>> derived:SP_ALERT_SCAN  (+ generic arm [21] SEC_POSTURE_METRIC, from V086)
{alert_scan}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 87 AS VERSION,
       'Security finding -> monitored rule (CoCo Sec35): ALTER ALERT_CONFIG ADD METRIC_NAME; SP_ALERT_SCAN re-derived from V086 with a generic arm [21] that raises every enabled rule carrying a METRIC_NAME when its newest MART_SECURITY_POSTURE_DAILY reading is at/over THRESHOLD_NUM (posture counts are higher=worse). Rules are operator-created from a Security generate-INSERT UI, so the arm is the shared raiser; stale posture excluded; daily-deduped. Arm count 15->16; the scanner is not fired at apply time.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 87);
"""

assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE TABLE" not in out and "CREATE TASK" not in out
assert "CREATE WAREHOUSE" not in out and "RESOURCE MONITOR" not in out
assert "EXCEPTION (-20087" in out and "IF (v < 86) THEN" in out
assert "SELECT 87 AS VERSION" in out and "WHERE VERSION = 87)" in out
assert "CALL DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN(" not in out   # never fire at apply
assert out.count("ADD COLUMN IF NOT EXISTS METRIC_NAME") == 1
assert "MART_SECURITY_POSTURE_DAILY" in out and "m.VALUE >= c.THRESHOLD_NUM" in out
assert "of 16 alert rule block(s)" in out and "/16 rule blocks ok" in out
assert "of 15 alert" not in out and "/15 rule blocks" not in out

target = Path(os.environ.get("V087_OUT") or (MIG / "V087__security_posture_rule.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
