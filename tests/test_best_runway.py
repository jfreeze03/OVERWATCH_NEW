"""Next-Fifty #19 (v4.589.0, cost-08): the exec runway (Brief card + verdict, Overview bar, Cost verdict)
comes from BILLING TRUTH — ORGANIZATION_USAGE.REMAINING_BALANCE_DAILY, storage and transfer included —
whenever it is readable and fresh, and falls back to the configured-credits model otherwise. The basis is
badged, a >15% gap between the two is disclosed, and the COST_CONTRACT_BREACH alert stays on credits."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from app.data import mart_sql
from app.logic import contract_planner
from app.logic import metric_registry as mr
from app.logic.contract_planner import best_runway, runway_basis_note

_ROOT = Path(__file__).resolve().parents[1]
_TODAY = date(2026, 9, 24)
_CREDITS = {"pct_consumed": 55.0, "days_left": 200.0, "exhaust_date": "2027-04-12",
            "decide_by": "2027-03-13", "severity": "ok", "lead_days": 30}


def _frame(values, *, end: date, currency: str | None = None) -> pd.DataFrame:
    days = pd.date_range(end=pd.Timestamp(end), periods=len(values), freq="D")
    df = pd.DataFrame({"DAY": days, "TOTAL_REMAINING": values, "ON_DEMAND_CONSUMPTION_BALANCE": 0.0})
    if currency:
        df["CURRENCY"] = currency
    return df


def test_balance_basis_preferred_when_fresh():
    b = best_runway(_frame([1000, 990, 980, 970, 960], end=_TODAY - timedelta(days=1)), _CREDITS, today=_TODAY)
    assert b["basis"] == "balance" and b["pct_consumed"] is None
    assert b["days_left"] == 96 - 1                    # runway from today (minus the 1-day as-of lag)
    assert b["severity"] == "ok"                       # 95 days > the 90-day warn band
    assert b["alt_days_left"] == 200 and b["gap_disclose"] is True
    assert b["as_of"] == (_TODAY - timedelta(days=1)).isoformat()


def test_stale_balance_falls_back_to_credits():
    b = best_runway(_frame([1000, 990, 980], end=_TODAY - timedelta(days=5)), _CREDITS, today=_TODAY)
    assert b["basis"] == "credits" and "stale" in b["fallback_reason"]
    assert b["pct_consumed"] == 55.0 and b["days_left"] == 200.0     # the credits dict is preserved


def test_no_burn_or_unusable_balance_falls_back():
    for bal in (_frame([500, 500, 500], end=_TODAY), pd.DataFrame(), None):
        assert best_runway(bal, _CREDITS, today=_TODAY)["basis"] == "credits"
    assert best_runway(None, None, today=_TODAY) is None


def test_exhausted_balance_is_zero_days_bad():
    b = best_runway(_frame([30, 20, 10, 0], end=_TODAY), _CREDITS, today=_TODAY)
    assert b["days_left"] == 0.0 and b["severity"] == "bad"


def test_gap_within_15pct_not_disclosed():
    b = best_runway(_frame([1000, 990, 980, 970, 960], end=_TODAY), {**_CREDITS, "days_left": 100.0},
                    today=_TODAY)
    assert b["days_left"] == 96 and b["gap_disclose"] is False


def test_non_usd_balance_still_yields_days():
    b = best_runway(_frame([1000, 990, 980], end=_TODAY, currency="EUR"), None, today=_TODAY)
    assert b["basis"] == "balance" and b["currency"] == "EUR" and b["days_left"] == 98


def test_horizon_no_overflow():
    b = best_runway(_frame([1e12, 1e12 - 1], end=_TODAY), None, today=_TODAY)
    assert b["basis"] == "balance" and b["exhaust_date"] is None and b["decide_by"] is None


def test_runway_basis_note():
    bal = best_runway(_frame([1000, 990, 980, 970, 960], end=_TODAY), _CREDITS, today=_TODAY)
    note = runway_basis_note(bal)
    assert "COST_CONTRACT_BREACH" in note and "CONTRACT_CREDITS" in note        # gap disclosed
    close = best_runway(_frame([1000, 990, 980, 970, 960], end=_TODAY), {**_CREDITS, "days_left": 100.0},
                        today=_TODAY)
    assert "CONTRACT_CREDITS" not in runway_basis_note(close)
    cred = best_runway(None, _CREDITS, today=_TODAY)
    assert "billing balance not readable" in runway_basis_note(cred)
    for n in (note, runway_basis_note(close), runway_basis_note(cred)):
        assert "$" not in n
    assert runway_basis_note(None) == ""


def test_exec_pages_share_one_org_balance_read():
    for rel in ("app/ui/pages/brief.py", "app/ui/pages/overview.py", "app/ui/pages/cost.py"):
        src = (_ROOT / rel).read_text(encoding="utf-8")
        assert "org_balance_result(_PAGE)" in src and "contract_planner.best_runway(" in src, rel
    c = (_ROOT / "app/ui/pages/cost_parts/contract.py").read_text(encoding="utf-8")
    assert "cost_sql.org_remaining_balance(contract_planner.ORG_BALANCE_DAYS)" in c
    assert "probe=True" in c and "_ow_org_balance_absent" in c
    assert contract_planner.ORG_BALANCE_DAYS == 120


def test_breach_alert_basis_untouched():
    assert "DAY BETWEEN DATEADD('day', -30, CURRENT_DATE())" in mart_sql.contract_exhaustion()


def test_runway_bar_label_only_for_balance(monkeypatch):
    from app.ui import components
    seen: list[str] = []
    monkeypatch.setattr(components.st, "markdown", lambda html, **_k: seen.append(html))
    components.contract_runway_bar({"pct_consumed": None, "basis_label": "billing balance",
                                    "as_of": "2026-09-20", "days_left": 40.0, "severity": "warn",
                                    "exhaust_date": "2026-11-02", "decide_by": "2026-10-03"})
    assert "ow-runway__label--solo" in seen[-1] and "billing balance" in seen[-1]
    assert "ow-runway__track" not in seen[-1]
    components.contract_runway_bar(_CREDITS)
    assert "ow-runway__track" in seen[-1] and "% of contract consumed" in seen[-1]


def test_metric_registry_names_the_balance_runway():
    m = mr.get("contract_balance_runway")
    assert m is not None and m.method == mr.BILLED
    assert mr.validate() == []
