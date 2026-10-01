# OVERWATCH — Operator Runbook

The complete operating manual. Assumes you have never seen this app: it
covers what every number means and how it is calculated, every scheduled
job, every alert rule, the AI engines and their grounding rules, every
database object, what happens when a source is missing, emergency levers,
troubleshooting, and disaster recovery.

**The one-paragraph version:** OVERWATCH is a Streamlit-in-Snowflake app
that watches this Snowflake account's cost, performance, pipelines, and
governance for the two companies sharing it (ALFA and Trexis). Hourly tasks
copy ACCOUNT_USAGE telemetry into small fact tables; hourly and daily scans
raise alert events against ~45 rules and push them to webhooks; the daily scans
catch anomalies, regressions, and drift; the app renders it all with honest
labels and generates (never silently executes) the SQL to fix what it finds.

---

## 1. Ten-minute orientation

- **Brief** is the one-scroll morning page (numbers, fires, asks); **Overview** loads the executive board (one cached mart
  query): MTD spend vs last month, month-end forecast, alerts, platform
  score, top actions.
- **Control Room** is the DBA morning page: triage queue, freshness,
  incident timeline, spend movers.
- The **status strip** at the top of Overview shows account-wide open
  criticals, undelivered criticals, telemetry age (the stalest source) and
  MTD credit spend. Brief renders the same signals in its body, with open
  criticals scoped to the selected company plus account-level events; the
  other pages do not repeat it (owner 2026-08-14).
- The **top filter strip** scopes almost every panel: Company (ALFA
  default), Date range, Database, and More (contains-filters for warehouse /
  user / schema), plus Reset. Filters match literally (`WH_` = literal
  underscore).
- Pages use **section pills** (only the active section runs its queries) and
  every table has a CSV download. `?page=` and `?section=` are shareable.
- **Saved preferences:** a saved default landing view, density and display
  timezone still hydrate at startup from USER_PREFS (the in-strip Views
  editors were dropped in v4.157.0); the sidebar **Audit detail** toggle
  saves the presentation mode.
- Roles: access is **SNOW_ACCOUNTADMINS** and **SNOW_SYSADMINS**, nothing
  else (owner decision 2026-07-13; the old monitor/operator layer is
  retired). Under SiS the navigation profile follows the viewer (`st.user`
  mapped through `config.VIEWER_PROFILES`; an unmapped viewer gets the
  read-only READER profile). Operator actions (viewers on
  `config.OPERATOR_USERS`; account-object ALTERs are re-checked in the
  executor) always show the SQL first. Reversible saves to OVERWATCH's own
  tables (alert ACK and snooze, action create/save, ownership and watchlist
  edits) are one click; classifying or account-touching writes (alert
  resolve and bulk actions, incident declare / mitigate / close, warehouse
  and emergency levers, SETTINGS edits) need a typed confirmation. Alert
  lifecycle actions write ALERT_AUDIT and levers write REMEDIATION_LOG (both
  append-only). The app itself runs with owner's rights — see DEPLOYMENT.md
  §2.

## 2. Architecture

**Layers.** `app/data/*` builds SQL strings only. `app/logic/*` is pure
Python (fully unit-tested; no Streamlit imports). `app/ui/*` renders.
`app/core/*` is the runtime: session, query engine, errors, state.

**Mart-first.** Hourly tasks MERGE ACCOUNT_USAGE into `FACT_*` tables;
pages read facts first and fall back to bounded live ACCOUNT_USAGE queries
with the source always labeled under the table ("mart" vs "live fallback").

**Query engine** (`app/core/query.py`):
- Five cache tiers (TTL seconds / statement timeout seconds):
  live 30/30 · recent 300/120 · hourly 3600/120 · historical 3600/180 ·
  metadata 14400/30.
- Cache key = SQL text + role + refresh salt + the domain salts of the
  tables the SQL reads (+ the viewer for USER_PREFS / CURRENT_USER() reads).
  Errors are never cached (cached functions raise; Streamlit does not cache
  exceptions).
- Row caps fetch n+1 rows and banner truncation; nothing is silently cut.
- `run_batch()` submits a section's queries server-side async in parallel;
  any failure falls back to serial per-query calls.
- "Refresh data" (sidebar) bumps the salt = full cold reload for you only.

**Company scoping** (`app/companies.py`, mirrored in the `COMPANY_SCOPE`
table with a sync test): Trexis = `COMPANY_SCOPE` mapping rows, the
`WH_TRXS_*` warehouses / `TRXS_*` databases, and users holding `%TRXS%`
roles. ALFA needs evidence too (V044): `WH_ALFA_*` warehouses,
`ALFA%` / `ADMIN` (and the app's own `DBA_MAINT_DB`) databases, `%ALFA%` or
DBA roles. Everything else classifies **UNKNOWN** (never NULL) and surfaces
on Cost Intelligence → Spend & Attribution (Unmapped entities) until a
`COMPANY_SCOPE` row maps it, so nothing silently bills ALFA. User `KEBARR1`
holds both companies' roles and is classified **ALFA** by explicit
override. This is a convenience scope on a shared account, not a security
boundary. Who can open the app is USAGE on the Streamlit object
(SNOW_ACCOUNTADMINS + SNOW_SYSADMINS); inside it every query runs with the
owner's rights, so page visibility (`config.VIEWER_PROFILES`) and writes
(`config.OPERATOR_USERS`) are keyed on the viewer.

**Honesty contracts** enforced by tests: no synthetic data anywhere; empty
states say why and what would fill them; estimated vs verified savings
never mix; every AI output is grounded in rows shown to it; every panel
labels its source and lag.

**Streamlit-in-Snowflake specifics:** OVERWATCH is an owner's-rights app:
every query runs with the app owner's privileges (`CURRENT_USER()` /
`CURRENT_ROLE()` are the owner's). The viewer's identity (`st.user`, mapped
through `config.VIEWER_PROFILES`; an unresolved viewer fails closed to the
least-privilege profile) selects only the navigation profile and the
operator gate. `ALTER SESSION` is not available to the app (capability
detected at connect). Streamlit-in-Snowflake stamps every statement the app runs with its own
QUERY_TAG naming the app (`"StreamlitName":"DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP"`) and
overrides any per-statement tag, so OVERWATCH's self-traffic filters key on that tag.
Reads are bounded by the warehouse STATEMENT_TIMEOUT_IN_SECONDS; Cortex evaluations also
send a 1m 30s per-statement timeout (unverified under SiS). The app
and all tasks run on the dedicated XSMALL warehouse **WH_ALFA_ADMIN**
(no resource monitor since v4.45 — OVERWATCH_RM was suspending it mid-use).

## 3. Install / upgrade

Run in order as the deployment role, **SNOW_ACCOUNTADMINS** (or
SNOW_SYSADMINS only if it can create the warehouse and grants — and, on a
fresh install, V002's OVERWATCH_RM resource monitor, which V045 drops; see
DEPLOYMENT.md §1):

| Migration | Creates |
|---|---|
| V001 core | DB context, `SETTINGS`, `COMPANY_SCOPE` (+`COMPANY_FOR_USER()`), `APP_ERROR_LOG`, `SCHEMA_VERSION` |
| V002 facts | `FACT_METERING_DAILY`, `FACT_WAREHOUSE_DAILY`, `FACT_QUERY_HOURLY`, `FACT_TASK_DAILY` (dropped by V043, restored by V045), `FACT_LOGIN_DAILY`, `FACT_STORAGE_DAILY`, loader procs, `WH_ALFA_ADMIN` + `OVERWATCH_RM`, `TASK_LOAD_HOURLY`/`TASK_LOAD_DAILY` |
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
| V015 pilot+backups | `MART_SPEND_ROLLUP_DT` (Dynamic Table pilot), `SP_BACKUP_OPERATOR_TABLES` + Sunday task (retired: V090, V161) |
| V021 precision+telemetry | `RESOLUTION_KIND` precision, `APP_QUERY_TELEMETRY` fleet sink, app self-cost |
| V022 per-route delivery | `ALERT_DELIVERIES` ledger, sender v2 (additive fan-out, honest retries) |
| V023 prod-scoped volume | sweep v4 (PIPE_VOLUME_DROP = PROD DBs only), scan v9 (CREDENTIALS columns) |
| V024 warehouse scorecard | `WAREHOUSE_CONFIG_SNAPSHOT` + `WAREHOUSE_CHANGE_REGISTRY`, `SP_WAREHOUSE_CHANGE_SCAN` + 06:40 task, `WH_CHANGE_REGRESSION` |
| V025 break-glass policy | `SEC_BREAK_GLASS_USE` disabled (ACCOUNTADMIN/SNOW_ACCOUNTADMINS are routine here) |
| V026 teams-safe delivery | sender v3: JSON-escaped payloads (quotes/newlines/tabs); Teams Workflows compatible |

> **This table lists landmark migrations only.** The full chain is **V001 through the repo tip**
> (every file in `snowflake/migrations/`, enumerated in `admin.py` `_EXPECTED_MIGRATIONS`
> and DEPLOYMENT.md §1). Run every migration in order — the V017 predecessor guard
> refuses to skip any. V027..V124 add the mart family (V027), the incident system,
> loader rewrites, the alert-scan split (V062), and the per-table storage mart (V124).

App files deploy to the dedicated stage
**`DBA_MAINT_DB.OVERWATCH.OVERWATCH_STAGE`** (V017; `snowflake.yml` pins it —
see DEPLOYMENT.md for the manual PUT path). V017 also inaugurates the
version guard: each migration refuses to run if its predecessor is missing.

Then `roles.sql` as **SNOW_ACCOUNTADMINS** (GRANT IMPORTED PRIVILEGES on
the SNOWFLAKE database needs ACCOUNTADMIN-tier; idempotent; re-run after
every upgrade) and
`validate.sql` (every row should read OK). Deploy the app with
`snow streamlit deploy --replace`, from a clean, committed tree: it ships
`snowflake.yml`'s artifacts as they are on disk, including the two
snowflake/ templates every viewer can read on Alerts ▸ Native delivery
(DEPLOYMENT.md §3).

**Deploy order and the schema gate (since 4.602).** Every app read of a
column a migration adds, and every caption that describes a migration's new
behaviour, checks that migration is in SCHEMA_VERSION first
(`app/ui/schema_gate.py`; it answers from the startup gate's own
SCHEMA_VERSION read, so it costs no query, and an unreadable version table
keeps the pre-apply behaviour). So the app can be deployed before or after
an apply; the house order is deploy first, then apply. After applying
V162-V165 the gated text and columns appear within 4 h (the metadata cache)
or at once on Refresh; Admin ▸ Migrations reads fresher.

**Opt-in scripts** (run deliberately, not part of the chain):
`webhook_delivery.sql` (notification integration + sender task — Microsoft
Teams needs the Workflows Adaptive-Card recipe in that file, see §19),
`native_alert_templates.sql` (CREATE ALERT equivalents if you prefer
native alerts), `ml_forecast_option.sql` (SNOWFLAKE.ML.FORECAST engine; its
procedure retrains the model on every run — see §7), `backfill_365.sql`
(one-time year of daily facts and 180 days of security facts — run before
ACCOUNT_USAGE history ages out; it suspends the hourly task graph around its
extract-fed loads, each load
is guarded so an error becomes a `FAILED:` row and Run All still reaches the
RESUME, and its last pane's `BACKFILL_CALLS_FAILED` and `LOADER_ARMS_FAILED`
must both read 0. If the worksheet stops early (a timeout or Stop), run its
`ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY RESUME` and
`SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY')`
(the two statements just above the file's final verify SELECT) or
`loader_chain_check.sql` step 0, or the hourly graph stays suspended).
`teardown.sql` is the surgical uninstall (never drops the schema).

## 4. Scheduled automation (all times America/Chicago)

| Task | Schedule / predecessor | Calls | Writes |
|---|---|---|---|
| TASK_LOAD_HOURLY | :07 hourly (hourly root) | SP_LOAD_HOURLY_FACTS | FACT_WAREHOUSE_DAILY (warehouse metering) |
| TASK_QH_EXTRACT | after TASK_LOAD_HOURLY | SP_LOAD_QH_EXTRACT(0) | OW_QH_EXTRACT (staged QUERY_HISTORY) + FACT_QUERY_HOURLY / FACT_QUERY_DAILY + MART_CLOUD_SVC_DAILY (via SP_LOAD_CLOUD_SVC_MART) |
| TASK_REFRESH_EXEC_BOARD | after TASK_QH_EXTRACT (V071) | SP_REFRESH_EXEC_BOARD | MART_EXEC_BOARD |
| TASK_ALERT_SCAN | after TASK_QH_EXTRACT (V071) | SP_ALERT_SCAN (hourly rules) | ALERT_EVENTS |
| TASK_ALERT_NOTIFY | after TASK_ALERT_SCAN | SP_NOTIFY_WEBHOOK | webhook sends, NOTIFIED_AT; V164: CRITICAL escalations (ALERT_EVENTS.ESCALATED_AT + one ALERT_AUDIT ESCALATE row each; re-post + OVERWATCH_EMAIL email, §19) |
| TASK_LOAD_MARTS_V27_HOURLY | after TASK_QH_EXTRACT | SP_LOAD_MARTS_V27('HOURLY', 2) | the V027+ mart family (query families, cost allocation, task graph, security posture, …; compile diet below) |
| TASK_OPS_DIAG_HOURLY | after TASK_QH_EXTRACT | SP_LOAD_OPS_DIAG(2) | MART_OPS_DIAG_HOURLY |
| TASK_LOAD_SECURITY_FACTS | after TASK_LOAD_MARTS_V27_HOURLY | SP_LOAD_SECURITY_FACTS(3) | FACT_SECURITY_LOGIN_DAILY, FACT_SECURITY_CHANGE, SECURITY_TRUST_SNAPSHOT |
| TASK_SLO_BREACH_SCAN | after TASK_LOAD_MARTS_V27_HOURLY | SP_SLO_BREACH_SCAN | ALERT_EVENTS (PERF_SLO_BREACH, §12) |
| TASK_INCIDENT_AUTODECLARE | after TASK_LOAD_HOURLY | SP_INCIDENT_AUTODECLARE | INCIDENTS + INCIDENT_MEMBERS (§21) |
| TASK_CHANGE_ATTRIBUTION | after TASK_LOAD_HOURLY | SP_CHANGE_ATTRIBUTION | WAREHOUSE_CHANGE_REGISTRY.CHANGED_BY (CHANGE_SOURCE is derived from it on read, §21) |
| TASK_LOAD_DAILY | 06:45 daily (daily root) | SP_LOAD_DAILY_FACTS | daily facts (metering, tasks, logins, storage) |
| TASK_NIGHTLY_RECONCILE | after TASK_LOAD_DAILY | SP_NIGHTLY_RECONCILE | re-loads the last 3 days of facts and marts (late-arriving ACCOUNT_USAGE rows) |
| TASK_LOAD_MARTS_V27_DAILY | after TASK_NIGHTLY_RECONCILE (V071) | SP_LOAD_MARTS_V27('DAILY', 3) | the daily-grain marts |
| TASK_PLATFORM_SCORE_DAILY | after TASK_NIGHTLY_RECONCILE (V071) | SP_LOAD_PLATFORM_SCORE(30) | FACT_PLATFORM_SCORE_DAILY |
| TASK_ALERT_SCAN_DAILY | after TASK_NIGHTLY_RECONCILE (V071) | SP_ALERT_SCAN_DAILY (daily rules, split out V062) | ALERT_EVENTS |
| TASK_LOCK_WAIT_DAILY | after TASK_LOAD_DAILY | SP_LOAD_LOCK_WAIT_MART(3) | MART_LOCK_WAIT_DAILY |
| TASK_PATTERN_COST_DAILY | after TASK_LOAD_DAILY | SP_LOAD_PATTERN_COST(3) | MART_PATTERN_COST_DAILY |
| TASK_LOAD_STORAGE_TRUTH | 06:30 daily | SP_LOAD_STORAGE_TRUTH(3) | FACT_STORAGE_ACCOUNT_DAILY |
| TASK_WAREHOUSE_CHANGE_SCAN | 06:40 daily | SP_WAREHOUSE_CHANGE_SCAN | WAREHOUSE_CONFIG_SNAPSHOT + WAREHOUSE_CHANGE_REGISTRY + WH_CHANGE_REGRESSION events |
| TASK_LEDGER_AUTOBOOK | after TASK_WAREHOUSE_CHANGE_SCAN | SP_LEDGER_AUTOBOOK | SAVINGS_LEDGER (auto-booked warehouse changes, §9) |
| TASK_LOAD_OBJECT_COST | 06:45 daily | SP_LOAD_OBJECT_COST(3) | FACT_OBJECT_COST_DAILY |
| TASK_CHANGE_IMPACT_SCAN | 06:50 daily | SP_CHANGE_IMPACT_SCAN | OBJECT_CHANGE_REGISTRY + regression events |
| TASK_LOAD_APP_COST | 06:55 daily | SP_LOAD_APP_COST(3) | FACT_APP_COST_DAILY |
| TASK_ANOMALY_SWEEP | 07:00 daily | SP_ANOMALY_SWEEP (v3) | ALERT_EVENTS: COST_ANOMALY_SWEEP (Cortex-explained DETAIL), PIPE_DT_FAILURES, COST_ORG_ACCOUNT_CREEP, PIPE_VOLUME_DROP, DQ_BREACH, DQ_SCHEMA_DRIFT (via SP_SCAN_SCHEMA_DRIFT, + DQ_SCHEMA_SNAPSHOT) and COST_CLOUD_SVC_ANOMALY (via SP_SCAN_CLOUD_SVC_ANOMALY) + (Mon) PERF_FINGERPRINT_DRIFT |
| TASK_LOAD_TABLE_STORAGE | 07:10 daily | SP_LOAD_TABLE_STORAGE_MART(14) | MART_TABLE_STORAGE_DAILY |
| TASK_DAILY_DIGEST | 07:20 daily | SP_DAILY_DIGEST | DAILY_DIGEST (Cortex) |
| TASK_LOAD_QUERY_OPERATOR_STATS | 07:20 daily | SP_LOAD_QUERY_OPERATOR_STATS(30) | FACT_QUERY_OPERATOR_STATS_DAILY |
| TASK_VERIFY_SAVINGS | 07:40 1st of month | SP_VERIFY_IDLE_SAVINGS | SAVINGS_LEDGER verifications |
| TASK_PURGE_FACTS | 05:20 1st of month | SP_PURGE_FACTS | deletes beyond retention |
| TASK_PURGE_QUERY_TELEMETRY | 06:20 1st of month | inline DELETE (no proc) | APP_QUERY_TELEMETRY rows older than 90 days |
| ~~TASK_BACKUP_OPERATOR~~ | retired V161 | ~~SP_BACKUP_OPERATOR_TABLES~~ | none: scheduled operator backups were removed (Time Travel + manual clones, §16) |
| TASK_CANARY_SENTINEL | 05:30 Mondays | SP_CANARY_SENTINEL | CANARY_RESULTS + OPS_CANARY_FAIL |

These are the 32 tasks the migrations leave live: `ops_sql.OVERWATCH_TASKS`,
`snowflake/task_audit.sql`'s expected set and `tests/test_task_set_contract.py`
are the machine-checked list. The opt-in scripts add TASK_ALERT_DRILL
(`alert_drill.sql`) and TASK_REFRESH_ML_FORECAST (`ml_forecast_option.sql`).

**Notes on the automation:** the Monday 05:30 sentinel deliberately leads
the morning batch, so its warehouse resume is shared, not extra. Its
`EXECUTE IMMEDIATE 'SELECT 1 FROM ' || name` loop is exempt from the sqlsafe
rule because the probe list is a hardcoded array — no user input ever
reaches it. **Scan-proc test strategy:** CI asserts structure (isolated
blocks, per-block dedupe, rule carryover on every regeneration) across both
scan procs — the hourly `SP_ALERT_SCAN` and the daily `SP_ALERT_SCAN_DAILY`
(split out in V062); runtime failures are the sentinel's and
OPS_SCAN_DEGRADED's job — a broken block logs `rule_block_failed` and
self-alerts, which IS the failure-injection test running in production, safely.

**Operator backups (retired V161, owner decision 2026-09-28):** there is no
scheduled operator-data backup any more. V161 dropped TASK_BACKUP_OPERATOR,
SP_BACKUP_OPERATOR_TABLES, the `DBA_MAINT_DB.OVERWATCH_BAK` schema with every
daily V158 generation, the weekly `*_BAK_LAST` copies, `OPERATOR_BACKUP_LOG`,
the BACKUP_KEEP_* settings and the `OPERATOR_BACKUP_DAILY` freshness row.
Recovery is Time Travel plus the manual clones you take before a risky change
(§16). Older `BackupOperatorTables` rows in APP_ERROR_LOG are history. The
Tasks-on-cadence objective and Tasks ▸ SLA leave the retired task out (its
TASK_HISTORY rows would otherwise read "silently stopped" for up to 90 days).

`SHOW TASKS IN SCHEMA DBA_MAINT_DB.OVERWATCH;` — every state should be
`started`. Since V071 the migrations resume TASK_ALERT_NOTIFY with the rest
of the hourly tree whether or not a delivery integration exists (it sends
only through enabled ALERT_ROUTES rows), so a suspended notifier means
someone suspended it; Admin ▸ Migrations & freshness ▸ Task health still
grades it "Suspended (expected)" rather than failing.

**Loader compile diet (V159):** two hourly procs skip work instead of
recompiling a heavy ACCOUNT_USAGE statement every hour. The hourly
`SP_LOAD_MARTS_V27('HOURLY', 2)` runs its three day-grain arms —
`MART_WAREHOUSE_EFFICIENCY_DAILY` [1], `MART_TASK_GRAPH_DAILY` [6] and
`MART_TASK_NODE_DAILY` [6b] — only in the 00/04/08/12/16/20 Central cycles,
and always when DAYS_BACK > 2 (the nightly reconcile's `('HOURLY', 3)`, which
re-loads D-3..today, and backfills). Every other mart still loads hourly.
Today's row in those three marts can be up to 4h old (5h across the
November DST night): the Optimize idle / sizing panels, the Unit costs
task-graph panel, the Operations node-timing board and `SP_SLO_BREACH_SCAN`
see it that much later; completed days are unaffected. Their freshness rows
advance only in those cycles, well inside the 30h DAILY cadence, so they
never read stale. To force them by hand, `CALL SP_LOAD_MARTS_V27('HOURLY', 3)`.
`SP_CHANGE_ATTRIBUTION` runs its QUERY_HISTORY UPDATE only while a
WAREHOUSE_CHANGE_REGISTRY row seen in the last 3 hours is still
unattributed (about three hourly attempts per change, the first ~07:07 after
the 06:40 scan) and otherwise returns `attribution pass skipped`.

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
- **MTD credit spend vs last month** — account-wide month-to-date billed $
  (today included). The delta compares the same number of completed days
  (today excluded, and before the 06:45 Central daily load also yesterday,
  whose metering row is still a partial snapshot) against the prior month;
  no configuration is needed
  (owner 2026-07-13: no monthly-budget KPI). When the prior month has no
  daily facts the card reads plain "MTD credit spend" with no delta. A set
  `MONTHLY_BUDGET_USD` adds a "% of budget" note to this card's help, and a
  separate **Pace vs budget calendar** card (signed variance vs the budget's
  straight-line expected-to-date, completed days only) appears only when
  `MONTHLY_BUDGET_USD` > 0.
- **Projected month-end** — see §7 Forecast engines. Shown with its band
  and the engine named in the help text.
- **Open alerts** — COUNT of OPEN `ALERT_EVENTS` by severity.
- **Platform score** — §6.
- **Spend trend** — daily billed $ as bars (mart-first) with a 7-day
  average line over calendar days (a day with no row counts as $0); the
  caption's weekly pace compares the two calendar weeks ending on the newest
  complete day. Only today's bar (account clock) renders dimmed — metering
  lags up to 24h, so it is partial, not a drop; a window that ends before
  today (Last month) dims nothing. Budget/day rule when a budget exists; the forecast
  range lives in the Projected KPI, not on the chart. Sparkline strip
  beneath = 14-day spend / query count / failures (from FACT_QUERY_HOURLY).
- **Top actions** — top 5 OPEN `ACTION_QUEUE` rows ranked by severity, due
  date, estimated dollars (`logic/actions.rank_actions`).
- **Cost drivers** — exec-board mart panel rows.
- **Morning AI digest** — yesterday's DAILY_DIGEST row (§8).
- **Executive summary download** — styled HTML (or plain text) of the
  numbers above.

### Control Room
Sections: Action Center · Pulse · Incidents & triage · Timeline & movers ·
Freshness & replay · Entity 360.
- **Triage queue** (Incidents & triage) — one ranked list built from open
  alerts + task failures + spend anomalies; rank = severity weight then
  recency (`logic/actions.triage_queue`). Failed tasks show their DATABASE.
- **Telemetry freshness** (Freshness & replay) — reads
  `SOURCE_FRESHNESS_STATE` (each loader stamps its own row on a successful
  load; the `MART_SOURCE_FRESHNESS` view is the pre-V040 fallback). A source
  is STALE past 30h when its name contains DAILY or METERING, and past 3h
  otherwise (`THRESHOLDS`; the same rule as the status strip, Admin's
  freshness list and OPS_PIPELINE_DEGRADED); a never-loaded source reads NOT
  LOADED. The board shows the counts; the per-source hours table is on
  Admin ▸ Migrations & freshness.
- **Incident correlation timeline** (Timeline & movers) — 7 days of alerts +
  task failures + DDL on one axis; click a row → everything ±30 minutes.
- **Spend movers** (Timeline & movers) — window vs prior window per
  warehouse (`warehouse_window_vs_prior`: the window vs the equal-length
  calendar window before it; the trailing presets exclude today, so both
  windows are complete).

### Cost Intelligence (sections)
Sections: Spend & Attribution · Contract & Forecast · Chargeback & AI ·
Unit costs · Compare · Optimization & Savings.
- **Spend** (Spend & Attribution) — daily billed by service category; KPIs: billed $, cloud-
  services rebate (always shown separately), AI spend at the AI rate.
  **Cloud-services health**: per-warehouse ratio = cloud-services credits ÷
  total credits (WATCH above 10%, ELEVATED above 20% — reading bands only:
  the fixed-ratio COST_CLOUD_SVC_RATIO alert was retired in V157, and
  COST_CLOUD_SVC_ANOMALY (V150, daily anomaly sweep) alerts when a
  warehouse's cloud-services credits step outside its own prior-28-day
  robust baseline). When ELEVATED, the
  compile-heavy families table explains why (families ≥20 runs averaging
  >0.5s compile).
- **Attribution** (Spend & Attribution) — allocated spend by dimension. Warehouse metering is
  exact billing truth; per-user/database attribution allocates each
  warehouse-hour's credits by elapsed-time share and is labeled
  "allocated". Waterfall = top contributors + Other, cumulative.
  **Grain coverage** (v4.597, moved from Decision Studio ▸ Cost Truth): one
  caption under the warehouse table shows measured object-query compute
  and user-allocated credits as a % of metered warehouse credits. These are
  separate lenses, not addends. All three come from one cost_truth read (one
  window, one scope), and the caption shows only when all three have data.
  Allocated is the owner-scoped MART_COST_ALLOCATION_DAILY, so per company it
  can exceed 100%; read it under Company = ALL. It is one extra mart read,
  inside the "Load company attribution" toggle only.
- **Storage** (Spend & Attribution, behind the "Load storage &
  unmapped-entity detail" toggle, with the Unmapped entities worklist) —
  storage GB by database × storage rate.
- **Contract** (Contract & Forecast) — pacing: consumed share vs elapsed-time share of
  `CONTRACT_CREDITS` between `CONTRACT_START_DATE`/`END`; pace ratio >1 =
  burning faster than the clock. Consumed counts the term only, up to
  (not including) `CONTRACT_END_DATE`. Once the term is over the section
  shows the final term consumption and the over/unused credits, and
  withholds pace, the projection and the steering levers until the new
  term's three settings are entered. **Renewal planner**: growth scenarios on
  trailing 30d burn; recommended commit = term consumption × (1+buffer).
  **Org accounts spend** (Contract & Forecast; moved from Admin in v4.48):
  ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY billed currency by account and
  service type, 30 days; without ORGANIZATION_USAGE_VIEWER it shows the
  grant hint.
- **Chargeback** (Chargeback & AI) — department = warehouse owner (`DEPARTMENT_MAP`):
  exact per-department billed credits; role-share within a warehouse as a
  secondary allocated lens; Unmapped bucket reconciles to the account
  total. Monthly statement export.
- **Company all-in showback** (Chargeback & AI, below Department
  chargeback) — per company: warehouse metering, the object-cost ledger's
  serverless arms, Cortex Code (Snowsight + CLI) and estimated storage; the
  cloud-services adjustment and the unattributed remainder are account
  rows, so the table ties out to billed metering + estimated storage over
  the complete metered days. On a long Window, a source that starts later
  than the Window leaves its earlier dollars on the unattributed row (the
  notes under the table name it). A negative family residual means the company rows read
  more than that family's metering in the span (different Snowflake views,
  different day boundaries); the note under the table names the family.
  An UNKNOWN row shrinks once a COMPANY_SCOPE mapping lands (Unmapped
  entities on Spend & Attribution): Cortex Code at once, the warehouse,
  object-cost and storage lines as the loaders re-stamp recent days (older
  days keep their stamp until a backfill). The Company-attributed share
  (and a named company's share) is of the spend BEFORE the cloud-services
  adjustment, so the company rows and the unattributed row add up to 100%;
  the all-in total and the tie-out stay after it. Each morning, between the
  06:45 CT metering load and the object-cost / Cortex Code reloads, a note
  says the span's newest day is only partly loaded on those lines (part of
  it sits on the unattributed row until they run); a note that persists
  past mid-morning means that loader is behind (Admin → Migrations &
  freshness). With Company = UNKNOWN, an empty table reads verified-clean
  only when every keyed source covers the span in full.
- **Cortex / AI spend** (Chargeback & AI) — Cortex daily spend
  (token-based credits × $2.20).
- **AI Users** (Chargeback & AI) — per-user Cortex consumption, exceptions (users over the
  per-user expectation), AI budget pacing when `AI_MONTHLY_BUDGET_USD` set.
  **Track top exceptions as work items** (v4.605, operators) writes the
  first 10 Exceptions rows through the same Track statement as Optimize and
  Control Room triage: one Action Center item per user (keyed on the user,
  every signal in its detail, the strongest signal's severity, under the
  user's own company; an unmapped user stays UNKNOWN, so their estimate is
  never summed into a named Company's queue) plus one item for the
  all-users budget breach, keyed on the Company scope and priced only at
  the exposure beyond the user items tracked in the same click. All-users
  items tracked from different Company views are priced separately and
  overlap, so do not add them together. Items land UNASSIGNED, priced
  MONTHLY when priced (the scope item carries no estimate when the user
  items already count its exposure). A user or scope with an open item
  from this page is not queued again, including items queued before v4.605
  under any of the page's three SOURCE names (matched by their old title).
  When a user's strongest signal now outranks every open item of that user
  from this page, the click first raises one of them, the strongest (on a
  tie the item keyed on the user, else the newest), and notes it in its
  detail; the estimate stays as first tracked. A user with several items
  queued before v4.605 keeps them all and only that one is raised, so one
  breach never counts twice in Critical / high; a user who already has an
  open item at that severity is left as is. A Security work item on
  the same user does not block it. An item still open from an earlier
  month now blocks a new one; a done or dismissed item does not.
- **Unit costs** — measured $ per query (QUERY_ATTRIBUTION_HISTORY credits,
  ~8h lag, warehouse idle time excluded), the top 50 stored procedures by
  measured spend with $/call, repeated patterns, AI $ by function/model
  with $/1M tokens, ETL unit costs for tagged pipelines (on demand),
  task-graph pipeline costs and serverless tasks.
- **Compare** — period vs period from facts and marts: last full month
  vs prior, or trailing 7d / 30d vs prior; warehouse movers, pattern
  movers (measured $) and volume shape. Clicking a warehouse row scopes
  the pattern movers to it with a live QUERY_HISTORY x
  QUERY_ATTRIBUTION_HISTORY read, the section's one live scan.
  Panel-local periods replace the global Window; only Company applies.
- **Optimization** (Optimization & Savings) — idle advisor (warehouse-hours billed with zero
  queries = auto-suspend opportunity); right-sizing simulator (spill +
  queue profile → size suggestion; its **Check cluster use** toggle reads
  each multi-cluster warehouse's hourly peak cluster over ≥35 days, and a
  higher MAX_CLUSTER_COUNT is suggested only where queries reached the
  current maximum — otherwise "Size up or split (cluster cap not
  reached)", or "not checked" with the toggle off. The read starts at
  midnight N days back, and its hours are the hours queries STARTED in.
  An empty SHOW WAREHOUSES reads "cluster ranges unknown", not "no
  multi-cluster warehouse"; the Resize picker opens one size up on a
  capacity-pressure verdict where the picker offers one, and otherwise
  (XXLARGE, larger than every option, or an unknown size) with no size
  picked, a note saying why, and no statement until a size is picked;
  a partly-listed profile names the warehouses SHOW did not list);
  toggled scans: repeat-query
  fingerprints (≥10 identical runs = caching/materialization candidates),
  query efficiency (families scanning >80% of ≥100-partition tables;
  zero-scan share trend), storage waste (Time-Travel/failsafe-heavy tables,
  STALE = no DML in 90d; tables nobody read in 90 days feed Addressable
  $/mo as storage waste); **guarded remediation** (§9); storage growth
  movers.
- **Savings ledger** (Optimization & Savings ▸ Remediation & ledger) —
  every claimed saving with STATE: ESTIMATED (booked
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
  (WAREHOUSE_LOAD_HISTORY; sustained PEAK_QUEUED ≳1 = add a cluster —
  on a multi-cluster warehouse raise MAX_CLUSTER_COUNT only if its
  queries reach the current maximum, checked on Cost ▸ Idle & sizing ▸
  Check cluster use; the Sizing & efficiency table says so under the
  table for its unchecked Add a cluster rows). The opener's attention list
  calls queueing "sustained", and ranks it above spend anomalies, only at
  peak queued ≥1 plus ~30 min/day of queued 5-minute intervals across its
  14-day read (84 intervals); a failed concurrency read shows "—", never a
  green "nobody is queueing". Below it, the **Contention** sub-panel (queue,
  spill & lock waits; lock waits from MART_LOCK_WAIT_DAILY, live
  LOCK_WAIT_HISTORY fallback) — Contention is part of Warehouses, not a
  section of its own.
- **Optimize** (v4.597, was Decision Studio ▸ Portfolio) — the
  recurring-query fix queue. Each measured query family gets its observed
  mart $, ONE diagnosis and a first fix: live profile > daily-mart advisor > portfolio
  heuristic > "Validate evidence" / "Profile it". **Track** (operators, in the
  detail pane) is one idempotent ACTION_QUEUE insert keyed on
  QUERY_FINGERPRINT + an open item, so re-clicks never duplicate.
  - Items land UNASSIGNED at MEDIUM (ACT NOW) or LOW, never HIGH.
  - They are unpriced unless the diagnosis is "Stabilize failures" (the
    failed-run share of observed cost, MONTHLY).
  - **Track all ACT NOW** takes up to 25 per click and skips OVERWATCH's own
    traffic, families already open and families dismissed or marked done in
    the last 90 days. If the Action Center status read fails, statuses show
    Unknown and Track all stays off until it reads.
  - The 90-day cooldown has one exception (v4.601): a family marked done
    whose measured outcome (Held?) reads Re-broke or Not fixed is re-admitted
    (Track all still takes only ACT NOW families with a specific diagnosis;
    others re-queue with a single Track). Too early, Unavailable and Not
    checked never lift it, and a dismissal (DROPPED) never does. Held? on a
    Control Room triage item is measured on the signal it was tracked for, and
    only Re-broke prompts a reopen.
  - Control Room triage Track (v4.601) writes UNASSIGNED MEDIUM/LOW unpriced
    items (SOURCE 'Control Room > Triage') for task-failure and warehouse
    spend rows through the same idempotent statement; alerts are never
    tracked (Acknowledge and the incident flow own them).
  - Cost > Chargeback & AI exceptions (v4.605) use the same statement too,
    keyed on the user (USER) or the Company scope (AI_BUDGET, a scope key
    with no Entity 360 page), keeping the exception's severity. A separate
    UPDATE runs first and only raises the severity of an open user item
    from that page when the user's signal has since grown stronger.

  No live read on first paint. The optional live-profile toggle reuses the
  Queries scan (shared cache).
- **Release compare** — before/after metric deltas around a chosen date.
- **Change impact** — §9 regression tracker verdicts with run-history
  drill and change-date rule line.
- **Pipeline SLA** — freshness SLAs from PIPELINE_SLA_CONFIG (target
  minutes per table), COPY/Snowpipe failures (7d, with sample errors),
  dynamic-table refresh health, on-demand stream staleness (SHOW STREAMS).
  **Built-in objectives** (Tonight, v4.597; they replace the retired
  Decision Studio SLO editor) are read-only and never page:
  - "Nightly cycle done by 07:00" = met/judged over the SLA finish forecast's
    newest 14 nights (late/failed/hung are misses; tonight's still-running
    cycle is excluded). It reuses the forecast, so it costs zero reads.
  - "Tasks on cadence" = on-time/total against each task's own cadence
    (late · stale). It is one TASK_HISTORY read shared with Tasks ▸ SLA and
    honors Company/Database/Schema.

  Existing ACTIVE SLO_OBJECTIVES rows still raise PERF_SLO_BREACH; change them
  in Snowsight (see that playbook).

### Security & Governance
Honest framing: hygiene and governance posture, **not** a threat-detection
SOC. **Governance drift score** at top (§6). Sections:
- **Access** — MFA gaps (password-login users without MFA, with login
  evidence), failed logins, break-glass role holders, expiring credentials
  (10d horizon since V028, EXPIRED/EXPIRING), dormant-user scan (toggled; 90d no
  login but roles still granted, severity by age/role count), role grants
  in window, auditor export pack (multi-sheet download: each sheet reads
  up to its own row cap, at most 10,000 rows; MANIFEST.txt states the span
  each windowed sheet covers and marks any sheet that hit its cap
  TRUNCATED).
- **Changes** — recent DDL stacked by change kind (create/alter/drop/
  grants) with a who-changed-most bar beside it, failed-login reasons
  (network-policy vs credential), break-glass activity trend.
- **Clients** — driver/version inventory from ACCOUNT_USAGE.SESSIONS
  (lags ~3h): driver + version from CLIENT_APPLICATION_ID ('(no client
  id)' when empty), PROGRAM from the client-reported CLIENT_ENVIRONMENT
  (VS Code/DBeaver report; many ODBC tools like Erwin do not). Since
  v4.603 (#34) each version is checked against Snowflake's own floor from
  SYSTEM$CLIENT_VERSION_INFO(): UNSUPPORTED (below the minimum supported
  version), NEARING END OF SUPPORT, BELOW RECOMMENDED, OK, NO VERSION,
  NOT LISTED. Snowflake-run rows (the Snowflake Web App / Snowsight
  backend, SnowServices ingress) are Snowflake's to upgrade: neutral, and
  out of the 'yours to upgrade' KPI and the BEHIND count. If the function
  cannot be read, or none of its entries lists a minimum supported version
  (a renamed key reads NULL without an error), support reads 'unavailable',
  the support KPIs show '—', and STATUS (BEHIND = older than the newest
  version of the same driver among your rows) is the fallback. A version
  with no minimum to compare with (NOT LISTED, or NO MINIMUM LISTED: its
  entry lists no minimum, so no verdict is given) is 'not checked' in the
  support KPIs for its own side (yours, or Snowflake-run): those show the count (or '—'
  when nothing could be checked) and are never a green 0, and the caption
  names yours. If no entry lists a nearing-end-of-support or recommended
  version (a renamed key), that KPI shows '—' and the caption says so.
  If the inventory hits the app's row cap, the support KPIs, the upgrade
  list and the behind count are withheld (they would be partial): narrow
  the window or company scope. CSV export on the panel.
- **Trust Center** — latest findings per scanner (needs
  TRUST_CENTER_VIEWER).

### Alerts
- **Open events** — click a row → drawer: full detail, rule config, that
  rule's recent history, first-response playbook, **Explain with AI** for
  the alerts that have a matching evidence pack (§8), Investigate→ (jumps to
  the owning page/section with filters applied), ack/resolve with note
  (audited). Bulk ack/resolve
  below. The tiles above the queue are Open critical / high / total.
- **Rules** — ALERT_CONFIG: enable/disable, thresholds (SQL generated,
  operator executes). The generator opens on the picked rule's current
  threshold and Enabled and its UPDATE sets only what you changed (toggling
  Enabled leaves THRESHOLD_NUM alone); a new threshold of 0 warns that most
  rules would then fire on every row.
- **History** — events by day (30d), colored by severity. **Response
  performance**: alert-grain MTTA (RAISED→ACK) and MTTR (RAISED→RESOLVED),
  event-weighted over the last 4 active weeks of a 90-day read, with
  machine closes (SUPERSEDED / AUTO_CLEARED / SNOOZE_SUPPRESSED /
  CONDITION_ENDED) left out of MTTR; then the incident-grain 90d medians
  (Incident lifecycle, §21), delivery health (SLO), route backlog and alert
  fatigue.
- **Native delivery** — delivery status (TASK_ALERT_NOTIFY state from SHOW
  TASKS on the 5-minute tier; a failed or empty task read says the state is
  unknown, never "suspended"), ALERT_ROUTES viewer + add-route recipe,
  and both snowflake/ templates to view or download — `native_alert_templates.sql`
  (CREATE ALERT equivalents for native-alert preference) and
  `webhook_delivery.sql` (Slack / Teams); both ship with the app as
  `snowflake.yml` artifacts.

### Admin
Settings (edit any key the app reads — `config.DEFAULT_SETTINGS`, incl.
`DEPLOY_ACTORS` — with typed confirm; a SETTINGS row outside that list is
flagged "no longer read (safe to delete)") ·
Migrations & freshness (SCHEMA_VERSION vs the expected V001-to-tip set — admin.py
`_EXPECTED_MIGRATIONS` — with a drift warning, and the on-demand Task health check) ·
Setup progress (one onboarding checklist: migrations applied, marts
loading, budget / contract / route settings) · Metrics (the cost metric
registry: method, grain, source and lag of every cost number) ·
App self-cost (the app's own queries/failures on WH_ALFA_ADMIN) ·
Performance (slowest app statement families by parameterized hash +
session cache-hit estimate) ·
Canary (§13) · Errors & telemetry (session + persisted APP_ERROR_LOG).
The org spend panel moved to Cost Intelligence ▸ Contract & Forecast in
v4.48 (Org accounts spend, §5 Cost Intelligence ▸ Contract).

### Proof
Renamed from Decision Studio in v4.597. It is read-only, and every profile
can open it, including EXECUTIVE. Old Decision Studio links, saved views and
`?page=decision-studio` deep links remap through `navigate.LEGACY_TARGETS`.
- **Proof** — "does OVERWATCH pay for itself", account-wide:
  - Pays for itself = verified active run-rate ÷ the app's trailing-30-day
    run cost. Verified savings run-rate, Saved to date, Added this quarter,
    Realization rate (+ carried realization vs OVERWATCH's own estimate),
    Settling, Acted on, Alert precision and On solid evidence sit beside it.
    The run-rate, Saved to date, Added this quarter, the ROI multiple and the
    attribution split are whole-ledger
    SQL aggregates; realization and the counts come from the newest ≤5,000
    ledger rows (disclosed when that cap binds).
  - Reverts (v4.600): a booked warehouse change (size, auto-suspend, max
    clusters, scaling policy) that a later change made costlier (or whose
    co-attributed partner, seen by the same scan, was undone) leaves the
    run-rate, Added this quarter, the ROI multiple, the attribution split and
    the Brief tile on the first render after the daily scan sees it. It is
    flagged in the evidence table and listed under Reverted savings. Items
    booked by hand are not revert-checked; the 12-month cap still bounds them.
  - Saved to date (v4.600) is dollars, not a run-rate: each verified item's
    monthly saving ÷ 30 × days in place, until it was undone or 12 months
    after verification. The card separates the measured part from the part
    carried forward at the verified rate, and never feeds the ROI multiple.
  - **What each saving rests on** has one row per ledger item: lever,
    target, old → new change, verdict, measured window and flags. Each row
    carries an attribution class: executed by OVERWATCH / recommended by
    OVERWATCH, executed elsewhere / booked in OVERWATCH / detected elsewhere /
    experiment.
  - The $ split comes from uncapped SQL totals. The ROI multiple is
    unchanged; the split is disclosed beside it.
- **Pipeline** — what is ahead:
  - Addressable $/mo (the Cost ▸ Optimization & Savings idle-timer rollup,
    optional right-sizing, and unread maintenance confirmed in Storage & waste
    this session, and unread-table storage waste from that section's
    storage-waste scan) plus queued Action Center work normalized to monthly,
    de-duplicated by entity. A caption names the levers counted and why any
    is missing. If the efficiency mart cannot be read (or has no metering in
    the window), the headline still totals the other counted levers and its
    delta says the idle timer is not counted; it is a dash only when no
    counted lever has an item (a lever counted at $0, such as a clean
    unread-maintenance scan or one whose objects are all already booked,
    still leaves the dash). Unread maintenance drops out on Refresh, a
    credit-rate change, or 1h after Storage & waste was last shown. An object
    booked in another session keeps counting until the scan is re-run at
    least 5m after that booking (the Savings-ledger read that leaves booked
    objects out is cached for up to 5m): at most 1h 5m after the booking.
    Storage waste drops out the same way, but on a storage-rate change rather
    than a credit-rate change. A table counted by both counts once.
  - A projection whose sliders default to MEASURED adoption and realization
    ("Reset to measured"). It runs in a fragment, so slider moves cost no
    reads. Verified savings never enter it.
- **Moved or retired** (v4.597):
  - Portfolio → Operations ▸ Optimize.
  - SLOs → Operations ▸ Pipeline SLA built-in objectives; the editor is
    retired.
  - Cost Truth → the Spend & Attribution grain ratio.
  - Experiments UI and the Action Center experiment expander: retired.
  - Products: hidden.

### Ask
- **Grounded Q&A (DBA only)** — ask in plain English; the answerer routes the
  question to the app's own vetted builders, cites the evidence (USD-grounded),
  and refuses to guess when a source is empty rather than fabricating a "no data"
  answer. No free-text SQL — it composes reviewed reads, not arbitrary queries.

## 6. Calculated scores

**Platform score** (`logic/scoring.py`) = 100 − Σ capped penalties; every
deduction is listed with evidence. Signals → penalty per unit (SETTINGS
key) [cap]:
budget pace: % over budget of the PROJECTED month-end spend
×`SCORE_PTS_BUDGET_PER_PCT` 0.5 [20] (MTD ÷ budget when no forecast is
available) · critical alerts ×`SCORE_PTS_PER_CRITICAL` 6 [24] · high
alerts ×2 [10] · query-fail % over 2% ×1.5 [12] · task-fail % over 1% ×2
[14] · queued minutes per day over 10 ×0.3 [10] · spill GB per day over 5
×0.5 [8] · stale sources ×4 [12] · open high actions ×1.5 [9]. States:
≥85 Healthy, ≥70 Watch, ≥50 Degraded, else At risk. While any critical
alert is open the score is capped at 84 and a "Critical veto" driver says
so. If a required or degraded source fails to load the score reads
**Incomplete** instead of a number (a failed read never improves it).
Weights are **uncalibrated starting points** — tune them in Settings
against your own incident history; caps are fixed so no single driver
dominates.

**Governance drift score** (`logic/governance.py`) = 100 − Σ capped:
MFA-gap users ×5 [25] · expired credentials ×8 [24] · expiring ×2 [10] ·
break-glass grants 30d ×6 [18] · warehouses without auto-suspend ×3 [12].
≥90 Healthy, ≥75 Watch, else Act. Weights are the `GOV_PTS_*` SETTINGS
(defaults shown, editable in Settings); caps are fixed. Resource monitors
are not scored (retired 2026-07-13, owner decision).

## 7. Forecast engines (`FORECAST_ENGINE` setting)

- **linear** (default): complete-day MTD (today's partial actual stays in
  the displayed MTD only) + a robust Theil-Sen daily trend fitted on
  calendar-day offsets over the last 14 complete-day rows, projected over
  today plus each remaining day (each day clamped at 0); the result is
  floored at MTD. Band = residual std (vs that line) × √(today + remaining
  days), widened for parameter uncertainty (√(1+1/n)) and within-week
  autocorrelation (×1.25).
- **seasonal**: today plus each remaining calendar day projected with its
  day-of-week mean over the last 42 complete-day rows; band from residuals
  vs the weekday means, with the same widening. Below 28 points it falls
  back to linear.
- Neither engine projects with fewer than 7 complete days of history (the
  basis reads "Needs at least 7 days of history"). A missing completed day
  this month (no fact row) is filled at the baseline mean when the
  surrounding history is dense, never counted as $0. Complete days end at
  the metering fact's newest row: before the 06:45 Central daily load that
  row (yesterday) is still a partial snapshot, so Overview projects it with
  today instead of counting it, and the basis says so.
- **ml_forecast**: reads `FORECAST_ML_DAILY` (materialized by the opt-in
  `ml_forecast_option.sql`: `SP_REFRESH_ML_FORECAST` retrains the
  SNOWFLAKE.ML.FORECAST model on every complete day, then writes the 45 days
  after it; the weekly task, created suspended, repeats both); MTD actual +
  today's forecast prorated by the hours left + the forecast days to
  month-end, credits × rate; falls back to seasonal when the table is absent,
  when it has no day from today onward (the basis says so and says to
  retrain), or when its days from today onward stop short of month-end (the
  basis names the table's last day and says to retrain). Installed before
  v4.606.0? Re-run `ml_forecast_option.sql` once — the old procedure never retrained, so its
  horizon drained week by week.
Every basis string names the engine in the KPI help.

## 8. AI engines (all grounded, all optional)

Model = `CORTEX_MODEL` setting (default `llama3.1-8b`); all calls are
SNOWFLAKE.CORTEX.COMPLETE inside your account — nothing leaves Snowflake.
Grounding rules enforced in the prompt builders (`logic/ai_prompts.py`):
evidence rows only, hard row/char caps, "answer only from the evidence",
required "inconclusive" escape, word limits.

- **Morning digest** — SP_DAILY_DIGEST (07:20) summarizes exec-board facts
  + alert counts into DAILY_DIGEST; shown in the Brief and Overview
  expanders. Since V165 every figure in the Cortex draft is checked against
  the FACTS it was given (stored on the row with GROUNDING_OK,
  FIGURES_CHECKED and UNGROUNDED); when any figure does not match, or Cortex
  fails, a templated digest built only from the facts is written and sent
  instead, labelled "not AI-written" (BODY_SOURCE = TEMPLATE; the draft stays
  in AI_BODY, never sent). A Cortex failure also logs `digest_ai_failed` to
  APP_ERROR_LOG. The sent text is JSON-escaped like the alert sender. The
  proc's RETURN names the version: `digest written (AI|TEMPLATE[; ...]); sent
  N/M routes`. A frequent TEMPLATE means the model states derived numbers:
  read UNGROUNDED, then consider CORTEX_MODEL. A figure matches within half a
  step of its shown precision, inclusive (1.25 shown as 1.3% or 1.2% passes), or
  0.5%. Until V165 is applied (and on the last pre-V165 row) the digest chip
  reads "Figures not checked" and the caption makes no checking claim.
- **Evaluation panels** — button-gated "AI evaluation" on release compare,
  task failures, etc.; never auto-run.
- **Pre-explained anomalies** — sweep v3 appends a grounded hypothesis to
  fresh COST_ANOMALY_SWEEP events server-side (capped 5/run) so webhook
  messages arrive explained.
- **Anomaly explanation (on-demand)** — alert drawer: assembles the evidence
  pack that matches the alert's metric (cloud-services credits by query
  shape, AI/Cortex spend, a service's daily credits, a query family's
  latency, a warehouse's queueing) and asks for the 1-2 most likely drivers
  with numbers, or "inconclusive". The generic pack (top query families by
  elapsed-hours vs their prior-7-day average, warehouse-scoped) serves only
  daily credits (account / warehouse), remote spill and a warehouse spike
  from the anomaly sweep; any other alert (budget pace / forecast, contract,
  storage, egress, org spend, query failure rate, security ...) shows no
  Explain button rather than off-topic rows. Operators may append the
  hypothesis to the event (audited UPDATE).

## 9. The find→fix→prove loop

- **Idle advisor / sizing / efficiency scans** find waste (§5 Cost).
- **Guarded remediation** (Cost → Optimization): pick warehouse → generated
  fix. `AUTO_SUSPEND 60` → typed confirm → execute → append-only
  REMEDIATION_LOG row; the daily change scan books and settles a tightened
  timer against 14 days of actuals (V038), so the app books an ESTIMATED
  savings-ledger item only for a change the scan can't book (e.g. a first
  timer on a never-suspend warehouse). An off-hours suspend/resume task pair
  (from the 14-day hour-of-day profile; it refuses to propose when no ≥4h
  quiet window pays) is **review-only**: a multi-statement CREATE TASK
  script OVERWATCH never runs (outside the executor allow-list). Run it in a
  worksheet, then an operator's **Book estimated saving** adds one ESTIMATED
  ledger item for that warehouse (a repeat click adds nothing unless the
  earlier item was REJECTED).
- **Savings verifier** (monthly) compares actual before/after spend and
  flips items to VERIFIED or REJECTED. Estimated and verified totals are
  never combined.
- **Query-family fixes** (Operations → Optimize, v4.597): a diagnosis and
  first fix per recurring family → **Track** queues it in Action Center
  (idempotent, keyed on the fingerprint) → assign and work it there.
- **Proof** (v4.597): the verified run-rate vs the app's own run cost, with
  what each saving rests on and who gets credit for it.
- **Change-impact tracker** (V010): any procedure/task change freezes a
  14-day pre-change baseline (runs, fails, median/p95, measured
  credits/call via QUERY_ATTRIBUTION_HISTORY roll-up by ROOT_QUERY_ID) and
  tracks 14 days after → verdicts REGRESSED / IMPROVED / NEUTRAL / PENDING
  / NO_BASELINE / INSUFFICIENT_AFTER; REGRESSED raises PERF_CHANGE_REGRESSION
  (CRITICAL at 2× cost or 50% failure rate). DATABASE_NAME/SCHEMA_NAME are
  first-class columns, so every change is attributable to its schema.

## 10. Emergency levers (Operations → Emergency; on Admin before v4.50)

Generate exact validated SQL, type EMERGENCY, execute, audited to
REMEDIATION_LOG. Warehouse-level (your role needs OPERATE/MODIFY):
**SUSPEND/RESUME** (spend kill-switch; running queries finish first — pair
with a statement timeout if something is stuck), **STATEMENT_TIMEOUT_IN_
SECONDS**, **MIN/MAX_CLUSTER_COUNT**, **SCALING_POLICY ECONOMY**.
Object-level: **ALTER PIPE ...
PIPE_EXECUTION_PAUSED** (ingestion flood), **ALTER TASK <root> SUSPEND**
(runaway graph), **ALTER USER ... DISABLED=TRUE** (compromised
credentials). ACCOUNT-level (run as SNOW_ACCOUNTADMINS; the panel
generates the SQL): **CORTEX_MODELS_ALLOWLIST = 'None'** (AI/Cortex-Code
spend kill-switch; 'All' restores; or pin cheap models),
**STATEMENT_TIMEOUT_IN_SECONDS** account default, **NETWORK_POLICY**
(lockdown — not generated; coordinate first so you don't lock yourself
out). (Resource-monitor levers removed in v4.45 — the owner runs none;
the Cortex allowlist and statement timeouts are the spend brakes.)

Statement-timeout posture (v4.601, Operations → Warehouses → Sizing &
efficiency): the panel's script is review-only. Delete the lines for ETL
warehouses that legitimately run long; each ALTER's undo is a comment. The
alert drawer's closed-loop "Statement timeout 1h" only ever tightens: it
re-reads the warehouse value on a 30-second tier before it generates the
ALTER, and when the warehouse is already capped at 1h or tighter it shows a
note, not SQL. Since v4.603 it also reads what a 1h cap would have cancelled
on that one warehouse in the last 30 days: when that is any completed
statement (or the read failed, so it is unknown) the ALTER is withheld until
you tick the override. Take that seriously on hour-plus ETL warehouses
(WH_TRXS_TRANSFORM ran 27 statements of 1h or more in 30 days, any status,
2026-09-29; one was a timeout cancel, so the drawer, which counts completed
statements only, shows at most 26). The posture table's "Fired at" is the
ceiling a timeout cancel actually fired at; "below cap" means the lowest
ceiling that fired is under the warehouse's effective cap, so a lower ceiling
fired: a user, session, client or task value, or an earlier, lower warehouse
or account value (the message gives the number of seconds, not which of these
set it; the cap shown is today's value and the window reaches back 30-90
days). "Managed compute" rows
(COMPUTE_SERVICE_WH*) are Snowflake's serverless-task and upgrade pools:
nothing to grant or set there. In the Emergency lever, a warehouse timeout of
0 is Snowflake's 7-day maximum, not "no cap", and a lower session/account
value still applies. A manual savings
verify is not prefilled when other booked changes share the measured window;
split the measured change by hand.

Maintenance on objects nobody reads (v4.601, Cost → Optimization & Savings →
Storage & waste): book the estimated saving only after the reviewed ALTER has
run in a worksheet (OVERWATCH never runs it). The row stays ESTIMATED because
no scan settles object-level serverless savings; verify it by hand on the
Savings ledger with its PROOF_SQL (maintenance credits after the booking
day, through the ledger's newest loaded day, against the booked monthly
baseline; MONTHLY_CREDITS_NOW stays NULL until a day after booking has
loaded). Before DROP SEARCH OPTIMIZATION, capture DESCRIBE SEARCH
OPTIMIZATION ON <table>: a bare ADD SEARCH OPTIMIZATION does not restore
per-column methods. A later RESUME RECLUSTER / ADD SEARCH OPTIMIZATION / MV
RESUME shows up only by re-running that proof.

## 11. Settings reference (Admin → Settings)

CREDIT_PRICE_USD 3.68 · AI_CREDIT_PRICE_USD 2.20 · STORAGE_USD_PER_TB_MONTH
23.00 · MONTHLY_BUDGET_USD 0=off · AI_MONTHLY_BUDGET_USD 0=off ·
CONTRACT_CREDITS / CONTRACT_START_DATE / CONTRACT_END_DATE (ISO dates) ·
CORTEX_MODEL llama3.1-8b (saved trimmed and lower-case; blank = the default) · FORECAST_ENGINE linear|seasonal|ml_forecast ·
SCORE_PTS_* (nine platform-score weights, §6) · FACT_RETENTION_DAYS_HOURLY
400 (floor 90) · FACT_RETENTION_DAYS_DAILY 800 (floor 365, raised from 180
in V054) · ERROR_LOG_RETENTION_DAYS 180 (floor 30) · APP_USAGE_RETENTION_DAYS
365 (floor 90) (the floors are enforced in SP_PURGE_FACTS, and the Admin editors start at them) ·
INCIDENT_AUTO_DECLARE_CRITICAL
TRUE (hourly auto-declare switch; the two V162 identity rules never
auto-declare either way) · AI_RUNAWAY_ROBUST_Z 3.5 and
AI_RUNAWAY_INCLUDE_FUNCTIONS FALSE (COST_AI_USER_RUNAWAY, V163; the cap
multiple is the rule's THRESHOLD_NUM, the cap is COCO_DAILY_CAP_CREDITS) ·
ESCALATE_AFTER_MIN 120 (0 = off) and ESCALATE_EMAIL_INTEGRATION
OVERWATCH_EMAIL (blank = no email leg; recipients = that integration's
DEFAULT_RECIPIENTS, set in Snowsight, never stored here) (V164, §19) ·
DEPLOY_ACTORS '' (comma list of deploy service users whose warehouse changes
read MANAGED; §21 Attribution — deleting a populated row flips them to
MANUAL) · CREDIT_PRICE_OVERRIDE FALSE (read only by validate.sql: set TRUE to run a
CREDIT_PRICE_USD other than 3.68 on purpose, else validate fails with -20013;
not seeded, and Admin never lists it as safe to delete). Values
are strings; bad numbers fall back to defaults. Changes take effect within
one cache cycle (≤5 min) or after Refresh; a retention change applies at the next
monthly purge (TASK_PURGE_FACTS).

## 12. Alert engine reference

**Delivery (V018 → V070 / V071 / V112 / V165):** `TASK_ALERT_NOTIFY` runs
in-chain AFTER `TASK_ALERT_SCAN` and is resumed with the hourly tree (V071).
V070 retired V018's `OVERWATCH_WEBHOOK`-only resume gate (it never fired on
this Teams-only account) and disabled any enabled route whose integration
does not exist. Delivery is live only when an ENABLED `ALERT_ROUTES` row
names an integration that exists (on this account
`OVERWATCH_WEBHOOK_TEAMS`); the Alerts page shows a live status chip (route
integration / task / last send). The one-time integration setup — the only
step that can't ship in git — is `snowflake/webhook_delivery.sql`; to
resume manually: `ALTER TASK DBA_MAINT_DB.OVERWATCH.TASK_ALERT_NOTIFY
RESUME;` (never re-run V018: its CREATE OR REPLACE would put back the
retired digest body). The morning digest (SP_DAILY_DIGEST, V165) is written
in-app first, then posted to every ENABLED route with DELIVER_DIGEST that is
not CRITICAL-only (V070 / V112). A failed send logs `digest_send_failed` for
that route, and `digest_undelivered` when no eligible route received it;
with no eligible route the digest stays in-app only. **Storm view:** Open
events has a group-by-rule toggle (5 warehouses over budget = 1 row); dedupe
semantics unchanged. **Closed loop:**
for warehouse-lever rules the drawer generates the fix inline — confirm,
execute, REMEDIATION_LOG row, ESTIMATED ledger item — and the expander
shows the ledger state of fixes already booked from that event
(ESTIMATED → VERIFIED/REJECTED), so the loop is visible end to end.

**Severity routing recipes:** CRITICAL→PagerDuty + HIGH→Slack are two
integrations and two `ALERT_ROUTES` rows — copy-paste blocks live in
`snowflake/webhook_delivery.sql`. Each route sends through its own named
integration with per-route failure isolation; MIN_SEVERITY is a rank filter
(CRITICAL ⊂ HIGH ⊂ MEDIUM ⊂ LOW).

**ROI (Brief):** "Verified savings run-rate" = the monthly run-rate of VERIFIED
ledger items verified in the last 12 months and not reverted (never estimates), shown against the
app's own trailing-30-day warehouse cost (green = pays for itself). It does not
reset when a quarter starts; "verified this quarter" lives in the help text and on
Proof ▸ Proof. The open ESTIMATED pipeline is a separate figure by design.

**Isolation (v7):** every rule block runs in its own INSERT with its own
exception handler — a broken rule logs `rule_block_failed` to APP_ERROR_LOG
and raises OPS_SCAN_DEGRADED while every other rule keeps firing.

**Cadence gates (V157, compile diet):** the hourly SP_ALERT_SCAN reads the
Central hour once per run and skips these blocks outside their slots —
SEC_CRED_EXPIRY [10], SEC_NEW_EXPOSURE [20] and their condition-ended clears
run at 01, 05, 09, 13, 17 and 21 Central; the OPS_PIPELINE_DEGRADED [22]
self-watch at 02, 05, 08, 11, 14, 17, 20 and 23 (the daily scan's copy still
runs every morning). A skipped arm compiles nothing and counts as ok in the
14-block tally (12 before V162); a failed hour read runs every gated block (fail-open,
`cadence_gate_failed`). The trade: those alerts and clears can arrive up to
~4h (~3h for [22]) later than an every-hour check. A condition that begins and
ends between two checks is never raised: a PUBLIC grant revoked within ~4h, or
a stale-source / idle-notifier episode that clears between [22] slots (logged
loader failures are still caught by the 24h ERR leg). A hand
`CALL SP_ALERT_SCAN()` obeys the same gates: outside a slot it skips those arms
and still reports 14/14 ok, so verify a fix to one of them in the 05 or 17
Central hour (both slots) or after its next scheduled slot. Every other arm and sweep
still runs every hour, including the two identity arms V162 added: [26]
SEC_LOGIN_TAKEOVER and [27] SEC_ADMIN_GRANT are ungated.

**Lifecycle:** rule (ALERT_CONFIG row) → scan inserts an event with a
DEDUPE_KEY (no duplicate while the key exists) → OPEN → ACK → RESOLVED,
each transition writing ALERT_AUDIT. Severity escalations are computed at
insert. Webhook delivery batches unnotified OPEN CRITICAL/HIGH (or per
ALERT_ROUTES family/severity → named integration; one failing route never
blocks others). **Machine closes** (excluded from precision/MTTR): SUPERSEDED
(V067 escalation), AUTO_CLEARED (V091 hysteresis — the 3 PERF rules only since
V157), SNOOZE_SUPPRESSED (V117) and CONDITION_ENDED (V157: an OPEN
SEC_CRED_EXPIRY / SEC_NEW_EXPOSURE event once ACCOUNT_USAGE shows the credential
rotated/removed or the PUBLIC grant batch fully revoked; ≥1h dwell, checked in the
4-hourly security slot so a clear lands up to ~4h after the evidence; ACK'd and
snoozed events are left for a human; and, since V160, an OPEN COST_SLEEP_POLLING event at
the weekly check once its poller stops, bills under the clear level or is idle on the
window's last 4 complete days — ACK'd events stay with a human there too).

**Rolling back V157 (order matters).** FIRST switch the two security rules' auto-clear
flag off, by hand in a worksheet (never inside a migration):
`UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG SET AUTO_CLEAR_ENABLED = FALSE WHERE RULE_ID IN ('SEC_CRED_EXPIRY','SEC_NEW_EXPOSURE');`
Only THEN re-run V141's `CREATE OR REPLACE PROCEDURE ... SP_ALERT_SCAN()` and
`... SP_ALERT_SCAN_DAILY()`. Reversed, an hourly TASK_ALERT_SCAN that lands between
the two steps runs V141's unscoped V091 sweep, which resolves every OPEN
SEC_CRED_EXPIRY / SEC_NEW_EXPOSURE event as AUTO_CLEARED 1h after raise, and V141's
arms [10]/[20] never re-raise an auto-cleared key: expiring credentials and new
PUBLIC grants silently leave the queue.

**Rolling back V160.** Re-run V157's `CREATE OR REPLACE PROCEDURE ... SP_ALERT_SCAN_DAILY()` (the tally goes back to 11 and nothing calls SP_SCAN_SLEEP_POLLING any more). Optionally disable the rule (`UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG SET ENABLED = FALSE WHERE RULE_ID = 'COST_SLEEP_POLLING';`), close its lingering OPEN, ACK'd or SNOOZED events as EXPECTED, and drop the proc (and, if wanted, the transient SLEEP_POLLING_WEEKLY table) with the teardown.sql lines. The weekly scan never runs at apply time; to re-check a week by hand, `CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_SLEEP_POLLING(TRUE);` (it can raise events and email).

**Rolling back V161.** First, within the dropped schema's retention (at most 1 day for a transient schema; `SHOW PARAMETERS LIKE 'DATA_RETENTION_TIME_IN_DAYS' IN DATABASE DBA_MAINT_DB`), run `UNDROP SCHEMA DBA_MAINT_DB.OVERWATCH_BAK;`. It must come before V158, whose `CREATE ... IF NOT EXISTS` would otherwise take the name (if it already did, `ALTER SCHEMA DBA_MAINT_DB.OVERWATCH_BAK RENAME TO OVERWATCH_BAK_NEW;` first). It brings back the generations, and also the ledger and the weekly copies, which V161 had moved INTO that schema: move them back before V158 creates empty ones, `ALTER TABLE DBA_MAINT_DB.OVERWATCH_BAK.<name> RENAME TO DBA_MAINT_DB.OVERWATCH.<name>;` for OPERATOR_BACKUP_LOG and each `<T>_BAK_LAST`. If the ledger's move had fallen back to a DROP (PART B V161.13 showed a fourth CRITICAL row), run `UNDROP TABLE DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG;` instead. Then re-run V015's TASK_BACKUP_OPERATOR block (lines 61-67 only: the whole file would re-create the retired MART_SPEND_ROLLUP_DT) and V158 in full, which brings back the task, the proc, the BACKUP_KEEP_* settings and the view carve-out. Redeploy app 4.597.0 as well (`snow streamlit deploy --replace` from main commit `0c8afb7`): 4.598 hides the task from Tasks ▸ SLA, has no BACKUP_KEEP_* editors (it lists them as unread settings), and its validate.sql FAILs the restored objects. Past the retention window the dropped generations are gone for good.

**Rolling back wave 4 (V162-V165).** Each migration re-derives its procs once and rolls back by re-running its base proc. V165, V164 and V163 each roll back on their own; V162 does not: roll V163 back before it (V163's [07] text points at the hourly SEC_LOGIN_TAKEOVER that V162 adds), and bringing V154's autodeclare back needs a 24-hour wait or a data step first (below). To undo the whole wave, go in reverse apply order (V165, V164, V163, V162). None of these rollbacks may run inside a migration. App 4.602.0 keeps working after any of them: every wave-4 read and caption is gated on its migration being in SCHEMA_VERSION, and the version rows stay, so a rolled-back proc can leave a caption that overclaims until the app is redeployed from an earlier tag.

**Rolling back V165.** Re-run V112's `CREATE OR REPLACE PROCEDURE ... SP_DAILY_DIGEST()` (V112__daily_digest_skips_paging_routes.sql, lines 26-143). The six DAILY_DIGEST columns can stay; new rows then carry NULLs, which the app shows as "Figures not checked". That also brings back the unescaped Teams send and the "Digest unavailable" body on a Cortex failure. Nothing runs at apply time; a hand `CALL DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST();` spends a Cortex call and posts to Teams.

**Rolling back V164.** Soft: Admin > Settings `ESCALATE_AFTER_MIN` = 0; the next hourly run skips the escalation pass (Alerts > Native delivery reads "Escalation is off"). Hard: re-run ONLY V064's SP_NOTIFY_WEBHOOK block (V064__webhook_drain_watermarks_alert_burn_telemetry.sql lines 74-351), never the whole file, which would also roll back SP_LOAD_DAILY_FACTS, SP_NIGHTLY_RECONCILE and SP_ALERT_SCAN_DAILY. That restores the old `[SEV] title` lines. `ALERT_EVENTS.ESCALATED_AT` and the two settings can stay; app 4.602 still works because its escalation line reads ALERT_AUDIT, not the column.

**Rolling back V163.** Re-run V160's `CREATE OR REPLACE PROCEDURE ... SP_ALERT_SCAN_DAILY()` (the second procedure in V160__sleep_polling_alert.sql, lines 401-1263): the tally goes back to 12, the two arms stop raising and the old [07] text returns. Optionally disable the two rules in Alerts > Rules (or `UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG SET ENABLED = FALSE WHERE RULE_ID IN ('COST_AI_USER_RUNAWAY','SEC_TRUST_REGRESSION');`) and close their lingering events as EXPECTED. The two SETTINGS rows can stay.

**Rolling back V162 (order matters).** Roll V163 back first (or accept that its [07] text keeps pointing at SEC_LOGIN_TAKEOVER). Then:

1. Re-run V157's `CREATE OR REPLACE PROCEDURE ... SP_ALERT_SCAN()` (V157__alert_scan_self_watch_idle_push.sql lines 121-1128, never the whole file, which would also put SP_ALERT_SCAN_DAILY back to V157's text and drop V160's and V163's daily arms): the tally goes back to 12 and SEC_LOGIN_TAKEOVER / SEC_ADMIN_GRANT stop raising. This is usually enough: V162's SP_INCIDENT_AUTODECLARE only narrows what it does for those two rules, so it can stay.
2. Only if V154's `CREATE OR REPLACE PROCEDURE ... SP_INCIDENT_AUTODECLARE()` (lines 54-235) must come back too, clear the way FIRST. V154's crit CTE has no rule exclusion and reads every OPEN or ACK CRITICAL of the last 24 hours, so a CRITICAL takeover raised before step 1 and still open would be auto-declared by the next hourly TASK_INCIDENT_AUTODECLARE. Either wait at least 24 hours after step 1, or resolve the lingering events as EXPECTED (SNOOZED included: a snooze wakes to OPEN):

   ```sql
   UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
      SET STATUS = 'RESOLVED', RESOLUTION_KIND = 'EXPECTED',
          RESOLVED_AT = CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ
    WHERE RULE_ID IN ('SEC_LOGIN_TAKEOVER', 'SEC_ADMIN_GRANT') AND STATUS IN ('OPEN', 'ACK', 'SNOOZED');
   ```

   RESOLVED_AT is a Central NTZ clock like every other ALERT_EVENTS stamp; `CONVERT_TIMEZONE` keeps it Central even
   in a UTC worksheet (a bare `CURRENT_TIMESTAMP()` would store the session's wall clock, 5-6 hours ahead there).

   Then re-run V154's procedure. Running the scan first only keeps out the events raised between the two steps; it does not stop the auto-declare on its own.

The two ALERT_CONFIG rows can stay; disable them in Alerts > Rules if wanted.

| Rule | Family | Fires when (threshold = THRESHOLD_NUM, editable) | Recurrence |
|---|---|---|---|
| COST_DAILY_CREDITS | COST | account credits/day over threshold | daily key |
| COST_WH_DAILY_CREDITS | COST | one warehouse's credits/day over threshold | daily per WH |
| COST_BUDGET_PACE | COST | MTD spend ahead of budget pace | daily |
| COST_FORECAST_BREACH | COST | projected month-end over budget | daily |
| ~~COST_CLOUD_SVC_RATIO~~ | COST | retired at V157 (wave-2b compile diet) — COST_CLOUD_SVC_ANOMALY (V150, daily: a warehouse's cloud-services credits step outside its own 28-day robust baseline) supersedes the fixed ratio; open, acknowledged and snoozed events were closed as EXPECTED; the WATCH/ELEVATED bands stay on Cost > Spend for reading | — |
| COST_CLOUD_SVC_ANOMALY | COST | a warehouse's daily cloud-services credits (MART_CLOUD_SVC_DAILY, ≥1 credit/day) at a robust z ≥ threshold (3.5) against its own trailing 28-day median/MAD, a spike or a collapse, the last 3 complete days scored; MEDIUM, HIGH at ≥2x the threshold — SP_ANOMALY_SWEEP → SP_SCAN_CLOUD_SVC_ANOMALY, V150 | per warehouse per day |
| COST_STORAGE_SURGE | COST | database grew > GB day-over-day | per DB per day |
| COST_SERVERLESS_CREEP | COST | non-WH/non-AI service credits up > % WoW (≥5 cr) | weekly while creeping |
| COST_AI_CREEP | COST | AI/Cortex credits (FACT_METERING_DAILY service types matching CORTEX / AI / INTELLIGENCE / COCO / COWORK) in the 7 newest complete days up > threshold % (50) vs the 7 before, with ≥5 credits this week, priced at AI_CREDIT_PRICE_USD (a brand-new AI workload reads 999%); MEDIUM, company ALL — daily [13b], V061 | weekly while creeping |
| COST_ANOMALY_SWEEP | COST | robust z ≥ threshold vs 28d (warehouse & service series) | per series per day |
| COST_CONTRACT_BREACH | COST | projected exhaustion ≤ threshold days (CRITICAL ≤14) | weekly |
| COST_IDLE_OPPORTUNITY | COST | a settings-verified AUTO_SUSPEND tightening recovers ≥ threshold USD/month (net of the 60s resume tail, 14 complete days, ≥7 covered; HIGH at ≥5x) — daily scan, V157 | weekly per WH |
| COST_SLEEP_POLLING | COST | a poller (warehouse x user, or task owner role) slept via SYSTEM$WAIT on ≥5 of the 7 newest complete days and billed ≥ threshold USD/week (Spend-panel billed basis; HIGH at ≥5x) — daily scan [25] → SP_SCAN_SLEEP_POLLING, once per ISO week, V160 | one event per poller per episode; CONDITION_ENDED when it stops |
| COST_AI_USER_RUNAWAY | COST | one user's AI credits on one complete day > threshold x COCO_DAILY_CAP_CREDITS (2 x 15 by default) AND a robust z ≥ AI_RUNAWAY_ROBUST_Z (3.5) against their own active days in the prior 90 (fewer than 5 such days = no baseline: the cap alone decides); Cortex Code only unless AI_RUNAWAY_INCLUDE_FUNCTIONS (inert until Functions spend is booked to a user); HIGH; company = the user's, ALL when unmapped — daily [28], V163 | per user per day; the last 3 complete days re-checked each morning |
| COST_EGRESS_SPIKE | COST | DATA_TRANSFER_HISTORY outbound ≥ threshold GB (100) in the last 24h (the event names the 14-day daily average and the top destination region); MEDIUM, company ALL — daily [19], V043 | daily key |
| PERF_QUERY_FAIL_PCT | PERF | window fail % over threshold | daily |
| PERF_QUEUED_MINUTES | PERF | queued minutes over threshold | daily |
| PERF_SPILL_GB | PERF | remote spill GB over threshold | daily |
| PERF_CHANGE_REGRESSION | PERF | changed proc/task worse than frozen baseline | once per change |
| WH_CHANGE_REGRESSION | WAREHOUSE | a warehouse setting change regressed against its frozen pre-change baseline within its 14-day tracking window: credits/day up > threshold % (15) and ≥1 credit/day, or p95 up 25% and ≥30s, or failure rate up 5 points, or queueing up 50% and ≥10 min/day; HIGH, CRITICAL at 2x credits/day — SP_WAREHOUSE_CHANGE_SCAN (06:40 daily), V024 / V109 | once per warehouse, setting and change day |
| PERF_FINGERPRINT_DRIFT | PERF | family p95 up > % (7d vs prior 28d), no change event; Mondays | weekly per hash |
| PERF_SLO_BREACH | PERF | an existing ACTIVE SLO_OBJECTIVES row in BREACH (STALE / NO_DATA and samples under 5 observations excluded); HIGH, CRITICAL at ≥2x error-budget burn — SP_SLO_BREACH_SCAN (TASK_SLO_BREACH_SCAN, after the hourly mart load), V085 / V096 | per objective per day per burn band (a same-day HIGH→CRITICAL gets its own key) |
| PIPE_TASK_FAILURES | PIPELINE | task failures in window over threshold | daily per task |
| PIPE_COPY_FAILURES | PIPELINE | failed/partial file loads 24h (CRITICAL ≥10 files) | daily per table |
| PIPE_DT_FAILURES | PIPELINE | dynamic-table refresh failures 24h (CRITICAL ≥5) | daily per DT |
| PIPE_ETL_TASK_FAILED | PIPELINE | a workflow's tasks failed on their final attempt tonight (≥ threshold, never below 1; HIGH for the terminal workflow; auto-clears once every retried task has finished clean — a retry still running keeps it open) — V156, via the V157 scan arm in the cycle run window (Central hours of ETL_SLA_TARGET_HHMM − 10h through target + 3h, plus a 15:00 pass): a daytime re-run failure, or the auto-clear of its retry, lands at the 15:00 pass or the window start, up to ~6h later | per workflow per night |
| PIPE_ETL_CYCLE_NOT_STARTED | PIPELINE | cycle starter silent past last week's same-night kickoff + threshold min (the Tonight *Cycle start: Overdue* test) — V156, via the V157 scan arm in the cycle run window (ETL_SLA_TARGET_HHMM − 10h through + 3h Central, plus 15:00) | per missed night |
| PIPE_ETL_CYCLE_LATE | PIPELINE | terminal unfinished within threshold min of ETL_SLA_TARGET_HHMM, or projected past the hard deadline (WARN); past the target (CRIT) / hard deadline (EXH): HIGH when the cycle already finished, CRITICAL (auto-declares an incident) when still unfinished; a terminal task is done at its first clean finish from an attempt that STARTED at/after the night's last kickoff, so a next-morning terminal re-run never re-grades the night and an afternoon attempt started before the real kickoff is never that finish (a next-morning starter re-run, or any re-run when starter = terminal workflow, re-grades it: loud); after an afternoon re-run of the whole chain, a real cycle that hangs before its terminal dispatches, or a chain whose terminal starts after the kickoff, can hide the real run (documented) — V156, via the V157 scan arm in the cycle run window (ETL_SLA_TARGET_HHMM − 10h through + 3h Central, plus 15:00; a hard deadline more than ~3h after the target is judged at the 15:00 pass) | per night per band |
| SEC_FAILED_LOGINS | SECURITY | failed logins over threshold on one day (nightly; yesterday and today are read, and today's row is the partial ~06:45 load and says 'so far'); since V163 the title and detail say whether the day also had a successful login — none reads as a lockout or a stale secret, a burst that ended in a success is SEC_LOGIN_TAKEOVER (hourly, V162, while enabled); Account-takeover candidates either way | daily per user |
| SEC_CRED_EXPIRY | SECURITY | credential expires ≤ threshold days — 10 by default since V028 (CRITICAL if expired); checked every 4h since V157 (01, 05, 09, 13, 17, 21 Central), so an event — EXPIRED included — can arrive up to ~4h late | once per band per expiry date (EXPIRING, then EXPIRED); a rotated credential's next expiry re-alerts even after a human resolve, however late (V157: a closed event blocks only its own expiry date, read from its DETAIL; a live one always blocks) |
| SEC_NEW_EXPOSURE | SECURITY | a new grant to PUBLIC (24h lookback) of ≥ threshold objects in one batch; checked every 4h since V157 (01, 05, 09, 13, 17, 21 Central); a grant revoked before the next check is never raised | once per grant batch (PRIVILEGE, GRANTED_ON, CREATED_ON); auto-clears as CONDITION_ENDED once the whole batch is revoked (V157) |
| SEC_LOGIN_TAKEOVER | SECURITY | ≥ threshold (5) failed logins by one user within 15 min, then a successful login within 60 min of that burst (every failed login counts); CRITICAL when the login is off-hours (20:00-06:00 Central, or a weekend) or the user directly held ACCOUNTADMIN / SECURITYADMIN / SYSADMIN / USERADMIN / ORGADMIN / SNOW_ACCOUNTADMINS / SNOW_SYSADMINS at that moment, else HIGH; company ALL — hourly [26], V162; never auto-declares an incident (SP_INCIDENT_AUTODECLARE skips it: declare by hand) | one event per episode (key ends in the anchor login's UTC millisecond time); a later WARN→CRIT crossing supersedes the WARN, a CRIT is never re-minted as WARN; a snooze never carries to the next episode |
| SEC_ADMIN_GRANT | SECURITY | a direct grant of one of those seven admin-tier roles to a user (GRANTS_TO_USERS, 26h lookback), raised even when already revoked; flat HIGH; the title flags off-hours and first-time grants; company ALL — hourly [27], V162; never auto-declares an incident | one event per grant (grantee, role, CREATED_ON) |
| SEC_NEW_ADMIN_NETWORK | SECURITY | a user with a direct ACCOUNTADMIN / SNOW_ACCOUNTADMINS / SNOW_SYSADMINS grant logs in from a CLIENT_IP first seen in the last 24h of a 90-day window, with ≥ threshold (1) logins from it; HIGH, company ALL — hourly [18], V043 | once per user and IP |
| `SEC_POSTURE_<METRIC>` | SECURITY | an operator-created posture monitor (Security's generate-upsert; not seeded; severity chosen when it is created): the newest MART_SECURITY_POSTURE_DAILY value of its METRIC_NAME is ≥ threshold and at most 2 days old — hourly [21], V087 | per rule, company and posture day |
| ~~SEC_BREAK_GLASS_USE~~ | SECURITY | retired at V034 (muted since V025) — admin-role activity stays as evidence on Security -> Changes | — |
| SEC_TRUST_REGRESSION | SECURITY | a CRITICAL or HIGH Trust Center scanner's at-risk count rose ≥ threshold (1) against its previous snapshot day (today's and yesterday's rows checked each morning; a scanner's first snapshot never raises; quiet without TRUST_CENTER_VIEWER); HIGH, company ALL — daily [29], V163 | per scanner per snapshot day (the counts of the scan that raised it; a further rise the same day is not pushed again); no self-clear |
| COST_DEPT_BUDGET_PACE | COST | department MTD > budget pace by threshold % (DEPT_BUDGETS) | daily per dept |
| COST_ORG_ACCOUNT_CREEP | COST | org account currency spend up threshold % WoW | weekly per account |
| PIPE_VOLUME_DROP | PIPELINE | table rows-added down threshold % vs prior-7d avg (≥1k rows/day) | daily per table |
| DQ_BREACH | PIPELINE | a registered table's latest rows-added load is a robust-z outlier (spike or drop, z ≥ threshold 3.5) against its own loads over 28 days — the same series as the Operations data-quality panel; MEDIUM — SP_ANOMALY_SWEEP, V132 | per table per load day |
| DQ_SCHEMA_DRIFT | PIPELINE | a table registered as an OBJECT entity has columns added, removed or retyped since its latest prior daily snapshot (a first snapshot is the baseline and never alerts); MEDIUM — SP_SCAN_SCHEMA_DRIFT from the sweep, V133 | per table per day |
| PIPE_REF_GAP | PIPELINE | ≥ threshold (1) source codes in one check missing from the XLAT reference table (SP_SCAN_REF_GAPS; the nightly load would fail on them; a check whose name has a character other than letters, digits, spaces and - _ . : / is skipped, which the Operations panel warns about); HIGH — daily add-on [17], not counted in the scan tally, V129 | per check per day |
| DQ_RECON_ERROR | PIPELINE | RECON_MTRC_ERROR shows source-vs-target mismatches inside the rule's window (48h) on ≥ threshold (1) metrics; HIGH — daily add-on [18] (SP_SCAN_RECON_ERRORS), not counted, V137 | daily key |
| OPS_CANARY_FAIL | PLATFORM | weekly source sentinel found failing dependency views | daily key |
| OPS_SCAN_DEGRADED | PLATFORM | one or more rule blocks failed in the last scan (v7 isolation) | daily key |
| OPS_PIPELINE_DEGRADED | PLATFORM | pipeline self-watch in BOTH scans (V157): a SOURCE_FRESHNESS_STATE row past its cadence (DAILY/METERING 30h, else 3h; incl. the ALERT_SCAN_HOURLY / ALERT_SCAN_DAILY heartbeats), a loader failure logged and swallowed, or the notifier idle 3h while a route is enabled; the hourly scan checks every 3h (02, 05, …, 23 Central), the daily scan every morning; a stale or idle episode that ends between checks is not raised | per source per last-load day; per failure type/source/day |
| OPS_SLOW_RENDER | PLATFORM | page p95 first paint > threshold s (7d, from APP_USAGE.RENDER_MS) | weekly per page |

Playbooks for each rule render in the alert drawer (`logic/playbooks.py`).

## 13. Object inventory (DBA_MAINT_DB.OVERWATCH)

**Operator/config tables** (no scheduled backup since V161: Time Travel plus
manual clones before a risky change, §16): SETTINGS,
COMPANY_SCOPE, ALERT_CONFIG, ALERT_EVENTS, ALERT_AUDIT (append-only),
ACTION_QUEUE, SAVINGS_LEDGER, DEPARTMENT_MAP, ALERT_ROUTES,
REMEDIATION_LOG (append-only), USER_PREFS, OBJECT_CHANGE_REGISTRY,
WAREHOUSE_CHANGE_REGISTRY, WAREHOUSE_CONFIG_SNAPSHOT, PIPELINE_SLA_CONFIG,
DAILY_DIGEST, DEPT_BUDGETS, INCIDENTS, INCIDENT_MEMBERS, ACTION_ACTIVITY,
EVIDENCE_LINKS, ENTITY_CATALOG, USER_WATCHLIST, OPTIMIZATION_EXPERIMENTS,
SLO_OBJECTIVES (all 25).
**Facts (transient, rebuildable, purged by retention):** FACT_METERING_DAILY,
FACT_WAREHOUSE_DAILY, FACT_QUERY_HOURLY, FACT_TASK_DAILY, FACT_LOGIN_DAILY,
FACT_STORAGE_DAILY. **Marts/views:** MART_EXEC_BOARD, MART_SOURCE_FRESHNESS,
control-room snapshot, and the V027+ scheduled-task mart family (the
MART_SPEND_ROLLUP_DT Dynamic-Table pilot was retired in V090).
**Procs:** SP_LOAD_HOURLY_FACTS, SP_LOAD_DAILY_FACTS, SP_REFRESH_EXEC_BOARD,
SP_ALERT_SCAN, SP_NOTIFY_WEBHOOK, SP_DAILY_DIGEST, SP_VERIFY_IDLE_SAVINGS,
SP_CHANGE_IMPACT_SCAN, SP_ANOMALY_SWEEP, SP_PURGE_FACTS
(+ opt-in SP_REFRESH_ML_FORECAST).
**Functions:** COMPANY_FOR_USER. **Tasks:** §4. **Misc:** SCHEMA_VERSION,
APP_ERROR_LOG, FORECAST_ML_DAILY (opt-in).

**Usage analytics disclosure:** `APP_USAGE` records the viewer's user name
and a timestamp for: one row per page entry (with first-render ms), a ~10%
sample of same-page reruns, each section or sub-view shown on a page entry or
chosen (`section_visit`/`subsection_visit` + the section label), operator
actions (acks, resolves, exports, remediations), and Ask questions
(`ask_answered`/`ask_failed` with the answer type, or `ask_refused` with an
8-word lower-cased stem of the question, digits masked). `APP_ERROR_LOG.CONTEXT`
records the viewer name, app build and a short code-location traceback per app
error (`ERROR_LOG_RETENTION_DAYS`, default 180, floor 30). A failed usage write
logs only the INSERT's target, column list and row count, never the row values.
Read by Admin (DBA profile), and by each viewer's own "since your last visit"
opener (their last-activity time only). Retention `APP_USAGE_RETENTION_DAYS`
(default 365, floor 90). Tell your users it exists; auditors will ask.

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
| ORGANIZATION_USAGE not granted | Cost Intelligence ▸ Contract & Forecast: the org balance and Org accounts spend panels show the grant hint; nothing else breaks |
| TRUST_CENTER not granted | Trust Center section shows the grant hint |
| Cortex/model unavailable | The morning digest sends the templated facts digest and logs `digest_ai_failed` (V165); AI panels surface the error; nothing else breaks |
| FORECAST_ML_DAILY absent, empty from today on, or stops before month-end | Forecast engine uses seasonal, basis string says so (a table with days from today on that stops short: its last day + "retrain it with SP_REFRESH_ML_FORECAST"; a table with no day from today on: "no row for today or later" + the same retrain hint) |
| Webhook integration missing | SP_NOTIFY_WEBHOOK returns a friendly failure; per-route errors log to APP_ERROR_LOG; events stay queued (NOTIFIED_AT null) |
| ALTER SESSION unsupported (SiS) | SiS stamps its own app QUERY_TAG on every statement (self-traffic keys on it); the warehouse-level timeout is the backstop for reads; Cortex also sends a 1m 30s per-statement timeout |
| Schema/db filters on mart-only panels | Panels that lack the dimension switch to live sources automatically |

## 15. Troubleshooting

**A page shows "not installed yet."** Admin → Migrations: compare
SCHEMA_VERSION to the expected set (V001 through the repo tip, admin.py `_EXPECTED_MIGRATIONS`); run what's missing, then roles.sql.

**Everything is stale.** `SHOW TASKS IN SCHEMA DBA_MAINT_DB.OVERWATCH;` (or Admin ▸ Migrations &
freshness ▸ Task health) —
suspended tasks are the usual cause (a failed run suspends after retries).
`SELECT * FROM TABLE(INFORMATION_SCHEMA.TASK_HISTORY()) ORDER BY
SCHEDULED_TIME DESC` for the error; fix; `ALTER TASK ... RESUME;`.

**~~Warehouse suspended by the resource monitor.~~** Retired v4.45:
OVERWATCH_RM is gone (it was suspending the app warehouse mid-use — the
owner correction). A suspended WH_ALFA_ADMIN now means someone ran the
suspend lever (Operations → Emergency) or an account-level change; resume it
and check App self-cost for the burn.

**Alert didn't fire.** Rule ENABLED? Threshold sane? DEDUPE_KEY may be
suppressing (by design — see recurrence in §12); resolve the old event or
wait out the period. Scan running? (task history for TASK_ALERT_SCAN.)

**Webhook silent.** Integration exists and TASK_ALERT_NOTIFY resumed?
Route rows ENABLED with the right MIN_SEVERITY? APP_ERROR_LOG shows
`route_send_failed` with the integration name when a single route breaks.

**Canary failures.** Column drift in ACCOUNT_USAGE or a dropped object.
The failing check names the builder, and the ERROR column on the canary
page has the SQL error (see the logging note below).
One conditional exception: cortex.code_token_types also FAILs (its error
names TOKENS_GRANULAR) on accounts whose Cortex Code views predate that
optional column. That is expected only if the CoCo efficiency review has
never shown token types on the account; if it has, the column was renamed
or dropped: fix cortex_sql.cortex_code_token_types.
The three POLICY_REFERENCES checks (security.data_policy_coverage,
masking_environment_parity, admin_network_policy_coverage) are not declared
gaps: if the app's role cannot read that view they FAIL here while Security
shows a calm needs_setup. IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE
(snowflake/roles.sql) covers that view. A renamed column FAILs here too,
and Security then shows a red "unavailable" with the error (v4.605).
Logging: a drift or absence FAIL (invalid identifier, does not exist,
unknown function) is not written to APP_ERROR_LOG, because the canary runs
as probe reads; only timeouts, privilege errors and other failures are
logged there. The results last only for the session: copy them.

**A red "unavailable" on an optional panel** (v4.605). A probe read shows
needs_setup only when the object is missing or not granted (or the function
does not exist). "Insufficient privileges" shows needs_setup too, like "does
not exist or not authorized", but it proves the object exists: a probe read
logs it to APP_ERROR_LOG, the canary FAILs it (never GAP), the Overview
health score reads it as a failed read (Incomplete, never a zero penalty),
and Admin → Setup progress marks the row Unknown with a re-apply-the-grants
FIX. A missing column (schema drift), a timeout or any other
failure shows "unavailable" with the error in its detail expander. A probe
read does not write a missing column to APP_ERROR_LOG, so that expander is
the only record: copy the error, then run Admin → Canary. That helps only
when the panel's builder is registered in app/data/canary.py (it then FAILs
there on drift). Every ACCESS_HISTORY column the app reads is covered there
too, as a FAIL: this account is Enterprise. Since v4.608 the six
ORGANIZATION_USAGE readers (cost.org_*) and the optional QUERY_INSIGHTS view
are registered as declared gaps: absent, they read GAP; a renamed column
FAILs. Two probe readers are still not registered: the SHOW-based reads
(EXPLAIN cannot compile SHOW) by design, and email_notification_history
and object_tag_probe; for those the expander error is the only record.
A timeout usually clears on a retry; drift does not (apply the missing
migrations, or redeploy). Admin → Setup progress marks a checklist row
Unknown (not Pending) when its read fails this way: FIX says Retry for a
timeout and names the schema drift for a missing column, and the
"could not be checked" line's Error detail lists each failed read's error.
An ACCESS_HISTORY read (Entity 360 blast radius, Proof consumer reach) names
the edition or role only when the view is absent; this account is
Enterprise, so a timeout there says it timed out.

**Numbers look wrong.** Check the source caption first (mart vs live +
lag). ACCOUNT_USAGE lags ≤45 min (query history) to ≤24h (metering daily);
never compare a half-filled current window to a complete prior one — the
app's comparison queries use complete calendar days (today excluded) for
exactly this reason.

**Arrow/serialization error on a table.** A mixed-type object column from
a new source; wrap the offending column in TO_VARCHAR in its builder (the
pattern used for alert timestamps).

**App slow.** Admin → Performance: the p95 statement families tell you
which builder; check cache-hit %; confirm sections are lazy (only the
active pill runs) and the batch path isn't falling back (telemetry key
`batch_fallback`).

**Migration drift warning in Admin.** The app expects V001 through its own tip
(admin.py `_EXPECTED_MIGRATIONS`). Missing = run them in order. Newer than this build = the database is
ahead of the deployed app: every Admin section shows a redeploy warning naming the versions — redeploy with
`snow streamlit deploy --replace` from the revision those migrations came from. Then check Admin ▸
Migrations & freshness ▸ Task health for suspended or failing tasks.

## 16. Disaster recovery

There are **no scheduled operator-data backups** since V161 (owner decision
2026-09-28). Recovery is Time Travel, UNDROP, and the manual clones you take
before a risky change. Know the window before you need it:
`SHOW PARAMETERS LIKE 'DATA_RETENTION_TIME_IN_DAYS' IN TABLE DBA_MAINT_DB.OVERWATCH.<T>;`
No migration sets it, so the account default applies. The TRANSIENT operator
tables (ALERT_EVENTS, ACTION_QUEUE and others; `SHOW TABLES` shows the kind)
keep at most 1 day and have no Fail-safe, so a bad edit to them must be undone
the same day.

1. **One bad table:** undo it with Time Travel and INSERT OVERWRITE, to an hour
   ago or to just before a known bad statement:
   `INSERT OVERWRITE INTO <T> SELECT * FROM <T> AT(OFFSET => -3600);`
   `INSERT OVERWRITE INTO <T> SELECT * FROM <T> BEFORE(STATEMENT => '<query_id>');`
   Run it as the table-owner role: INSERT OVERWRITE deletes, and roles.sql
   revokes DELETE on ALERT_AUDIT / REMEDIATION_LOG from both admin roles. It
   keeps the table's DDL, grants and audit seal. Past the retention window the
   only other source is a manual clone:
   `INSERT OVERWRITE INTO <T> SELECT * FROM <T>_BAK_<yyyymmdd>;`
   Never CLONE-restore: a TRANSIENT clone cannot clone back into a permanent
   table, and a re-materialized table re-applies the schema FUTURE grants.
   A clone taken before a migration that added columns has fewer columns, so
   `SELECT *` from it fails: ALERT_EVENTS gained ESCALATED_AT at V164, and
   DAILY_DIGEST six columns at V165. Restore those with an explicit column
   list, or add the column(s) to the clone first (for example
   `ALTER TABLE ALERT_EVENTS_BAK_<yyyymmdd> ADD COLUMN ESCALATED_AT TIMESTAMP_NTZ;`).
2. **Before a risky change** (a bulk edit, a rebuild, a factory reset): take
   the manual clones first, with today's date suffix, and check their row
   counts: `snowflake/rebuild/00_backup_operator_data.sql` (edit its suffix) or
   teardown.sql B0. Clone as `CREATE TRANSIENT TABLE ... CLONE`: a permanent
   clone of a transient table fails. They are the only copy outside Time
   Travel; drop them once the change proves out.
3. **Dropped object:** `UNDROP TABLE/SCHEMA ...` within retention.
4. **Schema gone:** UNDROP first (`UNDROP SCHEMA DBA_MAINT_DB.OVERWATCH;`); it
   brings back every table in it, manual clones included. Past retention the
   operator data is gone with the schema (the manual clones lived in it too):
   1) Apply every migration in order, V001 onward. Before V006, run
      `CREATE ROLE IF NOT EXISTS OVERWATCH_MONITOR;` and
      `CREATE ROLE IF NOT EXISTS OVERWATCH_OPERATOR;`: V006-V008 grant to
      these retired roles, roles.sql drops them again, and rebuild/02 runs
      both lines first. If your role lacks CREATE ROLE, create them as a role
      that has it and drop them with that role before roles.sql (whose own
      DROPs would otherwise stop it before its first grant). V002 sets
      WH_ALFA_ADMIN's STATEMENT_TIMEOUT_IN_SECONDS back to 300 and attaches
      OVERWATCH_RM (30 credits a month, SUSPEND at 100%) in place of any
      monitor until V045 sets RESOURCE_MONITOR to NULL and drops it. The
      warehouse is account-level and survives the dropped schema, so this
      hits its live settings. Record them first with
      `SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN WAREHOUSE WH_ALFA_ADMIN;`
      and `SHOW WAREHOUSES LIKE 'WH_ALFA_ADMIN';` (its resource_monitor), and
      put the timeout back afterwards
      (`ALTER WAREHOUSE WH_ALFA_ADMIN SET STATEMENT_TIMEOUT_IN_SECONDS = <value>;`,
      or `UNSET` if it showed no warehouse-level value). Expect no monitor
      (owner decision); the replay detaches any monitor, so if it named one
      other than OVERWATCH_RM, ask the owner before re-attaching it
      (`ALTER WAREHOUSE WH_ALFA_ADMIN SET RESOURCE_MONITOR = <monitor>;`). If
      the run stops between V002 and V045, detach the monitor before anything
      else (`ALTER WAREHOUSE WH_ALFA_ADMIN SET RESOURCE_MONITOR = NULL;` then
      `DROP RESOURCE MONITOR IF EXISTS OVERWATCH_RM;`; docs/FULL_REBUILD.md
      steps 0, 3 and 3b). V158's tail starts a backup
      run seconds before V161, which waits up to about 4 minutes for it. If V161
      still stops ("V161 stopped: a TASK_BACKUP_OPERATOR run was still in
      flight", or a statement timeout), re-run it once that run shows a final
      state in TASK_HISTORY.
   2) Re-enter what matters: SETTINGS rates, budgets and contract;
      DEPARTMENT_MAP names; routes. ALERT_CONFIG thresholds re-seed with
      defaults automatically.
   3) roles.sql → validate.sql (all OK) → facts refill from the loaders
      (history bounded by ACCOUNT_USAGE retention: 365d).
5. **Bad deploy:** `snow streamlit deploy --replace` from the previous git
   tag. Migrations are additive; no schema rollback exists or is needed.
6. **Verify after any recovery:** validate.sql all OK → Admin canary all
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
member of SNOW_ACCOUNTADMINS / SNOW_SYSADMINS (DBA profile); may execute
generated statements behind typed confirms. **Quiet window** — contiguous hours where a warehouse
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
synthetic CRITICAL; resolve as EXPECTED; Admin → Canary scores the streak:
consecutive calendar months (account time), counted back from the month whose
drill is due now (this month once the 1st's 09:00 CT run plus a 1h grace has
passed, else last month), each with a drill both delivered and
acknowledged — a failed month or a month with no drill ends it).

**Rule catalogue additions (§12).** `OPS_ALERT_DRILL` (PLATFORM, CRITICAL,
ENABLED=FALSE — the drill task inserts events directly; the scan never
fires it). `WINDOW_HOURS` is informational for every rule except
`DQ_RECON_ERROR`: scan windows are fixed per family in `SP_ALERT_SCAN` /
`SP_ALERT_SCAN_DAILY`, so edit thresholds, not windows. `DQ_RECON_ERROR`'s
`WINDOW_HOURS` is the reconciliation look-back `SP_SCAN_RECON_ERRORS` reads
(default 48h, named in the alert text).

**Alert lifecycle.** Resolutions carry a kind — ACTIONED / NOISE /
EXPECTED. Kinds feed the per-rule precision score and the threshold
suggestions on Alerts → Rules (keep ≥90% of ACTIONED, cut NOISE, basis
stated — it counts only tagged resolutions with a metric value, shows the
untagged count, and adds a caveat when untagged closes outnumber tagged
ones). Drills and maintenance closures are EXPECTED so they never skew
precision. The drawer's "Re-check condition now" replays supported rules
on the alert's own basis before you resolve: daily credits and the
cloud-services ratio since account-midnight; query fail %, queued time
and remote spill over the trailing 24h of FACT_QUERY_HOURLY (what the scan
and its auto-clear sweep read). A daily-credits alert for an earlier,
closed day re-checks only today's partial day, so it can read "Still over"
but never "Condition clear".

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

**Code layout.** Cost Intelligence sections live in
`app/ui/pages/cost_parts/{spend,contract,ai_chargeback,optimize}.py`;
`cost.py` is dispatch only. Wave-era test locks live under
`tests/history_locks/` (see `tests/README.md`).

## §19 Microsoft Teams delivery (Workflows) — setup & troubleshooting

Teams retired the O365 incoming-webhook connectors; channel webhooks are now
Power Automate **Workflows** URLs (`prod-XX.*.logic.azure.com/...`), and they
require an Adaptive Card envelope — `{"text": "..."}` fails inside the flow
("text card" error). Setup lives in `snowflake/webhook_delivery.sql` v2: a
`OVERWATCH_WEBHOOK_TEAMS` integration whose `WEBHOOK_BODY_TEMPLATE` wraps
`SNOWFLAKE_WEBHOOK_MESSAGE` in the card envelope, plus an `ALERT_ROUTES` row.
The file is safe to re-run: the route is added only when no row names that
integration (a route you disabled is never re-added), and an unedited copy
stops at the placeholder guard before it can overwrite the live secret or
recreate the integration.

Symptoms → fixes:
- `route_send_failed` hourly with a Teams route → integration still uses the
  `{"text"}` template: recreate it with the Adaptive-Card template (re-run
  the setup, then `SHOW GRANTS ON INTEGRATION OVERWATCH_WEBHOOK_TEAMS` —
  CREATE OR REPLACE drops its grants).
- Deliveries fail with webhook/HTTP errors after the Workflows URL was
  regenerated (alert_pipeline_check.sql STEP 4 / FIX C) → rotate the secret
  only: the ROTATION RUNBOOK step of `webhook_delivery.sql`, one
  `ALTER SECRET DBA_MAINT_DB.OVERWATCH.OVERWATCH_TEAMS_URL SET SECRET_STRING = '...'`
  pasted in Snowsight (never into the file). The integration, its grants
  and ALERT_ROUTES stay as they are; do not re-run the whole file for a
  rotation.
- Every card arrives twice → a duplicate `OVERWATCH_WEBHOOK_TEAMS` route
  left by an older, unguarded re-run of the setup: keep the oldest enabled
  `ALERT_ROUTES` row and set `ENABLED = FALSE` on the newer ones (query in
  the rotation runbook).
- Card arrives but truncated/garbled line breaks → V026 not applied (sender
  v3 JSON-escapes quotes/newlines/tabs; `\n` renders as a line break in the
  card). `SELECT MAX(VERSION) FROM SCHEMA_VERSION;` should be ≥ 26.
- Flow shows runs but channel silent → the flow's "Post card in a chat or
  channel" action points at the wrong team/channel; fix in Power Automate.
- Success returns **202 Accepted** (asynchronous) — a 202 with no card means
  the flow ran and failed internally; check the flow's run history.

**Line format (V164).** Each alert is one line:
`[SEV] <title, first 140 chars> | <company> | <detail, one line, first 100 chars> | event <EVENT_ID>`
(ASCII separators; the detail's line breaks and tabs become spaces). The event id is
ALERT_EVENTS.EVENT_ID, the row Alerts > Open events lists. The line is identical in the sender's
3000-character fit and in the message, so a card never cuts an event in half; lines
are about 3x longer than before, so one card holds about 8-13 alerts and a burst
drains over more hourly runs (max 6 cards per route per run; the rest follow).

**CRITICAL escalation (V164).** A CRITICAL still open and unacknowledged
`ESCALATE_AFTER_MIN` minutes (Admin > Settings, default 120; 0 = off) after its
first notification is escalated ONCE by the hourly notifier: a card headed
`OVERWATCH ESCALATION - CRITICAL unacknowledged 120+ min:` goes to every enabled
route that already delivered it, and an email goes through
`ESCALATE_EMAIL_INTEGRATION` (default `OVERWATCH_EMAIL`, to its
`DEFAULT_RECIPIENTS`; blank = no email). Acknowledging or snoozing the event (a
snooze V117 carried onto a re-raise counts too), or acknowledging, mitigating or
closing its incident after the alert joined it, prevents it; so does a resolve. The
automatic V154 mitigation does not count. Events a route delivered fill each
3000-character batch first. Each channel stamps `ALERT_EVENTS.ESCALATED_AT` right
after its send succeeds; one `ALERT_AUDIT` row with ACTION `ESCALATE` per event the
run stamped follows. Timing: hourly, so about 120-185 minutes after the first
notification. The monthly alert drill escalates too when nobody acknowledges it.

Escalation symptoms → fixes (Alerts > Native delivery shows the policy and the last
7 days):
- `escalation_email_failed` in APP_ERROR_LOG (page NotifyWebhook) → `OVERWATCH_EMAIL`
  has no `DEFAULT_RECIPIENTS`, or SNOW_ACCOUNTADMINS lacks `USAGE` on it
  (docs/EMAIL_RECIPIENT_RUNBOOK.md, requirement 4). The Teams re-post still went and
  the event is stamped, so that email is not retried; an event no route delivered
  retries every hour inside its 7-day window.
- `route_send_failed` whose CONTEXT says `escalation re-post` → the route's
  integration refused the re-post (same fixes as above for a Teams route). While
  a route keeps refusing AND the escalation email is off or failing too, the
  CRITICALs only that route delivered are never stamped, stay first in the
  escalation batch (oldest first) and can hold back another route's escalations
  until they are acknowledged or leave the 7-day window: fix or disable the
  failing route, or acknowledge those events. (A working email leg stamps every
  escalated event, and a single route has no other route to hold back, so
  today's one Teams route plus email cannot hit it.)
- `escalation_failed` → the pass itself errored; the normal deliveries of that run
  still went. The next hourly run retries what was not stamped; anything already
  re-posted or emailed that run is stamped, so it is not re-sent (its ESCALATE audit
  row may be missing).
- Nothing escalates → `ESCALATE_AFTER_MIN` is 0 or not a number (Alerts > Native
  delivery reads "Escalation is off"), or TASK_ALERT_NOTIFY is suspended. The task's
  TASK_HISTORY RETURN_VALUE stays NULL (a task that CALLs a proc does not publish the
  proc's return string), so the proc's `... CRITICAL(s) escalated` tally is not
  visible there; read ALERT_AUDIT ACTION `ESCALATE` and `ESCALATED_AT` instead. Never
  hand-CALL the notifier to see it: it can page and email.
- Too noisy → acknowledge or snooze from Alerts > Open events, raise
  `ESCALATE_AFTER_MIN`, or set it to 0. Soft rollback = 0; hard rollback = re-run
  ONLY V064's SP_NOTIFY_WEBHOOK CREATE (V064 lines 74-351, never the whole file,
  which would also roll back three other procs); it also restores the old line format.


## §20 App session timeout & idle cost (Streamlit-in-Snowflake)

Symptom (live, 2026-07-09): the app "restarts itself" about every 10 minutes
and anything still running dies with it. Admin → Performance shows the tell:
`execute streamlit ... OVERWATCH_APP()` median ~550s, p95 ~601s, high fail
count — the app's own keep-alive statement is being killed at a 600-second
STATEMENT_TIMEOUT_IN_SECONDS. The kill does NOT save money: a connected
browser relaunches the parent statement immediately, and every restart
cold-starts the caches (the expensive scans re-run).

Diagnose where the 600 lives, then raise it for the app's execution path:

```sql
SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN WAREHOUSE WH_ALFA_ADMIN;
SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN ACCOUNT;
-- warehouse-level 600 (likely): raise just the app warehouse
ALTER WAREHOUSE WH_ALFA_ADMIN SET STATEMENT_TIMEOUT_IN_SECONDS = 28800;  -- 8h
-- account-level 600: leave the account guard; raise per app user instead
-- (effective timeout is the LOWEST of warehouse vs session level)
ALTER USER <app_user> SET STATEMENT_TIMEOUT_IN_SECONDS = 28800;
```

Idle cost is bounded by two existing controls, not by the statement timeout:
Streamlit-in-Snowflake ends the app session after ~15 minutes without
browser interaction (platform behavior, not configurable), and V002 set
`AUTO_SUSPEND = 60` on WH_ALFA_ADMIN — so a forgotten tab costs at most
~16 minutes of XS credits. There is deliberately no hard cap on
WH_ALFA_ADMIN (owner decision 2026-07-13; V045 dropped OVERWATCH_RM because
its credit cap was suspending the app and task warehouse mid-use). The
COST_* alert rules (e.g. COST_WH_DAILY_CREDITS) and Admin ▸ App self-cost
are the guardrails; expect the Spend ceilings & resource monitors panel
(Cost ▸ Optimization & Savings) to list it as uncapped, by design. The app's
own queries stay bounded by window clamps and row caps regardless of the
parent statement's ceiling.

## §21 Incidents — the operator SOP (V032)

One incident = one story: alerts, task failures, warehouse changes, DDL and
fixes under a single key. Alert-grain panels stay; incidents answer the
question storms obscure ("how many real problems, how fast did we recover").

**Where:** Control Room -> Incidents & triage (incident queue above triage;
Brief shows the open count). All state changes are DBA-gated,
generate-then-run, audited, forward-only — the app has no reopen; a
recurrence is a new incident (INCIDENTS.REOPENED_FROM exists for a
hand-written SQL link, but nothing in the app sets it).

**Declaring.** Three paths, none silent:
1. Proposals expander — open alert families (48h) with nearby warehouse
   changes counted; pick one, review the generated SQL, type DECLARE. The
   family's open alerts link as members automatically, never double-linked.
2. Auto-declare — CRITICALs open an incident when their dedupe family has
   no open one: hourly, one per family per 24h. Toggle:
   Settings -> INCIDENT_AUTO_DECLARE_CRITICAL. Never for the two identity
   rules (V162): SP_INCIDENT_AUTODECLARE skips SEC_LOGIN_TAKEOVER and
   SEC_ADMIN_GRANT whatever their severity, so contact the user first and then
   declare by hand (path 1 or 3). A later CRITICAL of either rule still
   attaches to an open or mitigated incident a person declared for that
   rule, but only when that incident already holds the SAME user; a
   CRITICAL for another user stays unlinked (and keeps its escalation)
   until someone declares it.
3. Manual SQL — the panels show every statement they would run; copy and
   adapt for unusual cases (members: ALERT | TASK_FAIL | WH_CHANGE | DDL |
   DEPLOY | REMEDIATION).

**Closing.** Select the incident, pick a ROOT_CAUSE_KIND (DEPLOY /
CONFIG_CHANGE / DATA / CAPACITY / EXTERNAL / UNKNOWN), one-line note, type
RESOLVE. Only OPEN/MITIGATED rows move — resolved history never rewrites.

**Metrics** (Alerts → History, "Incident lifecycle (90d, incident grain)",
company-scoped): incident MTTA (detected -> first response, AUTO-declared
incidents only), time to mitigate, MTTR (medians) and alerts per incident
(storm compression). Reopen rate (INCIDENT_REOPEN_DAYS) and
change-correlated % are no longer shown: no writer persists REOPENED_FROM or
a WH_CHANGE/DEPLOY member, so both read a permanent 0% (Admin → Settings
lists INCIDENT_REOPEN_DAYS as a row the app no longer reads). Change
correlation lives in the Control Room RCA.

**Attribution (V033):** the warehouse-change scorecard shows CHANGED_BY and
CHANGE_SOURCE. MANAGED = a DEPLOY_ACTORS service user (Settings; a comma
list, matched as a whole member, case- and space-insensitive, never a
substring; empty until Flyway/Terraform land), MANUAL = a human, UNKNOWN = no matching ALTER
found near the snapshot. Populate DEPLOY_ACTORS the day a deploy tool gets
a service user. Since V159 the hourly attribution pass tries each change for
about 3 hours after the scan sees it (its fixed evidence window around
CHANGE_SEEN_AT gains no new QUERY_HISTORY rows after that unless
ACCOUNT_USAGE runs more than ~3h late); a later unattributed change
re-tries every unattributed row of the last 7 days.
