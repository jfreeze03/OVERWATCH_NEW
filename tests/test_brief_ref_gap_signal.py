"""Locks for the Brief reference-data-gap signal (v4.506.0).

A source code with no XLAT translation hard-fails the nightly ETL load, so the
Operations ▸ Pipeline reference-gap check now also leads the Brief landing page:
a verdict-line signal + a red primary button that jumps to the panel listing the
codes. The Brief read MUST be config-gated and fail-silent (probe read) so an
unset config or a missing SELECT grant never shows a setup/grant hint or spams
APP_ERROR_LOG on the hottest page — the Operations panel owns those hints.
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


def test_brief_version_is_current():
    assert 'APP_VERSION = "4.506.0"' in (_ROOT / "app" / "config.py").read_text(encoding="utf-8")
