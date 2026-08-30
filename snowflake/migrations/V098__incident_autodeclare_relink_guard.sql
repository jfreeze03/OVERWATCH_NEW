-- V098__incident_autodeclare_relink_guard.sql
--
-- SP_INCIDENT_AUTODECLARE must not re-link an already-membered alert. The proc (V032) can
-- attach an alert already a member of one incident to a second incident (double-count): the
-- `crit` CTE guards against an already-membered event seeding a NEW incident, but the member
-- INSERT independently re-scans ALERT_EVENTS with NO such guard. After an incident is resolved
-- while its CRITICAL alert stays OPEN (the closer never resolves member events), a new same-
-- family CRITICAL creates a second incident whose unguarded member INSERT re-attaches the old
-- still-OPEN alert -- INCIDENT_MEMBERS has no unique constraint, so it is counted twice.
--
-- Re-derives SP_INCIDENT_AUTODECLARE from V032 with the same anti-membership guard the crit CTE
-- uses added to the member INSERT (NOT EXISTS INCIDENT_MEMBERS m2 for this ALERT event), so an
-- event already linked to ANY incident is never re-attached.
--
-- Procedure re-derivation only, no schema change, no backfill. Owner applies in Snowsight after
-- V097; forward-healing (pre-existing double-membership is historical). This file never runs
-- from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20098, 'V098 requires V097 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 97) THEN
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
SELECT 98 AS VERSION,
       'SP_INCIDENT_AUTODECLARE re-link guard: re-derived from V032 so the member INSERT carries the same NOT EXISTS INCIDENT_MEMBERS anti-membership guard the crit CTE already has (alias m2), preventing an alert that is already a member of one incident (e.g. a still-OPEN CRITICAL whose incident was resolved without resolving the alert) from being re-attached to a second incident and double-counted in incident membership/metrics. Proc only, no schema change, no backfill; forward-healing (pre-existing double-membership is historical).' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 98);
