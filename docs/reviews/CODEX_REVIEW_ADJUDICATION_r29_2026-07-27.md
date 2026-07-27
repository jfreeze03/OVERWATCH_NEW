# Codex adjudication — r29 — 2026-07-27 (post-v4.55.0)

Second Codex analysis (20 fresh recommendations), headline: "the Executive
Board advertises 180/365-day windows but still reads only 90 days." Each
line-cited claim verified against the actual code before verdict. Verdicts:
**SHIP** (real; fixed in V054/now), **ROUTE** (real; a later measured round),
**DECLINE** (already handled or not agent-actionable, with the evidence).

Standing owner decisions applied: migrations run by Joe in Snowsight; agent
Snowflake access read-only; SNOW_ACCOUNTADMINS + SNOW_SYSADMINS only; the r28+
performance queue stands.

## Headline

**#1 is a real, confirmed regression that shipped in V052 — and it was mine.**
`SP_REFRESH_EXEC_BOARD`'s three source CTEs cap at 90 days
([V052:64/75/81](../../snowflake/migrations/V052__exec_board_windows_180_365.sql)),
and the window join reads those already-capped frames, so the 180/365 pills held
90-day values. Fixed in **V054** (source horizons → 365) with the **#20**
companion (retention floor 180→365) and a standing CI invariant. **#7, the other
"correctness" flag, is already fixed** (V042 r22 #7 — the reviewer cited the
superseded V041 body). The remaining 18 are performance/UX items, most already
in the r28+ queue.

## Verdicts

| # | Recommendation | Verdict | Evidence / reasoning |
|---|----------------|---------|----------------------|
| **1** | Fix V052's long windows — 180/365 read 90-day-capped sources | **SHIP — done (V054)** | CONFIRMED: `wh_daily`/`qh_daily`/`tk_daily` filter `DAY >= DATEADD('day',-90,CURRENT_DATE())` (V052:64/75/81); window join uses `-w.WINDOW_DAYS` (V052:88/95/101) over the capped frame. V054 widens the three to -365. Codex's "run 7-90 hourly, 180/365 daily" split DECLINED as unneeded complexity — the sources are daily-grain aggregates (tiny), so every-refresh recompute is cheap. |
| **20** | CI invariant: advertised windows have source history; V052 test misses the truncation | **SHIP — done (V054)** | TRUE: `test_v052_windows.py` asserted window *names* == config but never the source horizon. V054 adds a standing invariant — every effective board source horizon AND the `SP_PURGE_FACTS` daily floor must be ≥ `max(DAY_WINDOW_OPTIONS)`. Companion retention floor raised 180→365 (V045:1065) so the invariant holds end-to-end. |
| **7** | Advance QH watermark only after success | **DECLINE — already fixed** | `SP_LOAD_QH_EXTRACT` was re-derived in V042 (r22 #7): extract arm is `BEGIN TRANSACTION … COMMIT; ok := TRUE` with `ROLLBACK` on failure, and the watermark MERGE is gated `IF (ok) THEN` (V042:90/118-146/229). Codex cited V041:227 — the superseded body. |
| 3 | Eliminate remaining raw `QUERY_HISTORY` scans in marts | **ROUTE (r28)** | TRUE: 3 `ACCOUNT_USAGE.QUERY_HISTORY` refs in V045 — line 210 is the legit extract source; **654 and 1827** are mart branches that could feed from `OW_QH_EXTRACT`. Verify the extract carries every column each branch needs, then reroute. |
| 13 | Bound batch concurrency (7-10 async on XS warehouse) | **ROUTE (r28)** | TRUE: `_execute_batch` submits every SQL `block=False` with no cap ([query.py:378](../../app/core/query.py:378)). Cap 2-4, prioritize above-fold. |
| 12 | Per-query batch caching, not tuple-level | **ROUTE (r28)** | TRUE: batch cached as a whole tuple (`_fetch_*_batch(sqls: tuple, …)`), so one changed member cold-starts all. Real, but interacts with #13 — do together. |
| 8 | Use `SOURCE_FRESHNESS_STATE.GENERATION` for cache keys | **ROUTE (r28)** | TRUE and not a bug: `_cache_scope` keys on role/user/salt, not generation ([query.py:293](../../app/core/query.py:293)). Adding the fingerprint lets TTLs lengthen safely. |
| 14 | Enforce timeout tiers via Snowpark `statement_params` (SiS skips `ALTER SESSION`) | **ROUTE (r28) — verify first** | Plausible correctness gap: if SiS silently ignores session-level `ALTER SESSION`, the 30/120/180s tiers fall back to the 300s warehouse default. Confirm what `apply_statement_timeout` actually does live before acting. |
| 2 | Stop the nightly reconcile double-load | **ROUTE (measure)** | TRUE it deletes 3 days × ~10 tables and reloads ([V045:1152](../../snowflake/migrations/V045__task_monitoring_restored.sql:1152)) — but it is deliberate late-arrival (ACCOUNT_USAGE lag) reconciliation. "Targeted repair" is an optimization, not a bug; measure the overlap before rearchitecting. |
| 4 | Read `WAREHOUSE_METERING_HISTORY` once per cycle | **ROUTE (r28 loader batch)** | Plausible; fold into the loader-efficiency round. |
| 19 | Drop full-table MAX/COUNT freshness bookkeeping | **ROUTE (r28 loader batch)** | Plausible; track load ts/rowcount during the write, reserve exact counts for periodic audit. |
| 18 | Measure the loader critical path (per-node timing) | **ROUTE (r28 observability)** | Useful; capture schedule delay/queue/exec/rows/retries/credits per node. |
| 6 | Remove the 06:40 workload collision | **ROUTE (r28 scheduling)** | Plausible; stagger heavy branches by p95. |
| 9 | Mart-failure circuit breaker (fallback stampede) | **ROUTE (app perf)** | Plausible: `run_mart_first` launches live fallback per viewer on outage. Stale-last-good + explicit recovery. |
| 10 | Submit expensive text filters via forms | **ROUTE (app UX)** | Plausible; forms with Apply so partial keystrokes don't each fire a query. |
| 11 | Make measured Unit Costs progressive | **ROUTE (app perf)** | Plausible; mart summaries first, 7-day default, granular on demand. |
| 15 | Shorten Cost page first paint | **ROUTE (app perf)** | Plausible; parallelize headline, defer storage/unmapped. |
| 16 | Give Overview an explicit critical path | **ROUTE (app perf)** | Plausible; board/spend/alerts first, defer the rest. |
| 17 | Right-size cache memory (byte-aware eviction) | **ROUTE (app perf)** | Plausible; byte-aware eviction + per-query row caps + fingerprint export keys. |
| 5 | Separate UI and ETL warehouses | **DECLINE — not agent-actionable** | An infra + ongoing-cost decision (a second warehouse). Joe's call in Snowsight, not a code change. |

## Net

Genuinely new + urgent: **#1 (+#20)** — shipped in V054. **#7** was a false
positive (stale line reference). The other 17 are the standing r28+ performance
queue restated with fresh line cites — real, incremental, and best done as one
measured performance round (start with #13/#12/#8/#3, which are the
highest-leverage and already scoped) rather than folded into this correctness
migration.
