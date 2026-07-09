-- V025__break_glass_policy.sql — disable SEC_BREAK_GLASS_USE (owner policy).
--
-- 2026-07-08 live finding: the rule counts statements under ACCOUNTADMIN /
-- SNOW_ACCOUNTADMINS — but on this account those roles ARE the routine
-- operating roles (automation runs as SYSTEM under SNOW_ACCOUNTADMINS every
-- day: 772 statements = a normal Tuesday, not an incident). The rule watches
-- ONLY those two roles, so with them declared normal there is nothing left
-- for it to alert on. Owner decision (Joe, 2026-07-08): disable the rule.
--
-- What this does NOT change:
-- - Security → Changes → "Break-glass role activity" keeps showing the same
--   statement volumes for visibility — a panel, not a pager.
-- - The rule row stays in ALERT_CONFIG (audit trail + one-UPDATE re-enable
--   if the operating model ever changes).
-- Cleanup: bulk-resolve any open SEC_BREAK_GLASS_USE events as NOISE from
-- Alerts → Open events; they seed the threshold-suggestion evidence.
-- Config-as-code so both companies and any rebuild inherit the decision.
-- Idempotent. Apply IN ORDER after V024.

USE DATABASE DBA_MAINT_DB;
USE SCHEMA OVERWATCH;

EXECUTE IMMEDIATE $$
DECLARE
    v INT;
    not_ready EXCEPTION (-20025, 'BLOCKED: SCHEMA_VERSION < 24 - run migrations in order (see DEPLOYMENT.md)');
BEGIN
    SELECT COALESCE(MAX(VERSION), 0) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 24) THEN
        RAISE not_ready;
    END IF;
    RETURN 'ok';
END;
$$;

UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
   SET ENABLED = FALSE
 WHERE RULE_ID = 'SEC_BREAK_GLASS_USE';

MERGE INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION t
USING (SELECT 25 AS VERSION,
       'policy: SEC_BREAK_GLASS_USE disabled - ACCOUNTADMIN/SNOW_ACCOUNTADMINS are routine operating roles here' AS DESCRIPTION) s
ON t.VERSION = s.VERSION
WHEN NOT MATCHED THEN INSERT (VERSION, DESCRIPTION) VALUES (s.VERSION, s.DESCRIPTION);
