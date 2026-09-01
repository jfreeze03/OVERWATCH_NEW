"""Cost & Contract — the Chargeback & AI section bodies (department chargeback,
Cortex & storage, AI user attribution).

Formula honesty rules: billed dollars always include the cloud-services
adjustment; warehouse spend is exact; user/database spend is share-allocated
and says so; estimated and verified savings never mix.
"""

from __future__ import annotations

from dataclasses import replace

import streamlit as st

from app import companies
from app.config import MAX_LIVE_WINDOW_DAYS, core_object
from app.core.identity import identity_sql
from app.core.query import execute_statement, run
from app.core.sqlsafe import sql_literal, sql_number
from app.data import chargeback_sql, cortex_sql, cost_sql, mart27_sql, mart_sql
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
    with_aggregate_budget_row,
)
from app.logic.formulas import account_today, credits_to_usd, format_usd, md_dollars, safe_float
from app.ui import charts
from app.ui.components import (
    empty_state,
    export_button,
    guard,
    kpi_row,
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

_PAGE = "Cost & Contract"


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
        kpi_row([{"label": f"Cortex spend, {_win}d", "value": format_usd(float(df["USD"].sum())),
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
            if fn_res.ok and not fn_res.empty:
                fn = fn_res.df.copy()
                fn["USD"] = fn["TOTAL_CREDITS"].map(safe_float) * ai_rate
                charts.daily_stacked_usd(fn, "DAY", "SOURCE", "USD")
                result_caption(fn_res)
            elif fn_res.ok:
                st.caption("No AI Functions usage in this window.")
            else:
                st.caption(f"View not available in this account/role: {fn_res.error}")


def _ai_users_tab(company: str, days: int, ai_rate: float, settings: dict, is_operator: bool, *, bounds: tuple | None = None) -> None:
    """Cortex Code user attribution — ported from the original AI & Cortex
    Monitor. Token credits are exact per user; projections and severities are
    computed in tested logic, and budget severities only exist when an AI
    budget is actually configured."""
    ai_budget = safe_float(settings.get("AI_MONTHLY_BUDGET_USD"))
    # P2: FACT_AI_USAGE_DAILY (V061 loader arm [9]) already holds exactly what
    # the two live CORTEX_CODE_* scans compute — 22s + 15s on EVERY render, 30
    # of this page's 88 slow fetches. Go fact-first; the live scan only runs
    # when the fact cannot cover the asked window (mart27_sql's coverage gate
    # returns zero rows rather than a short answer).
    #
    # Deliberately NOT run_mart_first, for two reasons the helper cannot serve:
    #  1. probe=True. Accounts with no Cortex Code subscription fail with 002139
    #     (see below) — an EXPECTED absence that must not be error-logged and
    #     counted as a failed fetch on every render. run_mart_first has no probe
    #     passthrough.
    #  2. The live leg answers at a DIFFERENT grain on purpose (P9): ONE 365d
    #     user-day-source fetch under a days-independent cache key, from which
    #     BOTH this rollup and the daily-by-source chart are folded in pandas.
    #     One 22s payment per TTL instead of two per window.
    _lm = "_lm" if bounds is not None else ""
    rollup_res = run(mart27_sql.ai_code_user_rollup(days, company, bounds=bounds), page=_PAGE,
                     key=f"cortex_users_{company}_{days}{_lm}", tier="hourly",
                     source="FACT_AI_USAGE_DAILY (Cortex Code, daily loader)")
    live_res = None
    if not rollup_res.usable():
        live_res = run(cortex_sql.cortex_code_user_daily(company), page=_PAGE,
                       key=f"cortex_user_daily_{company}", tier="metadata",
                       source=("ACCOUNT_USAGE.CORTEX_CODE_*_USAGE_HISTORY "
                               "(365d live fallback, window derived in-app)"),
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
        # Keep the live result's source/freshness/error for the caption and the
        # guard; swap in the window-sliced rollup the panel actually renders.
        rollup_res = replace(live_res, df=rollup_from_user_daily(live_res.df, days, bounds=bounds))
    if not guard(rollup_res,
                 "No Cortex Code usage (Snowsight or CLI) recorded in this window for this scope.",
                 setup_hint="If these views are not enabled in this account, this tab stays honest and empty."):
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
    kpi_row([
        {"label": f"Active AI users ({days}d)", "value": f"{summary['active_users']:,}"},
        {"label": "Requests", "value": f"{summary['total_requests']:,}"},
        {"label": "Cortex Code spend", "value": format_usd(summary["spend_usd"]),
         "help": f"Exact token credits x ${ai_rate:.2f}/credit."},
        {"label": "Projected 30d", "value": format_usd(summary["projected_30d_usd"]),
         "help": (f"Run-rate over the {eff_days} day(s) this scope has actually been "
                  f"active, extended to 30 days."
                  + ("" if eff_days >= days else
                     f" Asked window was {days}d — dividing by days that predate the "
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
        with st.expander("Queue top exceptions to the Action Queue"):
            statements = []
            _queued = exceptions.head(10)
            # cost-hunt2: the '(all users)' aggregate row's PROJECTED_30D_USD IS the scope-wide
            # total = the SUM of the per-user projections (aggregate_budget_row reads
            # rollup_summary's projected_30d_usd_guarded). Queuing it at full value alongside the
            # per-user rows double-counts those dollars in any additive rollup -- e.g.
            # mart_sql.action_acceptance DONE_USD and ESTIMATED_OPEN_USD both SUM(ESTIMATED_USD)
            # with no de-overlap, so once both are DONE the breaching users are counted twice.
            # Stamp the aggregate's ESTIMATED_USD as the INCREMENTAL exposure not already itemized
            # by the other queued rows (scope total - sum of other queued projections, clamped
            # >=0) so the queued set sums to the scope total once. DETAIL still shows the true
            # scope total; only the additive ESTIMATED_USD field is de-overlapped. (A user
            # appearing as BOTH a budget breach and a CPR spike is a separate, smaller overlap.)
            _other_proj = sum(safe_float(_r['PROJECTED_30D_USD'])
                              for _, _r in _queued.iterrows()
                              if str(_r['USER_NAME']) != "(all users)")
            for _, r in _queued.iterrows():
                user = str(r['USER_NAME'])
                _is_agg = user == "(all users)"
                title = f"Cortex {r['SIGNAL']}: {user} ({r['SOURCE']})"
                _proj = safe_float(r['PROJECTED_30D_USD'])
                _est = max(0.0, _proj - _other_proj) if _is_agg else _proj
                detail = (f"{int(r['TOTAL_REQUESTS'])} requests, projected 30d "
                          f"{format_usd(_proj)}, cr/request {r['CREDITS_PER_REQUEST']:.4f}."
                          + (" Queued $ is the incremental scope exposure beyond the per-user "
                             "rows above (avoids double-count)." if _is_agg else ""))
                # Attribute a PER-USER breach to the user's REAL company (COMPANY_FOR_USER), not the
                # view's filter: in the ALL view `company` is 'ALL', which would both mis-file a
                # Trexis user's action under 'ALL' AND -- because the dedup keys on COMPANY -- let
                # the SAME breach queue a SECOND time from the Trexis scope (double-counting its
                # ESTIMATED_USD on the Workbench KPI). The scope-aggregate '(all users)' row IS the
                # per-scope total, so it stays under the view's company (distinct per scope).
                # codex#39: idempotent insert keyed on COMPANY + TITLE (TITLE encodes signal+user+
                # source) + this month + open status, so a double-click/retry is a no-op.
                # DS #7: ESTIMATED_USD here is PROJECTED_30D_USD -- a 30-day (monthly-equivalent)
                # figure -- so stamp PERIOD='MONTHLY' (else it summed beside one-time estimates).
                company_expr = (sql_literal(company) if _is_agg
                                else f"{companies.COMPANY_FOR_USER_FN}({sql_literal(user)})")
                statements.append(
                    f"INSERT INTO {core_object('ACTION_QUEUE')} (COMPANY, SEVERITY, TITLE, DETAIL, OWNER, SOURCE, ESTIMATED_USD, PERIOD)\n"
                    f"SELECT {company_expr}, {sql_literal(str(r['SEVERITY']).upper())}, {sql_literal(title)}, "
                    f"{sql_literal(detail)}, 'DBA / AI Governance', 'Cost & Contract > Chargeback & AI > AI users', "
                    f"{sql_number(_est)}, 'MONTHLY'\n"
                    f"WHERE NOT EXISTS (SELECT 1 FROM {core_object('ACTION_QUEUE')} q "
                    f"WHERE q.COMPANY = {company_expr} AND q.TITLE = {sql_literal(title)} "
                    f"AND UPPER(q.STATUS) IN ('OPEN', 'IN_PROGRESS') "
                    f"AND q.CREATED_AT >= DATE_TRUNC('month', CURRENT_DATE()));"
                )
            script = "\n".join(statements)
            st.code(script, language="sql")
            if (is_operator and st.button("Execute inserts", key="cortex_queue_exec")
                    and write_gate_open("cortex_queue_exec")):
                ok_all, count = True, 0
                for stmt in statements:
                    ok, _msg = execute_statement(stmt.replace("\n", " "), page=_PAGE)
                    ok_all, count = ok_all and ok, count + int(ok)
                stamp_write("cortex_queue_exec", ok_all)  # C48
                (st.success if ok_all else st.error)(f"{count}/{len(statements)} action(s) queued.")
            elif not is_operator:
                st.caption("Copy and run as SNOW_ACCOUNTADMINS / SNOW_SYSADMINS - in-app execution needs an admin profile.")

    _coco_cap = safe_float(settings.get("COCO_DAILY_CAP_CREDITS"), 15.0)
    _token_economics_panel(company, days, _coco_cap if _coco_cap > 0 else 15.0, bounds=bounds)


def _token_economics_panel(company: str, days: int, cap_credits: float, *, bounds: tuple | None = None) -> None:
    """CoCo efficiency review (repo review wave 2: TOKENS_GRANULAR). Cache-hit alone can't separate
    a heavy-but-targeted user from a high-intensity one, so this merges the token grain with
    per-user daily credits into peer-relative signals + a 🚩 Review flag. Tracks the page's Window
    filter (``days``). Opt-in toggle; a schema/telemetry miss degrades to an honest note."""
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
    _lm = "_lm" if bounds is not None else ""
    te_res = run(cortex_sql.cortex_code_token_types(days, bounds=bounds), page=_PAGE, key=f"cortex_token_types_{days}{_lm}",
                 tier="historical", source="CORTEX_CODE_*_USAGE_HISTORY (TOKENS_GRANULAR)",
                 probe=True)
    if not te_res.ok:
        st.caption("TOKENS_GRANULAR isn't available on this account's Cortex Code views yet — "
                   "token-type economics appear here automatically once the column exists.")
        return
    econ = token_economics(te_res.df)
    if econ.empty:
        empty_state("no_data_yet", "No token-type rows in the selected window.")
        return
    # Per-user daily credits drive the credit / session / over-cap signals. Same days-independent
    # cache key as _ai_users_tab's live leg, so this reuses that fetch when it ran.
    ud_res = run(cortex_sql.cortex_code_user_daily(company), page=_PAGE,
                 key=f"cortex_user_daily_{company}", tier="metadata",
                 source="ACCOUNT_USAGE.CORTEX_CODE_*_USAGE_HISTORY (daily, window derived in-app)",
                 probe=True, max_rows=200_000)
    # econ (token grain) is ACCOUNT-WIDE — cortex_code_token_types has no company clause — so the
    # panel only becomes company-scoped through the (scoped) daily credit set. scoped=True makes
    # coco_efficiency REFUSE the account-wide fallback for a company view, so it never lists other
    # companies' users — whether the daily scan didn't resolve OR the company's credits fall
    # outside the selected window (emptying the rollup after the cut). The ALL view (no clause)
    # keeps its correct account-wide fallback.
    _scoped = bool(companies.user_clause(company, "USER_NAME"))
    eff = coco_efficiency(econ, ud_res.df if ud_res.usable() else None,
                          cap_credits=cap_credits, window_days=days, as_of=account_today(),
                          scoped=_scoped)
    if _scoped and eff.empty:
        if not ud_res.usable():
            st.caption("Credit / session / over-cap signals need the Cortex Code daily usage scan, "
                       "which didn't resolve this run. The per-user token grain is account-wide and "
                       "can't be attributed to this company without it, so it's hidden here.")
        else:
            st.caption(f"No Cortex Code credit usage for this company in the last {days} days — "
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
        {"label": "Fleet cache-hit", "value": (f"{_fleet:.1f}%" if _has_cache else "n/a"),
         "help": "cache_read / (cache_read + input). See the caption — where it is high and "
                 "uniform, caching is not the lever; the review flag is."},
        {"label": "Users measured", "value": f"{len(eff):,}"},
    ])
    if not ud_res.usable():
        st.caption("Credit / session / over-cap signals need the Cortex Code daily usage scan, "
                   "which didn't resolve this run — showing cache-grain behaviour only.")
    _cols = ["USER_NAME", "FLAG", "TOTAL_CREDITS", "PEER_MULT", "AVG_DAILY_CR", "DAYS_OVER_CAP",
             "ACTIVE_DAYS", "CR_PER_REQ", "CACHE_WRITE_PCT", "READ_AMP", "CACHE_HIT_PCT", "REASON"]
    styled_table(with_user_names(eff[_cols], _PAGE), height=340, column_config={
        "FLAG": st.column_config.TextColumn("Flag"),
        "TOTAL_CREDITS": st.column_config.NumberColumn(f"Credits ({days}d)", format="%.1f"),
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
        f"confirm against delivered work before acting. {_cache_note} Peer-relative, {days}d.")
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
                        source="WAREHOUSE_METERING_HISTORY (calendar month)")
        if not month_res.usable():
            st.error(month_res.error or "No credits recorded for that month/scope.")
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

def _chargeback_tab(company: str, days: int, rate: float, is_operator: bool, *, bounds: tuple | None = None) -> None:
    """Department chargeback: warehouse = exact usage (idle + unadjusted CS), role = allocated usage lens."""
    _lm = "_lm" if bounds is not None else ""
    dept_res = run(chargeback_sql.department_window_credits(days, company, bounds=bounds), page=_PAGE,
                   key=f"cb_dept_{company}_{days}{_lm}", tier="historical",
                   source="WAREHOUSE_METERING_HISTORY x DEPARTMENT_MAP")
    if not guard(dept_res, "No warehouse credits in this window.",
                 setup_hint="Not installed yet — an admin can verify on Admin → Migrations & freshness. Seed department names in DEPARTMENT_MAP."):
        return
    df = dept_res.df.copy()
    df["USD"] = df["CREDITS_TOTAL"].map(lambda c: credits_to_usd(c, rate))
    dept = df.groupby("DEPARTMENT", as_index=False)["USD"].sum().sort_values("USD", ascending=False)
    unmapped_usd = float(dept[dept["DEPARTMENT"] == "Unmapped"]["USD"].sum())
    total_usd = float(dept["USD"].sum())

    kpi_row([
        {"label": f"Chargeback total ({days}d)", "value": format_usd(total_usd),
         "help": "Exact WAREHOUSE-COMPUTE metering x rate — includes each warehouse's "
                 "cloud-services credits, unadjusted (the account-level rebate lives "
                 "on Cost & Contract → Spend & Attribution). Reconciles to the scoped "
                 "warehouse spend by construction; storage, serverless, AI, and transfer "
                 "are not allocated here."},
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
        live_source="QUERY_HISTORY (elapsed share per warehouse, live fallback)")
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
        if "QUERY_HISTORY" in str(share_res.source) and days > MAX_LIVE_WINDOW_DAYS:
            _pr = run(chargeback_sql.department_window_credits(MAX_LIVE_WINDOW_DAYS, company),
                      page=_PAGE, key=f"cb_dept_{company}_{MAX_LIVE_WINDOW_DAYS}", tier="historical",
                      source="WAREHOUSE_METERING_HISTORY x DEPARTMENT_MAP (share-matched window)")
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

    st.markdown("**Department budgets & pace**")
    panel_help(
        "Budgets live in DEPT_BUDGETS; the hourly scan raises COST_DEPT_BUDGET_PACE when a "
        "department runs ahead of pace (threshold on the Alerts page). Spend is the "
        "department's warehouses — exact billing, same as the table above."
    )
    bud = run(mart_sql.dept_budgets(), page=_PAGE, key="dept_budgets", tier="live",
              source="DEPT_BUDGETS")
    if bud.ok and not bud.empty:
        styled_table(with_user_names(bud.df, _PAGE, user_col="UPDATED_BY", display_col="Updated by"))
    elif bud.ok:
        empty_state("needs_setup", "No department budgets set yet — add one below and the pace alert goes live.")
    if is_operator:
        dmap = run(chargeback_sql.department_map(), page=_PAGE, key="cb_dmap_bud", tier="recent",
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
        map_res = run(chargeback_sql.department_map(), page=_PAGE, key="cb_map", tier="recent",
                      source="DEPARTMENT_MAP")
        if map_res.usable():
            styled_table(with_user_names(map_res.df, _PAGE, user_col="UPDATED_BY", display_col="Updated by"),
                         height=280)
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
        if (is_operator and name and department and st.button("Execute mapping", key="cb_map_exec")
                and write_gate_open(f"cb_map_exec:{name}")):
            ok, msg = execute_statement(merge_sql.replace("\n", " "), page=_PAGE)
            stamp_write(f"cb_map_exec:{name}", ok)  # C48
            notify(ok, msg)
        elif not is_operator:
            st.caption("Copy and run as SNOW_ACCOUNTADMINS / SNOW_SYSADMINS - in-app execution needs an admin profile.")
