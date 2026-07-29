"""Admin — settings, migrations & freshness, metric registry, app self-cost,
performance, canary, and errors & telemetry.

Everything that was wrongly parked on the old app's executive page lives
here, where the people who can act on it will look for it.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.config import (
    APP_VERSION,
    DEFAULT_SETTINGS,
    OPERATOR_PROFILES,
    core_object,
    resolve_role_profile,
)
from app.core.errors import error_buffer, safe_page
from app.core.identity import identity_sql
from app.core.query import bump_refresh_salt, execute_statement, query_telemetry, run
from app.core.session import current_role
from app.core.sqlsafe import sql_literal
from app.data import cost_sql, mart_sql
from app.logic.formulas import safe_float
from app.ui.components import (
    guard,
    kpi_row,
    lazy_sections,
    load_settings,
    notify,
    page_header,
    result_caption,
    run_mart_first,
    selectable_table,
    styled_table,
)

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


def _settings_tab(is_operator: bool) -> None:
    settings = load_settings(_PAGE)
    st.caption(f"Values from: {settings.get('_source')}. Rates confirmed 2026-07: $3.68 compute / $2.20 Cortex.")
    res = run(mart_sql.settings(), page=_PAGE, key="settings_table", tier="live",
              source="SETTINGS")
    if guard(res, "SETTINGS is empty.", setup_hint="Run migration V001 to create and seed it."):
        styled_table(res.df)
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
    editable = [k for k in DEFAULT_SETTINGS if not k.startswith("_")]
    key = st.selectbox("Setting", editable, key="adm_setting_key")
    new_value = st.text_input("New value", key="adm_setting_value",
                              help="Numeric settings take numbers; dates are YYYY-MM-DD; blank clears.")
    update_sql = (
        f"UPDATE {core_object('SETTINGS')} SET VALUE = {sql_literal(new_value)}, "
        f"UPDATED_AT = CURRENT_TIMESTAMP(), UPDATED_BY = {identity_sql()} "
        f"WHERE KEY = {sql_literal(key)};"
    )
    st.code(update_sql, language="sql")
    if is_operator:
        confirm = st.text_input("Type the setting key to confirm", key="adm_setting_confirm")
        if st.button("Execute update", key="adm_setting_exec", disabled=(confirm != key)):
            ok, msg = execute_statement(update_sql, page=_PAGE)
            notify(ok, msg)
            if ok:
                st.caption("New value takes effect within one cache cycle (≤5 min) or after Refresh.")
    else:
        st.caption("Executing requires SNOW_ACCOUNTADMINS / SNOW_SYSADMINS; anyone can copy the SQL for review.")


def _migrations_tab() -> None:
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
        st.warning("Missing migrations: " + ", ".join(missing) + ". Run them in order (DEPLOYMENT.md).")
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
        with st.expander("Why stale? — diagnose without reading raw errors"):
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
                st.markdown(f"- **{name}** — {float(s['HOURS_SINCE_LOAD']):.0f}h since load. "
                            + (hint or "no matching error logged — check SHOW TASKS "
                                       "(tasks suspend if a migration half-applied)."))


_SCAN_NOTE = ("First load scans ACCOUNT_USAGE directly (a few seconds on a cold "
              "cache); results cache for an hour, so repeat views are instant.")


def _self_cost_tab() -> None:
    st.caption(
        "The monitoring app must never become the cost problem: WH_ALFA_OVERWATCH is XSMALL with a "
        "60-second auto-suspend, and every app query carries an OVERWATCH query tag (no resource monitor since v4.45 — OVERWATCH_RM was suspending the warehouse mid-use)."
    )
    st.caption(_SCAN_NOTE)
    res = run(mart_sql.app_self_cost(14), page=_PAGE, key="self_cost", tier="historical",
              source="ACCOUNT_USAGE.QUERY_HISTORY (OVERWATCH tag or WH_ALFA_OVERWATCH)")
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
        ("ACCOUNT_USAGE core", "SELECT 1 FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY LIMIT 1",
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
        styled_table(telemetry.sort_values("at", ascending=False))

    if st.button("Refresh all cached data", key="adm_refresh"):
        bump_refresh_salt()
        st.rerun()


def _performance_tab() -> None:
    """Prove (or disprove) that the app is fast: its own statement stats."""
    st.caption(
        "Every statement family the app has run on WH_ALFA_OVERWATCH, grouped by "
        "parameterized hash — the slowest rows are the builders worth optimizing next."
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
    st.caption(_SCAN_NOTE)
    res = run(mart_sql.app_statement_stats(7), page=_PAGE, key="app_stmt_stats",
              tier="historical", source="ACCOUNT_USAGE.QUERY_HISTORY (WH_ALFA_OVERWATCH)")
    if guard(res, "No statements on the app warehouse in the last 7 days.",
             setup_hint="Stats appear once the app and its tasks have run against WH_ALFA_OVERWATCH."):
        st.dataframe(res.df, hide_index=True, use_container_width=True,
                     column_config={
                         "MEDIAN_S": st.column_config.NumberColumn("Median s", format="%.2f"),
                         "P95_S": st.column_config.NumberColumn("p95 s", format="%.2f"),
                         "AVG_GB_SCANNED": st.column_config.NumberColumn("Avg GB scanned", format="%.3f"),
                     })
        result_caption(res)
        st.caption("Includes the loader/scan tasks — they share the warehouse by design.")

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
        styled_table(fq.df, height=280)
        st.caption(
            "Only fetches ≥2s or failed are persisted, plus a ~2% healthy sample "
            "(fire-and-forget, 60/session cap) — an EXCEPTION-WEIGHTED sample, so "
            "p50/p95 here read HIGHER than true fleet latency (r22 #20; weighted "
            "stats are queued). This is the regression surface across every user, "
            "not a complete census. Per-session telemetry lives on Errors & telemetry."
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
        # "likely fix" text): pain = p95 x slow-count, from the same frame.
        _tt = tbp.df.copy()
        try:
            _tt["PAIN"] = (_tt["P95_S"].astype(float) * _tt["SLOW_2S"].astype(float)).round(1)
            _tt = _tt.sort_values("PAIN", ascending=False).head(5)
            st.markdown("**Next tuning targets** — pain = p95 x slow fetches; "
                        "the telemetry picks, not opinions.")
            _sel = selectable_table(
                _tt[["PAGE", "P95_S", "SLOW_2S", "FAILED", "PAIN"]],  # r24: CACHE_HIT_PCT off — 0.0 by construction until weighted telemetry (review #3/#4)
                key="adm_tt_sel", height=160)
            # Codex r8 #1: click a target, see the slow keys behind the pain
            if _sel is not None:
                # selectable_table returns a POSITIONAL index into the frame
                # it displayed. v4.23.0 subscripted the int like a row, hit
                # TypeError, and the except below silently ate every click
                # (Joe 2026-07-11: "the screen flashes and does nothing").
                _pg = str(_tt.iloc[int(_sel)]["PAGE"])
                _det = None if fq_df is None else fq_df[fq_df["PAGE"].astype(str) == _pg]
                if _det is None or _det.empty:
                    st.caption(f"{_pg}: nothing slow or failed persisted for this page "
                               "in 7d — its pain is spread across sub-2s fetches.")
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
                 "value": f"{last['mtta_min']:.0f} min" if last["mtta_min"] is not None else "not acked",
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
        "Read a figure by its METHOD: BILLED ties to the invoice, METERED is "
        "exact usage (idle in, CS unadjusted), MEASURED is attributed compute "
        "(idle out), ALLOCATED is a share-based estimate, ESTIMATED is "
        "bytes/credits x a configured rate. Documentation-grade today: the "
        "drift test pins the registered entries, but cannot yet discover an "
        "unregistered KPI — register new cost metrics by hand."
    )
    _order = {m: i for i, m in enumerate(mr.METHODS)}
    _rows = sorted(mr.as_rows(), key=lambda r: _order.get(r["Method"], 99))
    styled_table(pd.DataFrame(_rows), height=460)
    st.caption(f"{len(_rows)} registered metrics · methods: {', '.join(mr.METHODS)}.")


@safe_page(_PAGE)
def render() -> None:
    page_header("Admin", "Settings, migrations, metrics, self-cost, performance, canary, and telemetry.", icon_name="admin")
    profile = resolve_role_profile(current_role())
    is_operator = profile in OPERATOR_PROFILES
    _context_section()
    section = lazy_sections(
        ["Settings", "Migrations & freshness", "Metrics", "App self-cost",
         "Performance", "Canary", "Errors & telemetry"], key="adm_section")
    if section == "Settings":
        _settings_tab(is_operator)
    elif section == "Migrations & freshness":
        _migrations_tab()
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
