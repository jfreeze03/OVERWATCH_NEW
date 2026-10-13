-- Repeatable (layer 50: procedures): SALES.SP_REFRESH_ORDER_SUMMARY
-- Ticket : EDW-0000          Author: EDW platform team (kit sample)
-- Why    : rebuilds SALES.ORDER_SUMMARY for a trailing 35-day window from SALES.ORDERS; called daily by
--          SALES.TASK_REFRESH_ORDER_SUMMARY (layer 80). The window is deleted and re-inserted in ONE transaction, so
--          readers never see it half-written and a failure leaves the previous totals in place.
--          EXECUTE AS OWNER: the procedure runs with the deploy role's privileges and resolves SCHEMA.OBJECT names in
--          its own database, so the same body is correct on every runway and in a zero-copy clone. Keeping the task
--          body a one-line CALL means logic changes replace only this procedure, never the task (no suspend needed).
--          CREATE OR REPLACE + COPY GRANTS keeps the USAGE grant from R__90 across the replace (guard FG011).
--          The body is a dollar-quoted block, which Flyway's Snowflake parser reads as ONE statement.
-- Rules  : docs/CONVENTIONS.md. Schema-qualify every object (SCHEMA.OBJECT); never name the database.

CREATE OR REPLACE PROCEDURE SALES.SP_REFRESH_ORDER_SUMMARY()
    COPY GRANTS
    RETURNS VARCHAR
    LANGUAGE SQL
    COMMENT = 'Rebuilds SALES.ORDER_SUMMARY for the trailing 35 days from SALES.ORDERS (one transaction)'
    EXECUTE AS OWNER
AS
$$
DECLARE
    window_start DATE DEFAULT DATEADD('day', -35, CURRENT_DATE());
    rows_written INTEGER DEFAULT 0;
BEGIN
    BEGIN TRANSACTION;
    DELETE FROM SALES.ORDER_SUMMARY
     WHERE ORDER_DATE >= :window_start;
    INSERT INTO SALES.ORDER_SUMMARY
        (ORDER_DATE, ORDER_STATUS, ORDER_COUNT, GROSS_AMOUNT, NET_AMOUNT, REFRESHED_AT)
    SELECT
        o.ORDER_DATE,
        COALESCE(o.ORDER_STATUS, 'OPEN') AS ORDER_STATUS,
        COUNT(*) AS ORDER_COUNT,
        SUM(o.GROSS_AMOUNT) AS GROSS_AMOUNT,
        SUM(SALES.FN_NET_AMOUNT(o.GROSS_AMOUNT, o.DISCOUNT_AMOUNT)) AS NET_AMOUNT,
        CURRENT_TIMESTAMP() AS REFRESHED_AT
    FROM SALES.ORDERS AS o
    WHERE o.ORDER_DATE >= :window_start
    GROUP BY o.ORDER_DATE, COALESCE(o.ORDER_STATUS, 'OPEN');
    rows_written := SQLROWCOUNT;
    COMMIT;
    RETURN 'SALES.ORDER_SUMMARY rebuilt from ' || TO_VARCHAR(window_start) || ': ' || rows_written || ' row(s)';
EXCEPTION
    WHEN OTHER THEN
        ROLLBACK;
        RAISE;
END;
$$;
