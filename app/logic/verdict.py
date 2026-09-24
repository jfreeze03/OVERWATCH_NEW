"""Compose a page-level verdict — the one 'should I worry?' line above a page.

CoCo do-first (Overview/Brief/Cost/Control-Room #1): several pages open with a
static description that never changes. A page collects the signals it already
computed (open criticals, contract runway, stale telemetry, ...) as `Signal`s and
this composes a single worst-first sentence with a Healthy / Watch / Attention
label. Pure — no Streamlit, no I/O — so the composition is test-locked and reused
by every surface; the rendering lives in ui.components.page_verdict_line.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from app.logic.formulas import humanize_duration, safe_float

# Mirrors the app's severity vocabulary (kpi_row / section_header: ok|warn|bad).
_RANK = {"bad": 3, "warn": 2, "ok": 1, "": 0}
_LABEL = {"bad": "Attention needed", "warn": "Watch", "ok": "Healthy"}


@dataclass(frozen=True)
class Signal:
    """One page signal. `level` is 'ok' | 'warn' | 'bad'; `phrase` is a short
    clause shown only when the level is worse than ok (e.g. '2 open criticals')."""

    level: str
    phrase: str = ""


def page_verdict(signals, *, healthy: str) -> dict:
    """Compose a page verdict from `signals`.

    The worst level wins the headline; the body leads with the worst concerns
    (bad before warn, insertion order within a level) and drops ok / blank
    signals. When nothing is worse than ok the body is the caller's `healthy`
    sentence. Returns {level, severity, label, body, sentence} where severity is
    the app's 'ok'|'warn'|'bad' for colouring and sentence = 'Label — body'.
    """
    graded = [s for s in signals if s is not None and str(s.level).strip()]
    worst = "ok"
    for s in graded:
        if _RANK.get(s.level, 0) > _RANK.get(worst, 0):
            worst = s.level

    if _RANK.get(worst, 0) <= _RANK["ok"]:
        label = _LABEL["ok"]
        return {"level": "ok", "severity": "ok", "label": label,
                "body": healthy, "sentence": f"{label} — {healthy}"}

    concerns = [
        s.phrase.strip()
        for s in sorted(graded, key=lambda s: -_RANK.get(s.level, 0))
        if _RANK.get(s.level, 0) > _RANK["ok"] and s.phrase.strip()
    ]
    body = "; ".join(concerns) if concerns else healthy
    label = _LABEL[worst]
    return {"level": worst, "severity": worst, "label": label,
            "body": body, "sentence": f"{label} — {body}"}


def oldest_open_hours(
    frame: pd.DataFrame | None,
    *,
    now,
    severity: str | None = None,
    time_col: str = "RAISED_AT",
    severity_col: str = "SEVERITY",
) -> float | None:
    """Hours since the oldest still-open row was raised (CoCo do-first: duration,
    not count — the MTTR-pressure signal a raw count hides).

    Optionally filters to `severity` (case-insensitive). `now` is an account-time
    timestamp the caller supplies — this stays pure, with no clock of its own.
    Returns None when the frame is empty/absent or carries no parseable timestamp
    in `time_col`.
    """
    if frame is None or getattr(frame, "empty", True) or time_col not in frame.columns:
        return None
    view = frame
    if severity is not None and severity_col in frame.columns:
        view = frame[frame[severity_col].astype(str).str.upper() == severity.upper()]
    raised = pd.to_datetime(view.get(time_col), errors="coerce").dropna()
    if raised.empty:
        return None
    return max(0.0, (pd.Timestamp(now) - raised.min()).total_seconds() / 3600.0)


def operations_signals(inputs: pd.DataFrame | None, stale_sources: int = 0) -> list[Signal]:
    """Wave 1 #7: ops-health Signals for the Operations page verdict.

    Reads the SAME per-day aggregates the platform score uses (QUERY_COUNT,
    FAILED_COUNT, TASK_RUNS, TASK_FAILED, QUEUED_SEC, SPILL_GB summed over the
    window) plus stale-source count (from the shell-shared health strip), so the
    verdict agrees with the score rather than inventing a parallel model. Thresholds
    mirror the score's penalty onset (query-fail 2%, task-fail 1%, queue 10 min,
    spill 5 GB). Pure — the caller supplies the frames, so it is test-locked.
    """
    sigs: list[Signal] = []
    if inputs is not None and not getattr(inputs, "empty", True):
        cols = {str(c).upper(): c for c in inputs.columns}

        def _sum(name: str) -> float:
            col = cols.get(name)
            if col is None:
                return 0.0
            return float(pd.to_numeric(inputs[col], errors="coerce").fillna(0).sum())

        queries, q_fail = _sum("QUERY_COUNT"), _sum("FAILED_COUNT")
        tasks, t_fail = _sum("TASK_RUNS"), _sum("TASK_FAILED")
        queued_sec, spill_gb = _sum("QUEUED_SEC"), _sum("SPILL_GB")
        ndays = max(1, len(inputs))
        qf_pct = (q_fail / queries * 100) if queries else 0.0
        tf_pct = (t_fail / tasks * 100) if tasks else 0.0
        queue_min_day = (queued_sec / 60.0) / ndays

        if qf_pct >= 5:
            sigs.append(Signal("bad", f"query failures {qf_pct:.1f}%"))
        elif qf_pct >= 2:
            sigs.append(Signal("warn", f"query failures {qf_pct:.1f}%"))
        if tf_pct >= 5:
            sigs.append(Signal("bad", f"task failures {tf_pct:.1f}%"))
        elif tf_pct >= 1:
            sigs.append(Signal("warn", f"task failures {tf_pct:.1f}%"))
        if queue_min_day >= 30:
            sigs.append(Signal("bad", f"warehouse queueing {queue_min_day:.0f} min/day"))
        elif queue_min_day >= 10:
            sigs.append(Signal("warn", f"warehouse queueing {queue_min_day:.0f} min/day"))
        # r28 (bug-hunt): remote spill is a PER-DAY rate (5 GB/day onset, matching the
        # platform score in scoring.py; the docstring above says "spill 5 GB"). It was
        # compared as a WINDOW TOTAL while queue above is correctly /ndays — so the
        # verdict over-fired ~Nx the window length (0.3 GB/day steady × 30d = 9 GB total
        # → false "Watch") and contradicted the score. Normalize per-day like queue.
        spill_gb_day = spill_gb / ndays
        if spill_gb_day >= 20:
            sigs.append(Signal("bad", f"remote spill {spill_gb_day:,.1f} GB/day"))
        elif spill_gb_day >= 5:
            sigs.append(Signal("warn", f"remote spill {spill_gb_day:,.1f} GB/day"))

    ss = int(stale_sources or 0)
    if ss >= 3:
        sigs.append(Signal("bad", f"{ss} stale sources"))
    elif ss >= 1:
        sigs.append(Signal("warn", f"{ss} stale source{'s' if ss > 1 else ''}"))
    return sigs


def decision_studio_signals(proof: dict | None) -> list[Signal]:
    """Wave 2 #8: page-verdict Signals for Decision Studio, DERIVED from the prove-it
    verdict (app.logic.proof.proof_verdict) the Scorecard section already computes — so
    the page-open 'should I worry?' line can never disagree with the scorecard banner
    below it (same thresholds, same 'unproven' honesty), and no parallel model drifts.

    Maps proof_verdict's level to the page vocabulary: 'good' -> no concern (Healthy);
    'watch' -> one warn Signal per worst-first reason; 'unproven' (not enough labeled
    evidence yet) -> a single warn, so an unproven account never reads as a green
    all-clear. proof_verdict has no critical tier, so the DS verdict tops out at Watch.
    Pure — the caller supplies the already-composed proof dict.
    """
    proof = proof or {}
    level = str(proof.get("level") or "unproven")
    if level == "good":
        return []
    if level == "unproven":
        return [Signal("warn", "not enough verified outcomes yet to prove value")]
    reasons = [str(r).strip() for r in (proof.get("reasons") or []) if str(r).strip()]
    if reasons:
        return [Signal("warn", r) for r in reasons]
    return [Signal("warn", "providing value, but some proof signals need watching")]


ATTENTION_HEALTHY = "no open criticals or incidents, delivery clear, telemetry fresh"


@dataclass(frozen=True)
class AttentionBundle:
    """rec1: the shared morning-attention inputs the Brief AND the Control Room verdicts read.
    None = the read failed / is unknown (-> a warn, never a silent all-clear); 0 = verified none."""
    open_crit: int | None = None
    oldest_crit_h: float | None = None
    undelivered: int = 0
    stale_sources: int | None = None        # None = health strip unavailable
    open_incidents: int | None = None
    ref_gap_n: int = 0
    ref_gap_label: str = ""
    night: dict[str, Any] = field(default_factory=dict)   # insights.cycle_night_summary(...)
    cycle: dict[str, Any] = field(default_factory=dict)   # insights.etl_cycle_sla_forecast(...)


def attention_bundle(*, strip_vals: dict | None, crit_row: dict | None,
                     open_incidents: int | None, etl: dict | None = None) -> AttentionBundle:
    """The ONE mapping from the pages' already-fetched reads (health_strip METRIC->VALUE dict,
    open_alert_severity_counts row, the uncapped open-incident count, attention.etl_attention()) onto
    the bundle — so Brief and Control Room cannot map them differently. Pure."""
    sv = strip_vals or None
    open_crit: int | None = None
    oldest: float | None = None
    if crit_row is not None:
        open_crit = int(safe_float(crit_row.get("CRIT")))
        if open_crit > 0:
            ocm = safe_float(crit_row.get("OLDEST_CRIT_MIN"), default=float("nan"))
            if ocm == ocm:
                oldest = max(0.0, ocm) / 60.0
    e = etl or {}
    return AttentionBundle(
        open_crit=open_crit, oldest_crit_h=oldest,
        undelivered=int(safe_float(sv.get("UNDELIVERED_CRITICAL"))) if sv else 0,
        stale_sources=int(safe_float(sv.get("STALE_SOURCES"))) if sv else None,
        open_incidents=open_incidents,
        ref_gap_n=int(safe_float(e.get("ref_gap_n"))),
        ref_gap_label=str(e.get("ref_gap_label") or ""),
        night=dict(e.get("night") or {}),
        cycle=dict(e.get("cycle") or {}),
    )


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def attention_signals(b: AttentionBundle) -> list[Signal]:
    """rec1: the shared 'should I worry?' Signals — criticals, undelivered, stale telemetry, open
    incidents, XLAT gaps, whole-night ETL (failed / did-not-run / cycle not started) and the cycle
    SLA. Brief and Control Room both compose from this (parity-locked); pages add only their own
    page-specific signals (Brief: contract runway). Pure."""
    sigs: list[Signal] = []
    if b.open_crit is None:
        sigs.append(Signal("warn", "open-critical count unavailable"))
    elif b.open_crit > 0:
        _age = (f", oldest {humanize_duration(b.oldest_crit_h, 'h')}"
                if b.oldest_crit_h is not None else "")
        sigs.append(Signal("bad", f"{b.open_crit} open critical alert(s){_age}"))
    if b.undelivered > 0:
        sigs.append(Signal("bad", f"{b.undelivered} critical(s) reached nobody"))
    if b.stale_sources is None:
        sigs.append(Signal("warn", "health telemetry unavailable"))
    elif b.stale_sources > 0:
        sigs.append(Signal("warn", f"{b.stale_sources} telemetry source(s) stale or not loaded"))
    if b.open_incidents is None:
        sigs.append(Signal("warn", "open-incident count unavailable"))
    elif b.open_incidents > 0:
        sigs.append(Signal("bad", f"{b.open_incidents} open incident(s)"))
    if b.ref_gap_n > 0:
        sigs.append(Signal("bad", f"{b.ref_gap_n} source {_plural(b.ref_gap_n, 'code', 'codes')} "
                                  "missing XLAT translation"
                                  + (f" ({b.ref_gap_label})" if b.ref_gap_label else "")))
    night = b.night or {}
    n_fail = int(safe_float(night.get("failed_tasks")))
    if n_fail > 0:
        _l = str(night.get("failed_label") or "")
        sigs.append(Signal("bad", f"{n_fail} failed ETL {_plural(n_fail, 'task', 'tasks')} tonight"
                                  + (f" ({_l})" if _l else "")))
    n_miss = int(safe_float(night.get("missing_wf")))
    if n_miss > 0:
        _l = str(night.get("missing_label") or "")
        sigs.append(Signal("bad", f"{n_miss} nightly {_plural(n_miss, 'workflow', 'workflows')} did not run"
                                  + (f" ({_l})" if _l else "")))
    if night.get("next_cycle_overdue"):
        _a = night.get("cycle_age_sec")
        sigs.append(Signal("bad", "nightly cycle hasn't started"
                                  + (f" — last start {humanize_duration(safe_float(_a), 's')} ago"
                                     if _a is not None else "")))
    cyc = b.cycle or {}
    if cyc:   # MOVED verbatim from brief.py:481-500, with `not n_fail` in place of `not _wf_fail_n`
        tgt = cyc.get("target_hhmm", "07:00")
        state = cyc.get("latest_state")
        rw = cyc.get("live_runway_sec")
        if cyc.get("latest_failed") and not n_fail:
            sigs.append(Signal("bad", "nightly cycle's terminal workflow failed — finish unconfirmed"))
        elif state == "INCOMPLETE" and rw is not None and safe_float(rw) < 0:
            sigs.append(Signal("bad", f"nightly cycle still running, "
                                      f"{humanize_duration(abs(safe_float(rw)), 's')} past the {tgt} target"))
        elif cyc.get("severity") == "High":
            m = cyc.get("latest_margin_sec")
            late = (f", {humanize_duration(abs(safe_float(m)), 's')} past {tgt}" if m is not None else "")
            sigs.append(Signal("bad", f"nightly cycle finished after the {tgt} target{late}"))
        elif cyc.get("severity") == "Medium":
            n2b = cyc.get("nights_to_breach")
            when = f", ~{n2b} night(s) to miss" if n2b else ""
            sigs.append(Signal("warn", f"nightly cycle finish trending later vs the {tgt} target{when}"))
    return sigs


def attention_healthy(b: AttentionBundle) -> str:
    """The shared all-clear sentence; claims the ETL cycle only when the night roll-up actually
    loaded (a silent/unconfigured probe must never read as 'cycle clean')."""
    if not b.night:
        return ATTENTION_HEALTHY
    _open = int(safe_float(b.night.get("running_wf"))) + int(safe_float(b.night.get("pending_wf")))
    return ATTENTION_HEALTHY + ("; nightly ETL: no failures so far" if _open else "; nightly ETL cycle clean")
