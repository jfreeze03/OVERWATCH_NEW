-- V008__chargeback.sql — department chargeback by warehouse (exact) and role
-- (allocated). No object tags required: every department owns its warehouses,
-- so DEPARTMENT_MAP is the billing truth. Idempotent.

CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.DEPARTMENT_MAP (
    MAP_TYPE   VARCHAR(20)  NOT NULL,   -- WAREHOUSE | ROLE
    NAME       VARCHAR(200) NOT NULL,   -- exact object name (uppercased on match)
    DEPARTMENT VARCHAR(120) NOT NULL,
    OWNER      VARCHAR(200) NOT NULL DEFAULT 'DBA',
    UPDATED_AT TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    UPDATED_BY VARCHAR(200)  NOT NULL DEFAULT CURRENT_USER(),
    CONSTRAINT PK_DEPARTMENT_MAP PRIMARY KEY (MAP_TYPE, NAME)
);

-- Seed from the known warehouse estate. Adjust departments in the app
-- (Cost & Contract > Chargeback > Manage mapping) — these are starting names.
MERGE INTO DBA_MAINT_DB.OVERWATCH.DEPARTMENT_MAP t
USING (
    SELECT * FROM VALUES
        -- ALFA
        ('WAREHOUSE', 'WH_ALFA_ANALYTICS',            'ALFA Analytics',        'DBA'),
        ('WAREHOUSE', 'WH_ALFA_LOAD',                 'ALFA Data Engineering', 'DBA'),
        ('WAREHOUSE', 'WH_ALFA_TRANSFORM',            'ALFA Data Engineering', 'DBA'),
        ('WAREHOUSE', 'WH_ALFA_UNLOAD',               'ALFA Data Engineering', 'DBA'),
        ('WAREHOUSE', 'WH_ALFA_QUERY',                'ALFA BI / Reporting',   'DBA'),
        ('WAREHOUSE', 'WH_ALFA_QA',                   'ALFA QA',               'DBA'),
        ('WAREHOUSE', 'COMPUTE_WH',                   'ALFA Shared / Legacy',  'DBA'),
        ('WAREHOUSE', 'BLCOMPUTE_WH',                 'ALFA Shared / Legacy',  'DBA'),
        ('WAREHOUSE', 'CROWDSTRIKE_WH',               'Security Tooling',      'DBA'),
        ('WAREHOUSE', 'DOC_ALWH',                     'ALFA Shared / Legacy',  'DBA'),
        ('WAREHOUSE', 'POSIT_WORKBENCH',              'ALFA Analytics',        'DBA'),
        ('WAREHOUSE', 'SNOWFLAKE_LEARNING_WH',        'Platform / DBA',        'DBA'),
        ('WAREHOUSE', 'SYSTEM$STREAMLIT_NOTEBOOK_WH', 'Platform / DBA',        'DBA'),
        ('WAREHOUSE', 'WH_ALFA_OVERWATCH',            'Platform / DBA',        'DBA'),
        -- Trexis
        ('WAREHOUSE', 'WH_TRXS_LOAD',                 'Trexis Data Engineering', 'DBA'),
        ('WAREHOUSE', 'WH_TRXS_TRANSFORM',            'Trexis Data Engineering', 'DBA'),
        ('WAREHOUSE', 'WH_TRXS_UNLOAD',               'Trexis Data Engineering', 'DBA'),
        ('WAREHOUSE', 'WH_TRXS_QUERY',                'Trexis BI / Reporting',   'DBA'),
        -- Role lens starters (usage attribution, not billing)
        ('ROLE', 'SNOW_ACCOUNTADMINS',                'Platform / DBA',        'DBA'),
        ('ROLE', 'SNOW_SYSADMINS',                    'Platform / DBA',        'DBA'),
        ('ROLE', 'OVERWATCH_MONITOR',                 'Platform / DBA',        'DBA'),
        ('ROLE', 'OVERWATCH_OPERATOR',                'Platform / DBA',        'DBA')
    AS s(MAP_TYPE, NAME, DEPARTMENT, OWNER)
) s
ON t.MAP_TYPE = s.MAP_TYPE AND t.NAME = s.NAME
WHEN NOT MATCHED THEN INSERT (MAP_TYPE, NAME, DEPARTMENT, OWNER)
     VALUES (s.MAP_TYPE, s.NAME, s.DEPARTMENT, s.OWNER);

GRANT SELECT ON TABLE DBA_MAINT_DB.OVERWATCH.DEPARTMENT_MAP TO ROLE OVERWATCH_MONITOR;
GRANT INSERT, UPDATE, DELETE ON TABLE DBA_MAINT_DB.OVERWATCH.DEPARTMENT_MAP TO ROLE OVERWATCH_OPERATOR;

MERGE INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION t
USING (SELECT 8 AS VERSION, 'chargeback: DEPARTMENT_MAP (warehouse=billing, role=usage lens)' AS DESCRIPTION) s
ON t.VERSION = s.VERSION
WHEN NOT MATCHED THEN INSERT (VERSION, DESCRIPTION) VALUES (s.VERSION, s.DESCRIPTION);

SELECT 'V008 applied' AS STATUS;
