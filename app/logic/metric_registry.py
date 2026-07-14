"""Metric registry — the single semantic contract for every cost number.

Codex architectural item 9 (2026-07-14): the app's core weakness was multiple
semantic contracts for the same figure (a "warehouse dollar" meant billed on
one page, exact usage on another, an allocation on a third). This module makes
each contract explicit — method, grain, source, timezone, latency, and the
formula version that produced it — so a reader (and a drift-guard test) always
knows what a number means and how it was derived. Pure module: no Streamlit,
no Snowflake.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- Method: HOW a number is derived (the contract's core) ------------------
BILLED = "BILLED"        # ties to the invoice: billed credits / org rate-card currency
METERED = "METERED"      # exact metering usage at its native grain (idle in, CS unadjusted)
MEASURED = "MEASURED"    # exact attributed compute (QUERY_ATTRIBUTION_HISTORY), idle excluded
ALLOCATED = "ALLOCATED"  # spread from a coarser grain by a share (idle included) — an estimate
ESTIMATED = "ESTIMATED"  # modeled from bytes/credits x a configured rate
METHODS = (BILLED, METERED, MEASURED, ALLOCATED, ESTIMATED)

UTC = "UTC"
ACCOUNT_TZ = "America/Chicago"


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    method: str          # one of METHODS
    grain: str
    source: str
    timezone: str
    latency: str
    formula_version: str
    notes: str = ""


METRICS: tuple[Metric, ...] = (
    Metric("account_billed_spend", "Account billed spend", BILLED,
           "account / day / service", "ACCOUNT_USAGE.METERING_DAILY_HISTORY (CREDITS_BILLED)",
           UTC, "up to 24h", "v4.30",
           "Billed = used + cloud-services adjustment. The number that ties to the bill."),
    Metric("org_reconciliation", "Org rate-card dollars", BILLED,
           "account / month / rating-type", "ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY",
           UTC, "up to ~72h; mutates until month close", "item6",
           "COMPUTE_USD = RATING_TYPE 'COMPUTE'; AI/storage/transfer/adjustment split; "
           "BILLING_TYPE uniformly CONSUMPTION on this account."),
    Metric("warehouse_usage", "Per-warehouse usage", METERED,
           "warehouse / day", "ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY (WAREHOUSE_ID > 0)",
           ACCOUNT_TZ, "~45 min", "item5",
           "Exact usage, NOT billed: idle included, cloud services unadjusted. "
           "The account rebate lives on the Spend panel."),
    Metric("department_chargeback", "Department chargeback", METERED,
           "department / warehouse / day", "FACT_WAREHOUSE_DAILY + DEPARTMENT_MAP",
           ACCOUNT_TZ, "hourly load (~1h)", "v4.34",
           "Exact warehouse usage per department; a department owns its warehouse idle."),
    Metric("measured_query_cost", "Measured query / CALL cost", MEASURED,
           "query / CALL", "ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY (COMPUTE + QAS)",
           UTC, "~8h", "item4",
           "Idle excluded — 'what did running THIS cost'. Includes Query Acceleration."),
    Metric("pattern_cost", "Repeated-pattern cost", MEASURED,
           "parameterized-hash / day", "MART_PATTERN_COST_DAILY (V047, incl. QAS)",
           UTC, "~8h (loaded)", "V047",
           "Measured $ per repeated statement; cheap-but-constant out-bills expensive-but-rare."),
    Metric("allocated_user_db", "Allocated user / database cost", ALLOCATED,
           "user / database",
           "FACT_COST_ALLOC_XDIM_DAILY (warehouse-hour credit share); live elapsed-share fallback",
           ACCOUNT_TZ, "hourly load / live", "items 2 & 8b",
           "Warehouse-scoped on every path; idle spread across queries. Mart path is "
           "credit-weighted (size-aware); the live fallback is elapsed-share (size-blind)."),
    Metric("per_db_storage", "Per-database storage $", ESTIMATED,
           "database / calendar month",
           "FACT_STORAGE_DAILY (MTD + prior-month daily average) x rate",
           ACCOUNT_TZ, "daily load", "items 3 & 7",
           "Active + fail-safe bytes, binary TiB, x $/TiB SETTING. Calendar-month basis. "
           "Estimate — org rate-card is billing truth."),
    Metric("account_tier_storage", "Account storage by tier $", ESTIMATED,
           "account / tier",
           "FACT_STORAGE_ACCOUNT_DAILY / ACCOUNT_USAGE.STORAGE_USAGE x tier rates",
           UTC, "daily load", "V046",
           "Table/stage/fail-safe/hybrid/archive. STORAGE_USAGE is Snowflake's own estimate "
           "and won't match the invoice; account grain (no per-DB split)."),
    Metric("ai_cortex_spend", "AI / Cortex spend", BILLED,
           "service / day / user",
           "METERING_DAILY_HISTORY (AI services) x AI rate; CORTEX_*_USAGE_HISTORY token credits",
           UTC, "up to 24h / varies", "v4.30",
           "Priced at the configured AI rate, not the compute rate."),
    Metric("cloud_services_ratio", "Cloud-services ratio", METERED,
           "warehouse / window", "ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY (CS / total)",
           ACCOUNT_TZ, "~45 min", "v4.30",
           "Per-warehouse heuristic; the real rebate is the account-level 10%-of-daily-compute rule."),
    Metric("object_query_cost", "Per-object query compute", MEASURED,
           "object / day",
           "FACT_OBJECT_COST_DAILY (QUERY_ATTRIBUTION_HISTORY split across ACCESS_HISTORY base objects)",
           UTC, "~8h / daily load", "V048",
           "Measured compute+QAS split EQUALLY across the base objects each query touched "
           "(additive). QUERY_COMPUTE_RESIDUAL = credits for queries touching no base object. "
           "Full-query 'influenced cost' is a separate non-additive lens."),
    Metric("object_maintenance_cost", "Per-object maintenance cost", MEASURED,
           "object / day / arm",
           "FACT_OBJECT_COST_DAILY (clustering / MV refresh / serverless task / Snowpipe / search-opt)",
           UTC, "daily load", "V048",
           "Direct per-object serverless credits — the classic silent burners."),
    Metric("month_end_forecast", "Month-end forecast", ESTIMATED,
           "account / month", "linear / seasonal / opt-in ML over FACT_METERING_DAILY",
           ACCOUNT_TZ, "as of last loaded day", "v4.x",
           "Projection with a 3-month backtest naming the most reliable engine."),
)


def as_rows() -> list[dict]:
    """Registry as display rows (for the Admin panel / a DataFrame)."""
    return [
        {"Metric": m.label, "Method": m.method, "Grain": m.grain, "Source": m.source,
         "Timezone": m.timezone, "Latency": m.latency, "Formula ver": m.formula_version,
         "Notes": m.notes}
        for m in METRICS
    ]


def by_method() -> dict:
    """Metric labels grouped by method — the 'read this as…' summary."""
    out: dict = {meth: [] for meth in METHODS}
    for m in METRICS:
        out.setdefault(m.method, []).append(m.label)
    return out
