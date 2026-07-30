# OVERWATCH Recs Review — 2026-07-31

Tech-lead synthesis of the adversarial verification of 20 Codex recommendations against v4.88.0. Verdicts are treated as verified input; this document sequences and dispositions them.

## 1. Verdict table

| ID | One-line | Verdict | Sev | Effort |
|---|---|---|---|---|
| A-score-1 | Overview critical/high **and platform score** read the 500-cap alert feed, not the uncapped aggregate | CONFIRMED | critical | S |
| rec5 | Exec downloads rebuilt from raw score fields — re-introduce `0/100 (Incomplete)`, account-wide scope, false blanket footer | CONFIRMED | high | S |
| A-score-2 | Only throughput+alerts fail closed; task/freshness/actions/budget read-failures become silent 0 penalties | PARTIAL_ALREADY_BUILT | high | M |
| rec4 | Warehouse dollar pool (clamped 182d, today-excluded) vs allocation shares (365d, today-included) diverge | CONFIRMED | high | M |
| 20 | Contract runway: Brief + COST_CONTRACT_BREACH still divide 31-date partial-day sum by literal 30 | PARTIAL_ALREADY_BUILT | high | M |
| A-score-3 | "Fixed 24h" score window is actually midnight-aligned 24–48h; task facts are day-grain | CONFIRMED | medium | S |
| rec12 | User/DB attribution renders redundant waterfall + bar of same top-10 | CONFIRMED | medium | S |
| rec11 | No per-section applied-scope line; status bar shows only company+days | CONFIRMED | medium | M |
| rec13 | Single card badge slot collides scope + freshness; method only in captions; not exported | CONFIRMED | medium | M |
| rec15 | a11y: ~10px labels, hover-only help, non-wrapping segmented controls, muted contrast <4.5:1 | PARTIAL_ALREADY_BUILT | medium | M |
| 7 | Shared DAILY_FACTS watermark: one broken sibling forces re-scan of costly METERING view every run | CONFIRMED | medium | M |
| 8 | Webhook drain newest-first — sustained volume indefinitely starves oldest (most-aged) criticals | CONFIRMED | medium | M |
| 19 | No route-level pre-boundary backlog metrics (eligible, queued bytes, oldest age, batches, drain ETA) | CONFIRMED | medium | M |
| 18 | Telemetry statistically biased (2% healthy, 100% slow/fail); sample-prob + query-id not persisted | CONFIRMED | medium | M |
| 9 | Overview batching unfinished — only the 2 live reads batched | PARTIAL_ALREADY_BUILT | medium | S |
| 16 | metric_registry is descriptive, not an executable CI-enforced contract | PARTIAL_ALREADY_BUILT | medium | L |
| 10 | task-node + score reads sit on 5-min `recent` TTL while sources refresh hourly/daily | CONFIRMED | low | S |
| 14 | Flat sidebar; no Watch/Analyze/Govern grouping | CONFIRMED | low | M |
| rec17 | No unified basis-aware cost coverage ladder across grains | PARTIAL_ALREADY_BUILT | low | L |
| 6 | Build the isolated V064 T3 loader-perf bundle | **DECLINE** | low | L |

## 2. Residuals of THIS session's work (lead — highest integrity)

These are direct gaps in code shipped v4.82–v4.88. We introduced them; we own the finish. Ordered by integrity cost.

**A-score-1 (CONFIRMED, critical, S) — the score inflates exactly when it must not.** C4+C7 added `open_alert_severity_counts` (the uncapped `COUNT_IF` aggregate) but wired it into the **Alerts page KPI only**. Overview still runs `mart_sql.open_alert_events(500, company)` in `overview.py` `_open_alert_counts` (135–146), and those capped counts feed **both** the "Open critical/high" KPI (398) **and the platform score** (357–358). A >500 open+ack storm undercounts criticals into the score, inflating it during the worst incident. Overview never renders an alert *list* — the 500 rows exist only to be counted — so the fix is a net simplification: replace the `open_alerts_{company}` batch leg with `open_alert_severity_counts(company)`, read CRIT/HIGH columns directly, and thread the `QueryResult` through to preserve `.ok/.error` KPI+score gating. **App-only. This is C4+C7 finished on the surface where it matters most.**

**A-score-2 (PARTIAL, high, M) — C1's fail-closed principle applied to 4 of 6 sources.** C1 shipped the Incomplete scaffold and `REQUIRED_SIGNAL_SOURCES={throughput,alerts}`. Task, freshness (`_hs`), actions, and budget/MTD read-failures still fall through as silent 0 penalties (`overview.py` 300–306, 328–332, 336–340; 222/356) — the same cardinal sin C1 was built to kill. **Do not key on `.usable()`**: key on `QueryResult.error_kind`. Add a source to the required/failed set only when `ok==False AND error_kind!='absent'`, so genuine outages fail closed while legitimately-uninstalled marts stay zero-penalty (preserving the partial-deployment design). Also add `task` and `budget` to the tracked set. **App-only.**

**20 (PARTIAL, high, M) — N11 fixed one of ≥3 runway sites; Brief + the alert still lie.** `contract.py` (320–332) correctly averages complete days excluding today. But `mart_sql.contract_exhaustion()` (698–716) still does `SUM(CREDITS_BILLED)/30 WHERE DAY >= today-30` — a **31-date span including today's partial metering divided by a literal 30**, biasing burn low → overstating days-left → **can suppress COST_CONTRACT_BREACH**. `brief.py` 60/109 consume it; the identical block lives in the V062 alert (now in `SP_ALERT_SCAN_DAILY`). Fix: one canonical trailing-30-**complete**-days burn in the mart (`BETWEEN today-30 AND today-1`, divide by actual complete-day count), mirror it in the migration block, point Brief + alert at it. **App-only for the mart; owner-migration for the alert block.**

**7 (CONFIRMED, medium, M) — V063 B34's shared watermark hold has collateral cost.** `SP_LOAD_DAILY_FACTS` (V063, 186–359) holds one `SOURCE='DAILY_FACTS'` mark; on any sibling failure (`failed_any`) all four sources re-read from the held window next run, re-MERGE-ing the costliest read (`METERING_DAILY_HISTORY`, which can never set `failed_any`). Split into per-source watermarks (`FACT_METERING/TASK/LOGIN/STORAGE_DAILY`); advance each in its own success path. Loads are idempotent, so this is wasted ACCOUNT_USAGE compute on a bounded window, not corruption — refines V063's self-heal, doesn't reverse it. **Owner-migration.**

**8 (CONFIRMED, medium, M) — V063 B9 preserved the pre-existing newest-first ordering.** `SP_NOTIFY_WEBHOOK` (V063, 70–82) fits `RAISED_AT DESC` into a 3000-char budget, one message/route/run. Under sustained volume the newest events always occupy the budget; the oldest never fit and eventually cross 24h → `undelivered_expired` — backwards from urgency. Fix: order the fitting-set `RAISED_AT ASC`, allow N batches/route/run, and factor **one** shared eligibility predicate used by both send-selection and expired-detection (currently written separately, so "flagged expired" ≠ "was eligible-but-unfit"). Keep the capture-once frozen-ARRAY mechanic. **Owner-migration. Sequence with rec19.**

**13 (CONFIRMED, medium, M) — C11 crammed 'account-wide' into the freshness slot.** `metric_card_html` (components.py 202–226) has one badge field bucketed mart|live|stale; C11's scope token competes with freshness so a card shows one or the other, never both. Give the card dict distinct `method`/`scope`/`freshness` keys, each its own CSS token, and thread them into `exec_summary_html`. **App-only. Pairs with rec5 + rec11 (one token source of truth).**

**A-score-3 (CONFIRMED, medium, S) and 10 (CONFIRMED, low, S)** are the cheap residuals of C2/N5/C18: relabel the "fixed 24h" strings (overview.py 293/302/412–419) to "previous + current calendar day" and note the day-grain task leg; flip the four score/task-node reads from `recent` to the existing `hourly` tier. Both trivial, both app-only, both bundle with the score work.

## 3. Prioritized sequence

Codex proposed 4,1,2,5,3 then 6–10. My reordering leads with the **critical score-inverter** (Codex buried it behind rec4) and clusters owner-migrations into one V065 to shrink review surface.

### DO-FIRST (ship next cycle — integrity of the headline numbers)
1. **A-score-1** — critical, S, app-only. Best value/effort on the board; stops score inflation during storms.
2. **rec5** — high, S, app-only. Exports re-introduce the `0/100` C1 hid and carry a footer that's false for window-spend. Cheap honesty.
3. **A-score-2** — high, M, app-only. Finishes C1's fail-closed for the other 4 penalty sources (error_kind-keyed).
4. **20** — high, M, app + owner block. Suppressible breach alert; Brief contradicts the page.
5. **rec4** — high, M, app-only. Per-entity dollar mis-attribution, maximal at 365d.

Bundle the two S-effort labeling/config fixes **A-score-3** and **10** into step 1's PR — they touch the same score reads.

### NEXT (medium; cluster the owner-migration into one V065)
- **8 + 19 + 7** → **one V065** (8/7 are SP edits; 19 is the app-side observability that shares rec8's eligibility predicate). Doing them together lets 8 and 19 share the predicate as designed and gives the owner one migration to review, not three.
- **rec13 → rec11** (shared scope-token source) — app-only, sequence after rec5 so exports/section-line/cards read one contract.
- **rec12** — S, app-only; delete the redundant waterfall, add an explicit "Other / not shown" row.
- **9** — S, app-only; batch only `_thr`+`_tk` (both `recent`, both company-scoped). **Decline the board+150d and health legs** — batching them fights the deliberate filter-vs-fixed cache-key and shell-share design.
- **18** — M, owner-migration (2-column schema add to `APP_QUERY_TELEMETRY`); persist `SAMPLE_PROB` + `QUERY_ID` so percentiles are re-weightable and joinable to Query History.
- **rec15** — M, app-only; a11y floor + contrast + accessible popover + wrapping controls.

### BACKLOG (defer — large or low-correctness)
- **16** (executable metric registry, L) — real value but architectural; sequence *after* rec20 lands, and see my rec #3 below for the shippable subset.
- **14** (sidebar grouping, M) — pure UX, zero correctness.
- **rec17** (coverage ladder, L) — honesty primitives already exist per-grain; if done, scope to a single read-only Spend "Coverage" expander, not new marts.

### DECLINE
- **6** (V064 T3) — see §4.

**App-only:** A-score-1, A-score-2, A-score-3, rec4, rec5, rec11, rec12, rec13, rec15, 9, 10, 14, 16, 19, rec17.
**Owner-migration:** 7, 8, 18, 20 (alert block only), 6.

## 4. Rec 6 (V064 T3) — the owner deferred it one turn ago

**Reaffirm DECLINE.** Codex re-proposes the exact bundle the owner just parked and adds **no new justification**: no fresh perf measurement, no change to the corruption-risk calculus, no resolution of the OW_QH_EXTRACT staleness precondition (scope doc line 190) that must close before arm [1] joins the extract consumers. The stated wins are single-digit seconds/hour (T3.2 "~5–15s/hour", T3.3 "~1–3s/hour") against T3.1's `d<=2` gate — recorded in CHANGELOG as "the single highest-corruption-risk edit — a wrong gate would corrupt loads." Value/risk is precisely why it was deferred; nothing changed in one turn. Codex's proposed row-parity + task-node-timing tests are the right guardrails **if/when** this is built, but they don't move the disposition. **Hold until either the win grows or the extract-staleness note is closed.**

## 5. My additional recommendations (Codex missed)

1. **Parity regression test for the score's alert-count source.** A-score-1 is the *second* time this feed diverged between Overview and Alerts. Add a lock-test asserting the platform-score critical/high inputs resolve through `open_alert_severity_counts` (the same source as the Alerts KPI) so this residual cannot silently regress a third time. Test-coverage gap, not just a code fix.

2. **Kill the self-referential drift comment in `contract_exhaustion`.** Its docstring literally says "Same math as the COST_CONTRACT_BREACH scan block" — that comment *is* the drift signal, and it's already stale (the page diverged at N11). When fixing rec20, extract the burn-window date bounds into one named definition referenced by both the mart function and the migration block, and delete the "same math as…" prose. Codex framed 20 as consolidation but didn't flag the docstring as the canary.

3. **Ship "registry-lite" before the full rec16.** rec4, rec5, rec11, rec13, A-score-3 all stem from one root: window/scope/method provenance computed ad hoc per surface. Extract a `resolve_effective_window(days)` (returns clamped half-open `[start, CURRENT_DATE())` + resolved-days metadata) and a scope/method token helper, consumed by the spend pool, the share denominator, the exec export, and the section line. This is the concrete, high-ROI subset of the L-effort executable registry that pays for itself immediately — do it as part of the rec4/rec11/rec13 cluster rather than waiting on the full CI-enforced registry.

4. **Telemetry: also record the sample *stream*, deterministic sampler, retention.** Beyond Codex's `SAMPLE_PROB`+`QUERY_ID` (18), persist a `STREAM`/`REASON` enum (exception-path=1.0 vs sampled-healthy=0.02) so downstream can segment cleanly, add a rollup/retention so the always-on exception stream doesn't dominate storage, and inject the sampler (`random.random()` at query.py:82 is unseeded → flaky tests). Extend the `_ow_qtel_oldshape` downgrade path for the two new columns.

5. **Verify C3 "freshness never-loaded" doesn't collide with A-score-2's absent-vs-outage logic.** C3 shipped never-loaded handling and A-score-2 introduces `error_kind=='absent'` → zero-penalty. Before shipping A-score-2, confirm a *never-loaded* mart and an *absent* mart resolve consistently — otherwise a freshly-provisioned deployment could oscillate between Incomplete and a falsely-healthy score. Add a golden test matrix per penalty source: `{timeout, unknown_function, other}` → Incomplete; `absent`/never-loaded → zero-penalty.

## 6. Single highest-value and highest-risk

**Highest-value: A-score-1.** Critical severity at S effort — the only critical on the board, cheapest fix, and it inverts the product's headline number in the exact condition (a >500-alert storm) where a trustworthy score matters most. It's also a net simplification (deletes a 500-row fetch the page discards). Runner-up is rec20 (a suppressible breach alert), but A-score-1 wins on severity × cost.

**Highest-risk: Rec 6 (V064 T3).** Its T3.1 `d<=2` gate is explicitly rated the single highest-corruption-risk edit in the codebase ("a wrong gate would corrupt loads"), against single-digit-seconds/hour of upside, with an unresolved extract-staleness precondition. **Recommendation: DECLINE / hold** — the correct handling of the highest-risk item this cycle is to not build it.



---

## 7. Per-rec evidence (verification detail)


### A-score-1 — Overview derives critical/high counts AND the platform score from the 500-cap open_alert_events feed instead of the uncapped severity aggregate

**CONFIRMED** · critical · S

**Current state:** app/ui/pages/overview.py _open_alert_counts (lines 135-146) runs mart_sql.open_alert_events(500, company) and counts SEVERITY=='CRITICAL'/'HIGH' from that capped df. The first-paint run_batch (lines 239-244) fetches the same open_alert_events(500) under key open_alerts_{company}; lines 245-246 feed it to _open_alert_counts. The resulting critical_alerts/high_alerts flow into BOTH the platform score (lines 357-358) and the 'Open critical / high alerts' KPI (line 398). The uncapped aggregate mart_sql.open_alert_severity_counts (mart_sql.py:365-382, COUNT_IF over the same STATUS/company predicate) exists and is used on alerts.py:482 but NOT on Overview. open_alert_events caps at LIMIT max 1000 (line 351, called with 500), and its own docstring at 366-368 says the 500-row feed undercounts 'when a storm exceeds the 500-row feed cap'.

**Assessment:** AGREE, and the fix is cleaner than the rec frames it. Overview never renders an alert detail LIST: grep confirms alerts_res is used only at 245-246 (counts), 349 (_available 'alerts'), and 398-402 (KPI ok/error) plus the summary lines 594/606. So the 500 rows are fetched purely to be counted. Fix: replace the open_alerts_{company} batch leg + _open_alert_counts with open_alert_severity_counts(company) and read the CRIT/HIGH columns directly. No detail-row fetch is needed on Overview at all; this fixes the undercount AND stops fetching 500 rows the page discards. Preserve the existing alerts_res.ok/.error KPI/score gating by threading the counts QueryResult through in place of the events one.

**Relation to shipped work:** Direct residual of this session's C4+C7. C4+C7 added open_alert_severity_counts and fixed the undercount on the Alerts page KPI tiles only; Overview's KPI and (worse) the platform-score critical/high inputs were left on the capped 500-row feed. A >500 open+ack storm would undercount criticals feeding the score, inflating it exactly when things are worst.


### A-score-2 — Only throughput+alerts fail closed; failed task/freshness/action-queue/MTD score reads silently become 0 penalties

**PARTIAL_ALREADY_BUILT** · high · M

**Current state:** scoring.py:69 REQUIRED_SIGNAL_SOURCES = frozenset({'throughput','alerts'}); platform_score (89-93) returns Incomplete only when those two are missing from `available`. In overview.py the _available set (346-354) adds 'throughput' if _thr.usable(), 'alerts' if alerts_res.ok, 'health' if _hs.ok, 'actions' if actions_res.ok. So health & actions are TRACKED but NOT required; task and budget/MTD are not tracked at all. Concretely: if _tk (fact_task_daily) fails, task_fail_pct=0 silently (300-306, no _available entry); if _hs (health_strip) fails, stale_sources=0 (328-332) but score still computes; if actions_res fails, open_high_actions=0 (336-340); if _bt_hist (fact_daily_spend 150) fails, mtd_spend=0 -> budget_pct=0 (222, 356). Each is the same cardinal sin C1 was fixing: an outage that suppresses a real failure/staleness/over-budget signal raises the score.

**Assessment:** AGREE the gap is real for 4 of 6 penalty sources, but implement it keyed on QueryResult.error_kind, NOT on .usable(). result.py:23-27 classifies errors as absent|unknown_function|timeout|other; many deployments legitimately lack task/freshness marts (error_kind=='absent'), and gating those to Incomplete would make every partial deployment permanently Incomplete, which fights the 'score works on partial deployments' design. Correct rec: for each penalty-bearing source, if the read is ok==False AND error_kind!='absent' (a genuine outage, not 'not installed'), add it to a required/failed set and return Incomplete. That extends C1's fail-closed principle to task/freshness/actions/budget while keeping absent marts as legit zeros. Also add 'task' and 'budget' to the tracked set (currently absent entirely).

**Relation to shipped work:** Extension/completion of this session's C1 (platform_score Incomplete gating) and C2/N5 (REQUIRED_SIGNAL_SOURCES repointed board->throughput). The Incomplete scaffold, the available set, and 2 of the sources shipped; this finishes the job for the other 4 penalty inputs. Not a misread: the scaffold's existence is exactly why this is PARTIAL not CONFIRMED.


### A-score-3 — 'Fixed 24h' score-health window is actually midnight-aligned 24-48h (previous+current calendar day), and task facts are day-grain

**CONFIRMED** · medium · S

**Current state:** overview.py:66 comment concedes '1 = midnight-aligned 24-48h', but the runtime sources and user-facing help still say '24h'. fact_query_window_summary (mart_sql.py:54-85) with days=1 builds WHERE HOUR_TS >= DATEADD('day', -1, CURRENT_DATE()) i.e. from yesterday-midnight through now = 24h at midnight growing to ~48h by end of day. fact_task_daily (mart_sql.py:204-216) with days=1 does DAY >= DATEADD('day', -1, CURRENT_DATE()) = yesterday+today, and is DAILY grain so it cannot be sub-day windowed. The score labels these 'FACT_QUERY_HOURLY (fixed 24h health window)' (overview.py:293) and 'FACT_TASK_DAILY (fixed 24h health window)' (302), and the Platform-score KPI help says 'read a fixed 24h window' (412-419). Effect: fail_pct denominator and the cumulative queue/spill sums (thresholds 10 queued-min / 5 GB, scoring.py:124/130) see a window that swells from 24h to 48h across the day, so identical conditions trip different deductions by time of day.

**Assessment:** AGREE on the mislabel; NARROW the fix. The lowest-risk, correct action is to relabel to 'previous + current calendar day' in the two source strings (293,302) and the KPI help (412-419), and note the day-grain basis of the task leg. The rec's alternative (last 24 completed hourly buckets) is viable for the query leg but (a) cannot apply to fact_task_daily's daily grain without new hourly task facts, and (b) fights the DELIBERATE midnight-alignment chosen at mart_sql.py:67-69 to keep this span identical to the live twin ops_sql.query_window_summary / fact_warehouse_pressure tiles. So: relabel + optionally normalize the cumulative queue/spill thresholds to be rate-based (per-hour) if calibration matters; don't switch to rolling buckets. This is accuracy/calibration, not a score-inverting bug, hence medium not high.

**Relation to shipped work:** Residual of this session's C2/N5, which introduced these fixed-window reads and the '24h' labels (fact_query_window_summary(1)/fact_task_daily(1)) to stop the spend window from moving the score. The window is deliberately midnight-aligned; only the '24h' wording and the time-of-day threshold drift are unaddressed.


### rec4 — Warehouse dollar pool and allocation shares use divergent effective windows

**CONFIRMED** · high · M

**Current state:** spend.py `_attribution_tab` builds the dollar pool `window_usd` from `mart_sql.fact_warehouse_window_vs_prior(days, company)` and applies each entity's share to it: `alloc["ALLOCATED_USD"] = alloc["ELAPSED_SHARE"] * window_usd` (spend.py:283). The two inputs use DIFFERENT windows: (1) CLAMP — `fact_warehouse_window_vs_prior` does `days = bounded_days(days, MAX_MART_WINDOW_DAYS // 2)` i.e. 365->182 (mart_sql.py:231), while the share builder `mart27_sql.alloc_xdim_attribution` does `days = bounded_days(days, 400)` so 365 passes unclamped (mart27_sql.py:763). At the 365 window the pool covers 182d but shares are computed over 365d. (2) TODAY — the pool builder excludes today via `DAY < CURRENT_DATE()` (mart_sql.py:233,240-241), but the share builder includes it: `x.DAY >= DATEADD('day', -days, CURRENT_DATE())` with no upper bound (mart27_sql.py:769). The live twins diverge too: `cost_sql.warehouse_window_vs_prior` uses lag_offset_start + a `< NOW-24h` horizon and default clamp 90 (cost_sql.py:65-71), while `cost_sql.allocated_attribution` uses a raw `CURRENT_DATE()` anchor, today included, default clamp 90 (cost_sql.py:110,117). The total reconciles (share x pool <= pool) but per-entity shares represent a different time span than the pool, so an entity heavy in the older half of a 365d window gets an inflated slice of the 182d dollar pool.

**Assessment:** AGREE. Real, unaddressed accuracy defect, maximal at the 365d window and present (smaller) at every window via the today-inclusion asymmetry. Concrete fix: compute ONE half-open effective window `[start, CURRENT_DATE())` in Python from the requested `days` (clamped once to the tightest shared bound, 182), thread it as explicit start/end literals into BOTH the warehouse pool builder and every allocation-share denominator (mart + live paths), and surface the resolved window as metadata in the result_caption so a silently-narrowed 365->182 request is visible. Add a lock-test asserting the pool window literals equal the share window literals for a given `days`.

**Relation to shipped work:** Residual of this session's window-unification theme. C2+N5 repointed the SCORE throughput/pressure reads to a FIXED fact_query_window_summary(1)/fact_task_daily(1) window, but the SPEND attribution pool-vs-share window was never unified — same class of bug, different surface.


### rec12 — User/database attribution renders two redundant top-10 charts of the same values

**CONFIRMED** · medium · S

**Current state:** In `_attribution_tab`, for each of USER_NAME and DATABASE_NAME, the code renders a waterfall of the top 10 AND a bar of the same series: `if len(alloc) > 1: charts.waterfall_usd(alloc.head(10), "DIMENSION", "ALLOCATED_USD"); st.caption("Waterfall: top 10 contributors (allocated).")` immediately followed by `charts.bar_usd(alloc, "DIMENSION", "ALLOCATED_USD", ...)` (spend.py:287-290). Both plot the same ALLOCATED_USD; `bar_usd` itself defaults `top_n=10` (charts.py:143) and `waterfall_usd` defaults `top_n=10` (charts.py:323), so the two charts show the identical top-10 rows twice. The waterfall's cumulative form implies full reconciliation even though only the top 10 appear. Coverage IS already surfaced right after: `st.caption(f"Rows shown cover {shown:.0%} of scoped spend ({format_usd(shown * window_usd)} of {format_usd(window_usd)}).")` (spend.py:291-293).

**Assessment:** AGREE on the redundancy and the misleading waterfall; the 'coverage dollars' third ask is already built (the shown caption gives % + $ of $). Concrete fix: delete the waterfall block and keep ONE sorted horizontal `bar_usd`, then append an explicit 'Other / not shown' row = `(1 - shown) * window_usd` so the chart itself accounts for the uncovered remainder rather than only the caption. Small, contained to spend.py:287-293.

**Relation to shipped work:** Follow-up to C15 (this session touched charts._stable_color and a label size) but the double-chart attribution rendering was not part of that work — untouched residual.


### rec17 — No unified basis-aware cost coverage ladder across grains

**PARTIAL_ALREADY_BUILT** · low · L

**Current state:** The constituent grains and residual/coverage concepts all exist, but scattered across separate surfaces with no single ladder: (a) warehouse grain (exact) + allocated-user/db grain with a coverage % + $ caption live in spend.py `_attribution_tab`; (b) the OBJECT grain with a measured-query split and explicit residual arm is the Object cost ledger — `QUERY_COMPUTE_READ/WRITE/RESIDUAL` plus maintenance arms, `QUERY_COMPUTE_RESIDUAL = credits for queries that neither read nor wrote a base object` (optimize.py:379-396, cost_sql.object_cost_by_arm); (c) the additive-contract check `object_cost_recon` proves query arms + residual = QUERY_ATTRIBUTION_HISTORY credits (cost_sql.py:596-640, surfaced on admin.py:512); (d) rate-card reconciliation on Contract frames the billed-vs-model residual as storage/transfer/serverless/discounts (contract.py:153-163); (e) the Spend service breakdown carries the non-additive serverless/AI/storage/replication tracks (spend.py:34-55,105-130). What does NOT exist is a single view that stacks billed cost -> covered-at-warehouse -> covered-at-measured-query -> covered-at-allocated-user/db -> covered-at-object, each with residual + unknown, alongside separate non-additive idle/serverless/AI/storage/cloud-svcs tracks.

**Assessment:** NARROW. The honesty primitives (per-grain residuals, coverage %, additive-contract proof) are already built and shown in-context; the gap is purely a consolidated presentation ladder, which is large effort for a synthesis of numbers already exposed honestly elsewhere. Recommend deferring or scoping down to a single read-only 'Coverage' expander on the Spend page that reuses the existing object-ledger residual + attribution coverage % rather than building the full multi-track ladder + new marts the rec implies. Lowest ROI of the three.

**Relation to shipped work:** Extension/consolidation of prior work (V048-V050 object ledger, v4.52 object_cost_recon, the attribution coverage caption) rather than a residual bug from this session — none of the v4.82-4.88 items touched a coverage-ladder surface.


### rec5 — Executive downloads not built from the honest screen view-model (Incomplete score, account-wide scope, blanket footer)

**CONFIRMED** · high · S

**Current state:** app/ui/pages/overview.py:585-617 builds BOTH exports from raw score fields, not the rendered KPI view-model. (1) Score: line 595 f'Platform score: {score.score}/100 ({score.state})' and line 607 score_line=f'{score.score}/100 ({score.state})' -> an Incomplete score exports as '0/100 (Incomplete)', whereas the on-screen KPI (line 406) renders the WORD 'Incomplete' and suppresses the number. The export re-introduces the 0/100 that C1 hid. (2) Scope: exec_summary_html heading is company-scoped 'Executive summary -- {company}' (formulas.py:236) and MTD/Projected cards (formulas.py:240-241) carry NO scope note, yet those figures are account-wide -- on screen they wear the 'account-wide' badge (overview.py:386,392) + caption 427-428. (3) Basis footer formulas.py:249-250 is a blanket 'Numbers come from ACCOUNT_USAGE-derived facts with the cloud-services adjustment applied' -- but the on-screen Window-spend help (overview.py:379-381) explicitly says the cloud-services REBATE is NOT in that number. So the blanket footer is false for the window-spend line.

**Assessment:** AGREE, real and unaddressed. Small fix, all in overview.py + formulas.py string builders: pass score_line='Incomplete' (mirror the KPI) when state=='Incomplete'; label MTD/Projected cards as 'account-wide' in both HTML and .txt; split the footer so 'cloud-services adjustment applied' scopes to MTD/Projected only and window-spend is noted as compute-metering-at-rate (rebate excluded). The KPI help strings already hold effective-dates/coverage text to reuse.

**Relation to shipped work:** Direct residual of C1 (Incomplete gating) + C11 (account-wide badges). The SCREEN got honest this session; the download path at overview.py:585-617 and formulas.py:194-251 was never updated to match, so exports still lie where the screen no longer does.


### rec11 — Section-level applied-scope line missing; status bar shows only company+days, ignored filters unsurfaced

**CONFIRMED** · medium · M

**Current state:** app/main.py:402-428 _persistent_status_bar renders one 'Scope' stat = f"{_f['company']} . {_f['days']}d" (line 415) -- company + days only. The global filter model carries more dims: _scope_is_active (main.py:438-449) checks flt_warehouse_contains, flt_user_contains, flt_schema_contains, flt_environment. Those global chips can be set while a given panel's query ignores them (e.g. account-wide MTD/Projected on Overview ignore company AND every dim), yet nothing per-section states what was actually applied vs ignored. No effective-date range is shown anywhere in the bar either.

**Assessment:** AGREE the gap is real; NARROW the scope. A full per-section provenance line on every panel is M+ and risks caption clutter. Highest value: a compact section-level line on the mixed-scope surfaces (Overview KPIs, Cost pages) that names applied dims + 'account-wide vs company' + explicitly-ignored global filters. Best delivered together with rec13's per-card scope token so the data has one source of truth rather than hand-written per section.

**Relation to shipped work:** Extension of C11. C11 added an 'account-wide' badge to individual KPI cards but did not add a section/status-bar line reconciling which GLOBAL filters a panel honored vs ignored. The status bar (main.py:415) still predates C11 and shows only company+days.


### rec13 — Single card badge slot overloads scope + freshness; method (allocated vs billed) only in captions; badges not carried to exports

**CONFIRMED** · medium · M

**Current state:** app/ui/components.py:202-226 metric_card_html has ONE badge slot: item['badge'] (line 220), bucketed to mart|live|stale, and anything else -> '--other' (line 222). C11 pushes 'account-wide' through that same single field, so scope and freshness compete for one slot -- a card can show EITHER 'account-wide' OR 'live/mart/stale', never both. theme.py:73-77 confirms the CSS token set is mart/live/stale/other only. Method (allocated-vs-billed / credits x rate) lives in help/caption text (overview.py:377-381,387,427-428), not a card token. metric_card_html output is not consumed by the exporters, so no token reaches the HTML/.txt.

**Assessment:** AGREE. Give the card dict distinct optional keys -- method, scope, freshness -- each its own CSS token, rendered as up to 3 small chips, and thread them into exec_summary_html so a downloaded card states its own method/scope/freshness. M because it touches the card contract, theme tokens, every call site that sets 'badge', and the export builder. Pairs naturally with rec5 (exports) and rec11 (section scope line reads the same tokens).

**Relation to shipped work:** Residual of C11: C11 crammed 'account-wide' into the freshness badge slot rather than adding a scope token, creating exactly the mutually-exclusive collision this rec flags. Unaddressed this session.


### rec15 — a11y polish: ~10px status labels, title-hover-only KPI help, non-wrapping segmented controls, marginal muted contrast

**PARTIAL_ALREADY_BUILT** · medium · M

**Current state:** theme.py: .ow-stat__k is 0.62rem ~= 9.9px (line 98); .ow-card__title / stMetricLabel / .ow-src-badge / .ow-section__badge all ~0.70rem (lines 59,78,73,90). KPI help is title-hover only -- metric_card_html builds title_attr=f' title="{help_t}"' (components.py:216), no keyboard/touch popover. Section/Window segmented controls force horizontal scroll: div[role=radiogroup][aria-label=Section/Window] has overflow-x:auto; flex-wrap:nowrap (theme.py:132) rather than wrapping. Muted text --ow-ink-mute #6b7a90 on --ow-bg #0a0f1c computes ~4.35:1 -- under WCAG AA 4.5:1 for the small (9.9px/11px) labels that use it. A @media(max-width:640px) block DOES exist (theme.py:175-179) shrinking metric value + stat basis, and prefers-reduced-motion is handled (187).

**Assessment:** AGREE the listed defects are real but NARROW the value framing: this is polish on an internal DBA tool, so medium not high. Concrete remaining work: raise the label floor off 0.62/0.70rem, lift --ow-ink-mute to clear 4.5:1, replace title-hover help with an accessible popover (st.popover / aria), and let the segmented controls wrap instead of overflow-scroll. Mobile/reduced-motion groundwork already exists so scope is smaller than the rec implies.

**Relation to shipped work:** C15 shipped this session was deliberately narrow (charts _stable_color + one 10px->11px label). The four items above were out of that scope and remain; the existing @640 media query and reduced-motion rule mean the 'mobile menu' portion is partially covered, hence PARTIAL not CONFIRMED.


### 6 — Build the isolated V064 T3 loader-perf bundle (efficiency arm via OW_QH_EXTRACT, single TASK_HISTORY stage, shared WMH stage, drop reconcile double-load)

**DECLINE** · low · L

**Current state:** V064 does not exist (snowflake/migrations/ ends at V063; ls V064* returns nothing; task #49 is still 'pending'). The four candidate edits are T3.1-T3.4 in docs/reviews/PERF_ROUND_2_SCOPE_2026-07-29.md:100-121. Their own stated wins are marginal: T3.2 '~5-15s/hour saved; TASK_HISTORY is small, mostly fixed secure-view overhead'; T3.3 '~1-3s/hour'; T3.4 an 'optional rider with coupled DELETE semantics'. CHANGELOG.md:29-30 records the deferral verbatim: T3.1's d<=2 gate is 'the single highest-corruption-risk edit — a wrong gate would corrupt loads'. Scope doc line 190 also flags an unresolved precondition: the OW_QH_EXTRACT long-runner staleness (V056:56-95) needs a design note or re-cover pass BEFORE arm [1] joins the extract consumers, else MART_WAREHOUSE_EFFICIENCY_DAILY inherits under-reported FAILS/P95/EXEC_HOURS.

**Assessment:** DECLINE — respect the owner's deferral. Codex re-proposes the exact bundle just parked and adds no new justification: no fresh perf measurement, no change to the corruption-risk calculus, no resolution of the arm-[1]/extract-staleness precondition. Value/risk is why it was deferred: single-digit-seconds-per-hour against the one edit rated highest-corruption-risk, on a table the scope doc itself calls 'small, mostly fixed secure-view overhead'. Codex's proposed row-parity + task-node-timing tests are the right guardrails IF built, but don't change the honest disposition: hold until either the win grows or the extract-staleness note is closed. Reaffirm deferral.

**Relation to shipped work:** Direct residual of this session's V062->V063->V064 split. V063 header line 13 states 'T3.1-T3.4 perf-loader restructures are handled in V064 (isolated by risk)'. This rec asks to now build that isolated bundle — which the owner deferred as the last step of the shipped work.


### 7 — Split the shared DAILY_FACTS watermark into per-source watermarks so successful sources stop repeatedly reloading when one sibling stays broken

**CONFIRMED** · medium · M

**Current state:** Confirmed in V063__webhook_capture_once_daily_facts_failguard.sql SP_LOAD_DAILY_FACTS (lines 186-359). There is ONE watermark row: line 199-200 SELECT MAX(WM_TS) ... WHERE SOURCE='DAILY_FACTS'; lo_metering/lo_short are both derived from that single wm (lines 201-206). failed_any (line 197) is set TRUE by any of the three wrapped tables failing (task 260, login 288, storage 315). On failure the shared advance is skipped (IF NOT failed_any at 324-330). So one broken sibling holds the mark, and the next run re-reads ALL four sources from the same held window: the unguarded FACT_METERING_DAILY MERGE from lo_metering (-5d, line 207-229) plus the three DELETE+INSERT wraps from lo_short (-3d). The METERING re-MERGE scans SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY — the most expensive read — and it can never set failed_any, so it re-scans every run a sibling is down.

**Assessment:** Agree, with calibrated severity. Exact fix: replace the single OW_LOAD_WATERMARKS SOURCE='DAILY_FACTS' row with one row per source ('FACT_METERING_DAILY','FACT_TASK_DAILY','FACT_LOGIN_DAILY','FACT_STORAGE_DAILY'); read each source's own wm to derive its own lo_* overlap window; advance each source's mark inside its own success path (metering unconditionally, each wrapped table only if its wrap didn't fail); derive aggregate task health separately from the per-source marks. Effect: 3 healthy sources advance and stop re-scanning; the broken one holds and self-heals — preserving V063's heal intent without the collateral re-scan. Severity medium not high: loads are idempotent (MERGE / DELETE+INSERT) so this is wasted ACCOUNT_USAGE compute + a frozen read window, not data corruption or staleness, and the window is bounded (held-mark minus fixed overlap, not growing). But the re-scanned METERING view is the loader's costliest read and it repeats every run until the sibling heals, so it's worth doing.

**Relation to shipped work:** Direct follow-up to the V063 B34 fail-guard shipped this session (failed_any + IF NOT failed_any watermark hold). Codex correctly identifies the collateral cost of that deliberate hold; the fix refines rather than reverses it — same self-heal, per-source granularity.


### 8 — B9 webhook drain is newest-first (RAISED_AT DESC, <=3000 chars) — sustained volume indefinitely starves older events; send oldest-first, cap messages per route/run, share one eligibility predicate

**CONFIRMED** · medium · M

**Current state:** Confirmed in V063 SP_NOTIFY_WEBHOOK (lines 30-183). The fitting-set ARRAY is built newest-first: ARRAY_AGG(...) WITHIN GROUP (ORDER BY f.RAISED_AT DESC, f.EVENT_ID) at line 70-71, and the cumulative-length window is ORDER BY e.RAISED_AT DESC ... ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW at lines 81-82, keeping rows where CUM_LEN <= 3000 (line 94). Only ONE message per route per run is sent (single SYSTEM$SEND at 119-122). So when eligible volume exceeds the ~3000-char budget, the newest events always occupy the budget and the oldest never fit; as new events keep arriving the oldest are pushed further out until they cross the 24h window (line 86 predicate) and get flagged undelivered_expired (lines 159-178) — exactly backwards from urgency. The send-eligibility predicate (lines 85-92) and the expired-detection predicate (lines 161-170) are also written separately, so what is flagged expired is not provably the same set that was eligible-but-unfit.

**Assessment:** Agree. Exact fix: (1) order the fitting-set oldest-first — ARRAY_AGG and the CUM_LEN window both ORDER BY RAISED_AT ASC, EVENT_ID — so events nearest the 24h boundary drain first; (2) allow multiple messages per route per run (loop the fit/send/ledger up to a per-route cap, e.g. N batches) so throughput can exceed one 3000-char message/run and keep pace with sustained volume; (3) factor the eligibility test (OPEN, RAISED_AT within 24h, family/company/severity match, NOT delivered on this route) into ONE reusable predicate shared by both the send selection and the expired-detection, so 'flagged expired' is exactly 'was eligible and still undelivered'. Keep the capture-once frozen-ARRAY mechanic (message/ledger/NOTIFIED_AT from one set) — only the ordering, batching, and predicate-sharing change. Severity medium: it starves the oldest, i.e. the most-aged criticals, but the expired-flagging still surfaces them loudly, so it degrades timeliness rather than dropping events silently.

**Relation to shipped work:** Direct follow-up to this session's V063 B9 capture-once rework. The frozen-ARRAY fix solved the send-vs-ledger race but preserved the pre-existing newest-first ordering; Codex catches that the ordering is the residual starvation bug B9 didn't touch.


### 19 — Add route-level delivery backlog metrics (eligible, queued escaped bytes, oldest age, batches required, success, retries, expired, est drain time) to detect starvation before the 24h boundary

**CONFIRMED** · medium · M

**Current state:** Confirmed gap in app/data/mart_sql.py. delivery_slo_summary (lines 1107-1137) is ACCOUNT-WIDE aggregate only: EVENTS_RAISED, EVENTS_DELIVERED, MEDIAN_MIN, P95_MIN, UNDELIVERED_CRITICALS_30M (critical-only, 30-min, not per route, not tied to the 24h webhook boundary), and ROUTE_FAILURES (a count of route_send_failed error rows over the window — no per-route breakdown). delivery_by_route (lines 1140-1150) IS per-route but reports only DELIVERED counts: SENDS, distinct EVENTS, LAST_SENT — it reads ALERT_DELIVERIES (what left), never the eligible-but-undelivered backlog. Both are surfaced on the alerts page (alerts.py:635 delivery_slo, :655 delivery_routes). Nothing computes, per route: eligible-undelivered count, cumulative escaped-JSON bytes queued, oldest eligible-event age, batches-required at the 3000-char budget, or estimated drain time.

**Assessment:** Agree — this is the missing observability layer for the rec-8 starvation failure mode, and it's a real gap. Build a route-level backlog query that, per enabled ALERT_ROUTES row, joins the SAME eligibility predicate rec 8 factors out (OPEN, within 24h, family/company/severity match, not delivered on this route) and reports: eligible count, SUM of per-event escaped-line bytes queued, oldest eligible RAISED_AT age (minutes-to-boundary), CEIL(queued_bytes/3000) batches required, and — given a known messages-per-route-per-run cap — estimated runs/time to drain; plus per-route delivered/expired/route_send_failed(retries) from ALERT_DELIVERIES + APP_ERROR_LOG. All OVERWATCH-owned tables, no ACCOUNT_USAGE, so it respects the hot-page perf budget. Surface on the alerts page next to delivery_by_route. Best sequenced WITH rec 8 so both share the one eligibility predicate.

**Relation to shipped work:** Follow-up to this session's undelivered-criticals/expired work (N2 + V063's undelivered_expired flag at SP_NOTIFY_WEBHOOK:159-178). Those flag starvation only AFTER the 24h boundary; this rec adds the pre-boundary route-level early-warning the shipped surfaces lack. Pairs with rec 8.


### 9 — FINISH Overview batching by dependency + cache tier

**PARTIAL_ALREADY_BUILT** · medium · S

**Current state:** app/ui/pages/overview.py render() still issues, before the KPI row: board_res = _load_board (hourly, key exec_board_{company}_{days}, filter-scoped, line 201); _bt_hist = run(fact_daily_spend(150)) (hourly, key fact_daily_150, fixed/account-wide, line 220); _live_pf = run_batch([open_alerts, action_queue]) (live, BATCHED — this is the N4 work, lines 239-244); _thr = run(fact_query_window_summary) (recent, score_throughput_{company}, line 291); _tk = run(fact_task_daily) (recent, score_tasks_{company}, line 300); score_inputs = run_mart_first(...) (recent two-tier, lines 311-316); _hs = run(health_strip) (live, key health_strip, line 326). So N4 batched ONLY the two live reads; the rec's premise (board/150d/throughput/task/score/health remain separate) is factually accurate.

**Assessment:** NARROW. The rec's framing implies broad batching, but most of the named reads must stay separate by deliberate design: (a) board (hourly, filter-scoped) and 150d (hourly, fixed) are explicitly kept unbatched — the comment at overview.py:197-204 documents that coupling filter-scoped + fixed reads cold-starts the fixed read on every company/days change, and run_batch keys its cache on the combined SQL text (_cache_scope of the joined statements, query.py:489), so batching them WOULD reintroduce exactly that regression → DECLINE that part; (b) _hs uses key='health_strip' precisely to hit the shell's warm shared cache (main.py), so batching it into Overview breaks the shell-share → DECLINE that part; (c) score_inputs uses run_mart_first (mart-then-live fallback) and does not fit the run_batch shape. The ONE clean, safe residual: _thr (throughput) and _tk (task) are both tier='recent', both company-scoped, both independent (lines 291-306) — batch these two via run_batch (member-level fallback already exists in run_batch). That saves one round trip on cold paint. Recommend implementing only that pair; do not batch board+150d or health.

**Relation to shipped work:** Direct follow-up to N4 (which batched only the 2 live reads open_alerts+action_queue). The remaining clean win (batch _thr+_tk) is a small residual; the larger-sounding board+150d batching fights the deliberate filter-vs-fixed cache-key design shipped alongside it.


### 10 — Move task-node (C18) + score reads from 'recent' to 'hourly' tier

**CONFIRMED** · low · S

**Current state:** All four reads sit on the 5-min 'recent' TTL (300s) while their sources refresh hourly/daily: operations.py:499-501 task_nodes → tier='recent', source MART_TASK_NODE_DAILY (its own caption line 503 says 'it loads hourly once V058 is applied'); overview.py:291-293 score_throughput → tier='recent', FACT_QUERY_HOURLY; overview.py:300-302 score_tasks → tier='recent', FACT_TASK_DAILY (daily); overview.py:311-316 score_inputs → mart_tier='recent'/live_tier='recent', FACT_PLATFORM_SCORE_DAILY (daily snapshot). CACHE_TTLS (query.py:27) = recent 300, hourly 3600; the r13 #3 comment at query.py:28-30 already established the 'hourly' tier for exactly this case ('mart/fact reads whose SOURCES load hourly or daily - a 300s TTL re-paid them 12x/hour').

**Assessment:** AGREE. These reads were left on 'recent' when the 'hourly' tier was introduced for the same class of hourly/daily-refresh facts (the board and fact_daily_150 already use 'hourly'). Moving them to tier='hourly' aligns cache TTL to data cadence and removes ~11 redundant re-fetches/hour/scope each. Refresh-salt invalidation is tier-independent (_cache_scope bakes the salt for every tier, query.py:309), and the Refresh button (bump_refresh_salt) still clears instantly — so no freshness loss. Low risk, pure config: change the tier string on the four reads (and the mart_tier/live_tier on score_inputs). Note score_inputs' live fallback is a 4-source live aggregation; hourly TTL there is still fine since it's a daily snapshot.

**Relation to shipped work:** Residual of C18 (task_nodes panel) and C2/N5 (fixed-window score reads) shipped this session — those introduced/repointed the reads but left them on the default 'recent' tier rather than the 'hourly' tier that r13 #3 created for hourly/daily facts.


### 18 — Performance telemetry is statistically biased; query IDs not persisted

**CONFIRMED** · medium · M

**Current state:** should_persist_telemetry (query.py:52-67) persists a row when (not ok) OR elapsed>=2000ms, PLUS ~2% of all healthy fetches (sample_roll < sample_rate=0.02). The 2% sample is passed from _persist_telemetry via random.random() (query.py:82). _persist_telemetry (query.py:70-104) writes PAGE, TIER, QUERY_KEY, ELAPSED_MS, ROWS_RETURNED, OK, CACHE_HIT, SQL_HASH, BATCH_SIZE, TRUNCATED — it does NOT persist the sample probability, and does NOT persist the query id. The query id IS captured (_LAST_QUERY_ID via async job handle, query.py:179) and stored in the in-SESSION buffer (_telemetry, query.py:344 'query_id') but is dropped on the persisted path — _telemetry passes query_id to the in-session dict only, never to _persist_telemetry (call at query.py:349-351 omits query_id).

**Assessment:** AGREE. All four sub-claims verify: all slow/failing persisted, only 2% healthy sampled, so any p95/pain ranking computed over APP_QUERY_TELEMETRY over-weights the tail and skews high; the sample probability is not persisted, so the bias is UN-correctable downstream (you cannot re-weight healthy rows by 1/0.02); and the captured query id never reaches the persisted table, so you cannot join to ACCOUNT_USAGE.QUERY_HISTORY for scan/spill/credits/queue/compile enrichment. Recommend: persist SAMPLE_PROB (1.0 for the complete exception stream, 0.02 for the sampled healthy stream) and QUERY_ID, then the Admin>Performance view computes weighted percentiles and joins to Query History. Effort M because it needs an APP_QUERY_TELEMETRY schema add (two columns) — owner-migration territory — plus buffer-shape update in _persist_telemetry (the _ow_qtel_oldshape downgrade path must be extended).

**Relation to shipped work:** Residual of N12, which buffered the telemetry writes (INSERT...SELECT...UNION ALL) but explicitly KEPT the 2% sampling and did not add sample-probability or query-id columns. The 2% healthy sample (Codex #19) partially addresses the earlier 'healthy baseline invisible' concern, but the bias-correction and the query-id join asked for here remain unbuilt.


### 14 — Group the flat sidebar by operator workflow (Watch/Analyze/Govern)

**CONFIRMED** · low · M

**Current state:** app/config.py:116-121 PAGES_BY_PROFILE maps each profile to a FLAT tuple of page names (e.g. DBA -> Brief, Overview, Control Room, Cost & Contract, Operations, Alerts, Security, Admin). app/main.py:97-98 renders them as a single un-grouped st.radio('Navigate', pages, ...). No Watch/Analyze/Govern grouping exists. Per-profile role visibility (page filtering) and the Brief/Overview/Control Room separation are preserved but shown as one flat list.

**Assessment:** AGREE the described state is exact (flat radio, no workflow grouping) but NARROW the value: pure UX affordance, zero correctness impact. st.radio can't render section headers natively, so implementing means either caption separators between groups or a move to st.navigation/st.Page groups (a nav refactor). Recommend, if done: add a NAV_GROUPS ordering in config.py {Watch:(Brief,Overview,Alerts), Analyze:(Control Room,Cost & Contract,Operations), Govern:(Security,Admin)} intersected with the profile's allowed pages, render group captions in _sidebar. App-only, low priority vs the correctness recs.

**Relation to shipped work:** Independent of this session's v4.82-4.88 work (scoring/freshness/alert-count/forecast/runway plumbing). Not a residual; a standalone nav-ergonomics item.


### 16 — Turn metric_registry into an EXECUTABLE contract (window/partial-day/filters/sources/unit/coverage/owner; panels+SQL tests key off it so drift fails CI)

**PARTIAL_ALREADY_BUILT** · medium · L

**Current state:** app/logic/metric_registry.py defines a frozen Metric dataclass with EXACTLY these fields: key, label, method, grain, source, timezone, latency, formula_version, notes. 17 metrics registered. It is DESCRIPTIVE metadata only. Missing every executable field the rec names: no window semantics, no partial-day policy, no supported-filters list, no structured required-sources, no unit/rate, no coverage rule, no owner. Surfaced ONLY as a read-only table on Admin (as_rows) and grouped by method (by_method). tests/test_metric_registry.py only checks field-presence/uniqueness/that Admin renders it. NO panel references a registry key to derive behavior, and NO SQL test asserts a panel/mart matches a registry contract, so registry<->panel drift CANNOT fail CI.

**Assessment:** AGREE, largely unaddressed. The 'single semantic contract' scaffold (C16 prior review) shipped and is real, but it is documentation, not an executable contract. What's LEFT: (1) add window/partial-day/filters/required_sources/unit/coverage/owner fields to Metric; (2) have panels import a registry key and read its window/partial-day policy instead of hard-coding; (3) a CI test that asserts each panel's declared metric key exists and that its SQL window/source matches the registry so drift breaks the build. That is the whole point of the rec and none of it is present. Effort L (touches many panels). Value is real (this session repeatedly patched the SAME semantic drift -- see rec 20 runway -- which an executable registry is designed to catch), but it's a large architectural investment; sequence it after the concrete runway consolidation.

**Relation to shipped work:** Directly motivated by this session's recurring drift patches: N11 (contract runway basis), C2+N5 (fixed-window score reads), C1/REQUIRED_SIGNAL_SOURCES all hand-fixed one-off semantic mismatches an executable registry would have caught. So it's the meta/follow-up to the shipped fixes rather than a residual of any single one.


### 20 — Consolidate contract RUNWAY into ONE canonical trailing-30-complete-days metric feeding Brief, Contract, COST_CONTRACT_BREACH

**PARTIAL_ALREADY_BUILT** · high · M

**Current state:** N11 fixed ONLY the Cost&Contract page KPI. app/logic/forecast.py:107-151 contract_pace now takes trailing_daily_credits; app/ui/pages/cost_parts/contract.py:320-332 computes it correctly from fact_daily_spend(30) EXCLUDING today's partial day (_bd < account_today(), line 327) and averaging complete days. BUT the Brief and the alert are UNCHANGED and still use the buggy math: app/data/mart_sql.py:698-716 contract_exhaustion() computes DAILY_BURN = SUM(CREDITS_BILLED)/30 WHERE DAY >= DATEADD('day',-30,CURRENT_DATE()) -- that predicate spans 31 distinct dates AND includes today's partial metering, yet divides by a fixed 30. brief.py:60 and :109 call contract_exhaustion() for the 'Contract exhausts' KPI. The V062 alert COST_CONTRACT_BREACH (V062__...sql:2402-2419, now in SP_ALERT_SCAN_DAILY per C9) has the IDENTICAL SUM/30 over DAY>=today-30 block. contract_exhaustion's own docstring says 'Same math as the COST_CONTRACT_BREACH scan block'. Separately the renewal planner (contract_planner.remaining_balance_summary, burn over a window) is a third computation. CONSUMED bases also differ: mart uses DAY>=CONTRACT_START_DATE; the page uses fact_contract_consumed(start).

**Assessment:** AGREE, and this is the highest-value of the three. The rec's specific defect is CONFIRMED in current code: Brief + COST_CONTRACT_BREACH divide a 31-date, partial-current-day sum by 30, biasing DAILY_BURN low (understating burn -> overstating days-left/exhaust date -> can suppress the alert). N11 fixed the page KPI to trailing-complete-days but deliberately left the share/pace fields lifetime and did NOT touch the mart. So Brief and the alert can still contradict the page. RECOMMEND: make ONE canonical trailing-30-COMPLETE-days burn in the mart: change contract_exhaustion() DAILY_BURN to AVG over DAY BETWEEN today-30 AND today-1 (exclude today, divide by actual complete-day count, not a literal 30), apply the same edit to the V062 COST_CONTRACT_BREACH block (owner migration -- bundle into the next V), and have the page KPI, Brief, and alert all read that single definition (coverage-start aware, AI-blended-rate-aware to match the page's _trailing_daily). Effort M: one mart_sql edit + one migration block + confirm brief/alert consume it; contract.py already conforms.

**Relation to shipped work:** Direct residual/follow-up of N11. N11 (v4.83-4.86) fixed exactly ONE of the >=3 runway sites (the contract page projection); the Brief runway and the COST_CONTRACT_BREACH alert -- the very site moved into SP_ALERT_SCAN_DAILY by V062 C9 this cycle -- were left on the old /30-partial-day math. This is the unfinished half of the same problem N11 started.
