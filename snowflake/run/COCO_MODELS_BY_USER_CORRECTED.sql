-- Cortex Code (CoCo): which models each user is billed for, one Central month, all three interfaces
-- (Snowsight, CLI and Desktop). Read-only, one scan of the view. Run as a role with IMPORTED PRIVILEGES on the
-- SNOWFLAKE database (SNOW_ACCOUNTADMINS or SNOW_SYSADMINS). Edit the three values in params.
-- What changed from the concept query:
--   * every credit leaf is COALESCEd, so a model entry missing input or output is no longer dropped (NULL + x = NULL);
--   * requests with no model breakdown are kept (OUTER => TRUE) and land on a "(not broken down by model)" row,
--     so each user's model rows add back to that user's TOTAL_CREDITS (TOKEN_CREDITS);
--   * the month is cut on the Central (account) day, not on the worksheet session's time zone;
--   * users are keyed by USER_ID (name from USERS), so a rename cannot split one person;
--   * no HAVING floor (small models stay visible); top 15 kept as in the concept (delete the USER_RANK line for all);
--   * one scan instead of two; USD at the AI rate, not the 3.68 compute rate.
--   * REQUESTS_AS_MAIN_MODEL = requests where this model carried the most credits: the closest thing the view has
--     to "the model the user picked" (one request can bill several models, e.g. a main model plus a helper).
WITH params AS (
    SELECT '2026-09-01'::DATE AS M_LO,          -- first day, inclusive (Central)
           '2026-10-01'::DATE AS M_HI,          -- day after the last day, exclusive (Central)
           2.20::FLOAT AS AI_RATE               -- USD per AI credit (SETTINGS AI_CREDIT_PRICE_USD, contract 2.20)
),
base AS (
    SELECT COALESCE(c.USER_ID, -1) AS USER_ID,
           c.USER_NAME AS VIEW_USER_NAME,
           LOWER(c.INTERFACE) AS INTERFACE,
           COALESCE(c.TOKEN_CREDITS, 0) AS TOKEN_CREDITS,
           c.CREDITS_GRANULAR
    FROM SNOWFLAKE.ACCOUNT_USAGE.SNOWFLAKE_COCO_USAGE_HISTORY c
    CROSS JOIN params p
    WHERE c.USAGE_TIME >= DATEADD('day', -1, p.M_LO)::TIMESTAMP_TZ      -- pruning slack only
      AND c.USAGE_TIME <  DATEADD('day', 1, p.M_HI)::TIMESTAMP_TZ
      AND CONVERT_TIMEZONE('America/Chicago', c.USAGE_TIME)::DATE >= p.M_LO   -- the exact Central-day cut
      AND CONVERT_TIMEZONE('America/Chicago', c.USAGE_TIME)::DATE <  p.M_HI
),
per_model AS (
    SELECT b.USER_ID, b.VIEW_USER_NAME, b.INTERFACE, b.TOKEN_CREDITS,
           f.SEQ AS RID,                                                    -- one id per request row
           COALESCE(NULLIF(TRIM(f.KEY::STRING), ''), '(not broken down by model)') AS MODEL_NAME,
           COALESCE(TRY_TO_DOUBLE(TO_VARCHAR(f.VALUE:input)), 0)
         + COALESCE(TRY_TO_DOUBLE(TO_VARCHAR(f.VALUE:cache_read_input)), 0)
         + COALESCE(TRY_TO_DOUBLE(TO_VARCHAR(f.VALUE:cache_write_input)), 0)
         + COALESCE(TRY_TO_DOUBLE(TO_VARCHAR(f.VALUE:output)), 0)
         + IFF(IS_OBJECT(f.VALUE), 0, COALESCE(TRY_TO_DOUBLE(TO_VARCHAR(f.VALUE)), 0)) AS MODEL_CREDITS
    FROM base b,
         LATERAL FLATTEN(INPUT => b.CREDITS_GRANULAR, OUTER => TRUE) f
),
ranked AS (
    SELECT pm.*,
           ROW_NUMBER() OVER (PARTITION BY pm.RID ORDER BY pm.MODEL_CREDITS DESC, pm.MODEL_NAME) AS MODEL_RANK
    FROM per_model pm
),
user_tot AS (
    SELECT r.USER_ID,
           MAX(r.VIEW_USER_NAME) AS VIEW_USER_NAME,
           LISTAGG(DISTINCT r.INTERFACE, '+') WITHIN GROUP (ORDER BY r.INTERFACE) AS USER_INTERFACES,
           COUNT_IF(r.MODEL_RANK = 1) AS USER_REQUESTS,
           SUM(IFF(r.MODEL_RANK = 1, r.TOKEN_CREDITS, 0)) AS USER_CREDITS,      -- each request counted once
           SUM(r.MODEL_CREDITS) AS USER_MODEL_CREDITS,
           ROW_NUMBER() OVER (ORDER BY SUM(IFF(r.MODEL_RANK = 1, r.TOKEN_CREDITS, 0)) DESC, r.USER_ID) AS USER_RANK
    FROM ranked r
    GROUP BY r.USER_ID
),
user_model AS (
    SELECT x.USER_ID, x.MODEL_NAME,
           LISTAGG(DISTINCT x.INTERFACE, '+') WITHIN GROUP (ORDER BY x.INTERFACE) AS MODEL_INTERFACES,
           SUM(x.MAIN) AS REQUESTS_AS_MAIN_MODEL,
           SUM(x.BILLING) AS REQUESTS_BILLING_MODEL,
           SUM(x.MODEL_CREDITS) AS MODEL_CREDITS
    FROM (
        SELECT r.USER_ID, r.MODEL_NAME, r.INTERFACE,
               IFF(r.MODEL_RANK = 1, 1, 0) AS MAIN, 1 AS BILLING, r.MODEL_CREDITS
        FROM ranked r
        UNION ALL
        -- residual: the user's TOKEN_CREDITS that no model leaf explains (or the excess, if leaves are larger)
        SELECT t.USER_ID,
               IFF(t.USER_CREDITS >= t.USER_MODEL_CREDITS, '(not broken down by model)',
                   '(model credits above the request totals)'),
               NULL, 0, 0, t.USER_CREDITS - t.USER_MODEL_CREDITS
        FROM user_tot t
        WHERE ABS(t.USER_CREDITS - t.USER_MODEL_CREDITS) >= 0.0001
    ) x
    GROUP BY x.USER_ID, x.MODEL_NAME
),
users1 AS (
    SELECT s.USER_ID, s.NAME
    FROM SNOWFLAKE.ACCOUNT_USAGE.USERS s
    QUALIFY ROW_NUMBER() OVER (PARTITION BY s.USER_ID ORDER BY s.CREATED_ON DESC NULLS LAST) = 1
)
SELECT t.USER_RANK,
       COALESCE(u.NAME, t.VIEW_USER_NAME, 'UNKNOWN (' || t.USER_ID || ')') AS USER_NAME,
       t.USER_INTERFACES,
       t.USER_REQUESTS,
       ROUND(t.USER_CREDITS, 2) AS TOTAL_CREDITS,
       ROUND(t.USER_CREDITS * p.AI_RATE, 2) AS TOTAL_USD,
       m.MODEL_NAME,
       m.MODEL_INTERFACES,
       m.REQUESTS_AS_MAIN_MODEL,
       m.REQUESTS_BILLING_MODEL,
       ROUND(m.MODEL_CREDITS, 2) AS MODEL_CREDITS,
       ROUND(m.MODEL_CREDITS * p.AI_RATE, 2) AS MODEL_USD,
       ROUND(100 * m.MODEL_CREDITS / NULLIF(t.USER_CREDITS, 0), 1) AS PCT_OF_USER_CREDITS
FROM user_tot t
JOIN user_model m ON m.USER_ID = t.USER_ID
LEFT JOIN users1 u ON u.USER_ID = t.USER_ID
CROSS JOIN params p
WHERE t.USER_RANK <= 15                          -- delete this line to list every user
ORDER BY t.USER_RANK, m.MODEL_CREDITS DESC NULLS LAST, m.MODEL_NAME;