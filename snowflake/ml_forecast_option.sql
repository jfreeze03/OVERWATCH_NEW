-- ml_forecast_option.sql — OPT-IN native ML forecasting (FORECAST_ENGINE=ml_forecast).
-- Like webhook_delivery.sql: run deliberately, not part of the numbered chain.
-- Requires the SNOWFLAKE.ML.FORECAST privileges (SNOWFLAKE.CORTEX_USER covers
-- current accounts) and bills serverless credits on train/refresh.
--
-- What you get over the built-in engines: modeled seasonality + real
-- confidence intervals. The app reads FORECAST_ML_DAILY when
-- SETTINGS.FORECAST_ENGINE = 'ml_forecast' and falls back to the seasonal
-- engine when this is absent.

-- 1) Train (weekly retrain keeps it honest as patterns drift):
CREATE OR REPLACE SNOWFLAKE.ML.FORECAST DBA_MAINT_DB.OVERWATCH.OVERWATCH_SPEND_FORECAST(
    INPUT_DATA => TABLE(
        SELECT DAY::TIMESTAMP_NTZ AS TS, SUM(CREDITS_BILLED) AS CREDITS
        FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
        WHERE DAY < CURRENT_DATE()
        GROUP BY DAY
    ),
    TIMESTAMP_COLNAME => 'TS',
    TARGET_COLNAME => 'CREDITS'
);

-- 2) Materialize 45 days of projections (a table, so page loads never pay
--    inference):
CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_REFRESH_ML_FORECAST()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
BEGIN
    CREATE OR REPLACE TABLE DBA_MAINT_DB.OVERWATCH.FORECAST_ML_DAILY AS
    SELECT ts AS TS,
           GREATEST(forecast, 0) AS FORECAST_CREDITS,
           GREATEST(lower_bound, 0) AS LOWER_BOUND,
           GREATEST(upper_bound, 0) AS UPPER_BOUND
    FROM TABLE(DBA_MAINT_DB.OVERWATCH.OVERWATCH_SPEND_FORECAST!FORECAST(
        FORECASTING_PERIODS => 45));
    RETURN 'ml forecast refreshed';
END;
$$;

CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_ML_FORECAST();

-- 3) Weekly refresh task (created suspended; resume once happy with cost):
CREATE TASK IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.TASK_REFRESH_ML_FORECAST
    WAREHOUSE = WH_ALFA_OVERWATCH
    SCHEDULE = 'USING CRON 50 5 * * 0 America/Chicago'
AS
    CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_ML_FORECAST();
-- ALTER TASK DBA_MAINT_DB.OVERWATCH.TASK_REFRESH_ML_FORECAST RESUME;

-- 4) Flip the app: UPDATE DBA_MAINT_DB.OVERWATCH.SETTINGS
--        SET VALUE = 'ml_forecast' WHERE KEY = 'FORECAST_ENGINE';
