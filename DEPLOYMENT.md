# Deployment

## 1. Snowflake objects (one-time, then per release)

Run as **SNOW_ACCOUNTADMINS** (or **SNOW_SYSADMINS** if it can create the
warehouse and grants) — these are the account's DBA roles:

> **⚠ EXISTING INSTALLS — widen SCHEMA_VERSION.DESCRIPTION once before applying V041+.**
> The column shipped as `VARCHAR(200)` in the original V001, but migration notes from V041 on
> run 200–1100 chars, so their `INSERT INTO SCHEMA_VERSION` fails with a string-too-long error.
> V001 now declares `VARCHAR(4000)` for fresh installs; an account created on the old baseline
> must widen it once (instant, in-place, no data loss):
> ```sql
> ALTER TABLE DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION
>     ALTER COLUMN DESCRIPTION SET DATA TYPE VARCHAR(4000);
> ```

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
snowflake/migrations/V069__exec_board_serverless_ai_drivers.sql
snowflake/migrations/V070__delivery_routing_teams_only.sql
snowflake/migrations/V071__task_graph_rechain_retry.sql
snowflake/migrations/V072__entity_aware_incident_proposals.sql
snowflake/migrations/V073__calendar_triage_windows.sql
snowflake/migrations/V074__operating_workbench_foundation.sql
snowflake/migrations/V075__security_operating_model.sql
snowflake/migrations/V076__anomaly_materiality_gate.sql
snowflake/migrations/V077__app_cost_ledger.sql
snowflake/migrations/V078__ai_usage_ts_cast.sql
snowflake/migrations/V079__ai_predicate_coco_historical_split.sql
snowflake/migrations/V080__security_change_risk_etl_exclusion.sql
snowflake/migrations/V081__unified_experiment_verify.sql
snowflake/migrations/V082__query_family_company_regrain.sql
snowflake/migrations/V083__action_estimate_period.sql
snowflake/migrations/V084__security_new_exposure_alert.sql
snowflake/migrations/V085__slo_breach_alert.sql
snowflake/migrations/V086__alert_snooze.sql
snowflake/migrations/V087__security_posture_rule.sql
snowflake/migrations/V088__security_change_risk_etl_exclusion_broadened.sql
snowflake/migrations/V089__backup_transient_clone.sql
snowflake/migrations/V090__drop_spend_rollup_dt_pilot.sql
snowflake/migrations/V091__alert_auto_clear.sql
snowflake/migrations/V092__action_lifecycle_clear_signals.sql
snowflake/migrations/V093__seed_default_settings.sql
snowflake/migrations/V094__fact_query_hourly_boundary_dedupe.sql
snowflake/migrations/V095__cost_alloc_role_company.sql
snowflake/migrations/V096__alert_scan_dedupe_keys.sql
snowflake/migrations/V097__anomaly_mean_ad_fallback.sql
snowflake/migrations/V098__incident_autodeclare_relink_guard.sql
snowflake/migrations/V099__incident_autodeclare_company_scope.sql
snowflake/migrations/V100__security_change_fact_reload_gap.sql
snowflake/migrations/V101__fact_task_daily_retry_collapse.sql
snowflake/migrations/V102__task_marts_retry_collapse.sql
snowflake/migrations/V103__wh_efficiency_active_hours_span.sql
snowflake/migrations/V104__sec_cred_expiry_dedupe_key.sql
snowflake/migrations/V105__change_risk_create_or_replace_destructive.sql
snowflake/migrations/V106__cost_dept_budget_pace_case_insensitive_join.sql
snowflake/migrations/V107__cost_dept_budget_pace_dept_join_and_pace_window.sql
snowflake/migrations/V108__cost_contract_breach_fires_when_exhausted.sql
snowflake/migrations/V109__warehouse_change_scan_fail_token.sql
snowflake/migrations/V110__alerting_hunt_sp_alert_scan_fixes.sql
snowflake/migrations/V111__cost_budget_pace_completed_days.sql
snowflake/migrations/V112__daily_digest_skips_paging_routes.sql
snowflake/migrations/V113__incident_timeline_task_fail_completed_time.sql
snowflake/migrations/V114__anomaly_sweep_after_daily_loader.sql
snowflake/migrations/V115__alert_supersede_includes_ack.sql
snowflake/migrations/V116__alert_clear_scope_proc.sql
snowflake/migrations/V117__alert_snooze_suppress_sweep.sql
snowflake/migrations/V118__ledger_autobook_dedup.sql
snowflake/migrations/V119__alert_autoclear_hysteresis_fix.sql
snowflake/migrations/V120__pattern_cost_runs_fanout_fix.sql
snowflake/migrations/V121__seed_coco_daily_cap.sql
snowflake/migrations/V122__anomaly_sweep_reconcile_race.sql
snowflake/migrations/V123__exec_board_account_clock.sql
snowflake/migrations/V124__table_storage_mart.sql
snowflake/migrations/V125__mfa_gap_active_user_coalesce.sql
snowflake/migrations/V126__task_graph_wh_credits_all_attempts.sql
snowflake/migrations/V127__wh_eff_idle_credits_actual_hours.sql
snowflake/migrations/V128__seed_etl_ref_gap_config.sql
snowflake/migrations/V129__pipe_ref_gap_alert.sql
snowflake/migrations/V130__experiment_verify_proof_guard.sql
snowflake/migrations/V131__incident_declare_atomic.sql
snowflake/migrations/V132__dq_breach_alert.sql
snowflake/migrations/V133__dq_schema_drift.sql
snowflake/migrations/V134__seed_etl_control_status_fqn.sql
snowflake/migrations/V135__seed_etl_control_run_params_fqn.sql
snowflake/migrations/V136__seed_etl_recon_error_fqn.sql
snowflake/migrations/V137__dq_recon_error_alert.sql
snowflake/migrations/V138__seed_etl_sla_clock.sql
snowflake/migrations/V139__object_cost_search_opt_column.sql
snowflake/migrations/V140__change_impact_exclude_self_procs.sql
snowflake/migrations/V141__alert_cadence_daily_cost_rules.sql
snowflake/migrations/V142__posture_arm_single_scan.sql
snowflake/migrations/V143__query_operator_stats_collector.sql
snowflake/migrations/V144__operator_time_pct_scale.sql
snowflake/migrations/V145__ledger_autobook_stamp_finding_type.sql
snowflake/migrations/V146__ai_usage_loader_repoint_ai_functions.sql
snowflake/migrations/V147__operator_stats_identity_grain.sql
snowflake/migrations/V148__exec_board_ai_predicate_restore_coco.sql
snowflake/migrations/V149__qh_extract_session_client_columns.sql
snowflake/migrations/V150__cloud_svc_anomaly_baseline.sql
snowflake/migrations/V151__security_change_risk_identity_policy_drops.sql
snowflake/migrations/V152__pipeline_freshness_coverage.sql
snowflake/migrations/V153__ledger_autobook_full_window_settle.sql
snowflake/migrations/V154__incident_attach_automitigate.sql
snowflake/migrations/V155__operator_stats_sis_app_tag.sql
snowflake/migrations/V156__etl_cycle_push_alerts.sql
snowflake/migrations/V157__alert_scan_self_watch_idle_push.sql
snowflake/migrations/V158__operator_backup_generations.sql
snowflake/migrations/V159__loader_compile_diet.sql
snowflake/migrations/V160__sleep_polling_alert.sql
snowflake/migrations/V161__retire_operator_backups.sql
snowflake/migrations/V162__security_takeover_admin_grant.sql
snowflake/migrations/V163__ai_runaway_trust_regression.sql
snowflake/migrations/V164__notify_actionable_lines_escalation.sql
snowflake/migrations/V165__daily_digest_grounding.sql
snowflake/migrations/V166__fact_loader_window_integrity.sql
snowflake/migrations/V167__mart_loader_edges_ai_coverage_pattern_reload.sql
snowflake/migrations/V168__alert_scan_hourly_keys_and_sweeps.sql
snowflake/migrations/V169__alert_scan_daily_windows_and_keys.sql
snowflake/migrations/V170__incident_declare_actor_and_proposals.sql
snowflake/migrations/V171__ops_selfwatch_digest_refgaps_seed.sql
snowflake/migrations/V172__detection_scans_company_and_accuracy.sql
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
>   **Outside this clone smoke test, do not hand-CALL `SP_NOTIFY_WEBHOOK()`**: it pages,
>   and since V164 it can also email (CRITICAL escalation). A manual call racing
>   `TASK_ALERT_NOTIFY` cannot double-send: V064's sender lease (`OW_SENDER_LEASE`) refuses
>   the overlapping run, which returns `skipped - another SP_NOTIFY_WEBHOOK run holds the
>   sender lease`; a lease held for more than 1h is treated as abandoned and reclaimed. If
>   you revert to the V063 body, which has no lease, two overlapping runs can double-send a
>   batch again (the send precedes the ledger write).
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
> logic in `SP_ALERT_SCAN_DAILY`, byte-verified by `tests/migrations/test_v065_alert_windows.py`.
> No new objects, no data heal, no app runtime change (the on-screen forecast was already
> fixed). Optional clone check: `CALL SP_ALERT_SCAN_DAILY();` runs clean and the
> `COST_FORECAST_BREACH` projection now uses a **complete-days-only** run-rate (early on
> day 1 of a month it does not fire — no complete day to rate yet), and `COST_AI_CREEP`
> compares two equal, today-excluded 7-day windows.

> **V066 verify (alert escalation + serverless window + timeline atomicity — no smoke
> test):** re-derives `SP_ALERT_SCAN`, `SP_ALERT_SCAN_DAILY`, and `SP_LOAD_MARTS_V27`;
> byte-verified by `tests/migrations/test_v066_alert_escalation.py`. No new objects. Deterministic
> alert-logic edits (severity bands on three dedupe keys so a HIGH→CRITICAL / MEDIUM→HIGH
> crossing re-pages; `COST_SERVERLESS_CREEP` today-exclusion) plus one atomicity wrap.
> Optional clone check for **#3**: the `MART_INCIDENT_TIMELINE` arm [8] rebuild now runs
> inside `BEGIN TRANSACTION … COMMIT` — induce a failure in its INSERT (e.g. temporarily
> rename a source) and confirm the trailing-48h rows survive (ROLLBACK), rather than the
> timeline going blank until the next hourly `SP_LOAD_MARTS_V27('HOURLY')`.

> **V067 verify (alert attribution + onset + supersede + object-cost — no smoke test):**
> re-derives `SP_ALERT_SCAN` + `SP_LOAD_OBJECT_COST`; byte-verified by
> `tests/migrations/test_v067_alert_attribution.py`. No new objects. Deterministic edits:
> `COMPANY_FOR_DATABASE` in two rules (#22), a 999 serverless-creep onset sentinel (#20), a
> post-scan escalation-supersede sweep (`RESOLUTION_KIND='SUPERSEDED'`, excluded from the
> precision score; #40), and a non-OK object-cost return on rollback (#10). Optional clone
> check: `CALL SP_ALERT_SCAN();` runs clean (since V157 a hand CALL skips the cadence-gated rules outside
> their Central-hour slots and still reports 14/14 ok, 12/12 before V162); after a HIGH→CRITICAL crossing, the earlier
> lower-band `ALERT_EVENTS` row flips to `RESOLVED`/`SUPERSEDED` while the CRITICAL stays OPEN.

> **V068 verify (standalone-mart freshness stamps — no smoke test):** re-derives
> `SP_LOAD_LOCK_WAIT_MART` + `SP_LOAD_PATTERN_COST`; byte-verified by
> `tests/migrations/test_v068_freshness_stamps.py`. The migration tail CALLs both procs, so verify is
> immediate: `SELECT SOURCE_NAME, LAST_LOAD_TS, SNAPSHOT_TS FROM
> DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE WHERE SOURCE_NAME IN
> ('MART_LOCK_WAIT_DAILY','MART_PATTERN_COST_DAILY');` — both rows should show a
> just-now timestamp (they had been frozen at apply time, ~2026-07-09), and the Brief
> "stalest telemetry" card stops naming MART_LOCK_WAIT_DAILY on the next app refresh.

> **V069 verify (exec-board serverless + AI cost drivers — no smoke test):** re-derives
> `SP_REFRESH_EXEC_BOARD` from V054; byte-verified by
> `tests/migrations/test_v069_exec_board_drivers.py`. No new objects, no app change (the new rows fill
> the existing COST_DRIVER contract). The migration tail CALLs the proc, so verify is
> immediate — read-only:
> ```sql
> SELECT DIMENSION, VALUE AS CREDITS, VALUE_USD
> FROM DBA_MAINT_DB.OVERWATCH.MART_EXEC_BOARD
> WHERE COMPANY = 'ALL' AND WINDOW_DAYS = 30 AND PANEL = 'COST_DRIVER'
>   AND (DIMENSION LIKE 'Serverless: %' OR DIMENSION LIKE 'AI/Cortex: %')
> ORDER BY VALUE_USD DESC;
> ```
> Expect one row per non-warehouse `SERVICE_TYPE` present in `FACT_METERING_DAILY`
> (auto-clustering, MV refresh, search optimization, snowpipe, serverless tasks, AI
> services…), and none named for a warehouse — warehouse metering stays on the original
> arm. Spot-check the house rate law: an `AI/Cortex:` row's `VALUE_USD / VALUE` should equal
> `AI_CREDIT_PRICE_USD` (2.20 by default) and a `Serverless:` row's should equal
> `CREDIT_PRICE_USD` (3.68). The rows appear on the **ALL** company pill only —
> `FACT_METERING_DAILY` is account-level and carries no company dimension, so splitting it
> across ALFA/Trexis would invent attribution. Overview → "Top cost drivers" then shows the
> serverless/AI lines alongside the warehouses on the next app refresh.

> **V070 verify (Teams-only delivery routing — no smoke test to apply):** re-derives
> `SP_DAILY_DIGEST` from V018 (byte-verified by `tests/migrations/test_v070_delivery_routing.py`) so
> the digest walks the enabled `ALERT_ROUTES` rows and sends through each route's own
> integration instead of the retired hardcoded `OVERWATCH_WEBHOOK`, ledgering failures as
> `digest_send_failed`; two idempotent blocks disable any enabled route whose integration is
> absent (#25) and resume `TASK_ALERT_NOTIFY` when an enabled route resolves to a live
> integration (#24). No new objects. Confirm the routes healed — read-only:
> ```sql
> SELECT ROUTE_ID, INTEGRATION_NAME, ENABLED FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES;
> ```
> Expect the Slack `OVERWATCH_WEBHOOK` row `ENABLED = FALSE` (its integration does not exist)
> and the `OVERWATCH_WEBHOOK_TEAMS` row `ENABLED = TRUE`. Delivery is runtime-only, so also
> prove one real card arrives once: `CALL DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST();` should
> return `digest written; sent 1/1 routes` and post the morning digest into the Teams channel
> (a `digest_send_failed` row in `APP_ERROR_LOG` naming the integration means that route's
> integration is missing a grant — see webhook_delivery.sql's rotation runbook).

> **V071 verify (task-graph re-chain + root retry — OWNER SMOKE TEST):** re-points the
> readers that were racing their own data and hardens the two roots; byte-verified by
> `tests/migrations/test_v071_task_graph.py`. It only `ALTER TASK ADD/REMOVE AFTER` + `SET` retry params
> and widens `SCHEMA_VERSION.DESCRIPTION` — it **never** re-defines a task body (no `CREATE OR
> REPLACE TASK`), so every existing schedule/warehouse/body is preserved. No new objects, no
> data heal. Idempotency is a **state check** (each re-point reads the live predecessors via
> `SHOW TASKS` and only ADDs the new predecessor when absent / REMOVEs the old when present,
> **ADD before REMOVE**), with **no error swallowing** — so a genuine `ALTER` failure (e.g. a
> graph run executing at apply time; `SUSPEND` does **not** stop an already-running instance)
> **aborts the migration loudly and leaves that graph SUSPENDED** rather than silently
> orphaning a task or reporting green. That is deliberate: **loud-suspended self-heals on a
> re-run** (the whole migration is idempotent), and ADD-before-REMOVE guarantees a failed
> re-point keeps its original predecessor (never zero). **The re-point + the SUSPEND/RESUME
> dance are runtime-only — a byte-compare cannot prove the graph resumed, and an abort leaves
> it suspended, so confirm it once after apply (read-only):**
> ```sql
> SHOW TASKS IN SCHEMA DBA_MAINT_DB.OVERWATCH;
> ```
> Confirm the `predecessors` column: `TASK_REFRESH_EXEC_BOARD` and `TASK_ALERT_SCAN` now list
> **`TASK_QH_EXTRACT`** (not `TASK_LOAD_HOURLY`); `TASK_LOAD_MARTS_V27_DAILY`,
> `TASK_PLATFORM_SCORE_DAILY` and `TASK_ALERT_SCAN_DAILY` now list **`TASK_NIGHTLY_RECONCILE`**
> (not `TASK_LOAD_DAILY`); and `TASK_ALERT_NOTIFY` still lists `TASK_ALERT_SCAN`. **Every one of
> the 12 tasks must read `state = started`** — the SUSPEND/RESUME re-enabled them, and a
> stranded-suspended child is the r07-r12 alert-outage class this migration ends both graphs
> with `SYSTEM$TASK_DEPENDENTS_ENABLE` to prevent. Also spot-check the roots:
> `SHOW PARAMETERS LIKE 'TASK_AUTO_RETRY_ATTEMPTS' IN TASK DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY;`
> should read `1` (same for `TASK_LOAD_DAILY`), and `SUSPEND_TASK_AFTER_NUM_FAILURES` should read
> `10`. If any task is `suspended`, run
> `SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY');` and the same
> for `TASK_LOAD_DAILY` (the migration is idempotent; re-running the whole file is also safe —
> each re-point's state check issues neither `ADD` nor `REMOVE` once the predecessors already
> match, so a matched re-run is a clean no-op).

> **V072 verify (entity-aware incident proposals):** this is a view-only change and does not
> run or replace the auto-declare procedure. After the owner applies V072 in Snowsight, query
> `DBA_MAINT_DB.OVERWATCH.INCIDENT_PROPOSALS` and confirm separate rows for distinct entity
> names in the same alert family. `CONFIDENCE` and `EVIDENCE` should disclose exact matched
> warehouse/object changes or task failures (since V170 a PIPE_TASK_FAILURES proposal's own task failures are
> labelled its alert source and no longer raise CONFIDENCE to HIGH). Incident creation remains a typed human
> action in Control Room. No app or automation should apply this migration directly.

> **V162-V172 (Next-Fifty wave 4 + the round-2 server fixes) — ONE apply, in order, then the owner repairs:**
> 1. **Deploy app 4.609.0 first**, then apply V162 → V172 in order in one sitting, from a worksheet whose first
>    statement is `ALTER SESSION SET TIMEZONE = 'America/Chicago';` (the runbox RUN_NEXT starts with it; the
>    owner worksheet is otherwise UTC, and PART B V172.4 compares `SCHEMA_VERSION.APPLIED_AT` with Central task
>    stamps). Each migration guards on the one before: stop on the first error, fix it, and re-run that file
>    (every one is idempotent). Apply outside 06:30-07:30 CT so no daily loader straddles a procedure swap.
> 2. **V164 still needs the escalation email chosen first** (PREFLIGHT P164.1: `DEFAULT_RECIPIENTS_SET` and
>    `SNOW_ACCOUNTADMINS_CAN_USE` TRUE, or seed `('ESCALATE_EMAIL_INTEGRATION','')` for a Teams-only escalation;
>    see the V164 verify note below). V165-V172 wait behind it: each guards on the one before.
> 3. Every app read of a new column and every new caption is gated on its own migration
>    (`app/ui/schema_gate.py`), so 4.609.0 is safe on either side of the apply; after it the gated text appears
>    within 4 h (the metadata cache) or at once on Refresh (the V170 declare overload and its two Control Room
>    captions read fresh: within 30 s). The Admin canary skips the two V167 coverage entries until V167 is
>    applied.
> 4. **Nothing is CALLed at apply time.** Outside the OWNER_REPAIRS blocks, never hand-CALL a scan, the
>    notifier, the canary sentinel or the digest from the migration worksheet (each can page, email or spend a
>    Cortex call). The only hand CALLs are the ones named after the apply: OWNER_REPAIRS R172.0's recommended
>    `SP_CHANGE_IMPACT_SCAN()` (point 6; it can raise PERF_CHANGE_REGRESSION exactly as its 06:50 run would), and
>    the V171 note's optional `SP_SCAN_REF_GAPS()`, which only rewrites its scratch table. Three migrations carry
>    bounded, idempotent in-migration repairs: **V166** (a MERGE that rewrites FACT_STORAGE_DAILY's averaged
>    multi-ID name-days), **V167** (a scan-free DELETE of stale-company MART_PATTERN_COST_DAILY twins) and
>    **V172** (the change registry's and the live, unlinked alerts' COMPANY re-stamped from the database in their
>    object FQN, the tracking TASK baselines re-frozen per scheduled run, and last the suffix-collided PROCEDURE
>    baselines nulled). If V172 fails part-way, V166-V171 stay in effect and V172 re-runs (R3, the PROCEDURE
>    null, runs last, right before the version row it is gated on).
> 5. Run the read-only PREFLIGHT first (`PREFLIGHT_WAVE4.sql` for V162-V165, `PREFLIGHT_V166_V172.sql` for
>    V166-V172; runbox): it lists exactly what each first run raises and what each repair rewrites. First runs:
>    V162's next hourly scan raises the last 24 h of takeover episodes and 26 h of admin grants; V163's next
>    daily scan the last 3 complete AI-usage days and Trust Center rises dated today or yesterday; V164's next
>    hourly notifier escalates every OPEN, unacknowledged CRITICAL of the last 7 days first notified 120+
>    minutes earlier (PREFLIGHT P164.2), and the CRITICAL takeovers V162's first scan raises escalate about
>    2-3 h after the apply (P162.4). An incident counts as acknowledging an alert only when a person
>    acknowledged, mitigated or closed it after the alert joined it. V168's first hourly scan auto-clears the
>    stranded OPEN PERF events (P168.1 counts them); V169's first daily scan may raise COST_IDLE_OPPORTUNITY for
>    never-suspend warehouses for the first time (P169.6).
> 6. **Then the owner-run repairs**, `OWNER_REPAIRS_V166_V172.sql` (runbox), after the whole apply, in a
>    Central session (its first statement pins it), off-peak, one block at a time, reading each verdict pane.
>    A failed verdict reads `FAILED ...`, and the guarded CALL blocks also log it to APP_ERROR_LOG and catch
>    their own errors, so a Run All continues past them. Two blocks do not: V167 step 1 verdict-gates its prune
>    (`FAILED (nothing pruned): ...`; a failed AI arm logs itself as MartLoader `mart_load_failed`) but writes
>    no OwnerRepairV167 row and does not catch a raised error, so a raise stops a Run All there; V167 step 4,
>    the atomic rebuild, rolls back and raises by design. A timeout or Stop is never caught (then run that
>    section's RESUME pair by hand):
>    - **V166** (R166.1-R166.6): the optional hole probe; `SP_LOAD_SECURITY_FACTS(180)` with the hourly graph
>      suspended around it (ALWAYS run the RESUME pair that follows the block); the R2-011 gap grids; the
>      storage-truth and app-cost (30+ days) heals.
>    - **V167** (steps 0-4): the read-only hole probes; **step 1, the AI DAILY 365 reload-then-prune, before
>      the next 06:45 CT DAILY marts run** (run it first if the V166 blocks cannot finish before then: until it
>      runs, Cortex Code totals around the apply double-count Central-evening usage); step 2
>      `SP_LOAD_PATTERN_COST(364)`; step 3 the HOURLY reload inside a TASK_LOAD_HOURLY suspend window; step 4
>      the atomic 364-day task-graph rebuild, outside the :07 Central slots.
>    - **V172**: R172.0, the recommended `SP_CHANGE_IMPACT_SCAN()` right after the apply (it closes the
>      mixed-basis day until the 06:50 scan; it can raise PERF_CHANGE_REGRESSION, delivered by the next hourly
>      notify, exactly as the 06:50 run would), then the read-only worklists R172.1-R172.4.
>    - Last, the `backfill_365.sql` opt-in heals: select and run only the cloud-services statement mart INSERT
>      (idempotent), and, once, the commented `SP_LOAD_OBJECT_COST(365)` block on a warehouse whose timeout
>      allows it.
>    Resolve the alerts the PREFLIGHT / PART B worklists name in the Alerts UI (never by SQL: RESOLVE feeds
>    per-rule precision), except the two OPTIONAL commented SUPERSEDED blocks R168.1 (PIPE_COPY_FAILURES
>    carry-over duplicates, PREFLIGHT P168.3) and R169.1 (P169.4's next-day DQ_RECON_ERROR twins): SUPERSEDED
>    is a machine-close kind the UI cannot set, and it stays out of RESOLVED, MTTR and precision. Close
>    member-less or duplicate incidents from Control Room (PART B V170.4 / V170.5). Which warehouse and
>    statement timeout the heavy reloads run on is the owner's call.

> **V166 (fact loader window integrity):** procs only, plus one bounded data repair that runs IN the migration
> (a MERGE that rewrites FACT_STORAGE_DAILY's DB_BYTES / FAILSAFE_BYTES for the name-days DATABASE_STORAGE_USAGE_HISTORY
> still holds as 2+ DATABASE_ID rows; no delete, LOAD_TS untouched, idempotent). Apply outside 06:30-07:15 CT so no
> daily loader straddles the swap. Corrections the owner will see: per-database storage KPIs, Storage MTD / prior
> month and the showback STORAGE_DB line step UP for databases re-created or clone-refreshed in the last 365 days
> (the fact had averaged them; the live twin already summed); Cost by application x user shows less (unknown) on days
> loaded after V166 (sessions resolve up to 30 days before the query's day). TASK_LOAD_APP_COST scans 33 days of
> SESSIONS instead of 10 (watch its next run in TASK_HISTORY). A failed app-cost or storage-truth run now rolls back,
> logs fact_load_failed and re-raises: its task reads FAILED, and the self-watch ERR leg raises OPS_PIPELINE_DEGRADED
> (whose detail says the run FAILED, V168 / V169). A single failed run no longer leaves a permanent hole. The task
> reloads 3 days: three failed runs in a row leave one stale day, and four or more also leave days no scheduled run
> reloads (OWNER_REPAIRS R166.3 lists them; R166.4 / R166.5 heal them).
> Owner-run heals after the whole V162-V172 apply: OWNER_REPAIRS V166 section (R166.1-R166.6), Central session.
> R166.2 (SP_LOAD_SECURITY_FACTS(180)) suspends TASK_LOAD_HOURLY around its CALL and resumes it itself; ALWAYS run
> the RESUME pair right after it (the only way back after a timeout or Stop). Its d>3 arm commits each DELETE before
> its INSERT: a failed run leaves the security change / login facts with ~3 days until it is re-run to ok.

> **V166 verify:** PART B V166.1 (four OK rows: the procs are the V166 text) and V166.2 (OK: every multi-ID
> name-day older than 2 days holds the SUM) right after the apply; the next morning V166.3 (TASK_LOAD_APP_COST /
> TASK_LOAD_STORAGE_TRUTH SUCCEEDED; RETURN_VALUE stays NULL) and V166.4 (no fact_load_failed rows). Never hand-CALL
> the loaders from the migration worksheet; the heals are the OWNER_REPAIRS blocks.

> **V167 (mart-loader window edges, AI coverage, atomic pattern reload):** one nullable column, three procs, and one
> bounded in-migration repair: a scan-free DELETE of MART_PATTERN_COST_DAILY rows older than their (DAY, QUERY_HASH,
> DATABASE_NAME) group's newest LOAD_TS by more than 10 minutes (stale-company twins from the V037 -> V044 -> V047
> era and any Apply-mapping remap; preview the count / credits / day range with PREFLIGHT P167.1, the kept same-stamp
> multi-company groups with P167.2; recoverable by Time Travel or a reload). Apply outside 06:40-07:30 CT. Nothing is
> CALLed. Corrections the owner will see: ALL-scope pattern dollars and runs step DOWN on the twin days (roughly Jun
> 11 - Jul 13 2026, plus days near any remap); Optimize idle / sizing and COST_IDLE_OPPORTUNITY read less idle for
> warehouses whose jobs cross Central midnight (new days at once; history only after OWNER_REPAIRS step 3, and idle
> days older than its N stay as they were); the Unit costs task-graph panel loses phantom child-named pipeline rows
> (history after step 4); Cortex Code days are Central days (the Admin AI recon may show Cortex Code drift from the
> V167 apply, once the app sees it -- within 4 h, or at once on Refresh -- until OWNER_REPAIRS step 1; before the
> apply the recon keys days the old way and shows none; rows older than the views' retention stay offset-keyed).
> **Run OWNER_REPAIRS step 1 in the same sitting as the apply -- before the next 06:45 Central DAILY run if at all
> possible.** Until it
> runs, each DAILY ('DAILY', 3) run re-keys only its last ~3 days to Central with a MERGE (no delete): a user whose
> only usage on an old UTC-keyed day was the previous Central evening keeps that old row beside the new Central-day
> row, so Cortex Code totals for the ~3 days around the apply DOUBLE-COUNT that evening usage (Chargeback & AI, the
> Spend CoCo tile, AI budgets and the runaway arm read slightly high), and the evening just before the run's left
> edge drops out instead. Step 1 (reload-then-prune) removes both. The Cortex Code panels (Chargeback & AI, the Spend
> CoCo tile, Security AI guardrails) answer 180d / 365d / Current year once OWNER_REPAIRS step 1 stamps
> COVERAGE_FROM, and the >90-day repeated-pattern window once step 2 does. The all-source Unit costs "AI spend"
> (Code + Functions) floors that stamp at 2026-01-05, the canonical Functions view's first day
> (mart27_sql.AI_FUNCTIONS_VIEW_FROM): a window that starts earlier keeps the first-use test and its labelled
> Functions-only fallback unless the fact already holds rows that old, so its 365-day window answers from about
> 2027-01-04 and Current year from 2027-01-01. Do not re-run SP_LOAD_PATTERN_COST before V167 is applied (the V120
> MERGE adds twins).

> **V167 verify:** PART B V167.1 (COVERAGE_FROM column present; values NULL until each loader's next run) and V167.2
> (every CHECK_n TRUE: the deployed bodies are V167's) right after the apply; V167.3 (TWIN_ROWS_LEFT = 0). After
> OWNER_REPAIRS step 1: V167.4 Cortex Code fact vs live per Central day, DRIFT ~0. The next 06:45 pattern run stamps
> MART_PATTERN_COST_DAILY.COVERAGE_FROM = today-3; the next DAILY marts run stamps FACT_AI_USAGE_DAILY = today-2.

> **V168 / V169 (hourly and nightly alert keys and windows):** V168 then V169, each stop-on-first-error; nothing
> CALLs; neither writes ALERT_EVENTS at apply. The first hourly scan after V168 auto-clears stranded OPEN PERF events
> (PREFLIGHT P168.1 counts them). Every V168 / V169 grid depends on the leading Central pin. Apply-window notes
> (once each): a table whose yesterday PIPE_COPY_FAILURES V162 re-raised after midnight under today's date absorbs the
> apply day's new failures of that band (PREFLIGHT P168.2 `HELD_BY_A_V162_EVENT`); a success that follows a
> failures-only V162 SEC_NEW_ADMIN_NETWORK event inside 48h does not raise its own event during the transition
> (PREFLIGHT P168.4 second grid). Verify: PART B V168.1 / V168.2 and V169.1 / V169.2 right after the apply; V168.3 /
> V168.4 after the next :07 Central hourly scan (WILL_AUTO_CLEAR -> 0); V169.3 and V169.4 the next morning (~07:00
> Central; V169.4 flags a reconciliation error cycle that folded into an event raised before it loaded). Both scans'
> [22] OPS_PIPELINE_DEGRADED ERR detail now says a V166 app-cost / storage-truth failure rolled back and FAILED
> (TASK_HISTORY shows it); other loaders keep "returned normally, so its task still reads SUCCEEDED". Resolve in
> Alerts: P169.1 day 2-5 COST_BUDGET_PACE (NOISE), P169.2 WOULD_RAISE_NEW = FALSE contract events (+ their
> auto-declared incidents), P169.3 re-created-database storage surges (EXPECTED), P169.7 SEC_TRUST_REGRESSION
> METRIC_VALUE < 1 (EXPECTED). P169.4 next-day DQ_RECON_ERROR twins: NOT in the Alerts UI (its RESOLVE radios offer
> only ACTIONED / NOISE / EXPECTED, which count in RESOLVED and MTTR). Use the OPTIONAL owner repair R169.1 in
> OWNER_REPAIRS_V166_V172.sql (V169 section): a commented-out UPDATE that closes the OLDER twin as RESOLUTION_KIND =
> SUPERSEDED, bounded by the EVENT_IDs you paste from P169.4's second grid, RULE_ID = DQ_RECON_ERROR and STATUS IN
> (OPEN, ACK). Run it in a Central session; as shipped, its placeholder matches no row. Never DELETE, never rewrite
> DEDUPE_KEY.

> **V170 (incident declare + proposals):** no apply-time run, no data change. Deploy 4.609.0 FIRST, then apply.
> Before the apply, 4.609.0 CALLs V131's 4-arg overload and reads its "DECLARED: n member(s) linked" verdict
> (Declared by stays the app owner and a caption says so). After the apply it CALLs the new 5-arg overload. While
> its 4 h schema cache lacks V170, Control Room re-reads SCHEMA_VERSION on the 30 s live tier, so a manual declare
> made 30 s or more after the apply writes the declaring DBA as Declared by / Linked by. Before that first declare,
> confirm that the Control Room SQL preview ends with the viewer as a 5th argument; if it shows 4 arguments, press
> Refresh data. A declare through the 4-arg CALL keeps the app owner for good. Off-order (V170 applied while
> 4.608.0 is still deployed): the old app keeps working on the re-derived 4-arg overload, but it discards the procedure's
> verdict, so when every proposal alert cleared before the CALL it still toasts "Incident declared with members
> linked." and logs incident_declare for a declare V170 rolled back (nothing written; that inflated event also
> feeds the optional R2-028 APP_USAGE heuristic) — redeploy promptly. Verify with PART B V170.1-V170.6 (Central
> session): V170.1 shows TWO SP_INCIDENT_DECLARE rows; V170.2 all TRUE; V170.3 has no BAND_AS_ENTITY; V170.4 /
> V170.5 are owner worklists (close member-less / duplicate incidents from Control Room with their root cause).
> Earlier manual declares keep the app owner as Declared by (no history rewrite; the Open incidents table keeps a
> caption saying so while one is listed). Follow-up: once 4.609.0 is deployed, a later migration drops the 4-arg
> overload (keep both teardown lines).

> **V171 verify (no smoke test to apply):** PREFLIGHT P171.5 reads OK on every row before the apply (the
> OPS_SLOW_RENDER title CASE compiles and reads Hr/Min/Sec). PART B V171.1-V171.4 read OK right after the apply
> (GET_DDL text of the three procs; exactly one CREDIT_PRICE_OVERRIDE row). The next morning after 07:20 CT, the latest
> DAILY_DIGEST row's FACTS starts `WINDOW_DAYS=7; WAREHOUSE_SPEND_USD=` (V171.5) and the warehouse figure is a
> little below the old today-inclusive KPI (PREFLIGHT P171.1 previews both). After the next daily alert scan, no
> new `ref_gap_scan_failed` row unless EVERY ref-gap check failed; a single failing check now logs
> `ref_gap_check_failed` with the check name in CONTEXT (V171.6). Nothing runs at apply time (a hand
> `CALL SP_DAILY_DIGEST()` spends a Cortex call and posts to Teams). Optional: `CALL
> DBA_MAINT_DB.OVERWATCH.SP_SCAN_REF_GAPS();` rewrites only its scratch table and should return
> `ref-gap scan complete (N ok, 0 failed)`. V171's only in-migration change besides the procs is the idempotent
> CREDIT_PRICE_OVERRIDE = FALSE seed (WHEN NOT MATCHED; an existing TRUE is never touched).

> **V172 (detection scans: company and accuracy):** V172 carries in-migration repairs (registry and alert COMPANY
> re-stamps, the TASK baseline re-freeze, and last the first-apply PROCEDURE baseline null); they read
> ACCOUNT_USAGE.PROCEDURES once and 30 days of TASK_HISTORY once. If V172 fails part-way, V166-V171 stay in effect
> and V172 re-runs (every repair is idempotent; R3, the PROCEDURE null, runs last, right before the version row it is
> gated on, so a retry after any earlier stop nulls those rows for the first time). Right after V172, run
> OWNER_REPAIRS R172.0 (uncomment `CALL DBA_MAINT_DB.OVERWATCH.SP_CHANGE_IMPACT_SCAN();`, off-peak; one daily scan's
> ACCOUNT_USAGE reads; it can raise PERF_CHANGE_REGRESSION, delivered by the next hourly notify, exactly as the 06:50
> run would): until the next scan the change-impact tracking rows sit on mixed bases -- the TASK baselines are
> re-frozen per scheduled run while AFTER_CALLS / AFTER_FAILS / VERDICT / VERDICT_DETAIL still hold the last V140
> scan's attempt-based values, and the PROCEDURE baselines R3 nulled still show their old VERDICT. If skipped, the
> Operations change table reads mixed until the 06:50 Central scan. Delivery: the five rules (PERF_CHANGE_REGRESSION,
> PIPE_DT_FAILURES, PIPE_VOLUME_DROP, DQ_BREACH, DQ_SCHEMA_DRIFT) now route by the V044 company. Alerts on a database
> with no COMPANY_SCOPE row that is not `TRXS_*` / `ALFA*` / ADMIN become UNKNOWN and stop posting to the ALFA-only
> Teams route (V034 set every existing route's COMPANY_FILTER to ALFA), with no undelivered_expired row;
> PERF_CHANGE_REGRESSION, PIPE_DT_FAILURES and PIPE_VOLUME_DROP seed HIGH, and PIPE_DT_FAILURES is CRITICAL at 5+
> failures. To keep them, map the database in Cost
> Intelligence > Spend & Attribution > Unmapped entities, or add an ALL or UNKNOWN route. Re-stamped OPEN events that
> now match another route's filter are delivered there once, inside their send window (24 h, 7 d for CRITICAL); an
> older one raised within 7 days logs one undelivered_expired row instead (V164's per-(EVENT_ID, ROUTE_ID) ledger).
> Rollback (RUNBOOK §12, "Rolling back V172"; the exact text is in the V172 header) has two ordered steps: re-run the
> base CREATEs, then, right after V140's CREATE and before the next change-impact scan, null the still-tracking TASK
> and PROCEDURE baselines, or V140 reads V172's per-run baselines against its own every-attempt AFTER counts and
> pages a false REGRESSED. Verify: PART B V172.1 / V172.2 right after the apply; V172.3 after the 06:40 / 06:50
> scans; V172.4 after the next TASK_CHANGE_IMPACT_SCAN (06:50) and TASK_ANOMALY_SWEEP (07:00) runs (FAIL only for a
> guarded arm that logged since the apply and was silent in the 14 days before).

> **V164 verify (actionable Teams lines + CRITICAL escalation — OWNER SMOKE TEST: the send, the ARRAY
> handling and the nested cursor loop are runtime-only):**
> 1. Before the apply: PREFLIGHT P164.1 must show `DEFAULT_RECIPIENTS_SET` and
>    `SNOW_ACCOUNTADMINS_CAN_USE` TRUE, or seed `('ESCALATE_EMAIL_INTEGRATION','')` for a Teams-only
>    escalation. P164.2 lists the first-run escalations of CRITICALs that already exist, and P162.4
>    the CRITICAL takeovers V162's first hourly scan raises (P164.2 cannot see those; they escalate
>    about 2-3 hours after the apply too). Read both: acknowledge stale ones (the new takeovers within
>    2 hours of the first hourly scan) or seed `('ESCALATE_AFTER_MIN','0')` (both seeds survive the
>    V164 MERGE, which is WHEN NOT MATCHED).
> 2. After the next hourly chain: TASK_ALERT_NOTIFY SUCCEEDED (its RETURN_VALUE stays NULL: a task
>    that CALLs a proc does not publish the proc's return string), no `escalation_failed` or
>    `escalation_email_failed` rows in APP_ERROR_LOG, and one ALERT_AUDIT `ESCALATE` row per
>    `ALERT_EVENTS.ESCALATED_AT` stamp.
> 3. The next Teams card shows `[SEV] title | company | detail | event <id>` lines.
> 4. End to end: leave the monthly OPS_ALERT_DRILL CRITICAL unacknowledged for 2 hours. Expect one
>    "OVERWATCH ESCALATION" card, one email, one ESCALATE audit row and ESCALATED_AT set.
> 5. Restoring ALERT_EVENTS from a manual clone taken before V164: the clone lacks ESCALATED_AT, so
>    `INSERT OVERWRITE ... SELECT *` fails. Use an explicit column list, or
>    `ALTER TABLE <clone> ADD COLUMN ESCALATED_AT TIMESTAMP_NTZ` first (RUNBOOK §16).

> **V165 verify (measured digest grounding — no smoke test to apply):** the next morning after
> 07:20 CT, the latest DAILY_DIGEST row has BODY_SOURCE `AI` or `TEMPLATE` and FACTS filled;
> TASK_DAILY_DIGEST SUCCEEDED; and the card arrived in Teams (V165 also JSON-escapes the digest text,
> which Teams Workflows otherwise rejects on a raw newline or quote). A frequent `TEMPLATE` means the
> model states numbers the facts do not hold: read UNGROUNDED. Nothing runs at apply time (a hand
> `CALL` spends a Cortex call and posts to Teams).

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
- `WH_ALFA_ADMIN` — XSMALL, `AUTO_SUSPEND = 60`, shared by the app and tasks.
  It has no resource monitor since v4.45 (owner correction: OVERWATCH_RM's
  30-credit cap was suspending the warehouse mid-use — V045 dropped it).

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
every query with the app owner's privileges, not the viewer's role. The
viewer's identity (`st.user`, mapped through `config.VIEWER_PROFILES`; an
unresolved viewer fails closed to the least-privilege profile) decides only
which navigation profile they see and whether the operator gate opens. Two
consequences the code accounts for:

- Viewer identity comes from `st.user` (`app/core/identity.py`), because
  `CURRENT_USER()` returns the app owner inside the app. Preferences,
  usage telemetry, and audit actor stamps all ride `identity_sql()`.
- The in-app execution gate is the `config.OPERATOR_USERS` viewer allowlist
  (`session.is_operator()`), plus a typed confirmation for classifying or
  account-touching writes. Because every viewer runs as the owner, that
  allowlist is the app's authorization boundary. The executors run one
  statement at a time from a fixed allow-list: DML (INSERT / UPDATE / DELETE /
  MERGE) on DBA_MAINT_DB.OVERWATCH objects, CALLs of DBA_MAINT_DB.OVERWATCH
  procs, and the Operations ▸ Emergency levers `ALTER WAREHOUSE`,
  `ALTER PIPE`, `ALTER TASK`, `ALTER USER` and `ALTER ACCOUNT SET`. The
  executor re-checks OPERATOR_USERS itself for those ALTER levers (and for
  query cancel), so anyone on the list can, with the owner's rights, change
  warehouse settings (size, suspend, timeouts, clusters), pause or resume
  pipes, suspend or resume tasks, disable or re-enable users and set account
  parameters.

- **The deployment role, SNOW_ACCOUNTADMINS (§1), owns the Streamlit app and
  the OVERWATCH objects.** On this account it is a routine operating role,
  not a break-glass one (V025 owner decision). The repo's owner-side grants
  name it: the integration USAGE grant in docs/FULL_REBUILD.md step 7b(a)
  (SP_NOTIFY_WEBHOOK runs as its owner), the PREREQS of
  snowflake/native_alert_templates.sql (a file run as the app owner role, so
  the app can see its alerts) and requirement 4 of
  docs/EMAIL_RECIPIENT_RUNBOOK.md. If
  you deploy as SNOW_SYSADMINS instead (§1 allows it), that role becomes the
  owner: point each of those grants at it.
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

Deploy from a clean, committed tree: the deploy uploads whatever is on
disk, but the secret scan (tests/test_no_committed_secrets.py) checks only
tracked files, and everything shipped is readable by every viewer (Alerts ▸
Native delivery renders both snowflake/ templates) — so never deploy with a
real webhook secret pasted into `webhook_delivery.sql`. `snowflake.yml`'s `artifacts` list is what ships —
`streamlit_app.py`, `environment.yml`, `app/`,
`snowflake/native_alert_templates.sql` and `snowflake/webhook_delivery.sql`
(tests/test_deploy_artifacts.py locks the two templates in).

Manual path (no CLI — SnowSQL or any PUT-capable client):

```sql
PUT file://streamlit_app.py @DBA_MAINT_DB.OVERWATCH.OVERWATCH_STAGE/app/ OVERWRITE=TRUE AUTO_COMPRESS=FALSE;
PUT file://environment.yml  @DBA_MAINT_DB.OVERWATCH.OVERWATCH_STAGE/app/ OVERWRITE=TRUE AUTO_COMPRESS=FALSE;
PUT file://app/*            @DBA_MAINT_DB.OVERWATCH.OVERWATCH_STAGE/app/app/ OVERWRITE=TRUE AUTO_COMPRESS=FALSE;
-- PUT does not recurse: repeat for EVERY subfolder, to the same relative stage path —
--   app/assets, app/core, app/data, app/logic, app/logic/ask, app/ui, app/ui/pages,
--   app/ui/pages/cost_parts, app/ui/pages/ops_parts
--   e.g. PUT file://app/logic/ask/* @DBA_MAINT_DB.OVERWATCH.OVERWATCH_STAGE/app/app/logic/ask/ OVERWRITE=TRUE AUTO_COMPRESS=FALSE;
-- A missing package makes app/main.py's page imports fail, so the whole app won't start.
-- the two templates Alerts > Native delivery reads (else it says "File not found in this deployment"):
PUT file://snowflake/native_alert_templates.sql @DBA_MAINT_DB.OVERWATCH.OVERWATCH_STAGE/app/snowflake/ OVERWRITE=TRUE AUTO_COMPRESS=FALSE;
PUT file://snowflake/webhook_delivery.sql       @DBA_MAINT_DB.OVERWATCH.OVERWATCH_STAGE/app/snowflake/ OVERWRITE=TRUE AUTO_COMPRESS=FALSE;

CREATE OR REPLACE STREAMLIT DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP
    ROOT_LOCATION = '@DBA_MAINT_DB.OVERWATCH.OVERWATCH_STAGE/app'
    MAIN_FILE = 'streamlit_app.py'
    QUERY_WAREHOUSE = WH_ALFA_ADMIN
    TITLE = 'OVERWATCH — Snowflake Command Center';
-- CREATE OR REPLACE drops every grant on the app: re-run snowflake/roles.sql
-- (its Streamlit-grants check must return 'Streamlit grants OK').
```

The uploaded set must mirror `snowflake.yml`'s `artifacts`: if the app gains
a subfolder or a runtime-read file, add it here too.

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
  facts/marts. Re-run the migrations in order (V001 through the repo tip) and
  the loaders repopulate — except the opt-in objects no migration creates:
  the four NATIVE_ALERT_* email alerts, TASK_ALERT_DRILL and the ML forecast
  objects. The opt-in tail at the end of Section B also runs live: it drops
  the ML forecast model, the webhook secrets and the OVERWATCH_* notification
  integrations (OVERWATCH_EMAIL, OVERWATCH_WEBHOOK_TEAMS, …). Re-create those
  with their opt-in scripts afterwards (docs/FULL_REBUILD.md step 7b).
- **Section B (commented, except two live parts):** operator data — settings,
  company scope, alert config/events/audit, action queue, savings ledger,
  error log, schema_version, OVERWATCH_STAGE. Uncomment only for a factory
  reset, and run the provided `CLONE` backups first: since V161 retired the
  scheduled backups they are the only copy outside Time Travel. `UNDROP TABLE
  ...` also works within Time Travel. Two parts of Section B run live: the
  three rebuildable tables APP_QUERY_TELEMETRY, ALERT_DELIVERIES and
  OW_SENDER_LEASE (the migrations re-create them; the emptied delivery ledger
  makes the first notifier run re-post the last 24 h of OPEN events, 7 days
  for a CRITICAL — docs/FULL_REBUILD.md step 7b(a)), and the opt-in tail above.
- **Section C (commented):** warehouse, Streamlit app
  object, roles — shared infrastructure, dropped only deliberately.

The verify query at the bottom lists any surviving OVERWATCH objects. A unit
test (`tests/test_teardown_coverage.py`) fails CI if a migration creates an
object the teardown does not cover, or if a destructive drop ever goes live.

Restore = every migration in order (V001 through the repo tip) -> roles.sql ->
validate.sql (all rows OK) -> docs/FULL_REBUILD.md step 5's V167 AI reload-then-prune (required: the replayed
V078 first-fill keyed a year of Cortex Code rows on the stored offset) -> step 7b for the opt-in objects.

## 6. Disaster recovery (summary — full detail in RUNBOOK.md)

- **No scheduled backups (V161, owner decision 2026-09-28):** recovery is Time
  Travel plus the manual clones taken before a risky change
  (`snowflake/rebuild/00_backup_operator_data.sql` or teardown.sql B0, as TRANSIENT
  clones with today's date suffix; both cover every operator table a factory reset drops). The TRANSIENT operator tables keep at most 1 day
  of Time Travel and no Fail-safe.
- **Undo one table:** Time Travel, as the table-owner role (INSERT OVERWRITE deletes;
  the audit tables revoke DELETE from both admin roles):
  `INSERT OVERWRITE INTO <t> SELECT * FROM <t> AT(OFFSET => -3600);`
  or `BEFORE(STATEMENT => '<query_id>')`, or from a manual clone:
  `INSERT OVERWRITE INTO <t> SELECT * FROM <t>_BAK_<yyyymmdd>;`
  `UNDROP TABLE <t>` within the retention window. Never CLONE-restore: a
  re-materialized table re-applies the schema FUTURE grants.
- **Schema dropped:** `UNDROP SCHEMA DBA_MAINT_DB.OVERWATCH;` first; it restores the
  manual clones too. Past retention, re-run every migration in order (if V161 stops
  on the backup run V158's tail started, after waiting ~4 minutes for it, re-run it), re-enter
  SETTINGS / DEPARTMENT_MAP / routes, then roles.sql + validate.sql. Facts refill
  from the loader tasks (history limited to ACCOUNT_USAGE retention). See RUNBOOK §16.
- **App broken after deploy:** `snow streamlit deploy --replace` with the
  previous git tag; migrations are additive so no schema rollback is needed.
- **"Failed to retrieve packages... Have you enabled External Access
  Integration (EAI)?" / pypi.org DNS errors on load:** the app is running on
  the CONTAINER runtime. Snowsight's editor defaults new deploys to it, and
  the container runtime installs from PyPI (blocked here — no EAI). The
  warehouse runtime installs from Snowflake's Anaconda channel via
  `environment.yml`.
- **Saving "Run on warehouse" fails with `ARTIFACT_REPOSITORIES are only
  supported by Streamlit applications running in the container runtime`:**
  Snowflake retained a container-only property on the existing app object.
  As the role that owns the app, run
  `snowflake/warehouse_runtime_reset.sql` in Snowsight. It first executes
  `ALTER STREAMLIT ... UNSET ARTIFACT_REPOSITORIES`, then explicitly sets
  `RUNTIME_NAME = 'SYSTEM$WAREHOUSE_RUNTIME'` and
  `QUERY_WAREHOUSE = WH_ALFA_ADMIN`. Reopen App settings and save. The
  warehouse-shaped `snowflake.yml` deliberately omits `runtime_name`,
  `compute_pool`, and `artifact_repositories`; omission alone does not clear a
  property already persisted on the Snowflake object.

## 7. Release checklist

1. `ruff check .`, `mypy` and `pytest -q` green (CI enforces all three).
2. New migration file if schema changed (never edit an applied `V00x` file).
3. Run migrations, then `snowflake/validate.sql` — all rows OK. Then
   `snowflake/task_audit.sql` — every task reads OK with no `DRIFT` row (diffs
   live `SHOW TASKS` state/warehouse/schedule/predecessor against the expected
   set, catching a stale `CREATE TASK IF NOT EXISTS` whose updated definition
   never re-applied), except a "live task not in expected set" row for each
   opt-in task you installed (alert_drill.sql's TASK_ALERT_DRILL,
   ml_forecast_option.sql's TASK_REFRESH_ML_FORECAST).
4. `snow streamlit deploy --replace`, from a clean, committed tree (§3).
5. Check app settings → runtime = **Run on warehouse**. If save reports a
   retained `ARTIFACT_REPOSITORIES` setting, run
   `snowflake/warehouse_runtime_reset.sql` as the app-owning role (see §6).
6. Open Admin → Migrations & freshness (no missing or 'newer than this
   build' migrations; Source freshness all fresh; Task health shows no
   Suspended, Failing or Not visible rows) and Admin → App self-cost (task +
   app spend sane). Task health grades a suspended TASK_ALERT_NOTIFY
   "Suspended (expected)", but since V071 the migrations leave it started, so
   it reads that only if someone suspended it.
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
