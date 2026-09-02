"""Regression locks for bug-hunt round 18 — cross-page presentation consistency + twin-divergence.

WLA-1(AI)  The Spend tab (round 13) relabels its tiles "last month" when the global window
           bounds the read to the previous calendar month; the sibling AI/Cortex/CoCo/Chargeback
           tabs on the SAME Cost & Contract page still hard-coded the trailing "{days}d", so under
           "Last month" scope they named a trailing N-day window ending today — a different window
           than the bounded data, disagreeing with the scope chip and the Spend tiles. Fix: the
           same `_wlab`/`_when` derivation on every AI/Cortex/CoCo/Chargeback label.
RECHECK    The Alerts "Re-check condition now" button help was one shared string promising
           "TODAY's data", but PERF_QUERY_FAIL_PCT re-checks a rolling trailing-24h window (the
           other four rules use since-midnight). Fix: recheck_window_phrase(rule_id) asks the
           builder which window a rule actually filters, so the help can never drift from the SQL.
MFA-CANON  The MFA-gap "active user" filter is COALESCE(U.DISABLED, FALSE) = FALSE app-wide (the
           worklist, its live fallback, the count fallback, and insights) so a NULL DISABLED user
           who password-logs-in without MFA is counted. This locks that live-side invariant (the
           mart-loader's bare `U.DISABLED = FALSE` divergence is tracked separately for an owner
           migration; it must never be "reconciled" by breaking these live sites down to bare).
"""

from __future__ import annotations

from pathlib import Path

from app.data import recheck_sql

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- WLA-1(AI): AI/Cortex/CoCo/Chargeback tabs relabel "last month" like the Spend tab ----
def test_ai_chargeback_tabs_use_bounded_window_label_not_raw_days():
    src = _src("app/ui/pages/cost_parts/ai_chargeback.py")
    # each of the three bounded tabs (_ai_users_tab, _token_economics_panel, _chargeback_tab)
    # derives the same honest label the Spend tab uses.
    assert src.count('_wlab = "last month" if bounds is not None else f"{days}d"') >= 3
    assert '_when = "last month" if bounds is not None else f"in the last {days} days"' in src
    # the fixed labels/captions read from _wlab/_when ...
    assert 'f"Active AI users ({_wlab})"' in src
    assert 'f"Credits ({_wlab})"' in src
    assert 'f"Chargeback total ({_wlab})"' in src
    assert "Peer-relative, {_wlab}." in src
    assert "for this company {_when} — " in src
    # ... and none of the raw trailing-window forms survive on these sibling tabs.
    for stale in (
        'f"Active AI users ({days}d)"',
        'f"Credits ({days}d)"',
        'f"Chargeback total ({days}d)"',
        "Peer-relative, {days}d.",
        "for this company in the last {days} days",  # the caption now reads from _when
        "Asked window was {days}d",
    ):
        assert stale not in src, stale


# --- RECHECK: the drawer button help window phrase matches the SQL the builder emits -------
def test_recheck_window_phrase_matches_builder_sql_for_every_rule():
    for rid in recheck_sql.RECHECKABLE:
        sql = recheck_sql.recheck_sql(rid, warehouse="OW_WH", company="ALFA")
        assert sql is not None, rid
        phrase = recheck_sql.recheck_window_phrase(rid)
        if "DATEADD('hour', -24" in sql:
            # rolling trailing-24h basis (matches the alert definition)
            assert phrase == "the last 24h of data", rid
            assert "CURRENT_DATE()" not in sql, rid
        else:
            # since account-midnight = "today"
            assert "START_TIME >= CURRENT_DATE()" in sql, rid
            assert phrase == "today's data", rid


def test_recheck_window_phrase_is_robust_to_case_and_unknown_rules():
    assert recheck_sql.recheck_window_phrase("  perf_query_fail_pct ") == "the last 24h of data"
    assert recheck_sql.recheck_window_phrase("COST_WH_DAILY_CREDITS") == "today's data"
    assert recheck_sql.recheck_window_phrase("") == "today's data"
    assert recheck_sql.recheck_window_phrase("NOT_A_RULE") == "today's data"


def test_alerts_button_help_reads_the_window_from_the_builder():
    src = _src("app/ui/pages/alerts.py")
    assert "recheck_sql.recheck_window_phrase(_rid)" in src
    assert "against TODAY's data" not in src          # the old hard-coded, sometimes-wrong claim
    assert 'source="live re-check (today)"' not in src  # and the matching source label


# --- MFA-CANON: the live MFA-gap active-user filter stays COALESCE app-wide ---------------
def test_mfa_gap_active_filter_is_coalesce_across_all_live_sites():
    for rel in ("app/data/security_sql.py", "app/data/insights_sql.py"):
        src = _src(rel)
        # no bare form: NULL DISABLED must resolve to "active" (counted), not dropped.
        assert "U.DISABLED = FALSE" not in src, rel
    sec = _src("app/data/security_sql.py")
    # the three security MFA/active-user filters + the count fallback all COALESCE.
    assert sec.count("COALESCE(U.DISABLED, FALSE) = FALSE") >= 3
    assert "COALESCE(U.DISABLED, FALSE) = FALSE" in _src("app/data/insights_sql.py")
