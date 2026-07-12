# V041 — the loader-efficiency pass (design freeze, 2026-07-12)

ONE migration: `V041__loader_efficiency.sql`. Build in a FRESH session; this
document is the contract. Read order before writing a line: V002 (hourly
loader + task roots), V003 (exec board), V027 (mart family + SP_LOAD_MARTS_V27),
V028→V030 (derivation chain), V031 (_OW_ALLOC_BASE, tag coverage, tuning),
V036/V037 (pattern mart), V039 (pseudo-warehouse), V040 (freshness state).
Then this doc top to bottom. PERF_BACKLOG.md Tier A maps each rider to its
measured evidence.

## Riders (eleven)

R1  STAGED EXTRACT. `OW_QH_EXTRACT` (transient): one QUERY_HISTORY scan per
    hourly cycle over [watermark-overlap, now), projected to the union of
    consumer columns. Consumers rewired to read the stage: FACT_QUERY_HOURLY,
    _OW_ALLOC_BASE, tag coverage, query-family, schema-hourly, role-hourly,
    incident timeline arm, R7 diagnostics. Evidence: 7-8 overlapping scans/cycle.
R2  CROSS-DIM ALLOC FACT. `FACT_COST_ALLOC_XDIM_DAILY`
    (DAY, WAREHOUSE_NAME, DATABASE_NAME, USER_NAME, EXEC_SEC, ALLOC_CREDITS)
    persisted from V031's _OW_ALLOC_BASE before it collapses to single-dim.
    NO schema grain (cardinality; schema stays live-filtered). Readers:
    alloc_attribution db/user-filtered views go mart-first (today = two live
    scans per filter value); user-within-database panel unlocked on Spend.
R3  AI USERS FROM FACT. cortex_code_user_rollup contract served from
    FACT_AI_USAGE_DAILY (has DAY/USER_NAME/SOURCE/MODEL_NAME) with the live
    view as fallback + probe semantics kept (002139 class). Evidence:
    cortex_users p50 17.6s x12.
R4  EXEC BOARD v2. Windows (7,14,30,60,90) — config offers all five but V003
    builds only 7/30, so 14/60/90 ALWAYS hit the 13-month live fallback.
    Single-pass aggregation per source (aggregate once, unpivot). Atomic
    visibility: build into OW_EXEC_BOARD_STAGE, then ALTER TABLE ... SWAP
    (DELETE+INSERT gap currently strands Overview on the live scan).
    PRESSURE_QUEUE / PRESSURE_SPILL / DB_MIX: zero app references
    (verified 2026-07-12, whole-tree grep = 0) — stop producing them.
R5  WATERMARKS. `OW_LOAD_WATERMARKS` (SOURCE, WM_TS). Loaders read since
    watermark minus overlap (45 min hourly / 1 day daily); nightly task
    delete-and-rebuilds the trailing 3 days so restated ACCOUNT_USAGE rows
    and disappeared groups cannot survive stale MERGE rows.
R6  LOADER-OWNED FRESHNESS. Every SP updates SOURCE_FRESHNESS_STATE
    (row count, generation, status) in its own commit. TASK_SNAPSHOT_FRESHNESS
    retired (144 wakes/day); SP_SNAPSHOT_FRESHNESS kept for manual refresh.
    Freshness gains a GENERATION column -> future cache invalidation token.
R7  OPS DIAGNOSTICS MART. `MART_OPS_DIAG_HOURLY` from R1's extract: top-N by
    elapsed + failure families per hour. Operations' first-paint scans go
    mart-first; exact per-query detail stays on-demand. Evidence: Ops pain
    2361, batch:pressure 30-37s.
R8  PLATFORM SCORE SNAPSHOT. `FACT_PLATFORM_SCORE_DAILY` stores the four
    input aggregates daily; weights stay in Python (configurable without
    reload). Overview sparkline reads the fact.
R9  UNUSED-ROLE POSTURE from FACT_QUERY_ROLE_HOURLY (coverage-gated: only
    when the fact's contiguous window >= the policy window) — drops the 90d
    QUERY_HISTORY anti-join from the daily posture loader.
R10 PSEUDO-WAREHOUSE SOURCE FILTER. The V039 promise: WAREHOUSE_ID > 0 moves
    into the re-derived V27-family loader source reads; the eff-mart READER
    name-filters drop next re-derivation after this one.
R11 MONITOR COUNTS in posture snapshot (r14 #18 second half): resource
    monitor totals land in the daily posture row; Security stops paying a
    SHOW + parse on render.

## Task graph (r15 #6 phasing)

TASK_LOAD_HOURLY (root, unchanged schedule)
  -> TASK_QH_EXTRACT (R1, first child)
     -> TASK_LOAD_MARTS_V27_HOURLY (re-derived; consumes extract)
        -> cheap fan-out unchanged (exec board, alert scan, autodeclare...)
Daily root unchanged; + TASK_NIGHTLY_RECONCILE (R5) after TASK_LOAD_DAILY.
EVERY task this migration touches gets an explicit RESUME, and the file ends
with SYSTEM$TASK_DEPENDENTS_ENABLE on BOTH roots + the standalone resumes —
the 07-12 alert outage class must be impossible to reintroduce here.

## Derivation law obligations

SP_LOAD_MARTS_V27 re-derived VERBATIM from the V030 pair + ENUMERATED edits
(R1 source swap, R2 persist step, R10 predicate, R6 freshness writes).
Equality/revert locks updated WITH semantics in: test_live_round4 (V027/V028
pair), V030 shape locks, test_v031 trio, test_v039 (loader = V002 + exactly
one predicate — supersede deliberately with a new enumerated-edits lock).
House laws: declared-exception guards only (never "RAISE EXCEPTION ("), v>=40
guard at top, teardown adds every new object (tasks suspended-then-dropped),
validate.sql floor bumps to V041, admin _EXPECTED_MIGRATIONS 41, canaries for
every new reader, _LIVE_SCAN_BUDGETS only go DOWN, EXPECTED_GAPS untouched.

## App-side swaps (same commit)

spend.py filtered attribution -> mart-first on R2 (live fallback stays);
ai_chargeback users tab -> R3; operations first paint -> R7; overview spark
-> R8; overview board reader unchanged (R4 is loader-side); posture panels
-> R9/R11. Every swap: the r15 lesson — grep the whole function for old
source columns; locks assert the old time column ABSENT.

## Test plan

Numeric recon (new class, r18 #5's ask): alloc xdim day-sums == existing
single-dim mart day-sums == never exceed FACT_WAREHOUSE_DAILY credits (text
locks + a pandas fixture harness on synthetic frames). Extract-consumer
contract: every consumer's columns ⊆ extract projection (AST/text lock).
Board windows: config tuple == loader windows tuple (cross-lock, kills the
7/30-only drift class). Task-graph lock: every CREATE OR REPLACE TASK in
V041 has a matching RESUME or DEPENDENTS_ENABLE in the same file.

## Joe's apply steps (after build ships)

Push -> Snowsight: V041 after V040 -> re-run roles.sql (new objects) ->
validate.sql expects V001..V041 -> redeploy on warehouse runtime ->
fleet board after 24h tells us what the pass actually bought.
