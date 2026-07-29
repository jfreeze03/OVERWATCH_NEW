# Performance round — scope & prioritization — 2026-07-29

4-cluster multi-agent scoping (query-execution, mart-loader-sql, reconcile-scheduling,
app-first-paint), each verified against the CURRENT code (post V054-V056 / Batch A).
Impact = user-visible latency or credits saved; verified, not inflated.

## [HIGH · medium effort · medium risk] #9 Live fallback stampedes per-viewer on a mart outage (user baked into cache scope)
**status:** still-real · **cluster:** query-execution · **`app/core/query.py:307-315`**

**Now:** _cache_scope() returns 'role=..|user=<viewer_name()>|salt=..|extra'. viewer_name() is per-person, so every tier fetcher's cache key is unique per viewer. run_mart_first (app/ui/components.py:347-357) routes the live ACCOUNT_USAGE fallback through run(), which uses this same scope. When a mart is empty/failed, each distinct viewer therefore MISSES cache independently and re-runs the same account-wide live scan (the code itself cites 46-56 GB). N concurrent viewers on a mart outage = N full scans on the one XS warehouse. The account-usage/mart data is NOT user-specific (role governs row visibility under SiS; user-isolation only matters for USER_PREFS/saved-view reads), so the user component is unnecessary for these reads and directly causes the stampede.

**Fix:** Scope account-wide mart/live reads by role only; include user in the key ONLY when the SQL touches user-specific objects (USER_PREFS/saved views). Add a per-read flag (e.g. user_scoped=False default) or detect USER_PREFS tokens like the _DOMAIN_TOKENS map already does, so mart/live fallbacks dedupe across viewers and one live scan serves the fleet during an outage.

## [HIGH · medium effort · medium risk] #15 Cost default section fires ~12 sequential blocking mart reads; storage + unmapped not deferred
**status:** still-real · **cluster:** app-first-paint · **`app/ui/pages/cost.py:64-90`**

**Now:** The default section 'Spend & Attribution' (cost.py:64) renders _spend_tab -> _storage_tab -> _attribution_tab -> unmapped in strict top-down order, each panel issuing its own blocking run(). Counting: _spend_tab (spend.py) fires metering_fact (55), csr_fact (104), cs_shapes (148), cs_users (161); _storage_tab fires storage_mtd (330), storage_prior (343), storage_acct (271); _attribution_tab fires wh_vs_prior (171), alloc x2 (219/227), fact_wh_daily(30) (248); plus unmapped_entities (cost.py:77). That is ~12 sequential round-trips on first paint, none using run_batch (contrast unit_costs.py:64 which does). Storage and Unmapped render below Spend/Attribution behind st.divider() yet still block first paint. Most are mart/fact 'recent' reads (individually cheap) but each is a separate Snowflake round-trip.

**Fix:** Group the independent top-level reads (metering_fact, csr_fact, wh_vs_prior, fact_wh_daily(30), unmapped) into one run_batch(tier='recent') so they submit async in parallel instead of 5 serial round-trips; keep the alloc reads (depend on window_usd) and cloud-svc drill separate. Move Storage and Unmapped below-fold into st.expander (lazy) or a 'Load storage/attribution detail' toggle so first paint pays only Spend. On a warm mart this collapses ~12 serial round-trips to ~2-3 parallel groups plus deferred detail — realistically ~1.5-3s off first paint.

## [MEDIUM · small effort · low risk · MIGRATION] #14 Statement-timeout tiers are a no-op in SiS — 30/120/180s silently fall back to the 300s warehouse default
**status:** still-real · **cluster:** query-execution · **`app/core/session.py:139-147`**

**Now:** apply_statement_timeout() is guarded by alter_session_supported() and issues 'ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS'. Its own docstring says 'no-op in SiS (warehouse timeout is the backstop)', and _try_alter_session (session.py:100-115) marks the session unsupported after the first failed probe. SiS runs EXECUTE AS OWNER where ALTER SESSION is rejected, so alter_session_supported() latches False and STATEMENT_TIMEOUTS['live']=30 etc. never take effect. A live-tier query that should abort at 30s can run to the warehouse STATEMENT_TIMEOUT_IN_SECONDS default (300s), burning up to 5 min of XS credits and holding a slot. The tier constants in query.py:31 are effectively dead in production SiS.

**Fix:** Since per-statement session timeouts don't apply in SiS, set the backstop where it CAN take effect: ALTER WAREHOUSE ... SET STATEMENT_TIMEOUT_IN_SECONDS=180 (or a tier-appropriate value) in a migration so runaway live scans abort well before 300s. Optionally pass statement_params on the Snowpark call if the runtime honors it. Keep the ALTER SESSION path for non-SiS/dev.

## [MEDIUM · small effort · low risk] #11 Unit costs runs heavy live QUERY_ATTRIBUTION_HISTORY leaderboards on open with no mart summary / toggle
**status:** partly-addressed · **cluster:** app-first-paint · **`app/ui/pages/cost_parts/unit_costs.py:53-80`**

**Now:** AI cost is correctly fact-first (ai_costs_by_model mart at 47, live Cortex only if fact unusable — r18 #3), and the query+procedure reads are already parallelized into one run_batch (64, 'Codex #15'). But measured_query_costs and procedure_costs_usd both scan SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY live (insights_sql.py:759, 794) — no mart exists for them — and they execute unconditionally the moment the section is selected. _graphs_tab also runs task_graphs (329) + a live serverless_task_daily SERVERLESS_TASK_HISTORY scan (368) on open. The genuinely on-demand detail (proc trend 168, call lookup 202) is gated behind text input, and the ETL scan is behind a default-off toggle (267) — those are done right.

**Fix:** The two attribution-history leaderboards are the heaviest reads here and fire before the user asks. Either surface a cheap mart/fact summary first (priciest-query KPI) and put the full measured leaderboards behind an expander/toggle like the ETL scan already is, or at minimum move serverless_task_daily behind the same gate. Deferring the two attribution scans off the open-path saves roughly 1-2s whenever Unit costs is selected; batching already prevents them from serializing.

## [MEDIUM · small effort · low risk] #16 Overview critical path is correctly ordered (KPI first) but the 4 KPI-feeding reads run serially
**status:** partly-addressed · **cluster:** app-first-paint · **`app/ui/pages/overview.py:149-234`**

**Now:** Ordering is already right: board (149), _bt_hist fact_daily_spend(150) (168), open_alerts (171) and score_inputs (217) all feed the KPI row painted at 281; monthly_spend_by_warehouse (285), spark activity (329), action_queue (391) and latest_digest (422) all come AFTER the KPI row, so first paint does NOT block on digest/action-queue/monthly — the concern's failure mode. The remaining cost is that the 4 KPI-feeding reads are serial round-trips. There is a deliberate design note (145-148) against batching board+MTD (separate cache keys so a filter change only refetches the board), so board must stay separate — but open_alerts + score_inputs + the 150d fact could be parallelized with the board.

**Fix:** Keep the board on its own key (honor the 145-148 note) but submit open_alerts, score_inputs and fact_daily_spend(150) as one run_batch so 3 serial round-trips overlap. Because these gate the KPI row, parallelizing them shaves roughly 0.5-1.2s off time-to-first-KPI on a warm mart. No reordering needed — the critical path itself is already correct.

## [MEDIUM · small effort · low risk] NEW: Cloud-services drill-in fires two MART_CLOUD_SVC_DAILY reads on first paint via default-selected '(all warehouses)'
**status:** new-issue · **cluster:** app-first-paint · **`app/ui/pages/cost_parts/spend.py:139-168`**

**Now:** In _spend_tab the drill-in selectbox (143) defaults to _ALL = '(all warehouses)', and cloud_svc_top_shapes (148) then cloud_svc_by_user (161) run() unconditionally on that default — two MART_CLOUD_SVC_DAILY reads paid on every first paint of the default Cost section, for an exploratory 'drill in by query shape & user' panel the user has not yet interacted with. This is separate from the elevated-warehouse compile/cs_types reads (which are conditional on an ELEVATED status existing).

**Fix:** Wrap the shape/user drill-in in an st.expander (collapsed) or only issue the reads once the user picks a specific warehouse (skip when pick == _ALL, or require an explicit 'Drill in' toggle). Removes 2 serial mart round-trips from the default Spend & Attribution first paint — ~0.3-0.8s warm, and compounds with the #15 batching fix.

## [MEDIUM · small effort · low risk · MIGRATION] Efficiency mart still scans raw ACCOUNT_USAGE.QUERY_HISTORY though the extract already holds it (#3)
**status:** still-real · **cluster:** mart-loader-sql · **`snowflake/migrations/V056__loader_reconcile_alert_fixes.sql:271`**

**Now:** In SP_LOAD_MARTS_V27 HOURLY scope, arm [1] (MART_WAREHOUSE_EFFICIENCY_DAILY) builds its q CTE FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY WHERE START_TIME >= DATEADD('day',-:d,CURRENT_DATE) (lines 271-274). Every OTHER query-fed HOURLY arm already reads OW_QH_EXTRACT: qfam (335), role_hr (376), schema_hr (410), tagcov (443), alloc q-CTE (479), timeline DDL (637); OPS_DIAG reads the extract too (948, 972). The recurring hourly task calls SP_LOAD_MARTS_V27('HOURLY',2) and the nightly reconcile passes 3; OW_QH_EXTRACT retains 3 days and carries every column q needs (EXECUTION_STATUS, TOTAL_ELAPSED_TIME, EXECUTION_TIME, QUEUED_OVERLOAD_TIME, BYTES_SPILLED_TO_REMOTE_STORAGE, WAREHOUSE_NAME). So for the whole routine/reconcile cycle (d<=3) this raw scan is fully redundant with the extract — it is the ONLY remaining avoidable QUERY_HISTORY scan in the mart loaders. Only wide backfills (backfill_365 passes d>3) genuinely need raw QUERY_HISTORY. QUERY_HISTORY is the largest ACCOUNT_USAGE view, so this is the most expensive redundant read in the cluster.

**Fix:** Route arm [1]'s q CTE to OW_QH_EXTRACT when :d<=3 and only fall back to raw QUERY_HISTORY when :d>3 (backfill). The m CTE (WAREHOUSE_METERING_HISTORY) is unchanged. Removes one full QUERY_HISTORY scan every hour (~24/day). Note the fail-count predicate must switch to the extract's stored value 'FAIL' (see the FAILED/FAIL item).

## [MEDIUM · medium effort · medium risk] #12 Batch cache is tuple-level: one changed member SQL busts the whole batch
**status:** still-real · **cluster:** query-execution · **`app/core/query.py:403-425`**

**Now:** _fetch_recent_batch(sqls: tuple, scope, _page) and siblings are st.cache_data-keyed on the full tuple of capped SQL strings. run_batch (query.py:477-479) joins all member SQLs into one key. If any single member's SQL text changes (a filter that affects only that query bakes into its SQL), the tuple changes and the ENTIRE batch cache misses -> all N members re-execute server-side, even the unchanged ones. The per-query run() path only kicks in on FAILURE (the _BatchPartial/except fallbacks at 480-527), not for cache reuse, so the happy path has no per-member cache. security.py batches 7 members, control_room 4+2, brief 5 — a single filter tweak re-runs the full set.

**Fix:** Split the cache boundary from the parallelism boundary: probe each member against the per-query tier cache first (or a per-member cache_data fetcher), then _execute_batch only the misses, and merge. Unchanged members are served from cache while just the changed one re-scans.

## [MEDIUM · medium effort · low risk] #17 Count-based cache eviction (not byte-aware) plus unbounded CSV bytes retained in session_state
**status:** still-real · **cluster:** query-execution · **`app/core/query.py:201-232; app/ui/components.py:656-659`**

**Now:** Tier fetchers set max_entries (256/512/128) and batch fetchers 64 — st.cache_data evicts by ENTRY COUNT, not bytes. Frames carry up to DEFAULT_MAX_ROWS=5000 (app/config.py:92), and detail/CS packs use max_rows=10_000 (security.py:366); 512 such frames is unbounded memory with no byte ceiling, which matters under SiS memory limits. Separately, big-frame CSV export stores the serialized blob in st.session_state[_prep_key] = df.to_csv(...).encode() (components.py:658) and never clears it — every distinct large table prepared in a session accumulates its full CSV bytes in session_state for the session's life.

**Fix:** Lower max_entries on the wide/detail tiers (or gate very large frames out of cache), and evict prepared CSV blobs — key them by a single rolling slot or drop the blob from session_state after the download_button renders / on page change (reset alongside _ow_dl_seq in page_header).

## [MEDIUM · medium effort · medium risk · MIGRATION] Daily fan-out launches 5 parallel children (incl. the heavy reconcile) on one XSMALL — self-inflicted concurrency pileup
**status:** new-issue · **cluster:** reconcile-scheduling · **`snowflake/migrations/V041__loader_efficiency.sql:1597-1607`**

**Now:** Five tasks are all keyed AFTER TASK_LOAD_DAILY (parallel siblings, not chained): TASK_LOAD_MARTS_V27_DAILY (V027:731), TASK_LOCK_WAIT_DAILY (V035:163), TASK_PATTERN_COST_DAILY (V036:161), TASK_NIGHTLY_RECONCILE (V041:1599), TASK_PLATFORM_SCORE_DAILY (V041:1605). In Snowflake, multiple AFTER-children all start concurrently the instant the parent finishes — so at ~06:46 five tasks launch at once on the single XSMALL, one of which (reconcile) internally serial-calls 5 heavy ACCOUNT_USAGE procs (QH_EXTRACT, HOURLY_FACTS, DAILY_FACTS, MARTS_V27, OPS_DIAG). This peak overlaps TASK_LOAD_OBJECT_COST (06:45 root) and TASK_CHANGE_IMPACT_SCAN (06:50 root). This parallel fan-out on a single X-Small is a concrete contributor to the 06:45+ contention and 300s-timeout failure risk.

**Fix:** Serialize the daily fan-out: chain the 5 children with AFTER edges (each AFTER the previous) instead of all AFTER the parent, and move/merge the heavy NIGHTLY_RECONCILE so it doesn't run concurrent with the daily-mart writers it competes with. Keeps one warehouse busy sequentially rather than 5-way contending. Needs migrations (recreate task edges).

## [LOW · small effort · low risk] #10 Global text filters and query-id/proc-name inputs do NOT submit per keystroke
**status:** not-a-win · **cluster:** app-first-paint · **`app/main.py:545-549`**

**Now:** The global contains-filters are plain st.text_input with only key= and no on_change (main.py:545 warehouse, 547 user, 549 schema); the proc-name (unit_costs.py:168) and query-id/session (unit_costs.py:202) inputs are likewise keyed text_inputs, each gated by `if _pname.strip()` / `if _ident.strip()` before any query runs. Streamlit st.text_input commits (reruns) only on Enter or blur, never on each keystroke — so there is no per-keystroke submission to eliminate; it already behaves as an implicit apply-on-commit. An Apply form would only batch the rare case of editing several collapsed advanced filters in one go.

**Fix:** No latency win here — the premise (per-keystroke reruns) does not hold for st.text_input. Optional polish only: wrap the collapsed 'More filters' expander (warehouse/user/schema) in a single st.form with one submit button so editing all three triggers one rerun instead of up to three; negligible first-paint effect.

## [LOW · small effort · low risk · MIGRATION] Loaders re-derive full-table MAX(LOAD_TS)/COUNT(*) for freshness after every write (#19)
**status:** still-real · **cluster:** mart-loader-sql · **`snowflake/migrations/V056__loader_reconcile_alert_fixes.sql:210`**

**Now:** Every loader recomputes MAX(LOAD_TS)/COUNT(*) after writing: SP_LOAD_QH_EXTRACT direct over OW_QH_EXTRACT + FACT_QUERY_HOURLY + FACT_QUERY_DAILY (V056:208-218); SP_LOAD_MARTS_V27 via the MART_SOURCE_FRESHNESS view — a UNION ALL of MAX(LOAD_TS)/COUNT(*) over ~24 fact/mart tables (V045__task_monitoring_restored.sql:2074+), filtered to a 9-source subset (V056:658-667) and a 2-source subset (852-856); SP_LOAD_OPS_DIAG (982); SP_LOAD_HOURLY_FACTS (V041:343). The loaders already set LOAD_TS=CURRENT_TIMESTAMP() on every merged row, so LAST_LOAD_TS is knowable without a lookup. HOWEVER the real cost is small: in Snowflake an unfiltered SELECT MAX(col), COUNT(*) FROM t is answered from micro-partition metadata (min/max + row count) with no compute scan; and although the freshness view's UNION-ALL branch pruning under the IN filter is not guaranteed, each branch is still metadata-only. So the premise is confirmed but this is close to a non-win.

**Fix:** Write LAST_LOAD_TS=CURRENT_TIMESTAMP() directly instead of a MAX() lookup; if ROW_COUNT is still wanted, query the specific base table rather than the 24-branch MART_SOURCE_FRESHNESS view so only the touched tables are referenced. Low priority given metadata-only aggregation already makes the current form cheap.

## [LOW · small effort · low risk · MIGRATION] NEW (correctness): FAILS columns filter EXECUTION_STATUS='FAILED' but the stored value is 'FAIL'
**status:** new-issue · **cluster:** mart-loader-sql · **`snowflake/migrations/V056__loader_reconcile_alert_fixes.sql:265`**

**Now:** Four HOURLY arms count failures with EXECUTION_STATUS='FAILED': efficiency q (V056:265), qfam (323), role_hr (374), schema_hr (406). But the primary query facts and OPS_DIAG use 'FAIL' — FACT_QUERY_HOURLY (121), FACT_QUERY_DAILY (156), OPS_DIAG FAIL_FAMILY (974) — with the explicit comment at line 142 "'FAIL' matches the V002 hourly-fact convention". SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY.EXECUTION_STATUS values are success/fail/incident, and the extract copies EXECUTION_STATUS verbatim (88-89). So the 'FAILED' arms almost certainly compute FAILS=0 in MART_WAREHOUSE_EFFICIENCY_DAILY, MART_QUERY_FAMILY_DAILY, FACT_QUERY_ROLE_HOURLY and FACT_QUERY_SCHEMA_HOURLY. This is a correctness bug, not a latency/credit win — flagged for honesty; confirm against one live failed row before changing.

**Fix:** Change 'FAILED' to 'FAIL' in the four COUNT_IF/IFF predicates so they match the extract's stored value and the fact-table convention. (Fixing the #3 efficiency arm to read the extract makes this change mandatory there anyway.)

## [LOW · medium effort · medium risk] #13 Batch submission is not concurrency-bounded — a page fires up to 7 async queries at once on the XS warehouse
**status:** still-real · **cluster:** query-execution · **`app/core/query.py:369-400`**

**Now:** _execute_batch loops over sqls and calls session.sql(sql).to_pandas(block=False) for every member with no semaphore/chunking, then gathers. security.py:38-51 submits 7 members (mfa, creds, logins, login_reasons, admins, grants, newnet); security.py:365 packs CS queries at max_rows=10_000 each. All fire simultaneously against the single XS warehouse. This is intentional parallelism (the docstring notes async avoids serialized collect), and 7 is within the default MAX_CONCURRENCY_LEVEL of 8, so it does not overflow — but there is genuinely no bound, and heavy concurrent ACCOUNT_USAGE scans compete for XS compute and can queue/slow each other under load.

**Fix:** Optional: cap in-flight submissions with a small window (e.g. submit in chunks of 4-5, gather, submit next). Honest caveat — bounding trades latency for lower contention; on a lightly loaded XS the current unbounded fan-out is usually the faster choice, so only pursue if telemetry shows batch members queuing. Not a clear win on its own.

## [LOW · medium effort · low risk · MIGRATION] WAREHOUSE_METERING_HISTORY scanned 3x per hourly cycle; one hourly staging could feed all (#4)
**status:** still-real · **cluster:** mart-loader-sql · **`snowflake/migrations/V056__loader_reconcile_alert_fixes.sql:257`**

**Now:** Within one hourly cycle WMH is scanned three times: (1) SP_LOAD_HOURLY_FACTS daily-grain -3d -> FACT_WAREHOUSE_DAILY credits (V041__loader_efficiency.sql:327); (2) SP_LOAD_MARTS_V27 arm [1] efficiency m-CTE daily-grain -:d -> MART_WAREHOUSE_EFFICIENCY_DAILY credits+billed_hours (V056:257); (3) arm [5] allocation wh-CTE hourly-grain -LEAST(:d,2) -> _OW_ALLOC_BASE (V056:468). All three filter WAREHOUSE_ID>0. Scans (1) and (2) compute the SAME per-day warehouse credits twice. The hourly-grain scan (3) is the finest grain and can roll up to serve both daily-grain consumers. The tasks run these in separate procs/tasks (TASK_LOAD_HOURLY -> TASK_QH_EXTRACT -> TASK_LOAD_MARTS_V27_HOURLY), so full cross-proc dedup needs a persistent staging table; but arm [1] and arm [5] live in the same proc and can trivially share one temp WMH-hourly table.

**Fix:** Materialize WMH once per hour at hourly grain (hour, warehouse, SUM credits, SUM compute, COUNT_IF billed) into a persistent OW_WH_METERING_HOURLY table in the extract task, then have hourly-facts, efficiency, and allocation roll from it. Cheap in-proc win: build that temp table at the top of the HOURLY scope and reuse it in arm [1] and arm [5]. Impact is modest because WMH is tiny (one row per warehouse per active hour).

## [LOW · medium effort · low risk · MIGRATION] NEW (perf): task-graph arm scans TASK_HISTORY twice per cycle
**status:** new-issue · **cluster:** mart-loader-sql · **`snowflake/migrations/V056__loader_reconcile_alert_fixes.sql:572`**

**Now:** SP_LOAD_MARTS_V27 arm [6] (MART_TASK_GRAPH_DAILY) scans SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY in the outer FROM (line 572) and AGAIN inside the QUERY_ATTRIBUTION_HISTORY join's IN-filter subquery (line 578: SELECT QUERY_ID FROM ...TASK_HISTORY WHERE QUERY_START_TIME >= ...), plus a QUERY_ATTRIBUTION_HISTORY scan at -:d-1 (575). So TASK_HISTORY is read twice every hourly cycle in this one arm. These ACCOUNT_USAGE views are small, so impact is low, but the second scan is avoidable.

**Fix:** Lift TASK_HISTORY into a single CTE (filtered to the -:d window, STATE IN ('SUCCEEDED','FAILED')) and join QUERY_ATTRIBUTION_HISTORY to it on QUERY_ID, instead of re-selecting QUERY_IDs from TASK_HISTORY inside the attribution subquery. One TASK_HISTORY scan instead of two.

## [LOW · medium effort · medium risk · MIGRATION] Nightly reconcile is a blunt 3-day delete+full-reload, not a targeted late-arrival repair; overlaps what the daily/hourly graph just loaded
**status:** partly-addressed · **cluster:** reconcile-scheduling · **`snowflake/migrations/V056__loader_reconcile_alert_fixes.sql:872-916`**

**Now:** SP_NIGHTLY_RECONCILE (runs AFTER TASK_LOAD_DAILY, ~06:46) DELETEs the trailing 2-3 days from ~11 fact/mart tables (L879-900), resets the QH_EXTRACT+DAILY_FACTS watermarks back 3 days (L903-906), then unconditionally re-runs SP_LOAD_QH_EXTRACT(0), SP_LOAD_HOURLY_FACTS(), SP_LOAD_DAILY_FACTS(), SP_LOAD_MARTS_V27('HOURLY',3), SP_LOAD_OPS_DIAG(3) (L908-912). SP_LOAD_DAILY_FACTS was just executed by TASK_LOAD_DAILY minutes earlier (V002:259-264), and its own overlap window is watermark-1d ~= yesterday only (SP_LOAD_DAILY_FACTS lo_short/lo_metering, V045:62-72) — so the reconcile re-scan genuinely adds D-2/D-3 coverage the narrow daily watermark skips, BUT the immediate SP_LOAD_DAILY_FACTS re-call plus the width-3 hourly re-scan largely duplicate work the 06:45 daily run and the 06:07 hourly run already did that morning. V056 (task #17) only narrowed the extract-fed-mart DELETEs from -3d to -2d to stop truncating a day the 72h extract can no longer refill; the full-rebuild redundancy itself is unchanged. Premise 'same 3 days the daily graph already loaded' is ~1/3 accurate — the daily graph reloads only ~1 of those 3 days.

**Fix:** Fold the late-arrival coverage into the routine daily loader by widening SP_LOAD_DAILY_FACTS overlap from ~1d to 3d (change the lo_short/lo_metering floor) and retiring the separate delete+reload proc, OR make the reconcile targeted: drop the immediate SP_LOAD_DAILY_FACTS re-call + DAILY_FACTS watermark thrash, keep only the width-3 extract/OPS_DIAG/hourly-mart extension that actually extends past the hourly task's width-2. All MERGE-idempotent, so DELETEs are unnecessary. Needs a migration (CREATE OR REPLACE PROCEDURE).

## [LOW · medium effort · low risk · MIGRATION] Six independent daily root tasks collide on the single XSMALL warehouse in the 06:30-06:50 window; two exact-timestamp collisions
**status:** still-real · **cluster:** reconcile-scheduling · **`snowflake/migrations/V002__facts.sql:9-16`**

**Now:** All daily tasks run on WH_ALFA_OVERWATCH (XSMALL, single-cluster, AUTO_SUSPEND=60, STATEMENT_TIMEOUT=300, V002:9-20). From the crons: 06:30 TASK_LOAD_STORAGE_TRUTH (V046:101), 06:40 TASK_ANOMALY_SWEEP (V012:238) + TASK_WAREHOUSE_CHANGE_SCAN (V024:337) fire simultaneously, 06:45 TASK_LOAD_DAILY (V002:261) + TASK_LOAD_OBJECT_COST (V048:160) fire simultaneously, 06:50 TASK_CHANGE_IMPACT_SCAN (V010:416). Standalone root tasks do NOT serialize by warehouse in Snowflake — same-timestamp tasks run concurrently, contending for the one XSMALL's compute/8 concurrency slots. The 06:45 collision is worst: TASK_LOAD_DAILY roots the entire daily fan-out. Two cost effects: (1) contention lengthens elapsed and risks the 300s statement timeout on heavy ACCOUNT_USAGE scans; (2) 5-10min gaps between staggered jobs exceed AUTO_SUSPEND=60s, so the warehouse suspends/resumes ~4x/morning, each paying the 60s minimum billing.

**Fix:** Consolidate the daily scans into one dependency chain (AFTER edges) so they run back-to-back in a single warehouse uptime window, serialized to avoid XSMALL contention, instead of firing on independent crons that both overlap and leave suspend/resume gaps. At minimum de-collide the two exact pairs (06:40, 06:45) by spacing them. Needs migrations (ALTER/CREATE TASK per node).

## [LOW · medium effort · low risk · MIGRATION] No per-node loader timing (scheduled delay / queue / exec / rows) captured — only per-pipeline elapsed + credits; can't tune the overlap
**status:** still-real · **cluster:** reconcile-scheduling · **`snowflake/migrations/V056__loader_reconcile_alert_fixes.sql:560-608`**

**Now:** MART_TASK_GRAPH_DAILY (schema V045:35-46) stores per-PIPELINE per-DAY aggregates: GRAPH_RUNS, TASK_RUNS, AVG_WALL_SEC, P95_WALL_SEC, WH_CREDITS, RUNS_WITH_FAILURES. Its loader (V056:560-608) computes WALL_SEC = MAX(COMPLETED_TIME)-MIN(QUERY_START_TIME) over the whole graph and sums QUERY_ATTRIBUTION credits to the pipeline grain. It reads TASK_HISTORY but never uses SCHEDULED_TIME, so the scheduled-vs-actual-start delay (the exact signal that would quantify the 06:40/06:45 warehouse contention and queueing) is discarded. There is also no per-node elapsed, no per-node credits (rolled up), and no rows-loaded per node (procs RETURN a status string that is not logged). So to tune #6/#2 you currently have only whole-pipeline elapsed totals — the queue-delay and per-step breakdown you'd tune from do not exist, even though SCHEDULED_TIME/QUERY_START_TIME are already in the source scan (free to add).

**Fix:** Widen the task-graph mart (or add a companion per-node table) to capture per-task SCHEDULED_TIME->QUERY_START_TIME dispatch/queue delay, per-node QUERY_START->COMPLETED exec, per-node credits, and (from proc RETURN parsing or a run-log insert) rows loaded. Cheap — same TASK_HISTORY scan already runs. Enables data-driven de-collision for #6 and targeting for #2. Needs a migration.

## [LOW · large effort · medium risk] #8 Cache keys omit mart generation (LAST_LOAD_TS) — hourly TTL can't safely lengthen with instant invalidation
**status:** still-real · **cluster:** query-execution · **`app/core/query.py:293-315`**

**Now:** _cache_scope encodes role, viewer, manual refresh salt, and post-write domain salts — but nothing tied to the underlying mart's load generation. SOURCE_FRESHNESS_STATE.LAST_LOAD_TS exists per source (app/data/mart_sql.py:459-465) and is the natural generation token. Because it is absent from the key, the hourly tier relies purely on a fixed 3600s TTL (query.py:27-31): a fresh mart load mid-hour isn't reflected until the TTL expires, and the TTL can't be lengthened beyond ~1h without risking staleness. Incorporating a generation stamp would let hourly/historical TTLs extend to many hours while a new load busts the key immediately.

**Fix:** Read SOURCE_FRESHNESS_STATE once per session at a short TTL (shared, not per-query), fold each read's relevant source LAST_LOAD_TS into _cache_scope for mart-tier reads, then raise hourly/historical TTLs. Net win is modest since the hourly TTL already cut 12x/hr to ~1x/hr; the gain is safe multi-hour caching plus instant post-load freshness. Weigh the added freshness read against the savings.
