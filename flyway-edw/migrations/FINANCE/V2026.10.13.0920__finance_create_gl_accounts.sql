-- Versioned migration: FINANCE.finance_create_gl_accounts
-- Ticket : EDW-0000          Author: EDW platform team (kit sample)
-- Why    : general-ledger chart of accounts, loaded by the finance ETL and read by reporting views.
--          CREATE TABLE IF NOT EXISTS, never CREATE OR REPLACE TABLE: a replace would drop the rows, their Time
--          Travel history and every stream on the table (guard FG006). Later changes are new versions that ALTER it.
--          The PRIMARY KEY is informational in Snowflake (not enforced); it documents the grain for consumers.
-- Rules  : docs/CONVENTIONS.md. Schema-qualify every object (SCHEMA.OBJECT); never name the database.

CREATE TABLE IF NOT EXISTS FINANCE.GL_ACCOUNTS (
    GL_ACCOUNT_ID       VARCHAR(20)     NOT NULL COMMENT 'Ledger account code, e.g. 4000-100',
    ACCOUNT_NAME        VARCHAR(200)    NOT NULL,
    ACCOUNT_TYPE        VARCHAR(20)     NOT NULL COMMENT 'ASSET, LIABILITY, EQUITY, REVENUE or EXPENSE',
    PARENT_ACCOUNT_ID   VARCHAR(20)     COMMENT 'Roll-up parent; NULL for a top-level account',
    IS_ACTIVE           BOOLEAN         NOT NULL DEFAULT TRUE,
    VALID_FROM          DATE            NOT NULL,
    LOADED_AT           TIMESTAMP_LTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT PK_GL_ACCOUNTS PRIMARY KEY (GL_ACCOUNT_ID)
)
COMMENT = 'General-ledger chart of accounts (one row per account code)';
