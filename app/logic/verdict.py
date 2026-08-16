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
