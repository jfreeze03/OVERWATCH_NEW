"""Per-alert AI evidence: right evidence shape per alert family (bugfix v4.235.0).

The Alerts 'Explain with AI' flow fed every COST_*/PERF_* alert the same
query-elapsed-by-warehouse pack, so cost/serverless/Cortex alerts were explained
with unrelated latency rows. These lock the resolver -> SQL -> prompt chain that
now grounds each alert in the metric that fired.
"""

from __future__ import annotations

import pandas as pd

from app.data import alert_evidence_sql
from app.logic.ai_prompts import alert_evidence_prompt
from app.logic.alert_evidence import EvidencePlan, plan_for_alert

# Titles copied from live alerts (owner screenshots 2026-08-17).
_CLOUD = "WH_ALFA_TRANSFORM_PRD cloud-services ratio 26.8% (24h)"
_AI = "AI/Cortex spend up 181% week-over-week ($601 vs $214 prior 7d)"
_SERVERLESS = "BACKUP credits up 999% week-over-week"
_SWEEP = "SERVICE WAREHOUSE_METERING spent 42.2 credits on 2026-08-09 (z=7.1)"
_DRIFT = "Query family p95 39.5s → 170.7s: CALL TRXS_ABC_FRAMEWORK.GW_CDA_TO_STAGE1_JSON_LOAD..."
_QUEUE = "WH_ALFA_BI_PRD queued 285.6 min in 24h"


def test_cloud_svc_alert_plans_cloud_services_evidence() -> None:
    plan = plan_for_alert("COST_CLOUD_SVC_RATIO", _CLOUD, "", "2026-08-17 00:07:51")
    assert plan is not None
    assert plan.kind == "cloud_svc"
    assert plan.warehouse == "WH_ALFA_TRANSFORM_PRD"
    sql = alert_evidence_sql.build(plan)
    assert "MART_CLOUD_SVC_DAILY" in sql
    assert "WH_ALFA_TRANSFORM_PRD" in sql  # scoped to the alert's warehouse


def test_cloud_svc_without_warehouse_withholds_evidence() -> None:
    # No warehouse to scope to -> better to withhold than to go account-wide.
    assert plan_for_alert("COST_CLOUD_SVC_RATIO", "cloud-services ratio high", "", "") is None


def test_ai_creep_plans_cortex_evidence() -> None:
    plan = plan_for_alert("COST_AI_CREEP", _AI, "", "2026-08-17 06:48:57")
    assert plan is not None and plan.kind == "cortex"
    sql = alert_evidence_sql.build(plan)
    assert "METERING_DAILY_HISTORY" in sql and "CREDITS_BILLED" in sql


def test_serverless_creep_scopes_to_named_service() -> None:
    plan = plan_for_alert("COST_SERVERLESS_CREEP", _SERVERLESS, "", "2026-08-17 00:08:03")
    assert plan is not None
    assert plan.kind == "metering_service" and plan.service == "BACKUP"
    sql = alert_evidence_sql.build(plan)
    assert "METERING_DAILY_HISTORY" in sql and "'BACKUP'" in sql


def test_anomaly_sweep_scopes_to_service_and_day() -> None:
    plan = plan_for_alert("COST_ANOMALY_SWEEP", _SWEEP, "", "2026-08-17 06:40:03")
    assert plan is not None
    assert plan.kind == "metering_service"
    assert plan.service == "WAREHOUSE_METERING"
    assert plan.day == "2026-08-09"  # the anomalous day named in the title, not raised_at
    sql = alert_evidence_sql.build(plan)
    assert "'WAREHOUSE_METERING'" in sql


def test_fingerprint_drift_scopes_to_the_named_family() -> None:
    plan = plan_for_alert("PERF_FINGERPRINT_DRIFT", _DRIFT, "", "2026-08-17 06:40:12")
    assert plan is not None and plan.kind == "query_family"
    assert plan.family_text.startswith("CALL TRXS_ABC_FRAMEWORK")
    assert not plan.family_text.endswith("...")  # trailing truncation marks stripped
    # This title names no warehouse; the family IS the identity, so scope by it alone.
    assert plan.warehouse == ""
    sql = alert_evidence_sql.build(plan)
    assert "QUERY_HISTORY" in sql
    assert "APPROX_PERCENTILE" in sql and "P95_SEC" in sql
    # scoped to the family by a literal LIKE (NOT the UI sanitizer, which strips
    # SQL keywords like CALL).
    assert "CALL TRXS" in sql and "ESCAPE '~'" in sql


def test_fingerprint_drift_uses_warehouse_when_the_title_carries_one() -> None:
    title = "Query family p95 10s → 40s on WH_ALFA_BI_PRD: SELECT * FROM ALFA_EDW_PRD.X"
    plan = plan_for_alert("PERF_FINGERPRINT_DRIFT", title, "", "2026-08-17")
    assert plan is not None and plan.warehouse == "WH_ALFA_BI_PRD"
    assert "WAREHOUSE_NAME = 'WH_ALFA_BI_PRD'" in alert_evidence_sql.build(plan)


def test_queued_minutes_scopes_to_the_warehouse() -> None:
    plan = plan_for_alert("PERF_QUEUED_MINUTES", _QUEUE, "", "2026-08-17 00:07:45")
    assert plan is not None and plan.kind == "queueing"
    assert plan.warehouse == "WH_ALFA_BI_PRD"
    sql = alert_evidence_sql.build(plan)
    assert "QUEUED_OVERLOAD_TIME" in sql and "WH_ALFA_BI_PRD" in sql


def test_unknown_family_falls_back_to_generic_but_only_for_cost_perf() -> None:
    generic = plan_for_alert("COST_SOMETHING_NEW", "WH_X spend odd", "", "2026-08-17")
    assert generic is not None and generic.kind == "generic"
    assert "ELAPSED_H_PRIOR_AVG" in alert_evidence_sql.build(generic)
    # Non cost/perf families get no AI-explain affordance at all.
    assert plan_for_alert("SEC_NEW_EXPOSURE", "new admin grant", "", "2026-08-17") is None


def test_prompt_framing_matches_the_family_and_forbids_invention() -> None:
    cloud_df = pd.DataFrame({
        "SAMPLE_TEXT": ["SHOW TABLES"], "QUERY_TYPE": ["SHOW"], "RUNS": [4000],
        "CS_CREDITS": [12.3], "CS_PER_1K_RUNS": [3.1], "AVG_EXEC_S": [0.2], "AVG_CACHE_PCT": [10],
    })
    cloud_prompt = alert_evidence_prompt("cloud_svc", _CLOUD, "", cloud_df, "last 7 days")
    assert "cloud-services credits" in cloud_prompt
    assert "CS_CREDITS" in cloud_prompt and "12.3" in cloud_prompt
    assert "Never invent" in cloud_prompt

    fam_df = pd.DataFrame({"DAY": ["2026-08-17"], "RUNS": [5], "P50_SEC": [40.0],
                           "P95_SEC": [170.0], "WAREHOUSE_NAME": ["WH_X"], "SAMPLE_TEXT": ["CALL X"]})
    fam_prompt = alert_evidence_prompt("query_family", _DRIFT, "", fam_df, "last 14 days")
    assert "p50/p95 latency" in fam_prompt and "170.0" in fam_prompt


def test_prompt_is_honest_when_evidence_is_empty() -> None:
    prompt = alert_evidence_prompt("cortex", _AI, "", pd.DataFrame(), "this week vs prior 7 days")
    assert "(no rows)" in prompt
    assert "inconclusive" in prompt  # the instruction to admit non-explanation survives


def test_evidence_plan_caption_fields_are_populated() -> None:
    plan = plan_for_alert("PERF_QUEUED_MINUTES", _QUEUE, "", "2026-08-17")
    assert isinstance(plan, EvidencePlan)
    assert plan.label == "this warehouse's queueing by hour"
    assert "warehouse WH_ALFA_BI_PRD" in plan.scope_note
