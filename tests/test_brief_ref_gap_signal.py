"""Locks for the Brief ETL signals (ref-data gap v4.506.0; failed ETL task v4.509.0).

Two ETL fires now lead the Brief landing page: a source code with no XLAT translation
(hard-fails the nightly load), and a FAILED task in the latest Informatica run (breaks a
downstream load). Each is a verdict-line signal + a red primary button that jumps to the
Operations ▸ Pipeline panel with the detail. Both reads MUST be config-gated and
fail-silent (probe read) so unset config or a missing SELECT grant never shows a
setup/grant hint or spams APP_ERROR_LOG on the hottest page — the Operations panels own
those hints. The failed-task signal reuses the workflow-runtimes scan + the shared
FAILED_TASK_STATUSES set, so the Brief and the panel never disagree.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_BRIEF = (_ROOT / "app" / "ui" / "pages" / "brief.py").read_text(encoding="utf-8")


def test_brief_reuses_the_operations_ref_gap_scan():
    # reuse, not a fork: the Brief must call the SAME builder the panel uses
    assert "from app.data import etl_control_sql" in _BRIEF
    assert "def _reference_gap_summary(settings: dict)" in _BRIEF
    assert "etl_control_sql.parse_ref_gap_checks(raw)" in _BRIEF
    assert "etl_control_sql.reference_gap_scan(checks, xlat)" in _BRIEF
    # account-wide on the Brief: it must NOT narrow by the scope-bar Database
    assert "filter_checks_by_database" not in _BRIEF


def test_brief_ref_gap_read_is_config_gated_and_fail_silent():
    # config-gated: unset XLAT or checks returns early, before any query
    assert "if not xlat or not raw:\n        return 0, \"\"" in _BRIEF
    # fail-silent: a probe read so a missing grant is an expected absence
    # (neither error-logged nor counted as a failed fetch), not a Brief banner
    assert "probe=True" in _BRIEF
    # only surfaces on a real, non-empty gap
    assert "res.ok and not res.empty" in _BRIEF


def test_brief_surfaces_the_gap_in_verdict_and_a_jump_button():
    # worst-first verdict signal names the shortfall
    assert 'Signal(\n            "bad", f"{_ref_gap_n} source {_rg_word} missing XLAT translation' in _BRIEF
    # a primary button (same idiom as the undelivered-critical banner) that jumps
    # straight to the Operations panel listing the exact codes to translate
    assert 'key="brief_ref_gap"' in _BRIEF
    assert 'type="primary"' in _BRIEF
    assert 'request_navigation("Operations", "Pipeline SLA")' in _BRIEF


def test_brief_surfaces_failed_etl_task_signal():
    # a FAILED task in the latest ETL run leads the Brief like the ref-gap fire
    assert "def _workflow_failure_summary(settings: dict)" in _BRIEF
    # reuses the SAME workflow-runtimes scan + shared failure set (no fork, no disagreement)
    assert "etl_control_sql.workflow_runtimes_scan(fqn)" in _BRIEF
    assert "etl_control_sql.FAILED_TASK_STATUSES" in _BRIEF
    # config-gated + fail-silent probe read, only fires on a real failure
    assert 'get("ETL_CONTROL_STATUS_FQN")' in _BRIEF
    assert "probe=True" in _BRIEF
    # verdict signal + its own jump button
    assert 'failed ETL {_wf_word} in the latest run' in _BRIEF
    assert 'key="brief_wf_fail"' in _BRIEF
