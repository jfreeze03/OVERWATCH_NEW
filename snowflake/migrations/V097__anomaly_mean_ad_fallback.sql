-- V097__anomaly_mean_ad_fallback.sql
--
-- SP_ANOMALY_SWEEP mean-absolute-deviation fallback when MAD=0. The COST_ANOMALY_SWEEP arm
-- (V076) computed the robust modified z as 0.6745*(CREDITS-MED)/NULLIF(MAD,0) and then hard-
-- filtered `WHERE l.MAD > 0`, so a majority-idle / intermittent series whose baseline
-- dispersion collapses to MAD=0 was silently dropped -- even for a large material spike. The
-- authoritative app twin app/logic/anomaly.py robust_zscores falls back to a mean-absolute-
-- deviation denominator (_MEANAD_K * dev / mean_ad) when mad==0, so a single spike cannot hide
-- itself; the server never got that fallback.
--
-- Re-derives SP_ANOMALY_SWEEP from V076 with the app estimator ported in, matching its
-- constants and gate order: a meanad sibling CTE (AVG(ABS(CREDITS-MED)) == abs_dev.mean());
-- the denominator is MAD-first (0.6745/MAD) else mean-AD (0.7979/MEAN_AD); both-zero yields a
-- NULL z that never fires. The hard MAD>0 filter is replaced by SIGNED_Z IS NOT NULL. The z<0
-- collapse suppression and the materiality gates ($50 floor, >=10 active days, spike-vs-
-- collapse) are unchanged.
--
-- Procedure re-derivation only, no schema change, no backfill. Owner applies in Snowsight
-- after V096; forward-healing (next daily sweep). This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20097, 'V097 requires V096 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 96) THEN
        RAISE not_ready;
    END IF;
END;
$$;

CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ANOMALY_SWEEP()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    zthr FLOAT;
    credit_price FLOAT;
    ai_model VARCHAR;
    ev_id VARCHAR;
    ev_title VARCHAR;
    day_s VARCHAR;
    series_s VARCHAR;
    wh_s VARCHAR;
    evidence VARCHAR;
    ai_prompt VARCHAR;
    ai_resp VARCHAR;
    c_new CURSOR FOR
        SELECT EVENT_ID, TITLE, DEDUPE_KEY
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
        WHERE RULE_ID = 'COST_ANOMALY_SWEEP'
          AND RAISED_AT >= DATEADD('minute', -15, CURRENT_TIMESTAMP())
          AND DETAIL NOT LIKE '%| AI:%'
        LIMIT 5;
BEGIN
    SELECT COALESCE(MAX(THRESHOLD_NUM), 3.5) INTO :zthr
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
    WHERE RULE_ID = 'COST_ANOMALY_SWEEP' AND ENABLED;

    -- V076: materiality floor mirrors the app-side warehouse anomaly gate
    -- (app/logic/anomaly.py): flag on real money AND a real baseline, so an
    -- idle warehouse cannot post a z+20 event on a trivial active day.
    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68)
      INTO :credit_price FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;

    INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
        (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
    WITH series AS (
        SELECT 'WAREHOUSE ' || WAREHOUSE_NAME AS SERIES, COMPANY, DAY,
               SUM(CREDITS_TOTAL) AS CREDITS
        FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
        WHERE DAY >= DATEADD('day', -29, CURRENT_DATE()) AND DAY < CURRENT_DATE()
        GROUP BY 1, 2, 3
        UNION ALL
        SELECT 'SERVICE ' || SERVICE_TYPE, 'ALL', DAY, SUM(CREDITS_BILLED)
        FROM DBA_MAINT_DB.OVERWATCH.FACT_METERING_DAILY
        WHERE DAY >= DATEADD('day', -29, CURRENT_DATE()) AND DAY < CURRENT_DATE()
        GROUP BY 1, 2, 3
    ),
    med AS (
        SELECT SERIES, MEDIAN(CREDITS) AS MED
        FROM series GROUP BY 1
    ),
    mad AS (
        SELECT s.SERIES, m.MED, MEDIAN(ABS(s.CREDITS - m.MED)) AS MAD
        FROM series s JOIN med m ON m.SERIES = s.SERIES
        GROUP BY 1, 2
    ),
    -- V097: mean-absolute-deviation fallback denominator (== abs_dev.mean() in the
    -- app twin app/logic/anomaly.py robust_zscores) for series whose MAD collapses to 0.
    meanad AS (
        SELECT s.SERIES, AVG(ABS(s.CREDITS - m.MED)) AS MEAN_AD
        FROM series s JOIN med m ON m.SERIES = s.SERIES
        GROUP BY 1
    ),
    active AS (
        SELECT SERIES, COUNT_IF(CREDITS > 0) AS ACTIVE_DAYS
        FROM series GROUP BY 1
    ),
    latest AS (
        SELECT s.SERIES, s.COMPANY, s.DAY, s.CREDITS, m.MED, m.MAD, a.ACTIVE_DAYS,
               IFF(m.MAD > 0, 0.6745, 0.7979) * (s.CREDITS - m.MED)
                   / NULLIF(IFF(m.MAD > 0, m.MAD, ma.MEAN_AD), 0) AS SIGNED_Z,
               ABS(IFF(m.MAD > 0, 0.6745, 0.7979) * (s.CREDITS - m.MED)
                   / NULLIF(IFF(m.MAD > 0, m.MAD, ma.MEAN_AD), 0)) AS ROBUST_Z
        FROM series s
        JOIN mad m ON m.SERIES = s.SERIES
        JOIN meanad ma ON ma.SERIES = s.SERIES
        JOIN active a ON a.SERIES = s.SERIES
        WHERE s.DAY = (SELECT MAX(DAY) FROM series)
    )
    SELECT 'COST_ANOMALY_SWEEP', l.COMPANY,
           IFF(l.ROBUST_Z >= :zthr * 2, 'HIGH', 'MEDIUM'),
           l.SERIES || IFF(l.SIGNED_Z < 0, ' collapsed to ', ' spiked to ') ||
               ROUND(l.CREDITS, 1) || ' credits on ' ||
               TO_VARCHAR(l.DAY) || ' (z=' || ROUND(l.SIGNED_Z, 1) || ')',
           'Median ' || ROUND(l.MED, 1) || ' credits/day over the prior 28d. ' ||
               'Robust z-score ' || ROUND(l.ROBUST_Z, 1) || ' vs threshold ' || :zthr ||
               '. Investigate: Cost > Spend / Attribution for that day.',
           l.ROBUST_Z,
           'COST_ANOMALY_SWEEP|' || l.SERIES || '|' || TO_VARCHAR(l.DAY)
    FROM latest l
    WHERE l.SIGNED_Z IS NOT NULL AND l.ROBUST_Z >= :zthr
      AND l.ACTIVE_DAYS >= 10
      AND (
          (l.SIGNED_Z > 0 AND l.CREDITS * :credit_price >= 50)
          OR (l.SIGNED_Z < 0 AND l.MED * :credit_price >= 50)
      )
      AND NOT EXISTS (
          SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
          WHERE e.DEDUPE_KEY = 'COST_ANOMALY_SWEEP|' || l.SERIES || '|' || TO_VARCHAR(l.DAY)
      );

    -- Dynamic-table refresh failures (guarded: accounts without the view
    -- keep the sweep's cost half working).
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        SELECT c.RULE_ID,
               IFF(d.DATABASE_NAME LIKE 'TRXS%', 'Trexis', 'ALFA'),
               IFF(d.FAILURES >= 5, 'CRITICAL', c.SEVERITY),
               d.DATABASE_NAME || '.' || d.SCHEMA_NAME || '.' || d.NAME ||
                   ': ' || d.FAILURES || ' dynamic-table refresh failure(s) (24h)',
               'Schema ' || d.DATABASE_NAME || '.' || d.SCHEMA_NAME ||
                   ' | last state ' || d.LAST_STATE ||
                   '. Downstream tables are serving stale data until this refreshes.',
               d.FAILURES,
               c.RULE_ID || '|' || d.DATABASE_NAME || '.' || d.SCHEMA_NAME || '.' || d.NAME ||
                   '|' || TO_VARCHAR(CURRENT_DATE())
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
        JOIN (
            SELECT DATABASE_NAME, SCHEMA_NAME, NAME,
                   COUNT_IF(STATE = 'FAILED') AS FAILURES,
                   MAX_BY(STATE, REFRESH_END_TIME) AS LAST_STATE
            FROM SNOWFLAKE.ACCOUNT_USAGE.DYNAMIC_TABLE_REFRESH_HISTORY
            WHERE REFRESH_END_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
            GROUP BY 1, 2, 3
            HAVING COUNT_IF(STATE = 'FAILED') > 0
        ) d ON c.RULE_ID = 'PIPE_DT_FAILURES' AND c.ENABLED AND d.FAILURES > c.THRESHOLD_NUM
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = c.RULE_ID || '|' || d.DATABASE_NAME || '.' || d.SCHEMA_NAME ||
                  '.' || d.NAME || '|' || TO_VARCHAR(CURRENT_DATE())
        );
    EXCEPTION
        WHEN OTHER THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AnomalySweep', 'dynamic_tables_unavailable', 'DT refresh view not readable',
                   'cost anomaly sweep unaffected', CURRENT_ROLE();
    END;


    -- PERF_FINGERPRINT_DRIFT (Mondays): p95 per query family, last 7d vs the
    -- prior 28d — catches regressions that arrive WITHOUT a DDL change
    -- (data growth, clustering decay, plan changes). Complements the
    -- change-anchored V010 tracker.
    IF (DAYOFWEEKISO(CURRENT_DATE()) = 1) THEN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        SELECT c.RULE_ID, 'ALL',
               IFF(f.P95_RECENT_S >= f.P95_BASE_S * 3, 'HIGH', c.SEVERITY),
               'Query family p95 ' || f.P95_BASE_S || 's -> ' || f.P95_RECENT_S || 's: ' ||
                   LEFT(f.SAMPLE_TEXT, 60),
               'Hash ' || f.QUERY_PARAMETERIZED_HASH || ' | runs ' || f.RUNS_BASE || ' -> ' ||
                   f.RUNS_RECENT || ' | 7d vs prior 28d, no change event required. ' ||
                   'Drill: Operations > Queries (heaviest queries).',
               ROUND(100 * (f.P95_RECENT_S / NULLIF(f.P95_BASE_S, 0) - 1), 1),
               c.RULE_ID || '|' || f.QUERY_PARAMETERIZED_HASH || '|' ||
                   TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
        JOIN (
            SELECT QUERY_PARAMETERIZED_HASH,
                   ANY_VALUE(LEFT(QUERY_TEXT, 80)) AS SAMPLE_TEXT,
                   COUNT_IF(START_TIME >= DATEADD('day', -7, CURRENT_TIMESTAMP())) AS RUNS_RECENT,
                   COUNT_IF(START_TIME < DATEADD('day', -7, CURRENT_TIMESTAMP())) AS RUNS_BASE,
                   ROUND(APPROX_PERCENTILE(IFF(START_TIME >= DATEADD('day', -7, CURRENT_TIMESTAMP()),
                                               TOTAL_ELAPSED_TIME, NULL) / 1000, 0.95), 1) AS P95_RECENT_S,
                   ROUND(APPROX_PERCENTILE(IFF(START_TIME < DATEADD('day', -7, CURRENT_TIMESTAMP()),
                                               TOTAL_ELAPSED_TIME, NULL) / 1000, 0.95), 1) AS P95_BASE_S
            FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
            WHERE START_TIME >= DATEADD('day', -35, CURRENT_TIMESTAMP())
              AND EXECUTION_STATUS = 'SUCCESS'
              AND QUERY_PARAMETERIZED_HASH IS NOT NULL
            GROUP BY 1
            HAVING RUNS_RECENT >= 20 AND RUNS_BASE >= 20
        ) f ON c.RULE_ID = 'PERF_FINGERPRINT_DRIFT' AND c.ENABLED
           AND f.P95_BASE_S > 0
           AND f.P95_RECENT_S > f.P95_BASE_S * (1 + c.THRESHOLD_NUM / 100)
           AND f.P95_RECENT_S >= 10
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = c.RULE_ID || '|' || f.QUERY_PARAMETERIZED_HASH || '|' ||
                  TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))
        );
    END IF;


    -- COST_ORG_ACCOUNT_CREEP (guarded): any org account's currency spend up
    -- threshold% week-over-week — a sibling account can't surprise you.
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               o.ACCOUNT_NAME || ' org spend up ' || ROUND(o.PCT, 0) || '% week-over-week',
               'Last 7d ' || ROUND(o.CUR, 0) || ' vs prior ' || ROUND(o.PRV, 0) || ' ' || o.CCY ||
                   '. Breakdown: Admin > Org spend.',
               o.PCT,
               c.RULE_ID || '|' || o.ACCOUNT_NAME || '|' || TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
        JOIN (
            SELECT ACCOUNT_NAME, CCY, CUR, PRV, (CUR / NULLIF(PRV, 0) - 1) * 100 AS PCT
            FROM (
                SELECT ACCOUNT_NAME, MAX(CURRENCY) AS CCY,
                       SUM(IFF(USAGE_DATE >= DATEADD('day', -7, CURRENT_DATE()), USAGE_IN_CURRENCY, 0)) AS CUR,
                       SUM(IFF(USAGE_DATE < DATEADD('day', -7, CURRENT_DATE()), USAGE_IN_CURRENCY, 0)) AS PRV
                FROM SNOWFLAKE.ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY
                WHERE USAGE_DATE >= DATEADD('day', -14, CURRENT_DATE())
                GROUP BY 1
            )
        ) o ON c.RULE_ID = 'COST_ORG_ACCOUNT_CREEP' AND c.ENABLED
           AND o.PCT > c.THRESHOLD_NUM AND o.CUR >= 100
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = c.RULE_ID || '|' || o.ACCOUNT_NAME || '|' ||
                  TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))
        );
    EXCEPTION
        WHEN OTHER THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AnomalySweep', 'org_usage_unavailable', 'ORGANIZATION_USAGE not readable',
                   'org creep check skipped', CURRENT_ROLE();
    END;

    -- PIPE_VOLUME_DROP (guarded): yesterday's rows-added collapsed vs the
    -- prior-7-day average on tables that normally move real volume.
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        SELECT c.RULE_ID,
               IFF(v.DB LIKE 'TRXS%', 'Trexis', 'ALFA'),
               c.SEVERITY,
               v.DB || '.' || v.SCH || '.' || v.TBL || ' volume down ' || ROUND(v.DROP_PCT, 0) ||
                   '% (' || v.Y_ROWS || ' rows vs ~' || ROUND(v.AVG_ROWS, 0) || '/day)',
               'Yesterday vs prior-7d average. Upstream feed, failed COPY, or intentional? ' ||
                   'Check Operations > Pipeline SLA.',
               v.DROP_PCT,
               c.RULE_ID || '|' || v.DB || '.' || v.SCH || '.' || v.TBL || '|' ||
                   TO_VARCHAR(CURRENT_DATE())
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
        JOIN (
            SELECT DB, SCH, TBL, Y_ROWS, AVG_ROWS,
                   (1 - Y_ROWS / NULLIF(AVG_ROWS, 0)) * 100 AS DROP_PCT
            FROM (
                SELECT d.DATABASE_NAME AS DB, d.SCHEMA_NAME AS SCH, d.TABLE_NAME AS TBL,
                       SUM(IFF(DATE(d.START_TIME) = DATEADD('day', -1, CURRENT_DATE()),
                               d.ROWS_ADDED, 0)) AS Y_ROWS,
                       SUM(IFF(DATE(d.START_TIME) < DATEADD('day', -1, CURRENT_DATE()),
                               d.ROWS_ADDED, 0)) / 7 AS AVG_ROWS
                FROM SNOWFLAKE.ACCOUNT_USAGE.TABLE_DML_HISTORY d
                WHERE d.START_TIME >= DATEADD('day', -8, CURRENT_DATE())
                  AND d.START_TIME < CURRENT_DATE()
                  -- PROD only, BOTH companies (owner decision 2026-07-08
                  -- after the DEV/SIT storm): ALFA_EDW_PRD + ALFA_EDW_MGM by
                  -- name, and every *_PRD database by suffix — which is what
                  -- covers Trexis PROD (TRXS_EDW_PRD, TRXS_GW_DATA_PRD,
                  -- TRXS_ABC_METADATA_PRD). DEV/SIT/SAN stay silent. Same
                  -- semantics as app environment_clause('PROD').
                  AND (UPPER(d.DATABASE_NAME) IN ('ALFA_EDW_PRD', 'ALFA_EDW_MGM')
                       OR UPPER(d.DATABASE_NAME) LIKE '%!_PRD' ESCAPE '!')
                GROUP BY 1, 2, 3
                HAVING AVG_ROWS >= 1000
            )
        ) v ON c.RULE_ID = 'PIPE_VOLUME_DROP' AND c.ENABLED
           AND v.DROP_PCT > c.THRESHOLD_NUM
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = c.RULE_ID || '|' || v.DB || '.' || v.SCH || '.' || v.TBL ||
                  '|' || TO_VARCHAR(CURRENT_DATE())
        );
    EXCEPTION
        WHEN OTHER THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AnomalySweep', 'dml_history_unavailable', 'TABLE_DML_HISTORY not readable',
                   'volume-drop check skipped', CURRENT_ROLE();
    END;

    -- Pre-explain fresh anomalies (guarded): grounded Cortex hypothesis is
    -- appended to the event DETAIL so the webhook message arrives explained.
    -- Capped at 5 events/run to bound AI spend.
    BEGIN
        SELECT COALESCE(MAX(IFF(KEY = 'CORTEX_MODEL', VALUE, NULL)), 'llama3.1-8b')
          INTO :ai_model FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;
        FOR e IN c_new DO
            ev_id := e.EVENT_ID;
            ev_title := e.TITLE;
            series_s := SPLIT_PART(e.DEDUPE_KEY, '|', 2);
            day_s := SPLIT_PART(e.DEDUPE_KEY, '|', 3);
            wh_s := IFF(series_s LIKE 'WAREHOUSE %', LTRIM(SUBSTR(series_s, 10)), '');
            SELECT LISTAGG(SAMPLE_TEXT || ' day=' || H_DAY || 'h prior_avg=' || H_PRI || 'h', '; ')
              INTO :evidence
            FROM (
                SELECT ANY_VALUE(LEFT(QUERY_TEXT, 60)) AS SAMPLE_TEXT,
                       ROUND(SUM(IFF(DATE(START_TIME) = TO_DATE(:day_s), TOTAL_ELAPSED_TIME, 0)) / 3600000, 2) AS H_DAY,
                       ROUND(SUM(IFF(DATE(START_TIME) < TO_DATE(:day_s), TOTAL_ELAPSED_TIME, 0)) / 7 / 3600000, 2) AS H_PRI
                FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                WHERE START_TIME >= DATEADD('day', -7, TO_DATE(:day_s))
                  AND START_TIME < DATEADD('day', 1, TO_DATE(:day_s))
                  AND (:wh_s = '' OR WAREHOUSE_NAME = :wh_s)
                  AND QUERY_PARAMETERIZED_HASH IS NOT NULL
                GROUP BY QUERY_PARAMETERIZED_HASH
                ORDER BY H_DAY DESC
                LIMIT 10
            );
            ai_prompt := 'You are a Snowflake cost analyst. ALERT: ' || :ev_title ||
                         '. EVIDENCE (top query families, elapsed hours on the day vs prior-7d avg): ' ||
                         COALESCE(:evidence, 'none') ||
                         '. Using ONLY this evidence, name the 1-2 most likely drivers with their ' ||
                         'numbers, or say evidence is inconclusive. Max 80 words. Never invent data.';
            ai_resp := SNOWFLAKE.CORTEX.COMPLETE(:ai_model, :ai_prompt);
            UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
               SET DETAIL = LEFT(COALESCE(DETAIL, '') || ' | AI: ' || :ai_resp, 2000)
             WHERE EVENT_ID = :ev_id;
        END FOR;
    EXCEPTION
        WHEN OTHER THEN
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AnomalySweep', 'cortex_pre_explain_unavailable',
                   'CORTEX.COMPLETE failed - events remain unexplained (drawer AI still works)',
                   'model or grant issue', CURRENT_ROLE();
    END;

    RETURN 'anomaly sweep v3 complete';
END;
$$;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 97 AS VERSION,
       'SP_ANOMALY_SWEEP mean-AD fallback: COST_ANOMALY_SWEEP re-derived from V076 so a series whose median-absolute-deviation collapses to 0 (intermittent / majority-idle serverless, or steady-constant) no longer silently drops its spike. Adds a meanad sibling CTE (AVG(ABS(CREDITS-MED))) and picks the robust-z denominator MAD-first (0.6745/MAD) else mean-AD (0.7979/MEAN_AD), mirroring app.logic.anomaly.robust_zscores constants + gate order; drops the hard WHERE MAD>0 for a SIGNED_Z IS NOT NULL guard. Collapse suppression + materiality gates ($50, >=10 active days) unchanged. Proc only, no backfill; forward-healing on the next sweep.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 97);
