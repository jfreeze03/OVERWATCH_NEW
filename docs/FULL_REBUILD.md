# Full rebuild — drop the OVERWATCH objects and reinstall V001 through the repo tip

Owner ask 2026-07-12: "a full database drop instead of this incremental
build." This runbook is that, made safe.

**The one rule: never drop DBA_MAINT_DB or the OVERWATCH schema.** The
schema is SHARED with the previous app's objects (teardown.sql's safety
model). "Full rebuild" here means: drop every OVERWATCH object by name,
then run every migration in snowflake/migrations/ in order (or paste
snowflake/rebuild/02_migrations_V001_V<tip>.sql, the same chain). Same end
state as a virgin install, except for the opt-in objects the migrations
never create (step 7b).

Everything below runs in Snowsight as your deployment role, the one that
owns the objects: SNOW_ACCOUNTADMINS here (DEPLOYMENT.md §1 and §2; step
7b(a)'s integration grant names it), in a worksheet with:

    USE DATABASE DBA_MAINT_DB;
    USE SCHEMA OVERWATCH;

## 0. Decide what survives

- **Rebuildables** (facts, marts, procs, tasks, views): always dropped and
  rebuilt. That is the point.
- **Operator data** (SETTINGS, COMPANY_SCOPE, ALERT_CONFIG/EVENTS/AUDIT,
  ACTION_QUEUE, SAVINGS_LEDGER, OBJECT/WAREHOUSE change registries,
  DEPT_BUDGETS, USER_PREFS, APP_ERROR_LOG, SCHEMA_VERSION):
  - **Recommended: KEEP.** The mess lives in rebuildable objects and task
    states, not here — and the registries' frozen baselines cannot be
    rebuilt from ACCOUNT_USAGE at all.
  - Factory reset (drop these too) only if you want zero history: run the
    Section B0 clone backups FIRST, verify row counts, then Section B.
- **Opt-in objects** (the email alerts, the alert drill, the ML forecast,
  the OVERWATCH_* notification integrations and their secrets): always
  dropped, and no migration re-creates them (step 7b). Note which ones you
  have first:

      SHOW ALERTS IN SCHEMA DBA_MAINT_DB.OVERWATCH;
      SHOW TASKS IN SCHEMA DBA_MAINT_DB.OVERWATCH;   -- TASK_ALERT_DRILL, TASK_REFRESH_ML_FORECAST
      SHOW NOTIFICATION INTEGRATIONS LIKE 'OVERWATCH%';
      DESC NOTIFICATION INTEGRATION OVERWATCH_EMAIL; -- ALLOWED_ / DEFAULT_RECIPIENTS go with it
      SHOW GRANTS ON INTEGRATION OVERWATCH_WEBHOOK_TEAMS; -- and each one listed above:
                                                     -- its grants go with it
      SELECT ROUTE_ID, INTEGRATION_NAME FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES
       WHERE ENABLED;                               -- the routes live now (step 7b)

  Keep the Teams Workflows URL to hand: its secret is dropped too. Keep the
  live ROUTE_IDs as well: step 7b re-enables exactly those (the step-1
  ALERT_ROUTES clone holds them too).

## 1. Backups (even for the keep-operator-data path — they cost nothing)

Run teardown.sql "B0. Backups" (the commented CLONE block) with today's
date suffix; verify counts:

    SELECT 'SETTINGS' T, COUNT(*) FROM SETTINGS_BAK_<date>
    UNION ALL SELECT 'ALERT_CONFIG', COUNT(*) FROM ALERT_CONFIG_BAK_<date>;
    -- ...one row per clone, equal to the source counts.

There are no scheduled backups since V161 (V158's `DBA_MAINT_DB.OVERWATCH_BAK`
generations and the weekly `*_BAK_LAST` copies were retired), so these manual
clones are the only copy outside Time Travel. Clone as `CREATE TRANSIENT TABLE`
(a permanent clone of a transient table such as ALERT_EVENTS fails), and change
the fixed date suffix in `snowflake/rebuild/00_backup_operator_data.sql` to today
first: it has no `IF NOT EXISTS`, so a suffix that already exists fails with
"already exists" (use a new suffix, or drop the older clones once they are no
longer needed; after a partial run, re-run from the failing CREATE). Restore with
`INSERT OVERWRITE INTO <T> SELECT * FROM <T>_BAK_<date>` as the table-owner role
(a TRANSIENT clone cannot CLONE back into a permanent table). Replaying V158
during a rebuild still starts one backup run through its tail `EXECUTE TASK`;
V161, a few files later, waits up to about 4 minutes for that run and then drops
everything it made (if V161 still stops on it, re-run V161 once the run ends).

## 2. Teardown

Run snowflake/teardown.sql top to bottom (Section A executes; B and C stay
commented unless you chose the factory reset in step 0). Two parts of
Section B run live anyway. Three rebuildable tables are dropped, and step 3
re-creates them: APP_QUERY_TELEMETRY and ALERT_DELIVERIES (the per-route
delivery ledger; step 7b(a) says what its reset means for delivery) come
back empty, OW_SENDER_LEASE with its one seed row. And the opt-in tail at
the end of Section B drops the ML forecast model, the webhook secrets and
the OVERWATCH_* notification integrations (step 7b puts them back). The
file says to run those integration drops as ACCOUNTADMIN; if your role
cannot drop one, that statement fails and Run All stops there, so run the
rest of the file, VERIFY included, by hand. The VERIFY query at the bottom
should list ONLY operator-data tables afterward (or nothing, after a
factory reset).

## 3. Migrations, in order, one file at a time

V001 → the repo tip (every file in snowflake/migrations/, enumerated in
admin.py `_EXPECTED_MIGRATIONS`), each file fully, **stopping at the first
error** — never run past a failure (a partial apply is how task trees end
up suspended; V041 resumes its graph both before and after its first fills
now, but the rule stands for every file). Notes:

- Several migrations end with a first-fill `CALL SP_LOAD_*` / `SP_REFRESH_*` at
  the file tail (mart/fact seed) — these are the slow ones; expect a few minutes
  each on WH_ALFA_ADMIN. The mart family (V027+) and the per-table storage mart
  (V124) added more of them, so watch for the trailing `CALL` in each file rather
  than relying on a fixed list.
- If you kept operator data, SCHEMA_VERSION already holds 1..tip: the
  guards pass, IF NOT EXISTS objects recreate only what teardown dropped,
  and the version MERGEs no-op. That is the designed restore path.
- If you factory-reset, apply every migration, then restore your real values
  from the step-1 clones, SETTINGS first, as the table-owner role (V001 re-seeds
  the SETTINGS/ALERT_CONFIG/COMPANY_SCOPE defaults):
      INSERT OVERWRITE INTO SETTINGS SELECT * FROM SETTINGS_BAK_<date>; -- etc.
      (or UPDATE the handful you care about: rates, budgets, routes.)
  There is no scheduled backup to fall back on since V161: the step-1 clones are
  the only copy.

## 4. Grants

Re-run snowflake/roles.sql (new V075 objects plus ALL + FUTURE grants).

## 5. History backfill (recommended)

Run snowflake/backfill_365.sql: a year of daily facts, 90 days of the
QUERY_HISTORY-derived marts (the extract fills first — V041), platform
score inputs, and 180 days of security login/change facts (the loader's
maximum: the new-network panel serves from the fact only when it holds the
window plus a 90-day baseline). A few minutes. It suspends TASK_LOAD_HOURLY
around the extract-fed loads; each load is guarded and keeps the loader's own verdict,
so an error or a failure verdict (`MARTS WITH ERRORS`,
`extract committed: false`) shows as a `FAILED:` row. The last pane must read 0 in both
`BACKFILL_CALLS_FAILED` and `LOADER_ARMS_FAILED` (arm failures a loader
logs without failing the CALL); `FAILURES` names each one. **If the
worksheet stops before the end** (a timeout or Stop), run the
`ALTER TASK ... RESUME` and `SYSTEM$TASK_DEPENDENTS_ENABLE` statements just
above the file's final verify SELECT, or snowflake/loader_chain_check.sql
step 0, or the hourly graph stays suspended.

## 6. Validate

Run snowflake/validate.sql — every row OK. (Task monitoring is no longer
part of this script — owner decision 2026-07-12; use
snowflake/loader_chain_check.sql when you need task-state diagnosis.)

## 7. Redeploy the app

Push the current build to the stage / Streamlit-in-Snowflake as usual
(DEPLOYMENT.md). The app expects V001 through its own tip: Admin ▸
Migrations & freshness shows any drift, and validate.sql's first row checks
the full chain.

## 7b. Re-install the opt-in objects

teardown.sql (rebuild/01) dropped these, and no migration re-creates them:
the four NATIVE_ALERT_* email alerts; TASK_ALERT_DRILL; the ML forecast
(OVERWATCH_SPEND_FORECAST, SP_REFRESH_ML_FORECAST, TASK_REFRESH_ML_FORECAST,
FORECAST_ML_DAILY); the OVERWATCH_EMAIL, OVERWATCH_WEBHOOK_TEAMS (and Slack
/ PagerDuty / FinOps recipe) notification integrations; and the
OVERWATCH_TEAMS_URL / OVERWATCH_WEBHOOK_URL secrets. Until they are back,
Alerts ▸ Native delivery reads the email path as not installed, a dead
scan or notifier sends no email, the monthly drill stops, and
`FORECAST_ENGINE = ml_forecast` falls back to the seasonal engine. Put back
what step 0 listed:

(a) **Teams delivery** (as ACCOUNTADMIN): snowflake/webhook_delivery.sql's
    first-time setup — the commented `CREATE SECRET IF NOT EXISTS` with the
    URL pasted in Snowsight only, then open its GATE and run it. The
    re-created integration carries no grants: re-apply each one step 0's
    SHOW GRANTS listed, at least the one the notifier needs
    (SP_NOTIFY_WEBHOOK runs as its owner, the deployment role, which is
    SNOW_ACCOUNTADMINS here):

        GRANT USAGE ON INTEGRATION OVERWATCH_WEBHOOK_TEAMS TO ROLE SNOW_ACCOUNTADMINS;

    A PagerDuty or FinOps integration step 0 listed comes back the same way,
    from that file's recipe, but skip the recipe's route INSERT: the kept
    route still exists, and the INSERT would add a second one.

    The setup's own route INSERT adds nothing when a route already names the
    integration. If you kept operator data, replaying V070 in step 3
    DISABLED every enabled route whose integration was gone. Re-enable
    exactly the routes step 0 recorded as live, never every row that names
    the integration: a duplicate route disabled on purpose would come back,
    and every alert, digest and escalation would post once per duplicate.

        UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES SET ENABLED = TRUE
         WHERE ROUTE_ID IN ('<each ROUTE_ID step 0 listed>');

    The teardown also emptied ALERT_DELIVERIES, so the first notifier run
    after this re-posts every OPEN event of the last 24 hours (7 days for a
    CRITICAL) that was already delivered. If that burst is unwanted, ACK or
    resolve the stale events before the UPDATE.

    Post one test card with the file's rotation-runbook CALL, then prove
    delivery through the notifier itself: that CALL runs with ACCOUNTADMIN's
    privileges, not the proc owner's, so it cannot catch a missing grant.
    After the next TASK_ALERT_NOTIFY run with an eligible event,
    ALERT_DELIVERIES holds new rows for the route and APP_ERROR_LOG has no
    new `route_send_failed` row (PAGE 'NotifyWebhook').

(b) **Email** (as ACCOUNTADMIN): re-create OVERWATCH_EMAIL from the PREREQS
    block of snowflake/native_alert_templates.sql (real ALLOWED_RECIPIENTS,
    USAGE to SNOW_ACCOUNTADMINS) and set its DEFAULT_RECIPIENTS for the V164
    escalation email (docs/EMAIL_RECIPIENT_RUNBOOK.md, step 2). Then, as
    SNOW_ACCOUNTADMINS, re-run native_alert_templates.sql with the real
    recipient. The alerts come back suspended: resume all four only after
    step 8 passes and that runbook's pre-flight is clean.
(c) **Drill and ML forecast**: re-run snowflake/alert_drill.sql (it resumes
    its own task) and snowflake/ml_forecast_option.sql (it retrains and
    rewrites FORECAST_ML_DAILY; its weekly task is created suspended, so
    resume it if it ran before).

## 8. Prove the chain ticks

An hour after step 7, run snowflake/loader_chain_check.sql: every task
'started', hourly rows landing, freshness HOURS_BEHIND < 2 for hourly
sources. The fleet board after 24h is the final word.
