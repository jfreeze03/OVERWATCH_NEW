-- V099__incident_autodeclare_company_scope.sql
--
-- Scope SP_INCIDENT_AUTODECLARE's family-already-open guard by company. The proc groups
-- per (FAMILY, COMPANY) -- one incident per family per company -- but the family-already-
-- open NOT EXISTS guard correlated only on the family, never on i.COMPANY = c.COMPANY.
-- Because ALFA and Trexis share rule families (FAMILY = RULE_ID collides across companies),
-- a CRITICAL for one company was silently NOT auto-declared whenever the OTHER company had
-- an open/mitigated incident of the same family -- a cross-company incident coverage gap
-- until the other company's incident resolved. The manual-declare path already scopes by
-- company (live round 8); this brings the auto path to the symmetric scope.
--
-- Re-derives SP_INCIDENT_AUTODECLARE from V098 with `AND i.COMPANY = c.COMPANY` added to the
-- family-already-open guard. No schema change, no new object, no backfill. Owner applies in
-- Snowsight after V098; forward-healing (the next hourly TASK_INCIDENT_AUTODECLARE run
-- auto-declares the cross-company CRITICALs that were being suppressed). This file never runs
-- from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20099, 'V099 requires V098 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 98) THEN
        RAISE not_ready;
    END IF;
END;
$$;

CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_AUTODECLARE()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    enabled VARCHAR;
    made INT DEFAULT 0;
BEGIN
    SELECT COALESCE(MAX(VALUE), 'TRUE') INTO :enabled
    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS WHERE KEY = 'INCIDENT_AUTO_DECLARE_CRITICAL';
    IF (UPPER(:enabled) <> 'TRUE') THEN
        RETURN 'auto-declare off';
    END IF;

    CREATE OR REPLACE TEMPORARY TABLE _OW_AUTODECL AS
    WITH crit AS (
        SELECT e.EVENT_ID, e.COMPANY, e.SEVERITY, e.TITLE, e.RAISED_AT,
               SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 1) AS FAMILY
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
        WHERE UPPER(e.SEVERITY) = 'CRITICAL'
          AND e.STATUS IN ('OPEN', 'ACK')
          AND e.RAISED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
          AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
                          WHERE m.MEMBER_KIND = 'ALERT' AND m.REF_ID = e.EVENT_ID)
    )
    SELECT UUID_STRING() AS INCIDENT_ID, FAMILY, COMPANY,
           MAX_BY(TITLE, RAISED_AT) AS TITLE,
           MIN(RAISED_AT) AS FIRST_TS
    FROM crit c
    WHERE NOT EXISTS (
        SELECT 1
        FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m
        JOIN DBA_MAINT_DB.OVERWATCH.INCIDENTS i ON i.INCIDENT_ID = m.INCIDENT_ID
        JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS a ON a.EVENT_ID = m.REF_ID
        WHERE m.MEMBER_KIND = 'ALERT'
          AND i.STATUS IN ('OPEN', 'MITIGATED')
          AND i.COMPANY = c.COMPANY
          AND SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 1) = c.FAMILY
    )
    GROUP BY FAMILY, COMPANY;

    INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENTS
        (INCIDENT_ID, TITLE, SEVERITY, STATUS, COMPANY, DETECTED_AT, STARTED_AT,
         ROOT_CAUSE_KIND, DECLARED_BY)
    SELECT INCIDENT_ID, LEFT('Auto: ' || TITLE, 300), 'CRITICAL', 'OPEN', COMPANY,
           CURRENT_TIMESTAMP(), FIRST_TS, 'UNKNOWN', 'SP_INCIDENT_AUTODECLARE'
    FROM _OW_AUTODECL;

    INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS
        (INCIDENT_ID, MEMBER_KIND, REF_ID, EVIDENCE_TS, AUTO_LINKED, LINKED_BY)
    SELECT d.INCIDENT_ID, 'ALERT', e.EVENT_ID, e.RAISED_AT, TRUE, 'SP_INCIDENT_AUTODECLARE'
    FROM _OW_AUTODECL d
    JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
      ON e.COMPANY = d.COMPANY
     AND UPPER(e.SEVERITY) = 'CRITICAL'
     AND e.STATUS IN ('OPEN', 'ACK')
     AND e.RAISED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
     AND SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 1) = d.FAMILY
     AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m2
                     WHERE m2.MEMBER_KIND = 'ALERT' AND m2.REF_ID = e.EVENT_ID);

    SELECT COUNT(*) INTO :made FROM _OW_AUTODECL;
    RETURN 'auto-declared ' || :made || ' incident(s)';
END;
$$;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 99 AS VERSION,
       'SP_INCIDENT_AUTODECLARE family-open guard scoped by company: re-derived from V098 with AND i.COMPANY = c.COMPANY added to the family-already-open NOT EXISTS guard, so a CRITICAL for one company is auto-declared even when the other company has an open incident of the same (company-shared) rule family. Fixes a cross-company incident coverage gap; the manual-declare path already scopes by company. Proc only, no schema change, no backfill; forward-healing on the next hourly run.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 99);
