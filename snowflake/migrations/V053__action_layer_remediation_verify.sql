-- V053a__action_layer_remediation_verify.sql — action layer phase a
-- (Tranche C continued; design + sign-off: docs/design/ACTION_LAYER_V053.md).
--
--   Typed savings link + re-derived monthly verifier only. This closes the
--   P1-A selection gap: the app stamps FINDING_TYPE/TARGET_OBJECT on its
--   existing savings-ledger inserts, and the verifier now selects those rows.
--   No stored procedure ships: SP_EXECUTE_REMEDIATION (free-text lever under
--   EXECUTE AS OWNER) and SP_VERIFY_SAVINGS (owner-privileged proof) were both
--   dropped after review — remediation and verify stay on the app's existing
--   guarded paths (typed confirmation + operator gating). Additive + a proc
--   re-derivation only.
--     D1 narrow allow-list (object tuning: warehouse/pipe/task/table; account
--        levers + cancel stay on their guarded raw path); D2 target identifier-validated
--        and NO concatenated stored proof (the injection source is gone); the
--        proof is operator-supplied; D3 proof evidence = QUERY_ID + snapshot;
--        row-affected checks on every UPDATE; sequential-dedup idempotency.
--
--   App paths are proc-first with legacy fallback: pre-V053a the app behaves
--   exactly as v4.54. Apply AFTER V052. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20053, 'V053a requires V052 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 52) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- Typed savings link + proof evidence (additive).
ALTER TABLE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER ADD COLUMN IF NOT EXISTS FINDING_TYPE VARCHAR(40);
ALTER TABLE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER ADD COLUMN IF NOT EXISTS TARGET_OBJECT VARCHAR(300);
ALTER TABLE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER ADD COLUMN IF NOT EXISTS PROOF_QUERY_ID VARCHAR(80);
ALTER TABLE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER ADD COLUMN IF NOT EXISTS PROOF_RESULT VARCHAR(16000);
ALTER TABLE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER ADD COLUMN IF NOT EXISTS PROOF_RUN_AT TIMESTAMP_NTZ;

-- >>> derived:SP_VERIFY_IDLE_SAVINGS
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_VERIFY_IDLE_SAVINGS()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    credit_price FLOAT;
BEGIN
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68)
      INTO :credit_price FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;

    INSERT INTO DBA_MAINT_DB.OVERWATCH.SAVINGS_VERIFICATION_RUNS
        (ITEM_ID, WAREHOUSE_NAME, BASELINE_EST_USD, MEASURED_IDLE_USD_30D, PROPOSED_VERIFIED_USD)
    WITH items AS (
        SELECT ITEM_ID,
               COALESCE(NULLIF(TARGET_OBJECT, ''),
                        TRIM(REPLACE(DESCRIPTION, 'Auto-suspend tune: ', ''))) AS WAREHOUSE_NAME,
               ESTIMATED_USD
        FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER
        WHERE STATE = 'ESTIMATED'
          AND (FINDING_TYPE = 'AUTO_SUSPEND' OR DESCRIPTION LIKE 'Auto-suspend tune: %')
    ),
    query_hours AS (
        SELECT DISTINCT WAREHOUSE_NAME, DATE_TRUNC('hour', START_TIME) AS HOUR_TS
        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
        WHERE START_TIME >= DATEADD('day', -30, CURRENT_DATE())
          AND WAREHOUSE_NAME IS NOT NULL
    ),
    idle_now AS (
        SELECT M.WAREHOUSE_NAME,
               SUM(IFF(Q.HOUR_TS IS NULL, COALESCE(M.CREDITS_USED, 0), 0)) * :credit_price AS IDLE_USD_30D
        FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY M
        LEFT JOIN query_hours Q
               ON Q.WAREHOUSE_NAME = M.WAREHOUSE_NAME
              AND Q.HOUR_TS = DATE_TRUNC('hour', M.START_TIME)
        WHERE M.START_TIME >= DATEADD('day', -30, CURRENT_DATE())
        GROUP BY M.WAREHOUSE_NAME
    )
    SELECT i.ITEM_ID, i.WAREHOUSE_NAME, i.ESTIMATED_USD,
           ROUND(COALESCE(n.IDLE_USD_30D, 0), 2),
           ROUND(GREATEST(0, i.ESTIMATED_USD - COALESCE(n.IDLE_USD_30D, 0)), 2)
    FROM items i
    LEFT JOIN idle_now n ON UPPER(n.WAREHOUSE_NAME) = UPPER(i.WAREHOUSE_NAME);

    RETURN 'savings verification run complete';
END;
$$;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 53 AS VERSION,
       'Action layer phase a (Tranche C): typed SAVINGS_LEDGER link (FINDING_TYPE/TARGET_OBJECT + proof evidence columns) + monthly verifier re-derived to select typed rows (closes the P1-A selection gap). Remediation/verify procs dropped after review; those actions stay on the app guarded path.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 53);
