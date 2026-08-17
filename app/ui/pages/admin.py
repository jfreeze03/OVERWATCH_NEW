"""Admin — settings, migrations & freshness, metric registry, app self-cost,
performance, canary, and errors & telemetry.

Everything that was wrongly parked on the old app's executive page lives
here, where the people who can act on it will look for it.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import streamlit as st

from app.config import (
    APP_VERSION,
    DEFAULT_SETTINGS,
    core_object,
)
from app.core.errors import error_buffer, safe_page
from app.core.identity import identity_sql
from app.core.query import bump_refresh_salt, execute_statement, query_telemetry, run
from app.core.session import is_operator as _is_operator
from app.core.sqlsafe import sql_literal
from app.core.state import filters
from app.data import cost_sql, mart_sql
from app.logic.formulas import humanize_duration, md_dollars, safe_float
from app.ui.components import (
    confirm_gate,
    guard,
    kpi_row,
    lazy_sections,
    load_settings,
    notify,
    page_header,
    panel_help,
    result_caption,
    run_mart_first,
    section_filter_contract,
    selectable_table,
    snowsight_profile_column,
    styled_table,
    with_user_names,
)
from app.ui.sizing import TABLE_H_LG

_PAGE = "Admin"
_EXPECTED_MIGRATIONS = {
    1: "core", 2: "facts", 3: "marts", 4: "alerts", 5: "actions", 6: "pipeline sla",
    7: "automation", 8: "chargeback", 9: "credentials", 10: "change impact",
    11: "proactive alerts", 12: "routing + anomaly sweep", 13: "user prefs",
    14: "lifecycle hardening", 15: "DT pilot + backups", 16: "closing loops",
    17: "hardening v7", 18: "delivery first-class", 19: "scoping fixes", 20: "credentials column",
    21: "precision + telemetry", 22: "delivery per route", 23: "prod-scoped volume",
    24: "warehouse change scorecard", 25: "break-glass policy", 26: "teams-safe delivery",
    27: "mart family + telemetry rider",
    28: "credential expiry 10d (rule + posture bucket)",
    29: "loader fix: role/schema-hour GROUP BY",
    30: "loader fix 2 (UDF outside aggregation) + posture inputs",
    31: "change-impact scan v2 + tag-coverage mart",
    32: "incident object (tables + lineage + auto-declare)",
    33: "change attribution (CHANGED_BY + DEPLOY_ACTORS)",
    34: "route company filter (sender v4, ALFA-only for now)",
    35: "lock-wait mart (page views never scan LOCK_WAIT_HISTORY)",
    36: "pattern-cost mart (measured $ per repeated statement)",
    37: "pattern mart v2: DATABASE_NAME grain + HLL users (compare env prep)",
    38: "ledger autobook (detected cost-lever changes settle themselves)",
    39: "pseudo-warehouse filter (CLOUD_SERVICES_ONLY out of the warehouse fact)",
    40: "freshness state table + 10-min snapshot (lookup, not 19 aggregates)",
    41: "loader efficiency: staged QH extract, xdim alloc fact, exec board v2, "
        "watermarks + nightly reconcile, loader-owned freshness, ops-diag + "
        "platform-score marts, posture riders",
    42: "codex r22: FACT_QUERY_DAILY, atomic extract + gated watermark, "
        "ops-diag backfill, purge coverage, AI fact usage stamps",
    43: "task retirement loader-side (fills/board/score/purge/reconcile/"
        "freshness + tables dropped, PIPE_TASK_FAILURES disabled) + r25 "
        "alert teeth (new-admin-network, egress spike)",
    44: "UNKNOWN classification (#18): evidence-based company both sides, "
        "COMPANY_SCOPE database mapping lever, board UNKNOWN scope",
    45: "owner correction: task monitoring restored (tables/procs/rule/"
        "refill; teeth + UNKNOWN scope kept); OVERWATCH_RM dropped",
    46: "storage truth: account tiers (stage/hybrid/archive) + per-DB "
        "monthly-average billing basis (COST_DB recon R3 / audit F1)",
    47: "pattern-cost mart includes Query Acceleration (Codex audit item 4)",
    48: "FACT_OBJECT_COST_DAILY object-cost ledger (measured split + serverless arms)",
    49: "write-target attribution (OBJECTS_MODIFIED joins the split; residual "
        "= no-read-no-write compute)",
    50: "one-pass object-cost loader (staged QAH + ACCESS_HISTORY) + read/write "
        "arm split (production vs consumption shares)",
    51: "action layer (scoped): SP_ALERT_LIFECYCLE atomic alert lifecycle + "
        "OW_ACTION_INTENTS idempotency",
    52: "exec-board loader windows gain 180/365 (mart-history window filter); "
        "live scans stay capped at 90",
    53: "typed savings link (FINDING_TYPE/TARGET_OBJECT) + monthly verifier "
        "re-derived to select app-booked rows (P1-A); no proc",
    54: "exec-board 180/365 windows read full history (V052 capped their source "
        "CTEs at 90); daily-fact retention floor raised 180->365",
    55: "per-query cloud-services credits persisted (OW_QH_EXTRACT column + "
        "MART_CLOUD_SVC_DAILY at shape/user grain) for the CS-ratio drill-down",
    56: "audit Batch B: FACT_QUERY_DAILY + nightly-reconcile day partial-freeze, "
        "ops-diag hour double-count, PIPE/SEC_CRED dedupe keys, cloud-svc company",
    57: "FAILS token fix: four SP_LOAD_MARTS_V27 arms counted EXECUTION_STATUS="
        "'FAILED' (never matches) -> 'FAIL', so efficiency/qfam/role-hr/schema-hr "
        "marts report real failures",
    58: "per-node loader-timing observability: MART_TASK_NODE_DAILY (queue + exec "
        "delay per task node) via a new contained arm in SP_LOAD_MARTS_V27; "
        "enables data-driven fan-out serialization / reconcile / de-collision",
    59: "task-graph pipeline credits: SP_LOAD_MARTS_V27 arm [6] rolls "
        "QUERY_ATTRIBUTION_HISTORY up by COALESCE(ROOT_QUERY_ID,QUERY_ID) so "
        "MART_TASK_GRAPH_DAILY.WH_CREDITS captures proc-body compute (audit #10)",
    60: "triage #5/#11 + alert guard: MART_QUERY_FAMILY_DAILY gains "
        "TOTAL_ELAPSED_SEC (COMPILE_PCT bounded 0-100 on post-V060 rows), "
        "schema-hourly queued includes provisioning, CS-ratio alert excludes "
        "CLOUD_SERVICES_ONLY",
    61: "AI loader/alert/score/purge correctness: AI usage arms day-aligned "
        "(C5), Query-Acceleration credits added to proc/pipeline attribution "
        "(C2), MTD budget/forecast alerts + platform score priced at the AI "
        "rate (C1), COST_AI_CREEP seeded (C6), self-alert block count 17->20 "
        "(B41), SP_PURGE_FACTS covers 3 more daily facts (B33)",
    62: "loader robustness + correctness: query fail predicate <> SUCCESS "
        "(R3-4, app+mart), backfill day-cap fix (B5), reconcile boundary-hour "
        "clamp (B10), hourly-facts watermark catch-up (B11), daily-facts/"
        "object-cost transaction wraps (B34), daily alert blocks split to "
        "SP_ALERT_SCAN_DAILY chained after TASK_LOAD_DAILY (C9). Webhook (B9) "
        "and perf loader (T3) deferred to V063",
    63: "webhook capture-once + daily-facts fail-guard: SP_NOTIFY_WEBHOOK freezes "
        "the fitting event set into ONE array so the message, the delivery ledger, "
        "and NOTIFIED_AT can't diverge (B9, no send-vs-ledger race; owner smoke "
        "test); SP_LOAD_DAILY_FACTS holds the watermark + returns non-success on a "
        "per-table failure instead of a false success (B34). Perf loader (T3) in V064",
    64: "webhook oldest-first bounded drain + per-source daily watermarks + "
        "contract-burn accuracy + telemetry columns: SP_NOTIFY_WEBHOOK drains the "
        "backlog in batches oldest-first so old alerts stop starving past the 24h "
        "window (rec8, owner smoke test); SP_LOAD_DAILY_FACTS + SP_NIGHTLY_RECONCILE "
        "keep per-source watermarks so one table's failure holds only its own mark "
        "(rec7); COST_CONTRACT_BREACH burns over trailing-30-complete-days not /30 "
        "(rec20-alert); APP_QUERY_TELEMETRY gains SAMPLE_PROB + QUERY_ID (rec18)",
    65: "alert run-rate window fixes (bug round 5): SP_ALERT_SCAN_DAILY re-derived so "
        "COST_FORECAST_BREACH (and the dead-column pace CTE) projects from a "
        "COMPLETE-days-only run-rate (was MTD$/day-of-month over a partial today -> "
        "under-projected -> suppressed the budget-breach alert; rank2), and COST_AI_CREEP "
        "compares equal today-excluded 7-complete-day windows (was THIS_WK 8d incl today "
        "vs PRIOR_WK 7d -> inflated WoW; rank3). No new objects. T3 perf loader still deferred",
    66: "alert escalation + serverless window + timeline atomicity (bug round 6): "
        "SP_ALERT_SCAN dedupe keys for PIPE_COPY_FAILURES (#1) and COST_DEPT_BUDGET_PACE "
        "(#11) gain a severity band so a within-bucket HIGH->CRITICAL / MEDIUM->HIGH "
        "crossing re-fires; COST_SERVERLESS_CREEP excludes today so both weeks are 7 "
        "complete days (#6); SP_ALERT_SCAN_DAILY COST_CONTRACT_BREACH weekly key gains a "
        "severity band (#2); SP_LOAD_MARTS_V27 incident-timeline arm [8] DELETE+INSERT "
        "wrapped in one transaction so a failed rebuild can't blank the trailing 48h (#3)",
    67: "alert attribution + serverless onset + escalation supersede + object-cost honesty "
        "(Codex review): SP_ALERT_SCAN COST_STORAGE_SURGE/PIPE_COPY_FAILURES use "
        "COMPANY_FOR_DATABASE not a raw TRXS%/ALFA guess (#22); COST_SERVERLESS_CREEP emits a "
        "999 onset sentinel when the prior week is 0 (#20); a post-scan sweep supersedes the "
        "lower-band OPEN alert when its higher-band sibling is open (#40, the V066 escalation "
        "follow-on); SP_LOAD_OBJECT_COST returns non-OK after a rolled-back load (#10)",
    68: "standalone-mart freshness stamps (screenshot finding): SP_LOAD_LOCK_WAIT_MART + "
        "SP_LOAD_PATTERN_COST gain the V041-R6 loader-owned SOURCE_FRESHNESS_STATE stamp "
        "they were never given when V041 retired the snapshot sweep (their rows froze at "
        "apply time; the Brief card showed MART_LOCK_WAIT_DAILY 448h stale). Stamped as a "
        "RUN timestamp so zero-event windows read fresh; migration tail heals both rows",
    69: "exec-board serverless + AI cost drivers (audit C5): SP_REFRESH_EXEC_BOARD built its "
        "COST_DRIVER rows from FACT_WAREHOUSE_DAILY alone, so serverless (auto-clustering, MV "
        "refresh, search optimization, snowpipe, serverless tasks) and AI/Cortex spend could "
        "never reach the Overview driver panel even as the fastest-growing line - while the "
        "same page's KPIs cover compute + serverless + AI. A second COST_DRIVER arm over "
        "FACT_METERING_DAILY excludes warehouse metering, prices AI credits at "
        "AI_CREDIT_PRICE_USD and the rest at CREDIT_PRICE_USD, keeps the same window "
        "semantics and column contract, and labels drivers 'Serverless:'/'AI/Cortex:' so the "
        "app needs no change. Account-level metering lands on the ALL scope only",
    70: "Teams-only delivery routing (V012/V018 forward fix): SP_DAILY_DIGEST hardcoded the "
        "retired Slack integration OVERWATCH_WEBHOOK and swallowed its send with WHEN OTHER "
        "THEN NULL, so the morning digest had NEVER been delivered on this Teams-only account "
        "while it still returned 'delivery attempted'. Re-derived from V018 to walk the "
        "enabled ALERT_ROUTES rows and send through each row's INTEGRATION_NAME "
        "(SP_NOTIFY_WEBHOOK's per-route idiom), ledgering each outcome to APP_ERROR_LOG as "
        "digest_send_failed instead of discarding it; the in-app write is untouched and the "
        "return is machine-readable 'sent N/M routes' (#23). Idempotent blocks disable any "
        "enabled route whose integration is absent so the dead default Slack route stops "
        "burying real errors (#25), and resume TASK_ALERT_NOTIFY when an enabled route "
        "resolves to a live integration so a healthy Teams integration satisfies the gate "
        "(#24). Only SP_DAILY_DIGEST is re-defined; no new objects",
    71: "task-graph re-chain + root retry policy (DAG surgery on applied tasks): Snowflake "
        "runs sibling child tasks in parallel, so readers hung off the roots as siblings of "
        "the extract/reconcile that feed them raced their own data. TASK_REFRESH_EXEC_BOARD + "
        "TASK_ALERT_SCAN re-pointed AFTER TASK_LOAD_HOURLY -> AFTER TASK_QH_EXTRACT (#3); "
        "TASK_LOAD_MARTS_V27_DAILY + TASK_PLATFORM_SCORE_DAILY + TASK_ALERT_SCAN_DAILY "
        "re-pointed AFTER TASK_LOAD_DAILY -> AFTER TASK_NIGHTLY_RECONCILE (#4; reconcile "
        "atomicity #7 still deferred). Both roots gain TASK_AUTO_RETRY_ATTEMPTS=1 + "
        "SUSPEND_TASK_AFTER_NUM_FAILURES=10 (#43); SCHEMA_VERSION.DESCRIPTION widened to "
        "VARCHAR(4000) before the guard (#42). ADD/REMOVE AFTER + SET only (no task body "
        "re-defined); each re-point is state-checked (ADD only if the new predecessor is "
        "absent, REMOVE only if the old is present, ADD before REMOVE) with no error "
        "swallowing, so a matched re-run is a no-op and any genuine ALTER failure aborts "
        "loudly leaving the graph suspended rather than silently orphaned; green-on-failure "
        "finalizer (#5) deferred. No new objects",
    72: "entity-aware incident proposals: groups open alerts by family, company, entity "
        "kind and entity name; exposes exact matched warehouse/object changes, task-failure "
        "evidence and confidence. Proposal keys carry entity scope so a human declaration "
        "links only matching members. View-only; auto-declare and lifecycle writes unchanged",
    73: "calendar triage windows: exec board materializes deduplicated Current month and "
        "Current year account-time offsets alongside the fixed rolling windows",
    74: "operating workbench: audited action lifecycle, evidence links, entity ownership, "
        "personal watchlists, optimization experiments and SLO objectives",
    75: "security posture facts: materialized login/change evidence and Trust Center deltas "
        "(read-only posture, no policy-editor or access-review write surfaces)",
    76: "anomaly materiality gate: SP_ANOMALY_SWEEP gates COST_ANOMALY_SWEEP on a $50 floor, "
        "10 active days, and a spike-vs-collapse baseline test (mirrors the app-side gate)",
    77: "cost by client application x user: FACT_APP_COST_DAILY joins SESSIONS "
        "(CLIENT_ENVIRONMENT:APPLICATION / driver) x QUERY_HISTORY x QUERY_ATTRIBUTION_HISTORY "
        "measured credits; daily loader task",
    78: "AI usage loader unbreak: SP_LOAD_MARTS_V27 ai_code arm casts USAGE_TIME "
        "(TIMESTAMP_TZ in the CORTEX_CODE_* views) to TIMESTAMP_NTZ so the arm stops "
        "failing every run; DAILY/365 first-fill included",
    79: "AI predicate historical split: SP_LOAD_PLATFORM_SCORE / SP_ALERT_SCAN_DAILY / "
        "SP_REFRESH_EXEC_BOARD broaden the AI service-type predicate to include Cortex "
        "Code / CoWork (SNOWFLAKE_COCO_SNOWSIGHT), and SP_ALERT_SCAN rewrites the "
        "COST_SERVERLESS_CREEP carve-out so CoCo is not double-alerted; re-fills score "
        "+ exec board",
    80: "Security change-risk ETL exclusion: V_SECURITY_EXCEPTION_QUEUE excludes "
        "DESTRUCTIVE (DROP/TRUNCATE) events by the 3 ETL-engine role families "
        "(Glue/Informatica/transform, across all environments) so routine "
        "truncate-and-reload stops flooding the queue; GRANT/REVOKE/POLICY and human "
        "drops still surface",
    81: "Unified experiment verify: SP_VERIFY_EXPERIMENT atomically records an "
        "experiment's verdict, upserts SAVINGS_LEDGER (keyed on ACTION_ID), and closes "
        "the source action so the experiment and savings ledgers can't diverge",
    82: "Query-family company regrain: MART_QUERY_FAMILY_DAILY gains COMPANY and is "
        "regrained to (DAY, QUERY_HASH, COMPANY) so query-family boards scope on the "
        "real company instead of the ANY_VALUE(DATABASE_NAME) heuristic; mart cleared, "
        "owner re-runs the HOURLY backfill",
    83: "Action estimate time-basis (DS #7): ACTION_QUEUE gains a nullable PERIOD "
        "column labeling the time basis of ESTIMATED_USD (MONTHLY / ANNUAL / ONE_TIME; "
        "NULL = unspecified) so authored estimates on different clocks are no longer "
        "summed as one number; the app stamps it at both writers and surfaces it",
    84: "SEC_NEW_EXPOSURE proactive alert (CoCo Sec36): seeds a SECURITY rule and adds "
        "arm [20] to SP_ALERT_SCAN that raises a HIGH alert when a new privilege was "
        "granted to PUBLIC in the last 24h (inherited by every role), reading "
        "GRANTS_TO_ROLES; re-derives SP_ALERT_SCAN from V079 (no table/task change)",
    85: "SLO-breach proactive alert (CoCo Control Room #16, notify half): seeds a "
        "PERFORMANCE rule PERF_SLO_BREACH and adds SP_SLO_BREACH_SCAN (a new raiser) "
        "+ a task serialized after the hourly mart load, which evaluates the configured "
        "SLO_OBJECTIVES and raises a HIGH alert per objective in breach (STALE/NO_DATA "
        "excluded, deduped per objective per day)",
    86: "Per-event alert snooze (CoCo Alerts29): ALTER ALERT_EVENTS ADD SNOOZED_UNTIL/"
        "SNOOZE_BY/SNOOZE_REASON; SP_ALERT_SNOOZE moves an OPEN/ACK event to "
        "STATUS='SNOOZED' with a server-computed wake time (audited, idempotent; hours=0 "
        "un-snoozes early) so it leaves the triage feed with no read-path change; a wake "
        "step in the re-derived SP_ALERT_SCAN (from V084) returns expired snoozes to their "
        "prior status",
    87: "Security finding -> monitored rule (CoCo Sec35): ALTER ALERT_CONFIG ADD "
        "METRIC_NAME; SP_ALERT_SCAN re-derived from V086 with a generic arm [21] that "
        "raises every enabled rule carrying a METRIC_NAME when its newest "
        "MART_SECURITY_POSTURE_DAILY reading is at/over THRESHOLD_NUM. Operators generate "
        "such rules from a Security finding (generate-INSERT UI); arm count 15->16",
    88: "Security change-risk ETL exclusion broadened (owner diagnostic 2026-08-17: "
        "94% of DESTRUCTIVE events are TF_* roles, 0% unattributed): "
        "V_SECURITY_EXCEPTION_QUEUE CHANGE RISK arm now excludes DESTRUCTIVE events by "
        "any Terraform service role (TF_* pattern, generalizing V080's fixed 18-role "
        "list that the flood outran) and on the app's own DBA_MAINT_DB.PUBLIC (narrowed "
        "to PUBLIC so audit-table drops stay visible). Still DESTRUCTIVE-only and "
        "NULL-safe; GRANT/REVOKE/POLICY and any non-TF DROP still surface; rows stay in "
        "FACT_SECURITY_CHANGE for audit. View-only, no reload",
}
# tests/test_perf_budgets.py locks this dict against snowflake/migrations/ —
# adding a migration without updating it fails CI (Codex r3 #1: the panel
# reported "all applied" while V021-V025 were missing from the expectation).


def _context_section() -> None:
    ctx = run(
        "SELECT CURRENT_ACCOUNT() AS ACCOUNT, CURRENT_REGION() AS REGION, CURRENT_ROLE() AS ROLE, "
        "CURRENT_WAREHOUSE() AS WAREHOUSE, CURRENT_VERSION() AS SNOWFLAKE_VERSION",
        page=_PAGE, key="context", tier="metadata", source="session context",
    )
    if ctx.usable():
        row = ctx.df.iloc[0]
        kpi_row([
            {"label": "Role", "value": str(row.get("ROLE", "?"))},
            {"label": "Warehouse", "value": str(row.get("WAREHOUSE", "?") or "none")},
            {"label": "Account", "value": str(row.get("ACCOUNT", "?"))},
            {"label": "App version", "value": APP_VERSION},
        ])
    elif not ctx.ok:
        st.error(f"No Snowflake session: {ctx.error}")


# rec45: typed editors for the "Change a setting" changer. Driven by an EXPLICIT
# per-key type map, NOT isinstance on the stored value — SCORE_PTS_*/GOV_PTS_*/
# *_RETENTION_DAYS are STRING-typed numbers in DEFAULT_SETTINGS, so probing the
# value's Python type would send every one of them through the wrong widget. Any
# key ABSENT from this map keeps the generic st.text_input (the safe default).
# Only the INPUT widget changes; the widget value is converted back to its stored
# STRING form (dates -> ISO, numbers -> str) before the unchanged upsert SQL runs.
_NUM = "number"
_SETTING_EDITORS: dict[str, tuple[str, object]] = {
    "FORECAST_ENGINE": ("enum", ["linear", "seasonal", "ml_forecast"]),
    "CONTRACT_START_DATE": ("date", None),
    "CONTRACT_END_DATE": ("date", None),
    # Rates / prices ($ per unit).
    "CREDIT_PRICE_USD": (_NUM, {"min_value": 0.0, "step": 0.01}),
    "AI_CREDIT_PRICE_USD": (_NUM, {"min_value": 0.0, "step": 0.01}),
    "STORAGE_USD_PER_TB_MONTH": (_NUM, {"min_value": 0.0, "step": 0.01}),
    "STORAGE_STAGE_USD_PER_TB_MONTH": (_NUM, {"min_value": 0.0, "step": 0.01}),
    "STORAGE_HYBRID_USD_PER_TB_MONTH": (_NUM, {"min_value": 0.0, "step": 0.01}),
    "STORAGE_ARCHIVE_COOL_USD_PER_TB_MONTH": (_NUM, {"min_value": 0.0, "step": 0.01}),
    "STORAGE_ARCHIVE_COLD_USD_PER_TB_MONTH": (_NUM, {"min_value": 0.0, "step": 0.01}),
    # Budgets / contract credits (dollars / credits).
    "MONTHLY_BUDGET_USD": (_NUM, {"min_value": 0.0, "step": 100.0}),
    "AI_MONTHLY_BUDGET_USD": (_NUM, {"min_value": 0.0, "step": 100.0}),
    "CONTRACT_CREDITS": (_NUM, {"min_value": 0.0, "step": 1000.0}),
    # Platform-score weights (per-unit penalties; STRING-typed in DEFAULT_SETTINGS).
    "SCORE_PTS_BUDGET_PER_PCT": (_NUM, {"min_value": 0.0, "step": 0.1}),
    "SCORE_PTS_PER_CRITICAL": (_NUM, {"min_value": 0.0, "step": 0.5}),
    "SCORE_PTS_PER_HIGH": (_NUM, {"min_value": 0.0, "step": 0.5}),
    "SCORE_PTS_QUERY_FAIL_PER_PCT": (_NUM, {"min_value": 0.0, "step": 0.1}),
    "SCORE_PTS_TASK_FAIL_PER_PCT": (_NUM, {"min_value": 0.0, "step": 0.1}),
    "SCORE_PTS_QUEUE_PER_MIN": (_NUM, {"min_value": 0.0, "step": 0.1}),
    "SCORE_PTS_SPILL_PER_GB": (_NUM, {"min_value": 0.0, "step": 0.1}),
    "SCORE_PTS_PER_STALE_SOURCE": (_NUM, {"min_value": 0.0, "step": 0.5}),
    "SCORE_PTS_PER_OPEN_ACTION": (_NUM, {"min_value": 0.0, "step": 0.5}),
    # Governance-drift weights (per-unit penalties; STRING-typed in DEFAULT_SETTINGS).
    "GOV_PTS_MFA_GAP": (_NUM, {"min_value": 0.0, "step": 0.5}),
    "GOV_PTS_EXPIRED_CRED": (_NUM, {"min_value": 0.0, "step": 0.5}),
    "GOV_PTS_EXPIRING_CRED": (_NUM, {"min_value": 0.0, "step": 0.5}),
    "GOV_PTS_BREAKGLASS_GRANT": (_NUM, {"min_value": 0.0, "step": 0.5}),
    "GOV_PTS_NO_AUTOSUSPEND": (_NUM, {"min_value": 0.0, "step": 0.5}),
    # Fact/log retention (whole days; STRING-typed in DEFAULT_SETTINGS).
    "FACT_RETENTION_DAYS_HOURLY": (_NUM, {"min_value": 1.0, "step": 1.0}),
    "FACT_RETENTION_DAYS_DAILY": (_NUM, {"min_value": 1.0, "step": 1.0}),
    "ERROR_LOG_RETENTION_DAYS": (_NUM, {"min_value": 1.0, "step": 1.0}),
    "APP_USAGE_RETENTION_DAYS": (_NUM, {"min_value": 1.0, "step": 1.0}),
}


def _num_to_str(value: float) -> str:
    """Stored settings are strings ('400', '3.68'), so trim the float a
    number_input hands back: an integral value writes '400' not '400.0'."""
    f = round(float(value), 6)   # trim binary-float noise: 0.30000000000000004 -> 0.3
    return str(int(f)) if f == int(f) else str(f)


def _setting_value_input(key: str, current: dict[str, str]) -> str:
    """rec45: render the type-appropriate editor for ``key`` and return the value
    already converted to its stored STRING form (so the upsert SQL is unchanged)."""
    cur = current.get(key, "")
    if cur in ("", None) and key in DEFAULT_SETTINGS:
        cur = str(DEFAULT_SETTINGS[key])
    editor = _SETTING_EDITORS.get(key)
    wkey = f"adm_setting_value::{key}"  # per-key so switching widget type never collides
    if editor is None:
        return st.text_input(
            "New value", key=wkey,
            help="Numeric settings take numbers; dates are YYYY-MM-DD; blank clears.")
    kind, spec = editor
    if kind == "enum":
        options = list(spec)  # type: ignore[arg-type]
        idx = options.index(cur) if cur in options else 0
        return st.selectbox("New value", options, index=idx, key=wkey)
    if kind == "date":
        try:
            default = dt.date.fromisoformat(cur) if cur else None
        except ValueError:
            default = None
        picked = st.date_input("New value", value=default, key=wkey,
                               min_value=dt.date(2000, 1, 1), max_value=dt.date(2100, 12, 31),
                               help="Clear the field to blank the date.")
        return picked.isoformat() if picked else ""
    # number
    spec = dict(spec or {})  # type: ignore[arg-type]
    try:
        value = float(cur) if cur not in ("", None) else float(spec.get("min_value", 0.0))
    except (TypeError, ValueError):
        value = float(spec.get("min_value", 0.0))
    return _num_to_str(st.number_input("New value", value=value, key=wkey, **spec))


def _settings_tab(is_operator: bool) -> None:
    settings = load_settings(_PAGE)
    # $-escape: the two literal rates would pair into a LaTeX math span
    st.caption(md_dollars(f"Values from: {settings.get('_source')}. Rates confirmed 2026-07: $3.68 compute / $2.20 Cortex."))
    panel_help(
        "Every tunable the app reads — credit/Cortex rates, monthly budget, platform-score "
        "weights and alert thresholds — plus who last changed each. Edit a value below "
        "(operators only; it takes effect within one cache cycle); a row flagged 'no longer "
        "read' is safe to delete."
    )
    # Deliberately live (audit rule, r24 #8): SETTINGS is an operator-edit surface, so
    # a concurrent admin's change must surface within 30s, not one cache cycle. The
    # perf T1.11 retier was declined here to keep that guarantee — see test_codex_r24.
    res = run(mart_sql.settings(), page=_PAGE, key="settings_table", tier="live",
              source="SETTINGS")
    if guard(res, "SETTINGS is empty.", setup_hint="Run migration V001 to create and seed it."):
        styled_table(with_user_names(res.df, _PAGE, user_col="UPDATED_BY", display_col="Updated by"))
        result_caption(res)
        # r27 H2: keys the app no longer reads (retired features leave rows
        # behind — SCORE_PTS_TASK_FAIL_PER_PCT after V043, for instance).
        try:
            _known = {k for k in DEFAULT_SETTINGS if not k.startswith("_")}
            _orphans = sorted(set(res.df["KEY"].astype(str)) - _known)
            if _orphans:
                st.warning("Settings rows the app no longer reads (safe to delete): "
                           + ", ".join(_orphans))
        except (KeyError, TypeError):
            pass

    st.markdown("**Change a setting**")
    # rec45: seed the typed editors from the live values so number/date/enum
    # widgets open on the current setting, not an empty box.
    current: dict[str, str] = {}
    if res.usable():
        try:
            current = dict(zip(res.df["KEY"].astype(str), res.df["VALUE"].astype(str),
                               strict=False))
        except (KeyError, TypeError):
            current = {}
    editable = [k for k in DEFAULT_SETTINGS if not k.startswith("_")]
    key = st.selectbox("Setting", editable, key="adm_setting_key")
    new_value = _setting_value_input(key, current)
    update_sql = (
        f"UPDATE {core_object('SETTINGS')} SET VALUE = {sql_literal(new_value)}, "
        f"UPDATED_AT = CURRENT_TIMESTAMP(), UPDATED_BY = {identity_sql()} "
        f"WHERE KEY = {sql_literal(key)};"
    )
    st.code(update_sql, language="sql")
    if is_operator:
        # rec42: one type-to-confirm gate (setting KEY, EXACT case) + action button.
        if confirm_gate(key, "Execute update", key="adm_setting",
                        prompt="Type the setting key to confirm"):
            ok, msg = execute_statement(update_sql, page=_PAGE)
            notify(ok, msg)
            if ok:
                st.caption("New value takes effect within one cache cycle (≤5 min) or after Refresh.")
    else:
        st.caption("Executing requires SNOW_ACCOUNTADMINS / SNOW_SYSADMINS; anyone can copy the SQL for review.")


def _migrations_tab() -> None:
    panel_help(
        "Compares the applied SCHEMA_VERSION rows against the migrations this app build expects. "
        "A 'Missing migrations' warning means the deployment is behind — run them in order "
        "(DEPLOYMENT.md); the Source freshness panel below reads stale when a loader task has "
        "stopped, with a per-source cause and the backfill to run."
    )
    res = run(mart_sql.schema_version(), page=_PAGE, key="schema_version", tier="metadata",  # r24 #8: changes only at migrations
              source="SCHEMA_VERSION")
    if not res.ok:
        st.error(f"Cannot read SCHEMA_VERSION: {res.error}")
        st.info("Run snowflake/migrations/V001__core.sql first.")
        return
    applied = set()
    if not res.empty:
        applied = {int(v) for v in pd.to_numeric(res.df["VERSION"], errors="coerce").dropna()}
        styled_table(res.df)
    missing = [f"V{n:03d} ({name})" for n, name in _EXPECTED_MIGRATIONS.items() if n not in applied]
    if missing:
        # $-escape: migration descriptions carry '$' (V036 "measured $", V065 "MTD$/...") —
        # joined into one warning they pair into a math span on exactly the fresh-install path
        st.warning(md_dollars("Missing migrations: " + ", ".join(missing) + ". Run them in order (DEPLOYMENT.md)."))
    else:
        st.success(f"All {len(_EXPECTED_MIGRATIONS)} migrations applied. App {APP_VERSION} expects exactly these.")

    fh = run(mart_sql.flyway_history(), page=_PAGE, key="flyway_history", tier="recent",  # r24 #8: external ledger probe
             source="flyway_schema_history (Flyway ledger)", probe=True)
    if fh.usable():
        st.markdown("**Flyway deploy history** — the transport's own ledger")
        styled_table(fh.df, height=220)
        st.caption("Flyway owns WHAT ran WHEN once adopted; SCHEMA_VERSION above stays "
                   "the app's contract check (and the in-file guards stay as defense "
                   "against Snowsight bypass). Adoption runbook: docs/FLYWAY_ADOPTION.md.")
    else:
        st.caption("Flyway not detected — SCHEMA_VERSION above is authoritative. When "
                   "procurement lands, docs/FLYWAY_ADOPTION.md is the adoption runbook; "
                   "this panel lights up on its own once flyway_schema_history exists.")

    st.markdown("**Source freshness**")
    fresh = run_mart_first(
        mart_sql.source_freshness_state(), mart_sql.source_freshness(),
        page=_PAGE, key="adm_freshness",
        mart_source="SOURCE_FRESHNESS_STATE (10-min snapshot)",
        live_source="MART_SOURCE_FRESHNESS (aggregate view, pre-V040 fallback)",
        mart_tier="recent", live_tier="recent")   # state moves every 10 min (r14 #13)
    if guard(fresh, "Freshness view empty — have the loader tasks run yet?",
             setup_hint="Tasks resume at the end of V004. Check SHOW TASKS IN SCHEMA DBA_MAINT_DB.OVERWATCH."):
        styled_table(fresh.df)
        if st.toggle("Diagnose stale sources", key="adm_stale_diagnose"):
            # The deploy-gap week (2026-07): stale marts meant a failing
            # loader, a never-run backfill, or a suspended task. Map each
            # stale source to its likeliest cause from evidence we hold.
            errs = run(mart_sql.app_error_log(100), page=_PAGE, key="adm_stale_errs",
                       tier="live", source="APP_ERROR_LOG")
            try:
                stale = fresh.df[fresh.df["HOURS_SINCE_LOAD"].astype(float) > 26]
            except (KeyError, TypeError, ValueError):
                stale = fresh.df.iloc[0:0]
            if stale.empty:
                st.success("Nothing stale past 26h — the loaders are keeping up.")
            for _, s in stale.iterrows():
                name = str(s["SOURCE_NAME"])
                hint = ""
                if float(s.get("ROW_COUNT", 0) or 0) == 0:
                    hint = ("never filled — run the backfill "
                            "(snowflake/backfill_365.sql: HOURLY 90, then DAILY 365).")
                if errs.ok and not errs.empty:
                    _m = errs.df[errs.df.apply(
                        lambda r, _n=name: _n in str(r.get("CONTEXT", ""))
                        or _n in str(r.get("ERROR_MESSAGE", "")), axis=1)]
                    if not _m.empty:
                        _r0 = _m.iloc[0]
                        hint = (f"last loader error {_r0['LOGGED_AT']}: "
                                f"{str(_r0['ERROR_MESSAGE'])[:160]}")
                st.markdown(f"- **{name}** — {humanize_duration(s['HOURS_SINCE_LOAD'], 'h')} since load. "
                            + (hint or "no matching error logged — check SHOW TASKS "
                                       "(tasks suspend if a migration half-applied)."))


_SCAN_NOTE = ("First load scans ACCOUNT_USAGE directly (a few seconds on a cold "
              "cache); results cache for an hour, so repeat views are instant.")


def _self_cost_tab() -> None:
    st.caption(
        "The monitoring app and loader tasks share WH_ALFA_ADMIN (XSMALL, 60-second "
        "auto-suspend). Every interactive app query carries an OVERWATCH query tag, so "
        "the table separates UI traffic from loader and task work without another warehouse."
    )
    st.caption(_SCAN_NOTE)
    res = run(mart_sql.app_self_cost(14), page=_PAGE, key="self_cost", tier="historical",
              source="ACCOUNT_USAGE.QUERY_HISTORY (WH_ALFA_ADMIN, split by query tag)")
    if guard(res, "No OVERWATCH-tagged or app-warehouse queries in the last 14 days (fresh install)."):
        df = res.df.copy()
        total = int(pd.to_numeric(df["APP_QUERIES"], errors="coerce").fillna(0).sum())
        failed = int(pd.to_numeric(df["FAILED"], errors="coerce").fillna(0).sum())
        kpi_row([
            {"label": "App queries (14d)", "value": f"{total:,}"},
            {"label": "Failed", "value": f"{failed:,}",
             "delta_color": "inverse" if failed else "off"},
        ])
        styled_table(df)
        result_caption(res)


def _access_self_check() -> None:
    """r27 H3: probe every privileged source the app reads and hand back the
    exact missing grant — the next access error becomes a checklist row,
    not a debugging session."""
    st.markdown("**Access self-check**")
    st.caption("Probes each privileged source with a 1-row read. Run after a rebuild, "
               "a role change, or when any panel reports an access error.")
    if not st.button("Run access self-check", key="adm_access_check"):
        return
    probes = [
        # P10: DATABASES, not QUERY_HISTORY. The grant being probed is IMPORTED
        # PRIVILEGES on the SNOWFLAKE database, which is all-or-nothing across
        # the ACCOUNT_USAGE schema — so any view in it proves the same thing.
        # QUERY_HISTORY is the most expensive one to touch (~10s even for LIMIT 1
        # against an account-lifetime view); DATABASES is a handful of rows and
        # answers instantly. Identical semantics, 10 seconds off the self-check.
        ("ACCOUNT_USAGE core", "SELECT 1 FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASES LIMIT 1",
         "GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE TO ROLE SNOW_ACCOUNTADMINS; (roles.sql)"),
        ("LOGIN_HISTORY", "SELECT 1 FROM SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY LIMIT 1",
         "Covered by IMPORTED PRIVILEGES — if core is OK and this is not, contact Snowflake."),
        ("CREDENTIALS", "SELECT 1 FROM SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS LIMIT 1",
         "Newer accounts expose this view by default; older ones need Snowflake to enable it."),
        ("DATA_TRANSFER_HISTORY", "SELECT 1 FROM SNOWFLAKE.ACCOUNT_USAGE.DATA_TRANSFER_HISTORY LIMIT 1",
         "Covered by IMPORTED PRIVILEGES (Security -> Egress reads this)."),
        ("Trust Center findings", "SELECT 1 FROM SNOWFLAKE.TRUST_CENTER.FINDINGS LIMIT 1",
         "GRANT APPLICATION ROLE SNOWFLAKE.TRUST_CENTER_VIEWER TO ROLE SNOW_ACCOUNTADMINS;"),
        ("App schema", "SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SETTINGS LIMIT 1",
         "Run snowflake/roles.sql as SNOW_ACCOUNTADMINS."),
        ("Warehouse metadata", "SHOW WAREHOUSES", "USAGE/MONITOR on warehouses (roles.sql)."),
    ]
    rows = []
    for name, sql, fix in probes:
        r = run(sql, page=_PAGE, key=f"acc_{name}", tier="metadata", source=name,
                max_rows=0 if sql.startswith("SHOW") else 1)
        rows.append({"SOURCE": name, "STATUS": "OK" if r.ok else "BLOCKED",
                     "FIX": "" if r.ok else fix,
                     "ERROR": "" if r.ok else str(r.error)[:140]})
    _df = pd.DataFrame(rows)
    blocked = int((_df["STATUS"] == "BLOCKED").sum())
    if blocked == 0:
        st.success(f"All {len(_df)} sources reachable.")
    else:
        st.error(f"{blocked} source(s) blocked — fixes below.")
    styled_table(_df)
    st.divider()


def _observability_tab() -> None:
    _access_self_check()
    st.markdown("**Recent app errors (this session)**")
    buffer = error_buffer()
    if not buffer:
        st.success("No errors recorded in this session.")
    else:
        styled_table(pd.DataFrame(buffer)[["at", "page", "type", "message"]])
    sink = run(mart_sql.app_error_log(100), page=_PAGE, key="error_sink", tier="live",
               source="APP_ERROR_LOG")
    st.markdown("**Persisted error log (all sessions)**")
    if sink.ok and sink.empty:
        st.success("Error sink is empty.")
    elif guard(sink, "", setup_hint="Sink table comes from V001."):
        # r27 H4: repeated identical errors read as ONE family, not N rows.
        _e = sink.df.copy()
        try:
            _e["FAMILY"] = (_e["ERROR_TYPE"].astype(str) + " · "
                            + _e["ERROR_MESSAGE"].astype(str).str.slice(0, 60))
            grouped = (_e.groupby(["PAGE", "FAMILY"], as_index=False)
                       .agg(COUNT=("FAMILY", "size"), FIRST_SEEN=("LOGGED_AT", "min"),
                            LAST_SEEN=("LOGGED_AT", "max")))
            styled_table(grouped.sort_values("LAST_SEEN", ascending=False), height=240)
            with st.expander(f"Raw rows ({len(_e)})"):
                styled_table(_e)
        except (KeyError, TypeError):
            styled_table(sink.df)

    st.markdown("**Query telemetry (this session)**")
    telemetry = query_telemetry()
    if telemetry.empty:
        st.caption("No queries have run yet this session.")
    else:
        _tel = telemetry.sort_values("at", ascending=False)
        _tel, _tel_cfg = snowsight_profile_column(_tel, _PAGE, id_col="query_id")
        styled_table(_tel, column_config=_tel_cfg or None)
        st.caption("Profile is unavailable for cache hits because no Snowflake query ran.")

    if st.button("Refresh all cached data", key="adm_refresh"):
        bump_refresh_salt()
        st.rerun()


def _performance_tab() -> None:
    """Prove (or disprove) that the app is fast: its own statement stats."""
    st.markdown("**Performance SLO scorecard (7d)**")
    slo = run(
        mart_sql.app_performance_slo(7), page=_PAGE, key="app_perf_slo",
        tier="recent", source="APP_USAGE + APP_QUERY_TELEMETRY",
    )
    if slo.ok and slo.empty:
        st.caption("Performance guardrails appear after first-paint and fetch telemetry accumulates.")
    elif guard(slo, ""):
        sdf = slo.df.copy()
        states = sdf["SLO_STATE"].astype(str)
        kpi_row([
            {"label": "Passing pages", "value": f"{int((states == 'PASS').sum())}"},
            {"label": "Failing pages", "value": f"{int((states == 'FAIL').sum())}",
             "delta_color": "inverse" if (states == "FAIL").any() else "off"},
            {"label": "Watch / insufficient", "value": f"{int(states.isin(('WATCH', 'INSUFFICIENT')).sum())}"},
        ])
        styled_table(sdf, height=300, sort_label="SLO state then p95 render time")
        st.caption(
            "Render p95 uses complete APP_USAGE first paints (minimum n=20). Failure and "
            "cache rates are sample-reweighted; batch-wall telemetry is excluded from totals."
        )
        result_caption(slo)

    st.caption(
        "Every fetch the app persisted, grouped by page and query key — the slowest "
        "rows are the builders worth optimizing next, and the key names the call site. "
        "The OVERWATCH-tagged WH_ALFA_ADMIN statement-family scan (grouped by parameterized "
        "hash, with bytes scanned) is one toggle below."
    )
    telemetry = query_telemetry()
    if not telemetry.empty:
        served = len(telemetry)
        fast = int((telemetry["elapsed_ms"] < 50).sum())
        kpi_row([
            {"label": "Statements this session", "value": f"{served:,}"},
            {"label": "Served in <50ms", "value": f"{fast / served * 100:.0f}%",
             "help": "Approximates the cache-hit rate: sub-50ms answers never left Streamlit's cache."},
            {"label": "Failed", "value": f"{int((~telemetry['ok']).sum())}",
             "delta_color": "inverse" if (~telemetry["ok"]).any() else "off"},
        ])
    # P11: served from the app's OWN persisted telemetry by default. The old
    # default was a 7-day ACCOUNT_USAGE.QUERY_HISTORY scan (4.6s) — this panel
    # exists to prove the app is fast and was itself one of the app's slowest
    # reads. APP_QUERY_TELEMETRY already has the timings at a better grain
    # (page + query key names the CALL SITE, not just a statement hash). The
    # one thing it cannot have is BYTES_SCANNED, so the scan stays one click away.
    res = run(mart_sql.app_statement_stats_telemetry(7), page=_PAGE, key="app_stmt_tel",
              tier="recent", source="APP_QUERY_TELEMETRY (the app's own fetch log)")
    if guard(res, "No fetches persisted in the last 7 days.",
             setup_hint="Needs migration V021 + a roles.sql re-run (APP_QUERY_TELEMETRY INSERT grant)."):
        _stmt, _stmt_cfg = snowsight_profile_column(
            res.df, _PAGE, id_col="SLOWEST_QUERY_ID", label="Slowest profile")
        styled_table(_stmt,  # rec21
                     column_config={
                         "MEDIAN_S": st.column_config.Column("Median"),
                         "P95_S": st.column_config.Column("p95"),
                         "EST_WAIT_S": st.column_config.Column("Est. wait"),
                         "CACHE_HIT_PCT": st.column_config.NumberColumn("Cache hit %", format="%.1f%%"),
                         **_stmt_cfg,
                     })
        result_caption(res)
        st.caption(
            "Ranked by EST_WAIT_S — estimated total fleet seconds spent on that key, "
            "sample-reweighted. RUNS is what was PERSISTED (every slow/failed fetch plus "
            "a ~2% healthy sample), so MEDIAN_S/P95_S read high; EST_RUNS is the "
            "re-weighted true count. Slowest profile opens the longest persisted "
            "server query for that call site; cache-only rows have no profile."
        )
    if st.checkbox("Also scan ACCOUNT_USAGE for bytes scanned (slower)", key="adm_stmt_scan",
                   help="The GB-scanned figure only exists in QUERY_HISTORY — this is the "
                        "one thing the app's own telemetry cannot record."):
        st.caption(_SCAN_NOTE)
        scan = run(mart_sql.app_statement_stats(7), page=_PAGE, key="app_stmt_stats",
                   tier="historical", source="ACCOUNT_USAGE.QUERY_HISTORY (tagged WH_ALFA_ADMIN)")
        if guard(scan, "No statements on the app warehouse in the last 7 days.",
                 setup_hint="Stats appear after OVERWATCH-tagged app traffic runs on WH_ALFA_ADMIN."):
            # House convention: styled_table, not a raw st.dataframe — it carries the
            # status/delta coloring, header prettifier, pinned identity column, and the
            # self-identifying CSV export that every other table on the page has.
            _scan, _scan_cfg = snowsight_profile_column(
                scan.df, _PAGE, id_col="SLOWEST_QUERY_ID", label="Slowest profile")
            styled_table(
                _scan,
                column_config={
                    "MEDIAN_S": st.column_config.Column("Median"),
                    "P95_S": st.column_config.Column("p95"),
                    "AVG_GB_SCANNED": st.column_config.NumberColumn("Avg GB scanned", format="%.3f"),
                    **_scan_cfg,
                })
            result_caption(scan)
            st.caption("Interactive statements only; the OVERWATCH query tag excludes loader and task work.")

    st.markdown("**Page adoption (30d)**")
    usage = run(mart_sql.app_usage_summary(30), page=_PAGE, key="app_usage", tier="recent",
                source="APP_USAGE")
    if usage.ok and usage.empty:
        st.info("No visits logged yet (logging starts after V016 + a roles.sql re-run).")
    elif guard(usage, "", setup_hint="APP_USAGE comes with migration V016; re-run roles.sql for the grant."):
        styled_table(usage.df)
        st.caption("Merging or retiring sections should follow this table, not opinions.")

    st.markdown("**Fleet slow/failed fetches (all viewers, 7d)**")
    fq = run(mart_sql.fleet_query_stats(7), page=_PAGE, key="fleet_qstats", tier="recent",
             source="APP_QUERY_TELEMETRY (V021)")
    if not fq.ok:
        st.info("Needs migration V021 + a roles.sql re-run (APP_QUERY_TELEMETRY INSERT grant).")
    elif fq.empty:
        st.success("No slow (≥2s) or failed fetches persisted in 7 days — every viewer is "
                   "riding the cache.")
    else:
        _fq, _fq_cfg = snowsight_profile_column(
            fq.df, _PAGE, id_col="SLOWEST_QUERY_ID", label="Slowest profile")
        styled_table(_fq, height=280, column_config=_fq_cfg or None)
        st.caption(
            "Only fetches ≥2s or failed are persisted, plus a ~2% healthy sample "
            "(fire-and-forget, 60/session cap) — an EXCEPTION-WEIGHTED sample, so "
            "p50/p95 here read HIGHER than true fleet latency (r22 #20; weighted "
            "stats are queued). This is the regression surface across every user, "
            "not a complete census. Slowest profile opens the longest persisted server "
            "query for each row. Per-session telemetry lives on Errors & telemetry."
        )
    _perf_rider_panels(fq.df if fq.ok and not fq.empty else None)


def _perf_rider_panels(fq_df=None) -> None:
    """V027 telemetry-rider readouts (Codex r6 #8, #12, #19)."""
    st.markdown("**Fleet telemetry by page (7d)**")
    tbp = run(mart_sql.telemetry_by_page(7), page=_PAGE, key="tel_by_page", tier="recent",
              source="APP_QUERY_TELEMETRY (persisted = slow/failed + 2% sample)")
    if tbp.usable():
        styled_table(tbp.df, height=260, column_config={
            "CACHE_HIT_PCT": st.column_config.NumberColumn("Cache hit %", format="%.1f%%"),
        })
        st.caption("Cache-hit % covers PERSISTED fetches only (slow/failed always + the 2% "
                   "healthy sample) and rows new enough to carry CACHE_HIT — a floor, not a census.")
        # Ranked next-tuning-targets (Codex r7 #3, minus the speculative
        # "likely fix" text). D5: PAIN is now EST_WAIT_S — the sample-reweighted
        # total seconds the fleet waited on the page — not p95 x slow-count.
        # The old metric could only see fetches that crossed the 2s persist
        # threshold, so a page whose real cost is tens of thousands of 1.4s
        # fetches scored ZERO pain while a page with three 30s outliers topped
        # the board. Ranking on summed wait puts the cheap-but-constant pages
        # where they belong; p95 stays on the table as the severity lens.
        _tt = tbp.df.copy()
        try:
            _tt["PAIN"] = _tt["EST_WAIT_S"].astype(float).round(1)
            # D5: a page with no measurable wait is not a tuning target — it used
            # to fill the board with PAIN=0 rows whenever fewer than five pages
            # had any, and every one of those was an un-drillable dead end.
            _tt = _tt[_tt["PAIN"] > 0].sort_values("PAIN", ascending=False).head(5)
            st.markdown("**Next tuning targets** — pain = estimated total fleet seconds "
                        "waited (sample-reweighted); the telemetry picks, not opinions.")
            if _tt.empty:
                st.caption("No page has measurable persisted wait in 7d — nothing to rank.")
                _sel = None
            else:
                _sel = selectable_table(
                    _tt[["PAGE", "PAIN", "P95_S", "SLOW_2S", "FAILED"]],  # r24: CACHE_HIT_PCT off — 0.0 by construction until weighted telemetry (review #3/#4)
                    key="adm_tt_sel", height=160)
                st.caption(
                    "Sub-2s pain is invisible here except through the ~2% healthy sample; "
                    "p95 is exception-weighted (every ≥2s fetch persists, almost no fast one "
                    "does) and reads higher than true fleet latency. PAIN re-weights by "
                    "1/sample-probability, which is why it can rank a page the p95 column does not."
                )
            # Codex r8 #1: click a target, see the slow keys behind the pain
            if _sel is not None:
                # selectable_table returns a POSITIONAL index into the frame
                # it displayed. v4.23.0 subscripted the int like a row, hit
                # TypeError, and the except below silently ate every click
                # (Joe 2026-07-11: "the screen flashes and does nothing").
                _pg = str(_tt.iloc[int(_sel)]["PAGE"])
                _det = None if fq_df is None else fq_df[fq_df["PAGE"].astype(str) == _pg]
                # C6: fq_df is the fleet list, LIMIT 40 by p95 — a page can top the
                # PAIN board and still have every slow key below that cut. Absence
                # from fq_df is therefore NOT evidence of absence, and the old
                # caption asserted two things ("nothing persisted" and "the pain is
                # sub-2s") that could both be false. Ask for the page directly
                # before saying anything.
                if _det is None or _det.empty:
                    _one = run(mart_sql.fleet_query_stats(7, page=_pg), page=_PAGE,
                               key=f"fleet_qstats_pg:{_pg}", tier="recent",
                               source="APP_QUERY_TELEMETRY (V021), this page only")
                    _det = _one.df if _one.usable() else None
                if _det is None or _det.empty:
                    st.caption(f"{_pg}: no slow (≥2s) or failed fetch persisted for this page "
                               "in 7d — its wait is spread across sub-2s fetches, which only "
                               "the ~2% sample sees.")
                else:
                    st.markdown(f"**{_pg} — the slow keys behind the pain (7d persisted)**")
                    styled_table(_det, height=200)
        except (KeyError, TypeError, ValueError) as exc:
            # never silent: a broken drill must say so, not flash and shrug
            st.caption(f"Tuning-target drill unavailable — {type(exc).__name__}: {str(exc)[:80]}")
    else:
        st.caption("Per-page telemetry appears after V027 and a day of traffic.")

    st.markdown("**Usage events (30d) & remediation acceptance (90d)**")
    ue = run(mart_sql.usage_event_summary(30), page=_PAGE, key="usage_events", tier="recent",
             source="APP_USAGE.EVENT_KIND (V027 rider)")
    if ue.usable():
        styled_table(ue.df, height=190)
        st.caption("page_visit dominates by design; the interaction kinds (acks, resolves, "
                   "exports, remediations) are the operator-effectiveness signal.")
    acc = run(mart_sql.acceptance_funnel(90), page=_PAGE, key="acceptance_funnel", tier="recent",
              source="REMEDIATION_LOG + SAVINGS_LEDGER")
    if acc.usable():
        a = acc.df.iloc[0]
        def _n(k):
            try:
                return f"{float(a.get(k) or 0):,.0f}"
            except (TypeError, ValueError):
                return "0"
        kpi_row([
            {"label": "Fixes executed / copied / failed",
             "value": f"{_n('FIXES_EXECUTED')} / {_n('FIXES_COPIED')} / {_n('FIXES_FAILED')}"},
            {"label": "Savings est -> verified / rejected",
             "value": f"{_n('SAVINGS_ESTIMATED')} -> {_n('SAVINGS_VERIFIED')} / {_n('SAVINGS_REJECTED')}"},
            {"label": "Verified savings (90d)",
             "value": f"${float(a.get('VERIFIED_USD') or 0):,.0f}"},
        ])
        st.caption("Generated -> executed -> verified, from audit rows. No impression "
                   "tracking — Streamlit cannot measure 'viewed' truthfully.")


def _canary_tab() -> None:
    st.caption(
        "Runs every registered SQL builder against the live account to catch "
        "ACCOUNT_USAGE column drift or missing OVERWATCH objects before a user does. "
        "Execute mode runs each statement with a 1-row cap; compile-only wraps each "
        "in EXPLAIN — same drift coverage for column/object errors, no data scanned. "
        "Failures are logged to APP_ERROR_LOG."
    )
    panel_help(
        "Runs every registered SQL builder against the live account to catch ACCOUNT_USAGE "
        "column drift or a missing OVERWATCH object before a user does. A FAIL (not a declared "
        "GAP) means a query the app depends on no longer compiles — fix the drift; the mart "
        "reconciliation panel below flags mart totals that diverge past 5% from live and the "
        "backfill to re-run."
    )
    from app.data.canary import CANARIES, EXPECTED_GAPS

    with st.expander("Object-cost ledger reconciliation (additive contract)"):
        st.caption(
            "v4.52 (Codex #7): query arms + residual vs QUERY_ATTRIBUTION_HISTORY, "
            "and each maintenance arm vs its source history, over a lag-safe window "
            "ending 2 days ago. DELTA ~0 is the contract; drift is late-arriving "
            "attribution (self-heals on the next daily load) or a loader defect."
        )
        if st.button("Run object-ledger recon", key="adm_objcost_recon"):
            _oc_res = run(cost_sql.object_cost_recon(7), page=_PAGE, key="objcost_recon",
                          tier="live", source="FACT_OBJECT_COST_DAILY vs QAH + serverless histories")
            if guard(_oc_res, "Object-cost ledger has no rows yet (V048+ not deployed or first load pending)."):
                styled_table(_oc_res.df, column_config={
                    "DELTA_PCT": st.column_config.NumberColumn("Delta %", format="%.2f%%")})
                result_caption(_oc_res)

    st.markdown(f"**{len(CANARIES)} registered statements**")
    compile_only = st.toggle(
        "Compile-only (EXPLAIN)", key="adm_canary_explain", value=True,
        help="v4.51 (Codex #17): EXPLAIN validates identifiers and objects without "
             "executing the aggregates — seconds instead of minutes. Untoggle for "
             "the classic executed probe (also proves row-level access).")
    if st.button("Run canary now", key="adm_canary_run"):
        results = []
        progress = st.progress(0.0, text="Running canary...")
        for idx, (name, builder) in enumerate(CANARIES):
            _sql = builder()
            if compile_only:
                _sql = "EXPLAIN USING TEXT\n" + _sql
            res = run(_sql, page=_PAGE, key=f"canary_{name}", tier="live",
                      source=name, max_rows=1, probe=True)
            # r10 #4: classified from the RAW exception in run(). r11 #7: GAP
            # must be DECLARED per entry — an absent core object is drift and
            # FAILS; only account-feature absences read as a calm GAP.
            _gap = (res.error_kind in ("absent", "unknown_function")
                    and name in EXPECTED_GAPS)
            results.append({"CHECK": name,
                            "STATUS": "PASS" if res.ok else ("GAP" if _gap else "FAIL"),
                            "ROWS": len(res.df), "ERROR": res.error[:160]})
            progress.progress((idx + 1) / len(CANARIES), text=f"{name}")
        progress.empty()
        import pandas as _pd

        frame = _pd.DataFrame(results)
        failed = frame[frame["STATUS"] == "FAIL"]
        gaps = frame[frame["STATUS"] == "GAP"]
        if not gaps.empty:
            st.caption(f"{len(gaps)} GAP: declared account-feature absences (Cortex "
                       "subscription/region) — absence, not drift. Anything absent "
                       "WITHOUT a declaration fails instead.")
        if failed.empty:
            st.success(f"All {len(frame) - len(gaps)} applicable canary statements passed.")
        else:
            st.error(f"{len(failed)} of {len(frame)} canary statements failed — see errors below.")
        st.session_state["_adm_canary_results"] = frame
    stored = st.session_state.get("_adm_canary_results")
    if stored is not None:
        from app.ui.components import styled_table as _styled

        view = stored.copy()
        view["STATUS"] = view["STATUS"].map({"PASS": "SUCCESS", "FAIL": "FAILED", "GAP": "GAP"})
        view = view.rename(columns={"STATUS": "EXECUTION_STATUS"})
        _styled(view, height=420)

    st.divider()
    st.markdown("**Mart reconciliation — do the numbers MATCH the source?**")
    st.caption(
        "Freshness proves the loaders ran; this compares mart totals against live "
        "ACCOUNT_USAGE over the same complete window. ±2% is normal late-arrival noise; "
        "beyond ±5%, re-run the backfill for that window (snowflake/backfill_365.sql, scoped)."
    )
    # r21 #7: merely opening this tab paid a 28d metering + 7d history scan.
    if not st.toggle("Run reconciliation", key="adm_recon_on",
                     help="Compares 28d metering and 7d query totals, mart vs live. "
                          "Cached for an hour once run."):
        return
    recon = run(mart_sql.mart_vs_live_recon(), page=_PAGE, key="mart_recon", tier="historical",
                source="FACT_* vs METERING_DAILY_HISTORY / QUERY_HISTORY")
    if guard(recon, "Reconciliation needs the facts (V002) installed.",
             setup_hint="Runs the mart and the live aggregate side by side; deploy marts first."):
        rdf = recon.df.copy()
        rdf["STATE"] = rdf["DRIFT_PCT"].map(
            lambda d: "OK" if abs(safe_float(d)) <= 2 else ("WARN" if abs(safe_float(d)) <= 5 else "BAD"))
        styled_table(rdf, column_config={
            "DRIFT_PCT": st.column_config.NumberColumn("Drift %", format="%.2f%%")})
        worst = rdf["DRIFT_PCT"].map(lambda d: abs(safe_float(d))).max()
        if worst > 5:
            st.error("Mart drift beyond ±5%: chargeback and exec numbers are off until the "
                     "backfill re-runs. This is exactly what this panel exists to catch.")
        elif worst > 2:
            st.warning("Mart drift in the 2-5% band — usually late-arriving metering rows; "
                       "re-check tomorrow before re-running backfills.")
        result_caption(recon)

    st.divider()
    st.markdown("**Fire-drill scoreboard — does the page reach a human?**")
    from app.logic.drill import drill_report
    drills = run(mart_sql.drill_history(14), page=_PAGE, key="drill_hist", tier="recent",
                 source="ALERT_EVENTS (OPS_ALERT_DRILL)")
    if not drills.ok:
        st.info("Drill history unavailable: " + drills.error)
    else:
        report = drill_report(drills.df if not drills.empty else None)
        if not report["ran"]:
            st.info("No drills yet — enable the monthly fire drill with the opt-in "
                    "snowflake/alert_drill.sql (one synthetic CRITICAL on the 1st; "
                    "the notify chain must deliver it and on-call must ACK it).")
        else:
            last = report["last"]
            kpi_row([
                {"label": "Drill streak", "value": f"{report['streak_months']} month(s)",
                 "severity": "ok" if report["streak_months"] >= 1 else "bad",
                 "help": "Consecutive months where the drill was DELIVERED and ACKED."},
                {"label": "Last drill delivered",
                 "value": "yes" if last["delivered"] else "NO",
                 "severity": "ok" if last["delivered"] else "bad"},
                {"label": "Time to ack",
                 "value": (humanize_duration(last["mtta_min"], "min")
                           if last["mtta_min"] is not None else "not acked"),
                 "severity": "ok" if last["acked"] else "warn"},
            ])
            styled_table(drills.df, height=200)
            st.caption("Resolve drills as EXPECTED — they're excluded from rule precision.")

    st.divider()
    st.markdown("**Restated days — did a reported number move after close?**")
    rest = run(mart_sql.metering_restatements(60), page=_PAGE, key="restatements",
               tier="recent", source="FACT_METERING_DAILY LOAD_TS lag")
    if rest.ok and rest.empty:
        st.success("No metering day was restated ≥48h after close in the last 60 days — "
                   "numbers reported from this app have stayed put.")
    elif guard(rest, ""):
        styled_table(rest.df, height=220)
        st.caption(
            "These days' metering changed ≥48h after the day ended (late-arriving rows or "
            "re-runs). If finance got a figure before the restatement, this is the receipt "
            "explaining the move."
        )


def _metric_registry_tab() -> None:
    """Phase 1 (architectural): the single semantic contract for every cost
    number — method, grain, source, timezone, latency, formula version."""
    from app.logic import metric_registry as mr
    st.markdown("**Cost metric registry — what every number means**")
    st.caption(
        "Read a figure by its METHOD: BILLED uses Snowflake's billed-credit basis "
        "(organization currency is invoice truth), METERED is "
        "exact usage (idle in, CS unadjusted), MEASURED is attributed compute "
        "(idle out), ALLOCATED is a share-based estimate, ESTIMATED is "
        "bytes/credits x a configured rate. Documentation-grade today: the "
        "drift test pins the registered entries, but cannot yet discover an "
        "unregistered KPI — register new cost metrics by hand."
    )
    _order = {m: i for i, m in enumerate(mr.METHODS)}
    _rows = sorted(mr.as_rows(), key=lambda r: _order.get(r["Method"], 99))
    styled_table(pd.DataFrame(_rows), height=TABLE_H_LG, size_note=False)  # caption states the count
    st.caption(f"{len(_rows)} registered metrics · methods: {', '.join(mr.METHODS)}.")


def _setup_progress_tab() -> None:
    """rec44: ONE onboarding checklist that consolidates the scattered 'Apply VNNN'
    walls (Action Center, Entity 360, SLOs, experiments, Security queue) plus marts
    loading and budget/contract/route config — so a fresh deploy has a single place
    that says what is done and what is pending, not a dozen per-surface walls. All
    reads are existing probes; this panel applies nothing."""
    panel_help(
        "What this install still needs. Each row reads live state — migrations applied, "
        "marts loading, and the settings/routes that light up alerting and forecasting. "
        "Apply migrations in Snowsight (owner); configure settings on the Settings tab."
    )
    rows: list[dict] = []

    def _add(step: str, done: bool, detail: str, fix: str, partial: bool = False) -> None:
        status = "Done" if done else ("Partial" if partial else "Pending")
        rows.append({"STEP": step, "STATUS": status, "DETAIL": detail,
                     "FIX": "" if done else fix})

    sv = run(mart_sql.schema_version(), page=_PAGE, key="setup_schema_version",
             tier="metadata", source="SCHEMA_VERSION", probe=True)
    applied: set[int] = set()
    if sv.ok and not sv.empty:
        applied = {int(v) for v in pd.to_numeric(sv.df["VERSION"], errors="coerce").dropna()}
    missing = [n for n in _EXPECTED_MIGRATIONS if n not in applied]
    _add("Database migrations", done=bool(applied) and not missing,
         detail=(f"{len(applied)} of {len(_EXPECTED_MIGRATIONS)} applied"
                 + (f"; {len(missing)} missing" if missing else "")) if sv.ok
                else "SCHEMA_VERSION unreadable — nothing applied yet",
         fix="Run the missing migrations in order (DEPLOYMENT.md).")
    for _v, _label, _feature in (
        (74, "V074", "Operating workbench: Action Center, Entity 360, SLOs, experiments"),
        (75, "V075", "Security posture facts: the decision queue"),
        (76, "V076", "Anomaly materiality gate"),
        (77, "V077", "Cost by application x user: FACT_APP_COST_DAILY"),
        (78, "V078", "AI usage loader unbreak: ai_code TZ->NTZ casts + backfill"),
        (79, "V079", "AI predicate historical split: loader procs count CoCo as AI"),
        (80, "V080", "Security change-risk ETL exclusion: ETL-role DROP/TRUNCATE de-noised"),
        (81, "V081", "Unified experiment verify: SP_VERIFY_EXPERIMENT books ledger + closes action in one txn"),
        (82, "V082", "Query-family company regrain: MART_QUERY_FAMILY_DAILY gains COMPANY (per-company grain)"),
        (83, "V083", "Action estimate time-basis: ACTION_QUEUE gains PERIOD labeling ESTIMATED_USD (DS #7)"),
    ):
        _add(f"{_label} — {_feature}", done=_v in applied,
             detail="applied" if _v in applied else "not applied",
             fix=f"Apply {_label} in Snowsight (owner).")

    fr = run(mart_sql.source_freshness_state(), page=_PAGE, key="setup_freshness",
             tier="recent", source="SOURCE_FRESHNESS_STATE", probe=True)
    if fr.usable():
        # NEVER loaded (LAST_LOAD_TS IS NULL), not merely empty — a healthy mart can
        # have ROW_COUNT=0 (e.g. no incidents yet) and must still count as loaded.
        _never = (int(fr.df["LAST_LOAD_TS"].isna().sum())
                  if "LAST_LOAD_TS" in fr.df.columns else 0)
        _add("Marts loading", done=_never == 0, partial=_never > 0,
             detail=f"{len(fr.df)} sources tracked"
                    + (f"; {_never} not loaded yet" if _never else ""),
             fix="Loader tasks fill these overnight; a never-loaded source needs its "
                 "backfill (Migrations & freshness tab).")
    else:
        _add("Marts loading", done=False,
             detail="Freshness view not readable — have the loader tasks run?",
             fix="Resume the loader tasks (end of V004) / run the backfill.")

    s = load_settings(_PAGE)
    _budget = str(s.get("MONTHLY_BUDGET_USD") or "").strip()
    _add("Monthly budget set", done=bool(_budget) and _budget not in ("0", "0.0"),
         detail=f"MONTHLY_BUDGET_USD = {_budget or '(unset)'}",
         fix="Set MONTHLY_BUDGET_USD on the Settings tab (drives budget pacing + score).")
    _contract = str(s.get("CONTRACT_START_DATE") or "").strip()
    _credits = str(s.get("CONTRACT_CREDITS") or "").strip()
    _add("Contract configured",
         done=bool(_contract) and bool(_credits) and _credits not in ("0", "0.0"),
         detail=f"start {_contract or '(unset)'}, credits {_credits or '(unset)'}",
         fix="Set CONTRACT_START_DATE and CONTRACT_CREDITS on the Settings tab "
             "(drives Contract & Forecast).")
    rt = run(mart_sql.alert_routes(), page=_PAGE, key="setup_routes", tier="recent",
             source="ALERT_ROUTES", probe=True)
    _n_routes = 0
    if rt.usable():
        _n_routes = (int(rt.df["ENABLED"].astype(str).str.upper().isin(("TRUE", "1")).sum())
                     if "ENABLED" in rt.df.columns else len(rt.df))
    _add("Alert routes configured", done=_n_routes > 0,
         detail=f"{_n_routes} enabled route(s)" if rt.usable() else "routes table not readable",
         fix="Add an enabled route so alerts reach Teams/email (Alerts → Rules & routes).")

    df = pd.DataFrame(rows)
    pending = int((df["STATUS"] != "Done").sum())
    if pending == 0:
        st.success("Setup complete — every checklist item is satisfied.")
    else:
        st.warning(f"{pending} setup item(s) still pending — see the FIX column.")
    styled_table(df, height=360)


@safe_page(_PAGE)
def render() -> None:
    f = filters()
    page_header("Admin", "Settings, migrations, metrics, self-cost, performance, canary, and telemetry.", icon_name="admin")
    # #3: operator gating resolves the VIEWER identity against the allowlist, not
    # CURRENT_ROLE() (which is the app owner's role for every viewer under owner's-rights
    # SiS). Falls back to the role->profile check off-SiS. `profile` still drives page nav.
    is_operator = _is_operator()
    _context_section()
    section = lazy_sections(
        ["Settings", "Migrations & freshness", "Setup progress", "Metrics", "App self-cost",
         "Performance", "Canary", "Errors & telemetry"], key="adm_section")
    section_filter_contract(
        f,
        applies=(),
        note=f"{section} is account-wide administration; analytical scope filters do not apply.",
    )
    if section == "Settings":
        _settings_tab(is_operator)
    elif section == "Migrations & freshness":
        _migrations_tab()
    elif section == "Setup progress":
        _setup_progress_tab()
    elif section == "Metrics":
        _metric_registry_tab()
    elif section == "App self-cost":
        _self_cost_tab()
    elif section == "Performance":
        _performance_tab()
    elif section == "Canary":
        _canary_tab()
    else:
        _observability_tab()
