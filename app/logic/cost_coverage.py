"""Cost-service classification and drill-coverage summaries.

The billing feed is authoritative at service grain.  This module describes
how far each service can be drilled without pretending that a narrower view
is additive when Snowflake does not expose the required allocation key.
"""

from __future__ import annotations

import pandas as pd

from app.logic.formulas import safe_float

SERVICE_CATEGORY: dict[str, str] = {
    "WAREHOUSE_METERING": "Warehouse",
    "WAREHOUSE_METERING_READER": "Warehouse (reader)",
    # v4.158.0: METERING_DAILY_HISTORY names Snowpipe "PIPE" and auto-clustering
    # "AUTO_CLUSTERING". The old keys ("SNOWPIPE", "AUTOMATIC_CLUSTERING") never
    # matched, so both landed in "Other" and were flagged as coverage gaps.
    # Keep the old spellings as aliases in case org-currency uses them.
    "PIPE": "Serverless",
    "SNOWPIPE": "Serverless",
    "SNOWPIPE_STREAMING": "Serverless",
    "SERVERLESS_TASK": "Serverless",
    "SERVERLESS_ALERTS": "Serverless",
    "AUTO_CLUSTERING": "Serverless",
    "AUTOMATIC_CLUSTERING": "Serverless",
    "MATERIALIZED_VIEW": "Serverless",
    "SEARCH_OPTIMIZATION": "Serverless",
    "QUERY_ACCELERATION": "Serverless",
    "SNOWPARK_CONTAINER_SERVICES": "Serverless",
    "COPY_FILES": "Serverless",
    "OPENFLOW_COMPUTE_SNOWFLAKE": "Serverless",
    # rec #31: newer serverless meters that previously fell to "Other" and dropped
    # out of the Serverless total while the exec-board serverless arm counted them.
    "LOGGING": "Serverless",
    "TRUST_CENTER": "Serverless",
    "DATA_QUALITY_MONITORING": "Serverless",
    # Snowflake stopped billing this service separately on 2026-03-01.  It
    # remains in historical windows, so the label must not imply a live fee.
    "HYBRID_TABLE_REQUESTS": "Hybrid requests (historical)",
    "REPLICATION": "Replication",
    "STORAGE": "Storage",
}


# DRILL_STATUS vocabulary: "Drill ready" (a native per-dimension view is wired and
# tested), "Object-ledger drill" (a per-object drill ships via the object cost
# ledger — rec #44), "Service total only" (only the service total is trustworthy).
# drill_ready_spend_share counts the first two. rec #42/#43/#47: statuses that
# advertised a drill with no backing query were downgraded to the truth.
_DRILL_COVERAGE: dict[str, tuple[str, str, str]] = {
    "WAREHOUSE_METERING": ("Warehouse / day", "Warehouse metering", "Drill ready"),
    # rec #43: no reader-account drill builder exists — do not advertise one.
    "WAREHOUSE_METERING_READER": (
        "Reader account (service total)",
        "Reader metering",
        "Service total only",
    ),
    # rec #47: DATABASE_REPLICATION_USAGE_HISTORY is empty here (replication bills
    # on the DR account); the Spend panel already falls back to org currency.
    "REPLICATION": ("Account (org currency)", "Org rate-card", "Service total only"),
    "SNOWPARK_CONTAINER_SERVICES": (
        "Compute pool / application",
        "Container-services usage",
        "Drill ready",
    ),
    # rec #42: a per-user/model Cortex drill exists, but no warehouse-grain one —
    # drop "warehouse" from the advertised grain.
    "CORTEX": ("User / model", "Cortex usage", "Drill ready"),
    "AI": ("User / model", "Cortex usage", "Drill ready"),
    # rec #43: no PIPE_USAGE_HISTORY / SNOWPIPE_STREAMING_CLIENT_HISTORY /
    # QUERY_ACCELERATION_HISTORY is read anywhere, so only the service total holds.
    "PIPE": ("Service total", "Pipe usage (no per-pipe drill wired)", "Service total only"),
    "SNOWPIPE": ("Service total", "Pipe usage (no per-pipe drill wired)", "Service total only"),
    "SNOWPIPE_STREAMING": ("Service total", "Streaming usage (no per-client drill wired)",
                           "Service total only"),
    "QUERY_ACCELERATION": ("Service total", "QAS (folded into pattern cost only)",
                           "Service total only"),
    # rec #44: each of these has an EXACT native per-object *_HISTORY key AND is
    # materialized in the object cost ledger (FACT_OBJECT_COST_DAILY /
    # clustering_by_table / serverless_task_daily) — a real, shipping drill.
    "SERVERLESS_TASK": ("Task / day", "Task history (object ledger)", "Object-ledger drill"),
    "AUTO_CLUSTERING": ("Table / day", "Clustering history (object ledger)", "Object-ledger drill"),
    "AUTOMATIC_CLUSTERING": ("Table / day", "Clustering history (object ledger)", "Object-ledger drill"),
    "MATERIALIZED_VIEW": ("Materialized view / day", "MV refresh (object ledger)", "Object-ledger drill"),
    "SEARCH_OPTIMIZATION": ("Table / day", "Search-opt history (object ledger)", "Object-ledger drill"),
}

_COLUMNS = [
    "SERVICE_TYPE",
    "CATEGORY",
    "BILLED_CREDITS",
    "BILLED_USD",
    "SHARE_PCT",
    "DETAIL_GRAIN",
    "DETAIL_METHOD",
    "DRILL_STATUS",
    "MATERIALITY",
]


def _is_ai_family(normalized: str) -> bool:
    """True for Cortex/AI service types billed at the AI credit rate.

    Includes Cortex Code / CoWork ("CoCo") — its metering names (e.g.
    ``SNOWFLAKE_COCO_SNOWSIGHT``) carry neither a CORTEX token nor an AI prefix,
    so a name-only match dropped them to "Other" and priced them at the compute
    rate — over-stating the row and violating the never-inline-AI-rate rule.
    """
    return (
        "CORTEX" in normalized
        or normalized.startswith("AI")
        or "INTELLIGENCE" in normalized
        or "COCO" in normalized
        or "COWORK" in normalized
    )


def service_category(service: object) -> str:
    """Return the display category for a Snowflake metering service type."""
    normalized = str(service or "").upper()
    if _is_ai_family(normalized):
        return "AI / Cortex"
    return SERVICE_CATEGORY.get(normalized, "Other")


def _coverage_for(service: str) -> tuple[str, str, str]:
    normalized = str(service or "").upper()
    # rec #48: Cortex Code / CoWork (CoCo) IS drilled to USER grain by the AI
    # Chargeback tab via FACT_AI_USAGE_DAILY (mart27_sql.ai_code_daily). The old
    # code left it on the "Service total only" default, marking a material AI line
    # as an un-drillable gap while the app already drilled it.
    if "COCO" in normalized or "COWORK" in normalized:
        return ("User / day", "FACT_AI_USAGE_DAILY (Cortex Code)", "Drill ready")
    # rec #42: AI_SERVICES aggregates Cortex functions PLUS Analyst / Search /
    # Document AI / Fine-tuning — none with a per-user drill — so it must NOT
    # inherit the Cortex "Drill ready" grain until per-service views back it.
    if normalized.startswith("AI_SERVICE"):
        return ("Service total", "AI services metering (no per-feature drill wired)",
                "Service total only")
    # The genuine CORTEX_* functions do have a per-user/model drill (grain fixed
    # to "User / model" — no warehouse-grain drill exists, rec #42).
    if "CORTEX" in normalized or normalized.startswith("AI") or "INTELLIGENCE" in normalized:
        return _DRILL_COVERAGE["CORTEX"]
    return _DRILL_COVERAGE.get(
        normalized,
        ("Service total", "Metering daily history", "Service total only"),
    )


def service_coverage_inventory(
    frame: pd.DataFrame,
    rate: float,
    ai_rate: float,
    *,
    material_usd: float = 500.0,
    material_share_pct: float = 1.0,
) -> pd.DataFrame:
    """Summarize billed services and disclose available drill granularity.

    The function operates on the already-loaded billing frame.  It never
    allocates account-level services to a company or object without a native
    Snowflake key.
    """
    if frame.empty or not {"SERVICE_TYPE", "CREDITS_BILLED"}.issubset(frame.columns):
        return pd.DataFrame(columns=_COLUMNS)

    grouped = (
        frame.assign(
            SERVICE_TYPE=frame["SERVICE_TYPE"].fillna("UNKNOWN").astype(str).str.upper(),
            CREDITS_BILLED=pd.to_numeric(frame["CREDITS_BILLED"], errors="coerce").fillna(0.0),
        )
        .groupby("SERVICE_TYPE", as_index=False)["CREDITS_BILLED"]
        .sum()
    )
    grouped["CATEGORY"] = grouped["SERVICE_TYPE"].map(service_category)
    grouped["BILLED_USD"] = grouped.apply(
        lambda row: safe_float(row["CREDITS_BILLED"])
        * (ai_rate if row["CATEGORY"] == "AI / Cortex" else rate),
        axis=1,
    )
    total_usd = float(grouped["BILLED_USD"].sum())
    grouped["SHARE_PCT"] = (
        grouped["BILLED_USD"] / total_usd * 100.0 if total_usd > 0 else 0.0
    )
    coverage = grouped["SERVICE_TYPE"].map(_coverage_for)
    grouped[["DETAIL_GRAIN", "DETAIL_METHOD", "DRILL_STATUS"]] = pd.DataFrame(
        coverage.tolist(), index=grouped.index
    )
    grouped["MATERIALITY"] = grouped.apply(
        lambda row: (
            "Material"
            if safe_float(row["BILLED_USD"]) >= material_usd
            or safe_float(row["SHARE_PCT"]) >= material_share_pct
            else "Below threshold"
        ),
        axis=1,
    )
    grouped = grouped.rename(columns={"CREDITS_BILLED": "BILLED_CREDITS"})
    return grouped[_COLUMNS].sort_values("BILLED_USD", ascending=False).reset_index(drop=True)


# Statuses that back a real per-dimension drill (native view or object ledger,
# rec #44) — the billed-dollar share the coverage KPI counts as drillable.
_DRILLABLE_STATUSES = ("Drill ready", "Object-ledger drill")


def drill_ready_spend_share(frame: pd.DataFrame) -> float:
    """Return billed-dollar share whose status has a real per-object drill path.

    rec #44: counts both native "Drill ready" and "Object-ledger drill" (serverless
    task/table lines the app already drills via the object cost ledger), so a
    serverless-heavy account no longer reads artificially low on the KPI.
    """
    if frame.empty or not {"BILLED_USD", "DRILL_STATUS"}.issubset(frame.columns):
        return 0.0
    total = float(pd.to_numeric(frame["BILLED_USD"], errors="coerce").fillna(0.0).sum())
    if total <= 0:
        return 0.0
    ready = frame[frame["DRILL_STATUS"].astype(str).isin(_DRILLABLE_STATUSES)]
    covered = float(pd.to_numeric(ready["BILLED_USD"], errors="coerce").fillna(0.0).sum())
    return covered / total * 100.0
