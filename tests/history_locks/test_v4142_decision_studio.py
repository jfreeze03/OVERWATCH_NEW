"""v4.142 locks for prioritization, semantic cost bases and closed-loop decisions."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.data import workbench_sql
from app.logic.decision import prioritize_workloads, scenario_projection, slo_summary
from app.logic.workbench import create_slo_objective_sql, investigation_target
from app.ui import charts

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (_ROOT / path).read_text(encoding="utf-8")


def test_portfolio_ranks_impact_confidence_effort_and_next_move() -> None:
    frame = pd.DataFrame(
        {
            "FINGERPRINT": ["hot", "broad", "thin"],
            "RUNS": [300, 60, 2],
            "FAILS": [0, 6, 0],
            "CREDITS": [100, 30, 1],
            "ACTIVE_DAYS": [30, 20, 1],
            "DATABASES": [1, 5, 1],
            "USERS": [2, 20, 1],
            "AVG_CACHE_PCT": [5, 80, 0],
            "P95_SEC": [2, 100, 1],
        }
    )

    result = prioritize_workloads(frame, 3.68, 30).set_index("FINGERPRINT")

    assert result.loc["hot", "LANE"] == "ACT NOW"
    assert result.loc["hot", "NEXT_MOVE"] == "Cache or materialize"
    assert result.loc["broad", "NEXT_MOVE"] == "Stabilize failures"
    assert result.loc["broad", "EFFORT_PROXY"] == "HIGH"
    assert result.loc["thin", "LANE"] == "VALIDATE"
    assert result.loc["hot", "IMPACT_USD_30D"] == 368.0


def test_portfolio_gates_missing_behavioral_evidence() -> None:
    # Decision-Studio #2: a top-priority, high-confidence family whose cache/P95/fails are
    # all NULL (a family-mart join miss) must NOT be coerced to 0 and driven to ACT NOW /
    # "Cache or materialize". The evidence gate forces VALIDATE instead.
    frame = pd.DataFrame(
        {
            "FINGERPRINT": ["blind", "filler"],
            "RUNS": [300, 10],
            "FAILS": [None, 0],
            "CREDITS": [1000, 5],
            "ACTIVE_DAYS": [30, 5],
            "DATABASES": [1, 1],
            "USERS": [2, 1],
            "AVG_CACHE_PCT": [None, 50.0],
            "P95_SEC": [None, 3.0],
        }
    )
    result = prioritize_workloads(frame, 3.68, 30).set_index("FINGERPRINT")
    # blind is the top-priority row with confidence >= 0.65 (so it would be ACT NOW), but
    # it carries NO behavioral evidence — the gate sends it to VALIDATE, not the ACT lane.
    assert result.loc["blind", "CONFIDENCE"] >= 0.65
    assert result.loc["blind", "LANE"] == "VALIDATE"
    assert result.loc["blind", "NEXT_MOVE"] == "Validate evidence"
    assert result.loc["blind", "EVIDENCE_COVERAGE"] == 0.0


def test_cost_truth_renders_no_evidence_not_a_fabricated_zero() -> None:
    # DS #4: cost_truth always returns 4 rows; an empty basis is NULL CREDITS. The render
    # must show "No evidence" per absent basis, not safe_float(...)-> $0.00.
    ct = _source("app/ui/decision_studio.py").split("def _cost_truth", 1)[1].split("\ndef ", 1)[0]
    assert 'pd.notna(row.get("CREDITS"))' in ct          # presence from the raw column
    assert "No evidence" in ct
    assert 'if present.get("METERED")' in ct             # KPI value gated on presence
    assert 'if present.get("MEASURED")' in ct
    assert 'if present.get("ALLOCATED")' in ct
    # the metered-ratio caption is skipped unless the bases are actually present
    assert 'present.get("MEASURED")\n' in ct or "present.get(\"MEASURED\")" in ct


def test_product_economics_scopes_the_catalog_by_company() -> None:
    # DS #3: the catalog CTE drives the product list, maps, warehouse_cost AND task_health,
    # so company-filtering it is the only way to scope tasks + the product list, not just cost.
    scoped = workbench_sql.data_product_economics(30, "Trexis")
    cat = scoped.split("WITH catalog AS (", 1)[1].split("), products AS (", 1)[0]
    assert "UPPER(COMPANY)" in cat and "TREXIS" in cat.upper()
    # ALL scope adds no catalog predicate
    all_cat = (workbench_sql.data_product_economics(30, "ALL")
               .split("WITH catalog AS (", 1)[1].split("), products AS (", 1)[0])
    assert "UPPER(COMPANY)" not in all_cat


def test_product_criticality_ranks_by_severity_not_lexically() -> None:
    # DS Wave-2 #12: MAX(CRITICALITY) returned 'STANDARD' for a product that contained a
    # CRITICAL entity (alphabetically S > C), masking it. Rank by severity (MIN rank).
    sql = workbench_sql.data_product_economics(30, "ALL")
    assert "MAX(CRITICALITY)" not in sql
    assert "WHEN 'CRITICAL' THEN 1" in sql and "WHEN 1 THEN 'CRITICAL'" in sql
    # ambiguous ownership (entities with different owners) is flagged, not hidden by MAX
    assert "COUNT(DISTINCT NULLIF(TRIM(OWNER_NAME), '')) > 1 AS OWNER_CONFLICT" in sql


def test_products_board_surfaces_owner_conflict() -> None:
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "app" / "ui"
           / "decision_studio.py").read_text(encoding="utf-8")
    assert '"OWNER_CONFLICT"' in src      # displayed column
    assert "Owner conflicts" in src       # KPI


def test_product_consumer_reads_scopes_catalog_not_access_history() -> None:
    # #28: consumer reach maps ACCESS_HISTORY reads to products via the catalog; company
    # scoping lives ENTIRELY on the catalog CTE — NEVER a per-row COMPANY_FOR_USER on the
    # huge ACCESS_HISTORY (that is the per-row UDF blowup companies.py warns against).
    scoped = workbench_sql.product_consumer_reads(30, "Trexis")
    cat = scoped.split("WITH catalog AS (", 1)[1].split("), object_map AS (", 1)[0]
    assert "UPPER(COMPANY)" in cat and "TREXIS" in cat.upper()
    assert "COMPANY_FOR_USER" not in scoped
    assert "UPPER(COMPANY)" not in workbench_sql.product_consumer_reads(30, "ALL")
    # reads, not writes: BASE_OBJECTS_ACCESSED, never OBJECTS_MODIFIED
    assert "BASE_OBJECTS_ACCESSED" in scoped and "OBJECTS_MODIFIED" not in scoped
    # match the domains the cost ETL charges, or an MV shows cost with zero reads
    assert "IN ('Table', 'Materialized view')" in scoped
    # the cost denominator (DISTINCT_CONSUMERS) is scoped to the RECENT window so it
    # aligns with the `days`-window object cost it is divided by (not the 2*days horizon)
    assert "USER_NAME, NULL)) AS DISTINCT_CONSUMERS" in scoped
    for col in ("DISTINCT_CONSUMERS", "TOTAL_READS", "RECENT_READS", "PRIOR_READS", "LAST_READ"):
        assert col in scoped, col


def test_cost_per_consumer_wired_into_products_board() -> None:
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "app" / "ui"
           / "decision_studio.py").read_text(encoding="utf-8")
    assert "product_consumer_reads(" in src and "insights.product_retirement(" in src
    assert "RETIREMENT_VERDICT" in src and "Retire candidates" in src
    assert "decision_product_consumers_" in src and "probe=True" in src


def test_mark_watched_flags_watchlist_by_type_case_insensitively() -> None:
    from app.logic.workbench import mark_watched
    frame = pd.DataFrame({"FINGERPRINT": ["abc", "def", "ghi"]})
    wl = pd.DataFrame({
        "ENTITY_TYPE": ["QUERY_FINGERPRINT", "WAREHOUSE", "query_fingerprint"],
        "ENTITY_KEY": ["ABC", "abc", "ghi"],   # case-insensitive; WAREHOUSE 'abc' is a diff type
    })
    watched = mark_watched(frame, wl, "QUERY_FINGERPRINT", "FINGERPRINT")
    assert watched.tolist() == [True, False, True]
    # graceful on empty / absent inputs — never raises
    assert mark_watched(frame, None, "QUERY_FINGERPRINT", "FINGERPRINT").tolist() == [False, False, False]
    assert mark_watched(None, wl, "QUERY_FINGERPRINT", "FINGERPRINT").empty


def test_portfolio_surfaces_and_pins_watched_families() -> None:
    # DS #1: Watch now does something on the primary decision table — a WATCHED flag,
    # a "Watching" count, and a within-lane pin (lane primary, watched secondary).
    ds = _source("app/ui/decision_studio.py")
    assert 'mark_watched(portfolio, _wl, "QUERY_FINGERPRINT", "FINGERPRINT")' in ds
    assert '"label": "Watching"' in ds
    assert '["_LR", "WATCHED", "PRIORITY_SCORE"]' in ds     # pinned within lane, not above it
    assert '"WATCHED", "EFFORT_PROXY"' in ds                # WATCHED leads the context columns


def test_mark_watched_pairs_matches_type_and_key_pair() -> None:
    from app.logic.workbench import mark_watched_pairs
    frame = pd.DataFrame({
        "SOURCE_ENTITY_TYPE": ["WAREHOUSE", "QUERY_FINGERPRINT", "WAREHOUSE"],
        "SOURCE_ENTITY_KEY": ["WH_A", "abc", "WH_B"],
    })
    wl = pd.DataFrame({
        "ENTITY_TYPE": ["WAREHOUSE", "QUERY_FINGERPRINT"],
        "ENTITY_KEY": ["wh_a", "xyz"],       # WH_A matches (case-insensitive); abc/WH_B don't
    })
    out = mark_watched_pairs(frame, wl, "SOURCE_ENTITY_TYPE", "SOURCE_ENTITY_KEY")
    assert out.tolist() == [True, False, False]
    assert (mark_watched_pairs(frame, None, "SOURCE_ENTITY_TYPE", "SOURCE_ENTITY_KEY").tolist()
            == [False, False, False])


def test_action_center_pins_watched_actions_within_severity() -> None:
    ds = _source("app/ui/decision_studio.py")
    assert 'mark_watched_pairs(adf, _wl, "SOURCE_ENTITY_TYPE", "SOURCE_ENTITY_KEY")' in ds
    assert '["_SR", "WATCHED"]' in ds        # pinned within severity band, not above CRITICAL


def test_scenario_deduplicates_entities_and_separates_closed_work() -> None:
    actions = pd.DataFrame(
        {
            "ACTION_ID": ["a1", "a2", "b1", "closed"],
            "STATUS": ["OPEN", "OPEN", "IN_PROGRESS", "DONE"],
            "SOURCE_ENTITY_TYPE": ["WAREHOUSE"] * 4,
            "SOURCE_ENTITY_KEY": ["WH_A", "WH_A", "WH_B", "WH_C"],
            "CONFIDENCE": [0.9, 0.8, 0.7, 1.0],
            "ESTIMATED_USD": [100, 80, 50, 500],
        }
    )

    result = scenario_projection(
        actions, adoption_pct=50, realization_pct=80, confidence_floor=0.6,
    )

    assert result == {
        "candidates": 2.0,
        "gross_estimate": 150.0,
        "expected_capture": 60.0,
        "low_capture": 45.0,
        "high_capture": 75.0,
    }


def test_slo_summary_keeps_breach_and_missing_evidence_distinct() -> None:
    frame = pd.DataFrame(
        {
            "STATUS": ["MET", "BREACH", "NO_DATA"],
            "BURN_MULTIPLE": [0.4, 3.2, None],
        }
    )
    assert slo_summary(frame) == {
        "total": 3.0,
        "met": 1.0,
        "breach": 1.0,
        "no_data": 1.0,
        "stale": 0.0,       # Wave-2 #11: stale evidence is a distinct verdict
        "worst_burn": 3.2,
        "has_burn": 1.0,    # Wave-2 #10: a success objective carries a burn
    }


@pytest.mark.parametrize(
    "builder",
    (
        lambda: workbench_sql.workload_portfolio(30, "Trexis"),
        workbench_sql.slo_cockpit,
        lambda: workbench_sql.data_product_economics(30, "Trexis"),
        lambda: workbench_sql.cost_truth(30, "Trexis"),
        lambda: workbench_sql.product_consumer_reads(30, "Trexis"),
        lambda: workbench_sql.product_consumer_reads(30, "ALL"),
    ),
)
def test_decision_sql_builders_parse(builder) -> None:
    sqlglot.parse(builder(), dialect="snowflake")


def test_workload_company_scope_applies_to_behavior_and_cost() -> None:
    statement = workbench_sql.workload_portfolio(30, "Trexis")
    assert statement.count("p.COMPANY = 'Trexis'") == 2
    assert "JOIN scoped_families" in statement
    # V082 (DS #3): the family mart now carries COMPANY at row grain, so the behavior
    # side scopes on the real company, not the lossy ANY_VALUE(DATABASE_NAME) heuristic.
    assert "s.COMPANY = f.COMPANY" in statement
    assert "s.DATABASE_NAME = f.DATABASE_NAME" not in statement


def test_workload_portfolio_keeps_fails_raw_for_evidence_presence() -> None:
    # Codex #1 (confirmed): FAILS must stay RAW (NULL on a family-mart join miss), NOT
    # COALESCE(,0) — else decision.py's notna() presence-tracking (see the
    # test_portfolio_gates_missing_behavioral_evidence gate) can't tell a blind family from
    # a measured 0, so has_behavior is always true and EVIDENCE_COVERAGE floors at 0.33.
    sql = workbench_sql.workload_portfolio(30, "ALFA")
    assert "f.FAILS AS FAILS" in sql
    assert "COALESCE(f.FAILS" not in sql
    assert "f.AVG_CACHE_PCT" in sql and "f.P95_SEC" in sql  # the raw-NULL siblings it matches


def test_cost_truth_and_product_economics_refuse_false_additivity() -> None:
    truth = workbench_sql.cost_truth(30, "Trexis")
    products = workbench_sql.data_product_economics(30, "Trexis")
    for basis in ("BILLED", "METERED", "MEASURED", "ALLOCATED"):
        assert f"'{basis}'" in truth
    assert "do not add to rows below" in truth
    assert "MEASURED_OBJECT_CREDITS" in products
    assert "METERED_WAREHOUSE_CREDITS" in products
    assert "MEASURED_OBJECT_CREDITS + METERED_WAREHOUSE_CREDITS" not in products


def test_slo_sql_burns_only_success_rate_objectives() -> None:
    statement = workbench_sql.slo_cockpit()
    assert "o.METRIC_KEY ILIKE '%SUCCESS_PCT'" in statement
    assert "BURN_MULTIPLE" in statement
    assert "TASK_P95_EXEC_SEC" in statement and "QUERY_P95_SEC" in statement


def test_slo_write_validates_metric_and_escapes() -> None:
    statement = create_slo_objective_sql(
        name="O'Brien loader", entity_type="TASK", entity_key="DB.S.T",
        metric_key="TASK_SUCCESS_PCT", comparator=">=", target_value=99.5,
        error_budget_pct=0.5, window_days=30, owner="O'Brien", notes="owner's SLO",
        actor="Joe",
    )
    sqlglot.parse(statement, dialect="snowflake")
    assert "O'Brien" not in statement and "TASK_SUCCESS_PCT" in statement
    with pytest.raises(ValueError):
        create_slo_objective_sql(
            name="bad", entity_type="TASK", entity_key="DB.S.T", metric_key="MADE_UP",
            comparator=">=", target_value=99, error_budget_pct=1, window_days=30,
            owner="", notes="", actor="Joe",
        )


def test_data_product_is_a_first_class_investigation_entity() -> None:
    target = investigation_target("Data product", "Finance")
    assert target.context == {"entity_type": "DATA_PRODUCT", "entity_key": "Finance"}


def test_decision_studio_wires_all_six_views_on_its_page() -> None:
    # rec8: Decision Studio is now its own page; the shell dispatches into the six
    # section bodies that still live in app/ui/decision_studio.py.
    studio = _source("app/ui/decision_studio.py")
    page = _source("app/ui/pages/decision_studio.py")
    for view in ("Portfolio", "SLOs", "Products", "Cost Truth", "Scenarios", "Experiments"):
        assert f'"{view}"' in page
    assert 'lazy_sections(' in page and 'key="decision_section"' in page
    # Control Room no longer carries Decision Studio (Entity 360 stays).
    assert '"Decision Studio"' not in _source("app/ui/pages/control_room.py")
    assert "Verified savings never enter the projection" in studio


def test_workload_portfolio_chart_compiles(monkeypatch: pytest.MonkeyPatch) -> None:
    rendered = []
    monkeypatch.setattr(charts.st, "altair_chart", lambda chart, **_kwargs: rendered.append(chart))
    frame = pd.DataFrame(
        {
            "FINGERPRINT": ["one"],
            "IMPACT_USD_30D": [125.0],
            "CONFIDENCE": [0.9],
            "BLAST_RADIUS": [4],
            "PRIORITY_SCORE": [20.0],
            "LANE": ["ACT NOW"],
            "NEXT_MOVE": ["Cache or materialize"],
        }
    )

    charts.workload_portfolio(frame)

    assert len(rendered) == 1
    spec = rendered[0].to_dict()
    assert spec["encoding"]["x"]["field"] == "IMPACT_USD_30D"
    assert spec["encoding"]["y"]["field"] == "CONFIDENCE"
