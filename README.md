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
snowflake/migrations/V055__cloud_services_breakdown.sql -- per-query cloud-services credits persisted (extract column + MART_CLOUD_SVC_DAILY) for the CS-ratio drill-down
snowflake/migrations/V056__loader_reconcile_alert_fixes.sql -- Batch B: loader/reconcile day partial-freeze, ops-diag hour double-count, alert dedupe/classification
snowflake/migrations/V057__fail_status_token.sql -- FAILS token fix: 4 mart arms counted EXECUTION_STATUS='FAILED' (never matches) -> 'FAIL'; failure counts were a constant 0
snowflake/migrations/V058__task_node_timing.sql -- per-node loader-timing observability: MART_TASK_NODE_DAILY (queue + exec delay per task) via a new contained arm; enables data-driven schedule tuning
snowflake/migrations/V059__task_graph_root_credits.sql -- task-graph pipeline credits: arm [6] rolls attribution up by COALESCE(ROOT_QUERY_ID,QUERY_ID) so WH_CREDITS captures proc-body compute (was ~0 for CALL tasks)
snowflake/migrations/V060__family_elapsed_queued_alert_guard.sql -- triage #5/#11 + guard: family mart gains TOTAL_ELAPSED_SEC (COMPILE_PCT bounded), schema-hourly queued includes provisioning, CS-ratio alert excludes the pseudo-warehouse
snowflake/migrations/V061__ai_loader_alert_score_purge_fixes.sql -- AI loader/alert/score/purge correctness: AI arms day-aligned (C5), QAS added to proc/pipeline attribution (C2), MTD alerts + score priced at the AI rate (C1), COST_AI_CREEP seeded (C6), self-alert block count fixed (B41), purge covers 3 more daily facts (B33). Tail heals FACT_AI_USAGE_DAILY (365) + score AI column.
snowflake/migrations/V062__loader_robustness_alert_split_webhook.sql -- loader robustness + correctness: query fail predicate <> SUCCESS (R3-4, app+mart), backfill day-cap fix (B5), reconcile boundary-hour clamp (B10), hourly-facts watermark catch-up (B11), daily-facts/object-cost transaction wraps (B34), daily alert blocks split to SP_ALERT_SCAN_DAILY after TASK_LOAD_DAILY (C9). Webhook truncation-delivery (B9) + perf loader (T3) deferred to V063 (does NOT modify SP_NOTIFY_WEBHOOK despite the filename). Tail heals interior holes via SP_LOAD_HOURLY_FACTS.
snowflake/migrations/V063__webhook_capture_once_daily_facts_failguard.sql -- webhook capture-once (B9: SP_NOTIFY_WEBHOOK freezes the fitting event set into ONE array so the message, ledger, and NOTIFIED_AT can't diverge -- no send-vs-ledger race; OWNER SMOKE TEST) + daily-facts fail-guard (B34: SP_LOAD_DAILY_FACTS holds the watermark + returns non-success on a per-table failure). Perf loader (T3) deferred to V064.
snowflake/migrations/V064__webhook_drain_watermarks_alert_burn_telemetry.sql -- webhook oldest-first bounded drain (rec8: SP_NOTIFY_WEBHOOK drains the backlog in batches oldest-first so old alerts stop starving past the 24h window; capture-once per batch; OWNER SMOKE TEST) + per-source daily watermarks (rec7: SP_LOAD_DAILY_FACTS + SP_NIGHTLY_RECONCILE keep one mark per source so a single table's failure holds only its own mark) + contract-breach trailing-30-complete-day burn (rec20-alert, was /30 over a partial day) + APP_QUERY_TELEMETRY SAMPLE_PROB/QUERY_ID (rec18). Perf loader (T3) still deferred.
snowflake/migrations/V065__alert_run_rate_windows.sql -- alert run-rate window fixes (bug round 5): SP_ALERT_SCAN_DAILY re-derived so COST_FORECAST_BREACH projects month-end from a COMPLETE-days-only run-rate (was MTD$/day-of-month over a partial today -> under-projected -> suppressed the budget-breach alert; rank2) and COST_AI_CREEP compares equal today-excluded 7-complete-day windows (was THIS_WK 8d incl today vs PRIOR_WK 7d -> inflated WoW; rank3). No new objects; no smoke test (deterministic, byte-verified).
snowflake/migrations/V066__alert_escalation_serverless_window_timeline_atomicity.sql -- alert escalation + serverless window + timeline atomicity (bug round 6): SP_ALERT_SCAN gains severity bands on the PIPE_COPY_FAILURES (#1) and COST_DEPT_BUDGET_PACE (#11) dedupe keys so a within-bucket HIGH->CRITICAL / MEDIUM->HIGH crossing re-fires, and COST_SERVERLESS_CREEP excludes today so both weeks are 7 complete days (#6); SP_ALERT_SCAN_DAILY COST_CONTRACT_BREACH weekly key gains a severity band (#2); SP_LOAD_MARTS_V27 incident-timeline arm [8] DELETE+INSERT wrapped in one transaction so a failed rebuild can't blank the trailing 48h (#3). No new objects; no smoke test (deterministic, byte-verified).
snowflake/migrations/V067__alert_attribution_onset_supersede_objectcost.sql -- alert attribution + serverless onset + escalation supersede + object-cost honesty (Codex review): SP_ALERT_SCAN COST_STORAGE_SURGE/PIPE_COPY_FAILURES use COMPANY_FOR_DATABASE not a raw TRXS%/ALFA guess (#22), COST_SERVERLESS_CREEP emits a 999 onset sentinel when the prior week is 0 (#20), and a post-scan sweep supersedes the lower-band OPEN alert when its higher-band sibling is open (#40, the V066 escalation follow-on); SP_LOAD_OBJECT_COST returns non-OK after a rolled-back load (#10). No new objects; no smoke test (deterministic, byte-verified).
snowflake/migrations/V068__standalone_mart_freshness_stamps.sql -- standalone-mart freshness stamps (screenshot finding): SP_LOAD_LOCK_WAIT_MART + SP_LOAD_PATTERN_COST gain the V041-R6 loader-owned SOURCE_FRESHNESS_STATE stamp they were never given when V041 retired the snapshot sweep (their rows froze at apply time -- the Brief card showed MART_LOCK_WAIT_DAILY 448h stale). Stamped as a RUN timestamp so zero-event windows read fresh (no-news is not no-load); the tail heals both rows immediately. No new objects; no smoke test (deterministic, byte-verified).
snowflake/migrations/V069__exec_board_serverless_ai_drivers.sql -- exec-board serverless + AI cost drivers (audit C5): SP_REFRESH_EXEC_BOARD built its COST_DRIVER rows from FACT_WAREHOUSE_DAILY alone, so serverless (auto-clustering, MV refresh, search optimization, snowpipe, serverless tasks) and AI/Cortex spend could never reach the Overview driver panel even as the fastest-growing line -- while the same page's KPIs cover compute + serverless + AI. A second COST_DRIVER arm over FACT_METERING_DAILY (the app fact, not ACCOUNT_USAGE) excludes warehouse metering, prices AI credits at AI_CREDIT_PRICE_USD and the rest at CREDIT_PRICE_USD, keeps the same -365d/windows semantics and column contract, and labels drivers 'Serverless:'/'AI/Cortex:' so the app needs no change; account-level metering lands on the ALL scope only. No new objects; no smoke test (deterministic, byte-verified).
snowflake/migrations/V070__delivery_routing_teams_only.sql -- Teams-only delivery routing (V012/V018 forward fix): SP_DAILY_DIGEST hardcoded the retired Slack integration OVERWATCH_WEBHOOK and swallowed its send with WHEN OTHER THEN NULL, so the morning digest had NEVER been delivered on this Teams-only account while it still returned 'delivery attempted'. Re-derived from V018 to walk the enabled ALERT_ROUTES rows and send through each row's INTEGRATION_NAME (SP_NOTIFY_WEBHOOK per-route idiom), ledgering each outcome to APP_ERROR_LOG as digest_send_failed instead of discarding it; the in-app write is untouched and the return is machine-readable 'sent N/M routes' (#23). Idempotent blocks disable any enabled route whose integration is absent so the dead default Slack route stops burying real errors (#25), and resume TASK_ALERT_NOTIFY when an enabled route resolves to a live integration (#24). Only SP_DAILY_DIGEST re-defined; no new objects; byte-verified.
snowflake/migrations/V071__task_graph_rechain_retry.sql -- task-graph re-chain + root retry policy (DAG surgery on applied tasks): Snowflake runs sibling child tasks in parallel, so readers hung off the roots as siblings of the extract/reconcile that feed them raced their own data. TASK_REFRESH_EXEC_BOARD + TASK_ALERT_SCAN re-pointed AFTER TASK_LOAD_HOURLY -> AFTER TASK_QH_EXTRACT so they read the refreshed query facts (#3); TASK_LOAD_MARTS_V27_DAILY + TASK_PLATFORM_SCORE_DAILY + TASK_ALERT_SCAN_DAILY re-pointed AFTER TASK_LOAD_DAILY -> AFTER TASK_NIGHTLY_RECONCILE so they read reconciled data rather than racing the delete+reload (#4; reconcile atomicity #7 stays deferred). Both roots gain TASK_AUTO_RETRY_ATTEMPTS=1 + SUSPEND_TASK_AFTER_NUM_FAILURES=10 (#43); SCHEMA_VERSION.DESCRIPTION widened to VARCHAR(4000) before the guard (#42). ADD/REMOVE AFTER + SET only -- no task body re-defined (schedules/warehouses/bodies preserved); each re-point is state-checked (ADD only if the new predecessor is absent, REMOVE only if the old is present, ADD before REMOVE) with no error swallowing, so a matched re-run is a no-op and any genuine ALTER failure aborts loudly leaving the graph suspended rather than silently orphaned; a clean run ends both graphs with SYSTEM$TASK_DEPENDENTS_ENABLE. Green-on-failure finalizer (#5) deferred. No new objects; OWNER SMOKE TEST (SHOW TASKS predecessors + all started).
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
