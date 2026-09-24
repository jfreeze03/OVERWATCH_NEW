"""Next-Fifty #3 (v4.588.0): the "Pays for itself" ROI multiple must not fall to 0x on the first
day of every quarter. Its numerator is now the ACTIVE verified monthly run-rate (every item
VERIFIED in the last SAVINGS_ACTIVE_MONTHS months), not this quarter's sum — each VERIFIED_USD is a
monthly run-rate that keeps saving after its quarter, while the trailing-30d run cost never resets."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import sqlglot

import app.ui.decision_studio as ds
from app.config import SAVINGS_ACTIVE_MONTHS
from app.core.result import QueryResult
from app.data import mart_sql
from app.logic.proof import proof_verdict

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_summary_builder_carries_active_run_rate():
    sql = mart_sql.savings_summary_quarter()
    for col in ("VERIFIED_QTD_USD", "VERIFIED_ITEMS", "VERIFIED_ACTIVE_MONTHLY_USD",
                "VERIFIED_ACTIVE_ITEMS", "ESTIMATED_OPEN_USD"):
        assert f"AS {col}" in sql
    # the active window anchors on the ACCOUNT clock, N months back
    assert f"DATEADD('month', -{SAVINGS_ACTIVE_MONTHS}, " in sql
    assert "CURRENT_DATE()" not in sql
    parsed = sqlglot.parse_one(sql, read="snowflake")
    assert {e.alias_or_name for e in parsed.expressions} >= {"VERIFIED_ACTIVE_MONTHLY_USD", "VERIFIED_QTD_USD"}


def test_proof_signals_roi_uses_active_run_rate_on_oct_1(monkeypatch):
    # The morning of Oct 1: nothing verified THIS quarter yet, $500/mo verified run-rate still active.
    ds.reset_proof_memo()

    def _qr(df):
        return QueryResult(df=df, ok=True, source="t")

    batch = {
        "sc_quarter": _qr(pd.DataFrame({"VERIFIED_QTD_USD": [0.0], "VERIFIED_ITEMS": [0],
                                        "VERIFIED_ACTIVE_MONTHLY_USD": [500.0],
                                        "VERIFIED_ACTIVE_ITEMS": [3], "ESTIMATED_OPEN_USD": [0.0]})),
        "sc_appcost": _qr(pd.DataFrame({"APP_CREDITS_30D": [10.0]})),
        "sc_accept": _qr(pd.DataFrame()),
    }
    ledger = pd.DataFrame([{"STATE": "VERIFIED", "ESTIMATED_USD": 600, "VERIFIED_USD": 500,
                            "CREATED_AT": "2026-08-01", "VERIFIED_AT": "2026-09-01"}])
    monkeypatch.setattr(ds, "run_batch", lambda specs, **_k: batch)
    monkeypatch.setattr(ds, "run", lambda sql, **k: _qr(ledger if k.get("key") == "decision_roi_ledger_full"
                                                        else pd.DataFrame()))
    try:
        sig = ds._proof_signals(3.68)
    finally:
        ds.reset_proof_memo()
    assert sig["roi"]["VERIFIED_USD"] == 500.0
    assert sig["roi"]["RATIO"] == round(500 / (10.0 * 3.68), 1)
    assert sig["roi"]["PAYS"] is True
    assert sig["verified_qtd"] == 0.0                    # the quarter KPI restarted; the ROI did not
    verdict = proof_verdict(sig["roi"], None, None, {"PRECISION_PCT": None, "UNTAGGED_SHARE_PCT": 0.0})
    assert "run cost not yet covered" not in verdict["headline"]


def test_surfaces_wire_the_active_numerator():
    d = _src("app/ui/decision_studio.py")
    assert "roi_multiple(verified_active, run_cost)" in d
    assert "roi_multiple(verified_qtd, run_cost)" not in d
    assert 'get("VERIFIED_ACTIVE_MONTHLY_USD")' in d
    b = _src("app/ui/pages/brief.py")
    assert 'rrow.get("VERIFIED_ACTIVE_MONTHLY_USD")' in b
    assert '"Verified savings (QTD)"' not in b
    assert b.count("ACCOUNT_USAGE") == 0                 # the Brief perf budget stays 0
