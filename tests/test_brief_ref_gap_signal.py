"""Locks for the Brief ETL signals (ref-data gap v4.506.0; failed ETL task v4.509.0; whole night v4.589.0).

Two ETL fires lead the Brief landing page: a source code with no XLAT translation (hard-fails the
nightly load), and a FAILED task — or a regular workflow that did not run — anywhere in TONIGHT's
Informatica cycle (breaks a downstream load). Each is a verdict-line signal + a red primary button that
jumps to the Operations ▸ Pipeline panel with the detail. Both reads MUST be config-gated and
fail-silent (probe read) so unset config or a missing SELECT grant never shows a setup/grant hint or
spams APP_ERROR_LOG on the hottest page — the Operations panels own those hints.

Next-Fifty #1: the reads moved to the shared app/ui/attention.py (ONE read path for the Brief and the
Control Room), and the signals to verdict.attention_signals — so these locks point there.
"""

from __future__ import annotations

from pathlib import Path

from app.logic.verdict import AttentionBundle, Signal, attention_signals

_ROOT = Path(__file__).resolve().parents[1]
_BRIEF = (_ROOT / "app" / "ui" / "pages" / "brief.py").read_text(encoding="utf-8")
_ATTN = (_ROOT / "app" / "ui" / "attention.py").read_text(encoding="utf-8")


def test_brief_reuses_the_operations_ref_gap_scan():
    # reuse, not a fork: the shared read calls the SAME builder the panel uses
    assert "attention.etl_attention(settings, page=_PAGE)" in _BRIEF
    assert "from app.data import etl_control_sql" in _ATTN
    assert "def reference_gap_summary(settings: dict, *, page: str)" in _ATTN
    assert "etl_control_sql.parse_ref_gap_checks(raw)" in _ATTN
    assert "etl_control_sql.reference_gap_scan(checks, xlat)" in _ATTN
    # account-wide on the Brief: it must NOT narrow by the scope-bar Database
    assert "filter_checks_by_database" not in _ATTN and "filter_checks_by_database" not in _BRIEF


def test_brief_ref_gap_read_is_config_gated_and_fail_silent():
    # config-gated: unset XLAT or checks returns early, before any query
    assert "if not xlat or not raw:\n        return 0, \"\"" in _ATTN
    # fail-silent: a probe read so a missing grant is an expected absence
    # (neither error-logged nor counted as a failed fetch), not a Brief banner
    assert "probe=True" in _ATTN
    # only surfaces on a real, non-empty gap
    assert "res.ok and not res.empty" in _ATTN


def test_brief_surfaces_the_gap_in_verdict_and_a_jump_button():
    # worst-first verdict signal names the shortfall (now composed by the shared attention signals)
    sigs = attention_signals(AttentionBundle(open_crit=0, stale_sources=0, open_incidents=0,
                                             ref_gap_n=2, ref_gap_label="pc_uwissuetype.code"))
    assert Signal("bad", "2 source codes missing XLAT translation (pc_uwissuetype.code)") in sigs
    # a primary button (same idiom as the undelivered-critical banner) that jumps
    # straight to the Operations panel listing the exact codes to translate
    assert 'key="brief_ref_gap"' in _BRIEF
    assert 'type="primary"' in _BRIEF
    assert 'request_navigation("Operations", "Pipeline SLA")' in _BRIEF


def test_brief_surfaces_whole_night_etl_signals():
    # the whole-night roll-up (every workflow tonight) replaced the single-RUN_ID read
    assert "def cycle_night_read(settings: dict, *, page: str)" in _ATTN
    assert "etl_control_sql.cycle_night_health_scan(" in _ATTN
    assert 'get("ETL_CONTROL_STATUS_FQN")' in _ATTN
    assert "probe=True" in _ATTN
    assert "workflow_runtimes_scan" not in _BRIEF and "workflow_runtimes_scan" not in _ATTN
    assert "_workflow_failure_summary" not in _BRIEF
    sigs = attention_signals(AttentionBundle(
        open_crit=0, stale_sources=0, open_incidents=0,
        night={"failed_tasks": 3, "failed_label": "WF_A, WF_B", "missing_wf": 1, "missing_label": "WF_C"}))
    assert Signal("bad", "3 failed ETL tasks tonight (WF_A, WF_B)") in sigs
    assert Signal("bad", "1 nightly workflow did not run (WF_C)") in sigs
    # each fire has its own jump button
    assert 'key="brief_wf_fail"' in _BRIEF
    assert 'key="brief_wf_missing"' in _BRIEF
