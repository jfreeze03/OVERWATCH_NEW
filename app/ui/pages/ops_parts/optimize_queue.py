"""Operations > Optimize: the recurring-query fix queue (v4.597, Option C).

The old Decision Studio Portfolio rebuilt as a work queue: every measured query family with its
observed mart dollars, ONE specific diagnosis and a first fix, and a one-click Track into Action
Center (an idempotent ACTION_QUEUE insert keyed on the fingerprint). No live read on first paint:
the queue, the tracked set and the watchlist come from app/mart tables in one mixed-tier round
trip. The optional live query profile reuses the Operations > Queries scan byte-for-byte, so the
two sections share one cache entry.

Write safety: a row click only selects (master_detail binds by identity); Track lives in the
detail pane behind an operator gate + the C48 latch, and Track all ACT NOW is one capped,
idempotent statement with its SQL shown first.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.core.identity import identity_sql, viewer_name
from app.core.query import execute_statement, run, run_batch_mixed
from app.core.state import navigation_context, request_navigation
from app.data import ops_sql, workbench_sql
from app.logic import query_opt
from app.logic.decision import prioritize_workloads
from app.logic.fix_queue import (
    TRACK_ALL_CAP,
    TRACK_COOLDOWN_DAYS,
    diagnose_workloads,
    own_traffic,
    track_all_eligible,
    track_fingerprints_sql,
    track_items,
    with_track_status,
)
from app.logic.formulas import format_usd, md_dollars, safe_float
from app.logic.workbench import mark_watched
from app.ui import charts
from app.ui.components import (
    alarm_health,
    decision_rows,
    empty_state,
    exception_summary,
    guard,
    kpi_row,
    master_detail,
    notify,
    read_model_caption,
    result_caption,
    section_header,
    stamp_write,
    stash_section_count,
    styled_table,
    write_gate_open,
)

_PAGE = "Operations"
_QUEUE_CAP = 200
_QUEUE_SOURCE = "MART_PATTERN_COST_DAILY + MART_QUERY_FAMILY_DAILY (advisor columns)"
_SOURCE_LABEL = {
    "live": "the live query profile",
    "mart": "the daily marts (day-grain averages; coarser than the live profile)",
    "heuristic": "the portfolio heuristic",
    "none": "nothing specific yet",
}
_OPEN_STATUS = "Tracked (open)"


def _can_open(page: str) -> bool:
    """Does this viewer's profile offer ``page``? Fail-open on an unreadable profile, like the
    since-last-visit opener (the navigation clamp still holds)."""
    try:
        from app.config import PAGES_BY_PROFILE
        from app.core.session import active_profile, current_role
        allowed = PAGES_BY_PROFILE.get(active_profile(current_role()), ())
    except Exception:  # noqa: BLE001 - a cross-link gate is chrome, never break the page
        return True
    return not allowed or page in allowed


def _open_entity(fingerprint: str) -> None:
    request_navigation("Control Room", "Entity 360",
                       context={"entity_type": "QUERY_FINGERPRINT", "entity_key": fingerprint})


def _live_profile(company: str, days: int, wh_filter: str, user_filter: str, database: str,
                  schema_contains: str, bounds: tuple | None):
    """The Operations > Queries live optimization profile, called with the SAME builder
    arguments, key and tier as that board so the two share one cache entry (a run() caches on
    the SQL text + tier, not the key). Returns (result, scored, breakdowns); the scored frame is
    None when the read failed, so the diagnosis never claims the live profile saw nothing."""
    _lm = "_lm" if bounds is not None else ""
    res = run(
        ops_sql.query_opportunity_fingerprints(
            days, company, wh_filter, user_filter, database, schema_contains, bounds=bounds),
        page=_PAGE, key=f"q_opp_{company}_{days}{_lm}", tier="historical",
        source="QUERY_HISTORY (live optimization profile)")
    if not res.ok:
        return res, None, None
    scored, breakdowns = query_opt.score_opportunities(res.df if not res.empty else None)
    return res, scored, breakdowns


def render_optimize(company: str, days: int, rate: float, *, bounds: tuple | None,
                    is_operator: bool, wh_filter: str = "", user_filter: str = "",
                    database: str = "", schema_contains: str = "") -> None:
    """Operations > Optimize: measured query families as a diagnosed, trackable fix queue."""
    _lm = "_lm" if bounds is not None else ""
    _viewer = viewer_name()
    _q_key = f"ops_opt_queue_{company}_{days}{_lm}"
    _q_sql = workbench_sql.optimize_queue(days, company, _QUEUE_CAP, bounds=bounds)
    _t_sql = workbench_sql.tracked_actions()
    specs = [
        {"key": _q_key, "sql": _q_sql, "tier": "historical", "source": _QUEUE_SOURCE},
        {"key": "ops_opt_tracked", "sql": _t_sql, "tier": "recent",
         "source": "ACTION_QUEUE (tracked query families)"},
    ]
    if _viewer:
        specs.append({"key": "ops_opt_watchlist", "sql": workbench_sql.watchlist(_viewer),
                      "tier": "recent", "source": "USER_WATCHLIST"})
    _pf = run_batch_mixed(specs, page=_PAGE)   # contract: always a dict with every key present
    result = _pf.get(_q_key) or run(_q_sql, page=_PAGE, key=_q_key, tier="historical",
                                    source=_QUEUE_SOURCE)
    _tr = _pf.get("ops_opt_tracked") or run(_t_sql, page=_PAGE, key="ops_opt_tracked",
                                            tier="recent", probe=True,
                                            source="ACTION_QUEUE (tracked query families)")
    _wl_res = ((_pf.get("ops_opt_watchlist") or run(workbench_sql.watchlist(_viewer), page=_PAGE,
                                                     key="ops_opt_watchlist", tier="recent",
                                                     source="USER_WATCHLIST"))
               if _viewer else None)
    if not guard(result, "No measured recurring-query cost exists in this scope."):
        return
    # W12: divide by the window's real day SPAN. Current month / Current year resolve `days` to a
    # day OFFSET (Aug 3 MTD = 2), so dividing by it overstated the 30-day normalization.
    span = (bounds[1] - bounds[0]).days if bounds is not None else days
    portfolio = prioritize_workloads(result.df, rate, span)

    # The live profile is opt-in (off first paint): its state is read here so the diagnoses below
    # can use it; the toggle itself renders under the section header.
    _live_on = bool(st.session_state.get("ops_opt_live", False))
    _live_res = _live_scored = _live_bd = None
    if _live_on:
        _live_res, _live_scored, _live_bd = _live_profile(
            company, days, wh_filter, user_filter, database, schema_contains, bounds)
    portfolio = diagnose_workloads(portfolio, live_scored=_live_scored, live_breakdowns=_live_bd)
    _tracked_df = _tr.df if _tr.usable() else None
    portfolio = with_track_status(portfolio, _tracked_df)
    # DS #1 carried over: a watched family is flagged and pinned to the top WITHIN its lane (an
    # ACT NOW item is never buried under a watched PLAN item). No pin when the read is unavailable.
    _wl = _wl_res.df if (_wl_res is not None and _wl_res.usable()) else None
    portfolio["WATCHED"] = mark_watched(portfolio, _wl, "QUERY_FINGERPRINT", "FINGERPRINT")
    if bool(portfolio["WATCHED"].any()):
        _lane_rank = portfolio["LANE"].map({"ACT NOW": 0, "PLAN": 1, "VALIDATE": 2}).fillna(3)
        portfolio = (portfolio.assign(_LR=_lane_rank)
                     .sort_values(["_LR", "WATCHED", "PRIORITY_SCORE"],
                                  ascending=[True, False, False])
                     .drop(columns="_LR").reset_index(drop=True))

    specific = portfolio["SPECIFIC"].astype(bool)
    untracked = ~portfolio["TRACK_STATUS"].isin([_OPEN_STATUS, "Dismissed"])
    act_now = portfolio[portfolio["LANE"].eq("ACT NOW") & specific & untracked]
    failure_risk = portfolio[portfolio["FAIL_PCT"].ge(2)]
    # Count the VALIDATE LANE itself (prioritize_workloads also forces blind, no-behaviour families
    # to VALIDATE whatever their confidence), not a re-derived confidence < 0.5 filter.
    validate = portfolio[portfolio["LANE"].eq("VALIDATE")]
    section_header("Fix queue", alarm_health(len(act_now)), "optimize", anchor="ops-optimize")
    st.toggle(
        "Add the live query profile (one query-history scan; shared with Operations ▸ Queries)",
        key="ops_opt_live",
        help="Scores each recurring query from live query history, which sees what the daily marts "
             "cannot (spill, partition pruning, queueing, rows returned), and uses that diagnosis "
             "where it finds one. It is the same scan as the Queries board's optimization toggle, so "
             "whichever runs first pays for both. It covers at most the last 90 days and honors the "
             "Warehouse, User, Database and Schema filters. Off on first paint.")
    if _live_res is not None:
        if not _live_res.ok:
            empty_state("unavailable", "The live query profile could not be read; the diagnoses "
                        "below use the daily marts only.", detail=_live_res.error)
        else:
            _n_live = int(portfolio["DIAG_SOURCE"].eq("live").sum())
            st.caption(f"Live profile: {len(_live_scored) if _live_scored is not None else 0:,} "
                       f"recurring queries scored; {_n_live:,} of the families below are diagnosed "
                       "from it.")
            result_caption(_live_res, note="live profile: diagnoses only, the dollars stay the marts'")
    read_model_caption("workload_portfolio")
    _scope_total = (int(safe_float(result.df["SCOPE_FAMILIES_TOTAL"].iloc[0]))
                    if "SCOPE_FAMILIES_TOTAL" in result.df.columns else len(portfolio))
    # #15 carried over: the queue caps at the top _QUEUE_CAP families by measured credits; the
    # app's truncation banner only fires at the 5000-row fetch cap, so disclose this smaller cap.
    if len(portfolio) >= _QUEUE_CAP:
        st.caption(
            f"Showing the top {_QUEUE_CAP} of {max(_scope_total, len(portfolio)):,} "
            "query families by measured credits — narrow the Window or Company to surface the rest.")

    exceptions = []
    if not act_now.empty:
        exceptions.append({
            "label": "Act now",
            "value": f"{len(act_now):,}",
            "detail": f"{format_usd(act_now['IMPACT_USD_30D'].sum())} observed 30-day cost with a "
                      "specific diagnosis, not yet tracked in Action Center or dismissed.",
            "severity": "warn",
        })
    if not failure_risk.empty:
        exceptions.append({
            "label": "Failure risk",
            "value": f"{len(failure_risk):,}",
            "detail": "Families at or above a 2% observed failure rate.",
            "severity": "bad",
        })
    if not validate.empty:
        exceptions.append({
            "label": "Needs validation",
            "value": f"{len(validate):,}",
            "detail": "Evidence confidence is below the action threshold, or behaviour evidence is missing.",
            "severity": "warn",
        })
    exception_summary(
        exceptions,
        "No untracked immediate-action, elevated-failure, or low-confidence query families.")
    # Headline cost is the UNCAPPED scope total (a SQL window total over every family in scope),
    # never a sum over the top-_QUEUE_CAP frame.
    _scope_credits = (safe_float(result.df["SCOPE_CREDITS_TOTAL"].iloc[0])
                      if "SCOPE_CREDITS_TOTAL" in result.df.columns
                      else safe_float(pd.to_numeric(portfolio["CREDITS"], errors="coerce").sum()))
    _observed_30d = _scope_credits * max(safe_float(rate), 0.0) / max(1, int(span or 1)) * 30
    _n_specific = int(specific.sum())
    _n_open = int(portfolio["TRACK_STATUS"].eq(_OPEN_STATUS).sum())
    kpi_row([
        {"label": "Measured families", "value": f"{max(_scope_total, len(portfolio)):,}",
         "help": f"Recurring query families with measured cost in this scope; the queue lists the top "
                 f"{_QUEUE_CAP} by credits."},
        {"label": "Observed cost (30d)", "value": format_usd(_observed_30d),
         "help": "Measured pattern credits for every family in scope x the compute rate, normalized "
                 "to 30 days; observed cost, not promised savings."},
        {"label": "Specific diagnosis", "value": f"{_n_specific:,} of {len(portfolio):,}",
         "help": "Listed families with a named diagnosis and first fix (live profile, daily-mart "
                 "advisor, or a specific portfolio heuristic). The rest need the live profile."},
        {"label": "Tracked (open)", "value": f"{_n_open:,}",
         "help": "Listed families with an OPEN or IN_PROGRESS Action Center item."},
        {"label": "Evidence coverage",
         "value": (f"{portfolio['EVIDENCE_COVERAGE'].mean() * 100:,.0f}%" if len(portfolio) else "—"),
         "severity": (("ok" if portfolio["EVIDENCE_COVERAGE"].mean() >= 0.8 else "warn")
                      if len(portfolio) else ""),
         "help": "Average share of the three evidence signals (cache, latency, fail-rate) present per "
                 "family. Low coverage means more calls rest on partial evidence — the per-row "
                 "EVIDENCE_COVERAGE column shows which."},
        {"label": "Watching", "value": f"{int(portfolio['WATCHED'].sum()):,}",
         "help": "Query families on your personal watchlist — pinned to the top of their lane. "
                 "Watch or unwatch a family from its Entity 360."},
    ])

    # Track all ACT NOW: one capped, idempotent statement, its SQL shown before the button.
    eligible = track_all_eligible(portfolio, _tracked_df)
    _n_elig = len(eligible)
    _bulk_sql = (track_fingerprints_sql(track_items(eligible, company), actor_sql=identity_sql(),
                                        bulk=True) if _n_elig else "")
    _own_act = int((portfolio["LANE"].eq("ACT NOW") & specific & own_traffic(portfolio)).sum())
    if is_operator:
        if _bulk_sql:
            with st.expander("Statement"):
                st.code(_bulk_sql, language="sql")
        if (is_operator and st.button(f"Track all ACT NOW ({_n_elig})", key="opt_track_all_btn",
                                      disabled=not _n_elig)
                and write_gate_open("opt_track_all")):
            ok, msg = execute_statement(_bulk_sql.strip(), page=_PAGE)
            stamp_write("opt_track_all", ok)  # C48
            notify(ok, (f"Track all ran for {_n_elig} famil{'y' if _n_elig == 1 else 'ies'} — "
                        "idempotent, so any already open in Action Center were skipped, not "
                        "re-queued.") if ok else msg)
            if ok:
                st.rerun()
    else:
        st.caption("Read-only — an operator can track these into Action Center.")
    st.caption(
        f"Track all takes ACT NOW families with a specific diagnosis that have no open Action Center "
        f"item and were not dismissed in the last {TRACK_COOLDOWN_DAYS} days, highest priority first, "
        f"at most {TRACK_ALL_CAP} per click"
        + (f"; {_own_act} OVERWATCH own-traffic famil{'y' if _own_act == 1 else 'ies'} skipped"
           if _own_act else "")
        + ". Items land UNASSIGNED at MEDIUM severity (never HIGH), unpriced unless the diagnosis is "
          "Stabilize failures.")

    _ctx_fp = str(navigation_context().get("fingerprint") or "").strip()
    _preselect = _ctx_fp if _ctx_fp in set(portfolio["FINGERPRINT"].astype(str)) else ""
    if _preselect:   # deliver a deep-link fingerprint ONCE per arrival (the Action Center idiom)
        _nav = st.session_state.get("_ow_nav_context")
        if isinstance(_nav, dict) and _nav.get("fingerprint"):
            st.session_state["_ow_nav_context"] = {k: v for k, v in _nav.items() if k != "fingerprint"}

    def _list(display_df, list_key):
        return decision_rows(
            display_df, key=list_key,
            decision_col="DIAGNOSIS", why_col="EVIDENCE", impact_col="IMPACT_USD_30D",
            confidence_col="DIAG_CONFIDENCE", status_col="TRACK_STATUS",
            context_cols=("LANE", "WATCHED", "RUNS", "FAIL_PCT", "AVG_CACHE_PCT", "P95_SEC",
                          "EVIDENCE_COVERAGE"),
            height=370, sort_label="decision lane, then evidence-weighted priority",
            impact_help="Measured pattern credits x the compute rate, normalized to 30 days — "
                        "observed cost, not promised savings.",
            confidence_label="Confidence (evidence)",
            confidence_help="Diagnosis confidence (0-1): the live profile's confidence when it "
                            "named the diagnosis, else the family's evidence heuristic (run recency, "
                            "active-day coverage, measured cost). NOT statistical confidence.")

    def _detail(row) -> None:
        _render_detail(row, company=company, is_operator=is_operator, live_on=_live_on)

    master_detail(
        portfolio, key="ops_optimize", id_col="FINGERPRINT",
        list_render_fn=_list, detail_render_fn=_detail, preselect_id=_preselect,
        empty_detail_msg="Select a query family on the left to see its diagnosis, first fix and Track.")

    with st.expander("Portfolio map"):
        charts.workload_portfolio(portfolio[[c for c in (
            "FINGERPRINT", "LANE", "IMPACT_USD_30D", "CONFIDENCE", "BLAST_RADIUS", "PRIORITY_SCORE",
            "NEXT_MOVE") if c in portfolio.columns]])
    # Trust: which numbers are measured and which are heuristics, with the exact lane rule.
    st.caption(
        "Impact $, runs, fail % and cache are **measured**; confidence, priority and lane are "
        "**evidence-weighted heuristics** for ordering, not guarantees, and Impact is observed cost, "
        "not promised savings. Lane rule: ACT NOW = top-20% priority AND confidence ≥ 0.65; "
        "VALIDATE = confidence < 0.5; otherwise PLAN. A family with no measured cache/latency/failure "
        "evidence is held at VALIDATE — a blank cell is missing data, not a measured zero. Diagnoses "
        "from the daily marts use day-grain averages and are coarser than the live profile. "
        "Own-traffic families are tagged and never bulk-tracked. WATCHED families are pinned to the "
        "top of their lane.")
    result_caption(result, note="credits are measured; diagnoses are advisory")
    stash_section_count(_PAGE, "Optimize", len(act_now), dims=("company", "days"))


def _render_detail(row, *, company: str, is_operator: bool, live_on: bool) -> None:
    """The selected family: diagnosis, first fix, breakdown, facts, SQL, tracking and Track."""
    fp = str(row.get("FINGERPRINT") or "")
    source = str(row.get("DIAG_SOURCE") or "none")
    conf = safe_float(row.get("DIAG_CONFIDENCE"), default=float("nan"))
    st.markdown(md_dollars(
        f"**{row.get('DIAGNOSIS') or 'Profile it'}** · from {_SOURCE_LABEL.get(source, source)}"
        + (f" · confidence {conf:.2f}" if conf == conf else "")))
    st.markdown(md_dollars(f"**First fix:** {row.get('FIRST_FIX') or '—'}"))
    breakdown = row.get("BREAKDOWN")
    if isinstance(breakdown, list) and breakdown:
        styled_table(
            pd.DataFrame(breakdown, columns=["Signal", "Points", "Fix"]), size_note=False,
            column_config={"Points": st.column_config.NumberColumn("Points", format="+%d")})
        st.caption("Additive, per-driver-capped findings; the top row is the first fix.")
    impact = safe_float(row.get("IMPACT_USD_30D"))
    priced = safe_float(row.get("PRICED_USD_MO"), default=float("nan"))
    st.caption(md_dollars(
        f"Observed cost {format_usd(impact)}/mo (measured, not promised savings) · "
        + (f"priced {format_usd(priced)}/mo (the failed-run share of that cost)" if priced == priced
           else "unpriced (only a failure diagnosis is priced)")
        + f" · {row.get('EVIDENCE') or '—'}"))
    preview = row.get("QUERY_PREVIEW")
    st.code(preview if isinstance(preview, str) and preview else "—", language="sql")
    status = str(row.get("TRACK_STATUS") or "Untracked")
    action_id = str(row.get("TRACKED_ACTION_ID") or "")
    st.caption(f"Action Center: {status}.")
    # Cross-page doorways only for a viewer whose profile offers Control Room (the pane already
    # sits in master_detail's column, so the two links stack rather than nest another column row).
    _cr_ok = _can_open("Control Room")
    if action_id and _cr_ok and st.button("Open in Action Center →", key=f"opt_open_ac:{fp[:16]}",
                                          type="tertiary"):
        request_navigation("Control Room", "Action Center", context={"action_id": action_id})
    if fp and _cr_ok and st.button("Open Entity 360 →", key=f"opt_open_360:{fp[:16]}",
                                   type="tertiary"):
        _open_entity(fp)
    if not fp:
        return
    if status == _OPEN_STATUS:
        st.caption("Already tracked: an open Action Center item exists for this family.")
        return
    if not is_operator:
        st.caption("Read-only — an operator can track this family into Action Center.")
        return
    _sql = track_fingerprints_sql(track_items([row.to_dict()], company), actor_sql=identity_sql(),
                                  bulk=False)
    with st.expander("Statement"):
        st.code(_sql, language="sql")
    if not live_on and source == "none":
        st.caption("No specific diagnosis yet — turn on the live query profile above before tracking.")
    if (is_operator and st.button("Track", key=f"opt_track_btn:{fp[:16]}")
            and write_gate_open(f"opt_track:{fp[:16]}")):
        ok, msg = execute_statement(_sql.strip(), page=_PAGE)
        stamp_write(f"opt_track:{fp[:16]}", ok)  # C48
        notify(ok, "Tracked in Action Center (idempotent: an item already open for this family is "
                   "left as is)." if ok else msg)
        if ok:
            st.rerun()
