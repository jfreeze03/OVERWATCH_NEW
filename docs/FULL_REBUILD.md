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
(DEPLOYMENT.md). App v4.456.0 expects exactly V001..V124.

## 8. Prove the chain ticks

An hour after step 7, run snowflake/loader_chain_check.sql: every task
'started', hourly rows landing, freshness HOURS_BEHIND < 2 for hourly
sources. The fleet board after 24h is the final word.
