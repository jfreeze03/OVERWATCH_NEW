-- Repeatable (layer 20: functions): SALES.FN_NET_AMOUNT
-- Ticket : EDW-0000          Author: EDW platform team (kit sample)
-- Why    : one definition of "net amount" (gross minus discount, never below zero) shared by SALES.V_OPEN_ORDERS
--          (layer 30) and SALES.SP_REFRESH_ORDER_SUMMARY (layer 50). Layer 20 sorts before both, so Flyway
--          re-creates the function before anything that calls it (guard FG013).
--          Repeatable = desired state: Flyway re-runs this file whenever its checksum changes, so the statement is
--          CREATE OR REPLACE, and COPY GRANTS keeps the explicit grants on the function across the replace
--          (guard FG011). The body names only its arguments; a body that read a table would write SCHEMA.TABLE,
--          which resolves in the function's own database on every runway and in every clone.
-- Rules  : docs/CONVENTIONS.md. Changing the argument list creates a NEW overload instead of replacing this one:
--          drop the old signature in a versioned migration when you do that.

CREATE OR REPLACE FUNCTION SALES.FN_NET_AMOUNT(GROSS_AMOUNT NUMBER, DISCOUNT_AMOUNT NUMBER)
    COPY GRANTS
    RETURNS NUMBER(18, 2)
    LANGUAGE SQL
    IMMUTABLE
    COMMENT = 'Net order amount: gross minus discount, floored at zero; NULL inputs count as zero'
AS
$$
    GREATEST(COALESCE(GROSS_AMOUNT, 0) - COALESCE(DISCOUNT_AMOUNT, 0), 0)
$$;
