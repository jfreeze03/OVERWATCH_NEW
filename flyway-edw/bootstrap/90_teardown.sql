-- =====================================================================================================================
-- bootstrap/90_teardown.sql -- remove the kit's Snowflake objects (one runway, or everything shared)
-- =====================================================================================================================
-- HOUSE RULE  Every destructive statement in this file is COMMENTED OUT. Nothing here removes anything when the file
--             is run. To act, read the step, then remove the leading "-- " of ONE statement at a time, run it, check,
--             and move on. Only the variables (PART 0) and the read-only inventory (PART 1) run as written.
-- KEPT        This file never removes EDW data. The six runway databases are not kit objects and are not touched
--             here at all; neither is the ETL warehouse WH_EDW_ETL_<ID> (it runs the runway's own work). The FLYWAY
--             schema (Flyway history + deploy log: the audit trail) is kept unless step 2.9 is chosen deliberately.
-- REMOVED     (when uncommented) what bootstrap/10 and 20 created for the kit: the service users, the deploy and
--             auditor roles, the deploy and auditor warehouses, the network rule / network policy / authentication
--             policy, the admin schema, and leftovers of the deploy job (rehearsal clones ALFA_EDW_RH_<ID>) and of
--             bootstrap/30 (parked runways <runway>_PRE_REFRESH). The runway's access roles EDW_<ID>_<SCHEMA>_READ /
--             _WRITE (bootstrap/10 PART 2e) only if nothing outside the kit uses them (step 2.6b).
-- ORDER       Logins first (nothing can deploy while you work), then what references other things before what is
--             referenced: a user's policies are unset before the user goes; a network policy goes before its network
--             rule (a rule assigned to a policy cannot be dropped); an authentication policy goes only when no user
--             carries it (it cannot be dropped while assigned); the deploy role's objects are handed to SYSADMIN
--             before the role goes (step 2.6); shared objects only after the last runway's PART 2.
-- WHO         A Snowflake admin; each step names its role. Jenkins steps are done by the Jenkins admin first.
-- =====================================================================================================================


-- =====================================================================================================================
-- PART 0 -- variables (same names as bootstrap/10; run before PART 1 and before any step you uncomment)
-- =====================================================================================================================
SET runway = 'DEV';                         -- DEV | SAN | SEA | PHX | MGM | PRD (PART 2)
SET admin_db = 'EDW_ADMIN_DB';
SET admin_schema = 'SECURITY';
SET network_rule_name = 'NR_EDW_FLYWAY_JENKINS';
SET network_policy_name = 'NP_EDW_FLYWAY_JENKINS';
SET auth_policy_name = 'AP_EDW_FLYWAY_KEYPAIR';

-- Derived names. Do not edit.
SET admin_schema_fqn = $admin_db || '.' || $admin_schema;
SET network_rule_fqn = $admin_schema_fqn || '.' || $network_rule_name;
SET auth_policy_fqn = $admin_schema_fqn || '.' || $auth_policy_name;
SET runway_db = 'ALFA_EDW_' || $runway;
SET deploy_role = 'EDW_' || $runway || '_DEPLOYER';
SET svc_user = 'SVC_EDW_FLYWAY_' || $runway;
SET deploy_wh = 'WH_EDW_DEPLOY_' || $runway;
SET flyway_schema_fqn = $runway_db || '.FLYWAY';
SET flyway_backup_fqn = $runway_db || '.FLYWAY_BAK_' || TO_CHAR(CURRENT_DATE(), 'YYYYMMDD');
SET rehearsal_db = 'ALFA_EDW_RH_' || $runway;           -- only MGM / PRD ever have one
SET parked_db = $runway_db || '_PRE_REFRESH';              -- only after a bootstrap/30 refresh


-- =====================================================================================================================
-- PART 1 -- inventory (read-only; SECURITYADMIN sees every user, role and grant)
-- =====================================================================================================================
USE ROLE SECURITYADMIN;

EXECUTE IMMEDIATE $$
DECLARE
    u VARCHAR;
    r VARCHAR;
    v VARCHAR;
    c1 INTEGER;
    c2 INTEGER;
    acc VARCHAR;
    report VARCHAR DEFAULT '';
BEGIN
    SELECT $svc_user, $deploy_role INTO :u, :r;

    SHOW USERS LIKE 'SVC_EDW_FLYWAY_%';
    SELECT COUNT_IF(t."name" = :u), LISTAGG(t."name" || IFF(LOWER(t."disabled"::VARCHAR) = 'true', ' (disabled)', ''),
                                            ', ') WITHIN GROUP (ORDER BY t."name")
      INTO :c1, :v
      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
     WHERE STARTSWITH(t."name", 'SVC_EDW_FLYWAY_');
    report := report || 'runway user ' || u || ': ' || IFF(c1 > 0, 'present', 'absent') || CHR(10)
        || 'all kit service users: ' || COALESCE(v, '(none)')
        || ' -- the shared policies (PART 3) may go only when none of them is left' || CHR(10);

    BEGIN
        EXECUTE IMMEDIATE 'SHOW GRANTS TO ROLE ' || :r;
        SELECT LISTAGG(x.GRANTED_ON || ' x' || x.N, ', ') WITHIN GROUP (ORDER BY x.GRANTED_ON)
          INTO :v
          FROM (SELECT t."granted_on"::VARCHAR AS GRANTED_ON, COUNT(*) AS N
                  FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
                 WHERE t."privilege" = 'OWNERSHIP'
                 GROUP BY 1) AS x;
        report := report || 'role ' || r || ' owns: ' || COALESCE(v, 'nothing') || CHR(10);
        EXECUTE IMMEDIATE 'SHOW GRANTS TO ROLE ' || :r;
        SELECT LISTAGG(t."name", ', ')
          INTO :v
          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
         WHERE t."privilege" = 'OWNERSHIP' AND t."granted_on" = 'DATABASE';
        report := report || 'databases owned by ' || r || ' (rehearsal clones; step 2.3): ' || COALESCE(v, 'none')
            || CHR(10);
        EXECUTE IMMEDIATE 'SHOW GRANTS OF ROLE ' || :r;
        SELECT LISTAGG(t."granted_to" || ' ' || t."grantee_name", ', ')
          INTO :v
          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
        report := report || 'role ' || r || ' is held by: ' || COALESCE(v, 'nobody') || CHR(10);
    EXCEPTION
        WHEN OTHER THEN
            report := report || 'role ' || r || ': ' || SQLERRM || CHR(10);
    END;

    SHOW ROLES LIKE 'EDW_%';
    SELECT LISTAGG(IFF(ENDSWITH(t."name", '_DEPLOYER') OR t."name" = 'EDW_FLYWAY_AUDITOR', t."name", NULL), ', ')
               WITHIN GROUP (ORDER BY t."name"),
           COUNT_IF(t."name" = 'EDW_FLYWAY_AUDITOR'),
           LISTAGG(IFF(REGEXP_LIKE(t."name", 'EDW_[A-Z]+_[A-Z][A-Z0-9_]*_(READ|WRITE)'), t."name", NULL), ', ')
               WITHIN GROUP (ORDER BY t."name")
      INTO :v, :c2, :acc
      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
    report := report || 'kit roles: ' || COALESCE(v, '(none)') || '; auditor role '
        || IFF(c2 > 0, 'present', 'absent') || CHR(10)
        || 'access roles (bootstrap/10 PART 2e; step 2.6b): ' || COALESCE(NULLIF(acc, ''), '(none)') || CHR(10);
    RETURN report;
END;
$$;

SELECT s.INDEX AS LINE_NO, s.VALUE AS INVENTORY
  FROM (SELECT $1 AS REPORT FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))) AS b,
       TABLE(SPLIT_TO_TABLE(b.REPORT, CHR(10))) AS s
 WHERE s.VALUE <> ''
 ORDER BY s.INDEX;

-- Warehouses, rehearsal clones and parked runways visible to SYSADMIN:
USE ROLE SYSADMIN;
SHOW WAREHOUSES LIKE 'WH_EDW_%';
SHOW DATABASES LIKE 'ALFA_EDW_RH_%';
SHOW DATABASES LIKE '%_PRE_REFRESH';


-- =====================================================================================================================
-- PART 2 -- one runway's kit objects (runway set in PART 0). Every statement is commented out: see HOUSE RULE.
-- =====================================================================================================================

-- 2.1  Jenkins admin, first: disable the job EDW/deploy/<ID>/deploy and delete the credential sf-flyway-<ID> from
--      the folder EDW/deploy/<ID>. Then nothing can deploy to this runway while the steps below run.

-- 2.2  USERADMIN -- stop the login (reversible: SET DISABLED = FALSE).
-- USE ROLE USERADMIN;
-- ALTER USER IDENTIFIER($svc_user) SET DISABLED = TRUE;

-- 2.3  Leftovers. The rehearsal clone (MGM / PRD only; normally the deploy job has dropped it) is owned by the deploy
--      role; a parked runway from bootstrap/30 by SYSADMIN. Check PART 1 first: both names come from PART 0.
-- USE ROLE IDENTIFIER($deploy_role);
-- DROP DATABASE IF EXISTS IDENTIFIER($rehearsal_db);
-- USE ROLE SYSADMIN;
-- DROP DATABASE IF EXISTS IDENTIFIER($parked_db);

-- 2.4  SECURITYADMIN -- take the policies off the user (an attached authentication policy blocks its own drop later).
-- USE ROLE SECURITYADMIN;
-- ALTER USER IDENTIFIER($svc_user) UNSET AUTHENTICATION POLICY;
-- ALTER USER IDENTIFIER($svc_user) UNSET NETWORK_POLICY;

-- 2.5  USERADMIN -- the service user (its key pair goes with it; also delete the .p8 from the vault).
-- USE ROLE USERADMIN;
-- DROP USER IF EXISTS IDENTIFIER($svc_user);

-- 2.6  The deploy role. Dropping a role hands everything it owns to the role that runs DROP ROLE. So the role is
--      first given to SYSADMIN, which then drops it: the runway's schemas and objects end up owned by SYSADMIN (the
--      database's owner), whole task graphs at once, so their links survive. Its database grants and its account
--      privileges (EXECUTE TASK, CREATE DATABASE) disappear with it. Verify on first run, afterwards:
--      SHOW SCHEMAS IN DATABASE <runway> (owner SYSADMIN) and SHOW TASKS IN DATABASE <runway> (resume what must
--      keep running; the new owner needs EXECUTE TASK for that).
-- USE ROLE USERADMIN;
-- GRANT OWNERSHIP ON ROLE IDENTIFIER($deploy_role) TO ROLE SYSADMIN COPY CURRENT GRANTS;
-- USE ROLE SYSADMIN;
-- DROP ROLE IF EXISTS IDENTIFIER($deploy_role);

-- 2.6b USERADMIN -- the runway's access roles (bootstrap/10 PART 2e), ONLY if no consumer outside the kit holds or
--      uses them (SHOW GRANTS OF ROLE <role> first): the layer-90 grants files are their only source of privileges,
--      and a dropped role takes every grant to it along. One line per role listed by PART 1, for example:
-- USE ROLE USERADMIN;
-- DROP ROLE IF EXISTS EDW_<ID>_SALES_READ;

-- 2.7  SYSADMIN -- the deploy warehouse. (WH_EDW_ETL_<ID> is NOT removed: it runs the runway's own tasks.)
-- USE ROLE SYSADMIN;
-- DROP WAREHOUSE IF EXISTS IDENTIFIER($deploy_wh);

-- 2.8  Only if the auditor stays for other runways (PART 3 removes the role, and with it all of these at once):
-- USE ROLE SYSADMIN;
-- REVOKE USAGE ON DATABASE IDENTIFIER($runway_db) FROM ROLE EDW_FLYWAY_AUDITOR;

-- 2.9  Optional and normally NOT done: the FLYWAY schema holds the history and the deploy log of every release to
--      this runway. Only when the database leaves Flyway for good and those records are archived elsewhere. Keep a
--      zero-copy backup first; the drop stays recoverable with UNDROP SCHEMA for the retention period.
-- USE ROLE SYSADMIN;
-- CREATE SCHEMA IDENTIFIER($flyway_backup_fqn) CLONE IDENTIFIER($flyway_schema_fqn);
-- DROP SCHEMA IF EXISTS IDENTIFIER($flyway_schema_fqn);


-- =====================================================================================================================
-- PART 3 -- shared objects, only after PART 2 for every runway (PART 1 lists no SVC_EDW_FLYWAY_<ID> user any more)
-- =====================================================================================================================

-- 3.1  Jenkins admin, first: delete the credential sf-flyway-AUDIT from EDW/ci and from every EDW/deploy/<ID>.

-- 3.2  The auditor login: stop it, unset its policies, then remove user, role (it owns nothing) and warehouse.
-- USE ROLE USERADMIN;
-- ALTER USER SVC_EDW_FLYWAY_AUDIT SET DISABLED = TRUE;
-- USE ROLE SECURITYADMIN;
-- ALTER USER SVC_EDW_FLYWAY_AUDIT UNSET AUTHENTICATION POLICY;
-- ALTER USER SVC_EDW_FLYWAY_AUDIT UNSET NETWORK_POLICY;
-- USE ROLE USERADMIN;
-- DROP USER IF EXISTS SVC_EDW_FLYWAY_AUDIT;
-- DROP ROLE IF EXISTS EDW_FLYWAY_AUDITOR;
-- USE ROLE SYSADMIN;
-- DROP WAREHOUSE IF EXISTS WH_EDW_FLYWAY_AUDIT;

-- 3.3  SECURITYADMIN -- the policies, in this order: the network policy, then its rule; the authentication policy
--      last (both fail with a clear error while a user still carries them: back to PART 2 / 3.2).
-- USE ROLE SECURITYADMIN;
-- DROP NETWORK POLICY IF EXISTS IDENTIFIER($network_policy_name);
-- DROP NETWORK RULE IF EXISTS IDENTIFIER($network_rule_fqn);
-- DROP AUTHENTICATION POLICY IF EXISTS IDENTIFIER($auth_policy_fqn);

-- 3.4  ACCOUNTADMIN -- the one ON ACCOUNT grant bootstrap/10 gave SECURITYADMIN; only if nothing else relies on it.
-- USE ROLE ACCOUNTADMIN;
-- REVOKE APPLY AUTHENTICATION POLICY ON ACCOUNT FROM ROLE SECURITYADMIN;

-- 3.5  SYSADMIN -- the admin schema and database, only if they hold nothing but the kit's objects
--      (SHOW OBJECTS IN SCHEMA / SHOW NETWORK RULES / SHOW AUTHENTICATION POLICIES in it first).
-- USE ROLE SYSADMIN;
-- DROP SCHEMA IF EXISTS IDENTIFIER($admin_schema_fqn);
-- DROP DATABASE IF EXISTS IDENTIFIER($admin_db);
