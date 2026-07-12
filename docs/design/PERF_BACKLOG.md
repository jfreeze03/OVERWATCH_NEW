# Performance backlog — full re-analysis at v4.35.1 (2026-07-12)

Ranked by measured pain (fleet boards 07-11/07-12) x effort. Everything here
is UNIMPLEMENTED as of daaef13; implemented items from Codex r11-r21 are in
CHANGELOG and excluded. Status tags: [V041] rides the loader pass, [BATCH]
next page/reader fix-batch, [CORE] query-core v2, [RENDER] polish round.

## Tier A — the loader pass IS the perf program [V041]

One migration, ELEVEN riders now. Kills the biggest measured keys:

1. Staged QUERY_HISTORY extract (one scan/cycle feeds facts, alloc, tag
   coverage, diagnostics, timeline). Evidence: 7-8 overlapping hourly scans.
2. Cross-dimensional alloc fact (DAY x WH x DB x USER) — db/schema-filtered
   attribution stops paying two live QUERY_HISTORY scans per value.
3. AI Users onto FACT_AI_USAGE_DAILY — cortex_users p50 17.6s x12 runs is
   the single worst user-facing key on Chargeback & AI.
4. Exec-board rework: build windows 14/60/90 (today they ALWAYS fall to the
   13-month live metering scan), single-pass KPI aggregation, atomic swap
   (DELETE+INSERT gap currently triggers the live fallback), drop the three
   panels with no reader (PRESSURE_QUEUE/SPILL, DB_MIX).
5. Watermark loads + nightly delete-and-rebuild reconcile; loader-owned
   freshness rows (retire the 144x/day snapshot task).
6. Ops-diagnostics mart (top-N samples + failure families from the extract)
   — retires the 30-37s batch:pressure keys.
7. Platform-score daily snapshot (Overview first-paint 4-source aggregation).
8. Unused-role posture from FACT_QUERY_ROLE_HOURLY (drops a 90d QH anti-join).

## Tier B — page batching + reader restructures [BATCH]

Grep evidence at daaef13: serial `= run(` per page — Admin 27 (8 live-tier),
Operations 25, Alerts 19 (6 live), Security 10. Brief's v4.19 batching cut
its p95 from 8.9s to ~2-3s; the same treatment is unapplied elsewhere.

9.  Tier-group Operations' tabs into run_batch calls (only the queries tab
    batches today). Biggest serial-latency win in the app.
10. Admin/Alerts live-tier audit: most of those 14 live reads are config
    tables that belong on recent/metadata; live is deliberate ONLY for the
    operator-edit surfaces (settings table, dept budgets post-save).
11. One-pass attribution (r21 #3): UNION ALL both dimensions over one scoped
    CTE, one global denominator — halves Attribution's heaviest scan while
    preserving the v4.33.1 law. Interim until rider 2 lands.
12. Optimize one-pass efficiency + split scans (r18 #7/#8): pruning +
    result-cache from ONE filtered relation; query vs pattern deep scans get
    independent toggles. Evidence: reclaim 19-25s, prune first-runs ~20s.
13. Change-impact drill predicate-first (r20 #15): filter db/schema BEFORE
    UPPER/REPLACE/POSITION text-normalizing every CALL. Evidence:
    chg_hist_TASK 69s.
14. Contract page single metering read (r19 #4 + r21 #17): year projection,
    consumed, and 30d burn from one frame; contract_exhaustion single
    settings pass + conditional aggregates.
15. Security single-scan pairs: grants evidence one tagged CTE (r19 #12),
    changes one QH base (r20 #16-adj), unused-role preaggregate (r21 #18),
    grant timestamp OR -> UNION ALL branches (r20 #19), driver-version
    parse after aggregation (r19 #18).
16. Sizing profile one QH pass via GROUPING SETS (r21 #2) and sizing frame
    feeding the idle advisor (r21 #1) — one reader pair off Optimization's
    heaviest path.
17. Proc leaderboard + graph costs one scoped CTE each (r20 #11/#12);
    CALL/session lookup sargable single-predicate (r19 #14).
18. Storage reclaim candidate-first (r19 #13): scoped storage candidates
    join a flattened ACCESS_HISTORY subset, not 90d account-wide flatten.

## Tier C — engine round [CORE]

19. Rerun-local memo above st.cache_data (skip unpickle for repeated keys),
    per-member batch caching (submit only misses, cap ~4 concurrent),
    Arrow pass-through for table-only consumers, byte-based cache budgets,
    buffered telemetry flush, quarantine retry backoff by error kind,
    canary compile-first mode + bounded concurrency (163 serial executes).

## Tier D — render micro [RENDER]

20. 26 row-wise apply/iterrows sites (hot: brief/overview/spend render
    loops); tz-localized display-frame cache; settings parsed-dict cache;
    lazy page-module imports; AI prompt factories; drill one-shot caching;
    Global Jump options persisted per session.

## Standing owner decision

Dedicated XSMALL UI warehouse (raised r14/r16/r18/r19): isolates page
latency from the task graph. Recommended yes; needs the quota/monitor call.
