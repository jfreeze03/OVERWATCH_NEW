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
    "SNOWPIPE": "Serverless",
    "SNOWPIPE_STREAMING": "Serverless",
    "SERVERLESS_TASK": "Serverless",
    "SERVERLESS_ALERTS": "Serverless",
    "AUTOMATIC_CLUSTERING": "Serverless",
    "MATERIALIZED_VIEW": "Serverless",
    "SEARCH_OPTIMIZATION": "Serverless",
    "QUERY_ACCELERATION": "Serverless",
    "SNOWPARK_CONTAINER_SERVICES": "Serverless",
    "COPY_FILES": "Serverless",
    "OPENFLOW_COMPUTE_SNOWFLAKE": "Serverless",
    # Snowflake stopped billing this service separately on 2026-03-01.  It
    # remains in historical windows, so the label must not imply a live fee.
    "HYBRID_TABLE_REQUESTS": "Hybrid requests (historical)",
    "REPLICATION": "Replication",
    "STORAGE": "Storage",
}


_DRILL_COVERAGE: dict[str, tuple[str, str, str]] = {
    "WAREHOUSE_METERING": ("Warehouse / day", "Warehouse metering", "Drill ready"),
    "WAREHOUSE_METERING_READER": (
        "Reader account / warehouse / day",
        "Reader metering",
        "Drill ready",
    ),
    "REPLICATION": ("Database / refresh", "Replication usage", "Drill ready"),
    "SNOWPARK_CONTAINER_SERVICES": (
        "Compute pool / application",
        "Container-services usage",
        "Drill ready",
    ),
    "CORTEX": ("User / model / warehouse", "Cortex usage", "Drill ready"),
    "AI": ("User / model / warehouse", "Cortex usage", "Drill ready"),
    "SNOWPIPE": ("Pipe / table", "Pipe usage", "Partial"),
    "SNOWPIPE_STREAMING": ("Client / table", "Streaming usage", "Partial"),
    "SERVERLESS_TASK": ("Task / day", "Task history", "Partial"),
    "AUTOMATIC_CLUSTERING": ("Table / day", "Clustering history", "Partial"),
    "MATERIALIZED_VIEW": ("Materialized view / day", "MV refresh history", "Partial"),
    "SEARCH_OPTIMIZATION": ("Table / day", "Search optimization history", "Partial"),
    "QUERY_ACCELERATION": ("Query / warehouse", "Query acceleration history", "Partial"),
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


def service_category(service: object) -> str:
    """Return the display category for a Snowflake metering service type."""
    normalized = str(service or "").upper()
    if "CORTEX" in normalized or normalized.startswith("AI") or "INTELLIGENCE" in normalized:
        return "AI / Cortex"
    return SERVICE_CATEGORY.get(normalized, "Other")


def _coverage_for(service: str) -> tuple[str, str, str]:
    normalized = str(service or "").upper()
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


def drill_ready_spend_share(frame: pd.DataFrame) -> float:
    """Return billed-dollar share whose status has a native drill path."""
    if frame.empty or not {"BILLED_USD", "DRILL_STATUS"}.issubset(frame.columns):
        return 0.0
    total = float(pd.to_numeric(frame["BILLED_USD"], errors="coerce").fillna(0.0).sum())
    if total <= 0:
        return 0.0
    ready = frame[frame["DRILL_STATUS"].astype(str) == "Drill ready"]
    covered = float(pd.to_numeric(ready["BILLED_USD"], errors="coerce").fillna(0.0).sum())
    return covered / total * 100.0
