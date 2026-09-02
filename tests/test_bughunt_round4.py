"""Regression locks for the round-4 bug hunt (v4.419.0)."""

from __future__ import annotations

import math
from pathlib import Path

from app.logic.formulas import humanize_bytes, pct_delta
from app.logic.sizing import simulate_scenario

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- v4418-autosuspend-zero: never-suspend (now=0) preserved -> ON reads as a saving
def _sim(now: int, new: int) -> dict:
    return simulate_scenario(size="MEDIUM", credits_window=100, idle_credits_window=80,
                             window_days=30, rate_usd=3.68, size_delta=0,
                             autosuspend_now_s=now, autosuspend_new_s=new)


def test_never_suspend_now_turning_on_autosuspend_is_a_saving():
    r = _sim(0, 60)                                   # never -> 60s
    assert r["monthly_low_usd"] < r["monthly_now_usd"]       # adding auto-suspend SAVES
    assert "never -> 60s" in next(a for a in r["assumptions"] if "Auto-suspend" in a)


def test_removing_autosuspend_is_not_a_saving():
    r = _sim(600, 0)                                  # 600s -> never
    assert r["monthly_high_usd"] >= r["monthly_now_usd"]     # removing it never reads as a saving


def test_normal_autosuspend_reduction_still_saves():
    r = _sim(600, 60)
    assert r["monthly_low_usd"] < r["monthly_now_usd"]


# --- nfe-2: pct_delta collapses -0.0 so a flat metric isn't signed/colored as a drop
def test_pct_delta_never_returns_negative_zero():
    d = pct_delta(99.99, 100.0)                       # tiny drop rounds to -0.0
    assert d == 0.0 and math.copysign(1.0, d) > 0     # positive zero, not -0.0
    assert pct_delta(100, 100) == 0.0
    assert pct_delta(99, 100) == -1.0                 # a real drop is untouched


# --- nfe-1: humanize_bytes promotes at the boundary instead of "1,024.0 MB"
def test_humanize_bytes_promotes_at_the_unit_boundary():
    assert humanize_bytes(1024 ** 3 - 1) == "1.0 GB"          # just under 1 GB -> 1.0 GB
    assert humanize_bytes(int(1023.97 * 1024 ** 2)) == "1.0 GB"
    assert humanize_bytes(512 * 1024 ** 2) == "512.0 MB"      # mid-range untouched
    assert humanize_bytes(int(1.5 * 1024 ** 3)) == "1.5 GB"


# --- recon-etl-drill-bounds: the drill honors Last-month bounds like its parent
def test_etl_drill_threads_bounds_like_the_parent():
    from datetime import date

    from app.data import etl_sql
    bounds = (date(2026, 8, 1), date(2026, 9, 1))
    drill = etl_sql.etl_failed_runs_for_pipeline("p", 30, "ALL", "", "", bounds=bounds)
    assert "2026-08-01" in drill and "2026-09-01" in drill    # bounded, not trailing-N-days
    plain = etl_sql.etl_failed_runs_for_pipeline("p", 30, "ALL", "", "")
    assert "DATEADD('day', -30, CURRENT_TIMESTAMP())" in plain  # default path unchanged
    uc = _src("app/ui/pages/cost_parts/unit_costs.py")
    assert 'f["schema_contains"], bounds=bounds)' in uc         # UI threads bounds
    assert "_sel_pipe[:40]}{_lm}" in uc                         # and the cache discriminator


# --- TLH-1/2/3: LIMIT-capped frames disclose their scope instead of posing as totals
def test_truncation_disclosures_are_present():
    ops = _src("app/ui/pages/operations.py")
    assert "_truncated = len(wdf) >= 50" in ops
    assert "top 50 fingerprints by wasted $" in ops
    opt = _src("app/ui/pages/cost_parts/optimize.py")
    assert "_wtrunc = len(sdf) >= 50" in opt
    assert "top 50 tables by retention bytes" in opt
    aic = _src("app/ui/pages/cost_parts/ai_chargeback.py")
    assert "_cc_trunc = len(enriched) >= 500" in aic


# --- fsl-1: Pulse spark/delta suppressed under a schema filter the fact can't honor
def test_pulse_spark_gated_on_schema_filter():
    cr = _src("app/ui/pages/control_room.py")
    assert '_spark_ok = not f["schema_contains"]' in cr
    assert "if _spark_ok else None)" in cr


# --- ALC-1/ALC-2: dead incident metrics removed (verified in test_v032_incidents too)
def test_dead_incident_metrics_gone_from_ui():
    a = _src("app/ui/pages/alerts.py")
    assert '"label": "Reopen rate"' not in a
    assert '"label": "MTTR (90d)"' in a               # relabeled from "MTTA / MTTR"


# --- ASK-1: the AI/Cortex answerer can't hijack a warehouse question
def test_cortex_answerer_excludes_warehouse_questions():
    from app.logic.ask.router import route
    q = "which warehouse costs the most to run ai workloads"
    r = route(q, default_days=30, company="ALL")
    title = r.answerer.title if r.answerer else ""
    assert "Cortex" not in title                       # no per-model hijack
    for genuine in ("which model is driving AI spend", "what is driving cortex credits"):
        rr = route(genuine, default_days=30, company="ALL")
        assert rr.answerer is not None and "Cortex" in rr.answerer.title


# --- CHART-2: operational_replay aligns the two panels on one shared x band
def test_operational_replay_shares_the_x_axis():
    ch = _src("app/ui/charts.py")
    assert 'resolve_scale(x="shared")' in ch
    assert 'bounds="flush"' in ch
