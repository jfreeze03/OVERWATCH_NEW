-- Versioned migration: SALES.sales_create_orders
-- Ticket : EDW-0000          Author: EDW platform team (kit sample)
-- Why    : order header table, loaded by the sales ETL; SALES.V_OPEN_ORDERS and SALES.SP_REFRESH_ORDER_SUMMARY
--          read it. CREATE TABLE IF NOT EXISTS, never CREATE OR REPLACE TABLE: a replace would drop the rows, their
--          Time Travel history and every stream on the table (guard FG006). Later changes are new versions that
--          ALTER it (see V2026.10.13.0915). The PRIMARY KEY is informational in Snowflake (not enforced).
--          Undo: U2026.10.13.0900__sales_create_orders.sql (DEV / test runways only, see that file).
-- Rules  : docs/CONVENTIONS.md. Schema-qualify every object (SCHEMA.OBJECT); never name the database.

CREATE TABLE IF NOT EXISTS SALES.ORDERS (
    ORDER_ID            NUMBER(38, 0)   NOT NULL,
    CUSTOMER_ID         NUMBER(38, 0)   NOT NULL,
    ORDER_DATE          DATE            NOT NULL,
    CURRENCY_CODE       VARCHAR(3)      NOT NULL DEFAULT 'USD' COMMENT 'ISO 4217 currency of the amounts',
    GROSS_AMOUNT        NUMBER(18, 2)   NOT NULL,
    DISCOUNT_AMOUNT     NUMBER(18, 2)   NOT NULL DEFAULT 0,
    LOADED_AT           TIMESTAMP_LTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT PK_ORDERS PRIMARY KEY (ORDER_ID)
)
COMMENT = 'Sales order headers (one row per order)';
