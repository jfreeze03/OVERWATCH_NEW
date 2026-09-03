"""Regression locks for bug-hunt round 20 — window/format honesty on cost + ops surfaces.

UC-CLAMP    Unit-costs clamped the measured-attribution reads to 30d and fired a "scanning 30d"
            caption + a "Price over the full window" toggle — but under "Last month" scope the
            bounds predicate already scans the exact (<=31-day) month and ignores the day count,
            so in 31-day months the caption lied ("scanning 30d" while 31 were scanned) and the
            toggle was inert. The clamp/toggle/caption are now gated on bounds is None.
SPEND-SERVED The Spend headline metering read is a manual mart(365d)-then-live(90d) twin; on a
            >90d trailing selection served by the live fallback the SUM tiles labeled a 90-day
            answer as the full window. The label now reflects the served window + a disclosure.
IDLE-FMT    IDLE_PCT rendered at 1dp on the Optimize idle-advisor evidence table but 0dp on the
            three other surfaces showing the same credit-weighted idle share; aligned to 0dp.
LOCK-SPAN   The Operations "Lock waits" panel covers up to 14d from the mart but 7d from the live
            fallback with no on-screen label; a disclosure now names the 7d cap when live serves.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- UC-CLAMP: the 30d clamp/toggle/caption only apply to long TRAILING windows ---------------
def test_unit_costs_30d_clamp_is_gated_on_unbounded_window():
    src = _src("app/ui/pages/cost_parts/unit_costs.py")
    assert "_uc_capped = bounds is None and int(days) > _UNIT_COST_MAX_DAYS" in src
    assert "_uc_full = _uc_capped and st.toggle(" in src
    assert "uc_days = _UNIT_COST_MAX_DAYS if (_uc_capped and not _uc_full) else days" in src
    # the old unconditional clamp (fired the false "scanning 30d" caption under Last month) is gone
    assert "uc_days = days if (_uc_full or int(days) <= _UNIT_COST_MAX_DAYS)" not in src


# --- SPEND-SERVED: the metering tiles label the window the live fallback actually scanned ------
def test_spend_metering_tiles_label_the_served_window():
    src = _src("app/ui/pages/cost_parts/spend.py")
    assert "_metering_live = False" in src
    assert "_metering_live = True" in src
    assert ("_served_days = (min(int(days), MAX_LIVE_WINDOW_DAYS)\n"
            "                    if (_metering_live and bounds is None) else int(days))") in src
    assert '_wlab = "last month" if bounds is not None else f"{_served_days}d"' in src
    # the served-window disclosure mirrors the sibling cloud-services panel
    assert "Scanned {_served_days}d of the {days}d window (the live fallback caps its scan)." in src
    # the old raw-days label (labeling a 90d answer as the full window) is gone
    assert '_wlab = "last month" if bounds is not None else f"{days}d"' not in src


# --- IDLE-FMT: IDLE_PCT is 0dp on all four surfaces that render the same idle share ------------
def test_idle_pct_precision_is_consistent_across_surfaces():
    opt = _src("app/ui/pages/cost_parts/optimize.py")
    # the idle-advisor evidence table no longer diverges at 1dp
    assert '"IDLE_PCT": st.column_config.NumberColumn("Idle %", format="%.1f%%")' not in opt
    # both Optimize IDLE_PCT columns are now 0dp, matching Operations
    assert opt.count('"IDLE_PCT": st.column_config.NumberColumn("Idle %", format="%.0f%%")') >= 2


# --- LOCK-SPAN: the live lock-wait fallback discloses its 7d cost cap --------------------------
def test_lock_waits_discloses_live_fallback_seven_day_cap():
    src = _src("app/ui/pages/operations.py")
    assert "if _served_live:" in src
    assert "Live fallback: lock waits cover the last ~7 days" in src
    # the mart/live call contract itself is unchanged (min(days, 14) to both arms)
    assert "ops_sql.lock_contention(min(days, 14), bounds=bounds)" in src
