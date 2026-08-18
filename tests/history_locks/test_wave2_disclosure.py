"""Decision Studio Wave-2 disclosure fixes: #13 per-section filter contracts,
#15 top-N truncation disclosure on the portfolio board.
"""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PAGE = (_ROOT / "app" / "ui" / "pages" / "decision_studio.py").read_text(encoding="utf-8")
_BODY = (_ROOT / "app" / "ui" / "decision_studio.py").read_text(encoding="utf-8")


def test_decision_page_has_per_section_filter_contracts():
    # #13: each section declares which page filters it honors, not one blanket contract
    assert "_contracts = {" in _PAGE
    assert "section_filter_contract(f, **_contracts[section])" in _PAGE
    # SLOs / Experiments ignore the page Company+Window; Scenarios is Company-only
    slos = _PAGE.split('"SLOs":', 1)[1].split("},", 1)[0]
    assert '"applies": ()' in slos
    experiments = _PAGE.split('"Experiments":', 1)[1].split("},", 1)[0]
    assert '"applies": ()' in experiments
    scenarios = _PAGE.split('"Scenarios":', 1)[1].split("},", 1)[0]
    assert '"applies": ("company",)' in scenarios


def test_portfolio_discloses_top_n_truncation():
    # #15: the capped portfolio board discloses it's a top-N, not the whole population
    assert "_PORTFOLIO_CAP" in _BODY
    assert "len(portfolio) >= _PORTFOLIO_CAP" in _BODY
    assert "query families by measured credits" in _BODY
