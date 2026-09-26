# Full rebuild — drop the OVERWATCH objects and reinstall V001..V124

Owner ask 2026-07-12: "a full database drop instead of this incremental
build." This runbook is that, made safe.

**The one rule: never drop DBA_MAINT_DB or the OVERWATCH schema.** The
schema is SHARED with the previous app's objects (teardown.sql's safety
model). "Full rebuild" here means: drop every OVERWATCH object by name,
then run all 124 migrations in order. Same end state as a virgin install.

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

Since V158 the daily backup task also keeps dated generations of the 25
operator tables in their own TRANSIENT schema, `DBA_MAINT_DB.OVERWATCH_BAK`
(`<T>_OWBAK_D<yyyymmdd>`, 14 daily + 8 Sunday-weekly, row counts in
OPERATOR_BACKUP_LOG). Teardown never touches that schema, so the generations
are a second copy that survives this whole procedure. Keep the manual
`_BAK_<date>` token for the clones above: the daily prune matches only
`_OWBAK_` names, so it can never drop them. Restore from either with
`INSERT OVERWRITE INTO <T> SELECT * FROM ...` as the table-owner role (a
TRANSIENT generation cannot CLONE back into a permanent table). V158's tail is
`EXECUTE TASK`, so replaying it during a rebuild runs the backup as SYSTEM,
inside the Security CHANGE RISK carve-out for its own prune.

## 2. Teardown

Run snowflake/teardown.sql top to bottom (Section A executes; B and C stay
commented unless you chose the factory reset in step 0). The VERIFY query
at the bottom should list ONLY operator-data tables afterward (or nothing,
after a factory reset).

## 3. Migrations, in order, one file at a time

V001 → V124, each file fully, **stopping at the first error** — never run
past a failure (a partial apply is how task trees end up suspended; V041
resumes its graph both before and after its first fills now, but the rule
stands for every file). Notes:

- Several migrations end with a first-fill `CALL SP_LOAD_*` / `SP_REFRESH_*` at
  the file tail (mart/fact seed) — these are the slow ones; expect a few minutes
  each on WH_ALFA_ADMIN. The mart family (V027+) and the per-table storage mart
  (V124) added more of them, so watch for the trailing `CALL` in each file rather
  than relying on a fixed list.
- If you kept operator data, SCHEMA_VERSION already holds 1..124: the
  guards pass, IF NOT EXISTS objects recreate only what teardown dropped,
  and the version MERGEs no-op. That is the designed restore path.
- If you factory-reset, **stop after V157** and restore before V158 runs.
  V001 re-seeds the SETTINGS/ALERT_CONFIG/COMPANY_SCOPE defaults. V158's tail
  then backs up whatever the operator tables hold, and it prunes with whatever
  SETTINGS holds (the re-seeded BACKUP_KEEP_* 14 / 8). So restore your real
  values first, SETTINGS first, as the table-owner role:
      INSERT OVERWRITE INTO SETTINGS SELECT * FROM SETTINGS_BAK_<date>; -- etc.
      (or UPDATE the handful you care about: rates, budgets, routes.)
  You can also use the newest `OVERWATCH_BAK` generation dated before the reset.
  If you dropped OPERATOR_BACKUP_LOG too, choose it by name and ROW_COUNT:
      SELECT TABLE_NAME, ROW_COUNT, CREATED FROM DBA_MAINT_DB.INFORMATION_SCHEMA.TABLES
      WHERE TABLE_SCHEMA = 'OVERWATCH_BAK' ORDER BY 1;
  Then apply V158 and the rest. If V158 already ran on the re-seeded tables,
  run `ALTER TASK DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR SUSPEND;` first.
  Restore SETTINGS first, and never restore from the generation dated the
  replay day (or later). Verify, then RESUME the task (RUNBOOK §16 step 3).

## 4. Grants

Re-run snowflake/roles.sql (new V075 objects plus ALL + FUTURE grants).

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
(DEPLOYMENT.md). App v4.456.0 expects exactly V001..V124.

## 8. Prove the chain ticks

An hour after step 7, run snowflake/loader_chain_check.sql: every task
'started', hourly rows landing, freshness HOURS_BEHIND < 2 for hourly
sources. The fleet board after 24h is the final word.
