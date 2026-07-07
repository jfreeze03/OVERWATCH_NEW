-- V019__scoping_fixes.sql — role-based user company scoping, WH_TRXS_LINEAGE,
-- and disable the credential-expiry rule on accounts without EXPIRES_AT.
--
-- The account's Trexis users are NOT prefixed TRXS_ — they hold roles that
-- carry _TRXS_ (e.g. SNOW_PRI_GFR_PRD_TRXS_DATA_TEAM). COMPANY_FOR_USER now
-- classifies by ROLE membership (KEBARR1 override preserved), so every
-- user-grained view (Cortex users, logins, grants, chargeback role lens)
-- separates Trexis from ALFA correctly. Idempotent.

EXECUTE IMMEDIATE $$
DECLARE
    v INT;
    not_ready EXCEPTION (-20019, 'BLOCKED: SCHEMA_VERSION < 18 - run migrations in order (see DEPLOYMENT.md)');
BEGIN
    SELECT COALESCE(MAX(VERSION), 0) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 18) THEN
        RAISE not_ready;
    END IF;
    RETURN 'ok';
END;
$$;

-- 1) Seed the new Trexis warehouse (validate.sql now expects 5).
MERGE INTO DBA_MAINT_DB.OVERWATCH.COMPANY_SCOPE t
USING (SELECT 'Trexis' AS COMPANY, 'WAREHOUSE' AS SCOPE_TYPE,
              'WH_TRXS_LINEAGE' AS PATTERN, 'Trexis lineage compute' AS NOTE) s
ON t.COMPANY = s.COMPANY AND t.SCOPE_TYPE = s.SCOPE_TYPE AND t.PATTERN = s.PATTERN
WHEN NOT MATCHED THEN INSERT (COMPANY, SCOPE_TYPE, PATTERN, NOTE)
                      VALUES (s.COMPANY, s.SCOPE_TYPE, s.PATTERN, s.NOTE);

-- 2) COMPANY_FOR_USER by role membership. Override table wins first; then a
--    user holding any _TRXS_ role is Trexis; otherwise ALFA. EXISTS keeps it
--    a scalar the optimizer can push down.
CREATE OR REPLACE FUNCTION DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(U VARCHAR)
RETURNS VARCHAR
AS
$$
    COALESCE(
        (SELECT MAX(COMPANY) FROM DBA_MAINT_DB.OVERWATCH.COMPANY_SCOPE
          WHERE SCOPE_TYPE = 'USER_OVERRIDE' AND PATTERN = UPPER(COALESCE(U, ''))),
        IFF(EXISTS (
                SELECT 1 FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS g
                WHERE g.DELETED_ON IS NULL
                  AND UPPER(g.GRANTEE_NAME) = UPPER(COALESCE(U, ''))
                  AND g.ROLE ILIKE '%TRXS%'),
            'Trexis', 'ALFA')
    )
$$;

-- 3) This account's ACCOUNT_USAGE.CREDENTIALS view does not expose EXPIRES_AT,
--    so SEC_CRED_EXPIRY can never evaluate — disable it to stop the hourly
--    OPS_SCAN_DEGRADED noise. Newer accounts: set ENABLED = TRUE to re-arm.
UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
   SET ENABLED = FALSE
 WHERE RULE_ID = 'SEC_CRED_EXPIRY';

-- 4) Department-map the new warehouse so chargeback doesn't bucket it Unmapped.
MERGE INTO DBA_MAINT_DB.OVERWATCH.DEPARTMENT_MAP t
USING (SELECT 'WAREHOUSE' AS MAP_TYPE, 'WH_TRXS_LINEAGE' AS NAME,
              'Trexis Lineage' AS DEPARTMENT, 'Trexis' AS OWNER) s
ON t.MAP_TYPE = s.MAP_TYPE AND t.NAME = s.NAME
WHEN NOT MATCHED THEN INSERT (MAP_TYPE, NAME, DEPARTMENT, OWNER)
                      VALUES (s.MAP_TYPE, s.NAME, s.DEPARTMENT, s.OWNER);

MERGE INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION t
USING (SELECT 19 AS VERSION,
       'scoping: role-based COMPANY_FOR_USER, WH_TRXS_LINEAGE, disable SEC_CRED_EXPIRY (no EXPIRES_AT)' AS DESCRIPTION) s
ON t.VERSION = s.VERSION
WHEN NOT MATCHED THEN INSERT (VERSION, DESCRIPTION) VALUES (s.VERSION, s.DESCRIPTION);
