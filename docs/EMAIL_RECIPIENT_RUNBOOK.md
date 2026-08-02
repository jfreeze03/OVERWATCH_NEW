# OVERWATCH Email Recipient Runbook

How to change (or restore) the recipient of OVERWATCH's **email** alerts.

Throughout, `<recipient>` means the destination address you want, e.g.
`someone@your-domain.com`. Run everything as **ACCOUNTADMIN** in Snowsight.

## What actually sends these emails

OVERWATCH's primary alert channel is the **Teams webhook** (`OVERWATCH_WEBHOOK`
notification integration). Email is a **separate, opt-in** path defined in
[`snowflake/native_alert_templates.sql`](../snowflake/native_alert_templates.sql):
two native Snowflake `ALERT` objects that call `SYSTEM$SEND_EMAIL` through an
email notification integration named **`OVERWATCH_EMAIL`**.

| Alert | Fires | Subject |
|---|---|---|
| `NATIVE_ALERT_NEW_EVENTS` | new OPEN critical/high alert events (every 30 min) | `OVERWATCH: new critical/high alerts` |
| `NATIVE_ALERT_STALE_FACTS` | `FACT_QUERY_HOURLY` unloaded > 3h (hourly) | `OVERWATCH: telemetry loads are stale` |

**There is no app-side email setting.** Nothing in the Streamlit app, `SETTINGS`,
or a numbered migration holds the recipient. (The `EMAIL` column on
`FACT_AI_USAGE_DAILY` is unrelated — it is Cortex-usage attribution data.)

## The complete footprint — the recipient lives in exactly 3 requirements

1. **Verification** — the address must be a *verified* email attached to a
   Snowflake **user** in the account. Snowflake will not send to an unverified
   address.
2. **Integration allow-list** — `OVERWATCH_EMAIL`'s `ALLOWED_RECIPIENTS` must
   include the address, and the integration must be `ENABLED`.
3. **Alert bodies** — each `SYSTEM$SEND_EMAIL(...)` call in the two alerts names
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

-- Both alerts present and started (not suspended)?
SHOW ALERTS IN SCHEMA DBA_MAINT_DB.OVERWATCH;

-- Did they run and fail to send recently?
SELECT NAME, SCHEDULED_TIME, STATE, ERROR
  FROM TABLE(INFORMATION_SCHEMA.ALERT_HISTORY(
         SCHEDULED_TIME_RANGE_START => DATEADD('day', -3, CURRENT_TIMESTAMP())))
 WHERE NAME IN ('NATIVE_ALERT_NEW_EVENTS','NATIVE_ALERT_STALE_FACTS')
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

### Step 3 — point both alerts at it

Re-run [`snowflake/native_alert_templates.sql`](../snowflake/native_alert_templates.sql),
replacing the recipient in **both** `SYSTEM$SEND_EMAIL(...)` calls:

```sql
    CALL SYSTEM$SEND_EMAIL(
        'OVERWATCH_EMAIL',
        '<recipient>',             -- the destination address
        ... );
```

Then resume them:

```sql
ALTER ALERT DBA_MAINT_DB.OVERWATCH.NATIVE_ALERT_NEW_EVENTS  RESUME;
ALTER ALERT DBA_MAINT_DB.OVERWATCH.NATIVE_ALERT_STALE_FACTS RESUME;
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
redeploy to carry your real default, edit those three lines locally — but that is
cosmetic: it changes nothing about live delivery, which is governed entirely by
the three requirements above.
