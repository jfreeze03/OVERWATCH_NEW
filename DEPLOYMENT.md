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

Each migration records itself in `OVERWATCH.CORE.SCHEMA_VERSION`; re-running is
safe (idempotent `CREATE OR REPLACE` / `CREATE IF NOT EXISTS` + MERGE seeds).
The Admin page compares `SCHEMA_VERSION` against the versions bundled with the
app and flags drift.

Cost controls installed by V002:
- `OVERWATCH_WH` — XSMALL, `AUTO_SUSPEND = 60`, dedicated to the app + tasks.
- `OVERWATCH_RM` — resource monitor, default 30 credits/month, suspends the
  warehouse at 100%. Adjust the quota in V002 before running if needed.

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
OVERWATCH_WH`); `environment.yml` pins the Snowflake-channel packages. Each
viewer runs under their own role — that is the access-control model.

## 4. Local development (dev only)

`.streamlit/secrets.toml`:

```toml
[connections.snowflake]
account = "<account>"
user = "<user>"
authenticator = "externalbrowser"   # or password
role = "OVERWATCH_MONITOR"
warehouse = "OVERWATCH_WH"
database = "OVERWATCH"
schema = "CORE"
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
