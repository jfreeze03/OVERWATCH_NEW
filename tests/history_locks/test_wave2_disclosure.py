"""Decision Studio Wave-2 disclosure fixes: #13 per-section filter contracts,
#15 top-N truncation disclosure on the portfolio board.
"""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PAGE = (_ROOT / "app" / "ui" / "pages" / "decision_studio.py").read_text(encoding="utf-8")
_BODY = (_ROOT / "app" / "ui" / "decision_studio.py").read_text(encoding="utf-8")
# v4.597 (Option C): the portfolio board is the Operations ▸ Optimize fix queue.
_OPTIMIZE = (_ROOT / "app" / "ui" / "pages" / "ops_parts" / "optimize_queue.py").read_text(encoding="utf-8")


def test_decision_page_has_per_section_filter_contracts():
    # #13: each section declares which page filters it honors, not one blanket contract
    assert "_contracts = {" in _PAGE
    assert "section_filter_contract(f, **_contracts[section])" in _PAGE
    # v4.597 (Option C): Proof (the ledger / run cost / acceptance / precision record) is
    # account-wide and ignores Company + Window; Pipeline (addressable $ + queued work) honors both.
    contracts = _PAGE.split("_contracts = {", 1)[1].split("\n    }", 1)[0]
    proof = contracts.split('"Proof":', 1)[1].split("},", 1)[0]
    assert '"applies": ()' in proof and "Account-wide" in proof
    pipeline = contracts.split('"Pipeline":', 1)[1]
    assert '"applies": ("company", "days")' in pipeline
    assert "account-wide" in pipeline                    # the measured slider defaults are not scoped
    for gone in ('"SLOs":', '"Experiments":', '"Scenarios":', '"Portfolio":'):
        assert gone not in contracts, gone


def test_portfolio_discloses_top_n_truncation():
    # #15: the capped portfolio board discloses it's a top-N, not the whole population
    # (v4.597: the board is Operations ▸ Optimize, capped at _QUEUE_CAP).
    assert "_QUEUE_CAP = 200" in _OPTIMIZE
    assert "len(portfolio) >= _QUEUE_CAP" in _OPTIMIZE
    assert "query families by measured credits" in _OPTIMIZE
    assert "_PORTFOLIO_CAP" not in _BODY and "def _portfolio" not in _BODY
