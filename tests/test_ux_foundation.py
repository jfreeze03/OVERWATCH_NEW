"""UX foundation: deep-link targets, filter application, formats, palettes."""

from app.logic import navigate
from app.ui import status_colors


def test_investigation_targets_known_rules():
    t = navigate.investigation_target(
        "COST_CLOUD_SVC_RATIO", "WH_TRXS_TRANSFORM cloud-services ratio 31.2% (24h)")
    assert t["page"] == "Cost & Contract" and t["section"] == "Spend"
    assert t["filters"]["warehouse_contains"] == "WH_TRXS_TRANSFORM"

    t = navigate.investigation_target(
        "PIPE_COPY_FAILURES", "TRXS_DW.STAGING.CLAIMS: 4 failed file load(s) (24h)")
    assert t["page"] == "Operations" and t["section"] == "Pipeline SLA"
    assert t["filters"]["database"] == "TRXS_DW"


def test_investigation_falls_back_by_family_prefix():
    t = navigate.investigation_target("COST_SOMETHING_NEW", "no entities here")
    assert t["page"] == "Cost & Contract"
    assert navigate.investigation_target("SEC_NEW_RULE")["page"] == "Security"
    assert navigate.investigation_target("UNKNOWN")["page"] == "Overview"


def test_section_keys_cover_all_lazy_pages():
    assert set(navigate.PAGE_SECTION_KEYS) == {
        "Cost & Contract", "Operations", "Security", "Alerts", "Admin"}


def test_light_palette_covers_every_dark_pair():
    pairs = set(status_colors.STATUS_COLOR_MAP.values()) | set(status_colors._VERDICTS.values())
    missing = pairs - set(status_colors._LIGHT_EQUIV)
    assert not missing, f"dark pairs without a light equivalent: {missing}"


def test_status_css_still_emits_css_outside_streamlit_runtime():
    css = status_colors.status_css("SEVERITY", "CRITICAL")
    assert "background-color" in css and "font-weight" in css


def test_auto_formats_by_convention():
    import pandas as pd

    from app.ui.components import _auto_formats

    df = pd.DataFrame({
        "SPEND_USD": [1234.5], "CREDITS_BILLED": [12.3456], "CLOUD_SVC_PCT": [31.25],
        "RUNS": [1500.0], "P95_S": [12.34], "WAREHOUSE_NAME": ["WH_X"], "CONFIGURED": [7.0],
    })
    fmts = _auto_formats(df, skip=set())
    assert fmts["SPEND_USD"] == "${:,.2f}"
    assert fmts["CREDITS_BILLED"] == "{:,.2f}"
    assert fmts["RUNS"] == "{:,.0f}"
    assert fmts["P95_S"] == "{:,.1f}"
    assert "WAREHOUSE_NAME" not in fmts and "CONFIGURED" not in fmts
    # column_config wins over convention
    assert "SPEND_USD" not in _auto_formats(df, skip={"SPEND_USD"})
