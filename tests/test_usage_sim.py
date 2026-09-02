"""CI coverage for the headless usage simulator (tests/usage_sim.py).

Keeps the profiler honest: source classification is correct (including the account_usage
scan that merely CALLS a COMPANY_FOR_* UDF — it must NOT count as a mart read), the driver
renders every heavy page without error and records its reads, and the stubbed module list
still covers the pages it claims to (a new page module left unpatched would silently
undercount, so this guards drift against the render-contract harness it mirrors).
"""

from __future__ import annotations

import pytest

st = pytest.importorskip("streamlit")
import usage_sim  # noqa: E402
from packaging.version import parse as _parse_version  # noqa: E402

_APPTEST_BUTTONGROUP_OK = _parse_version(st.__version__) >= _parse_version("1.55.0")
_SUBSET = ["Brief", "Overview", "Operations", "Security"]


def test_classify_source_covers_the_axes():
    au = ("SELECT * FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY "
          "WHERE DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(WH) = 'ALFA'")
    assert usage_sim.classify_source(au) == "account_usage"          # AU wins over the UDF ref
    assert usage_sim.classify_source("SELECT * FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY") == "mart"
    assert usage_sim.classify_source("SHOW DATABASES") == "metadata"
    assert usage_sim.classify_source("SELECT * FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY())") == "metadata"
    assert usage_sim.classify_source("SELECT 1") == "other"
    # a mart read that also calls a company UDF is still a mart, not 'other'
    assert usage_sim.classify_source(
        "SELECT DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(D) FROM DBA_MAINT_DB.OVERWATCH.MART_X") == "mart"


def test_patched_module_set_covers_the_pages():
    """Drift guard: the simulator must patch the same UI modules the shaped-render harness
    does, or a page's reads go uncounted. Assert the known page modules are all covered."""
    _main, modules = usage_sim._patched_modules()
    names = {m.__name__ for m in modules}
    expected = {
        "app.main", "app.ui.components", "app.ui.ai_panel", "app.ui.decision_studio",
        "app.ui.security_center", "app.ui.workbench",
        "app.ui.pages.overview", "app.ui.pages.control_room", "app.ui.pages.cost",
        "app.ui.pages.operations", "app.ui.pages.alerts", "app.ui.pages.security",
        "app.ui.pages.admin", "app.ui.pages.brief", "app.ui.pages.ask",
        "app.ui.pages.decision_studio",
        "app.ui.pages.cost_parts.ai_chargeback", "app.ui.pages.cost_parts.compare",
        "app.ui.pages.cost_parts.contract", "app.ui.pages.cost_parts.optimize",
        "app.ui.pages.cost_parts.spend", "app.ui.pages.cost_parts.unit_costs",
    }
    assert expected <= names, f"unpatched modules: {expected - names}"


@pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")
def test_simulate_renders_pages_and_records_reads():
    report = usage_sim.simulate(pages=_SUBSET, scopes={"default": {}}, measure_rerun=False)
    flows = report["flows"]
    assert len(flows) == len(_SUBSET)
    for f in flows:
        # every page renders cleanly (the recording stubs return shaped frames, so populated
        # branches run — same guarantee as the render-contract harness)
        assert not f["error"], f"{f['page']}: {f['error']}"
        # the source buckets partition the total
        assert f["account_usage"] + f["mart"] + f["metadata"] + f["other"] == f["total"]
        # chattiness ceiling: catches a page that suddenly issues far more queries per render
        assert f["total"] <= 60, f"{f['page']} issued {f['total']} cold queries"
    # the mart-heavy pages actually recorded reads (proves the stubs were installed)
    by_page = {f["page"]: f for f in flows}
    assert by_page["Overview"]["total"] > 0
    assert by_page["Operations"]["total"] > 0


@pytest.mark.skipif(not _APPTEST_BUTTONGROUP_OK, reason="streamlit<1.55 AppTest ButtonGroup bug")
def test_report_formats_without_error():
    report = usage_sim.simulate(pages=["Brief"], scopes={"default": {}}, measure_rerun=False)
    text = usage_sim.format_report(report)
    assert "queries per interaction" in text
    assert "Brief" in text
