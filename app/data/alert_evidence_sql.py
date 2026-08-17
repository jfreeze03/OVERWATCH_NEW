"""SQL dispatch for per-alert AI evidence.

Maps an app.logic.alert_evidence.EvidencePlan to the SQL that assembles the
evidence for that alert family, reusing the existing cost/insights/mart builders
so each alert's AI evaluation is grounded in the metric that actually fired.

Pure string building — no Snowflake, no Streamlit; tested in
tests/test_alert_evidence.py.
"""

from __future__ import annotations

from app.data import cost_sql, insights_sql, mart_sql
from app.logic.alert_evidence import EvidencePlan


def build(plan: EvidencePlan) -> str:
    """Return the evidence SQL for one resolved plan."""
    if plan.kind == "cloud_svc":
        # Top query shapes by cloud-services credits on the alert's warehouse.
        return mart_sql.cloud_svc_top_shapes(plan.days, "ALL", plan.warehouse)
    if plan.kind == "cortex":
        # Daily AI/Cortex billed credits by service type (week-over-week).
        return cost_sql.cortex_daily_spend(plan.days)
    if plan.kind == "metering_service":
        # The named service's daily credits (spike day / growth in context).
        return insights_sql.metering_service_history(plan.days, plan.service)
    if plan.kind == "query_family":
        # The drifted family's own daily p50/p95 latency history.
        return insights_sql.query_family_drift_history(
            plan.days, plan.family_text, plan.warehouse)
    if plan.kind == "queueing":
        # The warehouse's worst queueing hours.
        return insights_sql.warehouse_queue_by_hour(plan.days, plan.warehouse)
    # generic fallback: the original query-families-by-elapsed pack.
    return insights_sql.anomaly_evidence(plan.day, plan.warehouse)
