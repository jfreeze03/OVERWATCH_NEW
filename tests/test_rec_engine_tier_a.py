"""Tier-A recommendation-engine locks: engines that emit EXECUTABLE advice.

These three engines generate SQL the operator pastes and runs, and each was giving
advice in the WRONG DIRECTION (audit 2026-07-31). A wrong number on a dashboard
misleads; a wrong ALTER makes the account worse — so these get behavioural tests,
not source-locks.

  A1 tuning.py        — inverse-metric rules (fire when value <= threshold) were
                        told to RAISE the threshold, which WIDENS firing.
  A2 insights_sql.py  — quiet-hours computed in UTC, pasted into a Chicago CRON.
  A3 insights.py      — idle advisor proposed 60s to a warehouse already at 30s.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# A1 — threshold tuning must mirror for inverse metrics
# ---------------------------------------------------------------------------
def _evidence(noise_vals, actioned_vals) -> pd.DataFrame:
    rows = [{"METRIC_VALUE": v, "RESOLUTION_KIND": "NOISE"} for v in noise_vals]
    rows += [{"METRIC_VALUE": v, "RESOLUTION_KIND": "ACTIONED"} for v in actioned_vals]
    return pd.DataFrame(rows)


def test_a1_normal_rule_suggests_upward():
    from app.logic.tuning import suggest_threshold
    # higher-is-worse: noise is small, real alerts are big -> raise the threshold
    ev = _evidence([1, 2, 3, 2, 1, 2], [50, 60, 70, 80])
    out = suggest_threshold(ev, current_threshold=1.0, rule_id="COST_SPIKE")
    assert out["ok"] and out["suggested"] > 3


def test_a1_inverse_rule_suggests_downward_not_upward():
    from app.logic.tuning import suggest_threshold
    # SEC_CRED_EXPIRY fires when days-until-expiry <= threshold (30). So every resolved
    # event has value <= 30: noise = credentials with 20-30 days left (fired, not urgent),
    # actioned = the ones actually about to expire. The suggestion must move DOWN
    # (tighter) — raising it would fire on even MORE credentials, the opposite of intent.
    ev = _evidence([20, 25, 28, 22, 30, 26], [1, 2, 0, 3])
    out = suggest_threshold(ev, current_threshold=30.0, rule_id="SEC_CRED_EXPIRY")
    assert out["ok"], out["basis"]
    assert out["suggested"] < 30.0, f"inverse rule must tighten downward, got {out['suggested']}"
    assert out["suggested"] > 3, "must stay above the actioned ceiling (keep the real ones)"
    # the suggestion separates cleanly: keeps every actioned, cuts every noise event
    assert "keeps 100% of actioned" in out["basis"] and "cuts 100% of noise" in out["basis"]


def test_a1_normal_rule_direction_is_unchanged_by_the_fix():
    from app.logic.tuning import suggest_threshold
    # the same evidence on a normal (higher-is-worse) rule must still go UP —
    # proving the mirror is selected by rule_id, not applied globally.
    ev = _evidence([20, 25, 28, 22, 30, 26], [1, 2, 0, 3])
    out = suggest_threshold(ev, current_threshold=1.0, rule_id="COST_SPIKE")
    assert not out["ok"], "overlapping evidence on a >= rule is correctly non-separable"


def test_a1_inverse_pure_noise_also_goes_down():
    from app.logic.tuning import suggest_threshold
    ev = _evidence([60, 75, 90, 80, 70, 65], [])
    out = suggest_threshold(ev, current_threshold=30.0, rule_id="COST_CONTRACT_BREACH")
    assert out["ok"] and out["suggested"] < 30.0


def test_a1_zero_and_negative_evidence_is_kept():
    from app.logic.tuning import suggest_threshold
    # 0 = expires today, negative = already expired: the MOST actionable evidence.
    # The old `METRIC_VALUE > 0` filter silently discarded exactly these rows.
    ev = _evidence([60, 75, 90, 80, 70, 65], [0, -2, 1, 2])
    out = suggest_threshold(ev, current_threshold=30.0, rule_id="SEC_CRED_EXPIRY")
    assert out["ok"] and out["actioned_n"] == 4, "expired/expiring rows must count"


def test_a1_invalid_metric_values_are_rejected_not_coerced_to_zero():
    from app.logic.tuning import suggest_threshold
    frame = _evidence([60, 75, 90, 80, 70, 65], [1, 2])
    invalid = pd.DataFrame([
        {"METRIC_VALUE": None, "RESOLUTION_KIND": "ACTIONED"},
        {"METRIC_VALUE": "not-a-number", "RESOLUTION_KIND": "ACTIONED"},
        {"METRIC_VALUE": float("inf"), "RESOLUTION_KIND": "ACTIONED"},
    ])
    out = suggest_threshold(pd.concat([frame, invalid], ignore_index=True), 30.0,
                            "SEC_CRED_EXPIRY")
    assert out["ok"] and out["actioned_n"] == 2


def test_a1_direction_set_and_rule_id_wiring():
    from app.logic.tuning import LOWER_IS_WORSE, suggestions_by_rule
    assert {"SEC_CRED_EXPIRY", "COST_CONTRACT_BREACH"} <= set(LOWER_IS_WORSE)
    ev = _evidence([20, 25, 28, 22, 30, 26], [1, 2, 0, 3])
    ev["RULE_ID"] = "SEC_CRED_EXPIRY"
    frame = suggestions_by_rule(ev, {"SEC_CRED_EXPIRY": 30.0})
    # the per-rule path must pass rule_id through, i.e. produce the DOWNWARD answer
    assert float(frame.iloc[0]["SUGGESTED_THRESHOLD"]) < 30.0


# ---------------------------------------------------------------------------
# A2 — quiet-hours must be computed in ACCOUNT time (the CRON is Chicago)
# ---------------------------------------------------------------------------
def test_a2_hourly_activity_uses_account_timezone():
    from app.data import insights_sql
    sql = insights_sql.warehouse_hourly_activity(14, "ALFA")
    # the metering side is TIMESTAMP_LTZ (session tz); it must be converted before HOUR()
    assert "HOUR(CONVERT_TIMEZONE('America/Chicago', START_TIME))" in sql
    assert "HOUR(START_TIME)" not in sql, "raw session-tz hour would mismatch FACT_QUERY_HOURLY"
    # day counting must use the same converted timestamp, or DAYS_SEEN splits at UTC midnight
    assert "DATE(CONVERT_TIMEZONE('America/Chicago', START_TIME))" in sql


# ---------------------------------------------------------------------------
# A3 — idle advisor must respect the current AUTO_SUSPEND
# ---------------------------------------------------------------------------
def _idle_frame(auto_suspend=None):
    row = {"WAREHOUSE_NAME": "WH_X", "COMPANY": "ALFA",
           "TOTAL_CREDITS": 100.0, "IDLE_CREDITS": 40.0,
           "METERED_HOURS": 10.0, "IDLE_HOURS": 4.0}
    if auto_suspend is not None:
        row["AUTO_SUSPEND"] = auto_suspend
    return pd.DataFrame([row])


def test_a3_never_proposes_raising_a_tuned_warehouse():
    from app.logic.insights import idle_advisor
    out = idle_advisor(_idle_frame(auto_suspend=30), 3.68, 30)
    rec = str(out.iloc[0]["RECOMMENDATION"])
    assert "60s" not in rec, f"must not propose raising a 30s warehouse: {rec}"
    assert "already at AUTO_SUSPEND=30s" in rec
    assert "resume overhead" in rec


def test_a3_still_advises_an_untuned_warehouse():
    from app.logic.insights import idle_advisor
    out = idle_advisor(_idle_frame(auto_suspend=600), 3.68, 30)
    rec = str(out.iloc[0]["RECOMMENDATION"])
    assert "Reduce AUTO_SUSPEND to 60s" in rec and "currently 600s" in rec
    assert bool(out.iloc[0]["ACTIONABLE"])
    assert out.iloc[0]["ACTIONABLE_MONTHLY_USD"] > 0


def test_a3_without_current_setting_is_verification_only():
    from app.logic.insights import idle_advisor
    out = idle_advisor(_idle_frame(), 3.68, 30)     # no AUTO_SUSPEND column
    row = out.iloc[0]
    assert "Verify current AUTO_SUSPEND" in str(row["RECOMMENDATION"])
    assert not bool(row["ACTIONABLE"])
    assert row["ACTIONABLE_MONTHLY_USD"] == 0.0


def test_a3_tuned_warehouses_rank_below_fixable_ones():
    from app.logic.insights import idle_advisor
    df = pd.DataFrame([
        {"WAREHOUSE_NAME": "TUNED", "COMPANY": "ALFA", "TOTAL_CREDITS": 100.0,
         "IDLE_CREDITS": 40.0, "METERED_HOURS": 10.0, "IDLE_HOURS": 4.0, "AUTO_SUSPEND": 30},
        {"WAREHOUSE_NAME": "FIXABLE", "COMPANY": "ALFA", "TOTAL_CREDITS": 50.0,
         "IDLE_CREDITS": 20.0, "METERED_HOURS": 5.0, "IDLE_HOURS": 2.0, "AUTO_SUSPEND": 600},
    ])
    out = idle_advisor(df, 3.68, 30)
    # TUNED has more idle $, but FIXABLE is the actionable tuning target
    assert str(out.iloc[0]["WAREHOUSE_NAME"]) == "FIXABLE"
    tuned = out[out["WAREHOUSE_NAME"] == "TUNED"].iloc[0]
    assert tuned["ACTION_STATUS"] == "ALREADY TUNED"
    assert tuned["ACTIONABLE_MONTHLY_USD"] == 0.0


def test_a3_show_warehouses_merge_preserves_disabled_vs_unknown():
    from app.logic.insights import with_auto_suspend_settings
    idle = pd.DataFrame([
        {"WAREHOUSE_NAME": "wh_disabled"},
        {"WAREHOUSE_NAME": "WH_MISSING"},
    ])
    settings = pd.DataFrame({"name": ["WH_DISABLED"], "auto_suspend": [0]})
    out = with_auto_suspend_settings(idle, settings).set_index("WAREHOUSE_NAME")
    assert bool(out.loc["wh_disabled", "AUTO_SUSPEND_KNOWN"])
    assert out.loc["wh_disabled", "AUTO_SUSPEND"] == 0
    assert not bool(out.loc["WH_MISSING", "AUTO_SUSPEND_KNOWN"])


def test_a3_generated_sql_never_raises_the_timer():
    opt = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "optimize.py").read_text(encoding="utf-8")
    # rec2 indented this block one level (nested opt_section pills); match
    # whitespace-insensitively so the guard (skip already-short warehouses) still holds.
    assert "if 0 < _cur <= IDLE_TARGET_SUSPEND_SEC: continue" in " ".join(opt.split())
    assert "int(min(_cur, IDLE_TARGET_SUSPEND_SEC))" in opt
    assert 'idle_suspend_sql(r["WAREHOUSE_NAME"], _target)' in opt
