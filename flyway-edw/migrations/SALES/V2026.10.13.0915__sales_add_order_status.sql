-- Versioned migration: SALES.sales_add_order_status
-- Ticket : EDW-0000          Author: EDW platform team (kit sample)
-- Why    : order lifecycle status (OPEN, SHIPPED, CLOSED, CANCELLED) for SALES.V_OPEN_ORDERS and the order summary.
--          A table change is a NEW versioned migration that ALTERs the table; the applied create
--          (V2026.10.13.0900) is never edited (guard FG003: Flyway checksums would fail on every runway).
--          ADD COLUMN IF NOT EXISTS makes a re-run after a partial failure a no-op (Snowflake DDL autocommits, so
--          Flyway cannot roll a failed script back). Nullable with a DEFAULT, so the ALTER succeeds on a populated
--          table and loaders that do not send the column still get 'OPEN' on new rows.
-- Rules  : docs/CONVENTIONS.md. Schema-qualify every object (SCHEMA.OBJECT); never name the database.

ALTER TABLE SALES.ORDERS ADD COLUMN IF NOT EXISTS ORDER_STATUS VARCHAR(20) DEFAULT 'OPEN';

COMMENT ON COLUMN SALES.ORDERS.ORDER_STATUS IS 'Lifecycle status: OPEN, SHIPPED, CLOSED or CANCELLED';
