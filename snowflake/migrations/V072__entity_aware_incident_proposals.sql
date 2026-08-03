-- V072__entity_aware_incident_proposals.sql
--
-- Make incident suggestions entity-aware and evidence-bearing. V032 grouped
-- every open alert in the same rule family/company, which could collapse two
-- unrelated warehouses or tasks into one proposed incident. This migration
-- replaces only the read-only proposal view. It does not declare incidents,
-- alter the auto-declare procedure, or write lifecycle rows.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20072, 'V072 requires V071 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 71) THEN
        RAISE not_ready;
    END IF;
END;
$$;

CREATE OR REPLACE VIEW DBA_MAINT_DB.OVERWATCH.INCIDENT_PROPOSALS AS
WITH raw_alerts AS (
    SELECT e.EVENT_ID,
           e.RULE_ID,
           e.COMPANY,
           e.SEVERITY,
           e.TITLE,
           e.RAISED_AT,
           SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 1) AS FAMILY,
           SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 2) AS ENTITY_RAW
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
    WHERE e.STATUS IN ('OPEN', 'ACK')
      AND e.RAISED_AT >= DATEADD('day', -2, CURRENT_TIMESTAMP())
      AND NOT EXISTS (
          SELECT 1
          FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
          WHERE m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID
      )
),
classified AS (
    SELECT r.*,
           CASE
             WHEN r.RULE_ID IN ('COST_WH_DAILY_CREDITS', 'PERF_QUEUED_MINUTES',
                                'PERF_SPILL_GB', 'COST_CLOUD_SVC_RATIO',
                                'WH_CHANGE_REGRESSION') THEN 'WAREHOUSE'
             WHEN r.RULE_ID IN ('PIPE_TASK_FAILURES', 'PIPE_COPY_FAILURES',
                                'PERF_CHANGE_REGRESSION') THEN 'OBJECT'
             WHEN r.RULE_ID = 'COST_STORAGE_SURGE' THEN 'DATABASE'
             WHEN r.RULE_ID = 'COST_SERVERLESS_CREEP' THEN 'SERVICE'
             WHEN r.RULE_ID IN ('SEC_CRED_EXPIRY', 'SEC_NEW_ADMIN_NETWORK') THEN 'USER'
             WHEN r.RULE_ID = 'COST_DEPT_BUDGET_PACE' THEN 'DEPARTMENT'
             WHEN COALESCE(r.ENTITY_RAW, '') = ''
               OR TRY_TO_DATE(r.ENTITY_RAW) IS NOT NULL
               OR UPPER(r.ENTITY_RAW) IN ('DAILY', 'WARN', 'CRIT', 'MED', 'HIGH')
               THEN 'ACCOUNT'
             ELSE 'SCOPE'
           END AS ENTITY_KIND
    FROM raw_alerts r
),
open_alerts AS (
    SELECT c.*,
           IFF(c.ENTITY_KIND = 'ACCOUNT', 'ACCOUNT', c.ENTITY_RAW) AS ENTITY_NAME
    FROM classified c
),
proposal_groups AS (
    SELECT FAMILY,
           COMPANY,
           ENTITY_KIND,
           ENTITY_NAME,
           MAX_BY(TITLE, RAISED_AT) AS SUGGESTED_TITLE,
           DECODE(MAX(CASE UPPER(SEVERITY)
                        WHEN 'CRITICAL' THEN 3 WHEN 'HIGH' THEN 2 ELSE 1 END),
                  3, 'CRITICAL', 2, 'HIGH', 'MEDIUM') AS SEVERITY,
           MIN(RAISED_AT) AS FIRST_TS,
           MAX(RAISED_AT) AS LAST_TS,
           COUNT(*) AS ALERTS
    FROM open_alerts
    GROUP BY FAMILY, COMPANY, ENTITY_KIND, ENTITY_NAME
),
warehouse_evidence AS (
    SELECT g.FAMILY, g.COMPANY, g.ENTITY_KIND, g.ENTITY_NAME,
           COUNT(w.CHANGE_ID) AS MATCHED_WH_CHANGES
    FROM proposal_groups g
    LEFT JOIN DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY w
      ON g.ENTITY_KIND = 'WAREHOUSE'
     AND UPPER(w.WAREHOUSE_NAME) = UPPER(g.ENTITY_NAME)
     AND w.CHANGE_SEEN_AT BETWEEN DATEADD('minute', -30, g.FIRST_TS)
                              AND DATEADD('minute', 30, g.LAST_TS)
    GROUP BY 1, 2, 3, 4
),
object_evidence AS (
    SELECT g.FAMILY, g.COMPANY, g.ENTITY_KIND, g.ENTITY_NAME,
           COUNT(o.CHANGE_ID) AS MATCHED_OBJECT_CHANGES
    FROM proposal_groups g
    LEFT JOIN DBA_MAINT_DB.OVERWATCH.OBJECT_CHANGE_REGISTRY o
      ON ((g.ENTITY_KIND = 'OBJECT' AND UPPER(o.OBJECT_NAME) = UPPER(g.ENTITY_NAME))
          OR (g.ENTITY_KIND = 'DATABASE'
              AND UPPER(o.DATABASE_NAME) = UPPER(g.ENTITY_NAME)))
     AND o.CHANGE_SEEN_AT BETWEEN DATEADD('minute', -30, g.FIRST_TS)
                              AND DATEADD('minute', 30, g.LAST_TS)
    GROUP BY 1, 2, 3, 4
),
task_evidence AS (
    SELECT g.FAMILY, g.COMPANY, g.ENTITY_KIND, g.ENTITY_NAME,
           COALESCE(SUM(t.FAILED), 0) AS MATCHED_TASK_FAILURES
    FROM proposal_groups g
    LEFT JOIN DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY t
      ON g.ENTITY_KIND = 'OBJECT'
     AND UPPER(COALESCE(t.DATABASE_NAME, '') || '.' ||
               COALESCE(t.SCHEMA_NAME, '') || '.' || t.TASK_NAME) = UPPER(g.ENTITY_NAME)
     AND t.DAY BETWEEN DATEADD('day', -1, DATE(g.FIRST_TS))
                   AND DATEADD('day', 1, DATE(g.LAST_TS))
    GROUP BY 1, 2, 3, 4
),
evidence AS (
    SELECT g.*,
           COALESCE(w.MATCHED_WH_CHANGES, 0) AS MATCHED_WH_CHANGES,
           COALESCE(o.MATCHED_OBJECT_CHANGES, 0) AS MATCHED_OBJECT_CHANGES,
           COALESCE(t.MATCHED_TASK_FAILURES, 0) AS MATCHED_TASK_FAILURES
    FROM proposal_groups g
    LEFT JOIN warehouse_evidence w
      USING (FAMILY, COMPANY, ENTITY_KIND, ENTITY_NAME)
    LEFT JOIN object_evidence o
      USING (FAMILY, COMPANY, ENTITY_KIND, ENTITY_NAME)
    LEFT JOIN task_evidence t
      USING (FAMILY, COMPANY, ENTITY_KIND, ENTITY_NAME)
)
SELECT FAMILY || '|' || COMPANY || '|' || ENTITY_KIND || '|' || ENTITY_NAME AS PROPOSAL_KEY,
       SUGGESTED_TITLE,
       SEVERITY,
       COMPANY,
       ENTITY_KIND,
       ENTITY_NAME,
       FIRST_TS,
       LAST_TS,
       ALERTS,
       MATCHED_WH_CHANGES,
       MATCHED_WH_CHANGES AS NEARBY_WH_CHANGES,
       MATCHED_OBJECT_CHANGES,
       MATCHED_TASK_FAILURES,
       CASE
         WHEN MATCHED_WH_CHANGES + MATCHED_OBJECT_CHANGES + MATCHED_TASK_FAILURES > 0 THEN 'HIGH'
         WHEN ENTITY_KIND <> 'ACCOUNT' AND ALERTS >= 2 THEN 'MEDIUM'
         ELSE 'LOW'
       END AS CONFIDENCE,
       'alerts=' || ALERTS ||
       '; matched warehouse changes=' || MATCHED_WH_CHANGES ||
       '; matched object changes=' || MATCHED_OBJECT_CHANGES ||
       '; matched task failures=' || MATCHED_TASK_FAILURES AS EVIDENCE
FROM evidence;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 72 AS VERSION,
       'Entity-aware incident proposals: INCIDENT_PROPOSALS now groups open alerts by family, company, entity kind and entity name instead of collapsing unrelated warehouses/tasks; exposes exact matched warehouse/object changes, task-failure evidence, confidence and a human-readable evidence line. Proposal keys carry entity scope so the app links only matching members. View only: no incident auto-declare or lifecycle-write changes.' AS DESCRIPTION
WHERE NOT EXISTS (
    SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 72
);
