"""Compose a page-level verdict — the one 'should I worry?' line above a page.

CoCo do-first (Overview/Brief/Cost/Control-Room #1): several pages open with a
static description that never changes. A page collects the signals it already
computed (open criticals, contract runway, stale telemetry, ...) as `Signal`s and
this composes a single worst-first sentence with a Healthy / Watch / Attention
label. Pure — no Streamlit, no I/O — so the composition is test-locked and reused
by every surface; the rendering lives in ui.components.page_verdict_line.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

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
        if spill_gb >= 20:
            sigs.append(Signal("bad", f"remote spill {spill_gb:,.0f} GB"))
        elif spill_gb >= 5:
            sigs.append(Signal("warn", f"remote spill {spill_gb:,.0f} GB"))

    ss = int(stale_sources or 0)
    if ss >= 3:
        sigs.append(Signal("bad", f"{ss} stale sources"))
    elif ss >= 1:
        sigs.append(Signal("warn", f"{ss} stale source{'s' if ss > 1 else ''}"))
    return sigs
