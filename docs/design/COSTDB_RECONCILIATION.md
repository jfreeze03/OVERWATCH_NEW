# COST_DB reconciliation — the Snowflake-employee dashboard vs OVERWATCH vs the docs

2026-07-11. Subject: github.com/jfreeze03/COST_DB (`streamlit_app.py`, 9,220
lines, "CODE VERSION 2025-03-04-A") — the cost dashboard a Snowflake employee
provided, which seeded the old OVERWATCH's cost metrics and structure.

Coverage: 100% of the calculation surface was read — every `get_base_query`
(11 analyzers), the account-level AI query, the executive service-breakdown
query, the yearly-projection math, and the pricing/formatting conventions.
The remaining ~5K lines are chart/render code following one template that
was read in full four times; it computes nothing the queries don't.

## The one-sentence verdict

COST_DB is a competent *usage meter* — it answers "how many credits did each
service consume" from the documented views, and its per-service coverage is
broader than ours. It is not a *billing* or *action* tool: it never applies
the cloud-services adjustment, prices everything at a $2.00 default, projects
by straight run-rate, and stops at "investigate this." OVERWATCH's formula
discipline (billed vs used, measured vs allocated, verified vs estimated) is
ahead of it everywhere they overlap — but COST_DB covers services we ignore,
uses one docs-sanctioned filter we MISSED, and its storage model is closer to
how Snowflake actually bills storage.

## Three-way reconciliation

### 1. Warehouse compute
- **COST_DB**: `WAREHOUSE_METERING_HISTORY`, 12mo; `TOTAL = CREDITS_USED_COMPUTE
  + CREDITS_USED_CLOUD_SERVICES`; filters `WAREHOUSE_ID > 0` ("skip pseudo-VWs
  such as CLOUD_SERVICES_ONLY"); MoM, daily avg/peak, weekly stacked $,
  hour-of-day / day-of-week patterns.
- **OVERWATCH**: FACT_WAREHOUSE_DAILY `CREDITS_TOTAL = SUM(CREDITS_USED)` —
  the same compute+CS basis — with company grain, idle/efficiency/sizing on
  top. **No pseudo-warehouse filter (see R1).**
- **Docs**: `CREDITS_USED = CREDITS_USED_COMPUTE + CREDITS_USED_CLOUD_SERVICES`;
  this is USAGE, not billing — the CS component is largely rebated at the
  account level (only CS above 10% of daily compute is billed, UTC daily).
  Verdict: both apps use the docs-sanctioned per-warehouse lens (Snowsight
  does too). OVERWATCH additionally holds the BILLED truth at account grain
  (FACT_METERING_DAILY `CREDITS_BILLED = CREDITS_USED +
  CREDITS_ADJUSTMENT_CLOUD_SERVICES`, the documented formula) — COST_DB has
  no billed number anywhere. Our per-warehouse dollars (chargeback, movers,
  boss chart) include unadjusted CS — a defensible policy, said nowhere (R2).

### 2. Cloud services
- **COST_DB**: two dedicated lenses — per-query CS credits from
  `QUERY_HISTORY.CREDITS_USED_CLOUD_SERVICES` (by warehouse/user/QUERY_TYPE),
  and a client-application lens (SESSIONS→friendly names). Flags warehouses
  with CS > 10% of warehouse total.
- **OVERWATCH**: CS ratio by warehouse (FACT_WAREHOUSE_DAILY share) + the
  ELEVATED alert + compile-heavy-family drill. Nothing at QUERY_TYPE grain.
- **Docs**: the 10% rule is account-level daily CS vs compute; per-warehouse
  10% is a heuristic in both apps — fine as a heuristic.
  Verdict: parity on the ratio; their QUERY_TYPE cut (SHOW/DESCRIBE/metadata
  storms) is a better *driver* diagnostic than our compile-only drill (R6).
  Their client-app "consumption" lens counts ONLY cloud-services credits —
  it omits all warehouse compute and would misrank Tableau vs dbt by orders
  of magnitude. Do not copy it; build the honest version (R4).

### 3. Storage
- **COST_DB**: DB+failsafe (DATABASE_STORAGE_USAGE_HISTORY) + stage
  (STAGE_STORAGE_USAGE_HISTORY) + hybrid/archive-cool/archive-cold
  (STORAGE_USAGE), monthly AVERAGE of daily bytes, tiered hardcoded rates
  ($23/TB standard, $0.34/GB hybrid, $4/TB cool, $1/TB cold — AWS US East).
- **OVERWATCH**: FACT_STORAGE_DAILY = DB + failsafe bytes per database ×
  one SETTINGS rate; latest-day snapshot dollars, labeled estimate. Stage,
  hybrid, and archive storage are INVISIBLE to us.
- **Docs**: storage bills on the monthly average of daily bytes — their
  average-based month matches billing mechanics better than our snapshot;
  $23/TB is the *capacity* rate (on-demand is ~$40/TB), so their hardcode
  silently assumes capacity pricing — ours being a SETTING is right, their
  averaging is right (R3). Their stacked storage chart with an invisible
  cost-hover trace is a nice pattern.

### 4. Service-family coverage (the meter)
- **COST_DB** has dedicated analyzers we lack: SPCS compute pools, Openflow
  (`SERVICE_TYPE = 'OPENFLOW_COMPUTE_SNOWFLAKE'`), replication groups,
  automatic clustering **per table**, serverless tasks per task (we have
  this), and 10+ AI views (Functions, AI-Functions, Analyst, Search-daily,
  DocAI, Fine-tuning, REST API, Agent, Intelligence, Code CLI+Snowsight).
- **OVERWATCH**: everything flows through METERING_DAILY_HISTORY categories
  on Spend — but `_categorize` buckets OPENFLOW_COMPUTE_SNOWFLAKE and
  HYBRID_TABLE_REQUESTS into "Other" (R5); per-table clustering detail and
  the exotic AI views have no drill (R7, R8 — mostly GAP-class on this
  account today).
- Verdict: our top-line is complete (METERING catches every service); their
  DRILLS are broader. Adopt the drills that have data.

### 5. Projection & pacing
- **COST_DB**: yearly projection = YTD (METERING_HISTORY) + trailing-N-day
  mean × days remaining (N ∈ 7/14/30/60/90), full-year framing.
- **OVERWATCH**: month-end projection (linear/seasonal/opt-in ML) with a
  3-month backtest that names the most reliable engine, plus contract-term
  pacing and the renewal planner — strictly stronger machinery. What we lack
  is their *calendar-year* framing an exec asks for in December (R9).
- Their comparisons exclude the last 24h on both sides (latency guard); we
  achieve the same with 24h-offset windows + partial-day dimming. Parity.

### 6. Pricing & formatting
- **COST_DB**: global sidebar credit price, DEFAULT $2.00; separate AI token
  pricing in the AI tables; every number renders "credits ($dollars)".
- **OVERWATCH**: $3.68/$2.20 from SETTINGS with provenance, org rate-card
  reconciliation panel (billing truth vs model). Ours is ahead; their
  paired "credits ($)" formatting is worth stealing for credit-first tables
  (R10).

## Recommendations, ranked

- **R1 (fix-first, V039): filter the pseudo-warehouse.** Our warehouse fact
  loader ingests every WMH row; accounts emit a `CLOUD_SERVICES_ONLY` row
  (compute=0, CS>0) that lands as a phantom warehouse — it would classify
  ALFA, appear in movers/chargeback-unmapped/Compare, and the idle advisor
  would flag it as 100% idle burn nobody can suspend. COST_DB filters
  `WAREHOUSE_ID > 0`; adopt the name-based equivalent in SP_LOAD_FACTS +
  the live warehouse builders, delete phantom rows in-migration.
- **R2 (copy-only): say the CS caveat where we bill per-warehouse dollars.**
  Chargeback/movers/boss-chart help gains one clause: "includes each
  warehouse's cloud-services credits, unadjusted — the account-level rebate
  lives on Cost → Spend."
- **R3 (V040): storage truth pass.** Extend FACT_STORAGE_DAILY (or a small
  mart) with stage/hybrid/archive arms from STAGE_STORAGE_USAGE_HISTORY +
  STORAGE_USAGE; month-$ = monthly average of daily bytes × rate (billing
  mechanics), keep snapshot for the movers; optional tier rates as SETTINGS
  (default hybrid/archive rates seeded, labeled).
- **R4 (design+build): cost by client application — the honest version.**
  Their best idea, wrong math. SESSIONS client-app mapping (we already parse
  drivers) × our elapsed-share ALLOCATED compute per session's queries =
  "what does Tableau/dbt/PowerBI cost us monthly," labeled allocated. New
  mart arm or live toggle under Chargeback.
- **R5 (one-line): _categorize gains OPENFLOW_COMPUTE_SNOWFLAKE (Serverless)
  and HYBRID_TABLE_REQUESTS (Storage)** so Spend's "Other" stays honest.
- **R6 (small): CS drill gains QUERY_TYPE.** When the CS ratio is ELEVATED,
  break the warehouse's CS credits by QUERY_TYPE (QUERY_HISTORY) beside the
  compile-heavy families — metadata storms become visible.
- **R7 (small panel): per-table clustering spend** (AUTOMATIC_CLUSTERING_
  HISTORY by table, 30d) under Optimization — classic silent burner, feeds
  the savings ledger's future scope.
- **R8 (hold): exotic AI views** (Analyst/Search/DocAI/Fine-tuning/REST/
  Agent) — GAP-class on this account today; extend FACT_AI_USAGE_DAILY
  loader with probe-guarded arms when usage appears.
- **R9 (KPI): projected calendar-year total** on Contract & Forecast from
  the existing engines (exec ask their app answers and ours doesn't).
- **R10 (cosmetic): "credits ($)" paired formatting** for credit-first
  tables, and their storage chart's invisible cost-hover-trace trick.

## What we deliberately do NOT adopt

Their client-app lens as-is (CS-only credits misranked as consumption);
$2.00 hardcoded pricing; unadjusted totals presented as spend; run-rate-only
projection; per-query CS as a "cost" table (CS per query is noise below the
10% rebate line — the ratio + drivers are the actionable form).

## Sources

- WAREHOUSE_METERING_HISTORY (CREDITS_USED = compute + cloud services):
  docs.snowflake.com/en/sql-reference/account-usage/warehouse_metering_history
- Cloud-services adjustment (10% of daily compute, UTC; CREDITS_BILLED =
  CREDITS_USED + CREDITS_ADJUSTMENT_CLOUD_SERVICES):
  docs.snowflake.com/en/user-guide/cost-understanding-compute and
  community.snowflake.com "Cloud Services Billing: Understanding and
  Adjusting Usage"
- Storage billed on monthly average of daily bytes; capacity vs on-demand
  rates: docs.snowflake.com/en/user-guide/cost-understanding-overall
