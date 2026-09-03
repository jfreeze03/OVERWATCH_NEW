"""Regression locks for bug-hunt round 19.

WLA-1(sweep)  The app-wide WLA-1 sweep found 9 more tile/column/caption labels that hard-coded the
              trailing "{days}d" while their reads are bounded to the prior calendar month under
              "Last month" scope (overview badge, Operations Queries/Task-runs/Wasted-spend,
              Unit-costs AI-spend, Optimize Idle-waste/QAS/remediation, the missed Cortex-spend
              tab). Each now reads "last month" when bounds is set.
CoCo-REVERSE  v4.452 relabeled the CoCo efficiency credit columns to "last month" but coco_efficiency
              windowed the credit frame as a TRAILING span (no bounds) — the label was then wrong in
              reverse. coco_efficiency now honors bounds (bounded calendar window, mirroring
              cortex._window_slice) so the data matches the label and the sibling AI-users tile.
ROLE-TWIN     The live role_share_within_warehouse filtered EXECUTION_STATUS='SUCCESS' while the mart
              twin counts all statuses; the allocated warehouse credits include failed-query compute,
              so the share must be all-status too. Dropped the filter (mart is right).
UNIT-$        The per-call KPIs used format_usd, which collapses sub-cent $/call to "$0.00" and
              contradicts the $%.4f leaderboard row. New format_usd_precise preserves sub-$1 precision.
SPILL-FMT     SPILL_GB_PER_DAY had %.2f on Optimize but the Styler default (6dp) on Operations for the
              same frame; Operations now formats it %.2f too.
POSTURE-CRON  The posture panels claimed a "06:30" daily load; the mart actually loads after the 06:45
              nightly chain (06:30 is the unrelated storage-truth task). Captions corrected.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from app.data import chargeback_sql, mart27_sql
from app.logic.formulas import format_usd, format_usd_precise
from app.logic.wave2 import coco_efficiency

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- WLA-1 sweep: every remaining trailing-window label switches to "last month" when bounded ----
def test_overview_company_economics_badge_switches_to_last_month():
    src = _src("app/ui/pages/overview.py")
    assert '_ce_wlab = "last month" if _ov_bounds is not None else f"{days}d"' in src
    assert "badge=f\"{company} · {_ce_wlab}\"" in src
    assert 'badge=f"{company} · {days}d"' not in src


def test_operations_window_labels_switch_to_last_month():
    src = _src("app/ui/pages/operations.py")
    assert '_q_wlab = "last month" if bounds is not None else f"{_served_days}d"' in src
    assert 'f"Queries ({_q_wlab})"' in src
    assert '_tr_wlab = "last month" if bounds is not None else f"{days}d"' in src
    assert 'f"Task runs ({_tr_wlab})"' in src
    assert '"last month" if bounds is not None else f"{days}d"' in src  # wasted-spend _scope_lbl
    # the raw trailing forms are gone
    assert 'f"Queries ({_served_days}d)"' not in src
    assert 'f"Task runs ({days}d)"' not in src


def test_unit_costs_ai_spend_label_switches_to_last_month():
    src = _src("app/ui/pages/cost_parts/unit_costs.py")
    assert '_ai_wlab = "last month" if bounds is not None else f"{_ai_days}d"' in src
    assert 'f"AI spend ({_ai_wlab})"' in src
    assert 'f"AI spend ({_ai_days}d)"' not in src


def test_optimize_window_labels_switch_to_last_month():
    src = _src("app/ui/pages/cost_parts/optimize.py")
    assert '_iw_wlab = "last month" if bounds is not None else f"{_iw_days}d"' in src
    assert 'f"Idle credit waste ({_iw_wlab})"' in src
    assert "'last month' if bounds is not None else f'{days}d'" in src  # QAS
    assert '_rw_wlab = "last month" if bounds is not None else f"{remed_days}d"' in src
    assert 'f"Idle credit waste ({_iw_days}d)"' not in src
    assert 'f"QAS spend ({days}d)"' not in src
    assert "Idle credits in window ({remed_days}d)" not in src


def test_cortex_spend_tab_label_switches_to_last_month():
    src = _src("app/ui/pages/cost_parts/ai_chargeback.py")
    assert '_wlab = "last month" if bounds is not None else f"{_win}d"' in src
    assert 'f"Cortex spend, {_wlab}"' in src
    assert 'f"Cortex spend, {_win}d"' not in src


# --- CoCo-REVERSE: coco_efficiency honors bounds (bounded calendar window, not a trailing cut) ----
def _coco_daily() -> pd.DataFrame:
    return pd.DataFrame([
        {"USER_NAME": "u1", "USAGE_DATE": "2026-07-31", "CREDITS": 11.0, "REQUESTS": 1},
        {"USER_NAME": "u1", "USAGE_DATE": "2026-08-01", "CREDITS": 5.0, "REQUESTS": 1},
        {"USER_NAME": "u1", "USAGE_DATE": "2026-08-15", "CREDITS": 20.0, "REQUESTS": 1},
        {"USER_NAME": "u1", "USAGE_DATE": "2026-08-31", "CREDITS": 7.0, "REQUESTS": 1},
        {"USER_NAME": "u1", "USAGE_DATE": "2026-09-01", "CREDITS": 100.0, "REQUESTS": 1},
        {"USER_NAME": "u1", "USAGE_DATE": "2026-09-02", "CREDITS": 50.0, "REQUESTS": 1},
    ])


def test_coco_efficiency_bounds_uses_the_exact_calendar_month():
    # bounds = the whole of August (exclusive end) → only Aug rows count: 5 + 20 + 7 = 32.
    bounded = coco_efficiency(None, _coco_daily(), cap_credits=15.0,
                              window_days=31, as_of=date(2026, 9, 2),
                              bounds=(date(2026, 8, 1), date(2026, 9, 1))).set_index("USER_NAME")
    assert float(bounded.loc["u1", "TOTAL_CREDITS"]) == 32.0
    # trailing (no bounds), anchored at Sep 2 over 31 days → cut at Aug 2: 20 + 7 + 100 + 50 = 177.
    trailing = coco_efficiency(None, _coco_daily(), cap_credits=15.0,
                               window_days=31, as_of=date(2026, 9, 2)).set_index("USER_NAME")
    assert float(trailing.loc["u1", "TOTAL_CREDITS"]) == 177.0
    # the whole point: the two windows differ, so "last month" over the trailing frame was wrong.
    assert float(bounded.loc["u1", "TOTAL_CREDITS"]) != float(trailing.loc["u1", "TOTAL_CREDITS"])


# --- ROLE-TWIN: live role-share is all-status, matching the mart twin --------------------------
def test_role_share_within_warehouse_is_all_status_like_the_mart():
    live = chargeback_sql.role_share_within_warehouse(30, "ALFA")
    assert "EXECUTION_STATUS" not in live          # no success-only filter any more
    mart = mart27_sql.role_share(30, "ALFA")
    assert "EXECUTION_STATUS" not in mart          # the fact twin was already all-status
    # window anchor still matched (the round-earlier twin fix is untouched)
    assert "START_TIME >= DATEADD('day', -30, CURRENT_DATE())" in live


# --- UNIT-$: format_usd_precise preserves sub-cent unit costs ----------------------------------
def test_format_usd_precise_preserves_subcent_and_defers_above_one():
    assert format_usd_precise(0.0034) == "$0.0034"      # would collapse to $0.00 under format_usd
    assert format_usd_precise(0.006) == "$0.0060"       # would round to $0.01 under format_usd
    assert format_usd_precise(0.0) == "$0.00"           # zero stays clean
    assert format_usd_precise(5.0) == format_usd(5.0) == "$5.00"      # >= $1 defers
    assert format_usd_precise(12.5) == "$12.50"
    assert format_usd_precise(15000) == format_usd(15000)             # large defers to compact form


def test_unit_costs_per_call_kpis_use_precise_formatter():
    src = _src("app/ui/pages/cost_parts/unit_costs.py")
    assert 'format_usd_precise(credits_to_usd(safe_float(top_p.get("CREDITS_PER_CALL"))' in src
    assert "format_usd_precise(_tot / _calls)" in src


# --- SPILL-FMT: SPILL_GB_PER_DAY formatted %.2f on both right-sizing surfaces -------------------
def test_spill_gb_per_day_formatted_on_both_surfaces():
    # both right-sizing surfaces give the same column a NumberColumn("Spill GB/day", %.2f);
    # whitespace differs (Optimize wraps the call across two lines), so check the parts.
    for rel in ("app/ui/pages/cost_parts/optimize.py", "app/ui/pages/operations.py"):
        src = _src(rel)
        assert '"SPILL_GB_PER_DAY": st.column_config.NumberColumn(' in src, rel
        assert '"Spill GB/day", format="%.2f"' in src, rel


# --- POSTURE-CRON: the security posture load-time captions are accurate -------------------------
def test_security_posture_captions_do_not_claim_0630_load():
    src = _src("app/ui/pages/security.py")
    assert "daily 06:30 snapshot" not in src
    assert "Loaded daily at 06:30." not in src
    assert "daily post-06:45 snapshot" in src
    assert "after the ~06:45 nightly load" in src
