"""CoCo do-first: the persistent contract-runway bar (Overview #20 / Cost #4)."""
from pathlib import Path

import pandas as pd

from app.logic.formulas import contract_runway

_ROOT = Path(__file__).resolve().parents[1]


def _row(**kw):
    return pd.Series(kw)


def test_contract_runway_computes_pct_days_and_decide_by():
    rw = contract_runway(
        _row(TOTAL=1000.0, CONSUMED=750.0, DAYS_LEFT=45, EXHAUST_DATE="2026-10-14"),
        lead_days=30,
    )
    assert rw["pct_consumed"] == 75.0
    assert rw["days_left"] == 45.0
    assert rw["exhaust_date"] == "2026-10-14"
    assert rw["decide_by"] == "2026-09-14"   # exhaust minus the 30-day lead time
    assert rw["severity"] == "warn"          # 31-90 days left


def test_contract_runway_severity_bands():
    assert contract_runway(_row(TOTAL=100, CONSUMED=90, DAYS_LEFT=20,
                                EXHAUST_DATE="2026-09-05"))["severity"] == "bad"
    assert contract_runway(_row(TOTAL=100, CONSUMED=10, DAYS_LEFT=200,
                                EXHAUST_DATE="2027-03-01"))["severity"] == "ok"


def test_contract_runway_none_when_unconfigured():
    assert contract_runway(None) is None
    assert contract_runway(_row(TOTAL=0, CONSUMED=0, DAYS_LEFT=-1, EXHAUST_DATE=None)) is None


def test_contract_runway_overrun_is_bad_and_pct_capped():
    rw = contract_runway(_row(TOTAL=100, CONSUMED=150, DAYS_LEFT=-1, EXHAUST_DATE=None))
    assert rw["pct_consumed"] == 100.0                 # capped, never over 100%
    assert rw["exhaust_date"] is None and rw["decide_by"] is None
    assert rw["severity"] == "bad"                     # already over the commitment


def test_overview_and_brief_render_the_runway_bar():
    for rel in ("overview.py", "brief.py"):
        src = (_ROOT / "app" / "ui" / "pages" / rel).read_text(encoding="utf-8")
        assert "contract_runway_bar(" in src and "contract_runway(" in src, rel
