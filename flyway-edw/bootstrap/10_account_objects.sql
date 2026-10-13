-- =====================================================================================================================
-- bootstrap/10_account_objects.sql -- account-level objects for the ALFA EDW Flyway kit
-- =====================================================================================================================
-- WHAT    Everything Flyway and Jenkins log in with. None of it may live in a migration (guard FG007, rule ALFA06):
--           PART 0  variables + a guard
--           PART 1  once:        admin database/schema for policies (default EDW_ADMIN_DB.SECURITY), the Jenkins
--                                network rule + network policy, the key-pair-only authentication policy, and the
--                                read-only auditor (role EDW_FLYWAY_AUDITOR, warehouse WH_EDW_FLYWAY_AUDIT, user
--                                SVC_EDW_FLYWAY_AUDIT)
--           PART 2  per runway:  warehouse WH_EDW_DEPLOY_<ID>, ETL warehouse WH_EDW_ETL_<ID>, role EDW_<ID>_DEPLOYER
--                                (granted to SYSADMIN), service user SVC_EDW_FLYWAY_<ID>, EXECUTE TASK, and CREATE
--                                DATABASE for MGM / PRD only
--           PART 3  after PART 1 and after every PART 2: network + authentication policy on every SVC_EDW_FLYWAY_*
--           PART 4  public keys: the auditor's once, then each runway's own key
--           PART 5  verification: a PASS / FAIL report for the runway in the SET block
-- WHO     A Snowflake admin, in Snowsight, deliberately. Never the pipeline. Every section names the role it needs.
-- HOW     First time: edit PART 0, run PART 0 + 1, then PART 0 + 2 + 3 + 4 + 5 with runway = 'DEV'.
--         Each further runway: set runway, run PART 0 + 2 + 3 + 4b + 5. Every statement is idempotent; re-running a
--         part is safe. "Run All" in Snowsight stops at the first error, which is what you want here.
--         Keep a warehouse selected in the worksheet: the blocks read SHOW / DESC output with RESULT_SCAN, which
--         needs one. People's sessions use their default secondary roles (ALL), so the warehouse you normally use
--         stays usable after the USE ROLE switches below; if a block says "No active warehouse", USE WAREHOUSE yours.
-- NAMES   Must match runways.toml ([kit] auditor_user / auditor_role / auditor_warehouse; [runways.<ID>]
--         deploy_role / service_user / deploy_warehouse) and flyway.toml (placeholder etl_wh = WH_EDW_ETL_<ID>).
--         Change a name there and here in the same pull request.
--
-- WHY THESE ROLES (the least that works; each section switches explicitly)
--   SYSADMIN       creates and owns the admin database and the warehouses. Every EDW_<ID>_DEPLOYER and the auditor
--                  role are granted to SYSADMIN, so the runway objects roll up like every other custom role. That
--                  also means SYSADMIN holders can act as any deploy role: keep that group small (GUIDE B7).
--   USERADMIN      creates the roles and users and owns them.
--   SECURITYADMIN  all grants (MANAGE GRANTS), network rules / policies, authentication policies, and attaching
--                  them to users. It inherits USERADMIN, so it can alter the users USERADMIN owns.
--   ACCOUNTADMIN   only ON ACCOUNT privileges, in two short blocks: APPLY AUTHENTICATION POLICY for SECURITYADMIN
--                  (PART 1d) and EXECUTE TASK / CREATE DATABASE for a deploy role (PART 2c). Snowflake documents
--                  EXECUTE TASK being granted by ACCOUNTADMIN; keeping every ON ACCOUNT grant in those two blocks
--                  makes them easy to audit.
--
-- KEY PAIR (on a secured admin workstation; one key pair per service user, never shared between runways)
--   umask 077
--   U=SVC_EDW_FLYWAY_DEV                                         # or SVC_EDW_FLYWAY_AUDIT, SVC_EDW_FLYWAY_SAN, ...
--   openssl genrsa 2048 | openssl pkcs8 -topk8 -v2 aes256 -inform PEM -out "$U.p8"   # encrypted PKCS#8; asks for
--                                                                                     # the passphrase
--   openssl rsa -in "$U.p8" -pubout -out "$U.pub"                                     # public key (asks again)
--   grep -v -- '-----' "$U.pub" | tr -d '\n'; echo                                    # one line, no BEGIN/END lines:
--                                                                                     # paste it in PART 4
--   openssl rsa -pubin -in "$U.pub" -outform DER | openssl dgst -sha256 -binary | openssl enc -base64
--                                                       # fingerprint: PART 5 shows RSA_PUBLIC_KEY_FP = SHA256:<this>
--   The .p8 file + passphrase go to the Jenkins admin as an "SSH Username with private key" credential (username =
--   the service user) in the runway's folder only: sf-flyway-<ID> in EDW/deploy/<ID>, sf-flyway-AUDIT for the
--   auditor (docs/GUIDE.md B4). Then delete the local copy (shred -u "$U.p8") or file it in the enterprise vault.
--   The JDBC driver reads an encrypted key only with JAVA_ARGS
--   -Dnet.snowflake.jdbc.useBundledBouncyCastleForPrivateKeyDecryption=true (the Jenkinsfiles set it).
-- =====================================================================================================================


-- =====================================================================================================================
-- PART 0 -- variables (run this block before any other part; session variables last for the worksheet session)
-- =====================================================================================================================
SET runway = 'DEV';                                   -- DEV | SAN | SEA | PHX | MGM | PRD  (PART 2, 4b and 5)

-- Public egress addresses of the Jenkins agents (the addresses Snowflake sees), comma-separated IPv4 a.b.c.d or
-- a.b.c.d/8 .. /32. The value below is a documentation address (RFC 5737) that PART 1c refuses on purpose (as it
-- refuses 0.x and the other documentation ranges): replace it with yours. A session variable holds at most 256
-- characters; for a longer list, use wider CIDR blocks.
SET jenkins_egress_cidrs = '192.0.2.10/32';

-- Where the network rule and the authentication policy live. Never inside an ALFA_EDW_* database: a runway refresh
-- (bootstrap/30) or rehearsal clone would copy or replace them.
SET admin_db = 'EDW_ADMIN_DB';
SET admin_schema = 'SECURITY';
SET network_rule_name = 'NR_EDW_FLYWAY_JENKINS';
SET network_policy_name = 'NP_EDW_FLYWAY_JENKINS';
SET auth_policy_name = 'AP_EDW_FLYWAY_KEYPAIR';

-- Derived names (runways.toml / flyway.toml conventions). Do not edit.
SET admin_schema_fqn = $admin_db || '.' || $admin_schema;
SET deploy_role = 'EDW_' || $runway || '_DEPLOYER';
SET svc_user = 'SVC_EDW_FLYWAY_' || $runway;
SET deploy_wh = 'WH_EDW_DEPLOY_' || $runway;
SET etl_wh = 'WH_EDW_ETL_' || $runway;

-- Guard: a typo in runway would otherwise create a seventh role/user/warehouse family.
EXECUTE IMMEDIATE $$
DECLARE
    rw VARCHAR;
    adb VARCHAR;
    asch VARCHAR;
    nr VARCHAR;
    np VARCHAR;
    ap VARCHAR;
    bad_runway EXCEPTION (-20101, 'PART 0: runway must be one of DEV, SAN, SEA, PHX, MGM, PRD (upper case).');
    bad_name EXCEPTION (-20102, 'PART 0: admin_db, admin_schema and the policy names must be plain upper-case names.');
BEGIN
    SELECT $runway, $admin_db, $admin_schema, $network_rule_name, $network_policy_name, $auth_policy_name
      INTO :rw, :adb, :asch, :nr, :np, :ap;
    IF (rw IS NULL OR rw NOT IN ('DEV', 'SAN', 'SEA', 'PHX', 'MGM', 'PRD')) THEN
        RAISE bad_runway;
    END IF;
    IF (NOT (REGEXP_LIKE(adb, '[A-Z][A-Z0-9_]*') AND REGEXP_LIKE(asch, '[A-Z][A-Z0-9_]*')
             AND REGEXP_LIKE(nr, '[A-Z][A-Z0-9_]*') AND REGEXP_LIKE(np, '[A-Z][A-Z0-9_]*')
             AND REGEXP_LIKE(ap, '[A-Z][A-Z0-9_]*'))) THEN
        RAISE bad_name;
    END IF;
    RETURN 'PART 0 OK: runway ' || rw || ' -> EDW_' || rw || '_DEPLOYER / SVC_EDW_FLYWAY_' || rw
        || ' / WH_EDW_DEPLOY_' || rw || '; policies in ' || adb || '.' || asch;
END;
$$;


-- =====================================================================================================================
-- PART 1 -- shared objects (once; re-running is safe and re-applies the CIDR list)
-- =====================================================================================================================

-- ---------------------------------------------------------------------------------------------------- 1a SYSADMIN
-- SYSADMIN owns databases and warehouses in this account; SECURITYADMIN gets exactly what it needs to create the
-- network rule and the authentication policy in the admin schema (both are schema-level objects).
USE ROLE SYSADMIN;

CREATE DATABASE IF NOT EXISTS IDENTIFIER($admin_db)
    COMMENT = 'Account security objects (network rules, authentication policies). Never cloned with ALFA_EDW_*.';
CREATE SCHEMA IF NOT EXISTS IDENTIFIER($admin_schema_fqn)
    COMMENT = 'Network rule and authentication policy for the SVC_EDW_FLYWAY_* service users (bootstrap/10).';

GRANT USAGE ON DATABASE IDENTIFIER($admin_db) TO ROLE SECURITYADMIN;
GRANT USAGE, CREATE NETWORK RULE, CREATE AUTHENTICATION POLICY
    ON SCHEMA IDENTIFIER($admin_schema_fqn) TO ROLE SECURITYADMIN;

-- Auditor compute: read-only info/validate/promotion checks are metadata-light; a hard statement timeout keeps a
-- runaway PR build from burning credits.
CREATE WAREHOUSE IF NOT EXISTS WH_EDW_FLYWAY_AUDIT
    WITH WAREHOUSE_SIZE = 'XSMALL'
         AUTO_SUSPEND = 60
         AUTO_RESUME = TRUE
         INITIALLY_SUSPENDED = TRUE
         COMMENT = 'Read-only Flyway info / validate / promotion checks (EDW_FLYWAY_AUDITOR); bootstrap/10'
         STATEMENT_TIMEOUT_IN_SECONDS = 900;

-- --------------------------------------------------------------------------------------------------- 1b USERADMIN
-- TYPE = SERVICE: no password, no MFA prompts, no Snowsight; key pair (or OAuth / workload identity) only.
-- DEFAULT_SECONDARY_ROLES = (): the login runs with its one role. With secondary roles a deploy could create objects
-- that depend on grants other runways do not have, and the read-only login could read more than it should.
-- QUOTED_IDENTIFIERS_IGNORE_CASE = FALSE pinned on the user: Flyway's history table is the quoted lower-case
-- "flyway_schema_history"; an account-level flip of this parameter must not make it unreadable to Flyway.
USE ROLE USERADMIN;

CREATE ROLE IF NOT EXISTS EDW_FLYWAY_AUDITOR
    COMMENT = 'Read-only Flyway history: USAGE on each ALFA_EDW_* db + FLYWAY schema, SELECT on the history table';

CREATE USER IF NOT EXISTS SVC_EDW_FLYWAY_AUDIT
    TYPE = SERVICE
    DEFAULT_ROLE = EDW_FLYWAY_AUDITOR
    DEFAULT_WAREHOUSE = WH_EDW_FLYWAY_AUDIT
    DEFAULT_SECONDARY_ROLES = ()
    COMMENT = 'Jenkins read-only Flyway login (PR builds, promotion checks). Key pair only; bootstrap/10';

ALTER USER SVC_EDW_FLYWAY_AUDIT SET
    DEFAULT_ROLE = EDW_FLYWAY_AUDITOR
    DEFAULT_WAREHOUSE = WH_EDW_FLYWAY_AUDIT
    DEFAULT_SECONDARY_ROLES = ()
    QUOTED_IDENTIFIERS_IGNORE_CASE = FALSE;

-- ----------------------------------------------------------------------------------------------- 1c SECURITYADMIN
-- Network rule (MODE = INGRESS, TYPE = IPV4) + network policy: a SVC_EDW_FLYWAY_* login from anywhere but the
-- Jenkins agents is refused, even with a stolen key. The policy is attached to the service users only (PART 3),
-- never to the account, so no person can be locked out by a wrong CIDR.
-- Authentication policy: AUTHENTICATION_METHODS = ('KEYPAIR'); CLIENT_TYPES = ('DRIVERS', 'SNOWFLAKE_CLI') because
-- the pipeline logs in with two clients per service user: Flyway through the JDBC driver (DRIVERS) and the Snowflake
-- CLI `snow sql` (SNOWFLAKE_CLI) for the drift query and the rehearsal clone create / drop. Snowflake documents
-- CLIENT_TYPES as best effort, so it is defence in depth, never the sole control: the key pair, the network policy
-- and folder-scoped Jenkins credentials are the controls. This file is the source of truth for both lists: a re-run
-- re-applies them.
USE ROLE SECURITYADMIN;

EXECUTE IMMEDIATE $$
DECLARE
    cidrs VARCHAR;
    adb VARCHAR;
    asch VARCHAR;
    nr VARCHAR;
    np VARCHAR;
    ap VARCHAR;
    nr_fqn VARCHAR;
    ap_fqn VARCHAR;
    parts ARRAY;
    n INTEGER;
    c VARCHAR;
    vlist VARCHAR DEFAULT '';
    -- A declared exception's message must be one literal: the full rule is in the PART 0 comment.
    bad_cidr EXCEPTION (-20111, 'PART 1c: set jenkins_egress_cidrs to the Jenkins agents egress CIDRs (see PART 0).');
BEGIN
    SELECT $jenkins_egress_cidrs, $admin_db, $admin_schema, $network_rule_name, $network_policy_name,
           $auth_policy_name
      INTO :cidrs, :adb, :asch, :nr, :np, :ap;
    parts := SPLIT(REPLACE(COALESCE(cidrs, ''), ' ', ''), ',');
    n := ARRAY_SIZE(parts);
    FOR i IN 1 TO n DO
        c := GET(parts, i - 1)::VARCHAR;
        IF (NOT REGEXP_LIKE(c, '([0-9]{1,3}[.]){3}[0-9]{1,3}(/([89]|[12][0-9]|3[0-2]))?')
            OR STARTSWITH(c, '0.') OR STARTSWITH(c, '192.0.2.') OR STARTSWITH(c, '198.51.100.')
            OR STARTSWITH(c, '203.0.113.')) THEN
            RAISE bad_cidr;
        END IF;
        vlist := vlist || IFF(i > 1, ', ', '') || '''' || c || '''';
    END FOR;

    nr_fqn := adb || '.' || asch || '.' || nr;
    ap_fqn := adb || '.' || asch || '.' || ap;

    EXECUTE IMMEDIATE 'CREATE NETWORK RULE IF NOT EXISTS ' || :nr_fqn
        || ' TYPE = IPV4 MODE = INGRESS VALUE_LIST = (' || :vlist || ')'
        || ' COMMENT = ''Jenkins agent egress addresses allowed for SVC_EDW_FLYWAY_* logins (bootstrap/10)''';
    EXECUTE IMMEDIATE 'ALTER NETWORK RULE ' || :nr_fqn || ' SET VALUE_LIST = (' || :vlist || ')';

    EXECUTE IMMEDIATE 'CREATE NETWORK POLICY IF NOT EXISTS ' || :np
        || ' ALLOWED_NETWORK_RULE_LIST = (''' || :nr_fqn || ''')'
        || ' COMMENT = ''SVC_EDW_FLYWAY_* users only: Jenkins agents. Attach to users, never to the account.''';
    EXECUTE IMMEDIATE 'ALTER NETWORK POLICY ' || :np || ' SET ALLOWED_NETWORK_RULE_LIST = (''' || :nr_fqn || ''')';

    EXECUTE IMMEDIATE 'CREATE AUTHENTICATION POLICY IF NOT EXISTS ' || :ap_fqn
        || ' AUTHENTICATION_METHODS = (''KEYPAIR'') CLIENT_TYPES = (''DRIVERS'', ''SNOWFLAKE_CLI'')'
        || ' COMMENT = ''SVC_EDW_FLYWAY_* users: key pair only, Flyway (JDBC) and Snowflake CLI clients''';
    EXECUTE IMMEDIATE 'ALTER AUTHENTICATION POLICY ' || :ap_fqn
        || ' SET AUTHENTICATION_METHODS = (''KEYPAIR'') CLIENT_TYPES = (''DRIVERS'', ''SNOWFLAKE_CLI'')';

    RETURN 'PART 1c OK: network rule ' || nr_fqn || ' = (' || vlist || '); network policy ' || np
        || '; authentication policy ' || ap_fqn || ' (KEYPAIR; DRIVERS, SNOWFLAKE_CLI)';
END;
$$;

-- The auditor role rolls up to SYSADMIN like every custom role; its user and warehouse.
GRANT ROLE EDW_FLYWAY_AUDITOR TO ROLE SYSADMIN;
GRANT ROLE EDW_FLYWAY_AUDITOR TO USER SVC_EDW_FLYWAY_AUDIT;
GRANT USAGE ON WAREHOUSE WH_EDW_FLYWAY_AUDIT TO ROLE EDW_FLYWAY_AUDITOR;
-- Database-level auditor grants (USAGE on each ALFA_EDW_* + FLYWAY, SELECT on the history table) are per runway:
-- bootstrap/20_runway_database.sql.

-- ------------------------------------------------------------------------------------------------ 1d ACCOUNTADMIN
-- Setting an authentication policy on a user needs the ON ACCOUNT privilege APPLY AUTHENTICATION POLICY. Granting
-- it once to SECURITYADMIN keeps PART 3 (run after every new runway) out of ACCOUNTADMIN. Harmless if SECURITYADMIN
-- already holds it in your account (SHOW GRANTS TO ROLE SECURITYADMIN).
USE ROLE ACCOUNTADMIN;
GRANT APPLY AUTHENTICATION POLICY ON ACCOUNT TO ROLE SECURITYADMIN;


-- =====================================================================================================================
-- PART 2 -- one runway (set runway in PART 0 and run PART 0 first; repeat for DEV, SAN, SEA, PHX, MGM, PRD)
-- =====================================================================================================================

-- ---------------------------------------------------------------------------------------------------- 2a SYSADMIN
-- Deploy compute (XSMALL, 60 s auto-suspend): Flyway DDL is metadata work. The ETL warehouse is what the runway's
-- tasks run on (flyway.toml placeholder etl_wh); IF NOT EXISTS leaves an existing one exactly as it is.
USE ROLE SYSADMIN;

CREATE WAREHOUSE IF NOT EXISTS IDENTIFIER($deploy_wh)
    WITH WAREHOUSE_SIZE = 'XSMALL'
         AUTO_SUSPEND = 60
         AUTO_RESUME = TRUE
         INITIALLY_SUSPENDED = TRUE
         COMMENT = 'Flyway deploy compute for one ALFA EDW runway (its EDW_<ID>_DEPLOYER only); bootstrap/10';

CREATE WAREHOUSE IF NOT EXISTS IDENTIFIER($etl_wh)
    WITH WAREHOUSE_SIZE = 'XSMALL'
         AUTO_SUSPEND = 60
         AUTO_RESUME = TRUE
         INITIALLY_SUSPENDED = TRUE
         COMMENT = 'Task / ETL compute for one ALFA EDW runway (flyway.toml placeholder etl_wh); bootstrap/10';

-- --------------------------------------------------------------------------------------------------- 2b USERADMIN
-- The deploy role owns every schema and object in ALFA_EDW_<ID> (bootstrap/20), never the database itself: a deploy
-- can then change anything inside its runway but cannot drop, replace, rename or re-grant the database.
USE ROLE USERADMIN;

CREATE ROLE IF NOT EXISTS IDENTIFIER($deploy_role)
    COMMENT = 'Flyway deploy role: owns every schema and object in its ALFA_EDW_<ID> database, not the database';

CREATE USER IF NOT EXISTS IDENTIFIER($svc_user)
    TYPE = SERVICE
    DEFAULT_ROLE = $deploy_role
    DEFAULT_WAREHOUSE = $deploy_wh
    DEFAULT_SECONDARY_ROLES = ()
    COMMENT = 'Jenkins Flyway deploy login for one ALFA EDW runway. Key pair only; bootstrap/10';

-- Re-asserted on every run (CREATE ... IF NOT EXISTS does not touch an existing user); reasons in PART 1b.
ALTER USER IDENTIFIER($svc_user) SET
    DEFAULT_ROLE = $deploy_role
    DEFAULT_WAREHOUSE = $deploy_wh
    DEFAULT_SECONDARY_ROLES = ()
    QUOTED_IDENTIFIERS_IGNORE_CASE = FALSE;

-- ----------------------------------------------------------------------------------------------- 2c ACCOUNTADMIN
-- EXECUTE TASK (always): a task runs, and SYSTEM$TASK_DEPENDENTS_ENABLE / ALTER TASK ... RESUME succeed, only if the
-- role that OWNS the task holds the account-level EXECUTE TASK privilege. The deploy role owns the runway's tasks, and
-- both the task repeatables (layer 80) and afterMigrate__60_task_restore resume graphs as that role.
USE ROLE ACCOUNTADMIN;
GRANT EXECUTE TASK ON ACCOUNT TO ROLE IDENTIFIER($deploy_role);
-- Only if the runway has serverless tasks (no WAREHOUSE clause; bootstrap/20 lists them during adoption):
-- GRANT EXECUTE MANAGED TASK ON ACCOUNT TO ROLE IDENTIFIER($deploy_role);
-- Only if the runway has alerts (layer 85): resuming an alert needs EXECUTE ALERT on its owner role:
-- GRANT EXECUTE ALERT ON ACCOUNT TO ROLE IDENTIFIER($deploy_role);

-- CREATE DATABASE for the MGM and PRD deploy roles only. The deploy job rehearses every MGM/PRD release on a
-- zero-copy clone ALFA_EDW_RH_<ID> (`fw.py rehearsal-sql`), and the clone must be created BY the deploy role: a clone
-- contains only the tasks, pipes, streams and alerts the cloning role owns, so any other role would rehearse on an
-- incomplete copy. PRD's role is also the one bootstrap/30 clones test runways with, for the same reason.
-- Trade-off: CREATE DATABASE is account-wide. The role can create any database, not only ALFA_EDW_RH_<ID>, and owns
-- what it creates, so a stolen PRD key could copy production data into a new database and grant it out. Accepted
-- because (a) that key works only from the Jenkins agents (network policy) and only in the EDW/deploy/PRD folder,
-- (b) fw.py refuses any rehearsal name other than ALFA_EDW_RH_<ID>, (c) PART 5 and bootstrap/90 list every database
-- these roles own, and (d) the alternative -- an owner's-rights clone procedure owned by another role -- would own
-- the clone's tasks and pipes itself and rehearse a different ownership model than production. DEV/SAN/SEA/PHX never
-- rehearse and do not get it.
EXECUTE IMMEDIATE $$
DECLARE
    rw VARCHAR;
    r VARCHAR;
BEGIN
    SELECT $runway, $deploy_role INTO :rw, :r;
    IF (rw IN ('MGM', 'PRD')) THEN
        EXECUTE IMMEDIATE 'GRANT CREATE DATABASE ON ACCOUNT TO ROLE ' || :r;
        RETURN 'PART 2c: EXECUTE TASK and CREATE DATABASE (rehearsal clones) granted to ' || r;
    END IF;
    RETURN 'PART 2c: EXECUTE TASK granted to ' || r || '; no CREATE DATABASE (only MGM / PRD rehearse)';
END;
$$;

-- ----------------------------------------------------------------------------------------------- 2d SECURITYADMIN
-- Role hierarchy: deploy role under SYSADMIN (custom roles must roll up, or nobody can administer what they own).
-- The service user gets the deploy role and nothing else; people never get MGM/PRD deploy roles (GUIDE B7).
-- Database grants (USAGE, CREATE SCHEMA on ALFA_EDW_<ID>) are bootstrap/20, which also creates the FLYWAY schema.
USE ROLE SECURITYADMIN;

GRANT ROLE IDENTIFIER($deploy_role) TO ROLE SYSADMIN;
GRANT ROLE IDENTIFIER($deploy_role) TO USER IDENTIFIER($svc_user);
GRANT USAGE ON WAREHOUSE IDENTIFIER($deploy_wh) TO ROLE IDENTIFIER($deploy_role);
-- Tasks run with their owner's privileges: the owner (the deploy role) needs USAGE on the warehouse they name.
GRANT USAGE ON WAREHOUSE IDENTIFIER($etl_wh) TO ROLE IDENTIFIER($deploy_role);


-- =====================================================================================================================
-- PART 3 -- network policy + authentication policy on every SVC_EDW_FLYWAY_* user (SECURITYADMIN)
--           Run after PART 1 and after every PART 2. Idempotent. A user that already carries a DIFFERENT
--           authentication policy is reported and left alone: replacing someone else's policy is a decision.
-- =====================================================================================================================
USE ROLE SECURITYADMIN;

EXECUTE IMMEDIATE $$
DECLARE
    adb VARCHAR;
    asch VARCHAR;
    np VARCHAR;
    ap VARCHAR;
    ap_fqn VARCHAR;
    user_list ARRAY;
    n INTEGER;
    u VARCHAR;
    same_cnt INTEGER;
    any_cnt INTEGER;
    current_ap VARCHAR;
    report VARCHAR DEFAULT '';
BEGIN
    SELECT $admin_db, $admin_schema, $network_policy_name, $auth_policy_name INTO :adb, :asch, :np, :ap;
    ap_fqn := adb || '.' || asch || '.' || ap;

    SHOW USERS LIKE 'SVC_EDW_FLYWAY_%';
    SELECT COALESCE(ARRAY_AGG(t."name"::VARCHAR) WITHIN GROUP (ORDER BY t."name"), ARRAY_CONSTRUCT())
      INTO :user_list
      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
     WHERE STARTSWITH(t."name", 'SVC_EDW_FLYWAY_');
    n := ARRAY_SIZE(user_list);
    IF (n = 0) THEN
        RETURN 'PART 3: no SVC_EDW_FLYWAY_* users yet; run PART 1 / PART 2 first';
    END IF;

    FOR i IN 1 TO n DO
        u := GET(user_list, i - 1)::VARCHAR;
        BEGIN
            EXECUTE IMMEDIATE 'ALTER USER ' || :u || ' SET NETWORK_POLICY = ' || :np;
            report := report || 'ok      ' || u || ': network policy ' || np || CHR(10);
        EXCEPTION
            WHEN OTHER THEN
                report := report || 'FAILED  ' || u || ': network policy: ' || SQLERRM || CHR(10);
        END;
        BEGIN
            EXECUTE IMMEDIATE 'SELECT COUNT_IF(POLICY_DB = ''' || :adb || ''' AND POLICY_SCHEMA = ''' || :asch
                || ''' AND POLICY_NAME = ''' || :ap || '''), COUNT(*),'
                || ' MAX(POLICY_DB || ''.'' || POLICY_SCHEMA || ''.'' || POLICY_NAME)'
                || ' FROM TABLE(' || :adb || '.INFORMATION_SCHEMA.POLICY_REFERENCES('
                || 'REF_ENTITY_NAME => ''' || :u || ''', REF_ENTITY_DOMAIN => ''USER''))'
                || ' WHERE POLICY_KIND LIKE ''AUTHENTICATION%''';
            SELECT $1, $2, $3 INTO :same_cnt, :any_cnt, :current_ap FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
            IF (same_cnt > 0) THEN
                report := report || 'ok      ' || u || ': authentication policy ' || ap_fqn || ' (already set)'
                    || CHR(10);
            ELSEIF (any_cnt > 0) THEN
                report := report || 'CHECK   ' || u || ': already has authentication policy ' || current_ap
                    || '; NOT replaced. Unset it deliberately if ' || ap_fqn || ' should apply.' || CHR(10);
            ELSE
                EXECUTE IMMEDIATE 'ALTER USER ' || :u || ' SET AUTHENTICATION POLICY ' || :ap_fqn;
                report := report || 'ok      ' || u || ': authentication policy ' || ap_fqn || ' (set now)' || CHR(10);
            END IF;
        EXCEPTION
            WHEN OTHER THEN
                report := report || 'FAILED  ' || u || ': authentication policy: ' || SQLERRM || CHR(10);
        END;
    END FOR;
    RETURN report;
END;
$$;


-- =====================================================================================================================
-- PART 4 -- register the public keys (USERADMIN owns the users)
--           Paste the ONE-LINE public key body (no BEGIN/END lines) from the KEY PAIR commands in the header.
--           Snowflake rejects anything that is not a valid public key, so an unpasted line fails loudly here.
-- =====================================================================================================================
USE ROLE USERADMIN;

-- 4a  the auditor, once:
ALTER USER SVC_EDW_FLYWAY_AUDIT SET RSA_PUBLIC_KEY = '<paste the public key body here>';

-- 4b  this runway's deploy user (runway set in PART 0). Paste THIS runway's key: a key set on two users would let
--     one Jenkins folder's credential deploy to another runway (PART 5 fails on a shared fingerprint).
ALTER USER IDENTIFIER($svc_user) SET RSA_PUBLIC_KEY = '<paste the public key body here>';

-- Rotation (quarterly, zero downtime; docs/RUNBOOKS.md R11). Snowflake holds two key slots per user:
--   1. new key pair as in the header; register it in the free slot:
--        ALTER USER IDENTIFIER($svc_user) SET RSA_PUBLIC_KEY_2 = '<paste the new public key body here>';
--   2. Jenkins admin replaces the .p8 + passphrase in credential sf-flyway-<ID>; prove it with a DRY_RUN deploy;
--   3. retire the old slot:
--        ALTER USER IDENTIFIER($svc_user) UNSET RSA_PUBLIC_KEY;
--      The next rotation uses slot 1 again (SET RSA_PUBLIC_KEY, then UNSET RSA_PUBLIC_KEY_2).
--   DESC USER shows RSA_PUBLIC_KEY_FP and RSA_PUBLIC_KEY_2_FP; compare them with the openssl fingerprint.


-- =====================================================================================================================
-- PART 5 -- verification for the runway in PART 0 (+ the auditor and all service users) -- SECURITYADMIN
--           Read-only. Every line starts with PASS / FAIL / CHECK / INFO. The SELECT after the block shows the
--           report one line per row.
-- =====================================================================================================================
USE ROLE SECURITYADMIN;

EXECUTE IMMEDIATE $$
DECLARE
    rw VARCHAR;
    u VARCHAR;
    r VARCHAR;
    wh VARCHAR;
    ewh VARCHAR;
    np VARCHAR;
    adb VARCHAR;
    asch VARCHAR;
    ap VARCHAR;
    v VARCHAR;
    c1 INTEGER;
    c2 INTEGER;
    c3 INTEGER;
    c4 INTEGER;
    user_list ARRAY;
    n INTEGER;
    uu VARCHAR;
    fp1 VARCHAR;
    fp2 VARCHAR;
    seen VARCHAR DEFAULT '|';
    report VARCHAR DEFAULT '';
BEGIN
    SELECT $runway, $svc_user, $deploy_role, $deploy_wh, $etl_wh, $network_policy_name, $admin_db, $admin_schema,
           $auth_policy_name
      INTO :rw, :u, :r, :wh, :ewh, :np, :adb, :asch, :ap;
    report := 'INFO   runway ' || rw || ': user ' || u || ', role ' || r || ', warehouse ' || wh || CHR(10);

    -- (1) the service user's properties
    BEGIN
        EXECUTE IMMEDIATE 'DESC USER ' || :u;
        SELECT LISTAGG(CASE t."property"
                 WHEN 'TYPE' THEN IFF(t."value" = 'SERVICE', 'PASS', 'FAIL') || '   TYPE = ' || t."value"
                 WHEN 'DISABLED' THEN IFF(t."value" = 'false', 'PASS', 'FAIL') || '   DISABLED = ' || t."value"
                 WHEN 'DEFAULT_ROLE' THEN IFF(t."value" = :r, 'PASS', 'FAIL') || '   DEFAULT_ROLE = ' || t."value"
                 WHEN 'DEFAULT_WAREHOUSE' THEN IFF(t."value" = :wh, 'PASS', 'FAIL')
                      || '   DEFAULT_WAREHOUSE = ' || t."value"
                 WHEN 'DEFAULT_SECONDARY_ROLES' THEN IFF(COALESCE(t."value", '') IN ('[]', '', 'null'), 'PASS', 'CHECK')
                      || '   DEFAULT_SECONDARY_ROLES = ' || COALESCE(t."value", '') || ' (want empty)'
                 WHEN 'RSA_PUBLIC_KEY_FP' THEN IFF(STARTSWITH(COALESCE(t."value", ''), 'SHA256:'), 'PASS', 'FAIL')
                      || '   RSA_PUBLIC_KEY_FP = ' || COALESCE(t."value", '(none: PART 4b)')
                      || '  -- must equal the openssl fingerprint of THIS runway''s key'
                 END, CHR(10)) WITHIN GROUP (ORDER BY t."property")
          INTO :v
          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
         WHERE t."property" IN ('TYPE', 'DISABLED', 'DEFAULT_ROLE', 'DEFAULT_WAREHOUSE', 'DEFAULT_SECONDARY_ROLES',
                                'RSA_PUBLIC_KEY_FP');
        report := report || COALESCE(v, 'FAIL   DESC USER returned no properties') || CHR(10);
    EXCEPTION
        WHEN OTHER THEN
            report := report || 'FAIL   DESC USER ' || u || ': ' || SQLERRM || CHR(10);
    END;

    -- (2) network policy (a user parameter)
    BEGIN
        EXECUTE IMMEDIATE 'SHOW PARAMETERS LIKE ''NETWORK_POLICY'' IN USER ' || :u;
        SELECT MAX(IFF(t."value" = :np, 'PASS', 'FAIL') || '   NETWORK_POLICY = '
                   || COALESCE(NULLIF(t."value", ''), '(none: PART 3)'))
          INTO :v
          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
        report := report || COALESCE(v, 'FAIL   NETWORK_POLICY not set (PART 3)') || CHR(10);
    EXCEPTION
        WHEN OTHER THEN
            report := report || 'FAIL   network policy check: ' || SQLERRM || CHR(10);
    END;

    -- (3) authentication policy
    BEGIN
        EXECUTE IMMEDIATE 'SELECT COUNT_IF(POLICY_DB = ''' || :adb || ''' AND POLICY_SCHEMA = ''' || :asch
            || ''' AND POLICY_NAME = ''' || :ap || '''), COUNT(*)'
            || ' FROM TABLE(' || :adb || '.INFORMATION_SCHEMA.POLICY_REFERENCES('
            || 'REF_ENTITY_NAME => ''' || :u || ''', REF_ENTITY_DOMAIN => ''USER''))'
            || ' WHERE POLICY_KIND LIKE ''AUTHENTICATION%''';
        SELECT $1, $2 INTO :c1, :c2 FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
        report := report || IFF(c1 > 0, 'PASS', 'FAIL') || '   authentication policy ' || adb || '.' || asch || '.'
            || ap
            || IFF(c1 > 0, ' attached', IFF(c2 > 0, ' NOT attached (another policy is: PART 3 output)',
                                           ' NOT attached (PART 3)')) || CHR(10);
    EXCEPTION
        WHEN OTHER THEN
            report := report || 'FAIL   authentication policy check: ' || SQLERRM || CHR(10);
    END;

    -- (4) who holds the deploy role: SYSADMIN + the service user. People only on DEV / test runways (GUIDE B7).
    BEGIN
        EXECUTE IMMEDIATE 'SHOW GRANTS OF ROLE ' || :r;
        SELECT COUNT_IF(t."granted_to" = 'ROLE' AND t."grantee_name" = 'SYSADMIN'),
               COUNT_IF(t."granted_to" = 'USER' AND t."grantee_name" = :u),
               COUNT_IF(NOT ((t."granted_to" = 'ROLE' AND t."grantee_name" = 'SYSADMIN')
                             OR (t."granted_to" = 'USER' AND t."grantee_name" = :u))),
               LISTAGG(IFF((t."granted_to" = 'ROLE' AND t."grantee_name" = 'SYSADMIN')
                           OR (t."granted_to" = 'USER' AND t."grantee_name" = :u), NULL,
                           t."granted_to" || ' ' || t."grantee_name"), ', ')
          INTO :c1, :c2, :c3, :v
          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
        report := report || IFF(c1 > 0, 'PASS', 'FAIL') || '   ' || r || ' granted to ROLE SYSADMIN' || CHR(10)
            || IFF(c2 > 0, 'PASS', 'FAIL') || '   ' || r || ' granted to USER ' || u || CHR(10);
        IF (c3 > 0) THEN
            report := report || IFF(rw IN ('MGM', 'PRD'), 'FAIL', 'INFO') || '   ' || r || ' also granted to: ' || v
                || IFF(rw IN ('MGM', 'PRD'), ' (nobody may hold an MGM/PRD deploy role outside break-glass)', '')
                || CHR(10);
        END IF;
    EXCEPTION
        WHEN OTHER THEN
            report := report || 'FAIL   SHOW GRANTS OF ROLE ' || r || ': ' || SQLERRM || CHR(10);
    END;

    -- (5) what the deploy role holds at account / warehouse level
    BEGIN
        EXECUTE IMMEDIATE 'SHOW GRANTS TO ROLE ' || :r;
        SELECT COUNT_IF(t."privilege" = 'USAGE' AND t."granted_on" = 'WAREHOUSE' AND t."name" = :wh),
               COUNT_IF(t."privilege" = 'USAGE' AND t."granted_on" = 'WAREHOUSE' AND t."name" = :ewh),
               COUNT_IF(t."privilege" = 'EXECUTE TASK' AND t."granted_on" = 'ACCOUNT'),
               COUNT_IF(t."privilege" = 'CREATE DATABASE' AND t."granted_on" = 'ACCOUNT')
          INTO :c1, :c2, :c3, :c4
          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
        report := report || IFF(c1 > 0, 'PASS', 'FAIL') || '   USAGE on ' || wh || CHR(10)
            || IFF(c2 > 0, 'PASS', 'FAIL') || '   USAGE on ' || ewh || ' (task warehouse)' || CHR(10)
            || IFF(c3 > 0, 'PASS', 'FAIL') || '   EXECUTE TASK on account' || CHR(10)
            || IFF((c4 > 0) = (rw IN ('MGM', 'PRD')), 'PASS', 'FAIL') || '   CREATE DATABASE on account: '
            || IFF(c4 > 0, 'yes', 'no') || ' (want ' || IFF(rw IN ('MGM', 'PRD'), 'yes', 'no') || ')' || CHR(10);
    EXCEPTION
        WHEN OTHER THEN
            report := report || 'FAIL   SHOW GRANTS TO ROLE ' || r || ': ' || SQLERRM || CHR(10);
    END;

    -- (6) every SVC_EDW_FLYWAY_* user: SERVICE type, a key, and no key fingerprint shared between two users
    BEGIN
        SHOW USERS LIKE 'SVC_EDW_FLYWAY_%';
        SELECT COALESCE(ARRAY_AGG(t."name"::VARCHAR) WITHIN GROUP (ORDER BY t."name"), ARRAY_CONSTRUCT())
          INTO :user_list
          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t
         WHERE STARTSWITH(t."name", 'SVC_EDW_FLYWAY_');
        n := ARRAY_SIZE(user_list);
        IF (n = 0) THEN
            report := report || 'FAIL   no SVC_EDW_FLYWAY_* users found (PART 1 / PART 2)' || CHR(10);
        ELSE
            FOR i IN 1 TO n DO
                uu := GET(user_list, i - 1)::VARCHAR;
                EXECUTE IMMEDIATE 'DESC USER ' || :uu;
                SELECT MAX(IFF(t."property" = 'RSA_PUBLIC_KEY_FP', NULLIF(NULLIF(t."value", 'null'), ''), NULL)),
                       MAX(IFF(t."property" = 'RSA_PUBLIC_KEY_2_FP', NULLIF(NULLIF(t."value", 'null'), ''), NULL)),
                       MAX(IFF(t."property" = 'TYPE', t."value", NULL))
                  INTO :fp1, :fp2, :v
                  FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
                report := report || IFF(v = 'SERVICE' AND (fp1 IS NOT NULL OR fp2 IS NOT NULL), 'PASS', 'FAIL') || '   '
                    || uu || ': TYPE = ' || COALESCE(v, '?') || ', key '
                    || IFF(fp1 IS NOT NULL OR fp2 IS NOT NULL, 'registered', 'MISSING (PART 4)') || CHR(10);
                IF (fp1 IS NOT NULL AND CONTAINS(seen, '|' || fp1 || '|')) THEN
                    report := report || 'FAIL   ' || uu || ' shares key ' || fp1 || ' with another service user'
                        || CHR(10);
                END IF;
                IF (fp2 IS NOT NULL AND CONTAINS(seen, '|' || fp2 || '|')) THEN
                    report := report || 'FAIL   ' || uu || ' shares key ' || fp2 || ' with another service user'
                        || CHR(10);
                END IF;
                seen := seen || COALESCE(fp1, '-') || '|' || COALESCE(fp2, '-') || '|';
            END FOR;
        END IF;
    EXCEPTION
        WHEN OTHER THEN
            report := report || 'FAIL   service-user scan: ' || SQLERRM || CHR(10);
    END;

    -- (7) the auditor role's warehouse
    BEGIN
        SHOW GRANTS TO ROLE EDW_FLYWAY_AUDITOR;
        SELECT COUNT_IF(t."privilege" = 'USAGE' AND t."granted_on" = 'WAREHOUSE' AND t."name" = 'WH_EDW_FLYWAY_AUDIT')
          INTO :c1
          FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) AS t;
        report := report || IFF(c1 > 0, 'PASS', 'FAIL') || '   EDW_FLYWAY_AUDITOR: USAGE on WH_EDW_FLYWAY_AUDIT'
            || CHR(10);
    EXCEPTION
        WHEN OTHER THEN
            report := report || 'FAIL   SHOW GRANTS TO ROLE EDW_FLYWAY_AUDITOR: ' || SQLERRM || CHR(10);
    END;

    RETURN report;
END;
$$;

-- The report above, one line per row (reads the previous statement's result; run it right after the block).
SELECT s.INDEX AS LINE_NO, s.VALUE AS CHECK_RESULT
  FROM (SELECT $1 AS REPORT FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))) AS b,
       TABLE(SPLIT_TO_TABLE(b.REPORT, CHR(10))) AS s
 WHERE s.VALUE <> ''
 ORDER BY s.INDEX;

-- Next: bootstrap/20_runway_database.sql for the same runway (database grants, FLYWAY schema, adoption).
