-- Repeatable (layer 30: views): SALES.V_OPEN_ORDERS
-- Ticket : EDW-0000          Author: EDW platform team (kit sample)
-- Why    : the reporting contract for orders that are still open, with the net amount computed by
--          SALES.FN_NET_AMOUNT (layer 20, so Flyway re-creates the function first: guard FG013).
--          CREATE OR REPLACE + COPY GRANTS: the replace keeps the SELECT grants consumers already hold (guard FG011);
--          COPY GRANTS comes before COMMENT so no "AS" precedes it (code-review rule ALFA05).
--          SCHEMA.OBJECT names resolve in the view's own database, so the same text is correct on every runway
--          and in a zero-copy clone. Columns are listed explicitly: a SELECT * view changes shape, or fails with a
--          column-mismatch error, once its table is altered.
-- Rules  : docs/CONVENTIONS.md. Schema-qualify every object (SCHEMA.OBJECT); never name the database.

CREATE OR REPLACE VIEW SALES.V_OPEN_ORDERS
    COPY GRANTS
    COMMENT = 'Open sales orders (ORDER_STATUS = OPEN) with the net amount'
AS
SELECT
    o.ORDER_ID,
    o.CUSTOMER_ID,
    o.ORDER_DATE,
    o.CURRENCY_CODE,
    o.ORDER_STATUS,
    o.GROSS_AMOUNT,
    o.DISCOUNT_AMOUNT,
    SALES.FN_NET_AMOUNT(o.GROSS_AMOUNT, o.DISCOUNT_AMOUNT) AS NET_AMOUNT
FROM SALES.ORDERS AS o
WHERE o.ORDER_STATUS = 'OPEN';
