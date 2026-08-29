"""UI-layer bug hunt round 2 (v4.337.0) — locks for the seven confirmed fixes.

Behavioral where the code path is unit-reachable (the compare coverage warning and
the mart SQL); source-shape locks for the render-inline fixes that need a live
Streamlit surface to exercise. Each lock pins the DEFECT it prevents, not just the
line, so a silent revert fails here.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.data import mart27_sql
from app.ui.pages.cost_parts import compare

_ROOT = Path(__file__).resolve().parents[2]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# 1) control_room RCA — NULL STARTED_AT (pd.NaT) must fall back to DETECTED_AT, not
#    blank the flagship panel. bool(pd.NaT) is True, so a Python `or` can't gate it.
def test_rca_onset_fallback_is_na_safe_not_or():
    cr = _src("app/ui/pages/control_room.py")
    assert 'inc_row.get("STARTED_AT") or inc_row.get("DETECTED_AT")' not in cr
    assert 'pd.notna(_started)' in cr and 'inc_row.get("DETECTED_AT")' in cr


# 2) workbench Entity 360 catalog editor — every widget key scoped to the entity, so
#    switching entities can't leave a prior entity's ownership in the form and Save
#    (MERGE onto the NEW key) can't write stale values onto the wrong record.
def test_catalog_editor_keys_are_entity_scoped():
    wb = _src("app/ui/workbench.py")
    assert '_sig = f"{kind}_{key}"' in wb
    for field in ("label", "team", "owner", "steward", "oncall",
                  "criticality", "product", "slo", "notes"):
        assert f'key=f"entity_{field}_{{_sig}}"' in wb, field
        assert f'key="entity_{field}"' not in wb, field  # no unscoped fixed key


# 3) overview budget burndown — fed the account-wide full-month frame (proj_daily),
#    not the company-scoped `days`-windowed daily_complete that truncated the
#    cumulative actual and faked an "under pace" gap past ~the 8th.
def test_budget_burndown_uses_full_month_account_frame():
    ov = _src("app/ui/pages/overview.py")
    assert "budget_burndown(_burn_src, budget, account_today())" in ov
    assert "_burn_src = (" in ov and "proj_daily[" in ov
    assert "budget_burndown(daily_complete" not in ov


# 4) operations Queries sparklines — suppressed when a warehouse/user/schema filter is
#    active (the activity feed honors only company+DB), so a filtered KPI number is
#    never paired with a company-wide trend it doesn't describe. database IS honored.
def test_queries_sparkline_suppressed_under_unhonored_filters():
    ops = _src("app/ui/pages/operations.py")
    assert "_spark_ok = not (wh_filter or user_filter or schema_contains)" in ops
    assert "if _spark_ok and activity.usable()" in ops


# 5) operations failure-timeline header alarm — driven by the ACTUAL 7d failures the
#    body shows (len(timeline)), not the window-wide known_failures, so a >7d-old
#    failure can't paint an amber header over a verified-clean 7d body.
def test_failure_timeline_alarm_reflects_seven_day_body():
    ops = _src("app/ui/pages/operations.py")
    assert "alarm_health(len(timeline))" in ops
    # the old bug: alarm computed straight from the window-wide known_failures
    assert "alarm_health(None if known_failures is None else int(known_failures))" not in ops


# 6) mart27 compare SQL — carries LOADED_THROUGH (the loader's GLOBAL reach), company
#    scoped, so the coverage check can tell an unfinished backfill from a quiet day.
def test_compare_sql_exposes_global_loaded_through():
    sql_all = mart27_sql.compare_warehouse_credits(
        "2026-07-01", "2026-08-01", "2026-06-01", "2026-07-01", "ALL")
    sql_co = mart27_sql.compare_warehouse_credits(
        "2026-07-01", "2026-08-01", "2026-06-01", "2026-07-01", "ALFA")
    assert "LOADED_THROUGH" in sql_all and "cov.LOADED_THROUGH" in sql_all
    # global reach: the subquery is NOT bounded by the compared windows
    assert "FACT_WAREHOUSE_DAILY) AS LOADED_THROUGH" in sql_all      # ALL: no WHERE
    assert "WHERE COMPANY = 'ALFA') AS LOADED_THROUGH" in sql_co     # scoped subquery


# 7) compare _coverage_warning — judged by loader reach, NOT active-day count. A
#    fully-loaded window with idle (suspended) calendar days must NOT warn; only a
#    window whose end the loader hasn't reached does.
_PAIR = {"a": ("2026-07-01", "2026-08-01"), "b": ("2026-06-01", "2026-07-01"),
         "label_a": "A(month)", "label_b": "B(prior)"}


def test_coverage_warning_ignores_idle_days_when_loaded():
    # loader reached well past both window ends; interior days are idle (sparse fact)
    df = pd.DataFrame({"A_CREDITS": [1.0], "B_CREDITS": [1.0], "A_MAX_DAY": ["2026-07-24"],
                       "B_MAX_DAY": ["2026-06-27"], "LOADED_THROUGH": ["2026-08-15"]})
    assert compare._coverage_warning(df, _PAIR) == ""


def test_coverage_warning_flags_only_unreached_side():
    # loader stalled mid-July: window A (ends 07-31) is unreached; B (older) is covered
    df = pd.DataFrame({"A_CREDITS": [1.0], "B_CREDITS": [1.0], "A_MAX_DAY": ["2026-07-15"],
                       "B_MAX_DAY": ["2026-06-30"], "LOADED_THROUGH": ["2026-07-15"]})
    warn = compare._coverage_warning(df, _PAIR)
    assert "A(month)" in warn and "B(prior)" not in warn
    assert "loaded through 2026-07-15" in warn and "window ends 2026-07-31" in warn


def test_coverage_warning_empty_or_missing_column_is_silent():
    assert compare._coverage_warning(pd.DataFrame(), _PAIR) == ""
    # no LOADED_THROUGH column (older cached shape) -> silent, never a false alarm
    assert compare._coverage_warning(
        pd.DataFrame({"A_CREDITS": [1.0]}), _PAIR) == ""
