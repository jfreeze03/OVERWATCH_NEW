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

# --- rec16 executable-contract vocabularies ---------------------------------
# WINDOW: the time basis a metric is computed over (so a reader/CI knows whether
# today's partial is in, and which window the SQL must match).
WINDOWS = (
    "trailing-complete-days",   # [today-N, today) — today's partial EXCLUDED, divide by real days
    "vs-prior-half-window",     # the resolve_effective_window pool: clamp + [today-eff, today)
    "mtd",                      # month-to-date (today's partial included)
    "calendar-month",           # a whole calendar month
    "rolling-daily",            # a daily series, today's partial row included
    "point-in-time",            # a snapshot (no window)
    "projection",               # a forecast to a horizon
    "n/a",
)
UNITS = ("USD", "currency", "credits", "days", "percent", "count", "ratio", "bytes")
PARTIAL_DAY = ("excluded", "included", "n/a")

# rec32: header help for columns whose BASIS is otherwise only in a per-page
# caption. _render_table hangs these on the column header (st.column_config help)
# so hovering answers "which dollar is this?". Keyed by UPPER column name; a
# missing entry is harmless (no help shown). Extend as ambiguous columns recur.
COLUMN_HELP = {
    "CREDITS_BILLED": "BILLED — ties to the invoice (includes the cloud-services adjustment).",
    "CREDITS_MEASURED": "MEASURED — exact attributed compute (QUERY_ATTRIBUTION_HISTORY); idle excluded.",
    "CREDITS_ALLOCATED": "ALLOCATED — spread from a coarser grain by a share; an estimate (idle included).",
    "BILLED_USD": "BILLED USD — ties to the invoice.",
    "MEASURED_USD": "MEASURED USD — exact attributed compute; idle excluded.",
    "ALLOCATED_USD": "ALLOCATED USD — a coarse-grain total spread by a share; an estimate.",
    "ESTIMATED_USD": "ESTIMATED — modeled from bytes/credits x a configured rate, not billed.",
    "SPEND_USD": "USD = credits x the contract rate ($3.68 compute / $2.20 Cortex). Display-only conversion.",
    "USD": "USD = credits x the contract rate ($3.68 compute / $2.20 Cortex). Display-only conversion.",
}


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
    # rec16: executable-contract fields — a panel/CI can key off these instead of
    # re-deriving window/scope/unit provenance ad hoc per surface (the root cause
    # of the rec4/rec5/rec11/rec20 drift this session repeatedly hand-patched).
    window: str = "n/a"                       # one of WINDOWS
    partial_day: str = "n/a"                  # one of PARTIAL_DAY
    unit: str = "USD"                         # one of UNITS
    filters: tuple[str, ...] = ()             # global dimension filters this metric HONORS ('' = account-wide)
    required_sources: tuple[str, ...] = ()    # structured tables/marts it must read (for coverage gating)
    coverage: str = ""                        # coverage / residual rule in one line
    owner: str = "platform"                   # who owns this definition


METRICS: tuple[Metric, ...] = (
    Metric("account_billed_spend", "Account billed spend", BILLED,
           "account / day / service", "ACCOUNT_USAGE.METERING_DAILY_HISTORY (CREDITS_BILLED)",
           UTC, "up to 24h", "v4.30",
           "Billed = used + cloud-services adjustment. The number that ties to the bill.",
           window="rolling-daily", partial_day="included", unit="USD", filters=(),
           required_sources=("ACCOUNT_USAGE.METERING_DAILY_HISTORY", "FACT_METERING_DAILY"),
           coverage="billed = used + cloud-services adjustment; ties to the invoice", owner="platform"),
    Metric("replication_usage", "Replication usage", METERED,
           "database / refresh", "ACCOUNT_USAGE.DATABASE_REPLICATION_USAGE_HISTORY",
           UTC, "up to ~3h", "v4.134",
           "Native credits and transferred bytes for secondary databases; measured usage can lag "
           "or differ from billed metering adjustments.",
           window="rolling-daily", partial_day="included", unit="credits",
           filters=("company", "database"),
           required_sources=("ACCOUNT_USAGE.DATABASE_REPLICATION_USAGE_HISTORY",),
           coverage="native secondary-database grain; no allocation", owner="finops"),
    Metric("compute_pool_usage", "Snowpark Container Services usage", METERED,
           "compute pool / application", "ACCOUNT_USAGE.SNOWPARK_CONTAINER_SERVICES_HISTORY",
           UTC, "up to ~3h", "v4.134",
           "Account-wide compute-pool credits; the source exposes no company key.",
           window="rolling-daily", partial_day="included", unit="credits", filters=(),
           required_sources=("ACCOUNT_USAGE.SNOWPARK_CONTAINER_SERVICES_HISTORY",),
           coverage="native pool/application grain; account-wide", owner="platform"),
    Metric("notebook_container_usage", "Notebook container usage", METERED,
           "notebook / user / compute pool", "ACCOUNT_USAGE.NOTEBOOKS_CONTAINER_RUNTIME_HISTORY",
           UTC, "up to ~3h", "v4.134",
           "Notebook runtime and credits are a subset of SPCS and are never added to its total.",
           window="rolling-daily", partial_day="included", unit="credits", filters=(),
           required_sources=("ACCOUNT_USAGE.NOTEBOOKS_CONTAINER_RUNTIME_HISTORY",),
           coverage="native notebook subset; non-additive to SPCS", owner="platform"),
    Metric("marketplace_paid_usage", "Paid Marketplace charges", BILLED,
           "listing / day / currency", "ORGANIZATION_USAGE.MARKETPLACE_PAID_USAGE_DAILY",
           UTC, "up to ~24h", "v4.134",
           "Organization billing truth for this consumer account; native currency is preserved.",
           window="rolling-daily", partial_day="included", unit="currency", filters=(),
           required_sources=("ORGANIZATION_USAGE.MARKETPLACE_PAID_USAGE_DAILY",),
           coverage="native listing charge; no credit-rate conversion", owner="finops"),
    Metric("org_reconciliation", "Org rate-card dollars", BILLED,
           "account / month / rating-type", "ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY",
           UTC, "up to ~72h; mutates until month close", "item6",
           "COMPUTE_USD = RATING_TYPE 'COMPUTE'; AI/storage/transfer/adjustment split; "
           "BILLING_TYPE uniformly CONSUMPTION on this account.",
           window="calendar-month", partial_day="included", unit="USD", filters=(),
           required_sources=("ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY",),
           coverage="rate-card currency; mutates until month close", owner="finops"),
    Metric("warehouse_usage", "Per-warehouse usage", METERED,
           "warehouse / day", "ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY (WAREHOUSE_ID > 0)",
           ACCOUNT_TZ, "~45 min", "item5",
           "Exact usage, NOT billed: idle included, cloud services unadjusted. "
           "The account rebate lives on the Spend panel.",
           window="vs-prior-half-window", partial_day="excluded", unit="USD", filters=("company",),
           required_sources=("ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY",),
           coverage="exact at warehouse grain; idle in, CS unadjusted", owner="platform"),
    Metric("department_chargeback", "Department chargeback", METERED,
           "department / warehouse / day", "FACT_WAREHOUSE_DAILY + DEPARTMENT_MAP",
           ACCOUNT_TZ, "hourly load (~1h)", "v4.34",
           "Exact warehouse usage per department; a department owns its warehouse idle.",
           window="rolling-daily", partial_day="included", unit="USD", filters=("company",),
           required_sources=("FACT_WAREHOUSE_DAILY", "DEPARTMENT_MAP"),
           coverage="exact warehouse usage per department", owner="finops"),
    Metric("measured_query_cost", "Measured query / CALL cost", MEASURED,
           "query / CALL", "ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY (COMPUTE + QAS)",
           UTC, "~8h", "item4",
           "Idle excluded — 'what did running THIS cost'. Includes Query Acceleration.",
           window="rolling-daily", partial_day="included", unit="USD",
           filters=("company", "warehouse", "user", "database"),
           required_sources=("ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY",),
           coverage="idle excluded; measured compute + QAS", owner="platform"),
    Metric("pattern_cost", "Repeated-pattern cost", MEASURED,
           "parameterized-hash / day", "MART_PATTERN_COST_DAILY (V047, incl. QAS)",
           UTC, "~8h (loaded)", "V047",
           "Measured $ per repeated statement; cheap-but-constant out-bills expensive-but-rare.",
           window="rolling-daily", partial_day="included", unit="USD", filters=("company",),
           required_sources=("MART_PATTERN_COST_DAILY",),
           coverage="measured $ per repeated statement", owner="platform"),
    Metric("allocated_user_db", "Allocated user / database cost", ALLOCATED,
           "user / database",
           "FACT_COST_ALLOC_XDIM_DAILY (warehouse-hour credit share); live elapsed-share fallback",
           ACCOUNT_TZ, "hourly load / live", "items 2 & 8b",
           "Warehouse-scoped on every path; idle spread across queries. Mart path is "
           "credit-weighted (size-aware); the live fallback is elapsed-share (size-blind).",
           window="vs-prior-half-window", partial_day="excluded", unit="USD",
           filters=("company", "database"),
           required_sources=("FACT_COST_ALLOC_XDIM_DAILY",),
           coverage="estimate; named rows cover N%, 'Other' = residual", owner="finops"),
    Metric("per_db_storage", "Per-database storage $", ESTIMATED,
           "database / calendar month",
           "FACT_STORAGE_DAILY (MTD + prior-month daily average) x rate",
           ACCOUNT_TZ, "daily load", "items 3 & 7",
           "Active + fail-safe bytes, binary TiB, x $/TiB SETTING. Calendar-month basis. "
           "Estimate — org rate-card is billing truth.",
           window="calendar-month", partial_day="included", unit="USD",
           filters=("company", "database"), required_sources=("FACT_STORAGE_DAILY",),
           coverage="active + fail-safe bytes x rate; estimate", owner="platform"),
    Metric("account_tier_storage", "Account storage by tier $", ESTIMATED,
           "account / tier",
           "FACT_STORAGE_ACCOUNT_DAILY / ACCOUNT_USAGE.STORAGE_USAGE x tier rates",
           UTC, "daily load", "V046",
           "Table/stage/fail-safe/hybrid/archive. STORAGE_USAGE is Snowflake's own estimate "
           "and won't match the invoice; account grain (no per-DB split).",
           window="rolling-daily", partial_day="included", unit="USD", filters=(),
           required_sources=("FACT_STORAGE_ACCOUNT_DAILY",),
           coverage="Snowflake's own estimate; account grain (no per-DB split)", owner="platform"),
    Metric("ai_cortex_spend", "AI / Cortex spend", BILLED,
           "service / day / user",
           "METERING_DAILY_HISTORY (AI services) x AI rate; CORTEX_*_USAGE_HISTORY token credits",
           UTC, "up to 24h / varies", "v4.30",
           "Priced at the configured AI rate, not the compute rate.",
           window="rolling-daily", partial_day="included", unit="USD", filters=("company", "user"),
           required_sources=("ACCOUNT_USAGE.METERING_DAILY_HISTORY",),
           coverage="priced at the AI rate, not the compute rate", owner="platform"),
    Metric("cloud_services_ratio", "Cloud-services ratio", METERED,
           "warehouse / window", "ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY (CS / total)",
           ACCOUNT_TZ, "~45 min", "v4.30",
           "Per-warehouse heuristic; the real rebate is the account-level 10%-of-daily-compute rule.",
           window="vs-prior-half-window", partial_day="excluded", unit="ratio", filters=("company",),
           required_sources=("ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY",),
           coverage="per-warehouse heuristic; real rebate is account-level", owner="platform"),
    Metric("capacity_pressure_forecast", "Warehouse capacity pressure forecast", ESTIMATED,
           "warehouse / days-to-pressure",
           "FACT_QUERY_HOURLY + FACT_WAREHOUSE_DAILY + WAREHOUSE_CHANGE_REGISTRY",
           ACCOUNT_TZ, "hourly facts; complete days only", "v4.134",
           "Robust trend of 7-day median queue/spill pressure with workload corroboration, "
           "change-point suppression, and a 7-day holdout error gate.",
           window="projection", partial_day="excluded", unit="days", filters=("company",),
           required_sources=("FACT_QUERY_HOURLY", "FACT_WAREHOUSE_DAILY",
                             "WAREHOUSE_CHANGE_REGISTRY"),
           coverage="ETA only with >=30 observed days, >=80% coverage, and stable evidence",
           owner="platform"),
    Metric("object_query_cost", "Per-object query compute", MEASURED,
           "object / day",
           "FACT_OBJECT_COST_DAILY (QUERY_ATTRIBUTION_HISTORY split across ACCESS_HISTORY "
           "base objects read + write targets)",
           UTC, "~8h / daily load", "V048/V050",
           "Measured compute+QAS split EQUALLY across touched objects, arm labeled by "
           "role since V050: QUERY_COMPUTE_WRITE = production share (write targets, "
           "V049), QUERY_COMPUTE_READ = consumption share. Additive; write wins on a "
           "read+write collapse. QUERY_COMPUTE_RESIDUAL = credits for queries that "
           "neither read nor wrote a base object. Full-query 'influenced cost' is a "
           "separate non-additive lens.",
           window="rolling-daily", partial_day="included", unit="USD",
           filters=("company", "database"), required_sources=("FACT_OBJECT_COST_DAILY",),
           coverage="split equally across touched objects; RESIDUAL = no base object", owner="platform"),
    Metric("object_maintenance_cost", "Per-object maintenance cost", MEASURED,
           "object / day / arm",
           "FACT_OBJECT_COST_DAILY (clustering / MV refresh / serverless task / Snowpipe / search-opt)",
           UTC, "daily load", "V048",
           "Direct per-object serverless credits — the classic silent burners.",
           window="rolling-daily", partial_day="included", unit="USD",
           filters=("company", "database"), required_sources=("FACT_OBJECT_COST_DAILY",),
           coverage="direct per-object serverless credits", owner="platform"),
    Metric("etl_unit_cost", "ETL unit cost (per pipeline)", MEASURED,
           "pipeline / run",
           "QUERY_HISTORY (JSON QUERY_TAG) + QUERY_ATTRIBUTION_HISTORY credits",
           UTC, "~8h", "Phase 3",
           "$/run, $/M rows, $/TiB, failed-run waste for pipelines that set the structured "
           "QUERY_TAG (docs/design/ETL_COST_TAGS.md). Coverage % is credit-weighted.",
           window="rolling-daily", partial_day="included", unit="USD", filters=("company",),
           required_sources=("ACCOUNT_USAGE.QUERY_HISTORY", "ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY"),
           coverage="credit-weighted coverage %", owner="dataeng"),
    Metric("contract_runway", "Contract runway (days to exhaust)", ESTIMATED,
           "account", "FACT_METERING_DAILY (trailing-30-complete-day burn) vs CONTRACT_CREDITS",
           ACCOUNT_TZ, "up to 24h", "rec20",
           "days-left = (contracted - consumed) / trailing-30-COMPLETE-day burn. The ONE "
           "canonical runway feeding the Contract KPI, the Brief, and COST_CONTRACT_BREACH — "
           "burn averages complete days only (today's partial EXCLUDED), never a literal /30.",
           window="trailing-complete-days", partial_day="excluded", unit="days", filters=(),
           required_sources=("FACT_METERING_DAILY",),
           coverage="days = (contract - consumed) / trailing-30-complete-day burn", owner="finops"),
    Metric("month_end_forecast", "Month-end forecast", ESTIMATED,
           "account / month", "linear / seasonal / opt-in ML over FACT_METERING_DAILY",
           ACCOUNT_TZ, "as of last loaded day", "v4.4",
           "Projection with a 3-month backtest naming the most reliable engine.",
           window="projection", partial_day="excluded", unit="USD", filters=(),
           required_sources=("FACT_METERING_DAILY",),
           coverage="projection with a 3-month backtest", owner="platform"),
)


def get(key: str) -> Metric | None:
    """Look a metric contract up by key (None if absent) — so a panel/test can key
    off the registry instead of re-deriving window/scope/unit ad hoc."""
    for m in METRICS:
        if m.key == key:
            return m
    return None


def validate() -> list[str]:
    """rec16: assert the registry is an internally-consistent EXECUTABLE contract.
    Returns a list of problems (empty = clean); the CI test fails on any. This is
    the drift-guard's first half — every metric must declare a full, valid contract."""
    problems: list[str] = []
    seen: set[str] = set()
    for m in METRICS:
        if m.key in seen:
            problems.append(f"{m.key}: duplicate key")
        seen.add(m.key)
        if m.method not in METHODS:
            problems.append(f"{m.key}: method {m.method!r} not in METHODS")
        if m.window not in WINDOWS:
            problems.append(f"{m.key}: window {m.window!r} not in WINDOWS")
        if m.partial_day not in PARTIAL_DAY:
            problems.append(f"{m.key}: partial_day {m.partial_day!r} not in PARTIAL_DAY")
        if m.unit not in UNITS:
            problems.append(f"{m.key}: unit {m.unit!r} not in UNITS")
        if not m.required_sources:
            problems.append(f"{m.key}: no required_sources declared")
        if not m.coverage:
            problems.append(f"{m.key}: no coverage rule declared")
        # a windowed dollar/credit/day metric must state whether today's partial is in
        if m.window in ("trailing-complete-days", "vs-prior-half-window") and m.partial_day == "n/a":
            problems.append(f"{m.key}: windowed metric must declare partial_day")
        # window and partial_day must not CONTRADICT: the complete-day / vs-prior
        # windows exclude today by definition; rolling-daily includes it. (Caught
        # department_chargeback declaring rolling-daily + excluded, r4.)
        _excludes = {"trailing-complete-days", "vs-prior-half-window"}
        if m.window in _excludes and m.partial_day == "included":
            problems.append(f"{m.key}: window {m.window!r} excludes today but partial_day='included'")
        if m.window == "rolling-daily" and m.partial_day == "excluded":
            problems.append(f"{m.key}: rolling-daily includes today but partial_day='excluded'")
    return problems


def as_rows() -> list[dict]:
    """Registry as display rows (for the Admin panel / a DataFrame)."""
    return [
        {"Metric": m.label, "Method": m.method, "Grain": m.grain, "Window": m.window,
         "Partial day": m.partial_day, "Unit": m.unit, "Source": m.source,
         "Timezone": m.timezone, "Latency": m.latency, "Formula ver": m.formula_version,
         "Owner": m.owner, "Notes": m.notes}
        for m in METRICS
    ]


def by_method() -> dict:
    """Metric labels grouped by method — the 'read this as…' summary."""
    out: dict = {meth: [] for meth in METHODS}
    for m in METRICS:
        out.setdefault(m.method, []).append(m.label)
    return out
