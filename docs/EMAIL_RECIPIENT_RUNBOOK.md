# OVERWATCH Email Recipient Runbook

How to change (or restore) the recipient of OVERWATCH's **email** alerts.

Throughout, `<recipient>` means the destination address you want, e.g.
`someone@your-domain.com`. Run everything as **ACCOUNTADMIN** in Snowsight.

## What actually sends these emails

OVERWATCH's primary alert channel is the **Teams webhook** (`OVERWATCH_WEBHOOK`
notification integration). Email is a **separate, opt-in** path defined in
[`snowflake/native_alert_templates.sql`](../snowflake/native_alert_templates.sql):
four native Snowflake `ALERT` objects that call `SYSTEM$SEND_EMAIL` through an
email notification integration named **`OVERWATCH_EMAIL`**. Three of them are
out-of-band **dead-man** watchers: they email when OVERWATCH itself goes quiet, and
they do not depend on the in-app notifier, so they still fire when that is what broke.

| Alert | Fires | Subject |
|---|---|---|
| `NATIVE_ALERT_NEW_EVENTS` | new OPEN critical/high alert events (every 30 min) | `OVERWATCH: new critical/high alerts` |
| `NATIVE_ALERT_STALE_FACTS` | any source stale past its cadence (3h hourly / 30h daily) or a loader failure (hourly at :10) | `OVERWATCH: telemetry stale or a loader failed` |
| `NATIVE_ALERT_SCAN_HEARTBEAT` | no SUCCEEDED `TASK_ALERT_SCAN` / `TASK_ALERT_NOTIFY` run in 3h (hourly at :10) | `OVERWATCH: alert scan/notify heartbeat lost` |
| `NATIVE_ALERT_DELIVERY_FAILING` | a Teams/webhook send failure, or a CRITICAL open > 60 min with no delivery (hourly at :10) | `OVERWATCH: alert delivery failing` |

**Alerts > Native delivery** shows an **Email path** row (LIVE / FAILING / SUSPENDED /
PARTIAL / not visible) read from `SHOW ALERTS`, `ALERT_HISTORY` and
`NOTIFICATION_HISTORY`. It turns red only on a real send or evaluation failure.

**There is no app-side email setting.** Nothing in the Streamlit app, `SETTINGS`,
or a numbered migration holds the recipient. (The `EMAIL` column on
`FACT_AI_USAGE_DAILY` is unrelated — it is Cortex-usage attribution data.)

## The complete footprint — the recipient lives in exactly 3 requirements

1. **Verification** — the address must be a *verified* email attached to a
   Snowflake **user** in the account. Snowflake will not send to an unverified
   address.
2. **Integration allow-list** — `OVERWATCH_EMAIL`'s `ALLOWED_RECIPIENTS` must
   include the address, and the integration must be `ENABLED`.
3. **Alert bodies** — each `SYSTEM$SEND_EMAIL(...)` call in the four alerts names
   the recipient literally.

Change all three and the alerts must be `RESUME`d (they are created suspended).

## Why email can silently stop

- The previous recipient's user or email verification was removed/changed (the
  most common cause of a *sudden* stop — a verified address stops being valid).
- `OVERWATCH_EMAIL` was disabled, or its `ALLOWED_RECIPIENTS` no longer includes
  the working address.
- An alert was `SUSPEND`ed, or its warehouse (`WH_ALFA_ADMIN`) was unavailable.

Note: the alerts are `ALERT` objects, **not** tasks and **not** part of the
numbered migrations — task-graph or migration changes do not affect them.

## Diagnose

```sql
-- Integration enabled? Who is allowed to receive?
DESC NOTIFICATION INTEGRATION OVERWATCH_EMAIL;          -- check ENABLED + ALLOWED_RECIPIENTS

-- All four alerts present and started (not suspended)?
SHOW ALERTS IN SCHEMA DBA_MAINT_DB.OVERWATCH;

-- Did they run and fail to send recently?
SELECT NAME, SCHEDULED_TIME, STATE, SQL_ERROR_MESSAGE
  FROM TABLE(INFORMATION_SCHEMA.ALERT_HISTORY(
         SCHEDULED_TIME_RANGE_START => DATEADD('day', -3, CURRENT_TIMESTAMP())))
 WHERE NAME IN ('NATIVE_ALERT_NEW_EVENTS', 'NATIVE_ALERT_STALE_FACTS',
                'NATIVE_ALERT_SCAN_HEARTBEAT', 'NATIVE_ALERT_DELIVERY_FAILING')
 ORDER BY SCHEDULED_TIME DESC;
```

## Pre-flight before RESUME

Any row these return will email **hourly** until it is fixed, so they must come back
empty before you resume the dead-man alerts:

```sql
-- (1) Sources that would email now
SELECT SOURCE_NAME, LAST_LOAD_TS,
       ROUND(DATEDIFF('minute', LAST_LOAD_TS, CURRENT_TIMESTAMP()) / 60.0, 1) AS HOURS_BEHIND,
       IFF(SOURCE_NAME LIKE '%DAILY%' OR SOURCE_NAME LIKE '%METERING%', 30.0, 3.0) AS LIMIT_H
  FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
 WHERE LAST_LOAD_TS IS NULL
    OR DATEDIFF('minute', LAST_LOAD_TS, CURRENT_TIMESTAMP()) / 60.0
       > IFF(SOURCE_NAME LIKE '%DAILY%' OR SOURCE_NAME LIKE '%METERING%', 30.0, 3.0)
 ORDER BY 3 DESC;

-- (2) Chronic loader-failure / delivery-failure types in the last 24h
SELECT ERROR_TYPE, PAGE, COUNT(*) AS N_24H, MAX(LOGGED_AT) AS LAST_AT,
       ANY_VALUE(LEFT(ERROR_MESSAGE, 160)) AS SAMPLE
  FROM DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
 WHERE LOGGED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
   AND (ERROR_TYPE IN ('mart_load_failed', 'fact_load_failed', 'extract_load_failed',
                       'cloud_svc_mart_failed', 'object_cost_load_failed')
        OR PAGE = 'NotifyWebhook')
 GROUP BY 1, 2 ORDER BY 3 DESC;

-- (3) Heartbeat ground truth
SELECT NAME, STATE, SCHEDULED_TIME, COMPLETED_TIME, LEFT(ERROR_MESSAGE, 160)
  FROM TABLE(DBA_MAINT_DB.INFORMATION_SCHEMA.TASK_HISTORY(
         SCHEDULED_TIME_RANGE_START => DATEADD('hour', -3, CURRENT_TIMESTAMP()),
         RESULT_LIMIT => 10000))
 WHERE SCHEMA_NAME = 'OVERWATCH' AND NAME IN ('TASK_ALERT_SCAN', 'TASK_ALERT_NOTIFY')
 ORDER BY SCHEDULED_TIME DESC;
```

## Change the recipient

### Step 1 — verify the address (attach to a Snowflake user)

```sql
ALTER USER <username> SET EMAIL = '<recipient>';   -- or Snowsight: Admin > Users > [user] > Email
-- Snowflake emails a verification link to <recipient>; it must be clicked
-- before delivery works. This step is usually the real fix for a sudden stop.
```

### Step 2 — allow it on the integration

```sql
ALTER NOTIFICATION INTEGRATION OVERWATCH_EMAIL
      SET ALLOWED_RECIPIENTS = ('<recipient>');   -- comma-separate to CC several
ALTER NOTIFICATION INTEGRATION OVERWATCH_EMAIL SET ENABLED = TRUE;
```

### Step 3 — point all four alerts at it

Re-run [`snowflake/native_alert_templates.sql`](../snowflake/native_alert_templates.sql)
as **SNOW_ACCOUNTADMINS** (the app owner role, so the app can see the alerts), replacing
the recipient in **all four** `SYSTEM$SEND_EMAIL(...)` calls:

```sql
    CALL SYSTEM$SEND_EMAIL(
        'OVERWATCH_EMAIL',
        '<recipient>',             -- the destination address
        ... );
```

Then, once the pre-flight above is clean, resume all four:

```sql
ALTER ALERT DBA_MAINT_DB.OVERWATCH.NATIVE_ALERT_NEW_EVENTS       RESUME;
ALTER ALERT DBA_MAINT_DB.OVERWATCH.NATIVE_ALERT_STALE_FACTS      RESUME;
ALTER ALERT DBA_MAINT_DB.OVERWATCH.NATIVE_ALERT_SCAN_HEARTBEAT   RESUME;
ALTER ALERT DBA_MAINT_DB.OVERWATCH.NATIVE_ALERT_DELIVERY_FAILING RESUME;
```

### Step 4 — smoke test

```sql
CALL SYSTEM$SEND_EMAIL('OVERWATCH_EMAIL', '<recipient>',
     'OVERWATCH email test', 'Delivery restored.');
```

If this errors with a recipient/verification message, Step 1 has not completed
(the verification link has not been clicked yet).

## Keep the template in sync (optional)

The repo copy of `native_alert_templates.sql` ships a **placeholder** recipient
(`dba-team@example.com`) on purpose, so it is not tenant-specific. If you want a
redeploy to carry your real default, edit those lines locally (never commit them) — but that is
cosmetic: it changes nothing about live delivery, which is governed entirely by
the three requirements above.
