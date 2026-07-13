"""Locks for r24 (v4.40.0): profile links as a pattern + review #4/#8.

The owner liked the drill's Snowsight profile link (2026-07-13) — it is a
shared helper now, wired everywhere a QUERY_ID surfaces. Plus the dead
cache gauge comes off the pain board, and the first live-tier downgrades
land behind a systemic post-action freshness guarantee.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def test_profile_link_helper_is_shared_and_honest():
    comp = (_ROOT / "app" / "ui" / "components.py").read_text(encoding="utf-8")
    body = comp.split("def snowsight_profile_column", 1)[1].split("\ndef ", 1)[0]
    assert "app.snowflake.com" in body and "/profile" in body
    assert "_ow_snowsight_ctx" in body                     # one context fetch per session
    assert "return df, {}" in body                         # honest degrade, never a dead link
    assert "LinkColumn" in body


def test_profile_links_ride_every_query_id_table():
    for rel, marker in (
        ("app/ui/pages/operations.py", 'snowsight_profile_column(top.df, _PAGE)'),
        ("app/ui/pages/cost_parts/optimize.py", 'snowsight_profile_column(edf_q, _PAGE)'),
        ("app/ui/pages/cost_parts/unit_costs.py", 'snowsight_profile_column(cdf, _PAGE)'),
        ("app/ui/pages/admin.py", 'snowsight_profile_column(rq.df, _PAGE)'),
    ):
        assert marker in (_ROOT / rel).read_text(encoding="utf-8"), rel


def test_dead_cache_gauge_is_off_the_pain_board():
    adm = (_ROOT / "app" / "ui" / "pages" / "admin.py").read_text(encoding="utf-8")
    assert '_tt[["PAGE", "P95_S", "SLOW_2S", "FAILED", "PAIN"]]' in adm
    # the by-page table keeps the column WITH its floor-not-census caption
    assert "CACHE_HIT_PCT" in adm and "a floor, not a census" in adm


def test_actions_bump_the_refresh_salt_and_tiers_downgrade_safely():
    q = (_ROOT / "app" / "core" / "query.py").read_text(encoding="utf-8")
    body = q.split("def execute_statement(sql: str, *, page: str) -> tuple", 1)[1].split("\ndef ", 1)[0]
    assert '_ow_refresh_salt' in body                      # post-action freshness, systemic
    adm = (_ROOT / "app" / "ui" / "pages" / "admin.py").read_text(encoding="utf-8")
    assert 'key="schema_version", tier="metadata"' in adm
    assert 'key="flyway_history", tier="recent"' in adm
    al = (_ROOT / "app" / "ui" / "pages" / "alerts.py").read_text(encoding="utf-8")
    assert 'key="alert_routes", tier="recent"' in al
    # the deliberately-live operator-edit surfaces STAY live (the audit rule)
    assert 'key="settings_table", tier="live"' in adm
    assert 'key="emg_budgets", tier="live"' in adm

def test_overview_budget_kpi_replaced_by_the_pace_kpi():
    """Owner 2026-07-13: 'replace Monthly budget with something else — I
    don't like having useless features.' The KPI is now MTD paced against
    the prior month's same first-N-days (no configuration needed, zero new
    queries — it reuses the 150d backtest frame); a configured budget
    survives as help-text context only."""
    ov = (_ROOT / "app" / "ui" / "pages" / "overview.py").read_text(encoding="utf-8")
    assert "budget_kpi" not in ov                        # the dead KPI is gone
    assert "_mtd_pace_kpi(mtd_spend, _bt_hist, rate, budget)" in ov
    assert "mtd_pace_vs_prior_month" in ov
    assert "MTD vs last month (same days)" in ov
    assert "Budget context" in ov                        # configured budgets still visible
    assert "never a fabricated 0%" in (
        (_ROOT / "app" / "logic" / "formulas.py").read_text(encoding="utf-8"))

