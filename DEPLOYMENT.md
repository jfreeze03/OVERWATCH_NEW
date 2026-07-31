# Deployment

## 1. Snowflake objects (one-time, then per release)

Run as **SNOW_ACCOUNTADMINS** (or **SNOW_SYSADMINS** if it can create the
warehouse and grants) — these are the account's DBA roles:

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
snowflake/migrations/V021__precision_telemetry.sql
snowflake/migrations/V022__delivery_per_route.sql
snowflake/migrations/V023__prod_scoped_volume.sql
snowflake/migrations/V024__warehouse_change_scorecard.sql
snowflake/migrations/V025__break_glass_policy.sql
snowflake/migrations/V026__teams_safe_delivery.sql
snowflake/migrations/V027__mart_family.sql
snowflake/migrations/V028__cred_expiry_10d.sql
snowflake/migrations/V029__loader_fix.sql
snowflake/migrations/V030__loader_fix2.sql
snowflake/migrations/V031__scan_tuning_and_tagcov.sql
snowflake/migrations/V032__incident_object.sql
snowflake/migrations/V033__change_attribution.sql
snowflake/migrations/V034__route_company_filter.sql
snowflake/migrations/V035__lock_wait_mart.sql
snowflake/migrations/V036__pattern_cost_mart.sql
snowflake/migrations/V037__pattern_env_grain.sql
snowflake/migrations/V038__ledger_autobook.sql
snowflake/migrations/V039__pseudo_warehouse_filter.sql
snowflake/migrations/V040__freshness_state.sql
snowflake/migrations/V041__loader_efficiency.sql
snowflake/migrations/V042__codex_r22.sql
snowflake/migrations/V043__task_retirement_alert_teeth.sql
snowflake/migrations/V044__unknown_classification.sql
snowflake/migrations/V045__task_monitoring_restored.sql
snowflake/migrations/V046__storage_truth.sql
snowflake/migrations/V047__pattern_cost_qas.sql
snowflake/migrations/V048__object_cost_ledger.sql
snowflake/migrations/V049__write_target_attribution.sql
snowflake/migrations/V050__one_pass_read_write_arms.sql
snowflake/migrations/V051__action_layer.sql
snowflake/migrations/V052__exec_board_windows_180_365.sql
snowflake/migrations/V053__action_layer_remediation_verify.sql
snowflake/migrations/V054__exec_board_window_history.sql
snowflake/migrations/V055__cloud_services_breakdown.sql
snowflake/migrations/V056__loader_reconcile_alert_fixes.sql
snowflake/migrations/V057__fail_status_token.sql
snowflake/migrations/V058__task_node_timing.sql
snowflake/migrations/V059__task_graph_root_credits.sql
snowflake/migrations/V060__family_elapsed_queued_alert_guard.sql
snowflake/migrations/V061__ai_loader_alert_score_purge_fixes.sql
snowflake/migrations/V062__loader_robustness_alert_split_webhook.sql
snowflake/migrations/V063__webhook_capture_once_daily_facts_failguard.sql
snowflake/migrations/V064__webhook_drain_watermarks_alert_burn_telemetry.sql
snowflake/migrations/V065__alert_run_rate_windows.sql
snowflake/migrations/V066__alert_escalation_serverless_window_timeline_atomicity.sql
snowflake/migrations/V067__alert_attribution_onset_supersede_objectcost.sql
snowflake/migrations/V068__standalone_mart_freshness_stamps.sql
snowflake/roles.sql
snowflake/validate.sql   -- read the output; every row should be OK
```

> **V063 verify (webhook capture-once + daily-facts fail-guard):**
> - **⚠ B9 webhook — OWNER SMOKE TEST REQUIRED (ARRAY binding is runtime-only; a
>   byte-compare cannot prove it).** In a non-prod clone: pick an enabled route with a
>   real integration, insert ~25 OPEN `ALERT_EVENTS` (same company/family/severity,
>   each `TITLE` ~140 chars, staggered `RAISED_AT` within the last hour) so the escaped
>   message would exceed 3000 chars; `CALL SP_NOTIFY_WEBHOOK();` and confirm (a) the
>   delivered message is **not** truncated mid-event, (b) **only** the events that fit
>   (`ARRAY_CONTAINS(:fits_ids)`) get `NOTIFIED_AT` set and a `ALERT_DELIVERIES` row,
>   and (c) the non-fitting events keep `NOTIFIED_AT = NULL` and send on the **next**
>   run (no silent loss, no double-send). If a non-fitting event is marked delivered,
>   revert `SP_NOTIFY_WEBHOOK` to the V034/V062 body and report back.
> - **B34 daily-facts fail-guard** (byte-verifiable, but runtime-sensitive on recovery):
>   optionally verify in a clone that inducing a failure in one of the three per-table
>   wraps (e.g. rename a target column) leaves the `DAILY_FACTS` watermark **unchanged**,
>   returns a non-success string, and that the **next** run re-covers the missed day.
> - No new objects and no data heal — both are forward-healing proc swaps.

> **V064 verify (webhook oldest-first drain + per-source watermarks + burn + telemetry):**
> - **⚠ rec8 webhook — OWNER SMOKE TEST REQUIRED (`SYSTEM$SEND` + `ARRAY` binding + the
>   drain LOOP are runtime-only).** In a non-prod clone: pick an enabled route, insert
>   ~40 OPEN `ALERT_EVENTS` (same company/family/severity, each `TITLE` ~140 chars,
>   staggered `RAISED_AT` over the last few hours) so the backlog spans **several**
>   3000-char batches; `CALL SP_NOTIFY_WEBHOOK();` and confirm (a) the **oldest** events
>   are delivered **first**, (b) multiple message batches send in one call (bounded at 6),
>   (c) the loop **terminates** (does not spin), (d) each delivered event gets exactly one
>   `ALERT_DELIVERIES` row + `NOTIFIED_AT`, and (e) a second immediate `CALL` sends only
>   the remaining backlog (no re-send of delivered events). If the oldest starve or an
>   event double-sends, revert `SP_NOTIFY_WEBHOOK` to the V063 body and report back.
>   **Do not manually `CALL SP_NOTIFY_WEBHOOK()` while `TASK_ALERT_NOTIFY` may fire** —
>   the send precedes the ledger write, so two overlapping runs can double-send a batch.
>   The single scheduled task self-serializes, so scheduled delivery is unaffected; this
>   only bites a manual call racing the task.
> - **⚠ rec7 per-source watermarks — SMOKE TEST.** In a clone, induce a failure in one
>   per-table wrap (e.g. rename a `FACT_TASK_DAILY` column) and confirm only the
>   `FACT_TASK_DAILY` watermark is held while `FACT_METERING/LOGIN/STORAGE_DAILY` advance,
>   and the **next** run re-covers only the failed source. On the first post-V064 run the
>   four new `OW_LOAD_WATERMARKS` rows are created from the default window (the orphaned
>   `DAILY_FACTS` row is harmless). Confirm `SP_NIGHTLY_RECONCILE` still re-covers daily
>   facts (it now rewinds the four new keys).
> - **rec20-alert / rec18** (byte-verifiable): `COST_CONTRACT_BREACH` burns over
>   trailing-30-complete-days; `APP_QUERY_TELEMETRY` gains `SAMPLE_PROB` + `QUERY_ID`
>   (additive; existing rows read NULL). No new objects.

> **V065 verify (alert run-rate windows — no smoke test):** pure, deterministic alert
> logic in `SP_ALERT_SCAN_DAILY`, byte-verified by `tests/test_v065_alert_windows.py`.
> No new objects, no data heal, no app runtime change (the on-screen forecast was already
> fixed). Optional clone check: `CALL SP_ALERT_SCAN_DAILY();` runs clean and the
> `COST_FORECAST_BREACH` projection now uses a **complete-days-only** run-rate (early on
> day 1 of a month it does not fire — no complete day to rate yet), and `COST_AI_CREEP`
> compares two equal, today-excluded 7-day windows.

> **V066 verify (alert escalation + serverless window + timeline atomicity — no smoke
> test):** re-derives `SP_ALERT_SCAN`, `SP_ALERT_SCAN_DAILY`, and `SP_LOAD_MARTS_V27`;
> byte-verified by `tests/test_v066_alert_escalation.py`. No new objects. Deterministic
> alert-logic edits (severity bands on three dedupe keys so a HIGH→CRITICAL / MEDIUM→HIGH
> crossing re-pages; `COST_SERVERLESS_CREEP` today-exclusion) plus one atomicity wrap.
> Optional clone check for **#3**: the `MART_INCIDENT_TIMELINE` arm [8] rebuild now runs
> inside `BEGIN TRANSACTION … COMMIT` — induce a failure in its INSERT (e.g. temporarily
> rename a source) and confirm the trailing-48h rows survive (ROLLBACK), rather than the
> timeline going blank until the next hourly `SP_LOAD_MARTS_V27('HOURLY')`.

> **V067 verify (alert attribution + onset + supersede + object-cost — no smoke test):**
> re-derives `SP_ALERT_SCAN` + `SP_LOAD_OBJECT_COST`; byte-verified by
> `tests/test_v067_alert_attribution.py`. No new objects. Deterministic edits:
> `COMPANY_FOR_DATABASE` in two rules (#22), a 999 serverless-creep onset sentinel (#20), a
> post-scan escalation-supersede sweep (`RESOLUTION_KIND='SUPERSEDED'`, excluded from the
> precision score; #40), and a non-OK object-cost return on rollback (#10). Optional clone
> check: `CALL SP_ALERT_SCAN();` runs clean; after a HIGH→CRITICAL crossing, the earlier
> lower-band `ALERT_EVENTS` row flips to `RESOLVED`/`SUPERSEDED` while the CRITICAL stays OPEN.

> **V068 verify (standalone-mart freshness stamps — no smoke test):** re-derives
> `SP_LOAD_LOCK_WAIT_MART` + `SP_LOAD_PATTERN_COST`; byte-verified by
> `tests/test_v068_freshness_stamps.py`. The migration tail CALLs both procs, so verify is
> immediate: `SELECT SOURCE_NAME, LAST_LOAD_TS, SNAPSHOT_TS FROM
> DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE WHERE SOURCE_NAME IN
> ('MART_LOCK_WAIT_DAILY','MART_PATTERN_COST_DAILY');` — both rows should show a
> just-now timestamp (they had been frozen at apply time, ~2026-07-09), and the Brief
> "stalest telemetry" card stops naming MART_LOCK_WAIT_DAILY on the next app refresh.

> **V061 heal (runs in the migration tail; safe to re-run separately/off-hours):**
> `CALL SP_LOAD_MARTS_V27('DAILY', 365);` rewrites `FACT_AI_USAGE_DAILY` rows the old
> moving-timestamp AI arms corrupted (owner-chosen full-retention), and
> `CALL SP_LOAD_PLATFORM_SCORE(120);` backfills the new `CREDITS_BILLED_AI` column.
> The paired app change (score/scoring AI-rate blend) ships with the app release.

> **V062 heal + verify (loader robustness + alert split):**
> - The migration tail runs `CALL SP_LOAD_HOURLY_FACTS();` (B11 watermark catch-up —
>   fills the interior holes the old fixed −3d window left after any >3‑day loader
>   outage), and the task‑DAG section runs `CALL SP_ALERT_SCAN_DAILY();` once so the
>   6 daily alert blocks fire immediately instead of waiting for the next daily root run.
> - **Paired app release ships with V062 (R3‑4 parity):** the query "failed" predicate
>   moved to `<> 'SUCCESS'` in the mart loaders **and** the 4 app live‑fallback reads
>   (`ops_sql`, `insights_sql`, `mart_sql`) + `backfill_365.sql`. Apply the migration and
>   redeploy the app together, or the "Failed" tiles disagree mart‑vs‑live (like V057).
> - **New child task:** the DAG section adds `TASK_ALERT_SCAN_DAILY` after
>   `TASK_LOAD_DAILY` (suspends the root, creates the child, resumes children‑first then
>   the root, and calls `SYSTEM$TASK_DEPENDENTS_ENABLE`). Confirm the new task shows
>   `started` in `SHOW TASKS` after apply.
> - **NOT in V062 (deferred to V063):** despite the filename, V062 does **not** modify
>   `SP_NOTIFY_WEBHOOK`. The **B9** webhook truncation‑delivery fix is deferred because
>   an adversarial review found the authored fix re‑derived the fitting event set twice
>   (message vs ledger) straddling the network send, so a concurrent `ALERT_EVENTS`
>   insert or `CURRENT_TIMESTAMP` crossing 24h mid‑send could mark an unsent event
>   delivered. The correct fix captures the fitting `EVENT_ID`s **once** into an array
>   (`ARRAY_CONTAINS` in both the message and the ledger) — runtime semantics a
>   byte‑compare cannot prove — so it ships in a smoke‑tested **V063** alongside the
>   B34 partial‑failure observability refinement and the T3 perf‑loader restructures.
>   Until V063, `SP_NOTIFY_WEBHOOK` keeps its current (V034‑lineage) behavior.

Each migration records itself in `DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION`; re-running is
safe (idempotent `CREATE OR REPLACE` / `CREATE IF NOT EXISTS` + MERGE seeds).
The Admin page compares `SCHEMA_VERSION` against the versions bundled with the
app and flags drift.

Cost controls installed by V002:
- `WH_ALFA_ADMIN` — XSMALL, `AUTO_SUSPEND = 60`, dedicated to the app + tasks.
  No resource monitor since v4.45 (owner correction: OVERWATCH_RM's 30-credit
  cap was suspending the warehouse mid-use — V045 dropped it).

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

The loader chain runs on the dedicated **`WH_ALFA_ADMIN`** warehouse
(XSMALL, 60s auto-suspend; no resource monitor since v4.45).

## 2. Roles and execution model (owner's rights)

**Access is two roles, total** (owner decision 2026-07-13):
**SNOW_ACCOUNTADMINS** and **SNOW_SYSADMINS**. `roles.sql` grants both
directly (IMPORTED PRIVILEGES on the SNOWFLAKE db, read/write on the
OVERWATCH schema, warehouse usage) and actively retires the old
OVERWATCH_MONITOR / OVERWATCH_OPERATOR layer.

**OVERWATCH is an owner's-rights service.** Streamlit-in-Snowflake executes
every query with the app owner's privileges, not the viewer's role — the
viewer's role decides only which navigation profile they see. Two
consequences the code accounts for:

- Viewer identity comes from `st.user` (`app/core/identity.py`), because
  `CURRENT_USER()` returns the app owner inside the app. Preferences,
  usage telemetry, and audit actor stamps all ride `identity_sql()`.
- The in-app execution gate (typed confirmation + admin profile) is a UX
  guard, not a security boundary; the executor additionally enforces a
  statement allow-list (OVERWATCH tables/procs and warehouse levers only).

- Own the Streamlit app and the OVERWATCH objects with **SNOW_SYSADMINS** so
  day-to-day operation never requires the break-glass role.
- `ALERT_AUDIT` and `REMEDIATION_LOG` are append-only (UPDATE/DELETE
  explicitly revoked, even from the two admin roles). Admins can re-grant —
  the revokes block accidents, not adversaries; export on a schedule if an
  auditor needs stronger guarantees.
- `roles.sql` ends with a `SHOW GRANTS ON STREAMLIT` proof block: every
  grantee should be one of the two roles, and the output says so.

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
    QUERY_WAREHOUSE = WH_ALFA_ADMIN
    TITLE = 'OVERWATCH — Snowflake Command Center';
```

`LIST @DBA_MAINT_DB.OVERWATCH.OVERWATCH_STAGE` (or the directory table)
shows what is deployed; re-running PUT with OVERWRITE replaces files and the
app picks them up on next open.

`snowflake.yml` defines the app (`streamlit_app.py`, `query_warehouse:
WH_ALFA_ADMIN`); `environment.yml` pins the Snowflake-channel packages.
Queries execute with the app owner's rights; USAGE on the Streamlit object
(two roles only) is the access-control model.

## 4. Local development (dev only)

`.streamlit/secrets.toml`:

```toml
[connections.snowflake]
account = "<account>"
user = "<user>"
authenticator = "externalbrowser"   # or password
role = "SNOW_SYSADMINS"
warehouse = "WH_ALFA_ADMIN"
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
- **Section C (commented):** warehouse, Streamlit app
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
- **"Failed to retrieve packages... Have you enabled External Access
  Integration (EAI)?" / pypi.org DNS errors on load:** the app is running on
  the CONTAINER runtime. Snowsight's editor defaults new deploys to it, and
  the container runtime installs from PyPI (blocked here — no EAI). The
  warehouse runtime installs from Snowflake's Anaconda channel via
  `environment.yml`. Fix: app settings → **Run on warehouse**. The CLI path
  (`snow streamlit deploy --replace`) pins the warehouse runtime and never
  hits this — prefer it for redeploys.

## 7. Release checklist

1. `ruff check .` and `pytest -q` green (CI enforces).
2. New migration file if schema changed (never edit an applied `V00x` file).
3. Run migrations, then `snowflake/validate.sql` — all rows OK.
4. `snow streamlit deploy --replace`.
5. If the deploy happened through SNOWSIGHT instead of the CLI: check app
   settings → runtime = **Run on warehouse** (Snowsight resets it to the
   container runtime, which fails on PyPI/EAI at load — see §6).
6. Open Admin → Migration status (no drift), Source freshness (all fresh),
   Self-cost (task + app spend sane).
7. Tag the release; update `CHANGELOG.md`.


## Mid-migration expectations (append-only history)

Old migrations deliberately keep their era's `SP_ALERT_SCAN` text — including
columns later discovered not to exist on this account (`EXPIRES_AT`,
`CREDENTIALS.DELETED_ON`). Those bodies never execute once the sequence
completes: V019 disables SEC_CRED_EXPIRY, V020 re-points it (scan v8), V023
is terminal (scan v9). If the hourly scan fires while you are mid-sequence,
expect isolated `rule_block_failed` rows in the error log — they stop at the
next run after the sequence finishes. Do not rewrite historical migrations;
fix forward with a new scan version.
