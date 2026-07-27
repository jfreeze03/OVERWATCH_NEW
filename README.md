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

- Trexis: `COMPANY_SCOPE` mapping rows, `WH_TRXS_*`/`TRXS_*` prefixes, `%TRXS%` roles.
- ALFA: needs evidence too (V044) — `WH_ALFA_*` warehouses, `ALFA%`/`ADMIN`
  databases, `%ALFA%` or DBA roles.
- Everything else classifies **UNKNOWN** and surfaces on Cost & Contract →
  Spend & Attribution (Unmapped entities) until a `COMPANY_SCOPE` row maps it —
  nothing silently bills ALFA.
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
snowflake/migrations/V016__closing_loops.sql -- pre-explained anomalies, dept budgets, org creep, volume drop, canary sentinel
snowflake/migrations/V017__hardening_v7.sql -- scan v7 per-rule isolation, deploy stage, render SLA, version guard
snowflake/migrations/V018__delivery_first_class.sql -- notify task in-chain, guarded auto-resume, digest delivery
snowflake/migrations/V019__scoping_fixes.sql -- role-based Trexis user scoping, WH_TRXS_LINEAGE
snowflake/migrations/V020__credentials_column.sql -- CREDENTIALS.EXPIRATION_DATE, re-enable expiry rule
snowflake/migrations/V021__precision_telemetry.sql -- rule precision, fleet query telemetry, app self-cost
snowflake/migrations/V022__delivery_per_route.sql -- per-route delivery ledger; additive fan-out + honest retries
snowflake/migrations/V023__prod_scoped_volume.sql -- PROD-only volume-drop sweep; scan v9 (CREDENTIALS columns)
snowflake/migrations/V024__warehouse_change_scorecard.sql -- SHOW WAREHOUSES snapshots, change registry, WH_CHANGE_REGRESSION
snowflake/migrations/V025__break_glass_policy.sql -- SEC_BREAK_GLASS_USE disabled (routine admin roles here)
snowflake/migrations/V026__teams_safe_delivery.sql -- sender v3: JSON-safe payloads (Teams Workflows compatible)
snowflake/migrations/V027__mart_family.sql -- 9 scheduled marts + SP_LOAD_MARTS_V27 + telemetry rider
snowflake/migrations/V028__cred_expiry_10d.sql -- credential expiry: 10-day horizon (rule + posture bucket)
snowflake/migrations/V029__loader_fix.sql -- role/schema-hour loader arms: GROUP BY fix (superseded by V030)
snowflake/migrations/V030__loader_fix2.sql -- correct arm shape (UDF outside aggregation) + posture MFA/breakglass
snowflake/migrations/V031__scan_tuning_and_tagcov.sql -- change-impact scan v2 (tracking-bounded) + tag-coverage mart
snowflake/migrations/V032__incident_object.sql -- INCIDENTS + members + lineage + proposals + auto-declare
snowflake/migrations/V033__change_attribution.sql -- CHANGED_BY on the change registry + DEPLOY_ACTORS setting
snowflake/migrations/V034__route_company_filter.sql -- per-route COMPANY_FILTER; sender v4 (Teams = ALFA-only)
snowflake/migrations/V035__lock_wait_mart.sql -- lock-wait mart (page views never scan LOCK_WAIT_HISTORY)
snowflake/migrations/V036__pattern_cost_mart.sql -- pattern-cost mart (measured $ per repeated statement)
snowflake/migrations/V037__pattern_env_grain.sql -- pattern mart v2: DATABASE_NAME grain + HLL users (compare env prep)
snowflake/migrations/V038__ledger_autobook.sql -- ledger autobook (detected cost-lever changes settle themselves)
snowflake/migrations/V039__pseudo_warehouse_filter.sql -- pseudo-warehouse filter (CLOUD_SERVICES_ONLY out of the warehouse fact)
snowflake/migrations/V040__freshness_state.sql -- freshness state table + 10-min snapshot (lookup, not 19 aggregates)
snowflake/migrations/V041__loader_efficiency.sql -- loader efficiency: staged QH extract, xdim alloc fact, exec board v2, watermarks + nightly reconcile, loader-owned freshness, ops-diag + platform-score marts, posture riders
snowflake/migrations/V042__codex_r22.sql -- codex r22: FACT_QUERY_DAILY, atomic extract + gated watermark, ops-diag backfill, purge coverage, AI fact usage stamps
snowflake/migrations/V043__task_retirement_alert_teeth.sql -- task retirement loader-side (fills/board/score/purge/reconcile/freshness + tables dropped, PIPE_TASK_FAILURES disabled) + r25 alert teeth (new-admin-network, egress spike)
snowflake/migrations/V044__unknown_classification.sql -- UNKNOWN classification (#18): evidence-based company both sides, COMPANY_SCOPE database mapping lever, board UNKNOWN scope
snowflake/migrations/V045__task_monitoring_restored.sql -- owner correction: task monitoring restored (tables/procs/rule/refill; teeth + UNKNOWN scope kept); OVERWATCH_RM dropped
snowflake/migrations/V046__storage_truth.sql -- storage truth: account tiers (stage/hybrid/archive) + per-DB monthly-average billing basis (COST_DB recon R3 / audit F1)
snowflake/migrations/V047__pattern_cost_qas.sql -- pattern-cost mart includes Query Acceleration (Codex audit item 4)
snowflake/migrations/V048__object_cost_ledger.sql -- FACT_OBJECT_COST_DAILY object-cost ledger (measured split + serverless arms)
snowflake/migrations/V049__write_target_attribution.sql -- write-target attribution (OBJECTS_MODIFIED joins the split; residual = no-read-no-write compute)
snowflake/migrations/V050__one_pass_read_write_arms.sql -- one-pass object-cost loader + read/write arm split
snowflake/migrations/V051__action_layer.sql -- atomic alert-lifecycle proc + idempotency (scoped slice)
snowflake/migrations/V052__exec_board_windows_180_365.sql -- exec-board 180/365 windows (long-history filter)
snowflake/migrations/V053__action_layer_remediation_verify.sql -- remediation + verify procs (action layer phase a)
snowflake/migrations/V054__exec_board_window_history.sql -- exec-board 180/365 windows read full history (source-horizon fix + retention floor)
snowflake/roles.sql                      -- direct grants to SNOW_ACCOUNTADMINS / SNOW_SYSADMINS (monitor/operator layer retired v4.42)
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

- `FEATURES.md` — one-line map of every capability and where it lives (start here).

- `REBUILD_PLAN.md` — the plan this rebuild follows, with status.
- `ARCHITECTURE.md` — layers, data flow, caching, mart-first boundaries, security model.
- `DEPLOYMENT.md` — SiS deploy, migrations, roles, validation.
- `RUNBOOK.md` — the full operator manual: every metric, score, alert rule, AI engine, fallback, emergency lever, troubleshooting, DR.
- `docs/reviews/` — external review rounds and the point-by-point responses (decision trail).
- `CHANGELOG.md` — release history.
