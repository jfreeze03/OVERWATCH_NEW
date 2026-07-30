# Performance round 2 — scope & prioritization — 2026-07-29 (post-v4.70.1)

5-territory multi-agent scoping (deferred-queue re-verify, page first-paints, new-code perf,
SQL heaviness, loader SQL round 2), every item adversarially verified against CURRENT code.
All verdicts CONFIRMED (one DOWNGRADED, kept at its honest rating); nothing silently dropped —
declined/no-action items live in T4 with reasons, keeping dispositions auditable.


## T1 — app quick wins (small effort, low risk)

### T1.1 [medium · small · low] Adopt the 'hourly' cache tier for hourly/daily-loaded fact & mart reads (r13 #3 tier has zero direct adopters)
`app/core/query.py:221`

Move multi-day/30d fact & mart reads from tier="recent" to tier="hourly": cost.py:71-72/94-96, spend.py:76-78/129-132/179-181/192-194/205-208/283-285, overview.py:53-57/168-169/186-187/362-364/454, operations.py:51-53/59-61/72-74/458-459/519-520, control_room.py:254-255. Preserve possibly-deliberate recent overrides on morning-triage panels (control_room.py:239-241 1-day pulse, ops q_fact_summary, overview.py:228-233 explicit mart_tier="recent") and the source_freshness_state carve-out. Update test locks: tests/test_perf_pass.py:177 and tests/test_codex_r14.py:44-50. Win: first-paint reads on the three hottest pages stop re-missing 12x/hour per viewer.

### T1.2 [medium · small · low] Retier live ACCOUNT_USAGE window aggregates from 'recent' (300s) to 'historical' — sources lag 45min-3h
`app/ui/pages/operations.py:121`

Move to tier="historical": operations.py filtered QH batch :121-127 + serial fallbacks :130-133/:228-231, task_failure_details :243-245, copy_load_failures :390-391, volume_deltas :406, dynamic_table_health :420, task_runs fallback :461-462, warehouse_concurrency_peaks :545-547; security.py egress_daily :202-203, unload_activity :226-228, recent_ddl_changes :471-473, admin_role_activity :500-502, plus the recent-vs-historical-batch fallback mismatches at :80-82, :98-100, :108-110, :125-127, :186-187. Note: aligning the newnet fallback tier buys consistent TTL/timeout semantics, NOT cache sharing (batch results cache under the tuple-keyed fetcher). Refresh salt still forces instant re-fetch; no test pins these tiers.

### T1.3 [medium · small · low] Security Changes tab: restore the run_batch the comment claims (code is two serial live QH scans); Egress tab same shape
`app/ui/pages/security.py:467`

Add a 2-member run_batch for recent_ddl_changes (:471) + admin_role_activity (:500) — same tier, days-scoped, no mixing; do the same for _egress_tab (:202-203 DATA_TRANSFER_HISTORY + :226-228 unload QH scan). Composes with the historical-tier retier item. Git history shows the r23 #4 batch was dropped as collateral of a v4.49.0 panel move (922a4c6), not a decision — delete or correct the stale comment at :467-469 either way. Sum-to-max of two multi-second XS scans on section cold open.

### T1.4 [medium · small · low] Change-impact section: 28d WMH+QH drill scans run on every render with no selection — gate behind explicit selection or a load toggle
`app/ui/pages/operations.py:630`

warehouse_daily_series (live 28d WMH+QH join, run at :644-646 because :630 defaults to df.iloc[0]) and object_run_history (:706-709, always-truthy pick at :703; 28d QH ILIKE scan for PROCEDUREs) should run only when sel/sel_ci is not None or behind a load toggle matching the DAG/streams pattern on the same page (:434, :486); also move both to tier="historical". Both currently re-miss every 300s while the section is open. Flag the one-click UX regression to the owner. Note the r23 #2 '6s' comment predates the ILIKE prefilter — today's drill is cheaper but still a full 28d QH scan.

### T1.5 [medium · small · low] Operations Queries default (unfiltered) path: batch the 4-6 serial mart reads — batch only exists on the filtered path
`app/ui/pages/operations.py:102`

Prefetch summary (:51), spark (:72), and the two sequential run_mart_first ops_diag legs (:102-116) in one run_batch and hand results in via preloaded= — the pattern lives at overview.py:75-77 and cost.py:65-79 (NOT the stale 'unit_costs.py:53-79' cite). Accept the one-cheap-read coupling: ops_spark_activity is fixed-14d/company-scoped so a days change cold-starts it inside the batch. Default tab of a hot page at 300s TTL; ~3 round trips saved.

### T1.6 [medium · small · low] CSV prep blobs: positionally keyed, never evicted — wrong/stale table's bytes served after a page or section switch
`app/ui/components.py:649`

Fold PAGE identity + a content fingerprint of the current df into _prep_key (components.py:649) — not just section — and serve the 'CSV ready' branch (:651-655) only on match; keep a single blob slot per session (or delete on mismatch) so seq-shift orphans can't accumulate. styled_table (:665) has no key parameter (calls _render_table with key=None), so all its keys are purely positional (ow_dlprep__N) and _ow_dl_seq is one global key reset by every page_header (:125) — the collision is cross-PAGE. Nothing anywhere deletes ow_dlprep_* keys. Fix is local to components.py:645-659; covers both bug-note variants.

### T1.7 [low · small · low] Regroup run_batch members by filter dependency (adopted in lieu of the declined per-member cache split)
`app/ui/pages/security.py:38`

Split each page's batch into filter-coupled vs fixed groups so a filter change re-executes only the coupled group: security.py:38-53 (admins/grants(days)/90d-newnet are company-independent; either axis busts 3 of 7 unchanged members), operations.py:99-127 ('fails' lacks wh/user filters), unit_costs.py:53-64 ('p' lacks wh/user; conditional 'ai' reshapes the tuple), spend.py:59-68 ('daily' pinned at 30d). QueryResult has no __bool__, so callers' `.get(k) or run(...)` contract survives regrouping unchanged. Mostly credits (unchanged multi-second scans stop re-executing server-side), plus some wall time when an unchanged member is the batch's slowest (plausible for newnet's 90d LOGIN_HISTORY scan).

### T1.8 [low · small · low] Alerts pre-section chrome: retier MAX(NOTIFIED_AT) off live and batch the pre-dispatch reads
`app/ui/pages/alerts.py:120`

_delivery_status (:120-127) runs a MAX(NOTIFIED_AT) at tier='live' before section dispatch every rerun — retier to 'recent' (safe twice over: NOTIFIED_AT is written server-side by TASK_ALERT_NOTIFY, and the SQL contains ALERT_EVENTS so the alerts domain-salt bump forces refetch on in-app writes). Batch it with the events read at :464. Caution: no precedent exists for SHOW commands inside run_batch's async submit (the only SHOW batch-adjacent read is serial) — keep SHOW INTEGRATIONS/TASKS serial unless the SHOW-metadata batch is tested first. ~2 round trips cold plus one live read per 30s steady-state.

### T1.9 [low · small · low] Standardize open_alert_events LIMIT at 500 across singleton call sites so the live tier shares one cache entry
`app/ui/pages/control_room.py:362`

overview.py:90 (500), alerts.py:464 (300), control_room.py:362 (100) differ only by LIMIT baked into the SQL (mart_sql.py:334); with the caller key dropped from _cache_scope, the LIMIT is the only splitter. Use 500 at all three and head(n) client-side — order-safe because the SQL sorts severity-rank then RAISED_AT DESC before LIMIT. Leave brief.py:41 (50, inside the live batch tuple) alone. MUST also update the hardcoded '300 most recent' caption at alerts.py:479 (whose wording is wrong anyway — see bugs). Win: one fewer serial live read on pages 2-3 of the triage walk.

### T1.10 [low · small · low] Security Access: fold the healthy-path serial reads into the existing 7-member batch (DOWNGRADED to low)
`app/ui/pages/security.py:57`

users_without_mfa_live fires serially whenever the batch mfa member is ok-but-empty (:57-64) — and empty IS the healthy steady state; unused_roles is a serial run_mart_first at :173-177. Add both as batch members (rare wasted member when gaps exist is acceptable); gov_posture :278 correctly stays serial. Downgraded because the whole section runs at 3600s TTLs on a secondary page — extra reads are paid ~once/hour/scope. Cheap tidy-up.

### T1.11 [low · small · low] Admin Settings table read: retier live→recent — domain-salt invalidation already covers the write path
`app/ui/pages/admin.py:135`

The SETTINGS read at :135-136 is tier='live' but the write path (:156-165) goes through execute_statement whose _bump_refresh matches the 'OVERWATCH.SETTINGS' domain token, so an edit invalidates the entry regardless of TTL — retier to 'recent' loses nothing for the editing session, and the UI copy at :168 ('takes effect within one cache cycle (<=5 min)') already assumes recent semantics. Accepted caveat (same as alert_routes r24 #8 precedent): another admin's concurrent edit surfaces at 300s instead of 30s. Micro-credits.

### T1.12 [low · small · low] Unit-costs open path: gate the live serverless-task scan behind a toggle (round-1 #11 residue)
`app/ui/pages/cost_parts/unit_costs.py:368`

The clean piece worth doing: gate serverless_task_daily (:368-370, live scan run unconditionally via _graphs_tab at :318) behind a toggle matching the ETL precedent at :267. The two-member QAH leaderboard batch (:53-64, tier='historical', 3600s) already absorbed the recurring cost — ~1-2s cold wall remains. Caveat on the optional KPI substitution: MART_PATTERN_COST_DAILY is parameterized-hash grain, not 'priciest single query' — either label it pattern-grain or keep the KPIs behind the same toggle.


## T2 — app structural (medium effort)

### T2.1 [high · medium · medium] Control Room first paint: ~12-16 serial blocking reads — batch to ~3 round trips
`app/ui/pages/control_room.py:239`

Group the eight already-recent-tier members (pulse :239, activity :254, incident metrics :290, task fact :366, warehouse daily :373, timeline :419, lock spikes :451, freshness :189) into one run_batch — EXCEPT cr_movers (:463), the only days-scoped member: put it in its own group or leave serial, or every days change cold-starts the whole batch (the exact Codex #4 coupling). Second live group for open incidents :305, open alerts :362, incident proposals :333 (keep the _is_op gate on proposals — no other consumer; non-operators currently pay it). Preserve every mart→live fallback (:251, :369, :425, :467) — that is where the risk lives. Live trio is also re-paid serially every 30s, so batching helps steady-state too.

### T2.2 [high · medium · medium] Overview (the landing page) first paint: ~8-10 serial reads — round-1 #16 batching never landed
`app/ui/pages/overview.py:149`

overview.py imports only run — no run_batch anywhere. Batch exec board :149, 150d fact :168, score-inputs :228, spark :362, digest :454; live group for action_queue :250 (+company-scoped open_alerts coupling caveat, minor at 30s TTL, flagged by the :145-148 comment). Two mandatory wrinkles: (a) the monthly run_mart_first :318 rides the hourly tier (3600s) and requires 12 months to accept (:326) — give it its own hourly group or leave serial, do NOT silently drop it to 300s; (b) keep open_alerts :90/:182 serial — it is warm for free via health_strip's identical SQL at main.py:362 (caller key excluded from scope).

### T2.3 [medium · medium · low] Contract & Forecast: ~10 settings-driven reads issued serially behind the section click — batch to ~3
`app/ui/pages/cost_parts/contract.py:233`

All ~10 reads (cy_projection :102, org_month :139, fact_daily_70 :144, org_balance :48, org_items :61, org_spend :195, consumed mart :264, steer_idle :295, steer_pats :301, planner_burn :348) are settings-driven, none filter-coupled, all cold on first visit — 2-4s saving with three ORGANIZATION_USAGE reads among them. Three caveats the fix must absorb: (1) run_mart_first (components.py:322) has no preloaded= parameter — add the seam so mart_accept (:270-271) still applies to batched frames; (2) the three mart legs default tier='hourly' — use a third hourly-tier run_batch (supported, query.py:433-440), don't downgrade them to 300s; (3) keep membership gated on CONTRACT_* settings (:242-253) and org_items conditional on org_balance, or org-invisible installs feed a permanently failing member into quarantine.

### T2.4 [medium · small · medium] health_strip runs at tier live (30s) on every page's critical path — retier to recent (300s); needs product sign-off
`app/main.py:362`

main() computes _health_values() before any page renders; the 4-arm UNION (scanning SOURCE_FRESHNESS_STATE twice, mart_sql.py:481-509) is hourly-cadence data repaid up to 120x/hour. Move ALL three call sites in lockstep (main.py:362, overview.py:243, brief.py:56) — they share one entry only because SQL+tier+scope match. Ack/close stays instant for the acting operator (ALERT_EVENTS domain salt), but salts are per-SESSION: retier extends BOTH new-critical latency and cross-viewer ack/close visibility to up to 5 min — that is the sign-off. Free micro while editing: fold the two SOURCE_FRESHNESS_STATE arms into one.

### T2.5 [low · medium · low] run_batch absent-mart standdown: with marts absent, 4 serial failing reads rerun forever with telemetry spam
`app/core/query.py:494`

Absent marts put all four Spend prefetch specs (cost.py:71-72, spend.py:59-68) through quarantine (query.py:494-496) where rehab needs _solo.ok (:474) — impossible while objects are absent — so every rerun walks 4 serial failing run() calls plus record_error + persisted-telemetry INSERTs to the 60/session cap (~0.4-1s/rerun, degraded-state only). Seams exist: error_kind='absent' classification (query.py:122-132) and run(probe=True) (:596-600); add per-spec absent-standdown markers to run_batch, cleared on refresh-salt bump so recovery needs no restart. Healthy path verified to have no double reads — this is operational-noise hygiene as much as latency.


## T3 — migration (V061 loader-SQL candidates)

### T3.1 [medium · small · medium] V061: route efficiency arm [1] through OW_QH_EXTRACT when d<=2 — round-1 #3 was never applied
`snowflake/migrations/V060__family_elapsed_queued_alert_guard.sql:82`

Arm [1]'s q CTE still scans raw ACCOUNT_USAGE.QUERY_HISTORY at -:d (~24 two-day scans/day at d=2, +1 at d=3) while TASK_QH_EXTRACT runs immediately before in the same chain and all six other query-fed HOURLY arms already read the extract. The d<=2 gate is LOAD-BEARING: extract retention floors at CURRENT_TIMESTAMP-3d but arm windows are CURRENT_DATE-based, so the reconcile's d=3 window is not covered and the FULL OUTER JOIN + MERGE WHEN MATCHED would silently overwrite D-3 with zeros/partials — keep the raw path for d>=3. Fix the false V056:67 comment in the same migration. Semantic note: arm [1] inherits the extract's frozen-RUNNING long-runner staleness (see bugs) — document it. Largest remaining redundant ACCOUNT_USAGE scan in the loader.

### T3.2 [low · medium · low] V061: consolidate the 4x-per-cycle TASK_HISTORY scans in SP_LOAD_MARTS_V27 into one temp-table extract
`snowflake/migrations/V060__family_elapsed_queued_alert_guard.sql:384`

Four scans per hourly cycle: arm [6] outer (:384), the QAH IN-subquery (:390-392, the deferred round-1 double-scan), arm [6b] added by V058 (:452-454), and arm [8]'s 48h FAILED branch (:494-495, a provable subset for d>=2). Build one temp table over the -:d window feeding all four — _OW_ALLOC_BASE (:276) is the in-proc precedent. Caveat: d is clamped GREATEST(1,...), and at d=1 the arm [8] subset property breaks (-48h is wider than midnight D-1) — build over the wider window or leave arm [8] raw when d<2. ERROR_MESSAGE is unused by all three arms. ~5-15s/hour saved; TASK_HISTORY is small, mostly fixed secure-view overhead.

### T3.3 [low · small · low] V061: share one hourly-grain WMH staging across arms [1]+[5] (round-1 #4 re-verified)
`snowflake/migrations/V060__family_elapsed_queued_alert_guard.sql:64`

Arm [1] m CTE (daily-grain, -:d, :64-71) and arm [5] wh CTE (hourly-grain, -LEAST(:d,2), :277-283) are in the same proc; roll up from one hourly-grain staging — WMH is one row per warehouse-hour, so day-grain BILLED_HOURS = SUM of per-hour COUNT_IF(credits_used>0) flags reproduces the current COUNT_IF exactly, and SUM-of-SUMs preserves CREDITS_TOTAL/COMPUTE. Leave the third scan (SP_LOAD_HOURLY_FACTS, V041:327-329) and task edges alone per the T3/V058 deferral. ~1-3s/hour; bundle into the same V061 as the arm [1] routing.

### T3.4 [low · medium · medium] V061 (optional rider): trim the nightly reconcile's double-load of SP_LOAD_DAILY_FACTS and the 3d extract refill
`snowflake/migrations/V056__loader_reconcile_alert_fixes.sql:903`

Reconcile resets both watermarks to now-3d (:903-906) minutes after the 06:45 run, re-scanning daily facts ~4d-wide and refilling 3 extract days. Only worth folding into the larger V061, and the fixes are COUPLED: dropping the SP_LOAD_DAILY_FACTS re-call requires also dropping the FACT_METERING_DAILY DELETE (:881-882) or D-3..now metering sits empty until the next 06:45 (FACT_TASK/LOGIN/STORAGE_DAILY self-DELETE+INSERT and are safe). 'Keep DELETEs where ghosts matter' MUST include MART_QUERY_FAMILY_DAILY — its top-2000-per-day QUALIFY (V060:151) means stale families persist without the DELETE and the mart outgrows its advertised grain.


## T4 — deferred / declined (with reasons)

### T4.1 [low · medium · medium] T3 loader schedule changes A/B/C — still deferred awaiting V058 per-node timing data
`docs/reviews/PERF_ROUND_SCOPE_2026-07-29.md:1`

Per round-1 disposition: do not re-scope; the V058 per-node timing mart is deployed-queue-pending, and A/B/C decisions need its data. Noted only. Related trigger recorded elsewhere: exec-board split re-opens only if V058 MART_TASK_NODE_DAILY shows sibling contention.

### T4.2 [low · medium · medium] #8 mart-generation cache keys — keep deferred; win shrank to post-load visibility only
`app/core/query.py:293`

Both prerequisites now exist (SOURCE_FRESHNESS_STATE.GENERATION bumped by every loader MERGE; health_strip is a free MAX(GENERATION) carrier as a 5th arm), but the MERGEs bump GENERATION unconditionally at the same ~hourly cadence as the 3600s TTL, so a generation key CANNOT reduce steady-state executions — remaining value is post-load visibility within ~30s on hourly-tier panels. Wiring a live-tier read into core cache identity needs a salt-only degrade path on strip outage (medium risk). Not worth it now.

### T4.3 [medium · medium · medium] Full per-member batch cache split (probe layer) — declined in favor of the T1 filter-dependency regroup
`app/core/query.py:413`

The tuple-level mechanism is confirmed real (all five batch fetchers key on the full tuple of capped member SQLs + scope; one changed member re-executes every member server-side, e.g. the 90d newnet LOGIN_HISTORY scan on a company toggle). But st.cache_data exposes no probe/peek API, so a true split needs a session-state bookkeeping layer while preserving the all-or-nothing invariant, quarantine/rehab flow (r10 #2, r11 #4/#5), and scope/salt semantics — medium effort/risk is the floor. The T1 regroup captures most of the credits win at a fraction of the risk; revisit only if regrouped batches still show material unchanged-member re-execution.

### T4.4 [low · small · low] #17b byte-aware st.cache_data eviction — keep deferred; low real memory pressure
`app/core/query.py:201`

st.cache_data is count+TTL only (no byte ceiling); entries are bounded by max_entries 256/512/512/128/512 solo + 64 tuples per batch fetcher and TTLs 30-14400s, mostly small aggregate frames; largest single exposure is the 10-member x 10k-row security export pack (security.py:365-368). No OOM/restart evidence anywhere in the repo. Act only on an observed SiS memory symptom. (The CSV-blob retention half of old #17 was upgraded and moved to T1.)

### T4.5 [low · small · low] #19 loader freshness MAX()/COUNT(*) bookkeeping — closed as declined; metadata-only non-win
`snowflake/migrations/V060__family_elapsed_queued_alert_guard.sql:521`

Re-verified twice through the latest version of every loader proc (MARTS_V27 HOURLY/DAILY, QH extract, OPS_DIAG, hourly facts): unfiltered SELECT MAX(ts), COUNT(*) resolves from micro-partition metadata, runs inside scheduled procs during the loader window, never on a render path — no user-visible latency, ~nil credits. Purge side-check clean (monthly, range-predicate DELETEs, partition-pruned; body now has 25 DELETEs not 22 — immaterial). Touch only as an incidental rewrite if a loader migration rewrites these blocks anyway.

### T4.6 [low · small · low] Overview triple USD conversion of the 150d spend frame — verified negligible; opportunistic cleanup only
`app/ui/pages/overview.py:82`

Four copy+map sites (:82-85, :112-114, :177-179, :378-380) off one ~150-row frame — microseconds-to-low-ms each, and st.cache_data hands back a fresh copy per call so there is no cached-frame mutation risk. Close the 'proj_daily frame copy cost' question as not-a-win; consolidate to a single usd_daily only if overview.py is opened for other reasons (it will be, for the T2 batch — fold it in then).

### T4.7 [low · small · low] Exec-board hourly full-history rebuild — no action; verified cheap
`snowflake/migrations/V054:39`

Current SP_REFRESH_EXEC_BOARD reads only three LOCAL daily-grain fact tables, each pre-aggregated once at -365d, into a ~3.5k-row stage + SWAP — zero ACCOUNT_USAGE reads. Only redundancy is recomputing 180/365 windows hourly when they change daily: pennies, and splitting adds a second code path plus stage/SWAP staleness edges. Re-open condition: V058 MART_TASK_NODE_DAILY showing sibling contention with TASK_QH_EXTRACT.

### T4.8 [low · medium · low] OPS_DIAG 'overlap with marts arms' — non-issue; both scans are the local extract
`snowflake/migrations/V056__loader_reconcile_alert_fixes.sql:948`

SP_LOAD_OPS_DIAG reads OW_QH_EXTRACT exactly twice (top-elapsed :948-954, fail-family :972-974) and no ACCOUNT_USAGE at all — the extract IS the dedup staging. Fusing two shape-mismatched INSERTs (detail vs aggregate) into one INSERT ALL to save one pass over a ~2d local table is below the migration-overhead bar. Closed.


## Bugs found during verification (all verified; queued into the fix rounds)

1. CSV prep-blob collision is cross-PAGE, not just cross-section (extends item #17): _ow_dl_seq is a single global session key that EVERY page's page_header resets to 0 (components.py:125), and styled_table (components.py:665) has no key parameter at all, so all its prep keys collapse to ow_dlprep__N. A blob prepared on e.g. Operations table N is offered as '⬇ CSV ready' for Cost's table N after a page switch, and downloads under the current page's plausible filename (overwatch_table_{seq}.csv) with no mismatch signal. Any fix must fold PAGE identity plus content identity into the key, not just section.

2. Blob-orphan leak variant of #17: the seq counter is only consumed by tables with >=4 rows (components.py:634-636), so any toggle/filter that adds, removes, or shrinks an exportable table earlier on the page shifts seq for every later table. Previously prepared blobs then sit under keys that no longer match any rendered table — never served, never deleted — so session_state retention can grow beyond the 'explicit prepare clicks' bound in long sessions with layout churn. Same one-slot/eviction fix covers it.

3. alerts.py:479 — the 'Open total' KPI help text says 'Counts the 300 most recent open events — the feed cap', but open_alert_events (mart_sql.py:333) orders by severity rank FIRST, then RAISED_AT DESC, before LIMIT. When more than 300 events are open, the cap keeps the most SEVERE (old CRITICALs beat new LOWs), not the most recent — the caption misdescribes the truncation semantics. It also hardcodes '300', which goes stale if the LIMIT-standardization item lands.

4. control_room.py:116-138 — the `if _b_recent is not None and _b_hist is not None:` guard and its entire serial else-branch in _day_replay are dead code: run_batch has ALWAYS returned a dict since v4.20 (query.py run_batch docstring: 'ALWAYS returns {key: QueryResult} with every key present'; every return path returns a dict). The else-branch can never execute, and the guard misleads readers into thinking a None-fallback contract still exists. Behavior is correct today (failed members arrive as ok=False results and each is checked via usable()), but ~19 lines of unreachable fallback should be deleted or the guard rewritten.

5. security.py:363-368 (_export_pack) — the batch fallback comment says 'any batch failure falls back to the original serial per-sheet path with its own caching', and the code guards with `(_pack_batch or {}).get(name)`, but run_batch never returns None/falsy (it returns a dict with every key present, ok=False on failure). A failed sheet therefore yields an ok=False QueryResult from the batch dict, which is truthy, so the `or run(...)` per-sheet fallback never fires and the sheet's CSV is written from the ERROR frame instead of retrying serially. Same stale-contract pattern as the control_room dead fallback, but here it changes output: a transient batch-member failure puts an ERROR csv in the auditor pack where the documented intent was a serial retry.

6. MART_TASK_NODE_DAILY freshness is doubly untracked (correctness/observability, not perf): it is absent from the hourly SOURCE_FRESHNESS_STATE merge IN-list (snowflake/migrations/V060__family_elapsed_queued_alert_guard.sql:525-529) AND from the MART_SOURCE_FRESHNESS view feeding that merge (latest definition is V045__task_monitoring_restored.sql:2074, which predates V058 and has no MART_TASK_NODE_DAILY arm). Once the V058 deployment queue lands, the mart can silently stop loading and neither the health-strip STALEST arm, the STALE_SOURCES count, nor the platform score will ever flag it. The V061 fix needs BOTH a view arm and the merge-list entry.

7. Fragile hardcode in the steering lever math (app/ui/pages/cost_parts/contract.py:318-320): the mart-shape branch computes the pattern lever as `sum(CREDITS) / 30 * 30 * rate` — a numeric no-op that is only correct because the window is hardcoded to 30 at the call site (mart27_sql.pattern_cost(30, ...), line 302). If that window parameter ever changes, the monthly-dollar lever silently scales wrong instead of failing. Not currently wrong, but a correctness trap: derive the divisor from the actual window argument.

8. object_run_history PROCEDURE matching conflates procedures whose short name is a suffix of another's (C:/Users/jfree/Documents/GitHub/OVERWATCH_NEW/app/data/change_impact_sql.py:77-79): the match is POSITION('<SHORT>(' IN REPLACE(REPLACE(UPPER(QUERY_TEXT),' ',''),CHR(10),'')) with no left word boundary, so drilling a proc named e.g. BOARD also counts every CALL of SP_REFRESH_EXEC_BOARD ('...EXEC_BOARD(' contains 'BOARD('), and any X vs PREFIX_X pair mixes run histories in the Change-impact drill chart. A left-boundary check (preceding char in [.(,;)CALL-start] on the normalized text, or matching '.<SHORT>(' | 'CALL<SHORT>(') would fix it.

9. Stale comment mechanism for the Security Changes tab (context for the fix, verified via git): the r23 #4 batch was added in 597915b (v4.38.0) pairing ddl+login_reasons, then removed in 922a4c6 (v4.49.0) as a side effect of moving login_reasons/unused_roles to the Access tab — the comment at app/ui/pages/security.py:467-469 was left behind and now misdocuments the code. Any future perf pass reading comments as evidence would re-trust it; item 3's fix should delete or restore it either way.

10. Fragile locals() coupling in app/ui/pages/operations.py:483: _failure_timeline_section receives known_failures via locals().get('_known_failed'), where _known_failed is only assigned inside the `if failed_col:` branch at 465-469. Any refactor of the KPI block silently turns the r19 #6 skip-scan optimization off (fail-safe — the 7d TASK_HISTORY scan just runs again — so perf regression, not wrong data), and no test guards it.

11. Reconcile boundary-hour clobber (correctness, small, permanent): SP_NIGHTLY_RECONCILE deletes FACT_QUERY_ROLE_HOURLY/FACT_QUERY_SCHEMA_HOURLY only for HOUR_TS >= CURRENT_TIMESTAMP-3d (V056:889-892), but MARTS_V27('HOURLY',3) arms [3]/[4] (V060:174-239) source the extract, whose floor is exactly CURRENT_TIMESTAMP-3d (V056:58-64, 77-79). The hour bucket containing that floor (typically the 06:00 hour of D-3) is NOT deleted (its truncated HOUR_TS is before the cutoff) yet the MERGE WHEN MATCHED overwrites its previously-complete row with an aggregate built from only the tail of that hour (~14 of 60 minutes at a 06:46 run). Later hourly runs (d=2, midnight D-2 window) never reach back to re-correct it, so one hour per day in both hourly facts is permanently understated. Fix shape: exclude the partial boundary hour from the d=3 MERGE source (WHERE DATE_TRUNC('hour', START_TIME) > the extract floor's hour) or delete/refill on hour-aligned boundaries. Files: C:\Users\jfree\Documents\GitHub\OVERWATCH_NEW\snowflake\migrations\V056__loader_reconcile_alert_fixes.sql, V060__family_elapsed_queued_alert_guard.sql.

12. Extract long-runner staleness (honesty of FAIL/elapsed in extract-fed marts): SP_LOAD_QH_EXTRACT's incremental window is watermark-45min (V056:58-64), sized for ACCOUNT_USAGE lag only. A query whose runtime exceeds ~60-105 min is captured while RUNNING and its row is never re-covered by later hourly fills (its START_TIME falls before the next lo), so its final status, TOTAL_ELAPSED_TIME, and spill stay frozen in OW_QH_EXTRACT until the nightly 3d refill — every extract-fed mart (qfam, role, schema, tagcov, alloc, ops-diag, timeline DDL) under-reports such queries for up to a day. Pre-existing and healed nightly, but worth an explicit design note (or a second re-cover pass for rows with non-terminal status) before arm [1] joins the extract consumers, since MART_WAREHOUSE_EFFICIENCY_DAILY's FAILS/P95/EXEC_HOURS would inherit it. File: C:\Users\jfree\Documents\GitHub\OVERWATCH_NEW\snowflake\migrations\V056__loader_reconcile_alert_fixes.sql:56-95.

13. Stale comment (doc-only): V056:67 'The one QUERY_HISTORY scan of the hourly cycle' is false while arm [1] still scans raw QUERY_HISTORY hourly (V060:82) — item 1's fix already includes correcting it; if item 1 ships without the comment fix, fix the comment anyway. File: C:\Users\jfree\Documents\GitHub\OVERWATCH_NEW\snowflake\migrations\V056__loader_reconcile_alert_fixes.sql:67.


## Synthesis notes

Repo root for all relative paths: C:\\Users\\jfree\\Documents\\GitHub\\OVERWATCH_NEW. All items are CONFIRMED verdicts (Security Access is the one DOWNGRADED, kept at its honest low rating); nothing was REJECTED so nothing was dropped — declined/no-action items are recorded in T4 with reasons rather than silently omitted, per the round-1 discipline of keeping dispositions auditable. Duplicate findings were merged: the Security Changes stale-comment/serial item appeared in two verifier blocks (merged into one T1 item at the higher medium rating, which verified more — Egress tab, git history, historical-tier composition); deferred #12 appeared twice (merged as: T1 filter-dependency regroup adopted, full probe-layer split moved to T4 declined); the TASK_HISTORY [6b] item and the 4-scan temp-table item are one T3 entry; #19 was confirmed a non-win by two independent verifiers and closed once in T4. Ranking within tranches is by impact then breadth. T1 ordering note: items 1-2 (tier adoption) should land before item 3 (Changes-tab batch) so the batch is built at the final tier. T2's two high-impact items (Control Room, Overview) are the headline user-visible wins of this round — both pages' first paint drops from 8-16 serial round trips to ~3. The health_strip retier is small-effort but sits in T2 because it needs an explicit product sign-off (cross-viewer ack/close and new-critical visibility extend to up to 5 min; per-session salts keep the acting operator instant). T3 is one V061 migration: items 1-3 bundle naturally (item 4 is an optional rider with coupled DELETE semantics — read its caveats before including). V061 MUST also carry two correctness fixes from the bugs list: the MART_TASK_NODE_DAILY double-untracked freshness (view arm + merge-list entry, urgent before the V058 deploy queue lands) and ideally the reconcile boundary-hour clobber. The extract long-runner staleness note should be resolved (design note or re-cover pass) before T3 item 1 ships, since arm [1] would extend that semantics to MART_WAREHOUSE_EFFICIENCY_DAILY. One citation discrepancy between verifiers: block 2 stated 'unit_costs.py no longer exists' while blocks 1 and 4 verified unit_costs.py:53-64 in the current tree — the live file is app/ui/pages/cost_parts/unit_costs.py; block 2's remark referred to a stale pattern citation, and the correct preloaded= pattern references are overview.py:75-77 and cost.py:65-79. House constraints respected throughout: no item adds a live ACCOUNT_USAGE scan to a hot page (tests/test_perf_budgets.py), tier moves note the two test locks that pin tiers (test_perf_pass.py:177, test_codex_r14.py:44-50), and all round-1 done/declined dispositions were honored (nothing re-scopes #10, #13, or the shipped #9/#15/V057-V060 work; T3 schedule changes A/B/C remain parked in T4 awaiting V058 data).
