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
    "PIPE": ("Pipe / table", "Pipe usage", "Partial"),
    "SNOWPIPE": ("Pipe / table", "Pipe usage", "Partial"),
    "SNOWPIPE_STREAMING": ("Client / table", "Streaming usage", "Partial"),
    "SERVERLESS_TASK": ("Task / day", "Task history", "Partial"),
    "AUTO_CLUSTERING": ("Table / day", "Clustering history", "Partial"),
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
    # NOTE: narrower than _is_ai_family on purpose. A CORTEX_* service has a
    # per-user CORTEX_CODE_*_USAGE_HISTORY drill; CoCo/CoWork COMPUTE does not,
    # so it must keep the honest "Service total only" default rather than claim
    # a Cortex drill it cannot deliver. The rate fix (service_category) and the
    # drill claim are deliberately decoupled.
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


def material_unmapped_services(inventory: pd.DataFrame) -> pd.DataFrame:
    """rec #21: the live half of the unknown-SERVICE_TYPE canary.

    Given a ``service_coverage_inventory`` frame, return the rows that fell to the
    catch-all "Other" category AND cleared the materiality bar — i.e. real billed
    spend the taxonomy does not yet name. An empty frame means full coverage; any
    row is a prompt to add a SERVICE_CATEGORY key (Admin surfaces this, and
    ``tests/test_cost_coverage_taxonomy.py`` is the CI counterpart that fails on
    a KNOWN type going unmapped).
    """
    if inventory.empty or not {"CATEGORY", "MATERIALITY"}.issubset(inventory.columns):
        return inventory.iloc[0:0]
    mask = (inventory["CATEGORY"].astype(str) == "Other") & (
        inventory["MATERIALITY"].astype(str) == "Material"
    )
    return inventory[mask].reset_index(drop=True)


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
