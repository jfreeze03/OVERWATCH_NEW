# OVERWATCH — Operator Runbook

The complete operating manual. Assumes you have never seen this app: it
covers what every number means and how it is calculated, every scheduled
job, every alert rule, the AI engines and their grounding rules, every
database object, what happens when a source is missing, emergency levers,
troubleshooting, and disaster recovery.

**The one-paragraph version:** OVERWATCH is a Streamlit-in-Snowflake app
that watches this Snowflake account's cost, performance, pipelines, and
governance for the two companies sharing it (ALFA and Trexis). Hourly tasks
copy ACCOUNT_USAGE telemetry into small fact tables; an hourly scan raises
alert events against ~16 rules and pushes them to webhooks; daily scans
catch anomalies, regressions, and drift; the app renders it all with honest
labels and generates (never silently executes) the SQL to fix what it finds.

---

## 1. Ten-minute orientation

- **Brief** is the one-scroll morning page (numbers, fires, asks); **Overview** loads the executive board (one cached mart
  query): spend vs budget, month-end forecast, alerts, platform score, top
  actions.
- **Control Room** is the DBA morning page: triage queue, freshness,
  incident timeline, spend movers.
- The **sidebar health strip** (every page) shows open criticals, stalest
  telemetry hours, and MTD credits.
- The **top filter strip** scopes almost every panel: Company (ALFA default),
  Environment, Window days, Database, and contains-filters for warehouse /
  user / schema. Filters match literally (`WH_` = literal underscore).
- Pages use **section pills** (only the active section runs its queries) and
  every table has a CSV download. `?page=` and `?section=` are shareable.
- **💾 Views** (in the filter strip) saves page+section+filters per user,
  sets a default landing view, and a display timezone.
- Roles: viewers hold **OVERWATCH_MONITOR** (read-only), operators hold
  **OVERWATCH_OPERATOR** (may execute generated statements — always behind a
  typed confirmation, always audited). SNOW_SYSADMINS / SNOW_ACCOUNTADMINS
  map to the DBA navigation profile automatically.

## 2. Architecture

**Layers.** `app/data/*` builds SQL strings only. `app/logic/*` is pure
Python (fully unit-tested; no Streamlit imports). `app/ui/*` renders.
`app/core/*` is the runtime: session, query engine, errors, state.

**Mart-first.** Hourly tasks MERGE ACCOUNT_USAGE into `FACT_*` tables;
pages read facts first and fall back to bounded live ACCOUNT_USAGE queries
with the source always labeled under the table ("mart" vs "live fallback").

**Query engine** (`app/core/query.py`):
- Four cache tiers (TTL seconds / statement timeout seconds):
  live 30/30 · recent 300/120 · historical 3600/180 · metadata 14400/30.
- Cache key = SQL text + current role + refresh salt. Errors are never
  cached (cached functions raise; Streamlit does not cache exceptions).
- Row caps fetch n+1 rows and banner truncation; nothing is silently cut.
- `run_batch()` submits a section's queries server-side async in parallel;
  any failure falls back to serial per-query calls.
- "Refresh data" (sidebar) bumps the salt = full cold reload for you only.

**Company scoping** (`app/companies.py`, mirrored in the `COMPANY_SCOPE`
table with a sync test): Trexis = the four `WH_TRXS_*` warehouses,
`TRXS_*` databases, `TRXS_*` users; ALFA = everything else; user `KEBARR1`
holds both companies' roles and is classified **ALFA** by explicit
override. This is a convenience scope on a shared account, not a security
boundary — Snowflake roles are the security boundary.

**Honesty contracts** enforced by tests: no synthetic data anywhere; empty
states say why and what would fill them; estimated vs verified savings
never mix; every AI output is grounded in rows shown to it; every panel
labels its source and lag.

**Streamlit-in-Snowflake specifics:** each viewer runs under their own
role. `ALTER SESSION` is not available to the app (capability detected at
connect; query tags/timeouts degrade to warehouse-level backstops). The app
and all tasks run on the dedicated XSMALL warehouse **WH_ALFA_OVERWATCH**
under the **OVERWATCH_RM** resource monitor (30 credits/month default).

## 3. Install / upgrade

Run in order as a DBA role (SNOW_SYSADMINS unless noted):

| Migration | Creates |
|---|---|
| V001 core | DB context, `SETTINGS`, `COMPANY_SCOPE` (+`COMPANY_FOR_USER()`), `APP_ERROR_LOG`, `SCHEMA_VERSION` |
| V002 facts | `FACT_METERING_DAILY`, `FACT_WAREHOUSE_DAILY`, `FACT_QUERY_HOURLY`, `FACT_TASK_DAILY`, `FACT_LOGIN_DAILY`, `FACT_STORAGE_DAILY`, loader procs, `WH_ALFA_OVERWATCH` + `OVERWATCH_RM`, `TASK_LOAD_HOURLY`/`TASK_LOAD_DAILY` |
| V003 marts | `MART_EXEC_BOARD` (+refresh proc/task), control-room snapshot, `MART_SOURCE_FRESHNESS` |
| V004 alerts | `ALERT_CONFIG`, `ALERT_EVENTS`, `ALERT_AUDIT`, `SP_ALERT_SCAN`, `TASK_ALERT_SCAN` |
| V005 actions | `ACTION_QUEUE`, `SAVINGS_LEDGER` |
| V006 pipeline SLA | `PIPELINE_SLA_CONFIG` + status views |
| V007 automation | Budget rules, `DAILY_DIGEST` + digest task, savings auto-verify task, `ALERT_EVENTS.NOTIFIED_AT` |
| V008 chargeback | `DEPARTMENT_MAP` (warehouse→department) |
| V009 credentials | `SEC_CRED_EXPIRY` rule; scan learns CREDENTIALS |
| V010 change impact | `OBJECT_CHANGE_REGISTRY`, `SP_CHANGE_IMPACT_SCAN` + daily task, `PERF_CHANGE_REGRESSION` rule |
| V011 prevention | 5 rules: cloud-svc ratio, storage surge, serverless creep, copy failures, break-glass |
| V012 routing+sweep | `ALERT_ROUTES`, route-aware webhook sender, `SP_ANOMALY_SWEEP` + daily task, `REMEDIATION_LOG`, `PIPE_DT_FAILURES` |
| V013 user prefs | `USER_PREFS` (saved views / default landing / display TZ) |
| V014 lifecycle | `COST_CONTRACT_BREACH` (scan v5), `PERF_FINGERPRINT_DRIFT` (sweep v2, Mondays), `SP_PURGE_FACTS` + monthly task |
| V015 pilot+backups | `MART_SPEND_ROLLUP_DT` (Dynamic Table pilot), `SP_BACKUP_OPERATOR_TABLES` + Sunday task |

App files deploy to the dedicated stage
**`DBA_MAINT_DB.OVERWATCH.OVERWATCH_STAGE`** (V017; `snowflake.yml` pins it —
see DEPLOYMENT.md for the manual PUT path). V017 also inaugurates the
version guard: each migration refuses to run if its predecessor is missing.

Then `roles.sql` (idempotent; re-run after every upgrade) and
`validate.sql` (every row should read OK). Deploy the app with
`snow streamlit deploy --replace`.

**Opt-in scripts** (run deliberately, not part of the chain):
`webhook_delivery.sql` (notification integration + sender task),
`native_alert_templates.sql` (CREATE ALERT equivalents if you prefer
native alerts), `ml_forecast_option.sql` (SNOWFLAKE.ML.FORECAST engine), `backfill_365.sql`
(one-time year of daily facts — run before ACCOUNT_USAGE history ages out).
`teardown.sql` is the surgical uninstall (never drops the schema).

## 4. Scheduled automation (all times America/Chicago)

| Task | Schedule | Calls | Writes |
|---|---|---|---|
| TASK_LOAD_HOURLY | :07 hourly | SP_LOAD_HOURLY_FACTS | FACT_QUERY_HOURLY + hourly-grain facts |
| TASK_REFRESH_EXEC_BOARD | after hourly load | SP_REFRESH_EXEC_BOARD | MART_EXEC_BOARD |
| TASK_ALERT_SCAN | after hourly load | SP_ALERT_SCAN (v5) | ALERT_EVENTS |
| TASK_ALERT_NOTIFY | after scan (opt-in resume) | SP_NOTIFY_WEBHOOK | webhook sends, NOTIFIED_AT |
| TASK_LOAD_DAILY | 06:45 daily | SP_LOAD_DAILY_FACTS | daily facts |
| TASK_ANOMALY_SWEEP | 06:40 daily | SP_ANOMALY_SWEEP (v2) | anomaly + DT-failure + (Mon) drift events |
| TASK_CHANGE_IMPACT_SCAN | 06:50 daily | SP_CHANGE_IMPACT_SCAN | OBJECT_CHANGE_REGISTRY + regression events |
| TASK_DAILY_DIGEST | 07:20 daily | SP_DAILY_DIGEST | DAILY_DIGEST (Cortex) |
| TASK_VERIFY_SAVINGS | 07:40 1st of month | SP_VERIFY_IDLE_SAVINGS | SAVINGS_LEDGER verifications |
| TASK_PURGE_FACTS | 05:20 1st of month | SP_PURGE_FACTS | deletes beyond retention |
| TASK_BACKUP_OPERATOR | 05:40 Sundays | SP_BACKUP_OPERATOR_TABLES | `*_BAK_LAST` clones |
| TASK_CANARY_SENTINEL | 05:30 Mondays | SP_CANARY_SENTINEL | CANARY_RESULTS + OPS_CANARY_FAIL |
| MART_SPEND_ROLLUP_DT | TARGET_LAG 6h (Dynamic Table) | — | monthly spend rollup (pilot) |

**Notes on the automation:** the Monday 05:30 sentinel deliberately leads
the morning batch, so its warehouse resume is shared, not extra. Its
`EXECUTE IMMEDIATE 'SELECT 1 FROM ' || name` loop is exempt from the sqlsafe
rule because the probe list is a hardcoded array — no user input ever
reaches it. **Scan-proc test strategy:** CI asserts structure (17 isolated
blocks, per-block dedupe, rule carryover on every regeneration); runtime
failures are the sentinel's and OPS_SCAN_DEGRADED's job — a broken block
logs `rule_block_failed` and self-alerts, which IS the failure-injection
test running in production, safely. **DT pilot cost:** MART_SPEND_ROLLUP_DT
refreshes on WH_ALFA_OVERWATCH, so its cost shows up in Admin → App
self-cost; compare against the loader tasks before migrating more marts.

`SHOW TASKS IN SCHEMA DBA_MAINT_DB.OVERWATCH;` — every state should be
`started` except TASK_ALERT_NOTIFY before its integration exists.

## 5. Pages, sections, and every metric

**Money convention:** billed credits = `CREDITS_BILLED` where the source
provides it, else `GREATEST(0, CREDITS_USED + CREDITS_ADJUSTMENT_CLOUD_SERVICES)`
— the cloud-services rebate is always applied. Dollars = billed credits ×
`CREDIT_PRICE_USD` ($3.68) except AI which uses `AI_CREDIT_PRICE_USD`
($2.20); storage uses `STORAGE_USD_PER_TB_MONTH` ($23). Change rates in
Admin → Settings, never in code.

### Overview
- **Window spend** — billed credits in the filter window × rate. Source:
  FACT_METERING_DAILY; live fallback METERING_DAILY_HISTORY (lags ≤24h).
- **MTD vs budget** — month-to-date billed $ vs `MONTHLY_BUDGET_USD`
  (0 = "not configured"; the KPI never invents a denominator).
- **Projected month-end** — see §7 Forecast engines. Shown with its band
  and the engine named in the help text.
- **Open alerts** — COUNT of OPEN `ALERT_EVENTS` by severity.
- **Platform score** — §6.
- **Spend trend** — daily billed $ (mart-first), budget/day rule when a
  budget exists, forecast band shading; sparkline strip beneath = 14-day
  spend / query count / failures (from FACT_QUERY_HOURLY).
- **Top actions** — top 5 OPEN `ACTION_QUEUE` rows ranked by severity, due
  date, estimated dollars (`logic/actions.rank_actions`).
- **Cost drivers** — exec-board mart panel rows.
- **Morning AI digest** — yesterday's DAILY_DIGEST row (§8).
- **Executive summary download** — styled HTML (or plain text) of the
  numbers above.

### Control Room
- **Triage queue** — one ranked list built from open alerts + task
  failures + spend anomalies; rank = severity weight then recency
  (`logic/actions.triage_queue`). Failed tasks show their DATABASE.
- **Telemetry freshness** — `MART_SOURCE_FRESHNESS`: hours since each fact
  loaded; >26h is stale (daily facts legitimately lag up to ~24h + load).
- **Incident correlation timeline** — 7 days of alerts + task failures +
  DDL on one axis; click a row → everything ±30 minutes.
- **Spend movers** — window vs prior window per warehouse
  (`warehouse_window_vs_prior`, lag-offset so both windows are complete).

### Cost & Contract (sections)
- **Spend** — daily billed by service category; KPIs: billed $, cloud-
  services rebate (always shown separately), AI spend at the AI rate.
  **Cloud-services health**: per-warehouse ratio = cloud-services credits ÷
  total credits (24h scan threshold 20%; >10% = WATCH). When ELEVATED, the
  compile-heavy families table explains why (families ≥20 runs averaging
  >0.5s compile).
- **Attribution** — allocated spend by dimension. Warehouse metering is
  exact billing truth; per-user/database attribution allocates each
  warehouse-hour's credits by elapsed-time share and is labeled
  "allocated". Waterfall = top contributors + Other, cumulative.
- **Contract** — pacing: consumed share vs elapsed-time share of
  `CONTRACT_CREDITS` between `CONTRACT_START_DATE`/`END`; pace ratio >1 =
  burning faster than the clock. **Renewal planner**: growth scenarios on
  trailing 30d burn; recommended commit = term consumption × (1+buffer).
- **Chargeback** — department = warehouse owner (`DEPARTMENT_MAP`):
  exact per-department billed credits; role-share within a warehouse as a
  secondary allocated lens; Unmapped bucket reconciles to the account
  total. Monthly statement export.
- **Cortex & Storage** — Cortex daily spend (token-based credits × $2.20),
  storage GB by database × storage rate.
- **AI Users** — per-user Cortex consumption, exceptions (users over the
  per-user expectation), AI budget pacing when `AI_MONTHLY_BUDGET_USD` set.
- **Optimization** — idle advisor (warehouse-hours billed with zero
  queries = auto-suspend opportunity); right-sizing simulator (spill +
  queue profile → size suggestion); toggled scans: repeat-query
  fingerprints (≥10 identical runs = caching/materialization candidates),
  query efficiency (families scanning >80% of ≥100-partition tables;
  zero-scan share trend), storage waste (Time-Travel/failsafe-heavy tables,
  STALE = no DML in 90d); **guarded remediation** (§9); storage growth
  movers.
- **Savings ledger** — every claimed saving with STATE: ESTIMATED (booked
  by remediation/advisor) → VERIFIED or REJECTED by the monthly verifier
  comparing actual before/after spend. The two are never summed together.

### Operations (sections)
- **Queries** — window KPIs (count, fail rate, p95 runtime, queued
  minutes, remote spill GB) mart-first from FACT_QUERY_HOURLY (p95 there
  is *peak hourly cohort* p95 — the help text says so; a schema filter
  switches to live raw p95). Heaviest queries table (click → drill-through:
  full profile of one query).
- **Tasks** — task runs/failures by day (FACT_TASK_DAILY), failure detail
  with DATABASE column, RCA timeline for a selected failure.
- **Warehouses** — daily credits per warehouse, events, concurrency peaks
  (WAREHOUSE_LOAD_HISTORY; sustained PEAK_QUEUED ≳1 = add cluster).
- **Contention** — lock waits (LOCK_WAIT_HISTORY).
- **Release compare** — before/after metric deltas around a chosen date.
- **Change impact** — §9 regression tracker verdicts with run-history
  drill and change-date rule line.
- **Pipeline SLA** — freshness SLAs from PIPELINE_SLA_CONFIG (target
  minutes per table), COPY/Snowpipe failures (7d, with sample errors),
  dynamic-table refresh health, on-demand stream staleness (SHOW STREAMS).

### Security & Governance
Honest framing: hygiene and governance posture, **not** a threat-detection
SOC. **Governance drift score** at top (§6). Sections:
- **Access** — MFA gaps (password-login users without MFA, with login
  evidence), failed logins, break-glass role holders, expiring credentials
  (30d horizon, EXPIRED/EXPIRING), dormant-user scan (toggled; 90d no
  login but roles still granted, severity by age/role count), role grants
  in window, auditor export pack (multi-sheet download).
- **Changes** — recent DDL by database/schema, failed-login reasons
  (network-policy vs credential), break-glass activity trend.
- **Trust Center** — latest findings per scanner (needs
  TRUST_CENTER_VIEWER).

### Alerts
- **Open events** — click a row → drawer: full detail, rule config, that
  rule's recent history, first-response playbook, **Explain with AI** for
  COST_/PERF_ events (§8), Investigate→ (jumps to the owning page/section
  with filters applied), ack/resolve with note (audited). Bulk ack/resolve
  below. **MTTA/MTTR** KPIs = mean minutes RAISED→ACK and RAISED→RESOLVED
  over 90d.
- **Rules** — ALERT_CONFIG: enable/disable, thresholds (SQL generated,
  operator executes).
- **History** — events by day, colored by severity.
- **Native delivery** — ALERT_ROUTES viewer + add-route recipe; generated
  CREATE ALERT templates for native-alert preference.

### Admin
Settings (edit any SETTINGS key with typed confirm) · **Emergency** (§10) ·
Migrations & freshness (SCHEMA_VERSION vs expected 1..15 + drift warning) ·
App self-cost (the app's own queries/failures on WH_ALFA_OVERWATCH) · Org
spend (ORGANIZATION_USAGE currency by account) · Performance (slowest app
statement families by parameterized hash + session cache-hit estimate) ·
Canary (§13) · Errors & telemetry (session + persisted APP_ERROR_LOG).

## 6. Calculated scores

**Platform score** (`logic/scoring.py`) = 100 − Σ capped penalties; every
deduction is listed with evidence. Signals → penalty per unit (SETTINGS
key) [cap]:
over-budget %-points ×`SCORE_PTS_BUDGET_PER_PCT` 0.5 [20] · critical
alerts ×`SCORE_PTS_PER_CRITICAL` 6 [24] · high alerts ×2 [10] · query-fail
% over 2% ×1.5 [12] · task-fail % over 1% ×2 [14] · queued minutes over 10
×0.3 [10] · spill GB over 5 ×0.5 [8] · stale sources ×4 [12] · open high
actions ×1.5 [9]. States: ≥85 Healthy, ≥70 Watch, else Act. Weights are
**uncalibrated starting points** — tune them in Settings against your own
incident history; caps are fixed so no single driver dominates.

**Governance drift score** (`logic/governance.py`) = 100 − Σ capped:
MFA-gap users ×5 [25] · expired credentials ×8 [24] · expiring ×2 [10] ·
break-glass grants 30d ×6 [18] · warehouses without monitor ×4 [12] ·
without auto-suspend ×3 [12]. ≥90 Healthy, ≥75 Watch, else Act. Weights
fixed (drift items are countable facts).

## 7. Forecast engines (`FORECAST_ENGINE` setting)

- **linear** (default): MTD actual + mean of last 28 daily values ×
  remaining days; band = daily std × √remaining.
- **seasonal**: each remaining calendar day projected with its
  day-of-week mean (28d baseline); band from residuals vs weekday means;
  auto-falls back to linear under 14 data points.
- **ml_forecast**: reads `FORECAST_ML_DAILY` (materialized by the opt-in
  `ml_forecast_option.sql`: SNOWFLAKE.ML.FORECAST model + weekly refresh
  task); credits × rate; falls back to seasonal when absent.
Every basis string names the engine in the KPI help.

## 8. AI engines (all grounded, all optional)

Model = `CORTEX_MODEL` setting (default `llama3.1-8b`); all calls are
SNOWFLAKE.CORTEX.COMPLETE inside your account — nothing leaves Snowflake.
Grounding rules enforced in the prompt builders (`logic/ai_prompts.py`):
evidence rows only, hard row/char caps, "answer only from the evidence",
required "inconclusive" escape, word limits.

- **Morning digest** — SP_DAILY_DIGEST (07:20) summarizes exec-board facts
  + alert counts into DAILY_DIGEST; shown in an Overview expander. If
  Cortex is unavailable the digest row says so instead of failing.
- **Evaluation panels** — button-gated "AI evaluation" on release compare,
  task failures, etc.; never auto-run.
- **Pre-explained anomalies** — sweep v3 appends a grounded hypothesis to
  fresh COST_ANOMALY_SWEEP events server-side (capped 5/run) so webhook
  messages arrive explained.
- **Anomaly explanation (on-demand)** — alert drawer, COST_/PERF_ events: assembles
  the event day's evidence (top query families by elapsed-hours vs their
  prior-7-day average, warehouse-scoped) and asks for the 1-2 most likely
  drivers with numbers, or "inconclusive". Operators may append the
  hypothesis to the event (audited UPDATE).

## 9. The find→fix→prove loop

- **Idle advisor / sizing / efficiency scans** find waste (§5 Cost).
- **Guarded remediation** (Cost → Optimization): pick warehouse → generated
  fix (`AUTO_SUSPEND 60` or an off-hours suspend/resume task pair from the
  14-day hour-of-day profile; it refuses to propose when no ≥4h quiet
  window pays) → typed confirm → execute → append-only REMEDIATION_LOG row
  → ESTIMATED savings-ledger item.
- **Savings verifier** (monthly) compares actual before/after spend and
  flips items to VERIFIED or REJECTED. Estimated and verified totals are
  never combined.
- **Change-impact tracker** (V010): any procedure/task change freezes a
  14-day pre-change baseline (runs, fails, median/p95, measured
  credits/call via QUERY_ATTRIBUTION_HISTORY roll-up by ROOT_QUERY_ID) and
  tracks 14 days after → verdicts REGRESSED / IMPROVED / NEUTRAL / PENDING
  / NO_BASELINE / INSUFFICIENT_AFTER; REGRESSED raises PERF_CHANGE_REGRESSION
  (CRITICAL at 2× cost or 50% failure rate). DATABASE_NAME/SCHEMA_NAME are
  first-class columns, so every change is attributable to its schema.

## 10. Emergency levers (Admin → Emergency)

Generate exact validated SQL, type EMERGENCY, execute, audited to
REMEDIATION_LOG. Warehouse-level (your role needs OPERATE/MODIFY):
**SUSPEND/RESUME** (spend kill-switch; running queries finish first — pair
with a statement timeout if something is stuck), **STATEMENT_TIMEOUT_IN_
SECONDS**, **MIN/MAX_CLUSTER_COUNT**, **SCALING_POLICY ECONOMY**,
**RESOURCE_MONITOR attach**. Object-level: **resource monitor CREDIT_QUOTA**
(the hard brake; SUSPEND_IMMEDIATE kills at cap), **ALTER PIPE ...
PIPE_EXECUTION_PAUSED** (ingestion flood), **ALTER TASK <root> SUSPEND**
(runaway graph), **ALTER USER ... DISABLED=TRUE** (compromised
credentials). ACCOUNT-level (run as SNOW_ACCOUNTADMINS; the panel
generates the SQL): **CORTEX_MODELS_ALLOWLIST = 'None'** (AI/Cortex-Code
spend kill-switch; 'All' restores; or pin cheap models),
**STATEMENT_TIMEOUT_IN_SECONDS** account default, **NETWORK_POLICY**
(lockdown — not generated; coordinate first so you don't lock yourself
out). Note: resource monitors do NOT govern serverless/AI spend — the
allowlist is the AI brake.

## 11. Settings reference (Admin → Settings)

CREDIT_PRICE_USD 3.68 · AI_CREDIT_PRICE_USD 2.20 · STORAGE_USD_PER_TB_MONTH
23.00 · MONTHLY_BUDGET_USD 0=off · AI_MONTHLY_BUDGET_USD 0=off ·
CONTRACT_CREDITS / CONTRACT_START_DATE / CONTRACT_END_DATE (ISO dates) ·
CORTEX_MODEL llama3.1-8b · FORECAST_ENGINE linear|seasonal|ml_forecast ·
SCORE_PTS_* (nine platform-score weights, §6) · FACT_RETENTION_DAYS_HOURLY
400 (floor 90) · FACT_RETENTION_DAYS_DAILY 800 (floor 180) ·
ERROR_LOG_RETENTION_DAYS 180 (floor 30). Values are strings; bad numbers
fall back to defaults. Changes take effect within one cache cycle (≤5 min)
or after Refresh.

## 12. Alert engine reference

**Delivery (V018):** `TASK_ALERT_NOTIFY` is created in-chain (AFTER the
scan) and auto-resumes when the `OVERWATCH_WEBHOOK` integration exists; the
Alerts page shows a live status chip (integration / task / last send). The
one-time integration setup — the only step that can't ship in git — is
`snowflake/webhook_delivery.sql`; to resume manually:
`ALTER TASK DBA_MAINT_DB.OVERWATCH.TASK_ALERT_NOTIFY RESUME;`. The morning
digest also sends through the default route (guarded; absent integration =
in-app only). **Storm view:** Open events has a group-by-rule toggle (5
warehouses over budget = 1 row); dedupe semantics unchanged. **Closed loop:**
for warehouse-lever rules the drawer generates the fix inline — confirm,
execute, REMEDIATION_LOG row, ESTIMATED ledger item — and the expander
shows the ledger state of fixes already booked from that event
(ESTIMATED → VERIFIED/REJECTED), so the loop is visible end to end.

**Severity routing recipes:** CRITICAL→PagerDuty + HIGH→Slack are two
integrations and two `ALERT_ROUTES` rows — copy-paste blocks live in
`snowflake/webhook_delivery.sql`. Each route sends through its own named
integration with per-route failure isolation; MIN_SEVERITY is a rank filter
(CRITICAL ⊂ HIGH ⊂ MEDIUM ⊂ LOW).

**ROI (Brief):** "Verified savings (QTD)" = VERIFIED ledger items only,
shown against the app's own quarterly warehouse cost (green = pays for
itself); the open ESTIMATED pipeline is a separate figure by design.

**Isolation (v7):** every rule block runs in its own INSERT with its own
exception handler — a broken rule logs `rule_block_failed` to APP_ERROR_LOG
and raises OPS_SCAN_DEGRADED while every other rule keeps firing.

**Lifecycle:** rule (ALERT_CONFIG row) → scan inserts an event with a
DEDUPE_KEY (no duplicate while the key exists) → OPEN → ACK → RESOLVED,
each transition writing ALERT_AUDIT. Severity escalations are computed at
insert. Webhook delivery batches unnotified OPEN CRITICAL/HIGH (or per
ALERT_ROUTES family/severity → named integration; one failing route never
blocks others).

| Rule | Family | Fires when (threshold = THRESHOLD_NUM, editable) | Recurrence |
|---|---|---|---|
| COST_DAILY_CREDITS | COST | account credits/day over threshold | daily key |
| COST_WH_DAILY_CREDITS | COST | one warehouse's credits/day over threshold | daily per WH |
| COST_BUDGET_PACE | COST | MTD spend ahead of budget pace | daily |
| COST_FORECAST_BREACH | COST | projected month-end over budget | daily |
| COST_CLOUD_SVC_RATIO | COST | warehouse cloud-services ratio > % (24h, ≥0.5 cr) | daily per WH |
| COST_STORAGE_SURGE | COST | database grew > GB day-over-day | per DB per day |
| COST_SERVERLESS_CREEP | COST | non-WH/non-AI service credits up > % WoW (≥5 cr) | weekly while creeping |
| COST_ANOMALY_SWEEP | COST | robust z ≥ threshold vs 28d (warehouse & service series) | per series per day |
| COST_CONTRACT_BREACH | COST | projected exhaustion ≤ threshold days (CRITICAL ≤14) | weekly |
| PERF_QUERY_FAIL_PCT | PERF | window fail % over threshold | daily |
| PERF_QUEUED_MINUTES | PERF | queued minutes over threshold | daily |
| PERF_SPILL_GB | PERF | remote spill GB over threshold | daily |
| PERF_CHANGE_REGRESSION | PERF | changed proc/task worse than frozen baseline | once per change |
| PERF_FINGERPRINT_DRIFT | PERF | family p95 up > % (7d vs prior 28d), no change event; Mondays | weekly per hash |
| PIPE_TASK_FAILURES | PIPELINE | task failures in window over threshold | daily per task |
| PIPE_COPY_FAILURES | PIPELINE | failed/partial file loads 24h (CRITICAL ≥10 files) | daily per table |
| PIPE_DT_FAILURES | PIPELINE | dynamic-table refresh failures 24h (CRITICAL ≥5) | daily per DT |
| SEC_FAILED_LOGINS | SECURITY | failed logins over threshold | daily |
| SEC_CRED_EXPIRY | SECURITY | credential expires ≤ threshold days (CRITICAL if expired) | weekly until rotated |
| SEC_BREAK_GLASS_USE | SECURITY | > threshold statements/day under admin roles | daily per user |
| COST_DEPT_BUDGET_PACE | COST | department MTD > budget pace by threshold % (DEPT_BUDGETS) | daily per dept |
| COST_ORG_ACCOUNT_CREEP | COST | org account currency spend up threshold % WoW | weekly per account |
| PIPE_VOLUME_DROP | PIPELINE | table rows-added down threshold % vs prior-7d avg (≥1k rows/day) | daily per table |
| OPS_CANARY_FAIL | PLATFORM | weekly source sentinel found failing dependency views | daily key |
| OPS_SCAN_DEGRADED | PLATFORM | one or more rule blocks failed in the last scan (v7 isolation) | daily key |
| OPS_SLOW_RENDER | PLATFORM | page p95 first paint > threshold s (7d, from APP_USAGE.RENDER_MS) | weekly per page |

Playbooks for each rule render in the alert drawer (`logic/playbooks.py`).

## 13. Object inventory (DBA_MAINT_DB.OVERWATCH)

**Operator/config tables** (backed up weekly to `*_BAK_LAST`): SETTINGS,
COMPANY_SCOPE, ALERT_CONFIG, ALERT_EVENTS, ALERT_AUDIT (append-only),
ACTION_QUEUE, SAVINGS_LEDGER, DEPARTMENT_MAP, ALERT_ROUTES,
REMEDIATION_LOG (append-only), USER_PREFS, OBJECT_CHANGE_REGISTRY,
PIPELINE_SLA_CONFIG, DAILY_DIGEST.
**Facts (transient, rebuildable, purged by retention):** FACT_METERING_DAILY,
FACT_WAREHOUSE_DAILY, FACT_QUERY_HOURLY, FACT_TASK_DAILY, FACT_LOGIN_DAILY,
FACT_STORAGE_DAILY. **Marts/views:** MART_EXEC_BOARD, MART_SOURCE_FRESHNESS,
control-room snapshot, MART_SPEND_ROLLUP_DT (Dynamic Table pilot).
**Procs:** SP_LOAD_HOURLY_FACTS, SP_LOAD_DAILY_FACTS, SP_REFRESH_EXEC_BOARD,
SP_ALERT_SCAN, SP_NOTIFY_WEBHOOK, SP_DAILY_DIGEST, SP_VERIFY_IDLE_SAVINGS,
SP_CHANGE_IMPACT_SCAN, SP_ANOMALY_SWEEP, SP_PURGE_FACTS,
SP_BACKUP_OPERATOR_TABLES (+ opt-in SP_REFRESH_ML_FORECAST).
**Functions:** COMPANY_FOR_USER. **Tasks:** §4. **Misc:** SCHEMA_VERSION,
APP_ERROR_LOG, FORECAST_ML_DAILY (opt-in).

**Usage analytics disclosure:** `APP_USAGE` records user name, page, first
render time (ms), and timestamp — one row per page change per session, used
only for the Admin adoption/performance panels. Retention is
`APP_USAGE_RETENTION_DAYS` (default 365, floor 90) via the monthly purge.
Tell your users it exists; auditors will ask.

**Canary** (Admin → Canary): runs every registered SQL builder with 1-row
caps against the live account and reports PASS/FAIL — the drift detector
for ACCOUNT_USAGE column changes or missing objects. Run it after every
Snowflake release note that mentions ACCOUNT_USAGE, and after migrations.

## 14. Fallback matrix — what happens when a source is missing

| Missing / stale | Behavior |
|---|---|
| Any FACT_* empty | Panels fall back to bounded live ACCOUNT_USAGE queries, labeled "live fallback"; Overview board falls back to a bounded aggregate |
| MART_EXEC_BOARD stale | Freshness board flags it; overview still paints (fallback aggregate) |
| QUERY_ATTRIBUTION_HISTORY absent | Change-impact tracker logs one APP_ERROR_LOG row and verdicts use runtime + failure rate only |
| TASK_VERSIONS absent | Task change registration skipped; procedures still tracked |
| DYNAMIC_TABLE_REFRESH_HISTORY absent | DT alert block logs and skips; cost sweep unaffected |
| CREDENTIALS view absent | Credentials panel shows setup hint; scan block yields no rows |
| ORGANIZATION_USAGE not granted | Org spend tab shows the grant hint, nothing else breaks |
| TRUST_CENTER not granted | Trust Center section shows the grant hint |
| Cortex/model unavailable | Digest row says so; AI panels surface the error; nothing else breaks |
| FORECAST_ML_DAILY absent | Forecast engine silently uses seasonal, basis string says so |
| Webhook integration missing | SP_NOTIFY_WEBHOOK returns a friendly failure; per-route errors log to APP_ERROR_LOG; events stay queued (NOTIFIED_AT null) |
| ALTER SESSION unsupported (SiS) | Query tags/timeouts no-op; warehouse-level timeout backstops apply |
| Schema/db filters on mart-only panels | Panels that lack the dimension switch to live sources automatically |

## 15. Troubleshooting

**A page shows "not installed yet."** Admin → Migrations: compare
SCHEMA_VERSION to expected 1..15; run what's missing, then roles.sql.

**Everything is stale.** `SHOW TASKS IN SCHEMA DBA_MAINT_DB.OVERWATCH;` —
suspended tasks are the usual cause (a failed run suspends after retries).
`SELECT * FROM TABLE(INFORMATION_SCHEMA.TASK_HISTORY()) ORDER BY
SCHEDULED_TIME DESC` for the error; fix; `ALTER TASK ... RESUME;`.

**Warehouse suspended by the resource monitor.** OVERWATCH_RM hit quota —
raise it (Admin → Emergency → Resource monitor quota) or investigate the
burn first (App self-cost).

**Alert didn't fire.** Rule ENABLED? Threshold sane? DEDUPE_KEY may be
suppressing (by design — see recurrence in §12); resolve the old event or
wait out the period. Scan running? (task history for TASK_ALERT_SCAN.)

**Webhook silent.** Integration exists and TASK_ALERT_NOTIFY resumed?
Route rows ENABLED with the right MIN_SEVERITY? APP_ERROR_LOG shows
`route_send_failed` with the integration name when a single route breaks.

**Canary failures.** Column drift in ACCOUNT_USAGE or a dropped object.
The failing check names the builder; APP_ERROR_LOG has the SQL error.

**Numbers look wrong.** Check the source caption first (mart vs live +
lag). ACCOUNT_USAGE lags ≤45 min (query history) to ≤24h (metering daily);
never compare a half-filled current window to a complete prior one — the
app's comparison queries lag-offset both windows for exactly this reason.

**Arrow/serialization error on a table.** A mixed-type object column from
a new source; wrap the offending column in TO_VARCHAR in its builder (the
pattern used for alert timestamps).

**App slow.** Admin → Performance: the p95 statement families tell you
which builder; check cache-hit %; confirm sections are lazy (only the
active pill runs) and the batch path isn't falling back (telemetry key
`batch_fallback`).

**Migration drift warning in Admin.** The app expects exactly V001..V015;
missing = run them in order; extra = you're on a newer schema than the
deployed app — redeploy the app.

## 16. Disaster recovery

1. **One bad table:** `CREATE OR REPLACE TABLE <T> CLONE <T>_BAK_LAST;`
   (weekly Sunday clone) or Time Travel:
   `CREATE OR REPLACE TABLE <T> CLONE <T> AT(OFFSET => -3600);`
2. **Dropped object:** `UNDROP TABLE/SCHEMA ...` within retention.
3. **Schema gone:** UNDROP first. Otherwise: migrations V001..V015 →
   roles.sql → validate.sql (all OK) → facts refill from loaders (history
   bounded by ACCOUNT_USAGE retention: 365d) → operator tables from
   `*_BAK_LAST` if they survived, else re-seed (SETTINGS rates, budgets,
   contract; DEPARTMENT_MAP names; ALERT_CONFIG thresholds re-seed with
   defaults automatically).
4. **Bad deploy:** `snow streamlit deploy --replace` from the previous git
   tag. Migrations are additive; no schema rollback exists or is needed.
5. **Verify after any recovery:** validate.sql all OK → Admin canary all
   PASS → freshness board green after the next hourly run.

## 17. Glossary

**Billed credits** — usage credits with the cloud-services adjustment
applied; what Snowflake actually invoices. **Allocated** — dollars split
by elapsed-time share (an estimate, always labeled). **Robust z** —
Iglewicz-Hoaglin 0.6745·(x−median)/MAD; outlier-resistant. **Fingerprint /
family** — queries sharing QUERY_PARAMETERIZED_HASH (same SQL shape,
different literals). **Break-glass** — ACCOUNTADMIN / SNOW_ACCOUNTADMINS;
for emergencies and grants, not routine work. **Dedupe key** — string that
makes an alert fire once per object per period. **Mart-first** — read our
small fact tables before the big ACCOUNT_USAGE views. **Operator** —
OVERWATCH_OPERATOR role member; may execute generated statements behind
typed confirms. **Quiet window** — contiguous hours where a warehouse
burns credits with ~no queries. **Verified saving** — ledger item proven
by actual before/after spend, not projection.

---

## §18 — 4.1 → 4.6 additions (2026-07-07 passes)

**New Snowflake objects.** V021: `ALERT_EVENTS.RESOLUTION_KIND`,
`APP_QUERY_TELEMETRY` (+ `TASK_PURGE_QUERY_TELEMETRY`, 90d sliding). V022:
`ALERT_DELIVERIES` per-route ledger + `SP_NOTIFY_WEBHOOK` v3 — fan-out is
per (event, route); a Slack success no longer suppresses PagerDuty; failed
routes retry every chain run inside the 24h window; events aging out
undelivered write a loud `undelivered_expired` error-log row. **V022 has
not run against the live account yet** — apply, re-run roles.sql, then
prove it with the fire drill. Opt-in scripts: `alert_drill.sql` (monthly
synthetic CRITICAL; resolve as EXPECTED; Admin → Canary scores the streak).

**Rule catalogue additions (§12).** `OPS_ALERT_DRILL` (PLATFORM, CRITICAL,
ENABLED=FALSE — the drill task inserts events directly; the scan never
fires it). `WINDOW_HOURS` on every rule is informational: scan windows are
fixed per family in `SP_ALERT_SCAN`; edit thresholds, not windows.

**Alert lifecycle.** Resolutions carry a kind — ACTIONED / NOISE /
EXPECTED. Kinds feed the per-rule precision score and the threshold
suggestions on Alerts → Rules (keep ≥90% of ACTIONED, cut NOISE, basis
stated). Drills and maintenance closures are EXPECTED so they never skew
precision. The drawer's "Re-check condition now" replays supported rules
against today's data before you resolve.

**Trust surfaces (Admin → Canary).** Mart reconciliation (fact totals vs
live ACCOUNT_USAGE; ±2% is late-arrival noise, past ±5% re-run the scoped
backfill), restated-days detector (metering rows changed ≥48h after close),
fire-drill scoreboard, and fleet slow/failed fetch telemetry (Admin →
Performance; ≥2s or failed only, 60/session cap).

**Ops notes.** Query-cache identity is role+user+refresh-salt only — the
same SQL fetched anywhere shares one entry per TTL; "Refresh data"
invalidates and re-resolves role/user. MFA gap has ONE definition
everywhere: password-login evidence within 30d (FACT_LOGIN_DAILY-backed,
live LOGIN_HISTORY fallback). Storage panels read FACT_STORAGE_DAILY
first. Window anchoring convention lives in `app/data/common.py`.

**Code layout.** Cost & Contract sections live in
`app/ui/pages/cost_parts/{spend,contract,ai_chargeback,optimize}.py`;
`cost.py` is dispatch only. Wave-era test locks live under
`tests/history_locks/` (see `tests/README.md`).
