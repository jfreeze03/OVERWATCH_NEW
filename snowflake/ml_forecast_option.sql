-- ml_forecast_option.sql — OPT-IN native ML forecasting (FORECAST_ENGINE=ml_forecast).
-- Like webhook_delivery.sql: run deliberately, not part of the numbered chain.
-- Requires the SNOWFLAKE.ML.FORECAST privileges (SNOWFLAKE.CORTEX_USER covers
-- current accounts) and bills serverless credits on train/refresh.
--
-- What you get over the built-in engines: modeled seasonality + real
-- confidence intervals. The app reads FORECAST_ML_DAILY when
-- SETTINGS.FORECAST_ENGINE = 'ml_forecast' and falls back to the seasonal
-- engine when this is absent.

-- 1) + 2) Train AND materialize 45 days of projections, in one procedure (a
--    table, so page loads never pay inference). A trained ML.FORECAST model is
--    immutable and forecasts the 45 steps after ITS last training timestamp, so
--    inference alone rewrites the same fixed dates every week: the horizon
--    drains and the month-end projection shrinks to the few dates left
--    (c09 R1-229). The procedure therefore RETRAINS on every run (the weekly
--    task below), through all complete days, before it re-materializes.
--    EXECUTE IMMEDIATE runs the CREATE ... SNOWFLAKE.ML.FORECAST class statement
--    exactly as a top-level worksheet statement would. A failed retrain raises,
--    so FORECAST_ML_DAILY is never replaced by a stale-horizon table; the app
--    falls back to its disclosed seasonal engine when the horizon no longer
--    covers the month.
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_REFRESH_ML_FORECAST()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
BEGIN
    EXECUTE IMMEDIATE '
        CREATE OR REPLACE SNOWFLAKE.ML.FORECAST DBA_MAINT_DB.OVERWATCH.OVERWATCH_SPEND_FORECAST(
            INPUT_DATA => TABLE(
                SELECT DAY::TIMESTAMP_NTZ AS TS, SUM(CREDITS_BILLED) AS CREDITS
                FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
                WHERE DAY < CURRENT_DATE()
                GROUP BY DAY
            ),
            TIMESTAMP_COLNAME => ''TS'',
            TARGET_COLNAME => ''CREDITS''
        )';
    CREATE OR REPLACE TABLE DBA_MAINT_DB.OVERWATCH.FORECAST_ML_DAILY AS
    SELECT ts AS TS,
           GREATEST(forecast, 0) AS FORECAST_CREDITS,
           GREATEST(lower_bound, 0) AS LOWER_BOUND,
           GREATEST(upper_bound, 0) AS UPPER_BOUND
    FROM TABLE(DBA_MAINT_DB.OVERWATCH.OVERWATCH_SPEND_FORECAST!FORECAST(
        FORECASTING_PERIODS => 45));
    RETURN 'ml forecast retrained and refreshed';
END;
$$;

-- First train + materialize now (the task below repeats both weekly):
CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_ML_FORECAST();

-- 3) Weekly retrain + refresh task (created suspended; resume once happy with cost):
CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_REFRESH_ML_FORECAST
    WAREHOUSE = WH_ALFA_ADMIN
    SCHEDULE = 'USING CRON 50 5 * * 0 America/Chicago'
AS
    CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_ML_FORECAST();
-- ALTER TASK DBA_MAINT_DB.OVERWATCH.TASK_REFRESH_ML_FORECAST RESUME;

-- 4) Flip the app: UPDATE DBA_MAINT_DB.OVERWATCH.SETTINGS
--        SET VALUE = 'ml_forecast' WHERE KEY = 'FORECAST_ENGINE';
