"""Morning brief — one phone-friendly scroll: the numbers, the fires, the asks.

Deliberately tiny: five figures, open criticals, top three actions, a spend
sparkline. Everything links into the full pages for depth.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.core.errors import safe_page
from app.core.identity import viewer_name
from app.core.query import run, run_batch
from app.core.state import filters, request_navigation
from app.data import etl_control_sql, mart_sql
from app.logic import case_file
from app.logic.actions import rank_actions
from app.logic.formulas import (
    ExecutiveSummaryView,
    account_now,
    blended_billed_usd,
    contract_runway,
    daily_spend_last_n,
    executive_slide_bullets,
    executive_summary_csv,
    executive_summary_html,
    format_usd,
    humanize_duration,
    md_dollars,
    safe_float,
)
from app.logic.insights import etl_cycle_sla_forecast
from app.logic.verdict import Signal, oldest_open_hours, page_verdict
from app.ui import charts
from app.ui.components import (
    alarm_health,
    contract_runway_bar,
    daily_spend_wide,
    download_text_button,
    empty_state,
    export_button,
    kpi_row,
    load_settings,
    methodology_note,
    page_header,
    page_verdict_line,
    panel_help,
    section_filter_contract,
    section_header,
    selectable_table,
)
from app.ui.sizing import TABLE_H_SM
from app.ui.workbench import render_watch_badge

_PAGE = "Brief"


def _stalest_label(vals: dict) -> str:
    """Stalest-telemetry badge text: source name + age, or 'name never loaded'.

    The strip's STALEST_SOURCE_H is -1 exactly when the worst source has no data
    at all (C3); STALEST_SOURCE_NAME says which one (N15)."""
    name = str(vals.get("STALEST_SOURCE_NAME", "") or "")
    src = f"{name} " if name and name != "none" else ""
    # The -1 sentinel arrives from the mart as the scale-1 string "-1.0"
    # (TO_VARCHAR of a NUMBER(.,1)), so compare NUMERICALLY — a literal "-1" match
    # never fired, leaving a never-loaded source rendering "…-1h" instead of "never
    # loaded". A real age is always >= 0; anything below (or unparseable) is the
    # never-loaded sentinel.
    _h = safe_float(vals.get("STALEST_SOURCE_H"), default=float("nan"))
    if not (_h >= 0):
        return f"{src}never loaded" if src else "no data yet"
    return f"{src}{humanize_duration(_h, 'h')}"


def _reference_gap_summary(settings: dict) -> tuple[int, str]:
    """Source codes with no XLAT translation across every configured check.

    Reuses the Operations ▸ Pipeline reference-gap scan (etl_control_sql), run
    account-wide — pinned checks plus every configured check, no Database filter,
    the widest morning read. Config-gated and FAIL-SILENT: unset config, no valid
    check, or a missing SELECT grant (a probe read → the 'absent' branch, so it is
    neither error-logged nor counted as a failed fetch) all return (0, "") so the
    Brief never shows a setup or grant hint — the Operations panel owns that.
    Returns (code_count, label) where label names the affected check type(s),
    e.g. 'pc_uwissuetype.code'."""
    xlat = str(settings.get("ETL_REF_GAP_XLAT") or "").strip()
    raw = str(settings.get("ETL_REF_GAP_CHECKS") or "").strip()
    if not xlat or not raw:
        return 0, ""
    checks, _ = etl_control_sql.parse_ref_gap_checks(raw)
    scan_sql, _ = etl_control_sql.reference_gap_scan(checks, xlat)
    if not scan_sql:
        return 0, ""
    res = run(scan_sql, page=_PAGE, key="brief_ref_gaps", tier="recent",
              source="staging tables MINUS XLAT reference",
              max_rows=etl_control_sql.MAX_CODES, probe=True)
    if not (res.ok and not res.empty) or "CHECK_NAME" not in res.df.columns:
        return 0, ""
    types = list(dict.fromkeys(res.df["CHECK_NAME"].astype(str)))
    label = ", ".join(types) if len(types) <= 2 else f"{types[0]}, {types[1]} +{len(types) - 2} more"
    return len(res.df), label


def _workflow_failure_summary(settings: dict) -> tuple[int, str]:
    """Count FAILED tasks in the latest Informatica ETL run (CONTROL_STATUS).

    A failed nightly task usually breaks a downstream load, so it belongs on the
    morning read like a fire. Reuses the Operations workflow-runtimes scan (the same
    ``(sql, scope)`` cache entry — no extra query) and the shared FAILED_TASK_STATUSES
    set, so the Brief and the panel never disagree on what counts as a failure. Only
    real FAILED states count — a RUNNING task is not a failure. Config-gated and
    FAIL-SILENT (a probe read → unset config or a missing grant returns (0, "")).
    Returns (failed_count, workflow_label)."""
    fqn = str(settings.get("ETL_CONTROL_STATUS_FQN") or "").strip()
    if not fqn:
        return 0, ""
    scan_sql = etl_control_sql.workflow_runtimes_scan(fqn)
    if not scan_sql:
        return 0, ""
    res = run(scan_sql, page=_PAGE, key="brief_wf_runtimes", tier="recent",
              source="CONTROL_STATUS (latest run)", max_rows=etl_control_sql.MAX_TASKS, probe=True)
    if not (res.ok and not res.empty) or "TASK_STATUS" not in res.df.columns:
        return 0, ""
    status = res.df["TASK_STATUS"].astype(str).str.upper()
    n_fail = int(status.isin(etl_control_sql.FAILED_TASK_STATUSES).sum())
    if not n_fail:
        return 0, ""
    wf = ""
    if "WORKFLOW_NAME" in res.df.columns:
        wf = ", ".join(sorted(res.df["WORKFLOW_NAME"].astype(str).unique())[:2])
    return n_fail, wf


def _nightly_cycle_forecast(settings: dict) -> dict:
    """Whole-cycle SLA finish forecast for the Brief 'Nightly cycle' tile (fail-silent probe read).

    Reuses the Operations SLA-finish builder (cycle_finish_history_scan) + etl_cycle_sla_forecast: the
    cycle STARTER workflow → the TERMINAL workflow, each night's finish vs the clock deadline, trended.
    The anchor workflows + clock times default in DEFAULT_SETTINGS, so this works before V138 is applied.
    Config-gated on ETL_CONTROL_STATUS_FQN + both anchor workflows; returns {} when unconfigured or when
    there is no cycle data (a probe read — a missing grant is silent; Operations ▸ Pipeline owns hints)."""
    fqn = str(settings.get("ETL_CONTROL_STATUS_FQN") or "").strip()
    start_wf = str(settings.get("ETL_CYCLE_START_WORKFLOW") or "").strip()
    end_wf = str(settings.get("ETL_CYCLE_END_WORKFLOW") or "").strip()
    if not fqn or not start_wf or not end_wf:
        return {}
    scan_sql = etl_control_sql.cycle_finish_history_scan(
        fqn, start_workflow=start_wf, end_workflow=end_wf)
    if not scan_sql:
        return {}
    res = run(scan_sql, page=_PAGE, key="brief_cycle_finish", tier="recent",
              source="CONTROL_STATUS (cycle finish forecast)",
              max_rows=etl_control_sql.MAX_SLA_NIGHTS, probe=True)
    if not (res.ok and not res.empty):
        return {}
    fc = etl_cycle_sla_forecast(
        res.df,
        target_hhmm=str(settings.get("ETL_SLA_TARGET_HHMM") or "07:00").strip(),
        breach_hhmm=str(settings.get("ETL_SLA_BREACH_HHMM") or "08:00").strip())
    return fc or {}


def _nightly_cycle_kpi(fc: dict, wf_fail_n: int) -> dict:
    """The Brief 'Nightly cycle' KPI (replaces Open incidents): the SLA finish forecast + any failures.

    Worst-first — Failures → Overdue/In flight → Late → Regressing → On track — and it paints the
    green 'On track' ONLY when the LATEST night actually COMPLETED on time. A hung/in-flight or FAILED
    latest night, or a forecast with no completed baseline, must never read green off an older night's
    margin (that would be a false all-clear on the executive Brief). Pure: no I/O; takes the already-read
    forecast dict + the latest-run failure count."""
    tgt = (fc.get("target_hhmm") if fc else None) or "07:00"
    help_txt = ("Whole nightly ETL cycle: the SLA finish forecast (the cycle's finish vs the "
                f"{tgt} target, trended across nights) plus any failed tasks in the latest run. "
                "Operations ▸ Pipeline owns the detail.")

    def _tile(value: str, severity: str, delta: str) -> dict:
        return {"label": "Nightly cycle", "value": value, "severity": severity,
                "delta": delta, "help": help_txt}

    # 1. failures — a failed task in the latest run, OR the cycle's terminal workflow failed
    #    (checked directly off the forecast so a differently-scoped wf_fail_n read can't miss it).
    if wf_fail_n:
        _w = "task" if wf_fail_n == 1 else "tasks"
        return _tile("Failures", "bad", f"{wf_fail_n} failed {_w}")
    if fc and fc.get("latest_failed"):
        return _tile("Failures", "bad", "terminal workflow failed")
    # 2. no forecast data at all — never fake green
    if not fc:
        return _tile("—", "info", "awaiting cycle data")
    sev = fc.get("severity")
    margin = fc.get("latest_margin_sec")
    latest_state = fc.get("latest_state")
    # 3. latest night still running / hung — overdue if already past the target, else in flight
    if latest_state == "INCOMPLETE":
        runway = fc.get("live_runway_sec")
        if runway is not None and safe_float(runway) < 0:
            return _tile("Overdue", "bad",
                         f"{humanize_duration(abs(safe_float(runway)), 's')} past {tgt}, still running")
        return _tile("In flight", "info", "cycle still running")
    # 4. last completed night finished after the deadline
    if sev == "High":
        _late = (f"{humanize_duration(abs(safe_float(margin)), 's')} past {tgt}"
                 if margin is not None else (fc.get("forecast") or "late"))
        return _tile("Late", "bad", _late)
    # 5. finish trending later
    if sev == "Medium":
        n2b = fc.get("nights_to_breach")
        _when = f"trending later · ~{n2b} night(s) to miss" if n2b else "finishing later each night"
        return _tile("Regressing", "warn", _when)
    # 6. GREEN only when the latest night COMPLETED with a real on-time margin
    if latest_state == "COMPLETE" and margin is not None and safe_float(margin) >= 0:
        return _tile("On track", "ok", f"{humanize_duration(safe_float(margin), 's')} before {tgt}")
    # 7. anything else (insufficient history / no completed baseline) — neutral, never green
    return _tile("—", "info", "awaiting cycle data")


@safe_page(_PAGE)
def render() -> None:
    f = filters()
    company = f["company"]
    # F1: H1 matches the sidebar nav label verbatim; the subtitle keeps the
    # morning identity.
    page_header("Brief", "Top numbers, open fires, and the day's asks.", icon_name="brief")
    section_filter_contract(
        f,
        applies=(),
        partial=("company",),
        note="Company shapes open incidents/events; spend, contract, freshness, and owner queue use fixed account-wide horizons.",
    )
    settings = load_settings(_PAGE)
    rate = safe_float(settings.get("CREDIT_PRICE_USD"), 3.68)
    ai_rate = safe_float(settings.get("AI_CREDIT_PRICE_USD"), 2.20)

    # Two tier-grouped parallel batches (live round 10: ten serial reads made
    # the exec page the slow one — p95 8.9s). Any batch failure falls back to
    # the original serial per-query path below, unchanged.
    # health_strip deliberately stays outside this batch: it has its own shell
    # TTL on other pages and the serial call below keeps the same cache identity.
    _b_live = run_batch([
        {"key": "inc", "sql": mart_sql.open_incidents(5, company),
         "source": f"INCIDENTS (open, {company} + account-level)"},
        {"key": "inc_met", "sql": mart_sql.incident_metrics(90, company),
         "source": f"INCIDENTS (open-now count, {company} + account-level)"},
        {"key": f"brief_alert_counts_{company}",
         "sql": mart_sql.open_alert_severity_counts(company),
         "source": f"ALERT_EVENTS counts ({company} + account-level)"},
        {"key": "events", "sql": mart_sql.open_alert_events(50, company),
         "source": "ALERT_EVENTS"},
        {"key": f"acts_{company}", "sql": mart_sql.action_queue(100, company), "source": "ACTION_QUEUE"},
    ], page=_PAGE, tier="live")
    _b_rec = run_batch([
        {"key": "exh", "sql": mart_sql.contract_exhaustion(),
         "source": "SETTINGS + FACT_METERING_DAILY"},
        {"key": "roi", "sql": mart_sql.savings_summary_quarter(), "source": "SAVINGS_LEDGER"},
        {"key": "appq", "sql": mart_sql.app_cost_last_30d(),
         "source": "FACT_WAREHOUSE_DAILY (WH_ALFA_ADMIN trailing 30d)"},
        # PERF #46: the 14d spark moved OUT of this 'recent' batch to the shared hourly
        # daily_spend_wide() read below — batch members cache in a separate 'recent' store
        # that can't share with the solo hourly wide entry Overview/Contract also use.
        {"key": "digest", "sql": mart_sql.latest_digest(),
         "source": "DAILY_DIGEST (Cortex, grounded)"},
    ], page=_PAGE, tier="recent")

    # Perf: 'recent' (300s) — shares the health_strip cache entry with the sidebar/other shells
    # (see main.py); the mart loads hourly and writes invalidate via the domain salt.
    strip = run(mart_sql.health_strip(), page=_PAGE, key="health_strip", tier="recent",
                source="ALERT_EVENTS + SOURCE_FRESHNESS_STATE + FACT_METERING_DAILY")
    strip_up = strip.ok and not strip.empty
    vals = ({str(r["METRIC"]): str(r["VALUE"]) for _, r in strip.df.iterrows()}
            if strip_up else {})
    mtd_credits = safe_float(vals.get("MTD_CREDITS"))
    # C1: price AI/Cortex credits at the AI rate, not the flat compute rate. The
    # strip carries the split; fall back to the flat rate only if a stale cache
    # predates it (both split arms absent) so we never silently show $0.
    if "MTD_CREDITS_OTHER" in vals or "MTD_CREDITS_AI" in vals:
        mtd_usd = blended_billed_usd(safe_float(vals.get("MTD_CREDITS_OTHER")),
                                     safe_float(vals.get("MTD_CREDITS_AI")), rate, ai_rate)
    else:
        mtd_usd = mtd_credits * rate
    alert_counts = _b_live.get(f"brief_alert_counts_{company}")
    if alert_counts is None or not alert_counts.ok:
        alert_counts = run(
            mart_sql.open_alert_severity_counts(company),
            page=_PAGE,
            key=f"brief_alert_counts_{company}",
            tier="live",
            source=f"ALERT_EVENTS counts ({company} + account-level)",
        )
    scoped_crit: int | None = None
    if alert_counts is not None and alert_counts.usable():
        scoped_crit = int(safe_float(alert_counts.df.iloc[0].get("CRIT")))
    # CoCo do-first (duration, not count): the oldest still-open CRITICAL drives MTTR
    # urgency a raw count hides. Reuse the events already fetched in the batch — no query.
    _ev = _b_live.get("events")
    _oldest_crit_h = oldest_open_hours(
        _ev.df if (_ev is not None and _ev.usable()) else None,
        now=account_now(), severity="CRITICAL")
    # Honesty contract: when telemetry is unreachable the Brief says SO —
    # a zero here reads as "we spent nothing", which is a lie (review #5).
    kpis = [
        {"label": "MTD credit spend (account)",
         "badge": "mart" if strip_up else "stale",
         "value": format_usd(mtd_usd) if strip_up else "—",
         "delta": (f"{mtd_credits:,.0f} credits" if strip_up else "telemetry unreachable"),
         "delta_color": "off",
         "severity": "" if strip_up else "warn",
         "help": "Account-wide credit-billed services at configured rates this month. "
                 "Storage, transfer, and organization rate-card adjustments are separate; "
                 "the company filter scopes warehouse, attribution, and user views."},
        {"label": "Open criticals",
         "badge": "live" if scoped_crit is not None else "stale",
         "value": f"{scoped_crit}" if scoped_crit is not None else "—",
         "severity": "bad" if scoped_crit else ("" if scoped_crit is not None else "warn"),
         "delta_color": "inverse" if scoped_crit else "off",
         "help": f"Open criticals for {company} plus account-level events — the same scope as Fires."},
        {"label": "Stalest telemetry",
         # N15: name the source, not just the age — "which one?" is the DBA's
         # first question. N14: the strip's age arm is already cadence-aware.
         "value": _stalest_label(vals) if strip_up else "unknown",
         "severity": "" if strip_up else "warn"},
    ]
    if _oldest_crit_h is not None:
        kpis.append({
            "label": "Oldest open critical",
            "value": humanize_duration(_oldest_crit_h, "h"),
            "severity": "bad" if _oldest_crit_h >= 24 else "warn",
            "help": "Time since the oldest still-open (OPEN or ACK) CRITICAL alert was "
                    "raised — the responsiveness signal a raw count hides. Work the Fires below.",
        })
    if not strip_up:
        st.warning("Telemetry marts unreachable. Figures withheld until they load. "
                   + (strip.error or ""))
    exh = _b_rec.get("exh") or run(mart_sql.contract_exhaustion(), page=_PAGE, key="brief_exhaustion",
              tier="recent", source="SETTINGS + FACT_METERING_DAILY")
    if exh.usable():
        erow = exh.df.iloc[0]
        total = safe_float(erow.get("TOTAL"))
        days_left = safe_float(erow.get("DAYS_LEFT"), -1.0)
        if total > 0 and days_left >= 0:
            kpis.append({
                "label": "Credit commitment exhausts",
                "value": str(erow.get("EXHAUST_DATE")),
                "delta": f"{days_left:,.0f} days at current burn",
                "delta_color": "inverse" if days_left <= 90 else "off",
                "help": "Configured-rate credit runway from trailing 30 complete days. "
                        "It excludes storage, transfer, and organization currency adjustments; "
                        "billing-truth runway is on Cost & Contract > Contract & Forecast.",
            })
    roi = _b_rec.get("roi") or run(mart_sql.savings_summary_quarter(), page=_PAGE, key="brief_roi",
              tier="recent", source="SAVINGS_LEDGER")
    cost_q = _b_rec.get("appq") or run(mart_sql.app_cost_last_30d(), page=_PAGE, key="brief_app_cost",
                 tier="recent", source="FACT_WAREHOUSE_DAILY (WH_ALFA_ADMIN trailing 30d)")
    if roi.usable():
        rrow = roi.df.iloc[0]
        verified = safe_float(rrow.get("VERIFIED_QTD_USD"))
        pipeline = safe_float(rrow.get("ESTIMATED_OPEN_USD"))
        # A zero APP_CREDITS_30D is a MISSING/degenerate denominator (renamed app
        # warehouse, empty FACT_WAREHOUSE_DAILY) — the builder always returns one
        # COALESCE(SUM,0) row, so usable() is True even when it summed nothing. Treat
        # it as unmeasured (None), NOT $0, so the KPI does not paint a false green
        # "pays for itself" when nothing is verified either (0 >= 0). Mirrors
        # proof.roi_multiple's `run_cost > 0` guard + the DS "not measured yet" state.
        _app_credits = safe_float(cost_q.df.iloc[0].get("APP_CREDITS_30D")) if cost_q.usable() else 0.0
        app_usd = _app_credits * rate if _app_credits > 0 else None
        kpis.append({
            "label": "Verified savings (QTD)",
            "value": format_usd(verified),
            "delta": (f"vs {format_usd(app_usd)} monthly run cost" if app_usd is not None
                      else "app cost unavailable"),
            "delta_color": ("normal" if verified >= app_usd else "inverse")
                           if app_usd is not None else "off",
            "help": "VERIFIED ledger items only — proven by before/after actuals, never "
                    "mixed with estimates. App cost = the shared app/loader warehouse's trailing 30-day "
                    "(monthly) run cost -- same horizon as the monthly-magnitude savings. Green: "
                    "verified savings exceed the app's run cost.",
        })
        if pipeline > 0:
            kpis.append({
                "label": "Estimated pipeline",
                "value": format_usd(pipeline),
                "delta_color": "off",
                "help": "Open ESTIMATED items awaiting the monthly verifier. "
                        "Shown separately from verified savings.",
            })
    _inc_company = company
    _inc = _b_live.get("inc") or run(mart_sql.open_incidents(5, _inc_company), page=_PAGE,
               key=f"brief_incidents_{_inc_company}", tier="live",
               source=f"INCIDENTS (open, {_inc_company} + account-level)")
    if _inc.ok:
        # Count from the UNCAPPED incident_metrics.OPEN_NOW (the same builder Control Room reads), not
        # len() of the LIMIT-5 open_incidents feed used for the detail below -- otherwise the KPI
        # saturates at 5 and disagrees with the Control Room queue for >5 open incidents (recon-audit
        # 2026-08-30). Fall back to the capped len only if the metrics read is unavailable.
        _inc_met = _b_live.get("inc_met")
        if _inc_met is not None and _inc_met.usable():
            _n_inc = int(safe_float(_inc_met.df.iloc[0].get("OPEN_NOW")))
        else:
            _n_inc = len(_inc.df)
        # (The "Open incidents" KPI tile was replaced by the "Nightly cycle" tile below — owner ask
        # 2026-09-09. _n_inc still feeds the verdict line + the Fires detail; the Control Room owns
        # the incident queue and the executive incident glance.)
    # Reference-data gaps (ETL Phase 1): a source code with no XLAT translation
    # hard-fails tonight's load, so it belongs in the morning "should I worry?" read
    # like a fire. Reuses the Operations panel's scan, account-wide (pinned + every
    # configured check); config-gated and FAIL-SILENT (unset config / a missing grant
    # returns 0 — the Operations ▸ Pipeline panel owns the setup + grant hints).
    _ref_gap_n, _ref_gap_types = _reference_gap_summary(settings)
    # A FAILED task in the latest Informatica ETL run is a morning fire too (it usually
    # breaks a downstream load). Same reused scan + shared failure set as the Operations
    # panel; config-gated + fail-silent. Only fires when a real failure exists.
    _wf_fail_n, _wf_fail_wf = _workflow_failure_summary(settings)
    # Nightly-cycle health tile (replaces Open incidents, owner ask 2026-09-09): the whole-cycle SLA
    # finish forecast (good / regressing / late) plus any failed tasks in the latest run. Config-gated
    # on the ETL control table + fail-silent (probe reads); shown whenever ETL is configured so the
    # morning read leads with "is the nightly cycle OK?".
    _cyc = _nightly_cycle_forecast(settings)
    if str(settings.get("ETL_CONTROL_STATUS_FQN") or "").strip():
        kpis.append(_nightly_cycle_kpi(_cyc, _wf_fail_n))

    # CoCo do-first #1: a computed "should I worry?" opener, worst-first, above the
    # numbers — built from signals already on the page (no new query).
    _vsig = []
    if not strip_up:
        _vsig.append(Signal("warn", "telemetry marts unreachable — figures withheld"))
    if scoped_crit is None:
        _vsig.append(Signal("warn", "open-critical count unavailable"))
    elif scoped_crit > 0:
        _age = (f", oldest {humanize_duration(_oldest_crit_h, 'h')}"
                if _oldest_crit_h is not None else "")
        _vsig.append(Signal("bad", f"{scoped_crit} open critical alert(s){_age}"))
    if not _inc.ok:
        # A FAILED incident read must NOT read as a green "no incidents" all-clear (the healthy
        # verdict below explicitly asserts "no ... incidents"). Mirror the critical-side guard
        # above (open-critical count unavailable) so open-incident status is disclosed, not assumed.
        _vsig.append(Signal("warn", "open-incident count unavailable"))
    elif _n_inc > 0:
        # Use the UNCAPPED count (_n_inc, from incident_metrics.OPEN_NOW) the KPI above uses,
        # not len() of the LIMIT-5 feed — else the verdict says "5" while the KPI says the true
        # count for >5 open incidents (round-2 bug hunt; the KPI was hardened, this sibling wasn't).
        _vsig.append(Signal("bad", f"{_n_inc} open incident(s)"))
    if _ref_gap_n:
        _rg_word = "code" if _ref_gap_n == 1 else "codes"
        _vsig.append(Signal(
            "bad", f"{_ref_gap_n} source {_rg_word} missing XLAT translation ({_ref_gap_types})"))
    if _wf_fail_n:
        _wf_word = "task" if _wf_fail_n == 1 else "tasks"
        _vsig.append(Signal(
            "bad", f"{_wf_fail_n} failed ETL {_wf_word} in the latest run"
                   + (f" ({_wf_fail_wf})" if _wf_fail_wf else "")))
    # The nightly cycle failing / hanging / finishing late (or trending later) is a morning worry too,
    # from the same whole-cycle SLA forecast the tile shows — so the verdict is never silently green on
    # a non-completed latest night. Worst-first, one signal; task-level failures are covered above.
    if _cyc:
        _c_tgt = _cyc.get("target_hhmm", "07:00")
        _c_state = _cyc.get("latest_state")
        _c_rw = _cyc.get("live_runway_sec")
        if _cyc.get("latest_failed") and not _wf_fail_n:
            _vsig.append(Signal("bad", "nightly cycle's terminal workflow failed — finish unconfirmed"))
        elif _c_state == "INCOMPLETE" and _c_rw is not None and safe_float(_c_rw) < 0:
            _vsig.append(Signal(
                "bad", f"nightly cycle still running, "
                       f"{humanize_duration(abs(safe_float(_c_rw)), 's')} past the {_c_tgt} target"))
        elif _cyc.get("severity") == "High":
            _c_margin = _cyc.get("latest_margin_sec")
            _c_late = (f", {humanize_duration(abs(safe_float(_c_margin)), 's')} past {_c_tgt}"
                       if _c_margin is not None else "")
            _vsig.append(Signal("bad", f"nightly cycle finished after the {_c_tgt} target{_c_late}"))
        elif _cyc.get("severity") == "Medium":
            _c_n2b = _cyc.get("nights_to_breach")
            _c_when = f", ~{_c_n2b} night(s) to miss" if _c_n2b else ""
            _vsig.append(Signal(
                "warn", f"nightly cycle finish trending later vs the {_c_tgt} target{_c_when}"))
    if exh.usable():
        _erow = exh.df.iloc[0]
        if safe_float(_erow.get("TOTAL")) > 0:
            _dl = safe_float(_erow.get("DAYS_LEFT"), -1.0)
            if 0 <= _dl <= 30:
                _vsig.append(Signal("bad", f"contract runway {_dl:,.0f} days"))
            elif 0 <= _dl <= 90:
                _vsig.append(Signal("warn", f"contract runway {_dl:,.0f} days"))
    page_verdict_line(page_verdict(
        _vsig, healthy="no open criticals or incidents; contract runway healthy"))
    contract_runway_bar(contract_runway(exh.df.iloc[0]) if exh.usable() else None)
    panel_help(
        "Your one-scroll morning read: the headline numbers, then open fires, then the "
        "top asks. A dash (—) means telemetry was unreachable, not zero. When a figure "
        "turns red — open criticals above zero, or contract/savings in the danger band — "
        "work the Fires and Asks below, then drill into the linked full page "
        "(Alerts, Cost & Contract, Control Room)."
    )
    kpi_row(kpis)
    # N7: same disclosure as Overview — the headline dollars are credit-billed
    # services; storage and data-transfer bill separately (Cost & Contract).
    # #1: pure billing-basis disclosure → audit-mode only (the note Overview also hides).
    methodology_note("Spend covers credit-billed services (compute, serverless, AI); "
                     "storage and data-transfer bill separately.")

    spend = daily_spend_wide(_PAGE)   # PERF #46: shared wide read; sliced to 14d for the spark
    brief_spend_series: list[float] = []
    if spend.ok and not spend.empty:
        spark_df = daily_spend_last_n(spend.df, 14)
        charts.sparkline_row([("Spend, 14 days", spark_df, "DAY", "CREDITS_BILLED")])
        if {"CREDITS_BILLED_OTHER", "CREDITS_BILLED_AI"}.issubset(spark_df.columns):
            brief_spend_series = [
                blended_billed_usd(row["CREDITS_BILLED_OTHER"], row["CREDITS_BILLED_AI"],
                                   rate, ai_rate)
                for _, row in spark_df.iterrows()
            ]
        else:
            brief_spend_series = [safe_float(value) * rate
                                  for value in spark_df["CREDITS_BILLED"].tolist()]

    # N2: a critical that paged nobody hides behind a green board — call it out
    # on the one surface a half-awake on-call actually reads.
    _und = int(safe_float(vals.get("UNDELIVERED_CRITICAL", "0"))) if strip_up else 0
    _und_age = safe_float(vals.get("UNDELIVERED_OLDEST_MIN", "0")) if strip_up else 0.0
    _und_age_txt = f", oldest {humanize_duration(_und_age, 'min')}" if _und_age > 0 else ""
    if _und and st.button(f"⚠ {_und} critical alert(s) reached nobody{_und_age_txt} — "
                          "check delivery →", key="brief_undelivered", type="primary",
                          width="stretch"):
        request_navigation("Alerts", "Native delivery")

    # A source code with no XLAT translation hard-fails tonight's ETL load — the
    # DBA's every-morning MINUS check, surfaced on the landing page so it can't
    # hide three tabs deep. Jumps to the panel that lists the exact codes to add.
    if _ref_gap_n:
        _rg_word = "code" if _ref_gap_n == 1 else "codes"
        if st.button(f"⚠ {_ref_gap_n} source {_rg_word} with no XLAT translation "
                     f"({_ref_gap_types}) — add the translation rows before tonight's load →",
                     key="brief_ref_gap", type="primary", width="stretch"):
            request_navigation("Operations", "Pipeline SLA")

    # A FAILED task in the latest Informatica ETL run — surfaced here (the panel that
    # lists every task lives in Operations ▸ Pipeline). Only shows on a real failure.
    if _wf_fail_n:
        _wf_word = "task" if _wf_fail_n == 1 else "tasks"
        if st.button(f"⚠ {_wf_fail_n} ETL {_wf_word} FAILED in the latest run"
                     + (f" ({_wf_fail_wf})" if _wf_fail_wf else "")
                     + " — check the run before its downstream loads →",
                     key="brief_wf_fail", type="primary", width="stretch"):
            request_navigation("Operations", "Pipeline SLA")

    # Honor the company filter (live finding 2026-07-08: Trexis warehouse
    # fires showed under an ALFA scope). Account-level events always show.
    events = _b_live.get("events") or run(mart_sql.open_alert_events(50, company), page=_PAGE,
                 key=f"brief_events_{company}", tier="live", source="ALERT_EVENTS")
    # C23: "Fires" is amber only when critical/high fires EXIST.
    _crit_n = (int(events.df["SEVERITY"].astype(str).isin(["CRITICAL", "HIGH"]).sum())
               if events.ok and not events.empty else (0 if events.ok else None))
    section_header("Fires", alarm_health(_crit_n), "alerts")
    if events.ok and not events.empty:
        crit = events.df[events.df["SEVERITY"].astype(str).isin(["CRITICAL", "HIGH"])]
        if crit.empty:
            empty_state("clean", "No open critical or high alerts.")
        else:
            _fires = crit.head(5)
            _fire_sel = selectable_table(_fires[["RAISED_AT", "SEVERITY", "TITLE"]],
                                         key="brief_fires_sel", height=TABLE_H_SM)
            # rec29 sticky-selection guard: st.dataframe re-emits the selection on
            # every rerun, so open the event's drawer only when the row CHANGES.
            if _fire_sel is not None and _fire_sel != st.session_state.get("_brief_fire_sel_last"):
                st.session_state["_brief_fire_sel_last"] = _fire_sel
                _eid = str(_fires.iloc[int(_fire_sel)]["EVENT_ID"])
                request_navigation("Alerts", "Open events", context={"event_id": _eid})
            if company != "ALL":
                st.caption(f"Scoped to {company} plus account-level events.")
            if st.button("Open the alert queue →", key="brief_alerts", width="stretch"):
                request_navigation("Alerts", "Open events")
    else:
        # rec23/house-rule-8: green means VERIFIED CLEAN, never "nothing loaded".
        if events.ok:
            empty_state("clean", "No open alerts.")
        else:
            empty_state("needs_setup", "Alerting not installed yet.")

    section_header("Asks", "", "bolt")
    brief_action_lines: list[str] = []
    actions = _b_live.get(f"acts_{company}") or run(mart_sql.action_queue(100, company), page=_PAGE,
                  key=f"brief_actions_{company}", tier="live", source="ACTION_QUEUE")
    if actions.ok and not actions.empty:
        ranked = rank_actions(actions.df, limit=3)
        if ranked.empty:
            empty_state("clean", "Nothing waiting on an owner.")
        else:
            for _, a in ranked.iterrows():
                est = safe_float(a.get("ESTIMATED_USD"))
                # DS #7: disclose the estimate's time basis inline so a monthly run-rate
                # and a one-time saving don't read as the same number.
                _basis = {"MONTHLY": "/mo", "ANNUAL": "/yr", "ONE_TIME": " one-time"}.get(
                    str(a.get("PERIOD") or "").strip().upper(), "")
                brief_action_lines.append(
                    f"[{a['SEVERITY']}] {a['TITLE']} - owner {a.get('OWNER') or 'unassigned'}"
                    + (f" - about {format_usd(est)}{_basis}" if est > 0 else "")
                )
                # $-escape: TITLE is data — a '$' in it pairs with format_usd's '$'
                st.markdown(md_dollars(f"- **[{a['SEVERITY']}]** {a['TITLE']} — owner "
                            f"{a.get('OWNER') or 'unassigned'}"
                            + (f" · ~{format_usd(est)}{_basis}" if est > 0 else "")))
            # D1: the top three, by WHAT? Severity first, dollars only as a tiebreak
            # inside a band — without this line a reader takes a $-annotated list for
            # a $-ordered one and asks why the biggest number is not on top.
            st.caption("Top 3 by severity, then overdue, then estimated $, then age.")
    else:
        if actions.ok:
            empty_state("clean", "Action queue is empty.")
        else:
            empty_state("needs_setup", "Action queue not installed yet.")

    # Watch automation (owner ask 2026-08-17): the proactive half of "watch". If
    # any watched entity moved (cost spike/drop or health drop), the badge leads
    # here on the landing page with what moved and a jump to the Watchlist; quiet
    # when steady. Renders nothing when the viewer has no watchlist.
    render_watch_badge(viewer_name(), rate)

    # rec2: the AI narrative is context, not the headline — it sits BELOW the numbers,
    # fires, and asks (the page's "numbers first, fires second, asks third" contract)
    # and is collapsed by default so open fires stay above the fold.
    digest = _b_rec.get("digest") or run(mart_sql.latest_digest(), page=_PAGE, key="daily_digest", tier="recent",
                 source="DAILY_DIGEST (Cortex, grounded)")
    if digest.usable():
        drow = digest.df.iloc[0]
        with st.expander(f"AI morning narrative — {drow.get('DIGEST_DATE')}", expanded=False):
            st.markdown(str(drow.get("BODY") or ""))

    _brief_view = ExecutiveSummaryView(
        company=company,
        days=14,
        generated=account_now().strftime("%Y-%m-%d %H:%M") + " (account time)",
        cards=tuple(
            (str(item.get("label", "Metric")),
             str(item.get("value", "-")).replace("â€”", "-")
             + (f" | {item['delta']}" if item.get("delta") else ""))
            for item in kpis
        ),
        actions=tuple(brief_action_lines),
        spend_series=tuple(brief_spend_series),
        scope_notes=(
            "MTD spend, contract, savings, and freshness are account-wide unless the card says otherwise.",
            f"Alerts and incidents honor {company} plus account-level events; the action queue is account-wide.",
            "Metering can lag up to 24 hours. A dash means telemetry was unavailable, not zero.",
        ),
        title="Morning brief",
    )
    with st.expander("Executive export", expanded=False):
        _ex_html, _ex_slide, _ex_csv = st.columns(3)
        with _ex_html:
            export_button(
                "Presentation (HTML)",
                executive_summary_html(_brief_view, presentation=True),
                file_name="overwatch_morning_brief.html",
                mime="text/html",
                width="stretch",
            )
        with _ex_slide:
            download_text_button(
                "Slide bullets (.txt)",
                executive_slide_bullets(_brief_view),
                "overwatch_morning_brief_bullets.txt",
            )
        with _ex_csv:
            export_button(
                "Brief data (CSV)",
                executive_summary_csv(_brief_view),
                file_name="overwatch_morning_brief.csv",
                mime="text/csv",
                width="stretch",
            )

    _case_items = list(st.session_state.get(case_file.CASE_STATE_KEY, []))
    with st.expander(f"Operator Case File ({len(_case_items)})", expanded=bool(_case_items)):
        st.caption(
            "A session-only, cross-section handoff. Click **Add to Case** on evidence across "
            "Alerts, Operations, Security and Overview to collect it here, then export one "
            "Markdown document for a ticket or the next shift. Cleared when the session ends.")
        if not _case_items:
            st.caption("The case is empty.")
        else:
            _md = case_file.assemble_markdown(
                _case_items, generated=pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"))
            _dl, _clr = st.columns([3, 1])
            with _dl:
                download_text_button("Case File (.md)", _md, "overwatch_case_file.md")
            with _clr:
                if st.button("Clear case", key="ow_case_clear", width="stretch"):
                    st.session_state[case_file.CASE_STATE_KEY] = case_file.clear_items()
                    st.rerun()
            for _i, _it in enumerate(_case_items):
                _row, _rm = st.columns([6, 1])
                with _row:
                    st.markdown(
                        f"**{_it.get('title') or _it.get('section')}** — {_it.get('company')} · "
                        f"{_it.get('window')} · `{_it.get('source') or '—'}`")
                with _rm:
                    if st.button("Remove", key=f"ow_case_rm_{_i}", width="stretch"):
                        st.session_state[case_file.CASE_STATE_KEY] = case_file.remove_item(
                            _case_items, _it["id"])
                        st.rerun()
            # Raw markdown as the copy-out surface: a working path even when the SiS
            # download button is inert, and it sidesteps $-as-LaTeX in st.markdown.
            st.code(_md, language="markdown")

    st.caption(pd.Timestamp.now().strftime("Generated %Y-%m-%d %H:%M") +
               " · full detail lives on Overview and Control Room.")
