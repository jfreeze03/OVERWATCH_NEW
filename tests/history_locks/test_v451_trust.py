"""v4.51 Tranche A locks — outcome and audit trust (Codex adjudication 2026-07-27).

Pins the round: audit actors are the viewer (not the app owner), savings
captions promise only what actually settles, ETL unit costs aggregate at run
grain with written-rows denominators, batch telemetry reports measured time,
and the perf lint gains a builder-level companion that sees THROUGH page files
to the SQL their builders actually render.
"""

from __future__ import annotations

import importlib
import inspect
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tests"))
from test_p4_filter_matrix import _REQUIRED_ARGS  # noqa: E402 — shared arg table


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Builder-level reachable-scan gate (Codex P2): the literal-count budget in
#    test_perf_budgets is a lint proxy that cannot see builder-mediated scans —
#    unit_costs counted 0 while its builders reach 7 ACCOUNT_USAGE tables.
#    This gate renders every builder a page references (introspected defaults)
#    and pins the DISTINCT ACCOUNT_USAGE tables actually reachable from it.
# ---------------------------------------------------------------------------

_BUILDER_RE = re.compile(r"\b(\w+_sql)\.(\w+)\(")
_AU_RE = re.compile(r"SNOWFLAKE\.ACCOUNT_USAGE\.(\w+)")

_REACHABLE = {
    "app/ui/pages/brief.py": (),
    "app/ui/pages/overview.py": ("WAREHOUSE_METERING_HISTORY",),
    "app/ui/pages/control_room.py": (
        # + GRANTS_TO_ROLES: the incident auto-investigation's grant-change signal
        # (recent_grant_changes unions GRANTS_TO_USERS + GRANTS_TO_ROLES), reached only
        # when a DBA selects an incident — the same drill-scoped scan class as day-replay.
        # + LOCK_WAIT_HISTORY (R1-293, already reached, now seen): selecting a lock-wait spike row reads
        # that object's last-2-day events (ops_sql.lock_wait_object_detail) -- row-select gated.
        "GRANTS_TO_ROLES", "GRANTS_TO_USERS", "LOCK_WAIT_HISTORY", "QUERY_HISTORY", "TASK_HISTORY",
        "WAREHOUSE_METERING_HISTORY"),
    # v4.545: the Spend batch co-schedules the native-apps rollup (compute_pool_usage)
    # so the summary line + the Compute-pools detail share one SPCS read.
    "app/ui/pages/cost.py": ("QUERY_HISTORY", "SNOWPARK_CONTAINER_SERVICES_HISTORY"),
    "app/ui/pages/cost_parts/spend.py": (
        # V077 cost-by-application panel adds QUERY_ATTRIBUTION_HISTORY + SESSIONS
        # (the live 3-way join fallback behind the toggle).
        # rec#11: + DATA_TRANSFER_HISTORY — the egress $ panel (transfer_egress_priced),
        # click-gated in the detailed-service-attribution section.
        # v4.237.0 storage table drill (owner ask): + TABLE_STORAGE_METRICS +
        # TABLE_DML_HISTORY + TABLES (table_storage_breakdown) — the per-table
        # active/time-travel/fail-safe breakdown under the Storage detail toggle,
        # the same tables storage_waste reaches on Optimization.
        "DATABASE_REPLICATION_USAGE_HISTORY", "DATABASE_STORAGE_USAGE_HISTORY",
        "DATA_TRANSFER_HISTORY", "METERING_DAILY_HISTORY",
        "NOTEBOOKS_CONTAINER_RUNTIME_HISTORY", "QUERY_ATTRIBUTION_HISTORY",
        "QUERY_HISTORY", "SESSIONS", "SNOWPARK_CONTAINER_SERVICES_HISTORY",
        "STORAGE_USAGE", "TABLES", "TABLE_DML_HISTORY", "TABLE_STORAGE_METRICS",
        "WAREHOUSE_METERING_HISTORY"),
    # + METERING_DAILY_HISTORY (R1-293, already reached, now seen): the contract-consumed live leg of
    # run_mart_first (cost_sql.contract_consumed_credits) -- read only when FACT_METERING_DAILY does not
    # reach the contract start (coverage fallback).
    "app/ui/pages/cost_parts/contract.py": ("METERING_DAILY_HISTORY", "QUERY_HISTORY",
                                            "WAREHOUSE_METERING_HISTORY"),
    "app/ui/pages/cost_parts/ai_chargeback.py": (
        "CORTEX_AI_FUNCTIONS_USAGE_HISTORY", "CORTEX_CODE_CLI_USAGE_HISTORY",
        "CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY", "METERING_DAILY_HISTORY",
        # v4.543: per-user AI-quota block history — the one account-wide read for
        # native per-user AI quotas (probe- + toggle-gated in _ai_quota_panel).
        "QUERY_HISTORY", "QUOTA_ACCESS_BLOCK_HISTORY", "USERS"),
    "app/ui/pages/cost_parts/unit_costs.py": (
        # v4.556: cortex_model_costs repointed onto the canonical CORTEX_AI_FUNCTIONS_USAGE_HISTORY
        # (off CORTEX_AISQL_USAGE_HISTORY). Not a drop-in: CREDITS (not TOKEN_CREDITS) + tokens
        # summed from the METRICS array via LATERAL FLATTEN (unit='tokens').
        "CORTEX_AI_FUNCTIONS_USAGE_HISTORY", "CORTEX_CODE_CLI_USAGE_HISTORY",
        "CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY", "QUERY_ATTRIBUTION_HISTORY",
        "QUERY_HISTORY", "SERVERLESS_TASK_HISTORY", "TASK_HISTORY"),
    # +QUERY_HISTORY/QUERY_ATTRIBUTION_HISTORY v4.269 (UX sweep #6): a warehouse row-click
    # scopes the pattern-movers table via a live per-warehouse read (the pattern mart has no
    # warehouse grain) — interaction-gated on the click, not first paint.
    "app/ui/pages/cost_parts/compare.py": ("QUERY_ATTRIBUTION_HISTORY", "QUERY_HISTORY"),
    # +TABLES (2026-07-31, audit B4): the storage retention-fix estimate counted the whole
    # time-travel + failsafe pile as recoverable, which is systematically high — failsafe
    # drains in 7d regardless of RETENTION_TIME. ACCOUNT_USAGE.TABLES carries the per-table
    # retention the estimate has to scale by, so this is a DELIBERATE scan-surface change
    # buying a correct dollar figure (metadata view; no history scan).
    "app/ui/pages/cost_parts/optimize.py": (
        # rec#41: + QUERY_ATTRIBUTION_HISTORY — the measured-cost lens (measured_query_costs)
        # now offered on the Optimize expensive-queries panel, not just the Unit-costs tab.
        # rec#6: + QUERY_ACCELERATION_ELIGIBLE + QUERY_ACCELERATION_HISTORY — the QAS ROI board
        # (cost_sql.qas_roi) pairs QAS spend with the eligible-workload benefit signal.
        # v4.546: + TABLE_PRUNING_HISTORY — per-table at-rest pruning candidates
        # (clustering targets), in the toggle-gated query-efficiency scan.
        # Next-Fifty #30: + GRANTS_TO_ROLES — the unread-maintenance share guard (database grants to
        # shares; a metadata view, read only behind the Storage & waste toggle).
        # Next-Fifty #38 (v4.604): + warehouse_cluster_use reads QUERY_HISTORY (already pinned) — no set change.
        "ACCESS_HISTORY", "AUTOMATIC_CLUSTERING_HISTORY", "DATABASE_STORAGE_USAGE_HISTORY",
        "GRANTS_TO_ROLES", "QUERY_ACCELERATION_ELIGIBLE", "QUERY_ACCELERATION_HISTORY", "QUERY_ATTRIBUTION_HISTORY",
        "QUERY_HISTORY", "TABLES", "TABLE_DML_HISTORY", "TABLE_PRUNING_HISTORY",
        "TABLE_STORAGE_METRICS", "WAREHOUSE_METERING_HISTORY"),
    "app/ui/pages/operations.py": (
        # rec#17: + WAREHOUSE_METERING_HISTORY — the wasted-spend board
        # (wasted_query_spend_usd) allocates warehouse-hour credits to non-success
        # queries, click-gated in the Queries tab.
        # v4.247 (repo wave-2): + QUERY_INSIGHTS — Snowflake-authored suggestion
        # feed on the Queries tab (probe-gated optional view, historical-cached).
        # v4.580 (CS driver intelligence, Phase 1b): + SESSIONS — the cloud-services
        # chatter-by-application panel joins QUERY_HISTORY to SESSIONS on SESSION_ID
        # (chatter_sql.chatter_by_application / chatter_families_for_application) to
        # attribute metadata chatter to the client app/driver; toggle-gated, off first paint.
        # + QUERY_ATTRIBUTION_HISTORY (R1-293, already reached, now seen): the ETL run-cost attribution
        # (etl_control_sql.run_cost_attribution_scan) behind the 'Compute attributed cost' toggle.
        "COPY_HISTORY", "DYNAMIC_TABLE_REFRESH_HISTORY", "LOCK_WAIT_HISTORY",
        "QUERY_ATTRIBUTION_HISTORY",
        "QUERY_HISTORY", "QUERY_INSIGHTS", "SESSIONS", "TABLE_DML_HISTORY", "TASKS",
        "TASK_HISTORY", "TASK_VERSIONS", "WAREHOUSE_LOAD_HISTORY", "WAREHOUSE_METERING_HISTORY"),
    "app/ui/pages/security.py": (
        # v4.187: + ACCESS_HISTORY & TABLE_STORAGE_METRICS for the least-privilege
        # tab (rec#24) — held table grants vs. objects queries actually touched.
        # v4.246 (owner ask, CoCo ~13% of spend): + the AI-guardrails section —
        # CORTEX_CODE_* (shared cache with Cost's chargeback scan) and the
        # probe-gated optional CORTEX_AI_GUARDRAILS_USAGE_HISTORY.
        # v4.300 (Upgrade Board P1 #20): + TABLES & TAG_REFERENCES for the
        # object-tag governance coverage panel (probe-gated; TABLES is the verified
        # inventory denominator, TAG_REFERENCES the unverified tag-assignment side).
        # v4.589 (Next-Fifty #9): + POLICY_REFERENCES for the admin network-policy coverage panel
        # (toggle- and probe-gated, off first paint).
        # Next-Fifty #43 (v4.604): + data_policy_coverage / masking_environment_parity on Exposure -- same table,
        # set unchanged.
        "ACCESS_HISTORY", "CORTEX_AI_GUARDRAILS_USAGE_HISTORY",
        "CORTEX_CODE_CLI_USAGE_HISTORY", "CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY",
        "CREDENTIALS", "DATA_TRANSFER_HISTORY", "GRANTS_TO_ROLES",
        "GRANTS_TO_USERS", "LOGIN_HISTORY", "POLICY_REFERENCES", "QUERY_HISTORY", "ROLES", "SESSIONS",
        "TABLES", "TABLE_STORAGE_METRICS", "TAG_REFERENCES", "USERS"),
    # v4.603 (Next-Fifty #33 D1): + QUERY_HISTORY -- the drawer's 'Statement timeout 1h' lever reads ONE
    # warehouse's 30-day completed-statement impact (ops_sql.warehouse_timeout_impact: a single-row aggregate
    # filtered to that warehouse, probe + historical tier) before it offers the ALTER. Interaction-gated: it
    # runs only when an operator opens an alert's Respond expander, picks that lever, and the SET would
    # tighten -- never first paint. The page file still carries no new ACCOUNT_USAGE literal.
    # + METERING_DAILY_HISTORY (R1-293, already reached, now seen): the button-gated AI evidence pack
    # (alert_evidence_sql.build) reads it for the cortex / metering_service plans. That builder also
    # already reached QUERY_HISTORY before the v4.603 note above (the gate skipped it, so it was unseen).
    "app/ui/pages/alerts.py": ("METERING_DAILY_HISTORY", "QUERY_HISTORY"),
    # v4.597 (Option C): Operations > Optimize. The queue/tracked/watchlist reads are mart and
    # app tables; QUERY_HISTORY is the opt-in live-profile toggle (query_opportunity_fingerprints,
    # byte-identical to the Queries board call, so the two share one cache entry) — off first paint.
    "app/ui/pages/ops_parts/optimize_queue.py": ("QUERY_HISTORY",),
    # v4.597 (Option C): the Proof page. The shell reaches nothing; the body's ONLY reach is
    # ACCESS_HISTORY from the HIDDEN _products section (product_consumer_reads), which nothing
    # dispatches any more — the live Proof / Pipeline sections are app tables + marts
    # (test_proof_page pins that the body minus _products reaches no ACCOUNT_USAGE table).
    "app/ui/pages/decision_studio.py": (),
    "app/ui/decision_studio.py": ("ACCESS_HISTORY",),
    # v4.52: + the object-ledger recon builder (Codex #7) — QAH and the five
    # maintenance-arm source histories, click-gated on the Canary tab.
    # v4.589 (Next-Fifty #25): mart-vs-live recon gains the warehouse + AI arms (toggle-gated Canary tab).
    "app/ui/pages/admin.py": (
        "AUTOMATIC_CLUSTERING_HISTORY", "CORTEX_AI_FUNCTIONS_USAGE_HISTORY",
        "CORTEX_CODE_CLI_USAGE_HISTORY", "CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY",
        "MATERIALIZED_VIEW_REFRESH_HISTORY", "METERING_DAILY_HISTORY", "PIPE_USAGE_HISTORY",
        "QUERY_ATTRIBUTION_HISTORY", "QUERY_HISTORY", "SEARCH_OPTIMIZATION_HISTORY",
        "SERVERLESS_TASK_HISTORY", "WAREHOUSE_METERING_HISTORY"),
}


_QID = "01b2c3d4-0000-4000-8000-000000000001"


def _gate_args() -> dict[str, list[dict]]:
    """Per-builder arguments for the required parameters _REQUIRED_ARGS (the injection matrix's shared
    table) does not carry. Several renders = the union of their tables (one per basis / object type /
    evidence kind), so a branch-dependent reach is seen whichever branch the page takes. R1-293: the
    gate used to SKIP every such builder with a bare `continue`, and four page pins were wrong for it."""
    from datetime import date, timedelta

    from app.data.etl_control_sql import RefGapCheck
    from app.logic.alert_evidence import _KIND_LABEL, EvidencePlan
    from app.logic.date_windows import CalendarDayOffset
    from app.logic.ledger_measure import BASES

    recent = date.today() - timedelta(days=7)
    ctl = {"control_fqn": "DB.S.CONTROL_STATUS"}
    tbl = {"database": "DB", "schema": "S", "table": "T"}
    return {
        "alert_evidence_sql.build": [
            {"plan": EvidencePlan(kind, "last 7 days", days=7, warehouse="WH_X", service="AI_SERVICES",
                                  day="2026-06-14", family_text="SELECT 1")} for kind in _KIND_LABEL],
        "change_impact_sql.object_run_history": [
            {"object_type": "PROCEDURE", "object_name": "DB.S.MY_PROC"},
            {"object_type": "TASK", "object_name": "DB.S.MY_TASK"}],
        "cost_sql.contract_consumed_credits": [{"contract_start_date": "2026-01-01"}],
        "cost_sql.unread_maintenance_proof": [
            {"fqn": "DB.S.T", "booked_on": date(2026, 6, 1), "baseline_monthly_credits": 10.0}],
        # the shared Window-label helper (v4.606 holistic review): a pure phrase, rendered so its
        # (empty) reach is checked rather than skipped
        "etl_control_sql.calendar_window_phrase": [{"days": CalendarDayOffset(9), "today": date(2026, 9, 10)}],
        "etl_control_sql.cycle_finish_history_scan": [{**ctl, "start_workflow": "WF_A", "end_workflow": "WF_Z"}],
        "etl_control_sql.recon_errors_scan": [{"recon_fqn": "DB.S.RECON"}],
        "etl_control_sql.recon_recurrence_scan": [{"recon_fqn": "DB.S.RECON"}],
        "etl_control_sql.reference_gap_scan": [
            {"checks": [RefGapCheck("pc_x.code", "DB.S.STG", "CODE")], "xlat_fqn": "DB.S.XLAT"}],
        "etl_control_sql.run_cost_attribution_scan": [ctl],
        "etl_control_sql.run_inventory_scan": [{"run_id_fqn": "DB.S.RUN_ID"}],
        "etl_control_sql.run_params_scan": [{"params_fqn": "DB.S.PARAMS"}],
        "etl_control_sql.run_task_evidence_scan": [{**ctl, "task": "TASK_A"}],
        "etl_control_sql.run_tasks_scan": [{**ctl, "run_id": "R1"}],
        "etl_control_sql.task_runtime_history_scan": [ctl],
        "etl_control_sql.task_status_history_scan": [ctl],
        "etl_control_sql.workflow_list_scan": [ctl],
        "etl_control_sql.workflow_runtime_drift_scan": [ctl],
        "etl_control_sql.workflow_runtimes_scan": [ctl],
        "insights_sql.call_children_costs": [{"call_query_id": _QID}],
        "insights_sql.call_cost_lookup": [{"ident": _QID}],
        "insights_sql.query_detail": [{"query_id": _QID}],
        "insights_sql.table_retention_live": [tbl],
        "insights_sql.table_tco": [tbl],
        "mart_sql.deliveries_for_event": [{"event_id": _QID}],
        "mart_sql.fact_contract_consumed": [{"start_iso": "2026-01-01"}],
        "mart_sql.incident_members_detail": [{"incident_id": "INC-1"}],
        "mart_sql.ledger_before_after": [
            {"basis": b, "target_object": "DB.S.T", "booked_day": date(2026, 6, 1)} for b in BASES],
        "mart_sql.ledger_for_event": [{"event_id_prefix": "ab12cd34"}],
        "mart_sql.supersede_ledger_twins_sql": [{"actor_sql": "CURRENT_USER()"}],
        "ops_sql.lock_wait_object_detail": [{"database": "DB", "schema": "S", "object_name": "T"}],
        "ops_sql.operator_anatomy": [{"query_id": _QID}],
        "security_sql.role_holders": [{"role": "SOME_ROLE"}],
        "security_sql.role_privileges": [{"role": "SOME_ROLE"}],
        "security_sql.show_grants_to_share_sql": [{"share_name": "SOME_SHARE"}],
        "workbench_sql.entity_daily_signals": [
            {"entities": [("WAREHOUSE", "WH_X", recent), ("TASK", "DB.S.T", recent),
                          ("QUERY_FINGERPRINT", "abc123", recent)]}],
        "workbench_sql.watchlist": [{"viewer_name": "VIEWER"}],
    }


# Names the pages call through a *_sql module that are NOT SQL builders, so there is nothing to render.
# Reviewed list: anything else the gate cannot render FAILS it -- a builder that grows a new required
# argument must be taught in _gate_args, never skipped.
_UNRENDERABLE = {
    "etl_control_sql.filter_checks_by_database": "filters parsed RefGapChecks; renders no SQL",
    "etl_control_sql.parse_ref_gap_checks": "parses the ETL_REF_GAP_CHECKS setting; renders no SQL",
    "ops_sql.split_health_bundle": "splits an already-fetched DataFrame; renders no SQL",
    "recheck_sql.recheck_closed_day": "pure helper: whether an event's title day is closed; renders no SQL",
}


def _render_sqls(mod_name: str, fn) -> list[str]:
    """Every SQL the builder renders under the gate's arguments. KeyError = a required argument the
    gate was never taught; TypeError = it returned no SQL."""
    sqls = []
    for override in _gate_args().get(f"{mod_name}.{fn.__name__}", [{}]):
        kwargs = {}
        for pname, param in inspect.signature(fn).parameters.items():
            if pname == "company":
                kwargs[pname] = "ALFA"
            elif pname in override:
                kwargs[pname] = override[pname]
            elif param.default is not inspect.Parameter.empty:
                continue
            else:
                kwargs[pname] = _REQUIRED_ARGS[pname]      # KeyError -> unrenderable
        out = fn(**kwargs)
        if isinstance(out, tuple) and out and isinstance(out[0], str):
            out = out[0]                                  # the (sql, errors) builders
        if not isinstance(out, str):
            raise TypeError(fn.__name__)
        sqls.append(out)
    return sqls


def _reachable_tables(src: str) -> tuple[set, dict]:
    """(ACCOUNT_USAGE tables the source's builders render, {module.fn: why it could not be rendered})."""
    tables: set = set()
    skipped: dict = {}
    for mod_name, fn_name in sorted(set(_BUILDER_RE.findall(src))):
        try:
            mod = importlib.import_module(f"app.data.{mod_name}")
        except ModuleNotFoundError:
            continue
        fn = getattr(mod, fn_name, None)
        if fn is None or not inspect.isfunction(fn):
            continue
        try:
            for sql in _render_sqls(mod_name, fn):
                tables |= set(_AU_RE.findall(sql))
        except (KeyError, TypeError, ValueError) as exc:
            skipped[f"{mod_name}.{fn_name}"] = f"{type(exc).__name__}: {exc}"
    return tables, skipped


def test_reachable_account_usage_tables_per_page():
    """A page's true scan surface is what its BUILDERS render, not what its
    source spells. New reachable tables must be pinned here deliberately —
    growing this set is the honest version of raising a literal budget."""
    all_skipped: dict = {}
    for rel, expected in _REACHABLE.items():
        tables, skipped = _reachable_tables(_read(rel))
        all_skipped.update(skipped)
        assert tuple(sorted(tables)) == expected, (
            f"{rel}: reachable ACCOUNT_USAGE set changed — "
            f"got {sorted(tables)}, pinned {list(expected)}. "
            "Update the pin ONLY with a deliberate scan-surface change.")
    # R1-293: an unrenderable builder used to be dropped silently, so its scans never reached the pin.
    unexpected = {k: v for k, v in all_skipped.items() if k not in _UNRENDERABLE}
    assert not unexpected, f"teach _gate_args these builders' required arguments: {unexpected}"
    assert set(_UNRENDERABLE) <= set(all_skipped), (
        "stale _UNRENDERABLE entries (now rendered or no longer referenced): "
        f"{sorted(set(_UNRENDERABLE) - set(all_skipped))}")


# ---------------------------------------------------------------------------
# 2. Audit actors are the viewer (Codex P1-B)
# ---------------------------------------------------------------------------

def test_alert_audit_inserts_stamp_the_viewer():
    src = _read("app/ui/pages/alerts.py")
    inserts = re.findall(r"INSERT INTO \{core_object\('ALERT_AUDIT'\)\} \(([^)]+)\)", src)
    assert len(inserts) == 5   # ack/resolve single + bulk + V086 snooze + un-snooze + clear-queue
    for cols in inserts:
        assert "ACTED_BY" in cols, "ALERT_AUDIT insert lets CURRENT_USER() default record the owner"
    assert "the table has no" not in src                 # the false r27 comment stays dead


def test_remediation_log_inserts_stamp_the_viewer():
    expected = {"app/ui/pages/alerts.py": 1,
                # 3 -> 2 in the 2026-09-30 hygiene release review: the storage-waste retention control is
                # review only (the executor's allow-list refuses ALTER TABLE, so its audit row was always FAILED)
                "app/ui/pages/cost_parts/optimize.py": 2,
                "app/ui/pages/operations.py": 2}
    for rel, n in expected.items():
        src = _read(rel)
        total = src.count("INSERT INTO {core_object('REMEDIATION_LOG')}")
        stamped = src.count("RESULT_NOTE, EXECUTED_BY)")
        assert total == n, f"{rel}: REMEDIATION_LOG insert count moved ({total})"
        assert stamped == n, f"{rel}: {n - stamped} insert(s) still default EXECUTED_BY to the owner"


# ---------------------------------------------------------------------------
# 3. Savings captions promise only what settles (Codex P1-A)
# ---------------------------------------------------------------------------

def test_no_screen_promises_the_phantom_verifier():
    """The monthly verifier proposes only, and only for descriptions no
    executed path books; V038 settles only its own rows. Captions must not
    promise automatic settlement for app-booked entries."""
    for rel in ("app/ui/pages/cost_parts/optimize.py", "app/ui/pages/alerts.py"):
        src = _read(rel)
        for phrase in ("monthly verifier proves or rejects",
                       "verifier will test actuals",
                       "verifier tests actuals",
                       "monthly verifier compares",
                       "the monthly verifier later proves"):
            assert phrase not in src, f"{rel}: still promises the phantom verifier ({phrase!r})"


# ---------------------------------------------------------------------------
# 4. Telemetry reports measured time and carries the Snowflake query id
# ---------------------------------------------------------------------------

def test_batch_telemetry_is_honest_and_query_id_flows():
    q = _read("app/core/query.py")
    assert "elapsed / max(len(bspecs)" not in q, (
        "per-member batch time must be the measured batch wall (BATCH_SIZE labels "
        "the sharing), not an invented wall/size division")
    assert "query_id: str | None = None" in q            # telemetry accepts it
    assert "_LAST_QUERY_ID" in q                         # captured from the async job handle


# ---------------------------------------------------------------------------
# 5. ETL unit costs: run grain, written rows, honest run_id coverage (Codex #8/#10)
# ---------------------------------------------------------------------------

def test_etl_formulas_are_run_grain_with_written_rows():
    from app.data import etl_sql
    sql = etl_sql.etl_cost_by_pipeline(30, "ALFA", "ALFA_EDW_PRD", "STG")
    assert "GROUP BY t.PIPELINE, t.RUN_ID" in sql        # runs before pipelines
    assert "ROWS_WRITTEN" in sql and "'CREATE_TABLE_AS_SELECT'" in sql
    assert "RUN_ID_CREDIT_PCT" in sql                    # partial tagging degrades honestly
    assert "COUNT_IF(RUN_ID IS NOT NULL)" in sql
    # scope threading reaches the SQL (Codex #10: the panel ignored database/schema)
    assert "ALFA_EDW_PRD" in sql and "STG" in sql
    uc = _read("app/ui/pages/cost_parts/unit_costs.py")
    # both reads honor the same window (incl. Last-month bounds) and batch in one round-trip
    assert 'etl_sql.etl_cost_by_pipeline(days, company, f["database"], f["schema_contains"], bounds=bounds)' in uc
    assert 'etl_sql.etl_tag_coverage(days, company, f["database"], f["schema_contains"], bounds=bounds)' in uc


# ---------------------------------------------------------------------------
# 6. Docs carry the real migration floor (Codex #20)
# ---------------------------------------------------------------------------

def test_deploy_docs_track_the_migration_floor():
    latest = sorted((_ROOT / "snowflake" / "migrations").glob("V[0-9]*.sql"))[-1].name
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert latest in _read(rel), (
            f"{rel}: run-list trails the repo — add {latest} (the r27 #9 rewrite "
            "fixed narrative but the list drifted for 15 migrations)")
    assert "OVERWATCH_MONITOR" not in _read("README.md")  # retired-role reference stays dead


def test_validate_sql_floor_tracks_the_latest_migration():
    """validate.sql's 'V001..V0NN applied' floor and its count check must equal the
    latest migration number — generic so a new migration that forgets to bump it
    fails CI, without pinning the moving floor inside any per-migration test."""
    migs = sorted((_ROOT / "snowflake" / "migrations").glob("V[0-9]*.sql"))
    n = max(int(m.name[1:4]) for m in migs)
    v = _read("snowflake/validate.sql")
    assert f"V001..V{n:03d} applied" in v, f"validate.sql floor label != V{n:03d}"
    assert f"BETWEEN 1 AND {n}) = {n}" in v, f"validate.sql count check != {n}"


# ---------------------------------------------------------------------------
# 7. Canary compile-only mode + registry caption honesty
# ---------------------------------------------------------------------------

def test_canary_has_compile_only_mode():
    adm = _read("app/ui/pages/admin.py")
    assert "EXPLAIN USING TEXT" in adm and "adm_canary_explain" in adm


def test_registry_caption_stops_overclaiming_discovery():
    adm = _read("app/ui/pages/admin.py")
    assert "register new cost metrics by hand" in adm
    assert "registering it here fails the drift-guard test" not in adm
