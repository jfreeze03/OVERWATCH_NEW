-- V175__admin_role_members_proc.sql
--
-- The admin-access lookup as an owner-run procedure, so it keeps answering after the app's owner changes
-- (owner decision 2026-10-06: SNOW_SYSADMINS will own and run OVERWATCH_APP; design decision D4).
--
-- WHY: since 4.610 the app decides who is an admin by running SHOW GRANTS OF ROLE SNOW_PRI_GFR_PRD_ALFA_DSA as
-- the app owner. SHOW lists only what the CURRENT role can see. If SNOW_SYSADMINS sees fewer DSA grantees than
-- SNOW_ACCOUNTADMINS does (SYSADMINS_OWNER_PREFLIGHT_v2: S2 differs from Z2), every DSA-only admin silently
-- drops to read-only at the cutover (the lookup fails closed). The named admins (config OPERATOR_USERS) are
-- never affected: they need no lookup.
--
--   + SP_ADMIN_ROLE_MEMBERS(): EXECUTE AS OWNER, so it runs as the role that applies this file
--     (SNOW_ACCOUNTADMINS, the owner of every OVERWATCH object). It runs the SAME SHOW GRANTS OF ROLE the app runs
--     today and returns its two columns, granted_to and grantee_name, unfiltered: the app keeps only the direct
--     USER rows and reports a ROLE grantee as nested, exactly as it does with SHOW. The role is HARD-CODED: no
--     argument, so the procedure can list this one role's grantees and nothing else.
--   + COPY GRANTS: a later CREATE OR REPLACE of this procedure keeps its USAGE grants (without it, a re-create
--     drops them and the lookup fails closed again).
--   + GRANT USAGE to SNOW_SYSADMINS: the future app owner can CALL it. In this file, so a re-run (or a rebuild
--     replay) re-grants it. SNOW_SYSADMINS already reads the same membership through
--     SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS; this discloses nothing new, it only removes the up-to-2h lag.
--
-- The app (4.610.2+) CALLs it once this version row exists (schema_gate.has_migration(175)) and runs SHOW
-- before that, so one build works before and after the apply, under either owner. The fail-closed paths are
-- unchanged: a failed CALL is lookup_failed, an answer with no USER row is unverified, neither ever makes an admin.
--
-- COST: none at apply time. Per lookup: one CALL (the SHOW and one RESULT_SCAN inside it, cloud services only,
-- no warehouse scan) in place of one SHOW, at the same cadence (once per viewer session per 5 min, plus the
-- write-time re-check).
-- NEEDED: before the SNOW_SYSADMINS cutover when the preflight's S2 differs from Z2. Harmless before that: under
-- today's owner the CALL returns what SHOW returns.
-- ROLLBACK: do not drop the procedure while this version row exists: the app would CALL a missing procedure and
-- every DSA-only admin would be read-only (fail closed). RUNBOOK §12, "Rolling back V175".
-- Apply AFTER V174, as SNOW_ACCOUNTADMINS (the role that owns the OVERWATCH schema). Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20175, 'V175 requires V174 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 174) THEN
        RAISE not_ready;
    END IF;
END;
$$;

CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ADMIN_ROLE_MEMBERS()
COPY GRANTS
RETURNS TABLE ("granted_to" VARCHAR, "grantee_name" VARCHAR)
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
-- V175: who holds SNOW_PRI_GFR_PRD_ALFA_DSA (config.ADMIN_ACCESS_ROLE), as this procedure's owner sees it. Read-only.
DECLARE
    res RESULTSET;
BEGIN
    SHOW GRANTS OF ROLE SNOW_PRI_GFR_PRD_ALFA_DSA;
    res := (SELECT "granted_to", "grantee_name" FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())));
    RETURN TABLE(res);
END;
$$;

GRANT USAGE ON PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ADMIN_ROLE_MEMBERS() TO ROLE SNOW_SYSADMINS;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 175 AS VERSION,
       'Owner decision 2026-10-06 (SNOW_SYSADMINS will own the app): SP_ADMIN_ROLE_MEMBERS(), an EXECUTE AS OWNER procedure that runs SHOW GRANTS OF ROLE SNOW_PRI_GFR_PRD_ALFA_DSA (hard-coded) and returns granted_to and grantee_name, created WITH COPY GRANTS, USAGE granted to SNOW_SYSADMINS. The app CALLs it for the admin-access lookup once this row exists, so the lookup answers as this procedure''s owner whatever role owns the app. Read-only; no task change, no procedure run at apply time.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 175);
