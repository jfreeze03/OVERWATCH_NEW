-- V001__core.sql — database, schemas, settings, company scope, versioning.
-- Idempotent. Run as a deployment role (see DEPLOYMENT.md).

-- Owner decision 2026-07: objects live in the existing DBA_MAINT_DB.OVERWATCH
-- schema, shared with the previous OVERWATCH app. Everything here is
-- CREATE IF NOT EXISTS / MERGE — nothing existing is dropped or replaced
-- except OVERWATCH-owned functions/procedures listed in these migrations.
CREATE DATABASE IF NOT EXISTS DBA_MAINT_DB;
CREATE SCHEMA IF NOT EXISTS DBA_MAINT_DB.OVERWATCH;

-- ---------------------------------------------------------------------------
-- Migration bookkeeping
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (
    VERSION      NUMBER        NOT NULL,
    DESCRIPTION  VARCHAR(200)  NOT NULL,
    APPLIED_AT   TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    APPLIED_BY   VARCHAR(200)  NOT NULL DEFAULT CURRENT_USER()
);

-- ---------------------------------------------------------------------------
-- Settings — the authoritative rates/budget/contract store.
-- The app reads these; code constants are offline fallbacks only.
-- Confirmed 2026-07: $3.68 compute credit, $2.20 Cortex credit.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.SETTINGS (
    KEY        VARCHAR(80)   NOT NULL PRIMARY KEY,
    VALUE      VARCHAR(400),
    UPDATED_AT TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    UPDATED_BY VARCHAR(200)  NOT NULL DEFAULT CURRENT_USER()
);

MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS t
USING (
    SELECT * FROM VALUES
        ('CREDIT_PRICE_USD',        '3.68'),
        ('AI_CREDIT_PRICE_USD',     '2.20'),
        ('STORAGE_USD_PER_TB_MONTH','23.00'),
        ('MONTHLY_BUDGET_USD',      '0'),      -- 0 = not configured; set in Admin
        ('AI_MONTHLY_BUDGET_USD',   '0'),      -- 0 = not configured; Cortex user severities
        ('CONTRACT_CREDITS',        '0'),      -- 0 = not configured
        ('CONTRACT_START_DATE',     ''),
        ('CONTRACT_END_DATE',       '')
    AS s(KEY, VALUE)
) s
ON t.KEY = s.KEY
WHEN NOT MATCHED THEN INSERT (KEY, VALUE) VALUES (s.KEY, s.VALUE);

-- ---------------------------------------------------------------------------
-- Company scope seed — MUST mirror app/companies.py.
-- tests/test_companies.py::test_company_scope_seed_matches_code enforces it.
-- KEBARR1 holds both companies' roles; policy: classified as ALFA.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.COMPANY_SCOPE (
    COMPANY    VARCHAR(40)  NOT NULL,
    SCOPE_TYPE VARCHAR(40)  NOT NULL,   -- WAREHOUSE | DATABASE | USER_PREFIX | USER_OVERRIDE
    PATTERN    VARCHAR(200) NOT NULL,
    NOTE       VARCHAR(400)
);

MERGE INTO DBA_MAINT_DB.OVERWATCH.COMPANY_SCOPE t
USING (
    SELECT * FROM VALUES
        ('Trexis', 'WAREHOUSE',     'WH_TRXS_LOAD',          'Trexis load compute'),
        ('Trexis', 'WAREHOUSE',     'WH_TRXS_QUERY',         'Trexis query compute'),
        ('Trexis', 'WAREHOUSE',     'WH_TRXS_TRANSFORM',     'Trexis transform compute'),
        ('Trexis', 'WAREHOUSE',     'WH_TRXS_UNLOAD',        'Trexis unload compute'),
        ('Trexis', 'DATABASE',      'TRXS_ABC_METADATA_DEV', 'Trexis metadata DEV'),
        ('Trexis', 'DATABASE',      'TRXS_ABC_METADATA_PRD', 'Trexis metadata PRD'),
        ('Trexis', 'DATABASE',      'TRXS_ABC_METADATA_SIT', 'Trexis metadata SIT'),
        ('Trexis', 'DATABASE',      'TRXS_EDW_DEV',          'Trexis EDW DEV'),
        ('Trexis', 'DATABASE',      'TRXS_EDW_PRD',          'Trexis EDW PRD'),
        ('Trexis', 'DATABASE',      'TRXS_EDW_SIT',          'Trexis EDW SIT'),
        ('Trexis', 'DATABASE',      'TRXS_GW_DATA_DEV',      'Trexis GW data DEV'),
        ('Trexis', 'DATABASE',      'TRXS_GW_DATA_PRD',      'Trexis GW data PRD'),
        ('Trexis', 'DATABASE',      'TRXS_GW_DATA_SIT',      'Trexis GW data SIT'),
        ('Trexis', 'USER_PREFIX',   'TRXS_',                 'Trexis service/user prefix'),
        ('ALFA',   'USER_OVERRIDE', 'KEBARR1',               'Holds both companies'' roles; treated as ALFA by policy')
    AS s(COMPANY, SCOPE_TYPE, PATTERN, NOTE)
) s
ON t.COMPANY = s.COMPANY AND t.SCOPE_TYPE = s.SCOPE_TYPE AND t.PATTERN = s.PATTERN
WHEN MATCHED THEN UPDATE SET NOTE = s.NOTE
WHEN NOT MATCHED THEN INSERT (COMPANY, SCOPE_TYPE, PATTERN, NOTE)
                      VALUES (s.COMPANY, s.SCOPE_TYPE, s.PATTERN, s.NOTE);

-- ---------------------------------------------------------------------------
-- Company classification helpers (single-sourced from COMPANY_SCOPE)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(WH VARCHAR)
RETURNS VARCHAR
AS
$$
    COALESCE(
        (SELECT MAX(COMPANY) FROM DBA_MAINT_DB.OVERWATCH.COMPANY_SCOPE
          WHERE SCOPE_TYPE = 'WAREHOUSE' AND PATTERN = UPPER(COALESCE(WH, ''))),
        'ALFA'
    )
$$;

CREATE OR REPLACE FUNCTION DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(U VARCHAR)
RETURNS VARCHAR
AS
$$
    COALESCE(
        (SELECT MAX(COMPANY) FROM DBA_MAINT_DB.OVERWATCH.COMPANY_SCOPE
          WHERE SCOPE_TYPE = 'USER_OVERRIDE' AND PATTERN = UPPER(COALESCE(U, ''))),
        IFF(UPPER(COALESCE(U, '')) LIKE 'TRXS!_%' ESCAPE '!', 'Trexis', 'ALFA')
    )
$$;

CREATE OR REPLACE FUNCTION DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DB VARCHAR)
RETURNS VARCHAR
AS
$$
    IFF(UPPER(COALESCE(DB, '')) LIKE 'TRXS!_%' ESCAPE '!', 'Trexis', 'ALFA')
$$;

-- ---------------------------------------------------------------------------
-- App error sink (populated best-effort by the app's error boundary)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (
    LOGGED_AT     TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    PAGE          VARCHAR(80),
    ERROR_TYPE    VARCHAR(200),
    ERROR_MESSAGE VARCHAR(2000),
    CONTEXT       VARCHAR(2000),
    ROLE_NAME     VARCHAR(200)
);

MERGE INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION t
USING (SELECT 1 AS VERSION, 'core: db, schemas, settings, company scope, error log' AS DESCRIPTION) s
ON t.VERSION = s.VERSION
WHEN NOT MATCHED THEN INSERT (VERSION, DESCRIPTION) VALUES (s.VERSION, s.DESCRIPTION);

SELECT 'V001 applied' AS STATUS;
