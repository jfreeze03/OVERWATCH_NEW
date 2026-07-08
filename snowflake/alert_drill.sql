-- Monthly alert fire-drill (OPT-IN, like webhook_delivery.sql).
-- Proves the page actually reaches a human: inserts one clearly-labeled
-- synthetic CRITICAL on the 1st; the existing notify chain must deliver it
-- (NOTIFIED_AT) and on-call must ACK it. Admin > Canary scores the streak.
-- Resolve drills as EXPECTED — they are excluded from rule precision.
-- Idempotent. Teardown covers both objects.

USE DATABASE DBA_MAINT_DB;
USE SCHEMA OVERWATCH;

-- Rule row for routing/labels only: ENABLED=FALSE so the scan NEVER fires it;
-- the drill task inserts the event directly.
MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t
USING (SELECT 'OPS_ALERT_DRILL' AS RULE_ID, 'PLATFORM' AS FAMILY,
              'Monthly fire drill: end-to-end delivery + ack proof' AS NAME,
              FALSE AS ENABLED, 'CRITICAL' AS SEVERITY, 0 AS THRESHOLD_NUM, 744 AS WINDOW_HOURS) s
ON t.RULE_ID = s.RULE_ID
WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
VALUES (s.RULE_ID, s.FAMILY, s.NAME, s.ENABLED, s.SEVERITY, s.THRESHOLD_NUM, s.WINDOW_HOURS);

CREATE OR REPLACE TASK DBA_MAINT_DB.OVERWATCH.TASK_ALERT_DRILL
    WAREHOUSE = WH_ALFA_OVERWATCH
    SCHEDULE = 'USING CRON 0 9 1 * * America/Chicago'
AS
    INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
        (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, STATUS, DEDUPE_KEY)
    SELECT 'OPS_ALERT_DRILL', 'ALL', 'CRITICAL',
           '[DRILL] Monthly alert-pipeline fire drill — ACK to pass',
           'Synthetic event. Proves scan->event->webhook->ack works end to end. '
           || 'Acknowledge it, then resolve as EXPECTED. No action on Snowflake is needed.',
           0, 'OPEN',
           'DRILL_' || TO_CHAR(CURRENT_DATE(), 'YYYY_MM')
    WHERE NOT EXISTS (
        SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
        WHERE DEDUPE_KEY = 'DRILL_' || TO_CHAR(CURRENT_DATE(), 'YYYY_MM')
    );

ALTER TASK DBA_MAINT_DB.OVERWATCH.TASK_ALERT_DRILL RESUME;
