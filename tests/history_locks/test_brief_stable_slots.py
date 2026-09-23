"""rec6 (v4.585.0) — the Brief headline band has three FIXED slots (MTD spend / Open
criticals / Nightly cycle) that never reflow, with conditional cards demoted to a separate
secondary band. Source-scan lock (same idiom as test_brief_ref_gap_signal.py) so a
regression that floats the nightly slot or drops the export feed re-fails here."""

from __future__ import annotations

from pathlib import Path

_BRIEF = (Path(__file__).resolve().parents[2] / "app" / "ui" / "pages" / "brief.py").read_text(
    encoding="utf-8"
)


def test_headline_and_secondary_bands_exist() -> None:
    assert "headline = [" in _BRIEF and "secondary = [" in _BRIEF
    # Nightly cycle is an UNCONDITIONAL headline slot (fixed position), not a floating append.
    assert "headline.append(_nightly_card)" in _BRIEF
    # conditional context cards go to the secondary band, never the headline
    assert "secondary.append(" in _BRIEF
    assert "kpis.append(" not in _BRIEF


def test_nightly_cycle_has_an_honest_placeholder_when_unmonitored() -> None:
    # non-ETL accounts get a neutral 'info' / em-dash slot, never a green all-clear.
    assert '"label": "Nightly cycle"' in _BRIEF
    assert '"value": "—"' in _BRIEF
    assert '"delta": "ETL not monitored"' in _BRIEF


def test_two_bands_render_and_export_feed_is_preserved() -> None:
    assert "kpi_row(headline)" in _BRIEF
    assert "kpi_row(secondary)" in _BRIEF
    # the export still receives every displayed card (headline + secondary), dropping nothing
    assert "kpis = headline + secondary" in _BRIEF
    assert "for item in kpis" in _BRIEF
