# V027 — the mart family (design)

Status: SHIPPED end to end. Schema + loaders + readers + telemetry rider in V027__mart_family.sql (2026-07-08); WAVE 2 panel adoptions SHIPPED 2026-07-09 (v4.12.0) — ten surfaces go mart-first through app/data/mart27_sql.py aggregate readers via components.run_mart_first, live builders retained as labeled fallbacks, six more pages pinned in tests/test_perf_budgets.py (only go down). Rider PANELS (delivery SLO, fatigue, acceptance, forecast quality) land in the same release. Consolidates Codex rounds 2–3 items #2-4, #6-11,
#13-14, #17, #19 into one migration batch. Build AFTER a few days of v4.9
telemetry (the 2% sampled baseline + slow sink) confirms priority order.

## Why one batch

Every deferred item is the same shape: a recurring live ACCOUNT_USAGE scan
that a scheduled fact/mart would make O(rows-in-mart). Building them piecemeal
means five loader-proc edits and five backfills; one designed batch means one
loader revision (SP_LOAD_FACTS vN), one backfill script extension, one
teardown/roles/validate sync, one Snowsight apply.

## Decision drivers (fill from telemetry before building)

From `APP_QUERY_TELEMETRY` + `app_statement_stats` after ~3 days on v4.9:
p95 and call frequency per query family. Build order = (p95 × frequency)
descending. Current expectation from production so far: warehouse-efficiency
(idle/sizing ~90-97s), query-family (patterns/tag coverage ~30s), role share,
security posture, task-graph, cost allocation, incident timeline, AI usage.

## The marts

| # | Object | Grain | Feeds (replaces live scan in) | Cadence | Est. rows/day |
|---|---|---|---|---|---|
| 1 | `MART_WAREHOUSE_EFFICIENCY_DAILY` | day × warehouse | idle advisor, sizing profile, queue/spill panels (insights_sql) | hourly (last 2d) + daily compact | ~40 |
| 2 | `MART_QUERY_FAMILY_DAILY` | day × QUERY_PARAMETERIZED_HASH (top ~2000/day by exec time) | recurring patterns, compile-heavy, tag coverage, repeat-query, pruning | hourly | ~2k |
| 3 | `FACT_QUERY_ROLE_HOURLY` | hour × role × warehouse × company | role share (chargeback), break-glass activity, governance role views | hourly | ~1-2k |
| 4 | `FACT_QUERY_SCHEMA_HOURLY` | hour × db.schema × company (no user/wh dims — cardinality control) | Operations/Control Room schema-filtered summaries (removes the live fallback trigger) | hourly | ~1-3k |
| 5 | `MART_COST_ALLOCATION_DAILY` | day × dimension(user/db/schema/role) × key | Cost → Attribution allocated views, dept exports | hourly (today only) + daily freeze | ~3-5k |
| 6 | `MART_TASK_GRAPH_DAILY` | day × root pipeline | Operations → Task graphs ($) (keeps live for <24h tail) | hourly | ~100 |
| 7 | `MART_SECURITY_POSTURE_DAILY` | day × metric (mfa gaps, admin stmt counts, unused roles, cred expiry buckets, grant deltas) | Security first paint + export pack | daily 06:30 | ~50 |
| 8 | `MART_INCIDENT_TIMELINE` | event_ts × kind (alert/task-fail/ddl/grant/resize) | Control Room timeline + day replay | hourly (last 48h window) | ~500 |
| 9 | `FACT_AI_USAGE_DAILY` | day × user × source (code/functions) × model | AI chargeback rollups, unit-cost AI panel | daily | ~50 |

Telemetry schema rider (same migration): `APP_QUERY_TELEMETRY` gains
`CACHE_HIT BOOLEAN`, `SQL_HASH VARCHAR(64)`, `BATCH_SIZE NUMBER`,
`TRUNCATED BOOLEAN` (Codex r3 #17); `APP_USAGE` gains `IS_RERUN BOOLEAN`
so rerender timings become loggable without corrupting the first-paint p95
the OPS_SLOW_RENDER sentinel reads (r3 #19).

## Loader design

- Extend the existing hourly loader chain (facts task) with one new proc
  `SP_LOAD_MARTS_V27()` called AFTER the base facts load (marts 1-6, 8 read
  the freshly loaded hour). Security (7) and AI (9) join the daily 06:30 leg.
- Every mart load is MERGE-idempotent on its grain; per-mart EXCEPTION blocks
  (V017 isolation pattern) so one mart's source drift never starves the rest.
- `MART_SOURCE_FRESHNESS` gains one row per new mart (the freshness board and
  stale labels work unchanged).
- Backfill: extend `snowflake/backfill_365.sql` with scoped sections; marts
  2 and 5 backfill 90d only (QUERY_HISTORY retention + cost), the rest 365d
  where sources allow.

## App adoption pattern (unchanged house style)

Fact-first with the existing live builder kept as labeled fallback — exactly
the Control Room v4.8.2 pattern. Each adoption lowers a pinned budget in
`tests/test_perf_budgets.py` (they only go down). p95 budgets per page land
in the Admin → Performance panel once `CACHE_HIT` telemetry exists.

## Bookkeeping checklist (the usual gates enforce most of this)

- Guard -20027 (< 26), version row 27, validate → 27, admin dict + name
  (sync locks fail CI if forgotten).
- teardown.sql: drops for procs/tasks; marts join the preserved-tables list
  ONLY if they hold frozen history (none do — all rebuildable → real drops).
- roles.sql re-run note in DEPLOYMENT (blanket future-table grants cover
  SELECT already).
- Canary entries per new reader builder; sqlglot parse gate covers the DDL.
- Locks: per-mart builder shape tests + loader-isolation source-scrape +
  budget reductions.

## Explicitly out of scope

- Partial-success batching (r2 #3 / r3 #16): decided by batch_fallback
  telemetry, not by this batch.
- Dynamic Tables for these marts: the V015 DT pilot's cost review decides
  whether marts 1-2 convert later; V027 ships tasks (predictable, provable).

## Approved riders (Codex round 5, owner-approved 2026-07-08)

Fold into the V027 build — panels over existing/new data, no extra programs:
- **Delivery SLOs** (r5 #14): panel over ALERT_DELIVERIES + APP_ERROR_LOG —
  send success %, latency (RAISED_AT->SENT_AT), retries, unacked criticals.
- **Alert fatigue** (r5 #15): alerts/user/week, NOISE/EXPECTED/ACTIONED mix,
  repeated dedupe keys — over ALERT_EVENTS/ALERT_AUDIT.
- **Usage events** (r5 #19): APP_USAGE gains EVENT_KIND (page_visit, rerun,
  saved_view_apply, csv_export, remediation_exec) in the telemetry rider;
  the operator-effectiveness view (r5 #20) derives from it.
- **Acceptance metrics** (r5 #4, honest subset): generated -> executed ->
  verified/rejected derived from REMEDIATION_LOG + SAVINGS_LEDGER. No
  impression tracking — Streamlit cannot measure "viewed" truthfully.
- **Forecast quality** (r5 #11): extend the existing backtest panel with
  MAPE/bias per engine.

## Approved sequence after V027

Numbering updated 2026-07-09: V028 shipped as the credential-expiry policy
change (live round 4), so the initiatives below shift one slot. Initiatives
are named, not numbered — the next free migration slot at build time wins.

1. **Incident object (design doc first; migration ~V031 — V029 became the
   2026-07-10 loader hotfix and V030 its correction)** — DESIGN DRAFTED
   2026-07-09: docs/design/V029_INCIDENT_OBJECT.md (includes the Flyway/
   Terraform integration assumptions). Rolls alerts,
   DDL, task failures, warehouse changes, and fixes into one lifecycle
   object. Metrics: incident count, MTTA/MTTR, time-to-detect, reopen
   rate. The design also defines recommendation lineage (explicit
   RECOMMENDATION_ID/EVENT_ID/ACTION_ID links instead of notes-text
   references in SAVINGS_LEDGER — Codex r6 #6).
2. **Owner registry (~V032)**: generalize DEPARTMENT_MAP to OBJECT_OWNERS
   (warehouse/db/schema/task/pipeline/rule). Headline metric: unowned
   spend %. Feeds budget-by-owner (r5 #12), data-product scorecards, and
   owners for the client-driver inventory (r6 #16).
3. Re-assess the program-shaped items (r5 #9 data quality, #10 product
   scorecards, #17 classification) once 1-2 land — they need config
   owners, not just code.

