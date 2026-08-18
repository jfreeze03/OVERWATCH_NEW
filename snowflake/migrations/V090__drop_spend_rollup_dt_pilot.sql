-- V090__drop_spend_rollup_dt_pilot.sql
--
-- Retire the MART_SPEND_ROLLUP_DT Dynamic Table pilot (V015). It was a deliberate,
-- low-risk experiment to measure Dynamic-Table refresh cost vs the MERGE marts
-- ("Additive: nothing reads it yet"). The app standardized on scheduled-task marts;
-- the 2026-08-17 audit confirmed NOTHING reads the DT (no app query, procedure, task,
-- or downstream mart), yet it keeps auto-refreshing every ~6h on WH_ALFA_ADMIN,
-- spending serverless credits for no consumer. Drop it.
--
-- Idempotent DROP ... IF EXISTS; no data loss (the rollup is derivable from
-- FACT_METERING_DAILY, untouched). FACT_METERING_DAILY.CHANGE_TRACKING (enabled in
-- V015 for the DT) is left ON -- cheap, and a future reader could rely on it. Owner
-- applies in Snowsight after V089. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20090, 'V090 requires V089 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 89) THEN
        RAISE not_ready;
    END IF;
END;
$$;

DROP DYNAMIC TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.MART_SPEND_ROLLUP_DT;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 90 AS VERSION,
       'Retire the MART_SPEND_ROLLUP_DT Dynamic Table pilot (V015): a low-risk MERGE-vs-DT refresh-cost experiment that nothing ever read; the app standardized on scheduled-task marts. As a Dynamic Table it kept auto-refreshing every ~6h on WH_ALFA_ADMIN with no consumer -- pure serverless waste. DROP ... IF EXISTS; no data loss (derivable from FACT_METERING_DAILY).' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 90);
