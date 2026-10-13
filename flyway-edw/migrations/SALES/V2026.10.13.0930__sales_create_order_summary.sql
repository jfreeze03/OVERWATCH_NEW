-- Versioned migration: SALES.sales_create_order_summary
-- Ticket : EDW-0000          Author: EDW platform team (kit sample)
-- Why    : daily order summary (one row per order date and status), rebuilt for a trailing window by
--          SALES.SP_REFRESH_ORDER_SUMMARY (repeatable layer 50) on the schedule of
--          SALES.TASK_REFRESH_ORDER_SUMMARY (layer 80). Tables are versioned; the procedure and the task are
--          repeatable state, re-created only when their files change. CREATE TABLE IF NOT EXISTS keeps a re-run a
--          no-op. The PRIMARY KEY is informational in Snowflake (not enforced); it documents the grain.
-- Rules  : docs/CONVENTIONS.md. Schema-qualify every object (SCHEMA.OBJECT); never name the database.

CREATE TABLE IF NOT EXISTS SALES.ORDER_SUMMARY (
    ORDER_DATE          DATE            NOT NULL,
    ORDER_STATUS        VARCHAR(20)     NOT NULL,
    ORDER_COUNT         NUMBER(18, 0)   NOT NULL,
    GROSS_AMOUNT        NUMBER(18, 2)   NOT NULL,
    NET_AMOUNT          NUMBER(18, 2)   NOT NULL,
    REFRESHED_AT        TIMESTAMP_LTZ   NOT NULL,
    CONSTRAINT PK_ORDER_SUMMARY PRIMARY KEY (ORDER_DATE, ORDER_STATUS)
)
COMMENT = 'Daily order totals by status; written only by SALES.SP_REFRESH_ORDER_SUMMARY';
