# Deployment

## 1. Snowflake objects (one-time, then per release)

Run as a role that can create a database, warehouse, resource monitor, tasks,
and grants (break-glass admin is fine for setup; daily use should not be):

```
snowflake/migrations/V001__core.sql
snowflake/migrations/V002__facts.sql
snowflake/migrations/V003__marts.sql
snowflake/migrations/V004__alerts.sql
snowflake/migrations/V005__actions.sql
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
  lifecycle, action queue, savings ledger. Grant to the DBA(s) who ack alerts
  and edit settings.

## 3. Streamlit-in-Snowflake (primary target)

```bash
# Snowflake CLI
snow streamlit deploy --replace
```

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

## 5. Release checklist

1. `ruff check .` and `pytest -q` green (CI enforces).
2. New migration file if schema changed (never edit an applied `V00x` file).
3. Run migrations, then `snowflake/validate.sql` — all rows OK.
4. `snow streamlit deploy --replace`.
5. Open Admin → Migration status (no drift), Source freshness (all fresh),
   Self-cost (task + app spend sane).
6. Tag the release; update `CHANGELOG.md`.
