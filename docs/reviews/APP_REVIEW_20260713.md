# App review 2026-07-13 — twenty recommendations, grounded and ranked

State reviewed: v4.39.0 (post-rebuild, V001..V042 applied, fleet board
healthy — zero failures, pain concentrated in known routed keys). Every
item cites the code or telemetry that motivates it. Ranked by impact ÷
effort within each tier. These are RECOMMENDATIONS for adjudication —
nothing here ships without its own verified round.

## Tier 1 — data honesty (the numbers themselves get better)

1. **Percentile states on the query facts** (routed r22 #19; the headline
   of the next loader re-derivation). Every mart p95 today is a labeled
   "peak hourly/daily" stand-in (`MAX(P95_ELAPSED_SEC)` in
   fact_query_window_summary, eff_sizing_profile, schema_window_summary,
   fact_warehouse_pressure). Store APPROX_PERCENTILE_ACCUMULATE state on
   FACT_QUERY_HOURLY/_DAILY; readers COMBINE+ESTIMATE — true window p95s,
   caveats deleted everywhere at once.
2. **Settle V1 (EXECUTION_STATUS 'FAIL' vs 'FAILED') and centralize the
   constant.** V002-era loaders count `= 'FAIL'`; V027 arms count
   `= 'FAILED'` — one side has counted zero failures since birth. One
   Snowsight probe decides; the fix is one line PLUS a shared constant so
   the split cannot recur (docs/reviews/CODEX_R22_ADJUDICATION, item V1).
3. **Weighted fleet percentiles** (routed r22 #20). Telemetry persists all
   slow/failed + a 2% healthy sample, then computes plain p50/p95
   (mart_sql.fleet_query_stats) — systematically overstated (the board
   now says so; make it stop being true). PERSIST_REASON + SAMPLE_RATE
   columns, weighted stats.
4. **Cache-hit telemetry is a dead gauge.** The fleet board shows 0.0%
   cache hit on every page (2026-07-12 screenshots) — CACHE_HIT lands
   only on rows "new enough to carry it," and the persisted population is
   slow/failed-biased (a slow fetch is rarely a cache hit). Either
   populate CACHE_HIT on the 2% healthy sample reliably and label the
   gauge, or drop the column from the board — a gauge that always reads
   zero teaches people to ignore the board.
5. **Alert-precision feedback where alerts live.** rule_precision(90) is
   read on the Alerts page but buried in a lower panel; alert threshold
   edits happen at the top. Put precision + acceptance beside each rule's
   edit control so tuning uses evidence at the point of decision.

## Tier 2 — measured latency still on the board

6. **CS-by-query-type day mart** (routed r22 #16): cs_types sat at 21.4s
   on the post-rebuild board; the extract needs
   CREDITS_USED_CLOUD_SERVICES and a small day×type aggregate; the
   ELEVATED drill goes mart-first with live fallback.
7. **Failed-run detail mart for the task RCA** (completes r23 #3): the
   SCHEDULED_TIME prune helps, but t_rca's floor is still a TASK_HISTORY
   scan. A small hourly mart (failed runs only, with GRAPH_RUN_GROUP_ID
   for the root-cause/cascade split build_failure_timeline needs),
   extract-style coverage gate, live fallback.
8. **Alerts + Admin live-tier audit** (backlog #10): alerts.py carries 6
   live-tier reads, admin.py 8 — most are OVERWATCH config tables
   (ALERT_CONFIG, ROUTES, SETTINGS) that belong on recent/metadata tiers;
   live is deliberate only for operator-edit surfaces. First paint on the
   triage pages drops for free.
9. **AI users mart re-swap behind a visible proof** (r22 #15 app half).
   The fact now carries EMAIL + exact FIRST/LAST stamps (V042). Add an
   Admin recon row (fact vs live, same window, row+sum equality) and swap
   the tab only after it reads green for a week — the 17.6s×12 key
   retires WITHOUT re-earning July 12th.
10. **Loader v2 re-derivation bundle** (routed r22 #4/5/6/8 as ONE
    reviewed change): hour-scoped fact rebuilds, watermark-scoped mart
    refreshes, incremental diag, no-op MERGE suppression — plus #1 above
    riding the same re-derivation. One seam, one equality-lock supersede,
    one round.

## Tier 3 — triage workflow

11. **Shareable scope links.** The URL already carries page + section;
    serialize the whole filter set (company/env/days/db/contains) into
    params so a triage state pastes into Slack. apply_filters() and the
    saved-views popover (main.py `_views_popover`, V013) already do the
    hard half.
12. **Alert → pre-scoped drill links.** ALERT_EVENTS rows know company
    and (often) warehouse/database; the drawer's "investigate" action
    should request_navigation() with filters applied — today the reader
    re-builds scope by hand for every alert.
13. **Error-log families panel.** APP_ERROR_LOG renders raw in Admin; a
    grouped view (ERROR_TYPE × CONTEXT family, first/last seen, count,
    owning loader) would have made this week's two incidents one-glance
    diagnoses. The mart_load_failed vs extract_load_failed taxonomy from
    V041/V042 is already structured for it.
14. **Anomaly "so what" wiring.** The attribution tab flags anomalous
    warehouse-days (flag_anomalies) but the day-replay drill
    (day_spend_movers/day_activity, Control Room) is three navigations
    away — put a "replay this day" button on each flagged row.
15. **Saved views v2: per-view default landing page** — views restore
    filters today; landing page choice exists separately. Combine, so
    "Monday morning" = one click (prefs schema already per-user, V013).

## Tier 4 — platform hygiene

16. **Retention dashboard.** SP_PURGE_FACTS now governs 24 tables (V042);
    SOURCE_FRESHNESS_STATE already carries ROW_COUNT + GENERATION. A
    small Admin panel: rows vs retention window per table, last purge
    delta — storage drift becomes visible before it's a bill.
17. **Empty-state contract test.** The rebuild proved every fresh-install
    empty state by hand. Codify: an AppTest pass with EMPTY frames
    asserting every page renders its labeled empty state (no zeros, no
    crashes) — the no-fake-numbers contract, mechanized.
18. **Compare Phase 2 (environment lens).** V037 rebuilt the pattern mart
    with DATABASE_NAME grain + HLL users specifically for env-vs-env;
    Compare still only does period-vs-period. The groundwork is aging
    while the question ("PRD vs SIT cost of the same workload") stays
    manual.
19. **Query-tag governance loop.** tag_coverage names the top untagged
    users; the AI-exceptions pattern (generated INSERT statements into
    ACTION_QUEUE) applies verbatim — one expander turns the scoreboard
    into assigned work.
20. **Density toggle on the token layer.** theme.py's spacing/type tokens
    (--ow-1..6, radii) make a compact mode a variable swap — laptop
    triage currently scrolls; ops rooms live on laptops. Cheap because
    the token discipline already paid the hard cost.

## Suggested sequencing

r24: #2 (one line + constant), #4 (gauge honesty), #8 (tier audit) —
small, immediate. r25: #6 + #7 (the two remaining board keys, one
migration). r26: #10 with #1 riding it (the big loader round, one seam).
Workflow items (#11-#15) slot into any round as app-only riders; #17
lands with whichever round touches tests next.
