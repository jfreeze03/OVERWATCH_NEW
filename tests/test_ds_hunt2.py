"""Decision Studio bug-hunt #2 locks (2026-08-30, v4.366.0).

Second adversarial DS pass (6 finders). Nine surfaced, five confirmed, four refuted (experiment-detail
notes render in the master table; the error-budget dual-signal is deliberate SRE practice; the triage
severity path is unreachable given ALERT_EVENTS.SEVERITY NOT NULL + canonical seeds; the "Pays for
itself" horizon is genuinely consistent). All five fixes are app-side.
  - [MED] Portfolio "Needs validation" KPI filtered on CONFIDENCE<0.5, omitting the ~has_behavior
    families forced to LANE=VALIDATE with high confidence -> count on the LANE itself.
  - [MED] Experiments "Verified"/"Verified value" KPIs summed the LIMIT-300 display frame -> read an
    uncapped aggregate.
  - [MED] Cost Truth MEASURED folded the UNATTRIBUTED residual into "Object-attributed" and made the
    basis scope-variant -> exclude OBJECT_FQN='UNATTRIBUTED'.
  - [LOW] SUCCESS_PCT SLO target accepted values >100 -> permanent false BREACH -> cap at 100.
  - [LOW] acceptance_funnel counted only currently-ESTIMATED as the funnel top, so verified could
    exceed estimated -> count all booked-in-window.
"""

from __future__ import annotations

from pathlib import Path

from app.data import mart_sql, workbench_sql
from app.logic.workbench import create_slo_objective_sql

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Finding #1 -- "Needs validation" counts the VALIDATE lane, not confidence<0.5
# --------------------------------------------------------------------------- #
def test_needs_validation_counts_the_validate_lane() -> None:
    src = _src("app/ui/decision_studio.py")
    assert 'validate = portfolio[portfolio["LANE"].eq("VALIDATE")]' in src
    assert 'validate = portfolio[portfolio["CONFIDENCE"].lt(0.5)]' not in src


# --------------------------------------------------------------------------- #
# Finding #2 -- Verified count/value read the uncapped aggregate, not the display frame
# --------------------------------------------------------------------------- #
def test_experiment_verified_totals_is_uncapped() -> None:
    sql = workbench_sql.experiment_verified_totals()
    assert "LIMIT" not in sql
    assert "COUNT_IF(UPPER(STATUS) = 'VERIFIED')" in sql
    assert "OPTIMIZATION_EXPERIMENTS" in sql


def test_experiments_panel_uses_the_uncapped_totals() -> None:
    src = _src("app/ui/decision_studio.py")
    assert "workbench_sql.experiment_verified_totals()" in src
    assert '{"label": "Verified", "value": f"{_verified_ct:,}"' in src
    assert '"value": format_usd(_verified_usd)' in src


# --------------------------------------------------------------------------- #
# Finding #3 -- Cost Truth MEASURED excludes the UNATTRIBUTED residual
# --------------------------------------------------------------------------- #
def test_cost_truth_measured_excludes_unattributed_residual() -> None:
    for company in ("ALL", "ALFA"):
        sql = workbench_sql.cost_truth(30, company)
        assert "COST_ARM LIKE 'QUERY_COMPUTE%'" in sql
        assert "OBJECT_FQN <> 'UNATTRIBUTED'" in sql


# --------------------------------------------------------------------------- #
# Finding #4 -- SUCCESS_PCT SLO target is clamped to [0, 100]
# --------------------------------------------------------------------------- #
def _slo(metric: str, target: float) -> str:
    return create_slo_objective_sql(
        name="obj", entity_type="WAREHOUSE", entity_key="WH_X", metric_key=metric,
        comparator=(">=" if metric.endswith("SUCCESS_PCT") else "<="),
        target_value=target, error_budget_pct=1.0, window_days=30, owner="o", notes="", actor="a")


def test_success_pct_target_is_capped_at_100() -> None:
    # 150 on a percentage metric would make CURRENT_VALUE (<=100) fail the >= test forever
    sql = _slo("WAREHOUSE_SUCCESS_PCT", 150.0)
    assert "150" not in sql
    assert "100.0" in sql  # clamped to the ceiling


def test_latency_target_keeps_its_open_range() -> None:
    # a P95 latency target is seconds, not a percentage -- no 100 ceiling
    sql = _slo("WAREHOUSE_P95_SEC", 240.0)
    assert "240" in sql


def test_slo_editor_ui_caps_the_success_target() -> None:
    src = _src("app/ui/decision_studio.py")
    assert "max_value=100.0 if success_metric else None" in src


# --------------------------------------------------------------------------- #
# Finding #5 -- acceptance funnel top counts all booked-in-window, so verified <= estimated
# --------------------------------------------------------------------------- #
def test_acceptance_funnel_top_is_all_booked_in_window() -> None:
    sql = mart_sql.acceptance_funnel(90)
    # the SAVINGS_ESTIMATED (funnel-top) subquery no longer filters to the current ESTIMATED snapshot
    top = sql.split("AS SAVINGS_ESTIMATED")[0]
    tail = top.rsplit("SAVINGS_LEDGER", 1)[1]   # the subquery that produces SAVINGS_ESTIMATED
    assert "STATE = 'ESTIMATED'" not in tail
    # the verified/rejected snapshot counts still filter by state
    assert "STATE = 'VERIFIED'" in sql and "STATE = 'REJECTED'" in sql
