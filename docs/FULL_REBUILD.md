# Full rebuild — drop the OVERWATCH objects and reinstall V001 through the repo tip

Owner ask 2026-07-12: "a full database drop instead of this incremental
build." This runbook is that, made safe.

**The one rule: never drop DBA_MAINT_DB or the OVERWATCH schema.** The
schema is SHARED with the previous app's objects (teardown.sql's safety
model). "Full rebuild" here means: drop every OVERWATCH object by name,
then run every migration in snowflake/migrations/ in order (or paste
snowflake/rebuild/02_migrations_V001_V<tip>.sql, the same chain behind a
two-line role shim, step 3). Same end state as a virgin install, except for
the opt-in objects the migrations never create (step 7b) and the kept
operator data the replay rewrites (step 3b).

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
- **Opt-in objects**: the alert drill and the ML forecast are always
  dropped, and no migration re-creates them (step 7b). The account-level
  **delivery objects** (the four NATIVE_ALERT_* email alerts, the OVERWATCH_*
  notification integrations and their secrets) are KEPT: teardown only
  suspends the alerts, and their drops sit behind its DELIVERY GATE, which
  you open only for a true uninstall (owner decision 2026-10-02: the email
  default must never be overwritten again). Note what you have first:

      SHOW ALERTS IN SCHEMA DBA_MAINT_DB.OVERWATCH;
      SHOW TASKS IN SCHEMA DBA_MAINT_DB.OVERWATCH;   -- TASK_ALERT_DRILL, TASK_REFRESH_ML_FORECAST
      SHOW NOTIFICATION INTEGRATIONS LIKE 'OVERWATCH%';
      DESC NOTIFICATION INTEGRATION OVERWATCH_EMAIL; -- record ALLOWED_ / DEFAULT_RECIPIENTS
                                                     -- (they go with it if the gate is opened)
      SHOW GRANTS ON INTEGRATION OVERWATCH_WEBHOOK_TEAMS; -- and each one listed above:
                                                     -- its grants go with it (gate opened)
      SELECT ROUTE_ID, INTEGRATION_NAME FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES
       WHERE ENABLED;                               -- the routes live now (step 7b)

  If you will open the gate, keep the Teams Workflows URL to hand: its
  secret is dropped too. Keep the live ROUTE_IDs either way: step 7b
  re-enables exactly those (the step-1 ALERT_ROUTES clone holds them too).
- **WH_ALFA_ADMIN settings** (shared with the app and every loader): the
  replay changes two of them (step 3), and step 3b puts back what you record
  now:

      SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN WAREHOUSE WH_ALFA_ADMIN;
                                                     -- note the value and level
      SHOW WAREHOUSES LIKE 'WH_ALFA_ADMIN';          -- note resource_monitor

  V002 sets the timeout to 300. It also swaps whatever monitor is attached
  for OVERWATCH_RM, and V045 then sets RESOURCE_MONITOR = NULL, so the replay
  detaches any monitor attached now. Expect `null` (owner decision: no
  resource monitor on WH_ALFA_ADMIN). OVERWATCH_RM is a leftover of an
  aborted run: let it go. If it names any other monitor, note the name and
  ask the owner before step 3 whether it goes back; step 3b re-attaches it
  only on a yes.

- **Rebuild at the tip the account is on.** If the repo has migrations the
  account has not applied, apply them the normal way first (DEPLOYMENT.md
  §1), then start here: step 3b restores the step-1 clones over the replayed
  tables, which only works when both have the same columns.

## 1. Backups (the keep-operator-data path too: step 3b restores from them)

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
the end of Section B drops the ML forecast model (step 7b puts it back).
The DELIVERY GATE after it returns `kept: ...` and drops nothing: the
OVERWATCH_* notification integrations, the webhook secrets and the four
NATIVE_ALERT_* email alerts survive (the PREFLIGHT only suspended the
alerts; step 7b resumes them). Only for a true uninstall set
`drop_delivery_objects` TRUE in your Snowsight copy and run that block as
ACCOUNTADMIN; if your role cannot drop one of them, the block fails there,
so run the rest of the file, VERIFY included, by hand. The VERIFY query at
the bottom should list ONLY operator-data tables afterward (or nothing,
after a factory reset).

## 3. Migrations, in order, one file at a time

V001 → the repo tip (every file in snowflake/migrations/, enumerated in
admin.py `_EXPECTED_MIGRATIONS`), each file fully, **stopping at the first
error** — never run past a failure (a partial apply is how task trees end
up suspended; V041 resumes its graph both before and after its first fills
now, but the rule stands for every file). Notes:

- **Before V006, create the two retired roles.** V006, V007 and V008 grant
  to OVERWATCH_MONITOR and OVERWATCH_OPERATOR, the roles retired on
  2026-07-13 (owner decision). No migration creates them, so V006 stops on
  any current or fresh account without them; step 4's roles.sql drops both
  again. rebuild/02 runs these two lines first (its generated replay shim);
  one file at a time, run them yourself:

      CREATE ROLE IF NOT EXISTS OVERWATCH_MONITOR;
      CREATE ROLE IF NOT EXISTS OVERWATCH_OPERATOR;

  They need the CREATE ROLE privilege. If your role lacks it, create them as
  a role that has it, and drop them with that role before step 4 (once step
  3b is done). roles.sql opens with `DROP ROLE IF EXISTS` for both: that then
  finds nothing to drop, where a role that cannot drop them would stop
  roles.sql before its first grant. roles.sql also needs an ACCOUNTADMIN-tier
  role (IMPORTED PRIVILEGES), which a role without CREATE ROLE is not, so run
  step 4 as a role that has both.
- **V002 changes WH_ALFA_ADMIN.** It sets STATEMENT_TIMEOUT_IN_SECONDS back
  to 300 (step 3b restores the value step 0 recorded), creates the
  OVERWATCH_RM resource monitor (30 credits a month, SUSPEND at 100%) and
  attaches it in place of any monitor step 0 found; V045 detaches and drops
  it again (owner decision: no resource monitor, no hard cap on
  WH_ALFA_ADMIN; step 3b re-attaches a step-0 monitor only on the owner's
  yes). If the run stops anywhere between V002 and V045, detach it before
  you investigate:

      ALTER WAREHOUSE WH_ALFA_ADMIN SET RESOURCE_MONITOR = NULL;
      DROP RESOURCE MONITOR IF EXISTS OVERWATCH_RM;

  If a first-fill CALL is cancelled at 300 s, set the step-0 timeout back
  (step 3b's ALTER WAREHOUSE) and resume from that file.
- Several migrations end with a first-fill `CALL SP_LOAD_*` / `SP_REFRESH_*` at
  the file tail (mart/fact seed) — these are the slow ones; expect a few minutes
  each on WH_ALFA_ADMIN. The mart family (V027+) and the per-table storage mart
  (V124) added more of them, so watch for the trailing `CALL` in each file rather
  than relying on a fixed list.
- If you kept operator data, SCHEMA_VERSION already holds 1..tip, so every
  guard passes and the whole chain runs again against the kept tables, its
  one-time statements included. IF NOT EXISTS objects recreate only what
  teardown dropped and the version MERGEs no-op, but V034 sets every 'ALL'
  route's COMPANY_FILTER to 'ALFA'; V019/V020/V028 reset SEC_CRED_EXPIRY
  (enabled, threshold 10); V043/V045 re-enable PIPE_TASK_FAILURES; V091 and
  V157 turn AUTO_CLEAR_ENABLED on for five rules; V001 resets COMPANY_SCOPE
  notes; V070 disables every enabled route whose integration is gone (none,
  unless you opened the teardown's delivery gate); V118 and V145 re-run their one-time SAVINGS_LEDGER corrections
  (apply-time EXECUTE IMMEDIATE blocks, guarded so a re-run normally changes
  nothing); and the seed MERGEs put back the rules, routes, settings, scope and
  department rows you had deleted (V011 even re-adds two retired rules, which
  V034 and V157 delete again). Step 3b puts your values back.
- If you factory-reset, apply every migration, then restore your real values
  from the step-1 clones, SETTINGS first, as the table-owner role (V001 re-seeds
  the SETTINGS/ALERT_CONFIG/COMPANY_SCOPE defaults):
      INSERT OVERWRITE INTO SETTINGS SELECT * FROM SETTINGS_BAK_<date>; -- etc.
      (or UPDATE the handful you care about: rates, budgets, routes.)
  There is no scheduled backup to fall back on since V161: the step-1 clones are
  the only copy.

## 3b. Put back what the replay rewrote

After the last migration and before step 4, as the table-owner role
(SNOW_ACCOUNTADMINS here), with step 1's date suffix. On the
keep-operator-data path, restore the five config tables and SAVINGS_LEDGER,
the kept tables the replay rewrote (step 3's notes say how), from the step-1
clones. Restoring SAVINGS_LEDGER also undoes the settle V153's tail CALL ran
during the replay; TASK_LEDGER_AUTOBOOK settles those windows again on its
next daily run. The replay's own scans
raised events under the config it had reset: V045 switches
PIPE_TASK_FAILURES on and then scans, and V020/V028 re-enable
SEC_CRED_EXPIRY at a 10-day threshold. Nothing closes those events once
your values are back: the hourly scan auto-clears only enabled rules, and
the notifier posts an OPEN event whether or not its rule is enabled. So
first keep two lists, in the same worksheet: the rules the replay
re-seeded after you had deleted them, and every rule whose row the replay
changed at all (re-seeded, switched on, threshold or auto-clear reset):

    CREATE TEMPORARY TABLE OW_REPLAY_ONLY_RULES AS
      SELECT RULE_ID FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
      MINUS SELECT RULE_ID FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG_BAK_<date>;
    CREATE TEMPORARY TABLE OW_REPLAY_TOUCHED_RULES AS
      SELECT RULE_ID FROM (SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
                           MINUS SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG_BAK_<date>);
    INSERT OVERWRITE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS       SELECT * FROM DBA_MAINT_DB.OVERWATCH.SETTINGS_BAK_<date>;
    INSERT OVERWRITE INTO DBA_MAINT_DB.OVERWATCH.COMPANY_SCOPE  SELECT * FROM DBA_MAINT_DB.OVERWATCH.COMPANY_SCOPE_BAK_<date>;
    INSERT OVERWRITE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG   SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG_BAK_<date>;
    INSERT OVERWRITE INTO DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES   SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES_BAK_<date>;
    INSERT OVERWRITE INTO DBA_MAINT_DB.OVERWATCH.DEPARTMENT_MAP SELECT * FROM DBA_MAINT_DB.OVERWATCH.DEPARTMENT_MAP_BAK_<date>;
    INSERT OVERWRITE INTO DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER SELECT * FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER_BAK_<date>;

Then close, as EXPECTED, every event of a rule only the replay re-seeded,
and every event raised since the step-1 clone (so by the replay) whose rule
is off or gone in the restored ALERT_CONFIG:

    UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
       SET STATUS = 'RESOLVED', RESOLUTION_KIND = 'EXPECTED', RESOLVED_AT = CURRENT_TIMESTAMP()
     WHERE STATUS IN ('OPEN', 'ACK', 'SNOOZED')
       AND RULE_ID IN (SELECT RULE_ID FROM OW_REPLAY_ONLY_RULES);
    UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
       SET STATUS = 'RESOLVED', RESOLUTION_KIND = 'EXPECTED', RESOLVED_AT = CURRENT_TIMESTAMP()
     WHERE STATUS IN ('OPEN', 'ACK', 'SNOOZED')
       AND RULE_ID NOT IN (SELECT RULE_ID FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED)
       AND EVENT_ID NOT IN (SELECT EVENT_ID FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS_BAK_<date>);

A rule that is on again keeps its events, because your own threshold may
raise the same ones. The replay may still have raised some under a
threshold it had reset. List the events it raised for the rules it
changed, and ACK each one your values would not have raised before step 7b
re-enables the routes (the notifier posts only OPEN events):

    SELECT EVENT_ID, RULE_ID, RAISED_AT, TITLE FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
     WHERE STATUS = 'OPEN'
       AND RULE_ID IN (SELECT RULE_ID FROM OW_REPLAY_TOUCHED_RULES)
       AND EVENT_ID NOT IN (SELECT EVENT_ID FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS_BAK_<date>);
    UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
       SET STATUS = 'ACK', ACK_BY = CURRENT_USER(), ACK_AT = CURRENT_TIMESTAMP()
     WHERE STATUS = 'OPEN' AND EVENT_ID IN ('<each EVENT_ID to keep quiet>');

The restored ALERT_ROUTES has each route's pre-teardown ENABLED flag. The
teardown keeps the OVERWATCH_* notification integrations unless you opened
its delivery gate, but any integration that is gone (gate opened, or never
installed) must not keep an enabled route. Keep every route whose
integration is gone switched off until step 7b brings it back (run the two
statements together; the UPDATE reads the SHOW's result):

    SHOW NOTIFICATION INTEGRATIONS;
    UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES SET ENABLED = FALSE
     WHERE ENABLED AND UPPER(INTEGRATION_NAME) NOT IN
           (SELECT UPPER("name") FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())));

On every path, put back the warehouse settings step 0 recorded: the timeout
(V002 set it to 300), and the resource monitor only on the owner's yes in
step 0. The replay detached it; skip that line when step 0 showed `null` or
OVERWATCH_RM (owner decision: no monitor on WH_ALFA_ADMIN):

    ALTER WAREHOUSE WH_ALFA_ADMIN SET STATEMENT_TIMEOUT_IN_SECONDS = <step-0 value>;
    ALTER WAREHOUSE WH_ALFA_ADMIN SET RESOURCE_MONITOR = <step-0 monitor>;  -- owner's yes only
    SHOW WAREHOUSES LIKE 'WH_ALFA_ADMIN';            -- resource_monitor: null, or that monitor

(If step 0 showed no warehouse-level value, run `ALTER WAREHOUSE
WH_ALFA_ADMIN UNSET STATEMENT_TIMEOUT_IN_SECONDS` instead.)

If step 3 created the two retired roles with a role other than your
deployment role, drop them with it now, before step 4:
`DROP ROLE IF EXISTS OVERWATCH_MONITOR;` and
`DROP ROLE IF EXISTS OVERWATCH_OPERATOR;`.

## 4. Grants

Re-run snowflake/roles.sql (new V075 objects plus ALL + FUTURE grants).

## 5. History backfill (recommended)

Run snowflake/backfill_365.sql: a year of daily facts, 90 days of the
QUERY_HISTORY-derived marts (the extract fills first — V041), platform
score inputs, and 180 days of security login/change facts (the loader's
maximum: the new-network panel serves from the fact only when it holds the
window plus a 90-day baseline). It also fills the cloud-services statement
mart (MART_CLOUD_SVC_DAILY) for the days before its first load, back to 364
days (Central days; it runs before the suspend window, since it reads
QUERY_HISTORY, not the extract). That is the heaviest arm (a year of
QUERY_HISTORY): if it hits the statement timeout nothing is committed, so
narrow -364 and re-run it. The rest takes a few minutes. It suspends TASK_LOAD_HOURLY
around the extract-fed loads; each load is guarded and keeps the loader's own verdict,
so an error or a failure verdict (`MARTS WITH ERRORS`,
`extract committed: false`) shows as a `FAILED:` row. The last pane must read 0 in both
`BACKFILL_CALLS_FAILED` and `LOADER_ARMS_FAILED` (arm failures a loader
logs without failing the CALL); `FAILURES` names each one. **If the
worksheet stops before the end** (a timeout or Stop), run the
`ALTER TASK ... RESUME` and `SYSTEM$TASK_DEPENDENTS_ENABLE` statements just
above the file's final verify SELECT, or snowflake/loader_chain_check.sql
step 0, or the hourly graph stays suspended.

After a rebuild the object-cost ledger (FACT_OBJECT_COST_DAILY) holds only
14 days: the commented `SP_LOAD_OBJECT_COST(365)` block after the RESUME pair
reloads a year. Uncomment and run it once, off-peak, on a warehouse whose
STATEMENT_TIMEOUT_IN_SECONDS allows it (a timeout rolls the reload back and
the previous fill stays).

**Then run the V167 AI reload-then-prune (required, even if you skip the
backfill)**, right after 04 (or here, if you skip it) and outside 06:40-07:30
Central: the block below is the owner-repair file's V167 step 1, byte for
byte. Why: the 02 replay runs V078's top-level
`SP_LOAD_MARTS_V27('DAILY', 365)` on the pre-V167 loader, which keyed
Cortex Code days on USAGE_TIME's own stored offset, so FACT_AI_USAGE_DAILY
starts with a year of offset-keyed Code rows. The backfill's DAILY 365 runs
V167's loader, which re-keys days to Central with a MERGE and no delete: a
(stored-offset day, user) row whose only usage was the previous Central
evening never matches a Central key and survives beside the Central row, so
Cortex Code totals (Chargeback & AI, AI budgets, the runaway arm) read high
across the year. The block reloads DAILY 365 once more, then deletes only
the Code rows older than its own start inside the range it re-keyed, and only
when its own `ai_code` arm loaded. Expect `ok: MARTS OK (DAILY, 365d): ... |
re-keyed Code rows N from <day>, pruned stale offset-keyed rows M`. A
`FAILED (nothing pruned): ...` row means the AI arm did not load (its
`mart_load_failed` row in APP_ERROR_LOG says why): fix it and re-run the
block. The block does not catch a raised error, and re-running it is safe.

```sql
ALTER SESSION SET TIMEZONE = 'America/Chicago';
EXECUTE IMMEDIATE $$
DECLARE
    t0 TIMESTAMP_NTZ;
    rv VARCHAR;
    touched NUMBER DEFAULT 0;
    lo DATE;
    pruned NUMBER DEFAULT 0;
BEGIN
    t0 := CURRENT_TIMESTAMP()::TIMESTAMP_NTZ;
    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('DAILY', 365);
    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));
    -- prune only when THIS reload's ai_code arm loaded (its token is in the verdict); a failed required posture
    -- arm still reads 'MARTS WITH ERRORS' in the returned verdict -- re-run the block once it is fixed
    IF (rv IS NULL OR NOT ARRAY_CONTAINS('ai_code'::VARIANT, SPLIT(:rv, ' '))) THEN
        RETURN 'FAILED (nothing pruned): SP_LOAD_MARTS_V27(DAILY, 365) -> ' || COALESCE(rv, 'no verdict returned');
    END IF;
    SELECT COUNT(*), MIN(DAY) INTO :touched, :lo
    FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY
    WHERE SOURCE IN ('Snowsight', 'CLI') AND LOAD_TS >= :t0;
    IF (touched > 0) THEN
        DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY
         WHERE SOURCE IN ('Snowsight', 'CLI') AND DAY > :lo AND LOAD_TS < :t0;
        pruned := SQLROWCOUNT;
    END IF;
    RETURN 'ok: ' || rv || ' | re-keyed Code rows ' || touched || ' from ' || COALESCE(TO_VARCHAR(lo), 'n/a')
           || ', pruned stale offset-keyed rows ' || pruned;
END;
$$;
```

The long-window AI and repeated-pattern reads gate on
SOURCE_FRESHNESS_STATE.COVERAGE_FROM (V167), which fills on the loaders'
first runs. The backfill's (and the block's) `SP_LOAD_MARTS_V27('DAILY', 365)`
stamps the AI reach; the repeated-pattern panel reads past 90 days only after a
`CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_PATTERN_COST(364);`. The all-source Unit
costs "AI spend" (Code + Functions) floors that stamp at 2026-01-05, the
canonical Functions view's first day (mart27_sql.AI_FUNCTIONS_VIEW_FROM), on
any install: a window that starts earlier keeps the first-use test (it
answers only when the fact's first day, any source, is that old). A rebuild
also loses the Cortex Functions history before 2026-01-05 (it came from a
frozen view no loader reads), so unless its Cortex Code rows reach back that
far, a 365-day "AI spend" falls back to its labelled Functions-only read
until about 2027-01-04, and a Current-year one until 2027-01-01, by design.

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
TASK_ALERT_DRILL and the ML forecast (OVERWATCH_SPEND_FORECAST,
SP_REFRESH_ML_FORECAST, TASK_REFRESH_ML_FORECAST, FORECAST_ML_DAILY). It
KEPT the account-level delivery objects unless you opened its DELIVERY
GATE: the four NATIVE_ALERT_* email alerts (suspended), the OVERWATCH_EMAIL,
OVERWATCH_WEBHOOK_TEAMS (and Slack / PagerDuty / FinOps recipe) notification
integrations, and the OVERWATCH_TEAMS_URL / OVERWATCH_WEBHOOK_URL secrets.
Until the alerts are resumed (or, gate opened, re-created), Alerts ▸ Native
delivery reads the email path as suspended or not installed and a dead scan
or notifier sends no email; until the drill and forecast are back, the
monthly drill stops and `FORECAST_ENGINE = ml_forecast` falls back to the
seasonal engine. Put back what step 0 listed (`SHOW NOTIFICATION
INTEGRATIONS LIKE 'OVERWATCH%'` shows which delivery objects are gone):

(a) **Teams delivery** (as ACCOUNTADMIN). Integration kept (the default):
    nothing to re-create — only the route re-enable and the ALERT_DELIVERIES
    note below apply. Gate opened: snowflake/webhook_delivery.sql's
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
    integration. If you kept operator data, replaying V070 in step 3 (and
    step 3b after the restore) DISABLED every enabled route whose integration
    was gone. Re-enable
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

(b) **Email**. Integration and alerts kept (the default): check that
    `DESC NOTIFICATION INTEGRATION OVERWATCH_EMAIL` still shows ENABLED true
    and DEFAULT_RECIPIENTS set (the V164 escalation email goes only there),
    then resume the four alerts (below). Gate opened (as ACCOUNTADMIN):
    re-create OVERWATCH_EMAIL from the PREREQS block of
    snowflake/native_alert_templates.sql, with ALLOWED_RECIPIENTS naming EVERY
    address step 0's DESC listed (SET replaces the whole list, so a shorter
    list drops a recipient) and DEFAULT_RECIPIENTS set as
    docs/EMAIL_RECIPIENT_RUNBOOK.md requirement 4 says (without it every V164
    escalation email fails with `escalation_email_failed`), plus USAGE to
    SNOW_ACCOUNTADMINS; then, as SNOW_ACCOUNTADMINS, re-run
    native_alert_templates.sql with the real recipient in all four
    SYSTEM$SEND_EMAIL calls. Either way the alerts are suspended: resume
    all four only after step 8 passes and that runbook's pre-flight is clean.
(c) **Drill and ML forecast**: re-run snowflake/alert_drill.sql (it resumes
    its own task) and snowflake/ml_forecast_option.sql (it retrains and
    rewrites FORECAST_ML_DAILY; its weekly task is created suspended, so
    resume it if it ran before).

## 8. Prove the chain ticks

An hour after step 7, run snowflake/loader_chain_check.sql: every task
'started', hourly rows landing, freshness HOURS_BEHIND < 2 for hourly
sources. The fleet board after 24h is the final word.
