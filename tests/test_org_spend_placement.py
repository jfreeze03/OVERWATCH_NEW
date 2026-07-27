"""Lock: the org-spend surfaces live on Cost & Contract, not Admin (v4.48).

Moved 2026-07-15 (owner: "move them"). The Accounts Spend Summary and the
rate-card reconciliation are cost analysis, not app plumbing: Admin's other
eight sections are all about the app's own operation, while Contract &
Forecast is the designated ORGANIZATION_USAGE surface and the one Cost
section that is structurally company-agnostic — matching org-grain data.
Three references believed this before the code did (formulas.py F3 and two
ai_chargeback captions pointed at a Cost-page org rate-card panel that did
not exist there yet). The Admin placement was shipping convention from
2026-07-07, argued nowhere.
"""
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_ADMIN = (_ROOT / "app" / "ui" / "pages" / "admin.py").read_text(encoding="utf-8")
_CT = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "contract.py").read_text(encoding="utf-8")


def test_org_panels_render_on_contract():
    assert 'key="org_spend"' in _CT and "org_usage_in_currency" in _CT
    assert 'key="org_month_this"' in _CT and "org_account_month_usd" in _CT
    assert "Billing truth vs app model" in _CT


def test_admin_no_longer_hosts_them():
    assert "_org_spend_tab" not in _ADMIN
    assert '"Org spend"' not in _ADMIN
    assert "org_usage_in_currency" not in _ADMIN
    assert "org_account_month_usd" not in _ADMIN


def test_moved_panels_price_from_the_passed_settings():
    # cost.py loads SETTINGS once and passes the dict into _contract_tab; the
    # Admin copy re-called load_settings. The moved code must not regress to
    # a second settings read.
    assert "load_settings" not in _CT


def test_reconciliation_sits_with_the_rate_it_audits():
    # The audit renders before the pacing/steering/planner panels that price
    # at the audited CREDIT_PRICE_USD — drift is visible before the numbers
    # it distorts.
    assert _CT.index('key="org_month_this"') < _CT.index('key="steer_idle"')


def test_the_move_added_no_account_usage_literals():
    # contract.py is not perf-budgeted, but keep its live-scan surface honest:
    # the org panels bring ORGANIZATION_USAGE reads only. The single
    # pre-existing ACCOUNT_USAGE literal is the contract-consumed live
    # fallback's source label.
    assert _CT.count("ACCOUNT_USAGE") == 1
