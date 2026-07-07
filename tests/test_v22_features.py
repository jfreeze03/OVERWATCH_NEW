"""Locks for the V022 feature batch: query-$ attribution, what-if simulator,
storage reclaim, alert precision, mart/org reconciliation, fleet telemetry."""

from __future__ import annotations

from app.core.query import should_persist_telemetry
from app.data import cost_sql, insights_sql, mart_sql
from app.logic.sizing import normalize_size, shifted_size, simulate_scenario
from app.ui.pages.alerts import RESOLUTION_KINDS, _lifecycle_sql

# ---------------------------------------------------------------------------
# Right-size what-if simulator (pure)
# ---------------------------------------------------------------------------


def _sim(**over):
    base = {"size": "MEDIUM", "credits_window": 100.0, "idle_credits_window": 20.0,
            "window_days": 30, "rate_usd": 2.0, "size_delta": 0,
            "autosuspend_now_s": 600, "autosuspend_new_s": 600}
    base.update(over)
    return simulate_scenario(**base)


def test_sim_no_change_is_identity():
    sim = _sim()
    assert sim["ok"]
    assert sim["monthly_low_usd"] == sim["monthly_high_usd"] == sim["monthly_now_usd"]


def test_sim_size_down_bounded_between_half_and_neutral():
    sim = _sim(size_delta=-1, idle_credits_window=0.0)
    # busy 100 cr, 30d, $2: now = $200/mo; down 1 step: $100 (rate-scaled) .. $200 (cost-neutral)
    assert sim["monthly_now_usd"] == 200
    assert sim["monthly_low_usd"] == 100
    assert sim["monthly_high_usd"] == 200


def test_sim_suspend_cut_shrinks_idle_only():
    sim = _sim(autosuspend_new_s=60)  # 600s -> 60s: idle x0.1
    # busy 80 + idle 20*0.1=2 -> 82 cr -> $164/mo both bounds (no size change)
    assert sim["monthly_low_usd"] == sim["monthly_high_usd"] == 164


def test_sim_idle_burns_at_new_rate():
    sim = _sim(size_delta=1, autosuspend_new_s=600)
    # up: busy in [80, 160]; idle 20 * 2 (new rate) = 40 -> [120*2, 200*2] $/mo
    assert sim["monthly_low_usd"] == 240
    assert sim["monthly_high_usd"] == 400


def test_sim_clamps_at_ladder_ends():
    sim = _sim(size="XSMALL", size_delta=-2)
    assert sim["size_new"] == "XSMALL"
    assert sim["monthly_low_usd"] == sim["monthly_high_usd"]  # applied delta 0


def test_sim_unknown_size_declines():
    assert simulate_scenario(size="WEIRD", credits_window=1, idle_credits_window=0,
                             window_days=7, rate_usd=1)["ok"] is False


def test_size_helpers():
    assert normalize_size("X-Small") == "XSMALL"
    assert normalize_size("4X-LARGE") == "4XLARGE"
    assert shifted_size("SMALL", 1) == "MEDIUM"
    assert shifted_size("SMALL", -5) == "XSMALL"


# ---------------------------------------------------------------------------
# SQL builders: shape + safety
# ---------------------------------------------------------------------------


def test_expensive_queries_shape():
    sql = insights_sql.expensive_queries_usd(7, "Trexis", 50)
    assert "WAREHOUSE_METERING_HISTORY" in sql and "QUERY_HISTORY" in sql
    assert "ALLOCATED_CREDITS" in sql and "LIMIT 50" in sql
    assert "WH_TRXS_LOAD" in sql  # company scoping applied
    assert "NULLIF(t.TOTAL_EXEC_MS, 0)" in sql  # no divide-by-zero


def test_expensive_queries_bounds_inputs():
    import re

    sql = insights_sql.expensive_queries_usd(9999, "ALFA", 9999)
    assert "-90," in sql and "LIMIT 200" in sql
    hostile = insights_sql.expensive_queries_usd(7, "ALFA", 50, database="X'; DROP TABLE T--")
    # strip-literals invariant (same as the fuzz suite): once string literals
    # are masked, no hostile token remains anywhere executable.
    masked = re.sub(r"'(?:''|[^'])*'", "''", hostile)
    assert "DROP" not in masked and "--" not in masked.replace("T--", "")


def test_storage_reclaim_shape():
    sql = insights_sql.storage_reclaim("ALFA")
    assert "ACCESS_HISTORY" in sql and "NEVER_READ" in sql
    assert "RETAINED_FOR_CLONE_BYTES" in sql and "objectDomain" in sql


def test_rule_precision_shape():
    sql = mart_sql.rule_precision(9999)
    assert "RESOLUTION_KIND" in sql and "-365," in sql  # clamped
    assert "NULLIF(ACTIONED + NOISE, 0)" in sql


def test_mart_recon_two_checks():
    sql = mart_sql.mart_vs_live_recon()
    assert sql.count("DRIFT_PCT") >= 1 and "UNION ALL" in sql
    assert "METERING_DAILY_HISTORY" in sql and "FACT_METERING_DAILY" in sql
    assert "FACT_QUERY_HOURLY" in sql


def test_fleet_stats_clamps():
    assert "-90," in mart_sql.fleet_query_stats(9999)
    assert "APP_QUERY_TELEMETRY" in mart_sql.fleet_query_stats(7)


def test_org_month_shape():
    sql = cost_sql.org_account_month_usd(99)
    assert "CURRENT_ACCOUNT_NAME()" in sql and "-11," in sql  # 12-month clamp
    assert "USAGE_IN_CURRENCY_DAILY" in sql


# ---------------------------------------------------------------------------
# Alert lifecycle: resolution kinds (V021)
# ---------------------------------------------------------------------------


def test_resolve_embeds_valid_kind():
    sql = _lifecycle_sql("evt-1", "RESOLVE", "fixed it", "ACTIONED")
    assert "RESOLUTION_KIND = 'ACTIONED'" in sql
    assert "[ACTIONED] fixed it" in sql  # audit note carries the kind too
    assert "STATUS IN ('OPEN', 'ACK')" in sql  # can't resolve a resolved event


def test_resolve_drops_invalid_kind():
    sql = _lifecycle_sql("evt-1", "RESOLVE", "n", "SHRUG'); DROP TABLE X;--")
    assert "RESOLUTION_KIND" not in sql
    assert "DROP TABLE" not in sql.replace("''", "")


def test_ack_ignores_kind_and_gates_on_open():
    sql = _lifecycle_sql("evt-1", "ACK", "seen", "ACTIONED")
    assert "RESOLUTION_KIND" not in sql
    assert "STATUS = 'OPEN'" in sql


def test_kind_catalog_stable():
    assert RESOLUTION_KINDS == ("ACTIONED", "NOISE", "EXPECTED")


# ---------------------------------------------------------------------------
# Fleet telemetry gate (pure)
# ---------------------------------------------------------------------------


def test_persist_failed_always():
    assert should_persist_telemetry(5.0, ok=False, persisted=0)


def test_persist_slow_only_at_threshold():
    assert not should_persist_telemetry(1999.9, ok=True, persisted=0)
    assert should_persist_telemetry(2000.0, ok=True, persisted=0)


def test_persist_session_cap():
    assert not should_persist_telemetry(9999.0, ok=False, persisted=60)
