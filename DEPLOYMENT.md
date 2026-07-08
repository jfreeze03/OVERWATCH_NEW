# Deployment

## 1. Snowflake objects (one-time, then per release)

Run as **SNOW_ACCOUNTADMINS** (or **SNOW_SYSADMINS** if it can create the
warehouse/resource monitor and grants) — these are the account's DBA roles:

```
snowflake/migrations/V001__core.sql
snowflake/migrations/V002__facts.sql
snowflake/migrations/V003__marts.sql
snowflake/migrations/V004__alerts.sql
snowflake/migrations/V005__actions.sql
snowflake/migrations/V006__pipeline_sla.sql
snowflake/migrations/V007__automation.sql
snowflake/migrations/V008__chargeback.sql
snowflake/migrations/V009__credentials.sql
snowflake/migrations/V010__change_impact.sql
snowflake/migrations/V011__proactive_alerts.sql
snowflake/migrations/V012__routing_anomaly_remediation.sql
snowflake/migrations/V013__user_prefs.sql
snowflake/migrations/V014__lifecycle_hardening.sql
snowflake/migrations/V015__pilot_and_backups.sql
snowflake/migrations/V016__closing_loops.sql
snowflake/migrations/V017__hardening_v7.sql
snowflake/migrations/V018__delivery_first_class.sql
snowflake/migrations/V019__scoping_fixes.sql
snowflake/migrations/V020__credentials_column.sql
snowflake/roles.sql
snowflake/validate.sql   -- read the output; every row should be OK
```

Each migration records itself in `DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION`; re-running is
safe (idempotent `CREATE OR REPLACE` / `CREATE IF NOT EXISTS` + MERGE seeds).
The Admin page compares `SCHEMA_VERSION` against the versions bundled with the
app and flags drift.

Cost controls installed by V002:
- `WH_ALFA_OVERWATCH` — XSMALL, `AUTO_SUSPEND = 60`, dedicated to the app + tasks.
- `OVERWATCH_RM` — resource monitor, default 30 credits/month, suspends the
  warehouse at 100%. Adjust the quota in V002 before running if needed.

### Shared schema warning (read before migrating)

All objects live in **`DBA_MAINT_DB.OVERWATCH`** — the same schema the
previous OVERWATCH app used. Migrations are strictly `CREATE IF NOT EXISTS` +
`MERGE`: they will never drop or overwrite an existing table. That also means
**name collisions keep the OLD table shape** and this app's queries against
them will fail cleanly. Known collisions with the old app: `ALERT_CONFIG`,
`ALERT_EVENTS`, and `FACT_QUERY_HOURLY`. If those exist with the old shape,
rename them first (e.g. `ALTER TABLE ... RENAME TO ALERT_CONFIG_V3;`), then
run the migrations. `snowflake/validate.sql` checks the shapes and flags any
survivor.

Task graphs run on the dedicated **`WH_ALFA_OVERWATCH`** warehouse (XSMALL,
60s auto-suspend, `OVERWATCH_RM` resource monitor).

## 2. Roles

- `OVERWATCH_MONITOR` — read-only telemetry (IMPORTED PRIVILEGES on SNOWFLAKE
  db + SELECT on OVERWATCH objects). Grant to viewer roles.
- `OVERWATCH_OPERATOR` — MONITOR plus INSERT/UPDATE on settings, alert
  lifecycle, action queue, savings ledger. `roles.sql` grants it to
  **SNOW_SYSADMINS** and **SNOW_ACCOUNTADMINS** (the account's DBA roles);
  both already resolve to the DBA navigation profile in-app, so members get
  the Admin page and gated in-app execution with no extra setup.
- Own the Streamlit app and the OVERWATCH objects with **SNOW_SYSADMINS** so
  day-to-day operation never requires the break-glass role.
- `ALERT_AUDIT` and `REMEDIATION_LOG` are append-only for operators
  (INSERT granted, UPDATE/DELETE explicitly revoked). Members of the owning
  role can still modify them — if an auditor requires stronger guarantees,
  export the tables on a schedule or replicate them to a locked schema.

## 3. Streamlit-in-Snowflake (primary target)

App files live on the dedicated stage
**`DBA_MAINT_DB.OVERWATCH.OVERWATCH_STAGE`** (created by V017, directory
table enabled). `snowflake.yml` pins the deploy there.

```bash
# Snowflake CLI (uploads artifacts to OVERWATCH_STAGE, creates/updates the app)
snow streamlit deploy --replace
```

Manual path (no CLI — SnowSQL or any PUT-capable client):

```sql
PUT file://streamlit_app.py @DBA_MAINT_DB.OVERWATCH.OVERWATCH_STAGE/app/ OVERWRITE=TRUE AUTO_COMPRESS=FALSE;
PUT file://environment.yml  @DBA_MAINT_DB.OVERWATCH.OVERWATCH_STAGE/app/ OVERWRITE=TRUE AUTO_COMPRESS=FALSE;
PUT file://app/*            @DBA_MAINT_DB.OVERWATCH.OVERWATCH_STAGE/app/app/ OVERWRITE=TRUE AUTO_COMPRESS=FALSE;
-- (repeat per subfolder: app/core, app/data, app/logic, app/ui, app/ui/pages)

CREATE OR REPLACE STREAMLIT DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP
    ROOT_LOCATION = '@DBA_MAINT_DB.OVERWATCH.OVERWATCH_STAGE/app'
    MAIN_FILE = 'streamlit_app.py'
    QUERY_WAREHOUSE = WH_ALFA_OVERWATCH
    TITLE = 'OVERWATCH — Snowflake Command Center';
```

`LIST @DBA_MAINT_DB.OVERWATCH.OVERWATCH_STAGE` (or the directory table)
shows what is deployed; re-running PUT with OVERWRITE replaces files and the
app picks them up on next open.

`snowflake.yml` defines the app (`streamlit_app.py`, `query_warehouse:
WH_ALFA_OVERWATCH`); `environment.yml` pins the Snowflake-channel packages. Each
viewer runs under their own role — that is the access-control model.

## 4. Local development (dev only)

`.streamlit/secrets.toml`:

```toml
[connections.snowflake]
account = "<account>"
user = "<user>"
authenticator = "externalbrowser"   # or password
role = "OVERWATCH_MONITOR"
warehouse = "WH_ALFA_OVERWATCH"
database = "DBA_MAINT_DB"
schema = "OVERWATCH"
```

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

A local run uses one shared connection/role for every browser tab. Do not
expose a local/Community-Cloud deployment to mixed audiences — that model has
no per-user access control. This is a dev path only.

## 5. Teardown / drop-and-restore

`snowflake/teardown.sql` drops OVERWATCH's objects for a clean rebuild. It is
surgical by design — the schema is shared with the old app, so it never drops
`DBA_MAINT_DB.OVERWATCH` itself, only named objects:

- **Section A (live):** tasks, alerts, procs, functions, views, transient
  facts/marts. Safe anytime — re-run V001..V005 and the loaders repopulate.
- **Section B (commented):** operator data — settings, company scope, alert
  config/events/audit, action queue, savings ledger, error log,
  schema_version. Uncomment only for a factory reset, and run the provided
  `CLONE` backups first. `UNDROP TABLE ...` also works within Time Travel.
- **Section C (commented):** warehouse, resource monitor, Streamlit app
  object, roles — shared infrastructure, dropped only deliberately.

The verify query at the bottom lists any surviving OVERWATCH objects. A unit
test (`tests/test_teardown_coverage.py`) fails CI if a migration creates an
object the teardown does not cover, or if a destructive drop ever goes live.

Restore = migrations in order -> roles.sql -> validate.sql (all rows OK).

## 6. Disaster recovery (summary — full detail in RUNBOOK.md)

- **Weekly backups:** `TASK_BACKUP_OPERATOR` (Sun 05:40) clones every
  operator-editable table to `<NAME>_BAK_LAST` (zero-copy). Restore one table:
  `CREATE OR REPLACE TABLE <NAME> CLONE <NAME>_BAK_LAST;`
- **Fine-grained undo:** Time Travel — `SELECT * FROM <t> AT(OFFSET => -3600)`
  or `UNDROP TABLE <t>` within the retention window.
- **Schema dropped:** `UNDROP SCHEMA DBA_MAINT_DB.OVERWATCH;` first. If gone,
  re-run migrations V001..V015 + roles.sql + validate.sql; facts refill from
  the loader tasks (history limited to ACCOUNT_USAGE retention); operator
  tables restore from `*_BAK_LAST` clones if they survived, else re-seed.
- **App broken after deploy:** `snow streamlit deploy --replace` with the
  previous git tag; migrations are additive so no schema rollback is needed.

## 7. Release checklist

1. `ruff check .` and `pytest -q` green (CI enforces).
2. New migration file if schema changed (never edit an applied `V00x` file).
3. Run migrations, then `snowflake/validate.sql` — all rows OK.
4. `snow streamlit deploy --replace`.
5. Open Admin → Migration status (no drift), Source freshness (all fresh),
   Self-cost (task + app spend sane).
6. Tag the release; update `CHANGELOG.md`.


## Mid-migration expectations (append-only history)

Old migrations deliberately keep their era's `SP_ALERT_SCAN` text — including
columns later discovered not to exist on this account (`EXPIRES_AT`,
`CREDENTIALS.DELETED_ON`). Those bodies never execute once the sequence
completes: V019 disables SEC_CRED_EXPIRY, V020 re-points it (scan v8), V023
is terminal (scan v9). If the hourly scan fires while you are mid-sequence,
expect isolated `rule_block_failed` rows in the error log — they stop at the
next run after the sequence finishes. Do not rewrite historical migrations;
fix forward with a new scan version.
