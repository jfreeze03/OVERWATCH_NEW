# Compare mode — design (v1, 2026-07-11)

Owner scoping (Joe, 2026-07-11): the company-vs-company axis is DEAD —
ALFA and Trexis are different companies; a side-by-side proves nothing.
Two axes survive, both answering questions operators currently answer in
spreadsheets:

1. **Period vs period** — this month vs last (or trailing 7d vs prior 7d):
   "spend is up 12% — WHICH warehouses/patterns/users did it?"
2. **Environment vs environment within one company** — ALFA PRD vs SIT vs
   DEV: "is DEV outspending PRD?", "did last week's SIT promotion change
   PRD's cost shape?"

## Where it lives

A **Compare tab on Cost & Contract** (`cost_parts/compare.py`) — not a new
nav page (nav curation is pending on 30d usage data). Honors the triage
filters: company always; database/schema where the source has the grain.

## Axis 1 — period vs period

Pairing picker: `last full month vs prior` (default — matches the boss
chart), `trailing 7d vs prior 7d`, `trailing 30d vs prior 30d`. The
current partial month is NEVER a compare side by default (the house
partial-honesty rule); an "include partial (dimmed)" toggle is the escape
hatch, labeled.

Panels, all from EXISTING marts (no new scans):

| Panel | Source | Δ shown |
|---|---|---|
| Paired KPI strip | FACT_WAREHOUSE_DAILY (company spend $) + FACT_QUERY_HOURLY (queries/fail%/queued) + FACT_METERING_DAILY (account-billed $, labeled account-wide) | value + delta chip per side |
| Warehouse movers | FACT_WAREHOUSE_DAILY | top ± Δ$ by warehouse (reuses the CR spend-movers pattern) |
| Pattern movers | MART_PATTERN_COST_DAILY v2 (V037) | top ± Δ$ by parameterized hash — MEASURED, the silent-spend delta |
| Volume shape | FACT_QUERY_HOURLY | queries/fails/queued/spill by side |

Grain corrections (Codex r11 #12, adopted before build): metering-daily has
neither company grain nor failure/queue columns — company spend comes from
the warehouse fact, operational metrics from the query fact, and the
account-billed total stays its own labeled line. SHIPPED as Phase 1
(v4.28.0): tab on Cost & Contract, pairing picker (last-full-month default,
partial month only via the equal-length labeled toggle), paired KPI strip,
warehouse + pattern movers, volume shape, `charts.paired_bars`.

kpi_row already supports value+delta+badge; charts add one
`paired_bars()` helper (two-side grouped bars, side B hatched/dimmed).

## Axis 2 — environment vs environment (Phase 2 shape: the promotion lane)

Adopted framing (Codex r11 #17): render environments as the ORDERED
promotion lane — DEV -> UAT (PHX/SAN/SEA/SIT) -> PREPROD (MGM) -> PRD —
not generic paired bars. Each stage shows cost / p95 / fail-rate for the
period with stage-over-stage deltas; regressions flag where the channel
degrades. The MGM=PREPROD reconcile (companies.py `_PROD_DB_EXACT` still
counts MGM as PROD for the binary picker; the V023-seeded volume-scan
scope pins MGM too) lands HERE, with the lane replacing the binary
PROD/NONPROD classifier — changing companies.py alone would drift code
from seed (r11 #1 deferral, 2026-07-11).

## Axis 2 — original notes

`ENV` derives from DATABASE_NAME suffix: `_PRD` / `_SIT` / `_DEV` →
PRD/SIT/DEV, else OTHER. One pure helper in companies.py + the same CASE
inline in SQL (no UDF — keep it visible in the reader text).

Sources with database grain today: MART_QUERY_FAMILY_DAILY (runs,
exec-sec, compile — per env), FACT_QUERY_SCHEMA_HOURLY (volume per env).
**Measured $ per env needs a decision:**

> **Open question 1 (V037):** add DATABASE_NAME to MART_PATTERN_COST_DAILY
> now, while the mart is days old (grain: day x hash x company x database).
> Cheap today, a backfill headache after months of accrual. If declined,
> env-$ ships as exec-sec ALLOCATED (labeled, never mixed with measured).

## Build plan

- **Phase 1 (one release):** compare tab + pairing picker + paired KPI
  strip + warehouse/pattern movers. V037 only if open question 1 = yes.
- **Phase 2:** env lens (ENV helper + env split of the same panels).
- Locks: pairing math (period boundary edges), partial-month exclusion,
  triage-filter law, movers parity with the CR pattern.

## Owner answers (2026-07-11) — all three resolved

1. **V037: YES** — DATABASE_NAME joins MART_PATTERN_COST_DAILY grain now.
2. **Default pairing: last full month vs prior.**
3. **The ALFA promotion channel (authoritative, from the owner):**
   `ALFA_EDW_DEV` (DEV) -> four UAT environments: `ALFA_EDW_PHX`,
   `ALFA_EDW_SAN`, `ALFA_EDW_SEA`, `ALFA_EDW_SIT` (all UAT) ->
   `ALFA_EDW_MGM` (PREPROD) -> `ALFA_EDW_PRD` (PRD).

   ENV mapping therefore: `_DEV`->DEV; `_PHX/_SAN/_SEA/_SIT`->UAT;
   `_MGM`->PREPROD; `_PRD`->PRD; else OTHER. Trexis reuses the subset it
   has (`_DEV`->DEV, `_SIT`->UAT, `_PRD`->PRD). The env lens shows the
   channel in promotion order, not alphabetical — DEV, UAT, PREPROD, PRD.

## Open questions for the owner

1. V037 database column on the pattern mart now? (recommended: yes)
2. Default pairing: last-full-month vs prior (recommended), or trailing-30?
3. Env suffix set: is `_PRD/_SIT/_DEV` complete, or do `_QA/_UAT` exist
   anywhere worth recognizing?
