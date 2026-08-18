"""rec#41: the Optimize 'most expensive queries' panel offers the exact measured
(QUERY_ATTRIBUTION_HISTORY) lens alongside the hour-share allocated estimate,
instead of siloing the measured view on the Unit-costs tab.
"""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_OPT = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "optimize.py").read_text(encoding="utf-8")


def test_optimize_offers_both_allocated_and_measured_lenses():
    # the allocated (hour-share) estimate stays...
    assert "expensive_queries_usd(" in _OPT
    # ...and the measured (exact QAH) lens is now offered on the same panel
    assert "measured_query_costs(" in _OPT
    assert "cost_measq_toggle" in _OPT
    assert "MEASURED_USD" in _OPT


def test_measured_panel_is_labelled_measured_not_allocated():
    block = _OPT.split("cost_measq_toggle", 1)[0][-900:]
    assert "Measured, not allocated" in block
    assert "idle" in block.lower()   # the key distinction: measured excludes warehouse idle


def test_measured_panel_honors_the_active_object_filters():
    block = _OPT.split("measured_query_costs(", 1)[1][:400]
    assert "flt_database" in block
    assert "flt_schema_contains" in block
