"""Next-Fifty #1 (v4.589.0): ONE morning truth. The Brief and the Control Room verdicts compose the SAME
shared attention signals (verdict.attention_signals) from the SAME shared reads (app/ui/attention.py),
so the triage console can no longer say Healthy while the Brief says Attention. Pages add only their
own page-specific signals on top (Brief: contract runway)."""

from __future__ import annotations

import re
from pathlib import Path

from app.logic.verdict import (
    AttentionBundle,
    Signal,
    attention_bundle,
    attention_healthy,
    attention_signals,
)

_ROOT = Path(__file__).resolve().parents[1]
_CLEAR = {"open_crit": 0, "stale_sources": 0, "open_incidents": 0}


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _b(**kw) -> AttentionBundle:
    return AttentionBundle(**{**_CLEAR, **kw})


def test_attention_signal_matrix():
    assert attention_signals(_b()) == []
    assert attention_signals(_b(open_crit=None)) == [Signal("warn", "open-critical count unavailable")]
    assert attention_signals(_b(open_crit=3, oldest_crit_h=26.0)) == [
        Signal("bad", "3 open critical alert(s), oldest 26h")]
    assert attention_signals(_b(undelivered=2)) == [Signal("bad", "2 critical(s) reached nobody")]
    assert attention_signals(_b(stale_sources=None)) == [Signal("warn", "health telemetry unavailable")]
    assert attention_signals(_b(open_incidents=None)) == [Signal("warn", "open-incident count unavailable")]
    assert attention_signals(_b(open_incidents=4)) == [Signal("bad", "4 open incident(s)")]
    assert attention_signals(_b(ref_gap_n=1, ref_gap_label="pc_x.code")) == [
        Signal("bad", "1 source code missing XLAT translation (pc_x.code)")]
    assert attention_signals(_b(night={"failed_tasks": 2, "failed_label": "WF_A"})) == [
        Signal("bad", "2 failed ETL tasks tonight (WF_A)")]
    assert attention_signals(_b(night={"missing_wf": 3, "missing_label": "WF_A, WF_B +1 more"})) == [
        Signal("bad", "3 nightly workflows did not run (WF_A, WF_B +1 more)")]
    (ns,) = attention_signals(_b(night={"next_cycle_overdue": True, "cycle_age_sec": 100000.0}))
    assert ns.level == "bad" and ns.phrase.startswith("nightly cycle hasn't started")
    assert attention_signals(_b(cycle={"latest_failed": True})) == [
        Signal("bad", "nightly cycle's terminal workflow failed — finish unconfirmed")]
    # a terminal failure is suppressed when the night roll-up already names failed tasks
    assert attention_signals(_b(night={"failed_tasks": 1}, cycle={"latest_failed": True})) == [
        Signal("bad", "1 failed ETL task tonight")]
    (inc,) = attention_signals(_b(cycle={"latest_state": "INCOMPLETE", "live_runway_sec": -1200,
                                         "target_hhmm": "07:00"}))
    assert inc.level == "bad" and "past the 07:00 target" in inc.phrase
    (hi,) = attention_signals(_b(cycle={"severity": "High", "latest_margin_sec": -900}))
    assert hi.level == "bad" and hi.phrase.startswith("nightly cycle finished after the 07:00 target")
    (med,) = attention_signals(_b(cycle={"severity": "Medium", "nights_to_breach": 3}))
    assert med.level == "warn" and "~3 night(s) to miss" in med.phrase


def test_attention_bundle_maps_reads():
    b = attention_bundle(strip_vals=None, crit_row=None, open_incidents=None)
    assert (b.stale_sources, b.undelivered, b.open_crit, b.open_incidents) == (None, 0, None, None)
    b = attention_bundle(strip_vals={"STALE_SOURCES": "1", "UNDELIVERED_CRITICAL": "2"},
                         crit_row={"CRIT": 2, "OLDEST_CRIT_MIN": 90}, open_incidents=0)
    assert (b.stale_sources, b.undelivered, b.open_crit, b.oldest_crit_h) == (1, 2, 2, 1.5)
    assert attention_bundle(strip_vals={}, crit_row={"CRIT": 0, "OLDEST_CRIT_MIN": 90},
                            open_incidents=0).oldest_crit_h is None
    etl = {"ref_gap_n": 2, "ref_gap_label": "a", "night": {"failed_tasks": 1}, "cycle": {"severity": "OK"}}
    b = attention_bundle(strip_vals=None, crit_row=None, open_incidents=0, etl=etl)
    assert (b.ref_gap_n, b.ref_gap_label, b.night, b.cycle) == (2, "a", {"failed_tasks": 1}, {"severity": "OK"})


def test_healthy_claims_etl_only_when_known():
    assert "nightly ETL" not in attention_healthy(_b())
    assert attention_healthy(_b(night={"workflows": 12})).endswith("; nightly ETL cycle clean")


def test_brief_and_control_room_share_one_attention_path():
    brief, cr = _src("app/ui/pages/brief.py"), _src("app/ui/pages/control_room.py")
    ops = _src("app/ui/pages/operations.py")
    for page in (brief, cr):
        assert "attention.etl_attention(settings, page=_PAGE)" in page
        assert "attention_bundle(" in page and "attention_healthy(_attn)" in page
        for literal in ("open critical alert(s)", 'reached nobody")', "stale or not loaded", "failed ETL"):
            assert literal not in page, literal       # the Signal phrases live ONLY in verdict.py
    assert "attention_signals(_attn)" in cr
    assert "_vsig = attention_signals(_attn)" in brief
    pages = list((_ROOT / "app" / "ui" / "pages").rglob("*.py"))
    assert not [p.name for p in pages if "etl_control_sql.cycle_night_health_scan(" in p.read_text(encoding="utf-8")]
    assert "etl_control_sql.cycle_night_health_scan(" in _src("app/ui/attention.py")
    assert "attention.cycle_night_read(settings, page=_PAGE)" in ops
    # never prefetched: run_batch members cache in a separate store and would lose the cross-page hit
    for src in (brief, ops):
        for want in re.findall(r"_pipeline_prefetch\([^)]*want=\{([^}]*)\}", src):
            assert "cycle_night" not in want


def test_both_pages_feed_the_bundle_from_the_same_reads():
    # parity comes from the INPUTS: the same four reads (health strip, scoped critical counts, the
    # uncapped incident metrics at the SAME live tier, the shared ETL attention) on both pages
    brief, cr = _src("app/ui/pages/brief.py"), _src("app/ui/pages/control_room.py")
    for page in (brief, cr):
        for read in ("mart_sql.health_strip()", "mart_sql.open_alert_severity_counts(company)",
                     "mart_sql.incident_metrics(90, company)", "attention.etl_attention(settings, page=_PAGE)"):
            assert read in page, read
    hoist = cr.split("_inc_met = run(mart_sql.incident_metrics(90, company)", 1)[1][:200]
    assert 'tier="live"' in hoist                      # the Brief reads it in its live batch
    brief_batch = brief.split('"key": "inc_met"', 1)[0].rsplit("run_batch", 1)[1]
    assert 'tier="live"' in brief.split('"key": "inc_met"', 1)[1][:2000] or 'tier="live"' in brief_batch


def test_healthy_never_claims_clean_with_work_still_running():
    assert attention_healthy(_b(night={"workflows": 12, "running_wf": 1})).endswith("; nightly ETL: no failures so far")
    assert attention_healthy(_b(night={"workflows": 12, "pending_wf": 2})).endswith("; nightly ETL: no failures so far")
    assert attention_healthy(_b(night={"workflows": 12, "running_wf": 0, "pending_wf": 0})).endswith(
        "; nightly ETL cycle clean")
