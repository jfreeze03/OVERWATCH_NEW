"""Repo-review wave 1 (owner 2026-08-17; CoCo spend ~13% of total and growing):
AI Trust & Guardrails behavioral analytics, the known-spike calendar, and the
critical-veto platform-score rule."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.logic.ai_guardrails import behavior_kpis, user_behavior
from app.logic.anomaly import expected_spike_labels, suppress_expected_spikes
from app.logic.scoring import platform_score

_ROOT = Path(__file__).resolve().parents[1]
_NOW = pd.Timestamp("2026-08-17")


def _usage(rows):
    return pd.DataFrame(rows, columns=["USER_NAME", "USAGE_DATE", "REQUESTS", "CREDITS", "TOKENS"])


# ---------------------------------------------------------------- behavior ----

def test_velocity_flag_needs_ratio_AND_floor():
    rows = []
    # STEADY: 10 req/day for 35 days -> velocity ~1x, no flag.
    # SPIKER: baseline 2/day for 28d, then 30/day for 7d -> 15x + >=20 req floor -> flag.
    # TINY: baseline 0.1/day then 1/day (10x) but only 7 requests -> NO flag (floor).
    for i in range(35):
        day = _NOW - pd.Timedelta(days=34 - i)
        rows.append(("STEADY", day, 10, 0.5, 50_000))
        rows.append(("SPIKER", day, 30 if i >= 28 else 2, 1.0, 80_000))
        if i % 10 == 0 or i >= 28:
            rows.append(("TINY", day, 1, 0.01, 100))
    b = user_behavior(_usage(rows), _NOW)
    by = {r["USER_NAME"]: r for _, r in b.iterrows()}
    assert "velocity" in by["SPIKER"]["FLAGS"]
    assert "velocity" not in by["STEADY"]["FLAGS"]
    assert "velocity" not in by["TINY"]["FLAGS"]      # 10x ratio but under the floor


def test_new_heavy_flag_and_kpis():
    rows = [("VET", _NOW - pd.Timedelta(days=d), 5, 0.2, 10_000) for d in range(30)]
    rows += [("FRESH", _NOW - pd.Timedelta(days=d), 40, 3.0, 900_000) for d in range(3)]
    b = user_behavior(_usage(rows), _NOW)
    by = {r["USER_NAME"]: r for _, r in b.iterrows()}
    assert bool(by["FRESH"]["NEW_USER"]) and "new + heavy" in by["FRESH"]["FLAGS"]
    assert not bool(by["VET"]["NEW_USER"])
    k = behavior_kpis(b)
    assert k["active_users"] == 2 and k["flagged_users"] >= 1 and k["new_heavy"] == 1
    # flagged rows sort first.
    assert b.iloc[0]["FLAGS"] != ""


def test_steady_user_reads_1x_and_excludes_todays_partial_day():
    # Review fix #1: the recent window is half-open [today-7, today) — exactly 7
    # complete days. A perfectly steady user must read ~1.0x velocity (the old
    # ">= start" 8-day window inflated it ~14%) and today's rows must not count.
    rows = [("STEADY", _NOW - pd.Timedelta(days=d), 10, 0.5, 50_000) for d in range(36)]
    rows.append(("STEADY", _NOW, 999, 50.0, 9_999_999))   # today's partial day: excluded
    b = user_behavior(_usage(rows), _NOW)
    row = b[b["USER_NAME"] == "STEADY"].iloc[0]
    assert row["REQUESTS_7D"] == 70                        # 7 days x 10, not 8 days / not +999
    assert abs(float(row["VELOCITY"]) - 1.0) < 0.05
    assert row["FLAGS"] == ""


def test_partial_history_user_is_not_false_flagged():
    # Review fix #2: a steady user onboarded 10 days ago has only 3 baseline days;
    # dividing by a fixed 28 read ~9.5x velocity and false-flagged them for weeks.
    # The baseline now divides by the days the user actually existed in the window.
    rows = [("RAMP", _NOW - pd.Timedelta(days=d), 20, 1.0, 100_000) for d in range(1, 11)]
    b = user_behavior(_usage(rows), _NOW)
    row = b[b["USER_NAME"] == "RAMP"].iloc[0]
    assert abs(float(row["VELOCITY"]) - 1.0) < 0.15
    assert "velocity" not in row["FLAGS"]


def test_behavior_empty_and_missing_columns_are_safe():
    assert user_behavior(None, _NOW).empty
    assert user_behavior(pd.DataFrame(), _NOW).empty
    assert behavior_kpis(pd.DataFrame())["flagged_users"] == 0


# ---------------------------------------------------------- spike calendar ----

def test_month_and_quarter_end_labeling():
    days = pd.Series(pd.to_datetime([
        "2026-08-31", "2026-09-01",   # month-end window (n=1)
        "2026-09-30", "2026-10-01",   # quarter-end (n=2 also covers month-end)
        "2026-08-17",                 # mid-month
    ]))
    labels = expected_spike_labels(days, "MONTH_END:1;QUARTER_END:2")
    assert labels[0] == "month-end" and labels[1] == "month-end"
    assert labels[2] in ("month-end", "quarter-end") and labels[3] in ("month-end", "quarter-end")
    assert labels[4] == ""


def test_explicit_range_and_malformed_rules():
    days = pd.Series(pd.to_datetime(["2026-11-16", "2026-11-21"]))
    labels = expected_spike_labels(days, "2026-11-15..2026-11-20:renewal load test;GARBAGE;X:")
    assert labels[0] == "renewal load test" and labels[1] == ""
    # malformed calendar never raises.
    assert (expected_spike_labels(days, ";;NOT_A_RULE:zz;2026-13-99..x:y") == "").all()


def test_suppression_clears_upward_flags_but_never_collapses():
    frame = pd.DataFrame({
        "DAY": pd.to_datetime(["2026-08-31", "2026-08-31", "2026-08-17"]),
        "USD": [900.0, 5.0, 800.0],
        "Z_SCORE": [4.2, -4.0, 4.0],
        "IS_ANOMALY": [True, True, True],
    })
    out = suppress_expected_spikes(frame, "MONTH_END:1")
    assert not bool(out.loc[0, "IS_ANOMALY"])           # month-end spike -> expected
    assert out.loc[0, "EXPECTED_SPIKE"] == "month-end"
    assert bool(out.loc[1, "IS_ANOMALY"])               # collapse NEVER suppressed
    assert bool(out.loc[2, "IS_ANOMALY"])               # mid-month spike still flags
    # no calendar = no-op with the column present.
    none = suppress_expected_spikes(frame, "")
    assert none["IS_ANOMALY"].all() and (none["EXPECTED_SPIKE"] == "").all()


def test_spike_calendar_is_wired_into_spend_and_settings():
    spend = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "spend.py").read_text(encoding="utf-8")
    assert "suppress_expected_spikes(flagged" in spend
    cfg = (_ROOT / "app" / "config.py").read_text(encoding="utf-8")
    assert '"EXPECTED_SPIKE_CALENDAR"' in cfg


# ------------------------------------------------------------ critical veto ----

def test_critical_veto_caps_score_below_healthy():
    # One critical alone: 6-pt penalty -> raw 94 ("Healthy") — the veto caps it at 84.
    vetoed = platform_score({"critical_alerts": 1})
    assert vetoed.score == 84 and vetoed.state == "Watch"
    assert any(d.driver == "Critical veto" for d in vetoed.drivers)
    # No criticals: no veto driver, healthy stays healthy.
    clean = platform_score({"high_alerts": 1})
    assert clean.state == "Healthy"
    assert not any(d.driver == "Critical veto" for d in clean.drivers)
    # Already sub-85 with criticals: no double-penalty veto driver appended.
    low = platform_score({"critical_alerts": 5, "high_alerts": 5})
    assert low.score < 85
    assert not any(d.driver == "Critical veto" for d in low.drivers)


def test_ai_guardrails_section_is_wired_into_security():
    src = (_ROOT / "app" / "ui" / "pages" / "security.py").read_text(encoding="utf-8")
    assert '"AI guardrails"' in src and "_ai_guardrails_tab(f[\"company\"])" in src
    assert "guardrails_daily(30)" in src and "probe=True" in src
    # honest degrade when the optional guardrails view is absent.
    assert "isn't available on this account" in src
