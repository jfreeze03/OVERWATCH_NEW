# OVERWATCH

Snowflake usage, cost, and operations command center for the ALFA / Trexis shared
Snowflake account. Streamlit app, mart-first data architecture, built for
Streamlit-in-Snowflake with per-user roles.

This is a ground-up rebuild of the original OVERWATCH repo. Every architectural
decision here traces to a finding in the hostile panel review of the old app
(`PANEL_REVIEW_20260707.md` in the old repo). The short version of the thesis:
**smaller, honest, tested.**

## What changed vs. the old app

| Old-app finding | What this repo does instead |
|---|---|
| Fabricated exec trend line, hardcoded action rows, fictional $50k budget | No synthetic data anywhere. Charts render real series or an honest empty state. Budget comes from `DBA_MAINT_DB.OVERWATCH.SETTINGS` or the KPI says "not configured". |
| Wall of zeros on first paint | Overview loads the compact exec mart automatically (one cheap cached query). Live fallback is a bounded aggregate, not a blank page. |
| Errors cached as empty data for up to 4h | Cached query functions raise on failure; Streamlit never caches exceptions. Failures surface as labeled errors, not silent empty frames. |
| 461 silent `except Exception` sites | Central `safe_page` boundary + error ring buffer + optional Snowflake error sink. Ruff `BLE001` enforced in CI. |
| 4 copies of SQL-safety primitives | One module: `app/core/sqlsafe.py`. |
| 6,134-line setup SQL, no versioning | Numbered migrations in `snowflake/migrations/` + `SCHEMA_VERSION` table + status check on the Admin page. |
| 92k lines, two apps, 30 zombie section modules | One app, 7 pages, pure-logic layer with tests. No dead routes. |
| Anyone could change the $/credit execs see | Rates live in `DBA_MAINT_DB.OVERWATCH.SETTINGS` (seeded: **$3.68 compute, $2.20 Cortex**). Sidebar override is admin-gated and watermarked. |
| Cloud-services adjustment hardcoded to 0 | Billed dollars come from `METERING_DAILY_HISTORY` **with** `CREDITS_ADJUSTMENT_CLOUD_SERVICES` applied. |
| Silent LIMIT injection | Row caps fetch `n+1`, set a `truncated` flag, and the UI shows a truncation banner. |
| No deep links | Page navigation syncs to `?page=` query params where the runtime supports it. |

## Company scoping (deliberate, documented)

ALFA and Trexis share one Snowflake account, so scoping is **hardcoded on purpose**
in exactly one module: `app/companies.py` (mirrored in the `COMPANY_SCOPE`
seed, with a unit test that keeps the two in sync).

- Trexis: the four `WH_TRXS_*` warehouses, `TRXS_*` databases, `TRXS_*` users.
- ALFA: everything else.
- Exception: user `KEBARR1` holds both ALFA and Trexis roles and is classified
  as **ALFA** by explicit override.

This is a convenience scope for a shared account, not a security boundary; the
security boundary is Snowflake roles under Streamlit-in-Snowflake.

## Pages

| Page | Job |
|---|---|
| Overview | Exec glance: spend vs budget, month-end forecast, alerts, platform score, real top actions. |
| Control Room | DBA morning triage: ranked issue queue, source freshness, 24h failures, spend movers. |
| Alerts | Alert rules, open events, ack/resolve workflow, generated native ALERT SQL. |
| Cost & Contract | Service/warehouse/user attribution, contract pacing, Cortex + storage, savings ledger (estimated vs verified). |
| Operations | Queries, tasks, warehouses, contention, change impact — p95, failures, queue, spill, anomalies, post-change regression verdicts. |
| Security | MFA gaps (login-evidence based), failed logins, grants, recent DDL changes. |
| Admin | Settings, migration status, source freshness, app self-cost, error log, telemetry. |

## Quick start

Local dev (uses `.streamlit/secrets.toml` connection):

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Snowflake setup (run in order as a deployment role):

```text
snowflake/migrations/V001__core.sql      -- db, schemas, settings, company scope, schema_version
snowflake/migrations/V002__facts.sql     -- fact tables, load procs, warehouse + resource monitor, tasks
snowflake/migrations/V003__marts.sql     -- exec board, control room snapshot, freshness view
snowflake/migrations/V004__alerts.sql    -- alert config/events/audit + scan proc
snowflake/migrations/V005__actions.sql   -- action queue + savings ledger
snowflake/migrations/V006__pipeline_sla.sql -- pipeline freshness SLA config + status
snowflake/migrations/V007__automation.sql -- budget alerts, AI digest, savings verification
snowflake/migrations/V008__chargeback.sql -- department chargeback map (warehouse + role)
snowflake/migrations/V009__credentials.sql -- 30-day credential expiry alerting
snowflake/migrations/V010__change_impact.sql -- object-change regression tracking + alerts
snowflake/migrations/V011__proactive_alerts.sql -- preventive rules: cloud-svc ratio, storage surge, serverless creep, copy failures, break-glass
snowflake/migrations/V012__routing_anomaly_remediation.sql -- alert routing, daily anomaly sweep, remediation log, DT failure alerts
snowflake/migrations/V013__user_prefs.sql -- saved views + default landing per user
snowflake/migrations/V014__lifecycle_hardening.sql -- contract-breach projection, fingerprint drift, fact retention
snowflake/migrations/V015__pilot_and_backups.sql -- Dynamic Table pilot + weekly operator-table backups
snowflake/roles.sql                      -- OVERWATCH_MONITOR / OVERWATCH_OPERATOR
snowflake/validate.sql                   -- post-install checks
```

Streamlit-in-Snowflake: see `DEPLOYMENT.md` (uses `snowflake.yml`, `environment.yml`).

## Rates

Defaults seeded in `SETTINGS` and mirrored in `app/config.py`:
compute **$3.68/credit**, Cortex **$2.20/credit**, storage **$23/TB/mo**.
Change them in the Admin page (operator role) — not in code.

## Development

```bash
pip install -r requirements-dev.txt
ruff check .
pytest -q
```

CI runs both on every push. The `app/logic/` and `app/data/` layers are
Streamlit-free by design and fully unit-testable.

## Docs

- `REBUILD_PLAN.md` — the plan this rebuild follows, with status.
- `ARCHITECTURE.md` — layers, data flow, caching, mart-first boundaries, security model.
- `DEPLOYMENT.md` — SiS deploy, migrations, roles, validation.
- `RUNBOOK.md` — the full operator manual: every metric, score, alert rule, AI engine, fallback, emergency lever, troubleshooting, DR.
- `CHANGELOG.md` — release history.
