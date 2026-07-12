# Full rebuild — drop the OVERWATCH objects and reinstall V001..V041

Owner ask 2026-07-12: "a full database drop instead of this incremental
build." This runbook is that, made safe.

**The one rule: never drop DBA_MAINT_DB or the OVERWATCH schema.** The
schema is SHARED with the previous app's objects (teardown.sql's safety
model). "Full rebuild" here means: drop every OVERWATCH object by name,
then run all 41 migrations in order. Same end state as a virgin install.

Everything below runs in Snowsight as your deployment role (the one that
owns the objects — see DEPLOYMENT.md), in a worksheet with:

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

## 1. Backups (even for the keep-operator-data path — they cost nothing)

Run teardown.sql "B0. Backups" (the commented CLONE block) with today's
date suffix; verify counts:

    SELECT 'SETTINGS' T, COUNT(*) FROM SETTINGS_BAK_<date>
    UNION ALL SELECT 'ALERT_CONFIG', COUNT(*) FROM ALERT_CONFIG_BAK_<date>;
    -- ...one row per clone, equal to the source counts.

## 2. Teardown

Run snowflake/teardown.sql top to bottom (Section A executes; B and C stay
commented unless you chose the factory reset in step 0). The VERIFY query
at the bottom should list ONLY operator-data tables afterward (or nothing,
after a factory reset).

## 3. Migrations, in order, one file at a time

V001 → V041, each file fully, **stopping at the first error** — never run
past a failure (a partial apply is how task trees end up suspended; V041
resumes its graph both before and after its first fills now, but the rule
stands for every file). Notes:

- V027, V029, V030, V031, V041 end with first-fill CALLs — the slow ones;
  expect a few minutes each on WH_ALFA_OVERWATCH.
- If you kept operator data, SCHEMA_VERSION already holds 1..41: the
  guards pass, IF NOT EXISTS objects recreate only what teardown dropped,
  and the version MERGEs no-op. That is the designed restore path.
- If you factory-reset, V001 reseeds SETTINGS/ALERT_CONFIG/COMPANY_SCOPE
  defaults; restore your real values from the clones afterward:
      INSERT INTO SETTINGS SELECT * FROM SETTINGS_BAK_<date>; -- etc.
      (or UPDATE the handful you care about: rates, budgets, routes.)

## 4. Grants

Re-run snowflake/roles.sql (new V041 objects; ALL + FUTURE grants).

## 5. History backfill (recommended)

Run snowflake/backfill_365.sql: a year of daily facts, 90 days of the
QUERY_HISTORY-derived marts (the extract fills first — V041), platform
score inputs. A few minutes.

## 6. Validate

Run snowflake/validate.sql — every row OK. (Task monitoring is no longer
part of this script — owner decision 2026-07-12; use
snowflake/loader_chain_check.sql when you need task-state diagnosis.)

## 7. Redeploy the app

Push the current build to the stage / Streamlit-in-Snowflake as usual
(DEPLOYMENT.md). App v4.36.1 expects exactly V001..V041.

## 8. Prove the chain ticks

An hour after step 7, run snowflake/loader_chain_check.sql: every task
'started', hourly rows landing, freshness HOURS_BEHIND < 2 for hourly
sources. The fleet board after 24h is the final word.
