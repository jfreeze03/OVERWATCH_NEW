-- V170__incident_declare_actor_and_proposals.sql
--
-- Incident declare + proposals (bug-hunt round 2: R2-028, R2-030, R2-093, R2-031).
--
-- WHY:
--   R2-028  The manual declare (Control Room > Incidents & triage > Proposed incidents) wrote DECLARED_BY and every
--           member's LINKED_BY from their V032 default, CURRENT_USER(). SP_INCIDENT_DECLARE is EXECUTE AS OWNER and
--           the app is owner-rights SiS, so that is the app owner for every DBA. Control Room shows the column as
--           "Declared by". A NEW 5-arg overload takes P_ACTOR (the app passes identity_sql()) and writes both columns.
--   R2-030  The proc committed an incident whose members INSERT linked 0 rows (the proposal list is up to 30 s
--           stale; a sweep, another operator or a concurrent auto-declare can empty the linkable set first) and
--           returned 'DECLARED: 0 member(s) linked', which the app never read. Nothing ever attaches to or
--           mitigates such an incident. Both overloads now roll that back and return a NOOP, and the success
--           verdict becomes 'OK: declared <id> with <n> member(s) linked'.
--   R2-093  INCIDENT_PROPOSALS (V072) knew only V072-era rules and band tokens. An EXH band (COST_CONTRACT_BREACH,
--           PIPE_ETL_CYCLE_LATE) read as an entity called EXH, so the declare guard looked only for an incident
--           holding an EXH member and opened a SECOND incident for the same late night; 'ALL' read as an entity
--           too. Ten more rules whose key part 2 is a bare name get their entity kind: WAREHOUSE COST_IDLE_OPPORTUNITY,
--           COST_SLEEP_POLLING; OBJECT PIPE_DT_FAILURES, PIPE_VOLUME_DROP, DQ_BREACH, DQ_SCHEMA_DRIFT; USER
--           SEC_FAILED_LOGINS, SEC_LOGIN_TAKEOVER, SEC_ADMIN_GRANT, COST_AI_USER_RUNAWAY. Series-prefixed keys stay
--           SCOPE, as in V072: COST_CLOUD_SVC_ANOMALY ('CLOUD SVC <WH>') and COST_ANOMALY_SWEEP ('WAREHOUSE <WH>').
--           The declare matches the raw part 2, so ENTITY_NAME keeps the prefix and those proposals never match
--           warehouse-change evidence (they cannot reach CONFIDENCE HIGH).
--   R2-031  A PIPE_TASK_FAILURES proposal counted its OWN FACT_TASK_DAILY failures as corroboration, so every one
--           read CONFIDENCE HIGH. They now reach HIGH only with a matching task change (MEDIUM on repeat days).
--
--   ~ SP_INCIDENT_DECLARE(VARCHAR x4) re-derived from V131 (its only definer): the 0-member rollback + OK verdict.
--   + SP_INCIDENT_DECLARE(VARCHAR x5) derived from V131: the same body plus P_ACTOR -> DECLARED_BY / LINKED_BY.
--     The 4-arg is KEPT so an app that is not redeployed yet still declares (it keeps crediting the owner). The
--     app CALLs the 5-arg once SCHEMA_VERSION holds 170: while its 4 h schema cache lacks 170 it re-reads the
--     table on a 30 s tier, so the first declare after the apply already does. A later migration drops the 4-arg.
--   ~ INCIDENT_PROPOSALS re-derived from V072 (its current definer): rule kinds, EXH/ALL band tokens, and the
--     PIPE_TASK_FAILURES confidence + evidence label. Same columns, same order (the app reads SELECT *).
--
-- COST: none at apply (two proc swaps and one view swap). The view is computed at read time on the same tables;
-- the declare proc gains one IF.
-- FIRST RUN: the next proposal read re-classifies every open proposal; a manual declare from a 4.609.0 app made
-- 30 s or more after the apply writes the declaring DBA (Control Room's SQL preview then ends with the viewer as a
-- 5th argument). Nothing runs at apply time. History is not rewritten: earlier manual declares
-- keep the app owner as Declared by (an optional, owner-run heuristic repair is staged separately).
-- ROLLBACK: re-run V131's CREATE PROCEDURE and V072's CREATE VIEW. Remove the 5-arg overload (its teardown.sql
-- line names the signature) only once no deployed app calls it: a 4.609.0 app on a V170 schema CALLs the 5-arg.
-- Apply AFTER V169. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20170, 'V170 requires V169 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 169) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_INCIDENT_DECLARE  (from V131; 4-arg kept for un-redeployed apps: a member-less declare rolls back with a NOOP, success verdict OK, V170)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_DECLARE(
    P_TITLE VARCHAR,
    P_SEVERITY VARCHAR,
    P_COMPANY VARCHAR,
    P_PROPOSAL_KEY VARCHAR
)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    inc_id STRING;
    family STRING;
    entity_kind STRING;
    entity_name STRING;
    apply_entity BOOLEAN;
    created INT DEFAULT 0;
    members INT DEFAULT 0;
BEGIN
    IF (P_PROPOSAL_KEY IS NULL OR TRIM(P_PROPOSAL_KEY) = '') THEN
        RETURN 'INVALID: proposal key is required';
    END IF;
    inc_id := UUID_STRING();
    family := SPLIT_PART(:P_PROPOSAL_KEY, '|', 1);
    entity_kind := UPPER(SPLIT_PART(:P_PROPOSAL_KEY, '|', 3));
    -- entity_name is the 4th pipe-field. Realistic entity names (warehouse / db / task) contain no
    -- '|' -- a pipe would itself corrupt the pipe-delimited DEDUPE_KEY this is matched against
    -- (SPLIT_PART(...,2)) -- so SPLIT_PART(,4) equals the app's split('|',3) remainder for every
    -- real key, and matching a single field against the single DEDUPE_KEY entity position is correct.
    entity_name := SPLIT_PART(:P_PROPOSAL_KEY, '|', 4);
    -- Entity filter applies only for a non-ACCOUNT proposal that names an entity (matches the app's
    -- `len(parts)==4 and parts[2] != 'ACCOUNT'`); ACCOUNT proposals use the family-only scope.
    apply_entity := (ARRAY_SIZE(SPLIT(:P_PROPOSAL_KEY, '|')) >= 4 AND entity_kind <> 'ACCOUNT');

    BEGIN TRANSACTION;

    -- 1) open the incident, UNLESS an OPEN/MITIGATED incident already covers this (family, company
    --    [, entity]) — the family-already-open guard (identical predicate to the app's pre-check).
    INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENTS
        (INCIDENT_ID, TITLE, SEVERITY, STATUS, COMPANY, DETECTED_AT, ROOT_CAUSE_KIND)
    SELECT :inc_id, LEFT(:P_TITLE, 300), UPPER(:P_SEVERITY), 'OPEN', :P_COMPANY,
           CURRENT_TIMESTAMP(), 'UNKNOWN'
    WHERE NOT EXISTS (
        SELECT 1
        FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
        JOIN DBA_MAINT_DB.OVERWATCH.INCIDENTS i ON i.INCIDENT_ID = m.INCIDENT_ID
        JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS a ON a.EVENT_ID = m.REF_ID
        WHERE m.MEMBER_KIND = 'ALERT' AND i.STATUS IN ('OPEN', 'MITIGATED')
          AND (i.COMPANY = :P_COMPANY OR UPPER(i.COMPANY) = 'ALL')
          AND SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 1) = :family
          AND (NOT :apply_entity
               OR UPPER(SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 2)) = UPPER(:entity_name))
    );

    -- 2) link every open/ack alert of this family (+entity) in the 48h window as members, per-alert
    --    de-duplicated, but only if the incident row was actually created above.
    INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS
        (INCIDENT_ID, MEMBER_KIND, REF_ID, EVIDENCE_TS, AUTO_LINKED)
    SELECT :inc_id, 'ALERT', e.EVENT_ID, e.RAISED_AT, FALSE
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
    WHERE e.STATUS IN ('OPEN', 'ACK')
      AND e.RAISED_AT >= DATEADD('day', -2, CURRENT_TIMESTAMP())
      AND (e.COMPANY = :P_COMPANY OR UPPER(e.COMPANY) = 'ALL')
      AND SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 1) = :family
      AND (NOT :apply_entity
           OR UPPER(SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 2)) = UPPER(:entity_name))
      AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
                      WHERE m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID)
      AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS i2
                  WHERE i2.INCIDENT_ID = :inc_id);

    SELECT COUNT(*) INTO :created FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS WHERE INCIDENT_ID = :inc_id;
    SELECT COUNT(*) INTO :members FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS WHERE INCIDENT_ID = :inc_id;

    -- V170 (R2-030): never commit a member-less incident. The proposal the operator declared from can be up
    -- to 30 s stale, and an auto-clear or condition-ended sweep, another operator resolving the alerts, or a
    -- concurrent auto-declare can take every proposal alert out of OPEN/ACK (or into another incident)
    -- before this CALL; the members INSERT then links 0 rows. Undo the incident row (it was never visible
    -- outside this transaction) and say so.
    IF (:created = 1 AND :members = 0) THEN
        ROLLBACK;
        RETURN 'NOOP: no open alerts left to link - nothing declared';
    END IF;

    COMMIT;

    IF (:created = 0) THEN
        RETURN 'NOOP: this family already has an open incident';
    END IF;
    RETURN 'OK: declared ' || :inc_id || ' with ' || :members || ' member(s) linked';
EXCEPTION
    WHEN OTHER THEN
        ROLLBACK;
        RAISE;
END;
$$;

-- >>> derived:SP_INCIDENT_DECLARE  (from V131; NEW 5-arg overload: + P_ACTOR -> DECLARED_BY / LINKED_BY, with the same rollback and OK verdict, V170)
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_DECLARE(
    P_TITLE VARCHAR,
    P_SEVERITY VARCHAR,
    P_COMPANY VARCHAR,
    P_PROPOSAL_KEY VARCHAR,
    P_ACTOR VARCHAR
)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    inc_id STRING;
    v_actor STRING;
    family STRING;
    entity_kind STRING;
    entity_name STRING;
    apply_entity BOOLEAN;
    created INT DEFAULT 0;
    members INT DEFAULT 0;
BEGIN
    IF (P_PROPOSAL_KEY IS NULL OR TRIM(P_PROPOSAL_KEY) = '') THEN
        RETURN 'INVALID: proposal key is required';
    END IF;
    inc_id := UUID_STRING();
    -- V170 (R2-028): the DBA who declared. The app passes identity_sql() (st.user under owner-rights SiS);
    -- CURRENT_USER() there is the app owner, so it is only the fallback. LEFT(..., 200) = the width of
    -- INCIDENTS.DECLARED_BY and INCIDENT_MEMBERS.LINKED_BY (V032).
    v_actor := LEFT(COALESCE(NULLIF(TRIM(:P_ACTOR), ''), CURRENT_USER()), 200);
    family := SPLIT_PART(:P_PROPOSAL_KEY, '|', 1);
    entity_kind := UPPER(SPLIT_PART(:P_PROPOSAL_KEY, '|', 3));
    -- entity_name is the 4th pipe-field. Realistic entity names (warehouse / db / task) contain no
    -- '|' -- a pipe would itself corrupt the pipe-delimited DEDUPE_KEY this is matched against
    -- (SPLIT_PART(...,2)) -- so SPLIT_PART(,4) equals the app's split('|',3) remainder for every
    -- real key, and matching a single field against the single DEDUPE_KEY entity position is correct.
    entity_name := SPLIT_PART(:P_PROPOSAL_KEY, '|', 4);
    -- Entity filter applies only for a non-ACCOUNT proposal that names an entity (matches the app's
    -- `len(parts)==4 and parts[2] != 'ACCOUNT'`); ACCOUNT proposals use the family-only scope.
    apply_entity := (ARRAY_SIZE(SPLIT(:P_PROPOSAL_KEY, '|')) >= 4 AND entity_kind <> 'ACCOUNT');

    BEGIN TRANSACTION;

    -- 1) open the incident, UNLESS an OPEN/MITIGATED incident already covers this (family, company
    --    [, entity]) — the family-already-open guard (identical predicate to the app's pre-check).
    INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENTS
        (INCIDENT_ID, TITLE, SEVERITY, STATUS, COMPANY, DETECTED_AT, ROOT_CAUSE_KIND, DECLARED_BY)
    SELECT :inc_id, LEFT(:P_TITLE, 300), UPPER(:P_SEVERITY), 'OPEN', :P_COMPANY,
           CURRENT_TIMESTAMP(), 'UNKNOWN', :v_actor
    WHERE NOT EXISTS (
        SELECT 1
        FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
        JOIN DBA_MAINT_DB.OVERWATCH.INCIDENTS i ON i.INCIDENT_ID = m.INCIDENT_ID
        JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS a ON a.EVENT_ID = m.REF_ID
        WHERE m.MEMBER_KIND = 'ALERT' AND i.STATUS IN ('OPEN', 'MITIGATED')
          AND (i.COMPANY = :P_COMPANY OR UPPER(i.COMPANY) = 'ALL')
          AND SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 1) = :family
          AND (NOT :apply_entity
               OR UPPER(SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 2)) = UPPER(:entity_name))
    );

    -- 2) link every open/ack alert of this family (+entity) in the 48h window as members, per-alert
    --    de-duplicated, but only if the incident row was actually created above.
    INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS
        (INCIDENT_ID, MEMBER_KIND, REF_ID, EVIDENCE_TS, AUTO_LINKED, LINKED_BY)
    SELECT :inc_id, 'ALERT', e.EVENT_ID, e.RAISED_AT, FALSE, :v_actor
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
    WHERE e.STATUS IN ('OPEN', 'ACK')
      AND e.RAISED_AT >= DATEADD('day', -2, CURRENT_TIMESTAMP())
      AND (e.COMPANY = :P_COMPANY OR UPPER(e.COMPANY) = 'ALL')
      AND SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 1) = :family
      AND (NOT :apply_entity
           OR UPPER(SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 2)) = UPPER(:entity_name))
      AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
                      WHERE m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID)
      AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS i2
                  WHERE i2.INCIDENT_ID = :inc_id);

    SELECT COUNT(*) INTO :created FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS WHERE INCIDENT_ID = :inc_id;
    SELECT COUNT(*) INTO :members FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS WHERE INCIDENT_ID = :inc_id;

    -- V170 (R2-030): never commit a member-less incident. The proposal the operator declared from can be up
    -- to 30 s stale, and an auto-clear or condition-ended sweep, another operator resolving the alerts, or a
    -- concurrent auto-declare can take every proposal alert out of OPEN/ACK (or into another incident)
    -- before this CALL; the members INSERT then links 0 rows. Undo the incident row (it was never visible
    -- outside this transaction) and say so.
    IF (:created = 1 AND :members = 0) THEN
        ROLLBACK;
        RETURN 'NOOP: no open alerts left to link - nothing declared';
    END IF;

    COMMIT;

    IF (:created = 0) THEN
        RETURN 'NOOP: this family already has an open incident';
    END IF;
    RETURN 'OK: declared ' || :inc_id || ' with ' || :members || ' member(s) linked';
EXCEPTION
    WHEN OTHER THEN
        ROLLBACK;
        RAISE;
END;
$$;

-- >>> derived:INCIDENT_PROPOSALS  (from V072; EXH/ALL band tokens -> ACCOUNT, + user/warehouse/object rule kinds, PIPE_TASK_FAILURES no longer self-corroborates, V170)
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
                                'WH_CHANGE_REGRESSION', 'COST_IDLE_OPPORTUNITY',
                                'COST_SLEEP_POLLING') THEN 'WAREHOUSE'
             WHEN r.RULE_ID IN ('PIPE_TASK_FAILURES', 'PIPE_COPY_FAILURES',
                                'PERF_CHANGE_REGRESSION', 'PIPE_DT_FAILURES',
                                'PIPE_VOLUME_DROP', 'DQ_BREACH', 'DQ_SCHEMA_DRIFT') THEN 'OBJECT'
             WHEN r.RULE_ID = 'COST_STORAGE_SURGE' THEN 'DATABASE'
             WHEN r.RULE_ID = 'COST_SERVERLESS_CREEP' THEN 'SERVICE'
             WHEN r.RULE_ID IN ('SEC_CRED_EXPIRY', 'SEC_NEW_ADMIN_NETWORK', 'SEC_FAILED_LOGINS',
                                'SEC_LOGIN_TAKEOVER', 'SEC_ADMIN_GRANT',
                                'COST_AI_USER_RUNAWAY') THEN 'USER'
             WHEN r.RULE_ID = 'COST_DEPT_BUDGET_PACE' THEN 'DEPARTMENT'
             WHEN COALESCE(r.ENTITY_RAW, '') = ''
               OR TRY_TO_DATE(r.ENTITY_RAW) IS NOT NULL
               OR UPPER(r.ENTITY_RAW) IN ('DAILY', 'WARN', 'CRIT', 'MED', 'HIGH', 'EXH', 'ALL')
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
         WHEN MATCHED_WH_CHANGES + MATCHED_OBJECT_CHANGES
              + IFF(FAMILY = 'PIPE_TASK_FAILURES', 0, MATCHED_TASK_FAILURES) > 0 THEN 'HIGH'
         WHEN ENTITY_KIND <> 'ACCOUNT' AND ALERTS >= 2 THEN 'MEDIUM'
         ELSE 'LOW'
       END AS CONFIDENCE,
       'alerts=' || ALERTS ||
       '; matched warehouse changes=' || MATCHED_WH_CHANGES ||
       '; matched object changes=' || MATCHED_OBJECT_CHANGES ||
       IFF(FAMILY = 'PIPE_TASK_FAILURES', '; task failures (alert source, not corroboration)=',
           '; matched task failures=') || MATCHED_TASK_FAILURES AS EVIDENCE
FROM evidence;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 170 AS VERSION,
       'Incident declare + proposals (R2-028, R2-030, R2-093, R2-031): SP_INCIDENT_DECLARE re-derived from V131 (4-arg kept) plus a NEW 5-arg overload with P_ACTOR that writes INCIDENTS.DECLARED_BY and INCIDENT_MEMBERS.LINKED_BY (the app passes the viewer once V170 is applied; CURRENT_USER() under the owner-rights app is the owner). Both overloads roll back a declare whose members INSERT linked 0 rows and return NOOP: no open alerts left to link, and the success verdict is OK: declared <id> with <n> member(s) linked. INCIDENT_PROPOSALS re-derived from V072: EXH and ALL band tokens classify ACCOUNT (an EXH band no longer opens a second incident for the same family), ten more user, warehouse and object rules whose key carries a bare name get their entity kind (series-prefixed keys such as CLOUD SVC <WH> stay SCOPE), and a PIPE_TASK_FAILURES proposal no longer counts its own task failures as corroboration (HIGH only with a matching task change; its evidence labels the count as the alert source). Same view columns. No data change, nothing runs at apply.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 170);
