"""Round-3 UI-layer bug hunt (v4.340.0) — locks for the confirmed fixes.

Behavioral where the code path is unit-reachable (the ROI ledger totals, the
change-risk SQL); source-shape locks for the render-inline page fixes. Each lock
pins the DEFECT it prevents, so a silent revert fails here. (The brief "-1.0"
sentinel is locked in test_dofirst_wave; the resize-saving booking in
test_batch_a_audit_fixes.)
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.data import security_sql
from app.logic.actions import LEDGER_VERIFIED, ledger_totals

_ROOT = Path(__file__).resolve().parents[2]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# 2) ROI Realization delta ties to the % population -------------------------
def test_ledger_totals_exposes_the_realization_population():
    # A verified row with a zero/absent estimate is excluded from realization_pct
    # (_est_pos guard); the KPI delta must render the SAME restricted numerator/
    # denominator, or "$X of $Y estimated" contradicts the % beside it.
    df = pd.DataFrame({
        "STATE": [LEDGER_VERIFIED, LEDGER_VERIFIED],
        "ESTIMATED_USD": [1000.0, 0.0], "VERIFIED_USD": [1000.0, 200.0],
        "VERIFIED_AT": [None, None], "CREATED_AT": [None, None],
    })
    t = ledger_totals(df)
    assert t["realization_pct"] == 100.0
    # restricted to the positive-estimate row: 1000 of 1000, NOT the all-verified 1200/1000
    assert t["realized_verified_usd"] == 1000.0 and t["realized_estimated_usd"] == 1000.0
    assert t["verified_usd"] == 1200.0            # all-verified totals still exposed, unused by the delta
    ds = _src("app/ui/decision_studio.py")
    assert "totals['realized_verified_usd']" in ds and "totals['realized_estimated_usd']" in ds


# 8) Change-risk destructive count reflects ALL groups, not the top 200 ------
def test_change_risk_destructive_sql_carries_pre_limit_total():
    sql = security_sql.change_risk_destructive_breakdown(7)
    assert "SUM(COUNT(*)) OVER () AS TOTAL_EVENTS" in sql   # window total, evaluated before LIMIT
    assert "LIMIT 200" in sql                                # per-group table still capped
    sc = _src("app/ui/security_center.py")
    assert 'df["TOTAL_EVENTS"].iloc[0]' in sc                # headline uses the true total
    assert "if total > _shown:" in sc                        # discloses truncation


# 1) Admin stale-source keys on load age, never row count -------------------
def test_admin_stale_diagnose_keys_on_age_not_row_count():
    a = _src("app/ui/pages/admin.py")
    stale_block = a.split("Diagnose stale sources", 1)[1][:2600]
    # FRESH-1 (round 12): now cadence-aware (3h hourly / 30h daily, matching health_strip),
    # but still keyed on AGE only — never row count.
    assert "stale = fresh.df[(_hrs > _lim_hrs) | _hrs.isna()]" in stale_block   # no ROW_COUNT disjunct
    assert 'THRESHOLDS["stale_daily_fact_hours"]' in stale_block
    assert "(_rows.fillna(0) <= 0)" not in stale_block
    assert 'if pd.isna(s.get("HOURS_SINCE_LOAD")):' in stale_block        # "never filled" only if never loaded


# 3) Effective-access floats escalation paths before head(80) ---------------
def test_effective_access_retains_escalation_paths_under_the_cap():
    sc = _src("app/ui/security_center.py")
    blk = sc.split("Access path for", 1)[1][:1200]
    assert '"SELF_ESCALATION", "REACHES_ADMIN"' in blk       # sort keys
    assert 'kind="stable"' in blk and ".head(80)" in blk     # stable sort preserves risk order within groups


# 4) Priciest-procedure KPI picks the priciest PER CALL ---------------------
def test_priciest_procedure_kpi_sorts_by_per_call():
    uc = _src("app/ui/pages/cost_parts/unit_costs.py")
    blk = uc.split("Priciest procedure (per call)", 1)[0][-400:]
    assert 'sort_values(' in blk and '"_pc"' in blk          # re-sort by CREDITS_PER_CALL
    assert '_pp["CREDITS_PER_CALL"].map(safe_float)' in blk


# 5) Overview vs-prior delta labels the REAL (clamped) window ---------------
def test_overview_spend_delta_labels_effective_window():
    ov = _src("app/ui/pages/overview.py")
    assert "resolve_effective_window(days)" in ov
    assert 'vs prior {_eff}d' in ov                          # not the unclamped {days}
    assert 'vs prior {days}d' not in ov


# 7) Ask bullets neutralize untrusted SQL / error text ----------------------
def test_ask_bullets_wrap_untrusted_data_as_code():
    reg = _src("app/logic/ask/registry.py")
    assert "def _code(" in reg
    assert "_code(_sample(r.get('SAMPLE_TEXT'), 90))" in reg   # cloud-services SQL sample
    assert "_code(err)" in reg                                 # task LAST_ERROR string


# 9) Contract balance chart is currency-aware -------------------------------
def test_contract_balance_chart_uses_the_org_currency():
    c = _src("app/ui/pages/cost_parts/contract.py")
    assert 'title=f"Remaining balance ({_ccy})"' in c
    assert '_bal_unit = "usd" if _ccy == "USD" else "count"' in c
    assert 'title="Remaining balance ($)"' not in c           # the hardcoded $ is gone


# 11) Resize target widget key is entity-scoped -----------------------------
def test_resize_target_widget_key_is_warehouse_scoped():
    opt = _src("app/ui/pages/cost_parts/optimize.py")
    assert 'key=f"sizing_to_{srow[\'WAREHOUSE_NAME\']}"' in opt
    assert 'key="sizing_to"' not in opt                       # no fixed key leaking across rows
