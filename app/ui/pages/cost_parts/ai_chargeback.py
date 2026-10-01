"""Cost Intelligence — the Chargeback & AI section bodies (department chargeback,
Cortex & storage, AI user attribution).

Formula honesty rules: billed dollars always include the cloud-services
adjustment; warehouse spend is exact; user/database spend is share-allocated
and says so; estimated and verified savings never mix.
"""

from __future__ import annotations

from dataclasses import replace

import pandas as pd
import streamlit as st

from app import companies
from app.config import MAX_LIVE_WINDOW_DAYS, core_object
from app.core.identity import identity_sql
from app.core.query import execute_statement, run, run_batch_mixed
from app.core.result import QueryResult, is_setup_absence
from app.core.sqlsafe import sql_literal, sql_number
from app.data import chargeback_sql, cortex_sql, cost_sql, mart27_sql, mart_sql
from app.logic import showback
from app.logic.cortex import (
    BUDGET_LADDER,
    CPR_MIN_PROJECTED_USD,
    CPR_MIN_REQUESTS,
    CPR_SPIKE_Z,
    classify_exceptions,
    daily_from_user_daily,
    effective_window_days,
    enrich_user_rollup,
    rollup_from_user_daily,
    rollup_summary,
    token_types_window,
    with_aggregate_budget_row,
)
from app.logic.date_windows import window_label, window_phrase
from app.logic.fix_queue import (
    AI_SCOPE_ENTITY_TYPE,
    AI_TRACK_CAP,
    AI_TRACK_SEVERITIES,
    AI_TRACK_SOURCE,
    AI_USER_ENTITY_TYPE,
    ai_exception_track_items,
    ai_track_escalation_sql,
    track_entities_sql,
)
from app.logic.formulas import account_now, account_today, credits_to_usd, format_usd, md_dollars, safe_float
from app.logic.quotas import (
    DEFAULT_CAP_CREDITS,
    QUOTA_LOOKBACK_DAYS,
    QUOTA_MIN_ACTIVE_DAYS,
    RUNAWAY_CAP_MULTIPLE,
    RUNAWAY_ROBUST_Z,
    block_events,
    block_history,
    effective_cap,
    effective_z_min,
    in_window_rows,
    quota_summary,
    recommend_quotas,
)
from app.ui import charts
from app.ui.components import (
    empty_state,
    export_button,
    guard,
    kpi_row,
    methodology_note,
    notify,
    panel_help,
    reconciliation_footer,
    result_caption,
    run_mart_first,
    served_days,
    stamp_write,
    styled_table,
    with_user_names,
    write_gate_open,
)
from app.ui.schema_gate import has_migration

_PAGE = "Cost Intelligence"
# R1-162: chargeback_sql.department_window_credits / department_month_credits read the hourly-loaded
# FACT_WAREHOUSE_DAILY mart (v4.54), not the live metering view, so the source label names the mart.
_DEPT_MART = "FACT_WAREHOUSE_DAILY x DEPARTMENT_MAP"
_DEPT_SOURCE = _DEPT_MART + " (mart, loaded hourly)"
# The Track expander's first statement: raise at most one open item per user (fix_queue.ai_track_escalation_sql).
_ESCALATE = "ESCALATE"

# #42 Part 1: Company all-in showback (Cost > Chargeback & AI). Marts only; no source label
# spells the live-telemetry schema (this file sits at its live-scan budget).
_SHOWBACK_SOURCE = ("FACT_METERING_DAILY + FACT_WAREHOUSE_DAILY + FACT_OBJECT_COST_DAILY + FACT_AI_USAGE_DAILY"
                    " + FACT_STORAGE_DAILY + FACT_STORAGE_ACCOUNT_DAILY (marts, complete metered days)")
_SHOWBACK_HELP = (
    "Metered spend plus estimated storage for the window, split by company wherever the data carries a "
    "company key: warehouse metering (by warehouse), serverless maintenance from the object-cost ledger "
    "(by database), Cortex Code in Snowsight and the CLI (by user) and storage (by database). Spend with "
    "no company key stays on account-level rows, so the rows add up to the all-in total. Showback, not an "
    "invoice: nothing is spread by a share."
)
_SHOWBACK_TABLE_NOTE = (
    "Warehouse: exact warehouse metering before the cloud-services adjustment, by the warehouse's company "
    "(the same metering as Department chargeback above, over this panel's days). Serverless: the "
    "object-cost ledger's clustering, materialized-view refresh, search-optimization, serverless-task and "
    "Snowpipe credits, by the object's database; its query-compute arms are slices of warehouse compute "
    "and are left out so nothing counts twice. AI: Cortex Code token credits in Snowsight and the CLI, by "
    "user (Cortex Code Desktop is not read, so it stays on the unattributed row). Storage: estimated from "
    "average daily database and fail-safe bytes, by database. Other metered: metering with no company key."
)


# Split out of app/ui/pages/cost.py (V028): section bodies only —
# navigation/dispatch stays in cost.py. Import preamble mirrored from
# cost.py; ruff --fix prunes what this section does not use.

def _cortex_spend_tab(days: int, ai_rate: float, *, bounds: tuple | None = None) -> None:
    # v4.50: the storage panels moved to Spend & Attribution — storage is
    # neither chargeback nor AI, and the section label was hiding it.
    st.markdown("**Cortex / AI spend (account-wide)**")
    # NB: do NOT arm coverage_gate here. The AI-service metering series is naturally SPARSE (no
    # DAY row on idle days, and AI adoption may post-date a long window), so coverage_contract's
    # dense-series reach-back/interior-gap rules would reject a perfectly good mart on the common
    # case and degrade to the 90d-clamped live fallback — undercounting long windows. served_days
    # already labels the live-fallback path honestly; the mart is trusted for its available history.
    _lm = "_lm" if bounds is not None else ""
    res = run_mart_first(
        mart_sql.fact_cortex_daily_spend(days, bounds=bounds), cost_sql.cortex_daily_spend(days, bounds=bounds),
        page=_PAGE, key=f"cortex_{days}{_lm}",
        mart_source="FACT_METERING_DAILY (AI services, billed)",
        live_source="ACCOUNT_USAGE.METERING_DAILY_HISTORY (AI services, live fallback)")
    if guard(res, "No AI/Cortex service credits in this window."):
        df = res.df.copy()
        df["USD"] = df["CREDITS_BILLED"].map(safe_float) * ai_rate
        # The live fallback clamps to MAX_LIVE_WINDOW_DAYS, so label the window ACTUALLY
        # served (K1 contract), not the raw ask — else a 365d view over a cold mart shows a
        # 90-day sum under a "365d" label.
        _win = served_days(res, days)
        # WLA-1: this read honors bounds, so under "Last month" the data is bounded to the prior
        # calendar month — say "last month" then, matching the sibling AI tiles; the served-days
        # honesty (>90d live clamp) only bites on the trailing branch.
        _wlab = window_label(bounds, _win)
        kpi_row([{"label": f"Cortex spend, {_wlab}", "value": format_usd(float(df["USD"].sum())),
                  "help": f"Billed AI-service credits x ${ai_rate:.2f}."}])
        charts.daily_stacked_usd(df, "DAY", "SERVICE_TYPE", "USD")
        result_caption(res)

    # v4.157.0: the "AI Functions usage" breakout was a duplicate AI-spend chart
    # buried inside the per-user "AI users" panel. It is account-wide AI spend, so
    # it belongs here as an optional drill-down under Cortex/AI spend — one home
    # for account AI spend instead of two.
    with st.expander("AI Functions usage (optional view)"):
        # Expander bodies run even when collapsed (Codex r17 #18) — the scan
        # itself waits for the toggle, like the deep-scan forensics toggles.
        if not st.toggle("Load AI Functions usage", key="ai_fn_scan",
                         help="Scans CORTEX_AI_FUNCTIONS_USAGE_HISTORY once, then cached."):
            st.caption("Off until you ask — this view needs its own history scan.")
        else:
            # P8: metadata tier (4h). The source view lags hours and the numbers
            # are exact token metering, so an hourly re-scan re-pays the
            # secure-view expansion for an answer that cannot have changed.
            fn_res = run(cortex_sql.cortex_ai_functions_daily(days, bounds=bounds), page=_PAGE,
                         key=f"cortex_fn_{days}{_lm}", tier="metadata",
                         source="ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY")
            # R1-036: this is a plain live read that clamps a TRAILING window to the live-scan limit
            # (a calendar preset reads its exact range), so name the window it served -- a 365d page
            # must not read "no usage" for a 90-day scan. served_days() would not help: run() stamps
            # no served window.
            _fn_capped = bounds is None and int(days) > MAX_LIVE_WINDOW_DAYS
            _fn_win = window_label(bounds, min(int(days), MAX_LIVE_WINDOW_DAYS))
            _fn_cap = f" (live view, capped at {MAX_LIVE_WINDOW_DAYS}d)" if _fn_capped else ""
            if fn_res.ok and not fn_res.empty:
                fn = fn_res.df.copy()
                fn["USD"] = fn["TOTAL_CREDITS"].map(safe_float) * ai_rate
                charts.daily_stacked_usd(fn, "DAY", "SOURCE", "USD")
                result_caption(fn_res, note=f"AI Functions credits, {_fn_win}{_fn_cap}.")
            elif fn_res.ok:
                empty_state("no_data_yet", f"No AI Functions usage in {_fn_win}{_fn_cap}.")
            # R1-036 / R1-166: only a true absence is "not readable"; a timeout or drift is a failed
            # read with its error (the old caption called every failure "not available").
            elif is_setup_absence(fn_res.error_kind):
                empty_state("needs_setup",
                            "CORTEX_AI_FUNCTIONS_USAGE_HISTORY is not readable by this app's role "
                            "(the view is absent on some accounts).")
            else:
                empty_state("unavailable", "AI Functions usage could not be read.", detail=fn_res.error)


def _ai_users_tab(company: str, days: int, ai_rate: float, settings: dict, is_operator: bool, *, bounds: tuple | None = None) -> None:
    """Cortex Code user attribution — ported from the original AI & Cortex
    Monitor. Token credits are exact per user; projections and severities are
    computed in tested logic, and budget severities only exist when an AI
    budget is actually configured."""
    ai_budget = safe_float(settings.get("AI_MONTHLY_BUDGET_USD"))
    # Live-first (owner ask 2026-09-13): the User attribution detail shows CURRENT
    # Cortex Code usage, read straight from the live CORTEX_CODE_* usage views; the
    # daily FACT_AI_USAGE_DAILY snapshot (V061 loader arm [9]) is kept ONLY as a
    # resilience fallback on a transient live error. (It was fact-first in P2 to
    # dodge the ~22s scan on every render; the owner accepts that cost here for
    # freshness, and the whole tab is gated behind a "Load AI user attribution"
    # toggle so the scan is opt-in, not ambient.)
    #
    # Deliberately NOT run_mart_first, for two reasons the helper cannot serve:
    #  1. probe=True. Accounts with no Cortex Code subscription fail with 002139
    #     (see below) — an EXPECTED absence that must not be error-logged and
    #     counted as a failed fetch on every render. run_mart_first has no probe
    #     passthrough.
    #  2. The live leg answers at a DIFFERENT grain on purpose (P9): ONE 365d
    #     user-day-source fetch under a days-independent cache key, from which
    #     BOTH this rollup and the daily-by-source chart are folded in pandas.
    #     One scan per TTL instead of two per window.
    _lm = "_lm" if bounds is not None else ""
    # tier="recent": 5-min freshness with a 120s statement timeout — headroom for
    # the ~25s secure-view scan that "metadata"'s 30s timeout could not safely carry
    # as a primary read. The identical scan (same sql + tier) also backs the
    # CoCo-efficiency panel and the Security AI-guardrails fallback via one shared
    # (sql,scope) cache entry, so freshening it here freshens all three together.
    live_res = run(cortex_sql.cortex_code_user_daily(company), page=_PAGE,
                   key=f"cortex_user_daily_{company}", tier="recent",
                   source=("ACCOUNT_USAGE.CORTEX_CODE_*_USAGE_HISTORY "
                           "(365d live, window derived in-app)"),
                   probe=True, max_rows=200_000)
    if not live_res.ok and live_res.error_kind == "unknown_function":
        # Live finding 2026-07-10 (Joe traced it): the CORTEX_CODE_* views
        # internally call SYSTEM$GET_CORTEX_CODE_CLI_SUBSCRIPTION; without a
        # Cortex Code subscription that function does not exist (002139), so
        # OUR read throws even though our SQL never names it.
        empty_state(
            "needs_setup",
            "Cortex Code usage telemetry is not available in this account/region yet - "
            "Snowflake's usage views probe a subscription that is not present (002139). "
            "This tab lights up on its own if Cortex Code lands; nothing is misconfigured.")
        return
    if live_res.ok:
        # Live is the source of truth. An empty-but-ok frame = genuinely no usage
        # (reported by the guard below); never fall back to a staler fact on empty.
        rollup_res = replace(live_res, df=rollup_from_user_daily(live_res.df, days, bounds=bounds))
    else:
        # Transient live error (timeout/other): degrade to the daily FACT snapshot so
        # the panel still answers with the last loaded day rather than an error. The
        # result_caption discloses the fact source and its older freshness.
        live_res = None
        rollup_res = run(mart27_sql.ai_code_user_rollup(days, company, bounds=bounds), page=_PAGE,
                         key=f"cortex_users_{company}_{days}{_lm}", tier="hourly",
                         source="FACT_AI_USAGE_DAILY (Cortex Code, daily loader - live scan unavailable)")
    if not guard(rollup_res,
                 "No Cortex Code usage (Snowsight or CLI) recorded in this window for this scope.",
                 setup_hint="If these views aren't enabled in this account, this tab stays empty."):
        return

    # P8/C7: one divisor for the whole tab — days the scope has actually been
    # observable, never the asked window when the data is younger than it.
    eff_days = effective_window_days(rollup_res.df, days, bounds=bounds)
    enriched = enrich_user_rollup(rollup_res.df, ai_rate, eff_days, bounds=bounds)
    summary = rollup_summary(enriched, eff_days, bounds=bounds)
    budget_kpi_item = (
        {"label": "AI monthly budget", "value": format_usd(ai_budget),
         "help": "AI_MONTHLY_BUDGET_USD from SETTINGS; drives the severity flags below."}
        if ai_budget > 0 else
        {"label": "AI monthly budget", "value": "Not configured",
         "help": "Set AI_MONTHLY_BUDGET_USD in Admin to enable budget-breach severities. Nothing is assumed."}
    )
    # TLH-3: the user rollup is LIMIT-500 by credits desc. For a single-account fleet
    # that never binds, but disclose it if the frame is ever at the cap so the totals
    # aren't read as complete when they're the top-500 spenders only.
    _cc_trunc = len(enriched) >= 500
    # WLA-1 (round 18): mirror the Spend tab — under "Last month" the global window sets
    # days = the calendar span of last month (28-31) with bounds != None, but the reads
    # are bounded to that month. A raw "{days}d" label then reads as a trailing window
    # ending today (a DIFFERENT window than the data) and disagrees with the scope chip's
    # "Last month (Aug 1 - Aug 31)". Use "last month" when bounded, else the trailing "{days}d".
    _wlab = window_label(bounds, days)
    kpi_row([
        {"label": f"Active AI users ({_wlab})", "value": f"{summary['active_users']:,}"},
        {"label": "Requests", "value": f"{summary['total_requests']:,}"},
        {"label": "Cortex Code spend", "value": format_usd(summary["spend_usd"]),
         # NP-1: this is the sum of the per-user 'Spend $' rows (each rounded to cents) so it
         # reconciles exactly with the detail table below; it can differ by a few cents from the
         # Cost>Spend CoCo tile, which rounds the credit total ONCE. So "measured", not "exact".
         "help": f"Measured token credits x ${ai_rate:.2f}/credit, summed across the users below "
                 "(may differ by a few cents from the Cost page's single-rounded CoCo total)."
                 + (" Totals cover the top 500 users by spend (frame at cap); "
                    "account-wide totals are higher." if _cc_trunc else "")},
        {"label": "Projected 30d", "value": format_usd(summary["projected_30d_usd"]),
         "help": (f"Run-rate over the {eff_days} day(s) this scope has actually been "
                  f"active, extended to 30 days."
                  + ("" if eff_days >= days else
                     f" Asked window was {_wlab} — dividing by days that predate the "
                     "first Cortex request would under-report the burn."))},
        budget_kpi_item,
    ])

    left, right = st.columns([1.1, 1.0])
    with left:
        st.markdown("**Cost by user (exact token credits)**")
        # Owner ask (v4.50): the chart shows people, not login names — DISPLAY_NAME is "First Last".
        # Group by the UNIQUE login (USER_NAME), NOT DISPLAY_NAME: two distinct logins can share a
        # First+Last, and grouping by the display name would merge two people into one bar that then
        # disagrees with the login-keyed detail table below. Label by DISPLAY_NAME, disambiguating
        # with the login only where a name is shared, so namesakes stay distinct and legible.
        by_user = enriched.groupby(["USER_NAME", "DISPLAY_NAME"], as_index=False)["SPEND_USD"].sum()
        _dupe_name = by_user["DISPLAY_NAME"].duplicated(keep=False)
        by_user["LABEL"] = by_user["DISPLAY_NAME"].where(
            ~_dupe_name, by_user["DISPLAY_NAME"] + " (" + by_user["USER_NAME"].astype(str) + ")")
        by_user = by_user.sort_values("SPEND_USD", ascending=False)
        charts.bar_usd(by_user, "LABEL", "SPEND_USD", title="Spend (USD)", top_n=12, takeaway=True)
    with right:
        st.markdown("**Daily usage by source**")
        if live_res is not None:
            # The 365d fetch that served the rollup already holds every day —
            # folding it costs nothing, where the old live cortex_code_daily
            # was a second 15s secure-view scan of the same two views.
            daily_res = replace(live_res, df=daily_from_user_daily(live_res.df, days, bounds=bounds))
        else:
            # The rollup came off the fact, so the fact covers this window: an
            # empty daily read here is the ANSWER, not a cold mart. Reviving the
            # live scan would pay 15s to confirm what we already know.
            daily_res = run(mart27_sql.ai_code_daily(days, company, bounds=bounds), page=_PAGE,
                            key=f"cortex_daily_{company}_{days}{_lm}", tier="hourly",
                            source="FACT_AI_USAGE_DAILY (Cortex Code, daily loader)")
        if guard(daily_res, "No daily Cortex Code usage rows."):
            daily = daily_res.df.copy()
            daily["USD"] = daily["TOTAL_CREDITS"].map(safe_float) * ai_rate
            charts.daily_stacked_usd(daily, "DAY", "SOURCE", "USD")

    st.markdown("**User attribution detail**")
    styled_table(  # rec21: styled_table carries tz conversion, prettified headers, CSV
        enriched[[c for c in ["USER_NAME", "FIRST_NAME", "LAST_NAME", "EMAIL", "SOURCE",
                   "ACTIVE_DAYS", "TOTAL_REQUESTS",
                   "TOTAL_CREDITS", "TOTAL_TOKENS", "CREDITS_PER_REQUEST", "SPEND_USD",
                   "PROJECTED_30D_USD", "FIRST_USAGE", "LAST_USAGE"] if c in enriched.columns]],
        column_config={
            "TOTAL_REQUESTS": st.column_config.NumberColumn("Total Requests", format="%d"),
            "SPEND_USD": st.column_config.NumberColumn("Spend $", format="$%.2f"),
            "PROJECTED_30D_USD": st.column_config.NumberColumn("Proj. 30d $", format="$%.2f"),
            "CREDITS_PER_REQUEST": st.column_config.NumberColumn("Cr/request", format="%.4f"),
        },
    )
    # C37: exact per-user token metering — the rows sum to the Cortex Code
    # spend KPI above by construction (rollup_summary sums this same column).
    # review fix: sum-only — the section KPI derives from this SAME frame, so a
    # "parent" here was a tautological 100%, not an independent check (law 8).
    reconciliation_footer(float(enriched["SPEND_USD"].sum()), label="user rows")
    result_caption(rollup_res, note="Cortex Code token metering is exact per user; no allocation involved.")

    # C7: the per-user ladder plus the SCOPE-wide breach. Ten users at 20% of
    # budget each is 200% of the budget and used to print "no users over 25%".
    exceptions = with_aggregate_budget_row(
        classify_exceptions(enriched, ai_budget, ai_rate), summary, ai_budget)
    # E4: the thresholds belong wherever the verdicts are read — an operator
    # looking at a populated table should not have to empty it to learn what
    # "High" meant. One line, both rule families, the live constants.
    _rules = (
        "Rules — budget ladder on projected 30d spend: "
        + ", ".join(f"over {int(frac * 100)}% = {sev}" for frac, sev, _ in BUDGET_LADDER)
        + f" (per user, and once for the scope total). Cost-per-request spike = High when a "
        f"user/source's credits-per-request is a positive outlier vs the cohort "
        f"(median/MAD z ≥ {CPR_SPIKE_Z:.1f}), counted only with at least "
        f"{CPR_MIN_REQUESTS} requests and {format_usd(CPR_MIN_PROJECTED_USD)} projected 30d "
        f"— a spike is unusual for this account, not just a pricier model."
    )
    st.markdown("**Exceptions**")
    if exceptions.empty:
        if ai_budget > 0:
            empty_state("clean",
                        "No users over 25% of the AI budget, scope total inside budget, "
                        "and no cost-per-request spikes.")
        else:
            empty_state("needs_setup",
                        "No cost-per-request spikes. Configure AI_MONTHLY_BUDGET_USD to also flag budget pressure.")
        st.caption(md_dollars(_rules))
    else:
        styled_table(
            exceptions[["SEVERITY", "SIGNAL", "USER_NAME", "SOURCE", "TOTAL_REQUESTS",
                         "CREDITS_PER_REQUEST", "PROJECTED_30D_USD"]],
            column_config={
                "TOTAL_REQUESTS": st.column_config.NumberColumn("Total Requests", format="%d"),
            },
        )
        st.caption(md_dollars(_rules))
        _track_exceptions_expander(exceptions, company, is_operator)

    # The org daily cap as the V163 runaway arm reads it (junk / 0 / negative -> 15), resolved ONCE and
    # shared by the quota suggestions and the CoCo efficiency review below.
    _coco_cap = effective_cap(settings.get("COCO_DAILY_CAP_CREDITS"))
    # #37b: the suggestions reuse the live 365d user-day frame already fetched above (company-scoped by
    # cortex_code_user_daily's outer clause) -- None on the fact-fallback leg, where the panel says so.
    _ai_quota_panel(enriched, summary, days, bounds=bounds,
                    user_daily=(live_res.df if live_res is not None else None),
                    cap_credits=_coco_cap, ai_rate=ai_rate,
                    z_min=effective_z_min(settings.get("AI_RUNAWAY_ROBUST_Z")))

    _token_economics_panel(company, days, _coco_cap, bounds=bounds)


def _track_exceptions_expander(exceptions: pd.DataFrame, company: str, is_operator: bool) -> None:
    """Cost > Chargeback & AI > Exceptions: 'Track top exceptions as work items' (v4.605) -- the ONE shared Track
    write, as Operations > Optimize and Control Room triage use it. Three statements at most (raise at most one open
    item per user, then insert users, then the all-users scope), shown before the one-click button, executed in
    order behind the C48 latch."""
    with st.expander("Track top exceptions as work items"):
        # Replaces this page's own per-row INSERTs (pre-v4.605). One item per USER, keyed on the user with
        # every signal of that user in its detail, plus the all-users budget breach keyed on the Company
        # scope (AI_BUDGET). The NOT EXISTS is scoped to this page's SOURCE (a Security work item on the same
        # user never blocks an AI-spend item) and entity-keyed, and a still-open pre-v4.605 item from this
        # page (no entity key, under any SOURCE name the page ever wrote) blocks by its legacy TITLE, so nothing
        # queued the old way is duplicated. One item per user would swallow an escalation, so the first
        # statement raises ONE open item's SEVERITY per user when the new signal outranks every open item of that
        # user (never a downgrade; its estimate stays as first tracked; review r2 R2-2 / R2-9: a pre-v4.605 user
        # may hold several items, and raising them all counted one breach several times). The scope item's
        # estimate is the exposure beyond the user items built in the same click, so one click's items sum to
        # the scope total once; all-users items from other Company views or clicks are not subtracted
        # (fix_queue.ai_exception_track_items).
        _groups = ai_exception_track_items(exceptions, company)
        _stmts = [(kind, stmt) for kind, stmt in (
            (_ESCALATE, ai_track_escalation_sql(_groups[AI_USER_ENTITY_TYPE], actor_sql=identity_sql())),
            (AI_USER_ENTITY_TYPE, track_entities_sql(
                _groups[AI_USER_ENTITY_TYPE], entity_type=AI_USER_ENTITY_TYPE, source=AI_TRACK_SOURCE,
                actor_sql=identity_sql(), bulk=False, severities=AI_TRACK_SEVERITIES,
                company_from_user=True, source_scoped=True)),
            (AI_SCOPE_ENTITY_TYPE, track_entities_sql(
                _groups[AI_SCOPE_ENTITY_TYPE], entity_type=AI_SCOPE_ENTITY_TYPE, source=AI_TRACK_SOURCE,
                actor_sql=identity_sql(), bulk=False, severities=AI_TRACK_SEVERITIES,
                source_scoped=True)),
        ) if stmt]
        st.caption(f"Tracks the first {AI_TRACK_CAP} rows above into Action Center: one work item per "
                   "user, keyed on the user, with every signal of that user in its detail. Items land "
                   "UNASSIGNED at the strongest signal's severity, under the user's own company (UNKNOWN "
                   "when the user maps to none, so its estimate is never added to a named Company's "
                   "queue), priced monthly at the user's projected 30-day spend: all sources when the "
                   "user has a budget signal (an '(all sources)' row), else the sum of the spiking "
                   "sources. A user who already has "
                   "an open item from this page (tracked before, from any Company scope, including one "
                   "queued under an earlier page name) gets no new item. When the new signal is stronger than "
                   "every open item of that user, the first statement raises one of them, the strongest (on a "
                   "tie the item keyed on the user, else the newest), to the new severity and says so in its detail; its estimate stays as "
                   "first tracked. A user with several items queued before v4.605 keeps them all, and only "
                   "that one is raised.")
        if _groups[AI_SCOPE_ENTITY_TYPE]:
            st.caption(f"The all-users budget breach becomes one item for the {company} scope, estimated at "
                       "only the projected exposure beyond the user items tracked in the same click (none "
                       "when they count all of it). All-users items tracked from different Company views "
                       "are each priced this way and overlap, so do not add them together: the Queued work "
                       "total can count that exposure more than once.")
        for _kind, _stmt in _stmts:
            st.code(_stmt, language="sql")
        if (is_operator and _stmts and st.button("Track in Action Center", key="cortex_track_exec")
                and write_gate_open("cortex_track_exec")):
            ok_all, _err, _done = True, "", []
            for _kind, _stmt in _stmts:
                ok, _msg = execute_statement(_stmt.strip(), page=_PAGE)
                if not ok:
                    ok_all, _err = False, _msg
                    break                          # in order; stop at the first failure
                _done.append(_kind)
            stamp_write("cortex_track_exec", ok_all)  # C48
            if ok_all:
                notify(True, "Tracked in Action Center. An open item is never duplicated; a stronger signal "
                             "raises the user's strongest open item.")
            elif AI_USER_ENTITY_TYPE in _done:
                notify(False, f"The user items were tracked, but the all-users budget item was not: {_err}")
            elif _ESCALATE in _done:
                notify(False, "Open items were raised where a user's signal is stronger (at most one per "
                              f"user), but no new item was tracked: {_err}")
            else:
                notify(False, _err)
        elif not is_operator:
            st.caption("Copy and run as SNOW_ACCOUNTADMINS / SNOW_SYSADMINS - in-app execution needs an admin profile.")


def _ai_quota_panel(enriched: pd.DataFrame, summary: dict, days: int,
                    *, bounds: tuple | None = None, user_daily: pd.DataFrame | None = None,
                    cap_credits: float = DEFAULT_CAP_CREDITS, ai_rate: float = 0.0,
                    z_min: float = RUNAWAY_ROBUST_Z) -> None:
    """Native per-user AI cost quotas — who Snowflake has BLOCKED for hitting a
    per-user AI credit ceiling (SNOWFLAKE.CORE.QUOTA), from the account-wide
    QUOTA_ACCESS_BLOCK_HISTORY view (the one read a console can do; quota limits
    live in Snowsight, admin-scoped). When nothing is blocking, it quantifies the
    unguarded AI exposure from the per-user spend already fetched above. Reuses the
    tab's `enriched` frame + `summary` — no new per-user scan; only the block read.

    #37b: after every block outcome (a failed read included), a review-only table suggests a per-user
    daily and monthly
    quota from each user's OWN p95 over a fixed 90-day history, with a walk-forward
    back-test — pure pandas over ``user_daily`` (the live user-day frame the tab already
    holds; None on the fact-fallback leg), so still no new read."""
    rec = (recommend_quotas(user_daily, cap_credits, ai_rate, today=account_today(), z_min=z_min)
           if user_daily is not None else None)
    # WLA-1: match the tab's window label — "last month" under bounded scope, else "{days}d".
    _wlab = window_label(bounds, days)
    _wphrase = window_phrase(bounds, days)
    st.markdown("**Per-user AI quotas & blocks**")
    panel_help(
        "Snowflake per-user AI quotas (SNOWFLAKE.CORE.QUOTA) cap a user's daily/monthly AI "
        "credits and can AUTO-BLOCK new AI requests at the limit. OVERWATCH surfaces who is "
        "currently blocked — the one account-wide read Snowflake exposes; the quota limits "
        "themselves are set in Snowsight, Cost Management, Budgets. A blocked power user is an "
        "incident: raise or reset the quota, or investigate the runaway usage.")
    blk = run(cortex_sql.quota_access_block_history(days, bounds=bounds), page=_PAGE,
              key=f"ai_quota_blocks_{days}{'_lm' if bounds is not None else ''}", tier="recent",
              source="ACCOUNT_USAGE.QUOTA_ACCESS_BLOCK_HISTORY", probe=True, max_rows=1000)
    if not blk.ok:
        # v4.601.1: a failed read is never "no blocks". The read is a probe, so an absent view is not logged:
        # say what happened here instead (the v4.543 reader failed on every account and the panel reported no
        # blocks and no enforcing quota). A setup absence (absent, or an "Insufficient privileges" error, v4.605)
        # is a setup state; anything else is a failed read.
        if is_setup_absence(blk.error_kind):
            empty_state("needs_setup", "The quota block history view is not available to this app (per-user AI "
                        "quotas may not be enabled on this account, or the app's role cannot read it), so "
                        "blocks cannot be shown.")
        else:
            empty_state("unavailable", "The quota block history could not be read, so blocks cannot be "
                        "shown.", detail=blk.error)
    blocks, mapped = (block_history(blk.df, now=account_now()) if (blk.ok and not blk.empty)
                      else (pd.DataFrame(), True))
    # The read spans the page window AND the last 32 days: the window's rows drive the counts and the table,
    # while "currently blocked" is judged over every row (a user blocked today shows under 'Last month' too).
    in_win = in_window_rows(blocks)
    has_active = "IS_ACTIVE" in blocks.columns
    has_user = "USER" in blocks.columns
    _act = blocks["IS_ACTIVE"].astype(bool) if has_active else None
    _live = (int(blocks.loc[_act, "USER"].nunique()) if (has_active and has_user and _act is not None)
             else 0)
    if not in_win.empty or _live:
        _plus = "+" if blk.truncated else ""
        kpis = [{"label": f"AI-quota blocks ({_wlab})", "value": f"{block_events(in_win):,}{_plus}",
                 "help": "Times a user hit a per-user AI quota and was blocked in this window. "
                         "Account-wide — Snowflake exposes no company grain on this view."}]
        # "Currently blocked" counts distinct BLOCKED USERS (a user can hold >1 active block,
        # e.g. daily + monthly), not active rows — else it overcounts live incidents vs
        # "Users affected" (R1 fix). Falls back to an event count when USER is unmapped.
        if has_active and has_user:
            kpis.append(
                {"label": "Currently blocked",
                 "value": f"{_live:,}{_plus}",
                 "severity": "warn" if _live else "",
                 "help": "Distinct users whose AI access is blocked right now, whatever the window: their "
                         "latest action on a quota is a block that runs past now (BLOCKED_UNTIL, the start of "
                         "the quota's next cycle). Each is a live incident until the quota resets. Account-wide."})
        elif has_active and _act is not None:
            kpis.append(
                {"label": "Active block events", "value": f"{int(_act.sum()):,}",
                 "severity": "warn" if bool(_act.any()) else "",
                 "help": "Block rows still in effect (a user can hold more than one). Account-wide."})
        if has_user:
            kpis.append({"label": "Users affected", "value": f"{in_win['USER'].nunique():,}{_plus}",
                         "help": f"Distinct users blocked in {_wphrase}."})
        kpi_row(kpis)
        # the window's blocks; when none fall in the window, the blocks still in force
        _shown = in_win if not in_win.empty else (blocks.loc[_act] if _act is not None else blocks)
        if "IN_WINDOW" in _shown.columns:
            _shown = _shown.drop(columns=["IN_WINDOW"])
        _disp = (with_user_names(_shown.rename(columns={"USER": "USER_NAME"}), _PAGE)
                 if (mapped and has_user) else _shown)
        styled_table(_disp, slug="ai-quota-blocks", size_note=False)
        if in_win.empty:
            st.caption(f"No block was recorded in {_wphrase}; the table lists the blocks still in force."
                       if not blk.truncated else
                       f"The newest 1,000 block rows all fall outside {_wphrase}, so its blocks were not "
                       "read; the table lists the blocks still in force.")
        if blk.truncated:
            st.caption("Only the newest 1,000 block rows were read, so the window's counts are at least the "
                       "figures shown.")
        st.caption("Per-user AI quotas are account-wide — these blocks are NOT filtered to this "
                   "tab's company scope (the block view carries no company grain).")
    elif blk.ok or blk.error_kind == "absent":
        # No block in this window (or the view is not enabled here) -- which does NOT prove no quota exists, so
        # state the exposure the per-user spend on screen shows and point at the suggestions. A failed read gets
        # neither: its unavailable state above is the whole story for the blocks half.
        if blk.ok and blk.truncated:
            # the cap kept only newer rows (Last month): an empty window here is unknown, not clean
            empty_state("unavailable", f"The newest 1,000 block rows all fall outside {_wphrase}, so its "
                        "blocks were not read.")
        elif blk.ok:
            empty_state("clean", f"No per-user AI-quota blocks in {_wphrase}.")
        spend = safe_float(summary.get("spend_usd"))
        n_users = int(summary.get("active_users") or 0)
        if spend > 0 and n_users > 0 and "SPEND_USD" in enriched.columns and len(enriched):
            _top = enriched.sort_values("SPEND_USD", ascending=False).iloc[0]
            _top_name = str(_top.get("DISPLAY_NAME") or _top.get("USER_NAME") or "the top user")
            _qs = quota_summary(rec, cap_credits)
            _held = (f" Over the last {QUOTA_LOOKBACK_DAYS} complete days, limits at each user's own "
                     f"p95 would have held back {format_usd(_qs['backtest_usd_over'])} on "
                     f"{_qs['backtest_days_over']:,} user-day(s) (walk-forward back-test)."
                     if _qs["with_history"] else "")
            st.caption(md_dollars(
                f"AI exposure: OVERWATCH sees {format_usd(spend)} of AI spend across {n_users:,} "
                f"user(s) in {_wphrase} — top: {_top_name} at "
                f"{format_usd(safe_float(_top.get('SPEND_USD')))}.{_held} "
                "Suggested per-user limits are in the table below."))
    _suggested_quota_table(rec, enriched, cap_credits, z_min)


def _suggested_quota_table(rec: pd.DataFrame | None, enriched: pd.DataFrame,
                           cap_credits: float, z_min: float) -> None:
    """#37b: 'Suggested per-user AI quotas (review only)' — a ``quotas.recommend_quotas`` frame.

    Review only: OVERWATCH never creates or alters a quota (no quota DDL or method call here); the
    owner sets one in Snowsight. The history is FIXED at the last 90 complete days, whatever the
    page Window says (the served-window lesson: a standing limit is not a window statistic)."""
    st.markdown("**Suggested per-user AI quotas (review only)**")
    if rec is None:
        empty_state("needs_setup",
                    "Suggested quotas need the live Cortex Code user-day scan, which did not resolve "
                    "this run — the attribution above comes from the daily snapshot. They return with "
                    "the live scan.")
        return
    if rec.empty:
        empty_state("no_data_yet",
                    f"No Cortex Code usage by a named user in the last {QUOTA_LOOKBACK_DAYS} complete "
                    "days for this scope.")
        return
    qs = quota_summary(rec, cap_credits)
    kpi_row([
        {"label": f"Users with {QUOTA_MIN_ACTIVE_DAYS}+ active days",
         "value": f"{qs['with_history']:,} of {qs['users']:,}",
         "help": f"A suggestion needs at least {QUOTA_MIN_ACTIVE_DAYS} active days in the last "
                 f"{QUOTA_LOOKBACK_DAYS} complete days; the others show — until they have the history."},
        {"label": "p95 day above the daily cap", "value": f"{qs['p95_over_cap']:,}",
         "severity": "warn" if qs["p95_over_cap"] else "",
         "help": f"Users whose own p95 day exceeds COCO_DAILY_CAP_CREDITS ({cap_credits:g} credits): "
                 "a quota at the org cap would block them on an ordinary heavy day."},
        {"label": f"Runaway-rule days, {QUOTA_LOOKBACK_DAYS}d", "value": f"{qs['runaway_days']:,}",
         "severity": "warn" if qs["runaway_days"] else "",
         "help": f"User-days over {RUNAWAY_CAP_MULTIPLE:g}x the daily cap AND at least {z_min:g} "
                 "robust z above the user's own prior 90 active days (fewer than 5 = no baseline, "
                 "the cap alone decides) — the per-user AI runaway rule's test at its seed "
                 f"multiple, replayed day by day. {qs['runaway_users']:,} user(s) had at least one."},
        {"label": "Back-test: USD above the limits", "value": format_usd(qs["backtest_usd_over"]),
         "help": "Credits above each day's walk-forward limit x the AI credit price, summed over "
                 "users and days: what the suggested daily limits would have held back."},
    ])
    display = rec.copy()
    if {"USER_NAME", "DISPLAY_NAME"}.issubset(enriched.columns):
        # First Last from the rollup already on screen (no directory read); login is the fallback.
        names = (enriched[["USER_NAME", "DISPLAY_NAME"]].drop_duplicates("USER_NAME")
                 .set_index("USER_NAME")["DISPLAY_NAME"])
        display.insert(1, "DISPLAY_NAME", display["USER_NAME"].map(names).fillna(display["USER_NAME"]))
    styled_table(display, slug="ai-quota-suggestions", size_note=False)
    # The rule (COST_AI_USER_RUNAWAY) ships with V163; until then the replay is still exact, but there is
    # no rule row to tune yet. The shared schema gate answers from the startup read (no new statement).
    _tune = ("the live rule may be tuned in Alerts > Rules" if has_migration(163, _PAGE)
             else "the rule itself arrives with migration V163")
    st.caption(md_dollars(
        f"Method: a fixed {QUOTA_LOOKBACK_DAYS}-day history (the last {QUOTA_LOOKBACK_DAYS} complete "
        "days, not the page Window). Suggested daily = each user's own p95 active day, rounded up to "
        "a whole credit; suggested monthly = the p95 of their rolling 30-day totals (idle days count "
        "as 0). The back-test is walk-forward: each day is judged against the limit the same rule set "
        f"from the user's {QUOTA_LOOKBACK_DAYS} days BEFORE it (a day never sets its own limit), and "
        f"only days with {QUOTA_MIN_ACTIVE_DAYS}+ prior active days are judged. Runaway-rule days use "
        f"the runaway rule's seed multiple {RUNAWAY_CAP_MULTIPLE:g}x; {_tune}. Review only: "
        "OVERWATCH creates no quota — a per-user AI quota is set in Snowsight (Cost Management). "
        "Company-scoped like the User attribution detail above (unlike the account-wide blocks table); "
        "Cortex Code (Snowsight + CLI) credits."))


def _token_economics_panel(company: str, days: int, cap_credits: float, *, bounds: tuple | None = None) -> None:
    """CoCo efficiency review (repo review wave 2: TOKENS_GRANULAR). Cache-hit alone can't separate
    a heavy-but-targeted user from a high-intensity one, so this merges the token grain with
    per-user daily credits into peer-relative signals + a 🚩 Review flag. Tracks the page's Window
    filter (``days``). Opt-in toggle; a schema/telemetry miss (an absent view or TOKENS_GRANULAR column)
    degrades to an honest note, and any other failed read (another missing column = drift, a timeout)
    renders 'unavailable' with the error."""
    from app.logic.wave2 import (
        coco_coaching_count,
        coco_efficiency,
        fleet_cache_hit_pct,
        token_economics,
    )

    if not st.toggle("Load CoCo efficiency review", key="cortex_tok_econ",
                     help="Flattens TOKENS_GRANULAR (input / output / cache-read / cache-write) "
                          "per user and merges daily credits into peer-relative efficiency "
                          "signals — on demand; needs the newer view shape."):
        return
    # WLA-1 (round 18): honest window label — "last month" when the scope bounds the read
    # to the previous calendar month, else the trailing "{days}d" (see _ai_users_tab).
    _wlab = window_label(bounds, days)
    _when = ("in " + window_phrase(bounds, days))
    # v4.528: days-independent read (one 365d fetch, ONE cache entry shared across every
    # window/company); slice the window in pandas via cortex.token_types_window. The key is
    # now window-free so a window change reuses the cached fetch instead of re-scanning.
    te_res = run(cortex_sql.cortex_code_token_types(), page=_PAGE, key="cortex_token_types",
                 tier="historical",
                 source="CORTEX_CODE_*_USAGE_HISTORY (TOKENS_GRANULAR, window derived in-app)",
                 probe=True, max_rows=200_000)
    if not te_res.ok:
        # v4.603: the absence caption is only for the kinds run() treats as an EXPECTED absence on a probe read
        # (and so never logs): keep this tuple in lockstep with query.run's. A timeout or any other failure is
        # a failed read (logged by run()), never "the column doesn't exist" -- TOKENS_GRANULAR exists here.
        # v4.605: a missing column is the documented optional TOKENS_GRANULAR only when the error names it
        # (format_snowflake_error keeps the identifier: "does not expose X here"); any OTHER missing column is
        # schema drift -> 'unavailable'. Drift stays unlogged on a probe read, so that sentence names no log.
        _drift = te_res.error_kind == "missing_column" and "TOKENS_GRANULAR" not in str(te_res.error or "").upper()
        if te_res.error_kind in ("absent", "unknown_function", "missing_column") and not _drift:
            st.caption("TOKENS_GRANULAR isn't available on this account's Cortex Code views yet — "
                       "token-type economics appear here automatically once the column exists.")
        elif _drift:
            empty_state("unavailable", "A column the Cortex Code token-type read uses is missing from Snowflake's "
                        "view (schema drift), so token economics cannot be shown.", detail=te_res.error)
        elif is_setup_absence(te_res.error_kind):
            # v4.605: an "Insufficient privileges" error ('privilege', the one setup-absence kind the tuple above
            # leaves out because run() logs it) is a setup state like the other panels, never "Retry".
            empty_state("needs_setup", "The app's role cannot read the Cortex Code usage views (Insufficient "
                        "privileges), so token economics cannot be shown. Re-apply the app's grants (roles.sql).",
                        detail=te_res.error)
        else:
            empty_state("unavailable", "The Cortex Code token-type read failed this run, so token economics "
                        "cannot be shown. Retry, or see the Admin error log.", detail=te_res.error)
        return
    econ = token_economics(token_types_window(te_res.df, days, bounds=bounds))
    if econ.empty:
        empty_state("no_data_yet", "No token-type rows in the selected window.")
        return
    # Per-user daily credits drive the credit / session / over-cap signals. Same days-independent
    # scan + tier ("recent") as _ai_users_tab's live leg, so this reuses that one shared fetch.
    ud_res = run(cortex_sql.cortex_code_user_daily(company), page=_PAGE,
                 key=f"cortex_user_daily_{company}", tier="recent",
                 source="ACCOUNT_USAGE.CORTEX_CODE_*_USAGE_HISTORY (daily, window derived in-app)",
                 probe=True, max_rows=200_000)
    # econ (token grain) is ACCOUNT-WIDE — cortex_code_token_types has no company clause — so the
    # panel only becomes company-scoped through the (scoped) daily credit set. scoped=True makes
    # coco_efficiency REFUSE the account-wide fallback for a company view, so it never lists other
    # companies' users — whether the daily scan didn't resolve OR the company's credits fall
    # outside the selected window (emptying the rollup after the cut). The ALL view (no clause)
    # keeps its correct account-wide fallback.
    _scoped = bool(companies.user_clause(company, "USER_NAME"))
    # Thread bounds so the credit columns (TOTAL_CREDITS, AVG_DAILY_CR, DAYS_OVER_CAP, PEER_MULT)
    # are bounded to the prior calendar month under "Last month" scope — matching the v4.452 "last
    # month" column/caption label AND the sibling bounded "Active AI users" tile (both use the
    # bounded _window_slice). Without bounds coco_efficiency does a trailing cut, so the label read
    # "last month" over a trailing-N-day window ending today (a WLA-1-reverse mismatch).
    eff = coco_efficiency(econ, ud_res.df if ud_res.usable() else None,
                          cap_credits=cap_credits, window_days=days, as_of=account_today(),
                          scoped=_scoped, bounds=bounds)
    if _scoped and eff.empty:
        if not ud_res.usable():
            st.caption("Credit / session / over-cap signals need the Cortex Code daily usage scan, "
                       "which didn't resolve this run. The per-user token grain is account-wide and "
                       "can't be attributed to this company without it, so it's hidden here.")
        else:
            st.caption(f"No Cortex Code credit usage for this company {_when} — "
                       "widen the Window if they used CoCo earlier. Per-user token grain is "
                       "account-wide, so it isn't attributed to a single company here.")
        result_caption(te_res)
        return
    _flags = coco_coaching_count(eff)
    # Scope the cache KPIs/caption to the SHOWN (company) users, so a company view doesn't blend
    # other companies' account-wide token traffic into 'Fleet cache-hit' or the low-cache note.
    _shown = set(eff["USER_NAME"].astype(str)) if not eff.empty else set()
    _econ_shown = econ[econ["USER_NAME"].astype(str).isin(_shown)] if _shown else econ
    # The shown (credit) users can be DISJOINT from the account-wide token grain (e.g. their
    # Cortex Code rows predate TOKENS_GRANULAR), leaving _econ_shown empty. Show cache-hit as
    # n/a then, not a 0.0% that would contradict a "caching is high" caption computed off the
    # same empty frame.
    _has_cache = (not _econ_shown.empty) and ("CACHE_HIT_PCT" in _econ_shown.columns)
    _fleet = fleet_cache_hit_pct(_econ_shown) if _has_cache else 0.0
    _cap = round(cap_credits)
    kpi_row([
        {"label": "Flagged for review", "value": f"{_flags:,}",
         "delta_color": "inverse" if _flags else "off",
         "help": f"Users consistently over the {_cap} cr/day base allowance AND either heavy vs "
                 "peers or running extended autonomous sessions — a high-intensity usage pattern "
                 "worth reviewing."},
        {"label": "Users measured", "value": f"{len(eff):,}"},
    ])
    # v4.461 P2: fleet cache-hit demoted from a KPI card to a conditional caption —
    # it is a fleet aggregate the per-user CACHE_HIT_PCT column + the note below
    # already carry; the review flag, not caching, is the decision here.
    if _has_cache:
        st.caption(f"Fleet cache-hit {_fleet:.1f}% (cache_read / (cache_read + input)) — where high "
                   "and uniform, caching is not the lever; the review flag is.")
    if not ud_res.usable():
        st.caption("Credit / session / over-cap signals need the Cortex Code daily usage scan, "
                   "which didn't resolve this run — showing cache-grain behaviour only.")
    _cols = ["USER_NAME", "FLAG", "TOTAL_CREDITS", "PEER_MULT", "AVG_DAILY_CR", "DAYS_OVER_CAP",
             "ACTIVE_DAYS", "CR_PER_REQ", "CACHE_WRITE_PCT", "READ_AMP", "CACHE_HIT_PCT", "REASON"]
    styled_table(with_user_names(eff[_cols], _PAGE), height=340, column_config={
        "FLAG": st.column_config.TextColumn("Flag"),
        "TOTAL_CREDITS": st.column_config.NumberColumn(f"Credits ({_wlab})", format="%.1f"),
        "PEER_MULT": st.column_config.NumberColumn("Peer x", format="%.1f"),
        "AVG_DAILY_CR": st.column_config.NumberColumn("Avg cr/active day", format="%.1f"),
        "DAYS_OVER_CAP": st.column_config.NumberColumn(f"Days > {_cap}cr", format="%d"),
        "ACTIVE_DAYS": st.column_config.NumberColumn("Active days", format="%d"),
        "CR_PER_REQ": st.column_config.NumberColumn("Cr/request", format="%.3f"),
        "CACHE_WRITE_PCT": st.column_config.NumberColumn("Cache-write %", format="%.1f%%"),
        "READ_AMP": st.column_config.NumberColumn("Read-amp", format="%.0f"),
        "CACHE_HIT_PCT": st.column_config.NumberColumn("Cache hit %", format="%.1f%%"),
        "REASON": st.column_config.TextColumn("Why flagged"),
    })
    _low_cache = int((_econ_shown["CACHE_HIT_PCT"] < 80).sum()) if _has_cache else 0
    if not _has_cache:
        _cache_note = (
            "No token-grain (TOKENS_GRANULAR) cache data for these users, so cache-hit isn't "
            "measurable here — the flag relies on the credit / session / over-cap signals.")
    elif _low_cache == 0:
        _cache_note = (
            "Cache-hit % is high across every user here, so caching is NOT the lever — session "
            "weight, cache-write churn (context re-written at full price), and days-over-cap are.")
    else:
        _cache_note = (
            f"{_low_cache} user(s) have low cache-hit (<80%) — for them caching IS a real lever "
            "(context re-sent as fresh input); the flag adds the volume / session view for the rest.")
    st.caption(
        f"🚩 Review flags a high-intensity usage pattern — consistently over the {_cap} cr/day "
        f"allowance and either heavy sustained spend vs peers (peer x) or extended autonomous "
        f"sessions (cr/request, read-amp). It highlights a pattern to review, not a verdict — "
        f"confirm against delivered work before acting. {_cache_note} Peer-relative, {_wlab}.")
    with st.expander("Raw token grain (input / output / cache tokens)", expanded=False):
        # _econ_shown, not econ: stay scoped to the shown (company) users so a company view
        # doesn't leak other companies' per-user token traffic in this expander.
        styled_table(with_user_names(_econ_shown, _PAGE), height=280, column_config={
            "INPUT": st.column_config.NumberColumn("Input", format="%d"),
            "OUTPUT": st.column_config.NumberColumn("Output", format="%d"),
            "CACHE_READ": st.column_config.NumberColumn("Cache Read", format="%d"),
            "CACHE_WRITE": st.column_config.NumberColumn("Cache Write", format="%d"),
            "TOTAL": st.column_config.NumberColumn("Total", format="%d"),
            "CACHE_HIT_PCT": st.column_config.NumberColumn("Cache hit %", format="%.1f%%"),
        })
    result_caption(te_res)


@st.fragment
def _statement_export(company: str, rate: float) -> None:
    """Fragment: month picks and the zip build rerun this block only."""
    st.markdown("**Monthly statement export**")
    from datetime import timedelta

    today = account_today()
    this_month = today.strftime("%Y-%m")
    prev = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    month = st.selectbox("Statement month", [prev, this_month], key="cb_month",
                         help="Prior month is the finance-ready one; current month is partial.")
    st.caption(
        "Scope: warehouse compute only. Storage, serverless, AI/Cortex, and data-transfer "
        "dollars are not allocated here (DEPARTMENT_MAP maps warehouses and roles), so these "
        "statements will not tie out against the full invoice — allocate the rest from the "
        "org rate-card totals on Contract & Forecast."
    )
    if st.button("Build department statements", key="cb_build"):
        import io
        import zipfile

        month_res = run(chargeback_sql.department_month_credits(month, company), page=_PAGE,
                        key=f"cb_month_{company}_{month}", tier="historical",
                        source=_DEPT_MART + " (calendar month)")
        if not month_res.usable():
            # r-ux: separate a clean EMPTY month (no credits — neutral no_data) from a FAILED read
            # (unavailable + the error in a collapsed detail expander), instead of dumping either
            # into a red st.error blob.
            if month_res.ok:
                empty_state("no_data_yet", "No credits recorded for that month/scope.")
            else:
                empty_state("unavailable", "Month credits could not be read.", detail=month_res.error)
        else:
            frame = month_res.df.copy()
            frame["USD"] = frame["CREDITS_TOTAL"].map(lambda c: credits_to_usd(c, rate))
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
                # Group the summary by DEPARTMENT ALONE so its grain matches the per-department
                # statement files and the department count below. A department can span warehouses
                # mapped to different OWNER values (OWNER is per-warehouse free text), which would
                # otherwise split one department into several summary rows with partial totals —
                # none equal to its true spend; fold distinct owners into one cell instead.
                summary = frame.groupby("DEPARTMENT", as_index=False).agg(
                    DEPT_OWNER=("DEPT_OWNER", lambda s: "; ".join(sorted(set(s.astype(str))))),
                    USD=("USD", "sum"))
                bundle.writestr("00_summary.csv", summary.to_csv(index=False))
                # Two department names can sanitize to the SAME filename ('R&D'/'R/D' -> 'R_D',
                # or names sharing their first 60 chars), and a duplicate zip arcname silently
                # overwrites the earlier member on extraction — one department's statement would
                # vanish while the summary still lists it. De-dup arcnames (reserving the fixed
                # members) so every department gets its own file.
                _used = {"00_summary", "manifest"}
                for dept_name, block in frame.groupby("DEPARTMENT"):
                    base = "".join(ch if ch.isalnum() else "_" for ch in str(dept_name))[:60] or "dept"
                    safe_name, _n = base, 2
                    while safe_name.lower() in _used:
                        safe_name = f"{base[:56]}_{_n}"
                        _n += 1
                    _used.add(safe_name.lower())
                    bundle.writestr(f"{safe_name}.csv", block.to_csv(index=False))
                bundle.writestr(
                    "MANIFEST.txt",
                    f"OVERWATCH chargeback statements - {company} - {month}\n"
                    f"Rate: ${rate:.2f}/credit (CORE settings). Warehouse metering is exact; "
                    f"idle time bills to the owning department.\n"
                    "Scope: warehouse compute only. Storage, serverless, AI/Cortex, and data "
                    "transfer dollars are NOT allocated in these statements — see Cost & "
                    "Contract > Contract & Forecast for org rate-card totals. These statements "
                    "will not tie out against the full invoice.\n"
                    f"Total (warehouse compute): ${float(frame['USD'].sum()):,.2f} across "
                    f"{frame['DEPARTMENT'].nunique()} departments.",
                )
            export_button(
                "Statements (.zip)", data=buffer.getvalue(),
                file_name=f"overwatch_chargeback_{company}_{month}.zip",
                mime="application/zip", key="cb_dl",
            )
            st.success(f"{frame['DEPARTMENT'].nunique()} department statements for {month}.")

def _chargeback_tab(company: str, days: int, rate: float, is_operator: bool, *,
                    bounds: tuple | None = None) -> QueryResult | None:
    """Department chargeback: warehouse = exact usage (idle + unadjusted CS), role = allocated usage lens.

    Returns the co-scheduled Company all-in showback batch member (#42) for
    _company_showback_panel, or None when the batch did not return one (the panel then reads
    it serially). Both exits return it, so the panel never re-reads what the batch fetched."""
    _lm = "_lm" if bounds is not None else ""
    # WLA-1 (round 18): "last month" when bounded to the prior calendar month, else "{days}d".
    _wlab = window_label(bounds, days)
    # B4 (v4.532): the four independent first-paint reads — dept credits (historical), the
    # role-share MART leg (hourly), department budgets (live), and the department map (recent,
    # operator only) — co-schedule in ONE run_batch_mixed round trip instead of four serial ones.
    # dept still gates the tab; share stays run_mart_first with the batched mart leg passed as its
    # `preloaded` (a cold/short mart still falls through to the live share read); each other read
    # keeps its serial run() fallback (a missing/failed member just re-reads).
    _cb_specs = [
        {"key": "dept", "tier": "historical",
         "sql": chargeback_sql.department_window_credits(days, company, bounds=bounds),
         "source": _DEPT_SOURCE},
        {"key": "share", "tier": "hourly",
         "sql": mart27_sql.role_share(days, company, bounds=bounds),
         "source": "FACT_QUERY_ROLE_HOURLY (mart — exec-sec share)"},
        {"key": "bud", "tier": "live", "sql": mart_sql.dept_budgets(), "source": "DEPT_BUDGETS"},
    ]
    if is_operator:
        _cb_specs.append({"key": "dmap", "tier": "recent",
                          "sql": chargeback_sql.department_map(), "source": "DEPARTMENT_MAP"})
    # #42: the Company all-in showback (the next section) rides this SAME round trip — marts
    # only, hourly-cached; the panel reads it serially only when the batch returns no member.
    _cb_specs.append({"key": "showback", "tier": "hourly",
                      "sql": chargeback_sql.company_allin_showback(days, company, bounds=bounds),
                      "source": _SHOWBACK_SOURCE})
    _pf = run_batch_mixed(_cb_specs, page=_PAGE) or {}
    _showback = _pf.get("showback")
    dept_res = _pf.get("dept") or run(chargeback_sql.department_window_credits(days, company, bounds=bounds), page=_PAGE,
                   key=f"cb_dept_{company}_{days}{_lm}", tier="historical",
                   source=_DEPT_SOURCE)
    if not guard(dept_res, "No warehouse credits in this window.",
                 setup_hint="Not installed yet — an admin can verify on Admin → Migrations & freshness. Seed department names in DEPARTMENT_MAP."):
        return _showback
    df = dept_res.df.copy()
    df["USD"] = df["CREDITS_TOTAL"].map(lambda c: credits_to_usd(c, rate))
    dept = df.groupby("DEPARTMENT", as_index=False)["USD"].sum().sort_values("USD", ascending=False)
    unmapped_usd = float(dept[dept["DEPARTMENT"] == "Unmapped"]["USD"].sum())
    total_usd = float(dept["USD"].sum())

    kpi_row([
        {"label": f"Chargeback total ({_wlab})", "value": format_usd(total_usd),
         "help": "Exact WAREHOUSE-COMPUTE metering x rate — includes each warehouse's "
                 "cloud-services credits, unadjusted (the account-level rebate lives "
                 "on Cost Intelligence → Spend & Attribution). Reconciles to the scoped "
                 "warehouse spend by construction; storage, serverless, AI, and transfer "
                 "are not allocated here. The Company all-in showback below adds serverless, "
                 "Cortex Code and storage by company; transfer is not attributed anywhere."},
        {"label": "Departments", "value": f"{dept['DEPARTMENT'].nunique()}"},
        {"label": "Unmapped", "value": format_usd(unmapped_usd),
         "delta": "map warehouses below" if unmapped_usd > 0 else "fully mapped",
         "delta_color": "inverse" if unmapped_usd > 0 else "normal",
         "help": "Credits from warehouses with no DEPARTMENT_MAP row. Should be $0."},
    ])
    charts.bar_usd(dept, "DEPARTMENT", "USD", title="Spend (USD, exact)", takeaway=True)
    styled_table(
        df[["DEPARTMENT", "WAREHOUSE_NAME", "COMPANY", "CREDITS_TOTAL", "USD"]],
        column_config={"USD": st.column_config.NumberColumn("Spend $", format="$%.0f")},
    )
    # C37: the rows are the exact metering the KPI sums, so coverage is 100% by
    # construction — the footer proves the tie-out instead of asserting it.
    # review fix: sum-only — total_usd sums this SAME frame (tautological 100%).
    reconciliation_footer(float(df["USD"].sum()), label="department rows")
    result_caption(dept_res, note="Idle credits stay with the owning department - that is the point of chargeback.")

    st.markdown("**Role usage within warehouses (allocated)**")
    st.caption(
        "Execution-time share per role (elapsed on the live fallback) inside each warehouse x that warehouse's exact spend. "
        "Usage lens for conversations, not the billing number. Shares are whole-warehouse: "
        "roles outside this scope keep their slice, so a warehouse's rows can sum below 1."
    )
    share_res = run_mart_first(
        mart27_sql.role_share(days, company, bounds=bounds),
        chargeback_sql.role_share_within_warehouse(days, company, bounds=bounds),
        page=_PAGE, key=f"cb_share_{company}_{days}{_lm}",
        mart_source="FACT_QUERY_ROLE_HOURLY (mart — exec-sec share)",
        live_source="QUERY_HISTORY (elapsed share per warehouse, live fallback)",
        preloaded=_pf.get("share"))
    if share_res.usable():
        # r6-bug8: match the pool window to the share window. When the role mart is cold,
        # the live share leg (role_share_within_warehouse) clamps to <=90d while the pool
        # (department_window_credits) spans up to 365d — a 90d share x a 365d pool
        # over-attributes recent-only roles. If a >90d request is served LIVE, rebuild the
        # per-warehouse pool over the same clamped window (mirrors spend.py _alloc_pool).
        # r4: a SHORT-retention role mart (FACT_QUERY_ROLE_HOURLY purged below the window) used
        # to slip past this by serving the mart with a short-window share; role_share now carries
        # a reach-back coverage gate that makes it abstain in that case, so the live leg serves and
        # this same rematch fires — no separate mart-path branch needed.
        _pool_df = df
        # r9: this trailing-90d rematch is ONLY correct for a TRAILING >90d window. Under a calendar
        # preset (bounds set) the live share leg does NOT clamp to 90d — scope_window_where ignores
        # the bounded_days clamp once bounds is set and scans the FULL bounded range — so ELAPSED_SHARE
        # is a true full-window share, and `df` (department_window_credits(days, company, bounds=bounds))
        # is already the matching bounded pool. Rebuilding a trailing-90d pool here would scale a full
        # Current-year share by a last-90-days pool (~3x under-scale + a distorted split). Skip it.
        if bounds is None and "QUERY_HISTORY" in str(share_res.source) and days > MAX_LIVE_WINDOW_DAYS:
            _pr = run(chargeback_sql.department_window_credits(MAX_LIVE_WINDOW_DAYS, company),
                      page=_PAGE, key=f"cb_dept_{company}_{MAX_LIVE_WINDOW_DAYS}", tier="historical",
                      source=_DEPT_MART + " (share-matched window)")
            if _pr.usable():
                _pool_df = _pr.df.copy()
                _pool_df["USD"] = _pool_df["CREDITS_TOTAL"].map(lambda c: credits_to_usd(c, rate))
        # r6-bug12: aggregate to warehouse grain BEFORE mapping. _pool_df is grouped by
        # (DEPARTMENT, WAREHOUSE_NAME, COMPANY); set_index(WAREHOUSE_NAME).to_dict() keeps
        # only the LAST row when a warehouse spans multiple companies/departments, so every
        # role's allocation on that warehouse scaled by a fraction of the true pool.
        wh_usd = _pool_df.groupby("WAREHOUSE_NAME")["USD"].sum().to_dict()
        share = share_res.df.copy()
        # vectorized (r18 #16) — same math, Series-wise instead of per-row
        share["ALLOCATED_USD"] = (
            share["ELAPSED_SHARE"].map(safe_float)
            * share["WAREHOUSE_NAME"].astype(str).map(wh_usd).fillna(0.0)
        ).round(2)
        by_role = (share.groupby("ROLE_NAME", as_index=False)["ALLOCATED_USD"].sum()
                   .sort_values("ALLOCATED_USD", ascending=False))
        charts.bar_usd(by_role, "ROLE_NAME", "ALLOCATED_USD", title="Allocated $ by role", top_n=12)
        with st.expander("Role detail per warehouse"):
            styled_table(
                share[["WAREHOUSE_NAME", "ROLE_NAME", "QUERY_COUNT", "ELAPSED_SHARE", "ALLOCATED_USD"]],
                column_config={
                    "ELAPSED_SHARE": st.column_config.NumberColumn("Share", format="%.3f"),
                    "ALLOCATED_USD": st.column_config.NumberColumn("Allocated $", format="$%.0f"),
                },
            )
    # R1-166: the header + caption above used to sit over nothing when the share read was empty or failed.
    elif share_res.ok:
        empty_state("no_data_yet", f"No role activity on these warehouses in {_wlab}.")
    elif is_setup_absence(share_res.error_kind):
        empty_state("needs_setup", "The role-share sources are not readable by this app's role, so no role "
                                   "allocation is shown.")
    else:
        empty_state("unavailable", "Role usage shares could not be read, so no role allocation is shown.",
                    detail=share_res.error)

    st.markdown("**Department budgets & pace**")
    panel_help(
        "Budgets live in DEPT_BUDGETS; the hourly scan raises COST_DEPT_BUDGET_PACE when a "
        "department runs ahead of pace (threshold on the Alerts page). Spend is the "
        "department's warehouses — exact billing, same as the table above."
    )
    bud = _pf.get("bud") or run(mart_sql.dept_budgets(), page=_PAGE, key="dept_budgets", tier="live",
              source="DEPT_BUDGETS")
    if bud.ok and not bud.empty:
        styled_table(with_user_names(bud.df, _PAGE, user_col="UPDATED_BY", display_col="Updated by"))
    elif bud.ok:
        empty_state("needs_setup", "No department budgets set yet — add one below and the pace alert goes live.")
    # R1-166: a failed budgets read used to leave the section blank under its header.
    elif is_setup_absence(bud.error_kind):
        empty_state("needs_setup", "DEPT_BUDGETS is not installed or not readable yet — an admin can check "
                                   "Admin → Migrations & freshness.")
    else:
        empty_state("unavailable", "Department budgets could not be read.", detail=bud.error)
    if is_operator:
        dmap = _pf.get("dmap") or run(chargeback_sql.department_map(), page=_PAGE, key="cb_dmap_bud", tier="recent",
                   source="DEPARTMENT_MAP")
        dept_opts = (sorted(dmap.df["DEPARTMENT"].astype(str).unique())
                     if dmap.usable() and "DEPARTMENT" in dmap.df.columns else [])
        c_d, c_b = st.columns(2)
        pick_dept = c_d.selectbox("Department", dept_opts, key="bud_dept") if dept_opts else             c_d.text_input("Department", key="bud_dept_txt")
        bud_usd = c_b.number_input("Monthly budget USD (0 removes)", 0, 10_000_000, 0,
                                   step=500, key="bud_usd")
        # rec46: build the upsert/delete SQL and show it BEFORE the save button,
        # matching every peer write on this page (SQL always shown first). Low-risk
        # upsert, so no type-to-confirm — just make the statement visible pre-click.
        if bud_usd > 0:
            stmt_b = (
                f"MERGE INTO {core_object('DEPT_BUDGETS')} t "
                f"USING (SELECT {sql_literal(str(pick_dept))} AS D) s ON t.DEPARTMENT = s.D "
                f"WHEN MATCHED THEN UPDATE SET MONTHLY_BUDGET_USD = {sql_number(float(bud_usd))}, "
                f"UPDATED_AT = CURRENT_TIMESTAMP(), UPDATED_BY = {identity_sql()} "
                # Stamp UPDATED_BY on INSERT too — the column DEFAULTs to CURRENT_USER(), which in
                # owner's-rights SiS is the app owner, not the operator; a NEW budget would otherwise
                # be misattributed to the owner until the next edit (which the UPDATE branch fixes).
                f"WHEN NOT MATCHED THEN INSERT (DEPARTMENT, MONTHLY_BUDGET_USD, UPDATED_BY) "
                f"VALUES (s.D, {sql_number(float(bud_usd))}, {identity_sql()});"
            )
        else:
            stmt_b = (f"DELETE FROM {core_object('DEPT_BUDGETS')} "
                      f"WHERE DEPARTMENT = {sql_literal(str(pick_dept))};")
        if pick_dept:  # rec46: no SQL preview / save until a department is picked
            st.code(stmt_b, language="sql")
            if st.button("Save budget", key="bud_save") and write_gate_open(f"bud_save:{pick_dept}"):
                ok, msg = execute_statement(stmt_b, page=_PAGE)
                stamp_write(f"bud_save:{pick_dept}", ok)  # C48
                notify(ok, msg if not ok else f"Budget saved for {pick_dept}.")
        else:
            st.caption("Pick a department to preview and save its budget.")

    _statement_export(company, rate)

    with st.expander("Manage mapping"):
        # B4 (v4.532): reuse the batched department_map member for operators (identical
        # department_map() sql). run_batch_mixed caches its member in a layer run() doesn't
        # consult, so WITHOUT this an operator would fetch DEPARTMENT_MAP twice on first paint;
        # a non-operator (dmap not batched) falls through to the serial read exactly as before.
        map_res = _pf.get("dmap") or run(chargeback_sql.department_map(), page=_PAGE, key="cb_map", tier="recent",
                      source="DEPARTMENT_MAP")
        if map_res.usable():
            styled_table(with_user_names(map_res.df, _PAGE, user_col="UPDATED_BY", display_col="Updated by"),
                         height=280)
        # R1-166: the same silent-on-failure shape -- say whether the map is empty or unreadable.
        elif map_res.ok:
            empty_state("no_data_yet", "No warehouse or role mappings yet — add one below.")
        elif is_setup_absence(map_res.error_kind):
            empty_state("needs_setup", "DEPARTMENT_MAP is not installed or not readable yet — an admin can "
                                       "check Admin → Migrations & freshness.")
        else:
            empty_state("unavailable", "The department mapping could not be read.", detail=map_res.error)
        unmapped_whs = sorted(df[df["DEPARTMENT"] == "Unmapped"]["WAREHOUSE_NAME"].unique())
        c1, c2, c3 = st.columns(3)
        with c1:
            map_type = st.selectbox("Type", ["WAREHOUSE", "ROLE"], key="cb_map_type")
        with c2:
            default_name = unmapped_whs[0] if unmapped_whs and map_type == "WAREHOUSE" else ""
            name = st.text_input("Name", value=default_name, key="cb_map_name")
        with c3:
            department = st.text_input("Department", key="cb_map_dept")
        owner = st.text_input("Owner", value="DBA", key="cb_map_owner")
        merge_sql = (
            f"MERGE INTO {core_object('DEPARTMENT_MAP')} t\n"
            f"USING (SELECT {sql_literal(map_type)} AS MAP_TYPE, {sql_literal(name.upper())} AS NAME, "
            f"{sql_literal(department)} AS DEPARTMENT, {sql_literal(owner)} AS OWNER) s\n"
            "ON t.MAP_TYPE = s.MAP_TYPE AND t.NAME = s.NAME\n"
            "WHEN MATCHED THEN UPDATE SET DEPARTMENT = s.DEPARTMENT, OWNER = s.OWNER, "
            f"UPDATED_AT = CURRENT_TIMESTAMP(), UPDATED_BY = {identity_sql()}\n"
            # Stamp UPDATED_BY on INSERT too — it DEFAULTs to CURRENT_USER() (the app owner under
            # owner's-rights SiS), so a NEW mapping would credit the owner, not the operator, until
            # a later edit corrects it via the UPDATE branch.
            "WHEN NOT MATCHED THEN INSERT (MAP_TYPE, NAME, DEPARTMENT, OWNER, UPDATED_BY) "
            f"VALUES (s.MAP_TYPE, s.NAME, s.DEPARTMENT, s.OWNER, {identity_sql()});"
        )
        st.code(merge_sql, language="sql")
        if (is_operator and name and department and st.button("Map to department", key="cb_map_exec")
                and write_gate_open(f"cb_map_exec:{name}")):
            ok, msg = execute_statement(merge_sql.replace("\n", " "), page=_PAGE)
            stamp_write(f"cb_map_exec:{name}", ok)  # C48
            notify(ok, msg if not ok else f"Mapped {name} → {department}.")
        elif not is_operator:
            st.caption("Copy and run as SNOW_ACCOUNTADMINS / SNOW_SYSADMINS - in-app execution needs an admin profile.")
    return _showback


def _company_showback_panel(company: str, days: int, rate: float, ai_rate: float, settings: dict, *,
                            bounds: tuple | None = None, prefetched: QueryResult | None = None) -> None:
    """#42 Part 1: per company, warehouse + serverless + Cortex Code + estimated storage dollars, with
    the cloud-services adjustment and the unattributed remainder as account-level rows, so the rows
    tie out to billed metering plus estimated storage. Read-only; marts only; the read rides the
    Department chargeback batch (``prefetched``) and falls back to one serial run()."""
    panel_help(_SHOWBACK_HELP)
    res = prefetched if prefetched is not None else run(
        chargeback_sql.company_allin_showback(days, company, bounds=bounds), page=_PAGE,
        key=f"cb_showback_{company}_{days}{'_lm' if bounds is not None else ''}", tier="hourly",
        source=_SHOWBACK_SOURCE)
    if res.ok and res.truncated:
        # one row per Cortex Code user rides this frame: a capped read would under-count the company
        # rows (and could drop the WINDOW / COVERAGE rows), so it is never totalled
        empty_state("unavailable", "The showback read hit the row cap, so its company totals would be "
                                   "incomplete; it is not shown.")
        result_caption(res)
        return
    if not guard(res, "The showback read returned no rows for this window."):
        return
    out = showback.company_showback(res.df, rate=rate, ai_rate=ai_rate,
                                    storage_rates=showback.storage_tier_rates(settings), company=company)
    s = out["summary"]
    wlab = window_label(bounds, days)
    state = out["state"]
    if state == "shape":
        empty_state("unavailable",
                    "The showback read came back without the columns or rows this panel needs, so it cannot "
                    "be shown.",
                    detail="Missing: " + ", ".join(str(m) for m in s["missing"]))
        result_caption(res)
        return
    if state == "no_ledger":
        empty_state("needs_setup",
                    "Daily metering (FACT_METERING_DAILY) has no rows yet, so there is no billed total to "
                    "split. It fills once the daily facts loader has run; an admin can check Admin → "
                    "Migrations & freshness.")
        result_caption(res)
        return
    if state == "no_basis":
        stall = (f" The newest daily-metering day is {s['stall_day']}, so the metering loader may be behind "
                 "(Admin → Migrations & freshness).") if s.get("stall_day") else ""
        empty_state("no_data_yet",
                    "No complete metered day falls in this Window yet (today and the newest, possibly "
                    "unfinished, daily-metering day never count). Widen the Window to see the showback." + stall)
        result_caption(res)
        return

    span = s["span_label"]
    table = out["table"]
    if not s["scoped"]:
        t = safe_float(s["allin_total_usd"])
        c = safe_float(s["company_usd"])
        adj = safe_float(s["adjustment_usd"])
        u = safe_float(s["unattributed_usd"])
        pct = s["company_share_pct"]
        kpi_row([
            {"label": f"All-in total, {span}", "value": format_usd(t),
             "help": "Billed metering credits (cloud-services adjustment applied) priced at the configured "
                     "compute and AI rates, plus storage estimated at the configured storage rates, over the "
                     "complete metered days shown. The credit part is the same basis as the credit-spend tile "
                     "on Spend & Attribution, but complete days only. Excludes data transfer, Marketplace and "
                     "org-currency adjustments."},
            {"label": "Company-attributed share, before adjustment",
             "value": f"{pct:.0f}%" if pct is not None else "—",
             "help": "Company rows, UNKNOWN included, as a share of the spend before the cloud-services "
                     "adjustment: the company rows plus the unattributed row, which add up to 100%. Company "
                     "warehouse dollars are metering before the adjustment, and the adjustment is a credit on "
                     "the whole bill rather than on any company, so it is left out of the share (the all-in "
                     "total is after it). UNKNOWN is spend whose "
                     "warehouse, database or user has no company evidence yet; a COMPANY_SCOPE mapping moves "
                     "it (Cortex Code at once, the other lines as the loaders re-stamp recent days; see "
                     "Unmapped entities on Spend & Attribution). A different lens from Spend & "
                     "Attribution's 'Attributable to a company', which counts warehouse metering only."},
            {"label": "Unattributed (no company key)", "value": format_usd(u),
             "help": "The all-in total minus the company rows and the cloud-services adjustment: "
                     "reader-account and replication metering, AI services other than Cortex Code in "
                     "Snowsight and the CLI, serverless with no object-cost ledger arm, cloud services "
                     "outside any warehouse, storage with no per-database split, a few hours of day-boundary "
                     "offset, and anything on days a source has not loaded or has only partly loaded (named "
                     "under the table when it happens)."},
            {"label": "Cloud-services adjustment", "value": format_usd(abs(adj)),
             "help": "The daily cloud-services adjustment from metering: a credit that lowers the bill (a "
                     "positive amount here, negative in the table). It is account-level, so it is its own "
                     "row and is not taken out of any company; company warehouse dollars are exact metering "
                     "before it, the same basis as Department chargeback above."},
        ])
        company_rows = table[~table["COMPANY"].isin((showback.ADJUSTMENT_ROW, showback.UNATTRIBUTED_ROW))]
        if len(company_rows) >= 2:
            # takeaway stays off: its share note would be of the company rows, not of the all-in total
            charts.bar_usd(company_rows, "COMPANY", "TOTAL_USD", title="All-in USD", top_n=10)
        # No sort_label: with one, a 4+ row table draws F26's 0-floored in-cell bar, and the
        # adjustment row is negative.
        styled_table(table, slug="company-showback", size_note=False)
        st.caption(_SHOWBACK_TABLE_NOTE)
        # The table carries its own remainder row, so there is no reconciliation_footer (its
        # "variance" would read as an error); this line is the tie-out.
        st.caption(md_dollars(
            f"Ties out: company rows {format_usd(c)} {'−' if adj < 0 else '+'} cloud-services adjustment "
            f"{format_usd(abs(adj))} {'−' if u < 0 else '+'} unattributed {format_usd(abs(u))} = all-in total "
            f"{format_usd(t)}. The unattributed row is the remainder, so the rows always add up; its size is "
            "what carries no company key. Figures are rounded separately."))
        with st.expander("What the unattributed row holds"):
            st.caption("Account = the service family's all-in dollars; Company rows = what the company rows "
                       "above hold of it; Unattributed = the rest after the cloud-services adjustment. The "
                       "Unattributed column sums to the unattributed row.")
            styled_table(out["breakdown"], slug="showback-unattributed", size_note=False,
                         column_config={"CONTENTS": st.column_config.TextColumn("Contents", width="large")})
    else:
        if table.empty:
            gaps = [str(g) for g in out.get("keyed_gaps") or []]
            if company == "UNKNOWN" and gaps:
                # R1-14: an empty UNKNOWN read is verified clear only when every keyed source covers the
                # whole span in full; a missing, stale or partly loaded one leaves it unverified
                named = gaps[0] if len(gaps) == 1 else ", ".join(gaps[:-1]) + " and " + gaps[-1]
                empty_state("no_data_yet", f"Nothing read here is stamped UNKNOWN, but {named} "
                                           f"{'does' if len(gaps) == 1 else 'do'} not cover all of {span} in "
                                           "full (see the notes below), so UNKNOWN cannot be confirmed clear.")
            elif company == "UNKNOWN":
                empty_state("clean", "Nothing in this span is stamped UNKNOWN: every warehouse, serverless, "
                                     "Cortex Code and storage line read here has a company.")
            else:
                empty_state("no_data_yet", f"No warehouse, serverless, Cortex Code or storage spend is "
                                           f"stamped {company} in {span}.")
        else:
            pct = s["company_share_pct"]
            kpi_row([
                {"label": f"{company} all-in, {span}", "value": format_usd(safe_float(s["company_usd"])),
                 "help": "This company's warehouse, serverless, Cortex Code and estimated storage dollars "
                         "over the complete metered days shown: the same row the ALL view shows."},
                {"label": "Share of account spend, before adjustment",
                 "value": f"{pct:.0f}%" if pct is not None else "—",
                 "help": "This company's all-in dollars over the whole account's spend for the same days "
                         "before the cloud-services adjustment (billed metering plus estimated storage, with "
                         "the adjustment added back). The same basis as the ALL view's Company-attributed "
                         "share, where the company rows and the unattributed row add up to 100%."},
            ])
            styled_table(table, slug="company-showback", size_note=False)
            st.caption(_SHOWBACK_TABLE_NOTE)
        st.caption(f"Company scope: {company} only. The other companies, the cloud-services adjustment and "
                   "the unattributed remainder are account-level rows; set Company to ALL to see them and the "
                   "tie-out to the all-in total.")

    n = int(safe_float(s["span_days"]))
    st.caption(f"Covers {span}: {n} complete metered day{'' if n == 1 else 's'} of the selected Window "
               f"({wlab}).")
    for note in out["notes"]:
        st.caption(md_dollars(note))
    st.caption(md_dollars(
        f"Billing basis: billed metering credits (cloud-services adjustment applied) x ${rate:.2f} per "
        f"credit (${ai_rate:.2f} for AI), plus storage estimated at the Admin storage rates. Complete "
        "metered days only: today and the newest, possibly unfinished, daily-metering day never count. "
        "Data transfer, Marketplace and org-currency adjustments are not included; the org rate card on "
        "Contract & Forecast is the invoice. Metering days are UTC while the warehouse and object-cost facts "
        "use Central days, so a few hours at each end of the span can move between a company row and the "
        "unattributed row."))
    result_caption(res)
    methodology_note(md_dollars(
        "How it is computed: every row is cut to the days daily metering has closed in the Window. The "
        "metered side is priced by service family (AI at the AI rate). Company rows use each fact's stored "
        "company (warehouse, object-cost and storage rows keep the company stamped when they loaded; the "
        "loaders re-stamp only recent days), while Cortex Code uses today's user mapping. Storage prices "
        "each day's bytes at 1/(days in that month) of the monthly $/TiB rate. Nothing is allocated by a "
        "share; the unattributed row is the all-in total minus everything with a key. Shares divide by the "
        "spend before the cloud-services adjustment (the company rows plus the unattributed row), the same "
        "basis as the company warehouse dollars. The object-cost and Cortex Code facts reload after the "
        "morning metering load; until they do, a note names the newest day they hold only in part."))
