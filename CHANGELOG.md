# Changelog

## 4.40.0 — r24: profile links everywhere + the first honesty/tier ships (2026-07-13)

- **Snowsight query-profile links are a pattern now** (owner: "the
  hyperlinks to the query profile are helpful"). A shared helper turns any
  QUERY_ID column into a Profile link (org/account resolved once per
  session; no context = no column, never a dead link). Wired: Operations
  heaviest queries, Optimization costliest queries, Unit-costs CALL
  pricing, Admin running-queries. One click from any row to the plan,
  partitions, and spilling.
- **Review #4 — the dead cache gauge is off the pain board.** CACHE_HIT_PCT
  read 0.0 by construction (persisted rows are slow-biased); the tuning
  targets table drops it, the by-page table keeps it with the
  floor-not-census caption, and the real gauge returns with weighted
  telemetry (review #3).
- **Overview: the Monthly-budget KPI is replaced** (owner: "I don't like
  having useless features" — it read 'Not configured' forever). Its slot
  now paces MTD against the prior month's SAME first-N-days from the 150d
  frame the page already loads (zero new queries); a configured budget
  survives as help-text context. No prior-month data = no fabricated 0%.
- **Review #8, first slice — post-action freshness is systemic.**
  execute_statement bumps the refresh salt on success, so every cached
  read refetches after any operator action; with that guarantee in place,
  the risk-free live-tier downgrades land (SCHEMA_VERSION -> metadata,
  Flyway probe -> recent, alert routes -> recent). The deliberate live
  surfaces (settings table, dept budgets, alert queue, recheck-now)
  stay live. The queue itself is next slice, behind an ack-flow test.

Deploy: app-only — push the build.

## 4.39.0 — triage filters, visible; the 20-item forward review (2026-07-13)

(Entry restored in v4.40.0 — the stamp script died mid-run and the commit
went out titled v4.39.0 without it; the work itself shipped in f5852d4.)

- Triage filter strip visual pass: active-scope chips (contains-filters
  amber), a filtered-strip glow, one-click Reset, and an honest
  "Account-wide · default window" state. Token-layer CSS; user text
  escaped; locked in test_design_system.
- docs/reviews/APP_REVIEW_20260713.md: twenty grounded recommendations in
  four tiers with r24-r26 sequencing.

## 4.38.0 — r23: the post-rebuild fleet board's picks (2026-07-12)

App-only round, by design — the rebuild settles while the telemetry does
the picking (pain = p95 x slow fetches; the board's leaders, not opinions).
All four targets verified in code before shipping:

- **Queue/spill pressure goes fact-first (c_pressure, 17.8s p50).** The
  hourly fact carries exact queued/spill/count sums; a new
  fact_warehouse_pressure reader serves the Contention panel with the live
  scan as labeled fallback (p95 = peak hourly, labeled). Canaried;
  operations' live-scan budget drops 23 -> 22.
- **Task-failure RCA prunes on SCHEDULED_TIME (t_rca, 32.9s).** TASK_HISTORY
  prunes on SCHEDULED_TIME — the V031 builders bound both columns for
  exactly this reason; the RCA read now does too (+1d margin, semantics
  unchanged).
- **Change-impact drill pre-filters before normalizing (chg_hist, 6.1s).**
  The V031 scan-v2 trick applied to object_run_history's PROCEDURE branch:
  a cheap ILIKE rides in front of the POSITION text pass, so only plausible
  CALL rows pay the REPLACE/UPPER normalization.
- **Security's changes tab batches its two live reads** (DDL/DCL + failed-
  login reasons) — the Access-tab parallel-submit pattern, serial fallbacks
  unchanged.

Still open from the board after this round: cs_types 21.4s (routed r22 #16,
extract v2), the loader-v2 re-derivation set (#4/5/6/8/19), and the V1
EXECUTION_STATUS probe (one query in Snowsight decides which side gets the
one-line fix).

Deploy: app-only — push the build; no migration, no grants.

## 4.37.1 — validate: the stale V001-era user-prefix check (2026-07-12)

Live finding on the rebuilt account: 'TRXS_ prefix classifies as Trexis'
FAILed. The check probed a fictional user against V001's prefix rule, which
V019 deliberately replaced with role-membership classification — latently
wrong since then, surfaced by the fresh install. Replaced with two
deterministic checks that test the CURRENT contracts: the database prefix
rule (COMPANY_FOR_DATABASE('TRXS_EDW_PRD') = 'Trexis') and the unknown-user
ALFA fallback. Rebuild-bundle copy regenerated. Also confirmed from the
rebuild's error log: the four mart_load_failed rows are V027/V029 first-fill
replay artifacts (fixed by V030 in-sequence) — expected on every fresh
apply, not a live failure.

## 4.37.0 — Codex r22: eight ships, ten routes, two declines (2026-07-12)

Every claim verified in code first; the adjudication with evidence is
docs/reviews/CODEX_R22_ADJUDICATION_20260712.md. Shipped (V042 +
app):

- **#7 — the extract is atomic and the watermark gates on COMMIT.** The
  v4.36.1 isolation could delete the overlap, fail the insert, and still
  advance the watermark — a hole every consumer MERGEd in until the
  nightly repair. Each arm is one transaction now; a failed cycle
  re-covers its own window.
- **#1 — FACT_QUERY_DAILY** (day grain, year-backfillable): the exec
  board's 14/60/90 windows and the platform score read it, so a fresh
  rebuild starts with real query totals instead of undercounting while
  the hourly fact accrues from day one.
- **#2 — ops diagnostics backfill** (wide explicit loads; recurring stays
  2d) — the 7-day Operations first paint is mart-served on day one.
- **#10 — retention: sixteen V027/V041 tables join SP_PURGE_FACTS** (the
  whole mart family predated the purge, not just the new tables).
- **#15 loader half — the AI fact gains EMAIL + exact FIRST/LAST usage
  stamps.** The users tab STAYS live-first per the owner decision; the
  mart re-swap is queued behind a side-by-side proof.
- **#14 — AI users section is toggled** (the exact Cortex scan no longer
  runs ambiently with the chargeback group), **#17 — drill lookups bound
  to the clicked row's day ±1** (pasted IDs keep the broad scan), **#20
  label half — the fleet board names its exception-weighted sample.**

Routed: #4/#5/#6/#8 (one reviewed loader-v2 re-derivation, together),
#3/#11/#12/#16 (extract v2 / loader v2), #13 (fix-batch), #19 (loader v2
headline: percentile states), #20 stats half (fix-batch), #15 app half
(behind fact proof). Declined with evidence: #9 (COUNT/MAX are
metadata-served — freshness writes are constant-cost already), #18 (the
"second scan" is the share-law denominator; folding it post-filter is the
renormalization bug the law exists to prevent). Open: V1 — the
'FAIL' vs 'FAILED' EXECUTION_STATUS split between V002 facts and V027
marts needs one live probe; the loser gets a one-line fix.

Deploy: V042 after V041 (the rebuild bundle is regenerated to 42 files) ->
roles.sql -> backfill_365.sql (now fills FACT_QUERY_DAILY for the year +
the diagnostics mart for 90d) -> validate expects V001..V042.

## 4.36.2 — the one-shot rebuild bundle (2026-07-12)

Owner: "i want the full rebuild." snowflake/rebuild/ is docs/FULL_REBUILD.md
as six paste-and-run Snowsight files: 00 date-stamped clone backups of all
21 operator tables (verified counts, zero DROPs), 01 teardown (byte-copy),
02 all 41 migrations concatenated in order (Run All halts AT a failure;
every file idempotent), 03 roles, 04 backfill, 05 validate. GENERATED and
equality-locked against the sources (tests/test_rebuild_bundle.py) — edit
the sources, regenerate the bundle, never hand-edit it. Operator data
survives by default; the factory-reset variant deliberately stays manual.

## 4.36.1 — V041 corrections: the owner's regressions, fixed at the root (2026-07-12)

Owner reports after v4.36.0: the cortex user table lost emails/timestamps,
validate.sql errored on TASK_DEPENDENTS, task-graph failures and alerts
went quiet. Root causes + the review live in
docs/reviews/V041_INCIDENT_REVIEW_20260712.md; the live-account recovery
is snowflake/loader_chain_check.sql (step 0), then redeploy, then
docs/FULL_REBUILD.md for the clean slate.

- **Cortex user attribution reverted byte-for-byte to v4.34.2.** The R3
  mart swap served NULL emails and day-grain usage stamps — the exact
  who/when IS that table. Live-first again (probe semantics intact); the
  degraded reader + canary deleted. A correct R3 (EMAIL + FIRST_TS/LAST_TS
  on the fact first) is queued, not shipped.
- **The task tree can no longer strand suspended (the real alerts/task-
  graph killer).** v4.36.0 put every RESUME after seven first-fill CALLs —
  a halted worksheet run left both roots' children suspended (the 07-12
  outage class the design ordered dead). The full resume +
  SYSTEM$TASK_DEPENDENTS_ENABLE block now runs BEFORE the fills and again
  at file end; locked (two enables per root, order asserted).
- **Extract loader isolated + no bare NULLs.** A flaky QUERY_HISTORY scan
  now logs and degrades (consumers read the previous fill) instead of
  failing the task and SKIPping the hourly chain; watermark mode is
  DAYS_BACK <= 0 and the tasks pass 0.
- **Posture SHOW guarded:** a SHOW failure skips only the two monitor
  metrics (HAVING — never a lying zero), never core posture.
- **Ops diagnostics made exact:** top-50/hour (the unfiltered top-50 panel
  is exact by construction, not a sample); USERS_AFFECTED is an honest HLL
  window approx-distinct (V037 precedent), labeled.
- **validate.sql: task monitoring removed** (owner decision — unused, and
  TASK_DEPENDENTS needs a db context bare runs don't have). Task-state
  diagnosis moved to the new snowflake/loader_chain_check.sql.
- New: docs/FULL_REBUILD.md — the safe full drop-and-reinstall (the schema
  is SHARED with the previous app; nothing drops DBA_MAINT_DB).
- Review: v4.34.2 -> v4.36.1 delta audited file-by-file (the three shared
  infra changes are sound — no revert of v4.35.x needed); every remaining
  V041 swap holds its live contract exactly, with coverage-gated fallbacks.

Deploy: redeploy the app; run loader_chain_check.sql step 0 on the live
account now; rebuild per docs/FULL_REBUILD.md when ready (V041 re-applies
with the corrected file).

## 4.36.0 — V041: the loader-efficiency pass (2026-07-12)

One migration, eleven riders — built to the design freeze
(docs/design/V041_LOADER_PASS.md), in a fresh session, doc as contract.

- **R1 — one QUERY_HISTORY scan per hourly cycle.** `OW_QH_EXTRACT`
  (transient, watermark - 45 min, 3-day retention) feeds the design's
  consumer list exactly: FACT_QUERY_HOURLY, _OW_ALLOC_BASE, tag coverage,
  query-family, schema-hourly, role-hourly, the incident-timeline DDL arm,
  and the new R7 diagnostics. The warehouse-efficiency q-CTE and posture
  ADMIN_STMTS_24H are not on the list and deliberately stay live. Build
  note: the FACT_QUERY_HOURLY arm MOVED into SP_LOAD_QH_EXTRACT (verbatim,
  FROM swapped) — the root's proc runs before the extract fills, so an arm
  left behind would trail a cycle.
- **R2 — FACT_COST_ALLOC_XDIM_DAILY** (DAY x WH x DB x USER, no schema
  grain) persists from _OW_ALLOC_BASE before it collapses; Spend's
  database-filtered attribution goes mart-first (was two live scans per
  filter value) and user-within-database is mart-served.
- **R3 — AI users from the fact.** cortex_code_user_rollup's contract now
  reads FACT_AI_USAGE_DAILY (cortex_users p50 17.6s x12 was the worst
  user-facing key); the live view stays as fallback WITH probe semantics —
  the 002139 subscription note still fires where Cortex Code is absent.
- **R4 — exec board v2.** Builds all five config windows (7/14/30/60/90 —
  14/60/90 always fell to the 13-month live scan before), aggregates each
  source once and unpivots, and swaps in atomically via OW_EXEC_BOARD_STAGE
  (the DELETE+INSERT gap stranded Overview on the live fallback hourly).
  PRESSURE_QUEUE / PRESSURE_SPILL / DB_MIX retired: zero readers.
- **R5 — watermarks + nightly reconcile.** OW_LOAD_WATERMARKS; the extract
  reads watermark - 45 min, the daily loader watermark - 1 day (outage
  self-heal, 30d clamp); TASK_NIGHTLY_RECONCILE delete-and-rebuilds the
  trailing 3 days so restated ACCOUNT_USAGE rows and disappeared groups
  cannot survive stale MERGE rows.
- **R6 — loader-owned freshness.** Every SP merges its sources into
  SOURCE_FRESHNESS_STATE (+GENERATION invalidation token, +STATUS) in its
  own commit; TASK_SNAPSHOT_FRESHNESS retired (144 wakes/day);
  SP_SNAPSHOT_FRESHNESS kept for manual refresh.
- **R7 — MART_OPS_DIAG_HOURLY.** Top-20/hour by elapsed + failure families
  from the extract; Operations' UNFILTERED first paint goes mart-first
  (30-37s batch retired); any entity/schema filter keeps the true live
  top-N. Coverage-gated while the mart accrues.
- **R8 — FACT_PLATFORM_SCORE_DAILY.** The retro score's four input
  aggregates load daily; weights stay in Python; Overview's sparkline
  reads the fact with the live aggregation as fallback.
- **R9 — unused-role posture from FACT_QUERY_ROLE_HOURLY**, coverage-gated
  via HAVING (no row — never a lying zero — until the fact spans 90d);
  the 90d QUERY_HISTORY anti-join leaves the daily posture loader.
- **R10 — WAREHOUSE_ID > 0** joins the V27-family loader's two metering
  source reads (the V039 promise); eff-mart reader name-filters stay until
  the next re-derivation.
- **R11 — monitor counts in the posture row** (WH_NO_MONITOR /
  WH_NO_AUTOSUSPEND) via SHOW -> RESULT_SCAN in the daily posture arm
  (V024's scan is the owner's-rights precedent); Security's governance
  panel stops paying a SHOW + parse on render when the posture row
  carries them.

Derivation law: SP_LOAD_MARTS_V27 re-derived VERBATIM from V031's proc +
the enumerated edits; SP_LOAD_HOURLY_FACTS from V039's minus the moved arm;
SP_LOAD_DAILY_FACTS from V002's + watermark bounds. All three (and the
moved arm) are equality-locked in tests/test_v041_loader_pass.py, which
also carries the design's test plan: the numeric-recon pandas harness
(xdim day-sums == single-dim day-sums == never exceed metering), the
extract-consumer projection contract, the board-windows==config cross-lock,
and the task-graph lock (every V041 task has a RESUME; both roots end with
SYSTEM$TASK_DEPENDENTS_ENABLE — the 07-12 outage class stays dead).
Superseded with semantics: test_v039's "loader = V002 + one predicate"
(now the historical chain link), prep_iac's probe-count needle, wave2's
spend filter-split lock. Budgets went DOWN: overview 2->1, ai_chargeback
5->4, operations 24->23. Canaries added for all five new readers;
EXPECTED_GAPS untouched.

Deploy (Joe): push -> Snowsight: V041 after V040 -> re-run roles.sql (new
objects) -> validate.sql expects V001..V041 -> redeploy the app on the
warehouse runtime. Optional but recommended: re-run backfill_365.sql
(extract fills first now) so the xdim fact starts with 90d of history.
Fleet board after 24h tells us what the pass actually bought.

## 4.35.1 — Codex r21: four ships, two corrected claims (2026-07-12)

- **Fragment docstrings are binding now (#4).** _whatif_panel and
  _statement_export claimed "Fragment:" with no @st.fragment — every slider
  move and month pick re-rendered the whole grouped Cost page. Decorators
  added; an AST lock fails any future "Fragment:" docstring without one.
- **Reconciliation on demand (#7).** Opening Admin > Canary paid a 28d
  metering + 7d history comparison; it now waits for a Run toggle.
- **Settings honor manual refresh (#15, real bug).** The outer settings
  frame cache ignored the refresh salt, so edited settings could read stale
  for 5 minutes after an explicit refresh. Salt joins the key.
- **No-op query-param writes suppressed (#19)** for page and section.

Corrected: #5 — SHOW WAREHOUSES inside the what-if panel is metadata-tier
(4h cache), and with the fragment restored it reruns panel-locally; hoisting
buys nothing. #16 — a single WHERE with OR counts each row once; there is
no double-counting in app self-cost (the OR is at worst a pruning nit).
Routed: #1/#2/#3/#6/#9/#17/#18 -> fix-batch; #8/#11/#12 -> query-core v2;
#10/#13/#14/#20 -> polish round.

## 4.35.0 — Codex r20: five verified ships, one decline that mattered (2026-07-12)

- **Remediation reuses the advisor's read (#1).** The Optimization
  remediation block re-ran the live metering x history join the idle
  advisor had already answered mart-first; it now uses the identical
  builder pair, so the advisor's cache serves it.
- **Quarantine is cross-page safe (#2).** Batch-quarantine entries were
  short member keys ('act'), so one page's failure forced same-named
  members on other pages onto the serial path. Identity is now
  page + key + sql-hash.
- **Helper CTEs carry the company predicate (#4).** Idle, sizing, and
  hourly-activity supporting scans (QUERY_HISTORY / FACT_QUERY_HOURLY)
  ran account-wide and were only narrowed by the join; the predicate now
  applies before aggregation ('1 = 1' keeps ALL-scope SQL valid).
- **Sparks match their neighbors (#7).** Overview's activity sparkline is
  company-scoped; Operations' is company+database — same treatment
  Control Room got in v4.34.0.
- **One credentials scan (#17)** via COUNT_IF in the governance fallback.
- **#20 DECLINED — the registry stays full.** Codex claimed eight canary
  readers are unreachable; a whole-tree scan (including app/main.py, which
  subtree greps miss) shows every one has a live caller. Nothing removed.

Routed: #3 retry backoff -> query-core v2; #5/#6 pipeline+topology scoping,
#11-#16, #19 -> the standing fix-batch; #8/#9/#10 -> polish round.

## 4.34.3 — CI hotfix: floor-compat job vs bare sqlglot imports (2026-07-12)

The v4.34.1/v4.34.2 test files imported sqlglot at module level; CI's
floor-compat job installs only the Streamlit/pandas floor pins, so test
collection failed there (the local gates — pytest + ruff — never saw it).
Both files now use pytest.importorskip like the migrations gate. Local
gates gain CI parity permanently: mypy and a floor simulation (pytest with
sqlglot absent) run before every ship. mypy is clean on all 39 files.

## 4.34.2 — Codex r19: six page-level ships, the rest routed (2026-07-12)

Shipped (all verified in code first):

- **Day replay on demand (#2).** Six reads for a bottom-of-page feature ran
  on every Control Room rerun; now behind a Load toggle.
- **Open actions can't age out (#5).** The action reader fetched the newest
  200 of ANY status, then Python dropped closed rows — an old open critical
  could fall outside the window. Status now filters in SQL (literals
  cross-locked to logic.OPEN_STATUSES); ranking stays in one place.
- **Zero-failure scan skip (#6, task side).** When the >=7d task summary in
  the same scope counts zero failures, the 7d TASK_HISTORY root-cause scan
  is skipped. Query side declined: those members run in one parallel batch,
  and serializing them trades latency for bytes.
- **Storage snapshot fetches a snapshot (#7).** Both storage builders now
  QUALIFY to the latest loaded day (the panel discarded the window anyway,
  up to ~90x rows). Also fixed a stale source label that still claimed the
  live view after the fact swap.
- **One-member batch removed (#18)** — contention pressure uses plain run().
- **Exports (#19/#20).** Big-table prep now stores the CSV BYTES (the old
  boolean reserialized on every later rerun); every download_button is
  frontend-only (on_click="ignore", Streamlit >=1.44 — we pin ~=1.45), so
  downloads no longer rerun the app.

Routed: #1 board windows, #3 score snapshot, #8/#9/#10 exec-board rework ->
V041 loader pass (board rider). #4/#11/#12/#13/#14 -> next fix-batch with
r18 #4/#5/#7/#8. #15 canary concurrency, #19-full fingerprint cache ->
query-core v2. #16 declined (the live settings read is the operator
edit-freshness path; the table is tiny). #17 declined (ORDER BY is part of
reader contracts — sparklines and .iloc[0] depend on it; savings negligible
on aggregate outputs).

## 4.34.1 — Correctness audit batch 1 + Codex r18 verified fixes (2026-07-12)

Full-app filter/metric audit (owner ask) merged with r18 verification.
Confirmed and fixed:

- **Broken sizing fallback (r18 #2, REAL — my V039 edit).** The
  warehouse-sizing live builder carried a bare second WHERE and failed on
  every render since v4.30.0; nothing parsed app-side SQL. Fixed, and the
  class is dead: the parse gate now runs sqlglot over EVERY canary-registered
  builder (~165), not just migrations.
- **Admin tuning-target drill (owner report: "flashes and does nothing").**
  selectable_table returns a positional index; the drill subscripted it like
  a row since v4.23.0 and a silent except ate the TypeError on every click.
  Fixed with iloc, and the except now reports instead of passing.
- **Two more attribution-law violations.** Role shares (live + mart) filtered
  roles inside the per-warehouse denominator — excluded roles' slices were
  re-absorbed by this company's roles on shared warehouses. expensive_queries_usd
  filtered the q CTE that also built its warehouse-hour denominator (dead
  param masked it). Both now: whole-scope denominator, filters pick display
  rows after. Optimize's expensive-query scan now honors the sidebar
  database/schema filters.
- **max_rows authoritative (r18 #1).** A trailing LIMIT larger than the cap
  is rewritten down (a 20,000-row reader must honor a 1-row canary cap);
  small trailing limits still win.
- **AI fact-first ordering (r18 #3).** Unit Costs read FACT_AI_USAGE_DAILY
  only after paying the live Cortex scan in its batch, then usually threw
  the live result away. The live member now joins the batch only when the
  fact can't answer. Role-allocation math vectorized (r18 #16).

Audit sweep found no further violations: dept chargeback, CS drills,
fingerprints and pattern movers are measured/exact; deep-scan KPIs label
top-N sums as top-N. r18 #4/#5/#7/#8 (fact-first reader swaps) = approved
next fix-batch; #9-#15/#17-#20 map to V041, registry design, and
query-core v2 (details in the session log).

## 4.34.0 — Control Room follows the database filter (2026-07-11)

Owner ask: "i should be able to filter in Control room using database."
The pulse KPIs and task panels already followed it; now the whole page
answers coherently:

- Activity sparkline: fact_daily_activity gains company + database args
  (FACT_QUERY_HOURLY carries both), so the 14-day spark matches the pulse
  KPIs beside it. Defaults keep every other caller's SQL byte-identical.
- Lock-wait spikes: MART_LOCK_WAIT_DAILY has carried DATABASE_NAME since
  v4.21 — the spike scan now narrows to the selected database.
- Grain honesty where the filter can't reach: the incident timeline
  (company events), spend movers (warehouse grain), and the triage queue's
  alert/anomaly rows say so in one line each, only while a database is
  selected. The header scope chip shows the active database.
- Sidebar help updated: query, task, DDL, attribution, storage, and lock
  panels. Locks in tests/test_cr_db_filter.py.

## 4.33.2 — Codex r17: one new item in a fourth convergent round (2026-07-11)

18 of r17's 20 items restate the adjudicated queue (V041 loader riders,
registry design, query-core v2, standing owner decision on a dedicated UI
warehouse, measure-first clustering, executable perf contracts). Shipped the
one new bug: the optional AI Functions view ran its historical scan on every
AI-tab paint because expander bodies execute even when collapsed — the scan
now waits for an explicit toggle (deep-scan forensics pattern). r17 #17's
mechanism is wrong (st.text_input fires on Enter/blur, not per keystroke),
but the underlying cost of schema-filtered attribution is real and dies with
V041 rider nine; Apply-form/fragments ride the queued fragments round.

## 4.33.0 — Codex r16: the two new items, and calling the convergence (2026-07-11)

r16 is the third consecutive round recommending substantially the same
list. Shipped the two items that were both new AND safe as reader swaps:

- **Cortex service spend from the fact (r16 #7).** Chargeback & AI's
  Cortex panel read live METERING_DAILY_HISTORY although FACT_METERING_DAILY
  carries DAY, SERVICE_TYPE, and billed credits. Fact-first with the live
  scan as fallback; same SERVICE_TYPE predicate, same billed basis. Per the
  r15 lesson, the lock asserts the fact version contains no USAGE_DATE.
- **Overview reads metering once (r16 #17).** The 45d MTD read and the 150d
  backtest read collapse into one 150d frame loaded up front —
  _mtd_spend_usd's preloaded parameter existed for exactly this; the 45d
  read survives only as its internal fallback.

Convergence disposition (#1-#6, #8-#16, #18-#20): every remaining item is
already queued — the loader-efficiency pass (V041: staging extract,
watermarks, task-graph phasing, loader-owned freshness, ops mart, AI Users
fact, role-posture fact, Cortex reader landed here instead), the
coverage/cadence registry (now noting r16 #10's real find: backfill_365
only prepends before MIN(DAY) and cannot repair interior holes), the
query-core v2 design (per-member batch caching, real server metrics,
buffered telemetry, byte budgets), the dedicated UI warehouse (owner
decision), measure-first clustering, the fragments round, and executable
contracts. Recommendation: pause review rounds until the loader pass
lands — the last three rounds' marginal yield was two reader swaps and
two same-day bug catches on freshly written code.

Deploy: redeploy the app; no migration.

## 4.32.1 — r15 #1: my regression, their catch, everyone's class-killer (2026-07-11)

- **Chargeback window read fixed.** The r14 fact swap changed the FROM but
  left the live view's M.START_TIME in the WHERE — FACT_WAREHOUSE_DAILY has
  no such column, so every Chargeback render failed (and failures are never
  cached, so it re-failed each rerun). Now M.DAY, like its month sibling.
- **The class is dead, not just the instance.** New sweep test walks the
  entire canary registry: any builder reading ONLY our facts/marts must
  never reference the live views' time columns. The r14 lock checked the
  table name and missed the column — r15 #20's criticism was fair.
- **Brief stops paying for the health strip twice (r15 #14, the concrete
  half).** The app shell runs it every render under key="health_strip";
  Brief's batch tuple-cache paid the same SQL again. Brief now shares the
  shell's cache entry. (Per-member batch caching remains the query-core v2
  design.)
- r15 disposition: #2/#3/#4/#6/#7/#9/#10 all fold into the loader-efficiency
  pass (now EIGHT riders on the V27 re-derivation — next session, fresh
  context, one migration); #5 dedicated UI warehouse = owner decision,
  standing recommendation; #8 rides it too (Cortex spend from
  FACT_METERING_DAILY is a two-line reader swap once the pass lands);
  #11/#12/#13 = the coverage/cadence registry design; #15/#16/#17 =
  query-core v2 (byte-budgets noted); #18 clustering = measure-first note
  (SYSTEM$CLUSTERING_INFORMATION before any CLUSTER BY — likely unnecessary
  at current table sizes); #19 fragments round; #20 partially delivered
  tonight via the schema sweep.

Deploy: redeploy the app; no migration.

## 4.32.0 — Codex r14: the fact backfill pays off + a same-day bug caught (2026-07-11)

- **Contract coverage predicate fixed (r14 #8 — a bug shipped hours ago).**
  fact_contract_consumed computed MIN(DAY) after WHERE DAY >= start, so a
  quiet contract-start day would read as "no coverage" and fall back to the
  live rescan forever. Coverage (FACT_FIRST_DAY) is now computed over the
  whole fact; the contract-window sum moves into an IFF.
- **Four metering surfaces move to the fact (r14 #5).** The 365-day
  FACT_WAREHOUSE_DAILY backfill already existed — department chargeback
  (window + monthly statements), the Brief's app-cost quarter, and the boss
  chart's long-view fallback now read it instead of live
  WAREHOUSE_METERING_HISTORY. Same exact-metering basis (CREDITS_TOTAL =
  CREDITS_USED), pseudo-warehouse-filtered by construction, no live scans.
- **Freshness boards poll at snapshot cadence (r14 #13).** The V040 state
  table moves every 10 minutes; the boards' 30-second live tier bought
  nothing. Now "recent" (5 min).
- **Cache cardinality bounded (r14 #17).** max_entries on every tier
  fetcher (run + batch): refresh salts and filter permutations mint keys
  forever; process memory now has a ceiling.
- **Security header reads posture once (r14 #18, first half).** The
  governance score (latest day) and the 90-day trend share one read —
  the 3d + 90d double-read is gone. Persisting warehouse-monitor counts
  into the posture snapshot rides the V27-family re-derivation.
- r14 disposition elsewhere: #1+#3+#4+#6 = the loader-efficiency pass
  (one QUERY_HISTORY staging extract, watermark loads, loader-owned
  freshness rows, ops-diagnostics mart) — next session's design+build,
  bundled with the V27 re-derivation and its riders; #7 (AI Users from
  FACT_AI_USAGE_DAILY — the fact HAS user grain) joins it with proper
  contract tests, hourly tier already cut its refire 12x; #2 (dedicated
  UI warehouse) is an owner decision — recommended, needs a quota call;
  #9/#10/#12/#20 = the coverage/cost registry design; #11 waits for a
  second viewer; #14/#15/#16 = query-core v2 note; #19 = fragments
  polish round.

Deploy: no new migration — redeploy the app (V040 still pending if not applied).

## 4.31.0 — Codex r13: the cache stops re-paying hourly data every 5 minutes (2026-07-11)

Driven by the owner's fleet screenshots (1.5-3.4% cache hits on the top
pages with ONE viewer = TTL exhaustion, not user fan-out): the perf batch.

- **V040 (`V040__freshness_state.sql`) — freshness is a lookup (r13 #2).**
  SOURCE_FRESHNESS_STATE: one row per source, snapshotted from the
  19-aggregate view every 10 minutes server-side by SP_SNAPSHOT_FRESHNESS +
  TASK_SNAPSHOT_FRESHNESS (seeded on apply). The health strip and both
  freshness boards read the tiny table (staleness computed from
  LAST_LOAD_TS at render); the view remains the writer and the pre-V040
  fallback.
- **"hourly" cache tier (r13 #3).** TTL 3600 for reads whose sources load
  hourly/daily; `run_mart_first`'s mart side now defaults to it. The
  Refresh button still invalidates everything instantly. Loader-generation
  invalidation stays a design note — this captures most of the win for
  none of the plumbing.
- **Contract & Forecast sheds its live scans (r13 #6/#7).** Steering levers
  build from the efficiency mart + the V037 pattern mart (measured, with a
  shape adapter; live joins remain fallbacks), and contract consumption
  sums FACT_METERING_DAILY gated by a coverage predicate — the fact must
  actually REACH the contract start (FIRST_DAY) or the live rescan serves.
- **Compare pruning finished (r13 #11/#12).** Adjacent A/B windows predicate
  as one contiguous range; the pattern sample-text subquery is bounded on
  both ends.
- **Rendering (r13 #19).** STYLER_MAX_ROWS 1500→400 (Arrow-native formats
  above); table exports are two-step beyond 200 rows so big frames stop
  serializing CSV on every rerun.
- r13 disposition elsewhere: #1 cache policy deferred until a second viewer
  exists (r9 leak guardrail stands); #4+#17 = query-core v2 design note;
  #5 rides the V27 re-derivation (now with freshness-state + pseudo-warehouse
  riders); #8 self-retires as the mart accrues; #9 readiness-by-freshness
  joins the registry design (the state table is its foundation); #10's hot
  case covered by the hourly tier; #13/#14/#15 = R3/R4/usage-first law;
  #16 declined (sampled + capped, no reliable flush hook); #18/#20 queued
  (registry design / fragments polish round).

Deploy: apply V040 after V039, redeploy.

## 4.30.0 — COST_DB adoptions: the phantom warehouse dies (2026-07-11)

The approved batch from docs/design/COSTDB_RECONCILIATION.md — R1 plus the
quick wins. R3 (storage truth pass) and R4 (client-app cost lens) are queued
behind Compare Phase 2.

- **R1 / V039 (`V039__pseudo_warehouse_filter.sql`).** Accounts emit a
  CLOUD_SERVICES_ONLY row in WAREHOUSE_METERING_HISTORY (WAREHOUSE_ID = 0,
  compute = 0) for cloud services consumed outside any warehouse. Our fact
  loader ingested it: a phantom ALFA "warehouse" — 100% idle in the advisor
  with nothing to suspend, a chargeback-unmapped row, a movers/Compare/boss-
  chart slice. SP_LOAD_HOURLY_FACTS is re-derived VERBATIM from V002 plus
  exactly one predicate (WAREHOUSE_ID > 0 — the docs-sanctioned filter
  COST_DB carried and we missed); equality-locked in tests. Same filter in
  every live WMH builder (daily credits, window-vs-prior, CS ratio, idle,
  sizing, hourly activity, 13-month monthly) and backfill_365.sql (the
  loader mirror). Phantom rows deleted from FACT_WAREHOUSE_DAILY and the
  efficiency mart; eff-mart READERS filter by name until the V27-family
  loader's next planned re-derivation (that proc pair is equality-locked —
  not churned for one row a day).
- **R2.** Per-warehouse dollars say the quiet part: chargeback KPI help and
  the attribution note now state that warehouse totals include unadjusted
  cloud-services credits, with the account-level rebate on Spend.
- **R5.** `_categorize` gains OPENFLOW_COMPUTE_SNOWFLAKE (Serverless) and
  HYBRID_TABLE_REQUESTS (Storage) — Spend's "Other" stays honest.
- **R6.** The ELEVATED cloud-services drill gains a by-QUERY_TYPE cut
  (metadata storms — SHOW/DESCRIBE floods — visible beside compile-heavy
  families). Budget: spend.py 8→9, justified in the pin.
- **R7.** Optimization gains a toggled per-table automatic-clustering scan
  (serverless reclustering credits + TB reclustered — the classic silent
  burner). Budget: optimize.py 2→3, justified.
- **R9.** Contract & Forecast opens with the calendar-year strip: YTD billed
  credits + straight-line projected year total at trailing-30d burn, labeled
  as such (new unclamped fact_daily_spend_year builder — bounded_days'
  90-day default would have silently turned YTD into "last 90 days").
- Canaries for all three new builders; 10 new locks including the V039
  derivation-equality law.

Deploy: apply V039 after V038, redeploy. The phantom disappears from every
panel on the next loader run.

## 4.29.0 — V038: the savings ledger books itself (2026-07-11)

Owner, looking at an empty ledger: "how can we automate the savings ledger
— I don't think anyone will use this. I'm not even using it." Root cause:
booking required executing fixes THROUGH the app, and real changes happen
in Snowsight. Detection already existed — the V024 warehouse-change scan
sees every setting change within a day and measures 14 days of before/after
actuals. V038 connects the two:

- **SP_LEDGER_AUTOBOOK + TASK_LEDGER_AUTOBOOK** (chained after the daily
  change scan): a detected cost-lever change (AUTO_SUSPEND down, SIZE down,
  MAX_CLUSTERS down, SCALING_POLICY STANDARD→ECONOMY) books an ESTIMATED
  item at $0 the day it is seen — no invented numbers, the item is
  pipeline visibility. When the registry verdict lands (~14d), the item
  settles ITSELF: VERIFIED with measured credits/day delta x rate x 30
  ($5/mo noise floor, rate from SETTINGS) or REJECTED with the measured
  evidence in the note. Forward-only — settled items never rewrite.
  VERIFIED_BY = 'AUTO:TASK_LEDGER_AUTOBOOK' keeps auto and human verifies
  distinguishable. Dedupe via new SAVINGS_LEDGER.SOURCE_CHANGE_ID.
- **The migration runs a first pass on apply** against the registry's
  existing 90 days — the ledger stops being empty the moment V038 lands.
- Ledger UI reframed: SOURCE column (auto/manual), caption says the ledger
  books itself, manual add stays for one-offs (index rebuilds, contract
  renegotiations — things no scan can see). Monthly TASK_VERIFY_SAVINGS
  continues to own app-booked auto-suspend estimates; autobook items are
  settled by their own 14d verdicts.

Deploy: apply V038 after V037, redeploy. Nothing else to do — that is the
point.

## 4.28.1 — Compare survives an empty side (2026-07-11)

- Live crash (owner screenshots, both trailing pairings): `pct_delta`
  returns None when the B side is zero — its documented contract — and the
  new KPI delta chip formatted it (`NoneType.__format__`), with the same
  landmine in the volume-shape `round()`. Chips now say "no B-side data";
  the volume table carries a blank delta. Regression locks forbid
  formatting or rounding `pct_delta` output directly in compare.py, and a
  behavioral test pins the empty-B path.

## 4.28.0 — V037 + Compare Phase 1: which warehouses did it? (2026-07-11)

The spreadsheet-killer, built to the design doc (docs/design/COMPARE_MODE.md,
owner decisions 2026-07-11: V037 yes, last-full-month default, promotion
channel authoritative).

- **V037 (`V037__pattern_env_grain.sql`)** — MART_PATTERN_COST_DAILY v2:
  DATABASE_NAME joins the grain while the mart is days old (the Compare env
  lens's measured-$ source), and the USERS metric becomes honest (Codex r11
  #9): V036 stored per-warehouse daily distincts summed and readers took the
  max day — neither is window-distinct. The mart now stores a mergeable
  HLL_ACCUMULATE state; readers HLL_COMBINE + HLL_ESTIMATE, so USERS is a
  true approximate distinct over any day range. CREATE OR REPLACE + fresh
  30d backfill (cheaper than in-place while the mart is young). V030 shape
  law throughout; guard = declared-exception pattern.
- **Compare tab on Cost & Contract (Phase 1, period vs period).** Pairing
  picker: last full month vs prior (default), trailing 7d/30d vs prior —
  the current partial day/month is NEVER a side by default; the labeled
  escape hatch pairs MTD against the same day-count of the prior month
  (equal-length windows or nothing, clamped at short months). Paired KPI
  strip with the r11 #12 corrected grains: warehouse spend from
  FACT_WAREHOUSE_DAILY (exact, company-scopable), queries/fail-rate/queued
  from FACT_QUERY_HOURLY (company-scoped), account-billed from
  FACT_METERING_DAILY (account-wide, labeled so). Warehouse movers
  (paired_bars + table, ranked by |Δ$|), pattern movers (measured
  attribution $ per hash from the V037 mart — new-in-A patterns show B=$0),
  volume shape table. One parallel batch (Brief pattern) with serial
  fallbacks; period math in app/logic/compare.py (pure, boundary-tested).
- **charts.paired_bars** — two-side grouped bars, side B dimmed gray.
- Live-scan budget: compare.py pinned at 0 (mart/fact-only forever).
  Compare canaries anchor to recent windows (the fixed-date lock holds).
- **Design doc updated:** Phase-1 KPI grain table corrected (r11 #12);
  Phase 2 env axis recorded as the ORDERED promotion lane
  DEV→UAT→PREPROD→PRD (r11 #17) — the MGM=PREPROD reconcile lands there,
  replacing the binary PROD/NONPROD classifier (V023 seed stays in sync).
- 13 new locks/behavior tests (test_v037_compare.py): pairing edges incl.
  year boundary + Feb clamp, partial exclusion, grain assertions, injection
  + date validation, V037 guard/HLL/MERGE-key locks, budget-0 pin.

Deploy: apply V037 in Snowsight (after V036), redeploy the app. The
backfill runs inside the migration; the Compare tab and pattern panels
light up as soon as it lands.

## 4.27.0 — Codex r11 fix-first: the batch tells the truth about who failed (2026-07-11)

- **Boss chart coverage gate (r11 #2).** `run_mart_first` gains an optional
  `mart_accept` predicate: a usable-but-thin mart can now defer to the live
  view instead of suppressing it. Overview's monthly-spend chart requires
  12 distinct months from the accruing efficiency mart, else the 13-month
  WAREHOUSE_METERING_HISTORY view serves; a broken predicate accepts the
  mart (never breaks a page); if live cannot answer, the partial mart still
  beats an empty panel. Caption states the rate basis: "Dollars at today's
  $X/credit" (r11 #11).
- **Quarantine only confirmed failers (r11 #4).** A submission failure at
  batch member N used to stamp N and every UNSUBMITTED member with the same
  error — innocent keys got quarantined into solo-run purgatory.
  `_BatchPartial` now carries `pending` (unsubmitted ≠ failed): only the
  member whose submit raised is quarantined; pending members take the
  normal run() fallback untainted.
- **Quarantine heals itself (r11 #5).** A quarantined key's next clean solo
  run removes it from quarantine — it re-batches on the following rerun.
  Salt refresh is no longer the only exit. (The suggested reorder of
  healthy-batch-before-singles is a no-op: both paths complete within one
  run_batch call before anything renders — declined as such.)
- **Prefs attempts only post-identity (r11 #3).** `_apply_default_landing`
  returns without spending an attempt until `_ow_current_user` is hydrated
  — disconnected reruns no longer burn the 3-attempt budget, and the
  pre-identity read (the r9 #1 anonymous-scope cache leak class) cannot
  happen at all.
- **Environment filter stops lying (r11 #1, honesty half).** The
  Environment picker only ever narrowed the Database list, but the scope
  chip and the Scope stat claimed it scoped results. Chip and stat dropped;
  the picker's help now says exactly what it does. The MGM=PREPROD lane
  reconcile (companies.py + the V023-seeded volume scope) rides with the
  Compare env lens, where environments get real grain — the binary
  PROD/NONPROD classifier is replaced there anyway.
- **Canary gaps are declarative (r11 #7).** GAP status now requires the
  entry to be listed in `EXPECTED_GAPS` (the five cortex.* canaries — the
  002139 subscription class). An absent CORE object (mart.*, chargeback.*)
  is drift and FAILS loudly; fresh installs read Migrations & freshness for
  the calm view.
- **Behavioral failure-path tests (r11 #14).** tests/test_codex_r11.py:
  fake-session batch submission/gather failures, quarantine membership,
  rehab on clean solo run, mart_accept fall-through/keep/never-break,
  identity-gated prefs attempts, EXPECTED_GAPS hygiene. Two r10 source
  locks updated WITH the semantics they pin.
- **Repair:** v4.26.2's copy-pass commit shipped `operations.py` with its
  last two dispatch lines truncated (mount cp mid-line truncation that
  still compiled — Pipeline SLA / Release compare sections unreachable).
  Restored; ruff (F821) joins the gate so compile-clean truncations get
  caught before commit.
- r11 disposition elsewhere: #9 pattern USERS grain + #12 Compare doc
  grains + #17 promotion-lane framing land with V037/Compare next session;
  #10/#18/#19/#20 already on the roadmap; #6 #8 #13 #15(bridge) #16
  deferred with reasons (see session notes).

## 4.26.2 — the copy polish pass (2026-07-11)

Owner ask: "people understand what they are seeing but not bogged down
by unnecessary wording." Copy only — no SQL, keys, thresholds, or layout.

- Sweep of every page's captions, help text, info/empty states against
  six editorial rules: captions carry the verdict in one sentence where
  possible; detail moves to help= tooltips; plain words over jargon
  ("reads almost the whole table", not "missing pruning"); honesty
  labels (measured vs allocated, partial-not-a-drop, floor-not-census,
  account-level scope) all survive, said in fewer words; changelog
  archaeology dropped from user-facing copy ("(V021)", "retired at
  V034", "r5 #4 decision", "bulk closes used to skip this" and kin —
  version references stay only on Admin setup hints, where admins act
  on them); personality phrases kept only where they are the clearest
  way to say the thing ("the telemetry picks, not opinions" and
  "nothing groups silently" stay, "the fatigue denominator done right"
  and "revoke fodder" go).
- Files touched: overview, spend, unit_costs, contract, operations,
  security, alerts, control_room, admin. Already at standard, untouched:
  brief, cost, optimize, ai_chargeback, components, ai_panel, charts,
  main.
- One caption lock updated WITH its copy (test_live_round8: break-glass
  panel now pinned to "no alert fires on admin-role use").

## 4.26.1 — the pain table read correctly (2026-07-11)

- Today's Cost & Contract slow keys (reclaim 25s, prune/cachehit 15s) are
  the TOGGLE-GATED deep scans — operator-invoked forensics, slow by
  nature and by design (V038 on-demand pattern). Marting rarely-clicked
  scans is poor value; what they lacked was the last-runtime hint the
  other heavy toggles got in v4.20. Both toggles now show it, so the
  next click comes with an expectation instead of a surprise.

## 4.26.0 — V036: the boss chart + measured pattern costs (2026-07-11)

- **Monthly spend by warehouse** (Overview): stacked monthly bars, company-
  scoped, in-flight month dimmed (partial, not a drop), MoM delta on the
  last full month. Mart-first over MART_WAREHOUSE_EFFICIENCY_DAILY as it
  accrues history; 13-month WAREHOUSE_METERING_HISTORY live fallback until
  then (overview budget 1 -> 2, labeled). Better than the POC's version:
  scoped, sourced, and honest about the partial month.
- **Repeated patterns — the silent spend** (Cost -> Unit costs): measured
  QUERY_ATTRIBUTION_HISTORY compute per parameterized hash (V036 mart,
  daily 3d increment + 30d backfill in-migration), $0.01 floor, sample
  text joined from the family mart. The POC estimates; ours bills.
- Telemetry read honestly: SP_LOAD_MARTS_V27 at 1,144s was the ONE-TIME
  90d backfill (RUNS=1), not a regression — watch the hourly cadence
  number instead. Lock-mart MERGE averaged 84 GB because the 45d backfill
  dominated; if tomorrow's 3d increments stay heavy, the increment window
  gets bounded next round. Today's slow keys (reclaim/cachehit/prune on
  Cost & Contract) are the next marting targets per the pain table.

## 4.25.0 — Codex r10 fix-now batch (2026-07-11)

- **Typed error kinds (#4 — fixed a v4.23.2 bug)**: the friendly error
  formatter had erased the marker text my canary GAP classifier searched
  for, so missing objects still read FAIL. Errors are now classified from
  the RAW exception into QueryResult.error_kind (absent / unknown_function
  / timeout / other); the canary, the AI tab, and probe reads all consume
  the kind instead of parsing prettified strings.
- **Prefs bootstrap commits on success (#1)**: a failed first read retries
  (3 tries) instead of silently skipping your saved landing all session.
- **Batch submission harvest (#3)**: if submission dies at member N, the
  already-submitted jobs' results are collected instead of discarded —
  those queries run server-side either way; dropping handles re-paid them.
- **Session quarantine (#2)**: a key that fails inside a batch runs solo
  from then on while the healthy remainder re-batches smaller and CACHES —
  one persistently broken query no longer makes siblings re-execute every
  rerun. Manual refresh clears the quarantine.
- **Tail-aware row caps (#6)**: only a TRAILING limit counts as the outer
  bound; a subquery's LIMIT no longer disables the cap. Executable tests.
- **Query drill always available (#14)**: manual ID entry no longer
  disappears when the candidates table is empty.
- Already true, verified: #11's cheap half (the fact-window p95 is labeled
  as the PEAK hourly p95 in code and UI). Declined: #7 SiS cancel loop
  (warehouse timeout per RUNBOOK Section 20 is the designed backstop), #12
  salt generations (TTL-bounded; clear() would nuke every user), #9
  sampling weights (the floor is deliberate and labeled). Design-first
  next: #17 Compare mode, then #15 Action-Queue workbench, #19 blast
  radius, #18 predictive SLA; #8+#11-full = mart wave 3 note
  (APPROX_PERCENTILE_ACCUMULATE makes true percentiles buildable).

## 4.24.0 — Codex r9: the correctness batch, four real bugs (2026-07-11)

- **Pre-identity pref caching (#1, real)**: the USER_PREFS landing read ran
  before current_role() hydrated the cache scope, so it cached under the
  anonymous scope — and with SQL text identical across users, one user's
  prefs frame could serve another in-process. Identity now hydrates first.
- **Empty mart revived the giant scan (#2, real)**: run_mart_first treated
  a HEALTHY empty mart as a miss, so "no lock waits" re-paid the 46-56 GB
  live scan to confirm an answer we held. New opt-in empty_is_answer=True
  on the lock panel; marts with young-coverage ambiguity keep the default.
- **One failure re-ran the whole batch (#3, real)**: _execute_batch threw
  away already-computed survivor frames. _BatchPartial now carries them out
  of the cached fetcher (a partial batch is STILL never cached) and only
  failed members retry through run(). History lock evolved with the note.
- **Racy cache-hit sentinel (#4, real)**: _FETCH_MISS was a module dict
  shared across concurrent session threads; now a ContextVar. The batch
  wall-time-split telemetry compromise stays (schema change, deferred).
- **Failed settings reads cached defaults for 5 min (#5, real)**: the
  settings frame cache now raises on not-ok (cache_data never caches a
  raise), so a transient failure costs one render, not five minutes.
- Adoption metric counts first-paint page_visit rows only, plus WAU (#9);
  source-badge microtext 9px -> 11px (#18).
- Deferred with reasons: #6 Overview batching (absent from the slow board —
  telemetry picks batching targets); #7 lazy expanders, #10 SECTION+flow,
  #11 central interaction logging, #19 chart semantics, #17 aria pass
  (next polish round, together); #12 Action Queue workbench and #13
  maintenance windows are feature rounds that deserve design docs; #14
  shareable links queued with breadcrumbs; #20 behavioral tests adopted
  narrowly as this round's locks. Declined: #8 per-statement query tags
  (an ALTER SESSION per fetch doubles statements; key-level telemetry
  already attributes cost); #15 per-panel filter muting (account-level
  labels + legend already declare scope); #16 sticky strip (fights SiS
  DOM under Streamlit 1.45 — revisit on upgrade).

## 4.23.2 — the Cortex Code 002139, traced to us after all (2026-07-10)

- Joe ran the QUERY_HISTORY diagnostic and the caller was the dashboard:
  our AI-chargeback reads of ACCOUNT_USAGE.CORTEX_CODE_*_USAGE_HISTORY.
  We never name SYSTEM$GET_CORTEX_CODE_CLI_SUBSCRIPTION, but those views
  call it INTERNALLY; without a Cortex Code subscription the function
  does not exist (002139), so our query throws it for us. The tab now
  shows a truthful "not available in this account/region" note instead
  of a red box, both reads are probe=True (no error-log/failure spam),
  and probe learned the gated-view class ("Unknown function").
- Canary sweep: feature gaps and pre-migration objects now report GAP,
  not FAIL — absence is not drift; the sweep table stays the alarm and
  stops double-logging to APP_ERROR_LOG.

## 4.23.1 — the Flyway probe stops crying wolf (2026-07-10)

- The Admin Flyway panel always degraded honestly ("Flyway not detected"),
  but the probe's failure still landed in APP_ERROR_LOG and the failed-
  fetch telemetry on every visit — a recurring 'flyway_schema_history does
  not exist' error for a table we know does not exist yet. run() gains
  probe=True for optional-object reads: expected absence is neither
  error-logged nor counted as a failure; every other error on the same
  read still records normally. The panel lights up on its own when
  Flyway lands, exactly as before.

## 4.23.0 — Codex r8 adopts: drill-downs, diagnostics, consistency (2026-07-10)

- **Tuning targets drill down** (#1): click a pain-ranked page on Admin ->
  Performance and the slow keys behind it appear (7d persisted telemetry).
- **"Why stale?" diagnostics** (#16): stale freshness rows now map to their
  likeliest cause — never-backfilled (with the RUNBOOK call), the last
  matching loader error, or a suspended-task hint. This week's deploy-gap
  archaeology, turned into a panel.
- **Lock-wait spike watch** (#13): Control Room flags objects locking >=3x
  their prior 6-day baseline; quiet pre-V035 and when calm. The Operations
  panel also names its source now (#14, house result_caption).
- Consistency set: run_batch callers drop the dead `or {}` (#3 — the
  contract guarantees a dict since v4.20); the new lock readers use the
  shared sql_literal helper (#15 — older locked builders keep their
  pinned text); KPI badges moved from inline CSS to .ow-src-badge theme
  classes (#8).
- Declined with reasons: #2 per-key fallback evidence already exists (the
  bfb: key prefix in persisted telemetry names the failing member); #4
  fleet-p95 toggle hints cost a query to decorate a caption; #7 the legend
  already sits in the topbar; #9 the 8 styled_table holdouts are deliberate
  and locked at <=8; #12 blocker/waiter attribution waits for the mart to
  accumulate real data; #18 AppTests cover smoke and snapshot infra costs
  more than it catches; #20 evidence bundles belong to incidents (wave 2),
  not a parallel export. Deferred: #5 workload split (needs builder recon),
  #6 badges-everywhere (opt-in exists; wire per-panel as touched),
  #10/#11/#17 stay queued per r7 reasoning.

## 4.22.1 — V035 guard fix, owner-diagnosed in Snowsight (2026-07-10)

- **V035's guard used invalid scripting** — RAISE only accepts a DECLAREd
  exception name; the inline RAISE EXCEPTION (code, msg) form fails to
  compile ("unexpected (" — the paren after EXCEPTION). Joe hit it live,
  moved the exception into DECLARE, and it worked; the repo file now
  matches that fix (the house pattern every applied migration V001-V034
  already used — V035 alone drifted). If V035 already applied with the
  hand-fix, re-running the fixed file is safe but unnecessary.
- New repo-wide lock: no migration may ever contain "RAISE EXCEPTION (" —
  the sqlglot gate can't see inside $$ bodies, so this class gets its own
  gate.

## 4.22.0 — V035: page views never scan LOCK_WAIT_HISTORY again (2026-07-10)

- **The lock-wait mart** (live finding, owner's Heaviest-queries panel):
  the contention reads were scanning 46-56 GB at 74-259s per page view —
  the single heaviest thing the app did. MART_LOCK_WAIT_DAILY now carries
  day x db x schema x object x lock-type (with COMPANY via the object's
  database), loaded by TASK_LOCK_WAIT_DAILY on a 3-day increment after the
  daily fact load; the migration backfills 45 days once. The panel reads
  the mart first and keeps the live scan only as the pre-V035 fallback.
- Screenshot triage, recorded honestly: everything else on the board is
  the deploy gap, not new bugs — the 08:44 loader error is V029's shape
  (V030 not applied), the compile_fams compilation error is the alias
  shadow fixed in v4.13, the Brief 8.9s p95 predates the batch split, and
  unused_roles/gov_counts burn 12-32s live because their marts never fill
  while the loader fails. One deploy clears the set.
- Admin's 0% cache / 30s p95 deliberately NOT tuned this round: those
  fetches queue behind the lock scans and loader retries on the same
  warehouse — re-measure after V030-V035 land before touching code.

## 4.21.0 — second-tier batch: environments, badges, the queue (2026-07-10)

- **Lock waits name their environment** (owner ask): DATABASE_NAME and
  SCHEMA_NAME join the contention table so a wait on PC_PAMODIFIER in SAN
  reads differently from the same table in PRD. Grain widened to match;
  never-acquired-first ranking unchanged.
- **KPI cards carry source badges** (r7 #12, the deferred lead): a tiny
  mart/live/stale chip inside the card — trust at eye level instead of a
  caption below the fold. Wired on the Brief's money and criticals cards
  first; any kpi_row item can opt in via "badge".
- **Admin ranks the next tuning targets** (r7 #3, honest version): pain =
  p95 x slow-fetch count from the existing per-page telemetry frame, top
  five — the telemetry picks, not opinions; no speculative "likely fix"
  text, because guessing fixes without reading the code is how reviews
  go wrong.
- Still queued with reasons: prior-period deltas on more charts (#6,
  per-chart work), breadcrumbs (#7, needs UX design against 1.45
  constraints), more heatmaps (#9, where data supports them).

## 4.20.0 — polish batch + partial-success batching (2026-07-10)

Codex r7 adopts (owner-approved, including #1 over the evidence gate):

- **run_batch is partial-success aware**: the cached batch unit stays
  all-or-nothing (failures are never cached), but a failed batch now
  retries PER KEY through run() — individual caching, telemetry and error
  isolation per query; one bad member no longer drags its siblings back
  to serial-cold. Always returns every key; callers unchanged; the
  batch_fallback evidence stream survives.
- **Heavy toggles price themselves**: dormant-user, right-sizing and
  repeat-query toggles show the last observed runtime this session
  before you click.
- **The proc trend is discoverable**: click a $/call leaderboard row and
  the trend panel prefills with that procedure.
- **Legend popover** beside Views: severity colors, mart/live/stale
  source labels, measured/allocated/ESTIMATED-vs-VERIFIED money
  semantics — for operators who didn't sit through the design reviews.
- **30 raw st.dataframe calls migrated to styled_table** (shared
  formatting, pinning, status colors); the 8 holdouts carry bespoke
  kwargs and are deliberate.
- **Docs pass**: CHANGELOG date drift fixed (07-11 -> 07-10), FEATURES.md
  gains the since-v4.9 capability table. Deferred with reasons: KPI
  source badges (#12 — needs kpi_row surgery, next batch), attention
  dashboard (#8 — Control Room IS that view), scorecards/campaigns
  (#10/#11 — owner registry first).

## 4.19.0 — the Brief gets fast + the everything-check (2026-07-10)

App-only release:

- **Brief: ten serial reads -> two tier-grouped batches** (fleet board:
  p95 8.9s, 18/19 fetches slow — unacceptable for the exec page). One
  live batch (health strip, incidents, alerts, actions) + one recent
  batch (exhaustion, savings, app cost, sparkline, digest), serial
  per-query fallbacks unchanged, honesty contract and company scoping
  intact, budget still zero live scans.
- **Full repo sweep came back clean**: migrations V001..V034 contiguous
  with sequential guards; all five replaced procs owned by their latest
  migration (derivation chains intact); 135 canaries; every live-scan
  budget exactly at its pin; zero correlated-subquery or alias-shadow
  landmines in any builder (the sweep's four hits were a docstring and
  three cross-scope CTE false positives — verified by eye).

## 4.18.1 — live round 9: the scorecard's CHANGE_SOURCE runs (2026-07-10)

- **Warehouse-setting-changes panel crashed** ('unsupported subquery type'):
  the v4.16 CHANGE_SOURCE derivation used a CORRELATED aggregate subquery
  (outer w.CHANGED_BY inside a MAX over SETTINGS) — Snowflake rejects that
  shape at runtime and the sqlglot gate can't see it (semantic, and app
  builders aren't gated). Fixed uncorrelated-by-construction: the
  DEPLOY_ACTORS setting resolves once via CROSS JOIN, POSITION runs per
  row. Empty actors still reads honestly as MANUAL/UNKNOWN. Lock forbids
  the correlated shape returning. Lesson recorded with the alias-shadow
  rule: correlated scalar subqueries in select lists are a runtime
  landmine — resolve settings once, join them in.
- Noted from the owner's fleet graphics: Brief p95 8.9s (18/19 fetches
  slow) is the next tuning target; MART_SECTION_DECISION_CURRENT_FLAT in
  the volume-drop panel is the PREVIOUS app's orphaned mart (shared
  schema), not an OVERWATCH loader failure — drops with OVERWATCHV2.

## 4.18.0 — trend one procedure, by name (2026-07-10)

App-only release (owner ask: "can I enter it myself" — yes):

- **Cost & Contract -> Unit costs -> "Trend one procedure"**: type a proc
  name (bare or db.schema-qualified; bare matches any qualification via
  the suffix arm) and get its daily measured $ — total, calls, $/call,
  fails, attributed-calls diagnostics — as the standard bars + 7d-average
  chart plus detail table. Same REGEXP extraction and ROOT_QUERY_ID
  rollup as the $/call leaderboard, so the two always agree; honors the
  page filters (company/database/schema/window) per the triage-filter
  law. On-demand live scan (bounded, cached hourly) — occasional-use
  cost profile, no mart needed.
- Existing answers for context: leaderboard = window totals for EVERY
  proc; Price-a-CALL = one run's children; change-impact = before/after
  around an ALTER. This closes the by-name daily-trend gap.

## 4.17.0 — V034 + live round 8: delivery scoping and the triage-filter law (2026-07-10)

Migration V034 (apply after V033):

- **Teams delivery is ALFA-only for now** (owner decision): ALERT_ROUTES
  gains COMPANY_FILTER ('ALL' default; every existing route flips to
  'ALFA'); sender v4 carries a route's company plus account-level events —
  the open_alert_events convention applied to delivery. The expiry
  watchdog learns the same policy, so company-filtered-out events can
  never spam undelivered_expired. In-app visibility untouched. Derived
  VERBATIM from V026's sender with five enumerated edits, revert-locked.
- **Incidents honor the triage filter everywhere** (live round 8: the new
  section showed both companies under ALFA): open_incidents,
  incident_proposals and incident_metrics all take company (company rows +
  account-level, keys scoped); the Brief chip reads the page filter; and
  the declare flow now matches members on the proposal's COMPANY as well
  as its dedupe family — both companies share rule families, so
  family-only linking could have attached Trexis alerts to an ALFA
  incident.
- **The triage-filter law, recorded**: every new metric/panel takes the
  page filters (company at minimum) at birth — locked per-surface and
  written into the SQL discipline notes so review catches it before
  production does.
- **SEC_BREAK_GLASS_USE retired** (owner: admins know what they are
  doing): rule row deleted, lingering open events resolved EXPECTED,
  activity panel stays as evidence. Muted since V025; gone at V034.
- Tests: tests/test_live_round8.py. 623 green.

## 4.16.0 — V033: attribution + Flyway-readiness + incidents SOP (2026-07-10)

The in-the-meantime batch (migration V033, apply after V032):

- **Who made each warehouse change**: WAREHOUSE_CHANGE_REGISTRY gains
  CHANGED_BY via SP_CHANGE_ATTRIBUTION (hourly, chained after
  TASK_LOAD_HOURLY): best-effort join to the successful ALTER in the 65
  minutes before the snapshot saw the change — evidence, not lineage (the
  V010 rule). MANAGED vs MANUAL derives AT READ TIME from the new
  DEPLOY_ACTORS setting (empty today, so everything honestly reads MANUAL
  or UNKNOWN); the scorecard shows both columns with a caption. The
  OPS_UNMANAGED_CHANGE rule deliberately does NOT ship — an alert with no
  populated actors would be decorative config (review #8); it arrives with
  its scan arm the day DEPLOY_ACTORS is populated.
- **Flyway-readiness**: Admin -> Migrations reads flyway_schema_history
  when it exists (quoted-lowercase, deliberately NOT canaried — absence is
  legitimate until adoption) and says plainly which ledger is
  authoritative; docs/FLYWAY_ADOPTION.md is the adoption runbook (service
  user, DEPLOY_ACTORS entry, baseline-at-tip, compatibility replay, what
  changes vs what stays); snowflake/flyway.toml.example ships key-pair
  auth with cleanDisabled.
- **Incidents are documented and visible**: RUNBOOK section 21 (declare /
  auto-declare / close SOP, metric definitions, attribution semantics);
  the Brief gains an Open-incidents chip (guarded — silent pre-V032).
- Tests: tests/test_prep_iac.py (10 locks). 615 green.

## 4.15.0 — V032: the incident object ships (2026-07-10)

Migration V032 (apply after V031, then RE-RUN roles.sql — operator DML
grants on the two new tables):

- **INCIDENTS + INCIDENT_MEMBERS** — permanent, operator-curated, PRESERVED
  in teardown: alerts, task failures, warehouse changes, DDL, deploys and
  remediations under one lifecycle key. Forward-only statuses; reopen is a
  NEW incident carrying REOPENED_FROM (14-day window, owner-set).
- **Three births, none silent**: manual declare (Control Room, DBA-gated,
  generate-then-run — three statements sharing one session-var id, family
  alerts linked without doubling); auto-declare for CRITICALs
  (SP_INCIDENT_AUTODECLARE chained after TASK_LOAD_HOURLY — one incident
  per dedupe family per 24h, never doubles an open family, SETTINGS
  toggle ON per owner decision); INCIDENT_PROPOSALS view (open alert
  families + nearby warehouse changes — suggestions a human confirms).
- **Lineage becomes joins** (Codex r6 #6): REMEDIATION_LOG +EVENT_ID/
  INCIDENT_ID, SAVINGS_LEDGER +REMEDIATION_ID. Additive; history stays
  NULL — no invented lineage.
- **Control Room -> Incidents**: lifecycle KPI strip (open now, incident
  MTTA/MTTR, reopen rate, alerts-per-incident compression,
  change-correlated % — the IaC payoff number), open-incident queue with
  member detail, close flow with root-cause kind + note,
  incident_declare/incident_close usage events.
- The design doc's IS_RERUN scan rider closed as ALREADY-SAFE: rerun rows
  persist RENDER_MS NULL (V027) and OPS_SLOW_RENDER has filtered NULLs
  since V17 — documented in the migration header.
- IaC hooks (deploy members, OPS_MIGRATION_FAILED, OPS_UNMANAGED_CHANGE)
  still SHIP DISABLED until Flyway/Terraform land, per the design doc.
- Tests: tests/test_v032_incidents.py (11 locks). 604 green.

## 4.14.0 — V031: the tuning trio (2026-07-10)

Migration V031 (apply after V030):

- **Change-impact scan v2** (first replacement since V010; median 278s/call
  was the biggest statement family on the shared warehouse). The
  after-window joins now bound to the OLDEST STILL-TRACKING change
  (:trk_lo) instead of a blanket -18d — nothing tracking means near-zero
  scan — and a cheap ILIKE pre-filter runs before the double-REPLACE
  POSITION match, so full-text normalization only touches plausible CALLs.
  Verdict semantics unchanged; derived VERBATIM from V010 with enumerated
  edits, locked.
- **MART_TAG_COVERAGE_DAILY** closes wave 2's last honest non-adoption:
  day x user tagged/untagged exec-time, loaded hourly under the V030 shape
  law (UDF only ever touches a plain column of an aggregated derived
  table). Freshness view re-emitted with the 17th arm; Cost -> Query-tag
  governance goes mart-first with the live scan as labeled fallback
  (tagcov was p95 35.8s live).
- **lock_contention capped at 7 days** (was 14; ~56GB per run on this
  account). Lock triage is a this-week question.
- Tests: tests/test_live_round7.py (derivation, prefilter counts, tag arm
  shape, reader contract, adoption, freshness 17th arm, clamp). 596 green.

## 4.13.0 — V030: the correct loader shape + measured CALL pricing (2026-07-10)

Live round 6. Migration V030 (apply after V029; run the two backfill CALLs
in the file header once for history):

- **Loader fix 2** — V029's MAX() wrap failed differently: COMPANY_FOR_* is
  a SQL UDF that CORRELATES its argument into a subquery, so the aggregate
  landed inside the inlined WHERE ('invalid aggregate function in where
  clause'). V030 uses the bulletproof shape: aggregate first in a derived
  table, UDF applied to a plain column outside — the same reason the other
  seven arms never failed. Derivation lock chain now V027->V028->V029->V030.
- **Posture snapshot completes the governance inputs** (MFA_GAP_USERS,
  BREAKGLASS_GRANTS_30D join the daily 06:30 arm) and the Security first
  paint reads it mart-first — gov_counts topped the fleet slow-fetch board
  (13 hits, p95 12.3s) and now runs only as the labeled live fallback.
- **Unused roles go mart-first** (p95 32s live) via FACT_QUERY_ROLE_HOURLY
  with a coverage guard: a young fact returns zero rows and the live path
  serves — a role used 60 days ago can never be called unused because the
  fact is 3 days old. Activates after the 90d backfill.
- **Price a CALL or session** (owner question: 'three procs in one session,
  no graph id'): Unit costs gains a measured-pricing panel — paste a CALL's
  QUERY_ID or a SESSION_ID; children roll up via
  QUERY_ATTRIBUTION_HISTORY.ROOT_QUERY_ID (no task graph needed), and a
  single CALL also shows the per-child breakdown ('where the money went
  inside this CALL', root's own time labeled). ~6h lag, idle excluded —
  same caveats as the proc leaderboard, stated on the panel.
- **Primary buttons force dark ink across Streamlit markups** ('2 open
  critical(s)' chip and Execute bulk RESOLVE rendered pale-on-pale): the
  accent-pill rule now covers kind= and data-testid variants with
  !important on every descendant.
- Numbering: incident object -> ~V031, owner registry -> ~V032.
- Tests: tests/test_live_round6.py (9 locks incl. the four-link derivation
  chain). 587 green.

## 4.12.1 — V029 hotfix: the loader arms that never loaded (2026-07-10)

Live round 5. Migration V029 (apply after V028; then optionally run the
90-day backfill CALL noted in the file header once):

- **FACT_QUERY_ROLE_HOURLY / FACT_QUERY_SCHEMA_HOURLY never loaded** — the
  V027 loader's COMPANY expressions called COMPANY_FOR_*() on the raw
  column while GROUP BY covered COALESCE(col, 'NONE'), so both arms failed
  hourly since apply (the per-mart EXCEPTION isolation kept the other
  seven loading, and role share / schema summaries silently used their
  live fallbacks — the fallback pattern worked; the facts were empty).
  V029 replaces the proc, derived VERBATIM from V028's with exactly two
  edits: COMPANY_FOR_*(MAX(COALESCE(col, ''))) — the derivation lock chain
  extends V027 -> V028 -> V029.
- **compile-heavy mart reader crashed Cost & Contract** ('aggregate
  functions cannot be nested'): Snowflake resolved the bare RUNS inside
  later aggregates to the SUM(RUNS) AS RUNS alias. Fixed by qualifying
  every column (f.) in family_compile_heavy — and the same latent bug in
  family_repeat_fingerprints, eff_sizing_profile (behind the sizing
  toggle) and ai_costs_by_model before they fired live. New discipline:
  an output alias must never share a name with a column referenced later
  in the same select list; qualified references cannot be shadowed.
- **Multiselect chips are readable** (Alerts bulk picker was a pale wash):
  dark chip, accent hairline, real text color.
- **Heaviest queries gains the date**: START_TIME first column with a
  Started (MMM DD, HH:mm) format, from the same builder.
- Design-doc numbering: incident object -> ~V030, owner registry ->
  ~V031 (V029 became this hotfix).
- Tests: tests/test_live_round5.py (derivation chain, healed arms,
  qualified-aggregate locks, chip CSS, date column). 574 green.

## 4.12.0 — WAVE 2: the marts take over the panels (2026-07-09)

No migration — app release only (needs V027/V028 applied and the loader
tasks running; every adoption degrades to its live builder otherwise).

- **Ten surfaces go mart-first** via the new components.run_mart_first
  helper (mart read under `<key>_fact`, live fallback under the original
  key, source labeled either way): idle advisor, sizing profile and
  repeat-query scan (optimize — the 90s metering x history joins become
  mart reads), compile-heavy families and allocated attribution (spend —
  the mart path dollarizes ALLOC_CREDITS directly instead of share x
  window), role share (chargeback — keeps BOTH the COMPANY column and the
  TRXS role-heuristic guards), task graphs and schema-filtered summaries
  (operations), schema-filtered 24h pulse and the 48h incident timeline
  (control room), AI by function/model (unit costs — FACT_AI_USAGE_DAILY
  unifies Code + Functions, KPI and panel now share one source).
- **Security posture trend** (Codex r6 #15): 90-day direction per metric
  from MART_SECURITY_POSTURE_DAILY under the governance score — unlocks
  at 2+ days of loader history; default metric follows the V028 10-day
  credential bucket.
- **Eight new contract-matched aggregate readers** in mart27_sql (idle,
  sizing, compile, repeat, role share, allocation, schema summary, AI by
  model) + task_graphs gains full filter parity + the timeline reader
  emits the live column contract (AT/EVENT_TYPE/LABEL, account-level rows
  kept). All canaried. Grain caveats ride the source labels (peak-daily
  p95, exec-time hours, day-grain LAST_RUN).
- **Perf budgets**: six more pages pinned in tests/test_perf_budgets.py
  (optimize 2, spend 8, chargeback 5, operations 25, unit_costs 0,
  security 18) — counts only go down from here. Honest non-adoptions:
  tag coverage (needs user grain the family mart lacks) and pruning
  (needs partition stats) stay live by design.
- **Rider panels** (approved r5, refined r6): Alerts -> History gains
  Delivery health (events delivered, p50/p95 raise->send latency,
  undelivered criticals 30m+, route failures with the RUNBOOK 19 pointer)
  and Alert fatigue (events/week per rule, ACTIONED/NOISE/EXPECTED mix,
  UNTAGGED closes, dedupe repeats). Admin -> Performance gains per-page
  fleet telemetry from the V027 rider (p95, cache-hit % — labeled as a
  floor over persisted fetches — batch size, truncation), usage events by
  EVENT_KIND, and the remediation acceptance funnel (executed/copied/
  failed -> estimated/verified/rejected + verified $, audit rows only).
  Overview promotes forecast quality to a page-level readout (per-engine
  ±MAPE, most-reliable engine named) with the monthly evidence kept in
  the expander.
- **Bulk RESOLVE now requires a resolution kind** (r6 #11, verified: the
  single flow forced it since V021, bulk skipped it — untagged closes
  fell out of the precision score). **Reverse guidance** (r6 #18):
  remediation.reverse_hint at the resize and closed-loop exec sites —
  points at WAREHOUSE_CHANGE_REGISTRY for the prior value and
  REMEDIATION_LOG.STATEMENT_SQL for what ran, never invents values.
  **Usage events**: alert_ack / alert_resolve (single + bulk) and
  remediation_exec now log through log_ui_event (r6 #7).
- Tests: tests/test_wave2_adoptions.py (14 locks) +
  tests/test_wave2_riders.py (12 locks). 568 green + floor leg.

## 4.11.0 — V028: live round 4 — replay scope, 10-day creds, readable trends, driver inventory (2026-07-09)

Migration V028 (apply after V027, then validate.sql — expects V001..V028):

- **Credential expiry policy: 30d -> 10d** (owner decision). One UPDATE to
  ALERT_CONFIG.THRESHOLD_NUM (the scan reads it at runtime, since V009) +
  the posture mart bucket follows (metric EXPIRING_CRED_10D). The bucket
  ships as a replacement SP_LOAD_MARTS_V27 derived VERBATIM from V027 with
  exactly two edits — a lock asserts the equality so the copies can't
  drift. App side moves with it: Security panel (10-day horizon), export
  pack sheet, governance counts + deduction message, canary.
- **Day replay now honors the company filter on every metric** (live
  finding: Trexis rows under an ALFA replay). day_spend_movers /
  day_activity / day_task_failures / day_alerts all take company —
  baselines scoped too, alerts keep account-level rows (open_alert_events
  convention); both batch and serial call sites pass the scope and the
  caption says so.
- **Spend trend redesigned** (owner: "not sure what they mean… people will
  ask", twice). Daily bars + 7-day average line instead of the gradient
  wash; the newest day renders dimmed with a caption naming the 24h
  metering lag (partial, not a drop); the forecast band rectangle is gone
  — the Projected month-end KPI already carries the range. Caption states
  window total + week-over-week pace.
- **Security Changes redesigned**: kind-stacked daily bars (create /
  alter / drop / grants) with a statements-by-user bar beside it — the
  chart now answers "what kind" and "who", not just "how many".
- **Client driver inventory** (Security -> Clients, owner ask): driver +
  version parsed from SESSIONS.CLIENT_APPLICATION_ID, PROGRAM from the
  client-reported CLIENT_ENVIRONMENT (VS Code/DBeaver report; many ODBC
  tools like Erwin don't — labeled honestly), users/sessions/first/last
  seen, and BEHIND vs the newest version of the same driver seen in the
  account (padded segment compare, so 3.10 > 3.9). CSV export; canaried.
- README migration list de-duplicated (V021-V026 appeared twice) and both
  install lists gain V027/V028. RUNBOOK: spend-trend + Security sections
  refreshed, new §20 on the SiS 600s statement-timeout restart loop (the
  p95 601s / 33-fails signature) and idle-cost bounds.
- Tests: tests/test_live_round4.py (12 locks incl. the V027/V028 proc
  equality); spend-trend locks in test_ui_round4/test_stress updated to
  the bar design. 542 tests green.

## 4.10.0 — V027: the mart family ships (2026-07-08)

Migration V027 (apply after V026, then re-run roles.sql + validate.sql):

- Nine scheduled marts per the approved design: warehouse efficiency,
  query families (top 2000/day by exec time), role-hour + schema-hour
  query facts, cost allocation (exec-time share of each warehouse-hour,
  four dimensions), task-graph daily, security posture history, 48h
  incident timeline, AI usage (Cortex Code per user + Functions per
  model).
- ONE loader, SP_LOAD_MARTS_V27(scope, days_back): hourly leg chained
  AFTER TASK_LOAD_HOURLY, daily leg AFTER TASK_LOAD_DAILY; per-mart
  EXCEPTION isolation (one mart's source drift never starves the rest);
  MERGE-idempotent on every grain; the migration runs a first fill so
  panels aren't empty until the next task tick. Backfill calls the SAME
  proc with big windows (one loading codepath).
- MART_SOURCE_FRESHNESS gains all nine arms — the freshness board and
  stale labels cover the new marts unchanged.
- Telemetry rider: APP_QUERY_TELEMETRY + CACHE_HIT (real detection via
  the fetcher-body sentinel, not an elapsed-ms guess), SQL_HASH,
  BATCH_SIZE, TRUNCATED; APP_USAGE + EVENT_KIND/IS_RERUN with sampled
  (10%) rerun rows (RENDER_MS NULL so the first-paint p95 sentinel stays
  honest) and interaction events (saved_view_apply, csv_export via
  components.log_ui_event). App inserts degrade gracefully pre-apply.
- Readers for all nine marts (app/data/mart27_sql.py), all canaried.
- WAVE 2 (deliberately separate): panel adoptions go fact-first once the
  marts hold data — adopting before data exists only exercises fallbacks.

## 4.9.1 — visual pass (Codex round 4, Streamlit-reality-checked) (2026-07-08)

Eleven adopted, four declined with rationale, five deferred. Streamlit 1.45
constraints shaped the calls: no sticky positioning, no side drawers.

- FIXED: the spend-trend area gradient had both stops at offset 0.0 — a
  flat wash that never faded (Codex caught a real rendering bug). Now a
  transparent-floor -> accent fade.
- KPI rows cap at four cards and wrap (five-up rows cramped laptops).
- Alerts KPIs are severity-colored (critical=red rail, high=amber) and the
  bulk-execute button is primary — faster reads under pressure.
- The warehouse/user/schema contains-filters collapse into "More filters",
  auto-expanded whenever one is active so a live filter can never hide.
- Compact density toggle (Views popover): tighter cards/tables for triage
  screens; hierarchy and colors unchanged. Session-scoped v1.
- Calm surfaces: hover motion removed (border/shadow response only),
  radii tightened 12/16 -> 8/12, heading letter-spacing zeroed (the
  uppercase kicker tracking stays — that's a label convention).
- Budget line on the spend trend now labels itself without hover
  (screenshots and phones); 💾 emoji retired from the Views control.
- Scope (company · env · days) rides the persistent status bar — the
  1.45-compatible answer to "sticky filter bar".
- Declined: freshness-caption reduction (that trust surface caught a live
  regression; it gets quieter, not fewer), drawer detail views (no side
  drawers in Streamlit; dialogs hide the list being triaged), storm view
  (already exists — Alerts "Group by rule"), broad semantic recolor (cyan
  is the deliberate data brand; status colors already live where status
  does). Deferred: panel-shell component, small multiples, DAG polish,
  Brief redesign (it already has the Now/Fires/Asks bands).

## 4.9.0 — Teams-safe delivery (V026), docs sync, mart-family design (2026-07-08)

- FIXED (V026, sender v3): webhook payloads are JSON-escaped before the
  integration body template splices them into a JSON string — raw newlines
  (the LISTAGG separator + prefix) and quotes in alert titles produced
  invalid JSON: Slack partially tolerated it, Microsoft Teams Workflows
  rejected the card (the "text card" error and the hourly
  route_send_failed rows). CHR()-code escaping only — backslashes don't
  survive multiple string layers (V022/CALLs+ lessons). Everything else in
  the sender is byte-identical to v2.
- webhook_delivery.sql v2: real Microsoft Teams (Workflows) recipe — the
  retired O365 {"text"} shape replaced with the Adaptive-Card
  WEBHOOK_BODY_TEMPLATE, ALERT_ROUTES row, 202-Accepted note; teardown
  covers OVERWATCH_WEBHOOK_TEAMS. RUNBOOK §19: setup + symptom->fix table.
- Docs synced to v4.9: README/DEPLOYMENT migration lists through V026,
  RUNBOOK object table V021-V026, FEATURES "Cost intelligence (v4.7-4.9)"
  section, ARCHITECTURE "Performance model" (fact-first, join-then-group,
  tier batching, telemetry loop).
- docs/design/V027_MART_FAMILY.md: the designed mart batch (9 marts +
  telemetry schema rider, grains, cadences, loader isolation, backfill,
  bookkeeping checklist). Build order finalizes on ~3 days of v4.9
  sampled telemetry.

Deploy: apply V026 in Snowsight (after V025), re-run validate.sql
(V001..V026), recreate the Teams integration per webhook_delivery.sql v2.

## 4.8.4 — Codex round 3: the migration-contract bug + on-demand heavies (2026-07-08)

Round 3 was mostly the already-queued V026 mart family; five items were
actionable now. Best catch of the round was real: Admin's expected-migrations
dict stopped at V020, so the panel could report "all applied" while
V021-V025 were missing.

- FIXED (#1): _EXPECTED_MIGRATIONS covers V021-V025 — and a new CI lock
  scrapes snowflake/migrations/ so the dict AND validate.sql can never
  trail the repo again.
- #5: the right-sizing profile (the ~90s Optimization scan) is on-demand
  behind a toggle; the idle advisor stays default.
- #12: the Security access-review pack fetches all ten sheets in one
  parallel batch (serial cached fallback kept).
- #15: batch_fallback telemetry now records tier, batch size, keys, and
  exception class — the data that decides whether partial-success
  batching (#16) is ever worth building.
- #20 (test half): hot pages carry pinned live-scan budgets — a new
  ACCOUNT_USAGE reference on Brief/Overview/Control Room fails CI with
  instructions to go fact-first instead.
- Deferred to the designed V026 batch: schema/role query facts, warehouse
  efficiency + query-family + cost-allocation + task-graph + incident +
  security + AI marts (#2-4, #6-11, #13-14), telemetry schema additions
  (#17, #19). Declined: #18 (sampled+capped telemetry is <=60 async
  inserts/session; buffering saves little and risks losing the tail).

## 4.8.3 — Codex round 2: caching economics + the healthy baseline (2026-07-08)

Nine of the twenty adopted (several improved on); the mart-family items
(role/schema facts, optimization/security/timeline/graph marts) are deferred
to a designed V026 batch, and partial-success batching is declined until the
batch_fallback telemetry says it matters.

- Health strip fetched+parsed ONCE per rerun in main() and passed to the
  sidebar strip, top bar, and status bar (#1 — third time Codex flagged it,
  now actually fixed rather than argued with).
- run_batch covers all four tiers (live/metadata added) (#2).
- Overview UN-batched (#4, Codex was right): coupling the filter-scoped
  board with the fixed 45d MTD read cold-started the fixed read on every
  filter change. Each read keeps its own cache key now.
- Cost -> Attribution movers and the cloud-services ratio are fact-first
  with live fallback (#5, #6). Improvement on #6: FACT_WAREHOUSE_DAILY
  already stores TOTAL and COMPUTE credits, so cloud services = the
  difference — no migration, contra the recommendation.
- measured_query_costs joins the filtered window FIRST, then aggregates —
  the whole attribution view is never pre-aggregated (#11, same fix the
  graph/proc builders got).
- Unit costs' three reads go out as one parallel batch (#15).
- APP_USAGE.RENDER_MS now spans sidebar/topbar/status chrome (#18).
- Telemetry persists a ~2% sample of ALL fetches, so the fleet view sees
  the healthy baseline, not just the slow tail (#19).

## 4.8.2 — perf pass: fewer scans, parallel first paints (2026-07-08)

Codex-informed review, verified against our own telemetry (renders 63%
sub-50ms; the pain is warehouse scans). No behavior changes — same numbers,
fewer/faster queries.

- Optimization ran the identical idle-warehouse scan twice under different
  cache tiers (advisor vs remediation) — different TTLs could even disagree
  about what "idle" is mid-session. One tier, one cache entry, one scan.
- Control Room 24h pulse is fact-first (FACT_QUERY_HOURLY, live fallback,
  p95 labeled "peak hourly") and spend movers read the new
  fact_warehouse_window_vs_prior instead of scanning metering live.
- Overview first paint and day replay batch their independent reads in
  parallel (tier-grouped; serial cached path on any failure).
- The jump box no longer costs queries on normal paints — SHOW WAREHOUSES
  and alert rules load once per session via an explicit "load all" row.
- The 139s attribution family: graph and procedure cost builders prune
  QUERY_ATTRIBUTION_HISTORY to task/CALL queries BEFORE the big GROUP BY.
- Canary release-compare anchors were pinned to 2026-01-01 (a half-year
  scan, 153s); they now anchor 3 days back.
- Declined from the review: cache-scope sharing (SiS runs one container
  per viewer — no cross-user cache exists to share, and it reintroduces
  the USER_PREFS leak class); use_container_width migration (blocked by
  the streamlit 1.45 SiS floor; becomes a shim when the channel moves).

## 4.8.1 — live round 3: six fixes from the first full day on v4.8 (2026-07-08)

- POLICY (V025): SEC_BREAK_GLASS_USE disabled — ACCOUNTADMIN /
  SNOW_ACCOUNTADMINS are this account's routine operating roles, and the
  rule watches only those two. The Security page panel keeps the
  visibility; bulk-resolve the open events as NOISE.
- FIXED: stored-proc $/call leaderboard was empty — the CALL-name regex
  reached Snowflake as 'CALLs+' (the string literal ate the backslash; the
  V022 lesson, one layer deeper). POSIX [[:space:]] now — zero backslashes
  at any layer. $0-attribution procs stay visible with an ATTRIBUTED_CALLS
  count instead of vanishing.
- FIXED: AI unit costs fall back to the Cortex CODE usage views
  (Snowsight/CLI token credits) — that's where this account's AI spend
  actually bills; the Functions/model view stays primary where populated.
- FIXED: Trexis roles no longer leak into ALFA's role-usage chart and day
  replay — new companies.role_clause (name heuristic) on role-grain
  builders (role share, day DDL, day grants).
- CHARTS: axis labels no longer truncate mid-name (labelLimit 260, value
  headroom on bar charts); every daily chart now labels DAYS ("Jul 05")
  instead of "12 PM" hour ticks that read as intra-day data.
- CLARITY: Overview spend KPI documents its warehouse-exact lens; Cost →
  Spend gains "Why totals differ across pages (and vs Snowsight)" with the
  actual split (billed vs warehouse-exact vs storage/transfer).
- New cost builders registered in the canary (column drift pages us, not
  a user); locks updated.

## 4.8.0 — unit costs: the price tag on one query, one CALL, one AI request (2026-07-08)

- NEW (Cost & Contract → Unit costs): MEASURED per-unit dollars, no
  migration needed. Most expensive individual queries (attribution credits,
  idle excluded — the "what did THIS cost" lens, alongside Optimization's
  allocated "who owns the bill" lens); a $/call leaderboard for EVERY
  stored procedure via ROOT_QUERY_ID roll-up (change-impact keeps watching
  the changed ones); AI spend by function + model with $/1M tokens.
  Queries and procedures honor company/Database/Schema (+ warehouse/user
  contains for queries); the Cortex usage view has no database dimension
  and is labeled account-wide.

## 4.7.0 — task-graph cost trends + warehouse change scorecard (2026-07-08)

- NEW (Operations → Task graphs ($)): pipeline cost over time — one row per
  graph run via GRAPH_RUN_GROUP_ID, MEASURED warehouse credits per run
  (QUERY_ATTRIBUTION_HISTORY roll-up, ~6h lag), $/run (allocated), success
  %, p95 wall time, and a CHEAPER/PRICIER/FLAT trend per pipeline. Honors
  the Company, Database, and Schema filters. Serverless task credits are
  listed separately at task-day grain — never smeared across graphs.
- NEW (Operations → Change impact): warehouse setting changes tracked like
  object changes. V024 snapshots SHOW WAREHOUSES daily (this account has no
  ACCOUNT_USAGE.WAREHOUSES), diffs snapshots into WAREHOUSE_CHANGE_REGISTRY,
  freezes a 14-day pre-change baseline ($/day, p95, queue min/day, spill,
  fail %), refreshes the after-window daily until day 14, and raises
  WH_CHANGE_REGRESSION alerts (CRITICAL at 2x $/day). Verdicts live in the
  proc — the page and the alert can never disagree; the UI adds per-metric
  deltas and the credits/day line with the change marked.
- validate.sql expects V001..V024; teardown drops the new task/proc and
  preserves the registry + snapshot tables (frozen baselines are not
  rebuildable). Locks in tests/test_graph_wh_scorecard.py.

## 4.6.4 — live round 2: filters that actually filter + contract truth (2026-07-08)

- FIXED: alert feeds (Brief fires, Alerts queue, Control Room triage,
  Overview counts) now honor the Company filter — Trexis warehouse fires
  no longer surface under an ALFA scope. Account-level events
  (COMPANY='ALL') always show for everyone, deliberately.
- FIXED: the Database picker honors the Environment filter — ALFA + PROD
  offers exactly ALFA_EDW_PRD/ALFA_EDW_MGM, and a lingering DEV pin resets
  when the environment changes. companies.databases_for() shares
  classify_environment with the SQL clause so list and filter cannot drift.
- NEW: Contract & Forecast shows Snowflake's own contract balance when the
  role can see SNOWFLAKE.ORGANIZATION_USAGE — REMAINING_BALANCE_DAILY
  (the balance that burns down daily) + CONTRACT_ITEMS (commit, term
  dates): remaining $, burn/day (down-days only, so renewal top-ups don't
  poison it), runway, on-demand overrun, burn-down chart. Zero config;
  degrades honestly to the SETTINGS flow when org views aren't visible.
- Locks in tests/test_company_env_scope.py (21 tests).

## 4.6.3 — V022 apply failure: comma-eating comment + a parse gate (2026-07-08)

- FIXED: V022's ALERT_DELIVERIES CREATE TABLE was unparseable — the inline
  ROUTE_ID comment swallowed the column-list comma (caught by the user in
  Snowsight; the guard had run, nothing else applied, so re-running the
  fixed file from the top is clean).
- NEW GATE: tests/test_migrations_parse.py parses every migration/script's
  plain SQL with sqlglot (snowflake dialect) — CREATE TABLE/VIEW, INSERT,
  MERGE, UPDATE, DELETE, SELECT — with a real statement splitter that
  respects strings and comments. Scripting blocks and dialect gaps (tasks,
  alerts, grants, procs) remain Snowsight-only. The gate provably fails on
  the exact V022 bug class. sqlglot added to requirements-dev.

## 4.6.2 — Trexis-PROD lock + teardown integration audit (2026-07-08)

- V023's PROD volume scope verified and LOCKED for both companies:
  tests/test_migration_v023.py scrapes the migration's predicate and proves
  TRXS_EDW_PRD / TRXS_GW_DATA_PRD / TRXS_ABC_METADATA_PRD keep alerting
  while every DEV/SIT/SAN database goes quiet — and that the SQL agrees
  with the app's classify_environment, so PROD has one definition.
- Teardown audit (user catch: "do we drop email integrations?"): NO — the
  webhook integration, its URL secret, the email/recipe integrations, and
  the ML forecast model all survived teardown. Now dropped (integrations
  under an ACCOUNTADMIN-labeled block). The teardown-coverage test now
  parses SECRET / NOTIFICATION INTEGRATION / SNOWFLAKE.ML.FORECAST kinds
  across ALL opt-in scripts, so this class can't slip through again.

## 4.6.1 — first live-fire morning: three fixes from real telemetry (2026-07-08)

Migration V023 (apply in order after V022): sweep v4 + scan v9.

- PIPE_VOLUME_DROP scoped to PROD databases (ALFA_EDW_PRD/MGM, *_PRD). The
  first production sweep raised 700+ HIGHs from DEV/SIT scratch and dated
  backup tables — volume collapse only matters where consumers are.
  Cleanup: bulk-resolve the open storm as NOISE (it seeds the
  threshold-suggestion evidence).
- Scan v9: SEC_CRED_EXPIRY no longer filters CREDENTIALS.DELETED_ON — the
  column doesn't exist on this account (sibling of the V020 EXPIRES_AT
  discovery). Without this, applying V020's v8 would swap the hourly
  EXPIRES_AT failure for an hourly DELETED_ON failure.
- App side: expiring_credentials + governance_counts stripped of the same
  phantom column (live Security-page error 2026-07-08 08:06).

Validated by the instrumentation shipped yesterday: the change-impact
tracker flagged SP_ALERT_SCAN as REGRESSED, and the persisted error log
carried the exact failing identifier per hour.

## 4.6.0 — review-debt closure, delivery v3, structure (2026-07-07)

Migration V022 (ALERT_DELIVERIES per-route ledger + SP_NOTIFY_WEBHOOK v3) —
UNTESTED ON LIVE until applied; prove with the fire drill. Re-run roles.sql.

Review debt closed (consolidated 2026-07-08 review):
- Delivery: per-route fan-out (a Slack success no longer starves PagerDuty),
  failed routes retry inside the window, aging-out events flagged loudly.
- Brief refuses to invent numbers: unreachable telemetry renders n/a + a
  warning, ROI shows "app cost unavailable" instead of $0.00.
- Lock waits: never-acquired locks (the worst cases) are counted and ranked
  first instead of being zeroed by COALESCE.
- Storage movers company label: database-grain CASE (was the warehouse CASE
  applied to a database column — everything read ALFA).
- ONE MFA-gap definition app-wide: password-login evidence (30d), governance
  score included; evidence wording updated.
- THRESHOLDS trimmed to the two knobs code reads; WINDOW_HOURS labeled
  informational in the Rules generator; window-anchoring convention
  documented in data/common.py. Contract-dates guard verified already sound.

Structure:
- cost.py (1,290 lines) split: dispatch-only cost.py + cost_parts/{spend,
  contract,ai_chargeback,optimize}; fixtures stub the parts.
- 13 wave-era lock files moved to tests/history_locks/ + tests/README.md map.
- RUNBOOK §18 syncs 4.1→4.6 (objects, drill, precision workflow, trust
  surfaces, cache identity, layout).

## 4.5.1 — formula fact-check: three corrections (2026-07-07)

Every number-producing function hand-verified (tests/test_formula_audit.py
pins the results). Three discrepancies found and fixed:

- allocate_by_share leaked pennies: naive per-part rounding made chargeback
  parts sum to 99.99 against a 100.00 warehouse total. Largest-remainder
  allocation now sums exactly, preserving proportionality.
- Day-replay activity baseline divided by a fixed 14: loader gaps and quiet
  days deflated the baseline and over-flagged replay days. Divides by days
  actually present.
- Cortex per-user 30d projection used an active-day basis (a user active 2
  of 30 days projected at 15x real burn) while the page's rollup used the
  calendar window — the two surfaces disagreed (review finding #11). Both
  now use the calendar basis; AVG_DAILY_CREDITS stays as the intensity
  metric.

Verified correct as-built (no change): credits/billed/pct math, month_days,
contract_pace, flat-series forecast (+collapsing band), scoring weights and
caps, price-per-run bounds, steering math, MTTA/MTTR NULL handling,
restatement anchor, spend-movers per-warehouse baselines.

## 4.5.0 — differentiators: what Snowsight structurally can't do (2026-07-07)

No migration; one OPT-IN script (snowflake/alert_drill.sql).

- Day replay (Control Room): pick a date → spend movers vs each warehouse's
  own 14d baseline, query activity vs baseline, DDL landed, grant changes,
  task failures, alerts — one cross-domain story with worst-first headlines.
- Contract steering (Cost → Contract): the gap to commit in $/day and how
  far the named levers reach (idle tuning + top recurring patterns), with
  an honest coverage verdict. Estimates route through the verifier.
- Blast radius: every warehouse suspend/resize confirmation (sizing panel,
  alert closed-loop) now shows who ran what there in the last 7 days —
  users, roles, tooling tags — before the typed confirm.
- Object TCO: selecting a storage-reclaim row prices the table end to end —
  storage $/mo + reads/writes/last-touch from ACCESS_HISTORY — and calls
  out "refreshed but never read." Degrades honestly on Standard edition.
- Price-a-pattern: pick any recurring fingerprint → observed $/run and a
  bounded estimate at ±size steps (same assumption pair as the what-if).
- Monthly fire drill (opt-in): synthetic CRITICAL on the 1st must be
  delivered AND acked; Admin → Canary scores the streak and time-to-ack.
- Query-tag governance (Cost → Attribution): exec-time-weighted tag
  coverage with the top untagged workloads named.
- Restated-days detector (Admin → Canary): metering days whose rows changed
  ≥48h after close — the receipt when a reported number moves (v1;
  first-reported snapshots would need a snapshot fact).
- New pure modules: replay, steering, drill; day_literal date gate;
  16 unit locks; 10 canary registrations; teardown covers the drill task.

## 4.4.0 — feature-depth batch: the features earn their claims (2026-07-07)

No migration needed (builds on V021's resolution kinds).

- Threshold suggestions from YOUR resolutions: Rules now computes, per rule,
  the threshold that keeps ≥90% of ACTIONED alerts while cutting NOISE —
  with the statistical basis stated. Advice through the same generate-only
  flow; overlapping distributions honestly say "redesign, don't tune."
- Live re-check in the alert drawer: one button re-runs the rule's condition
  against TODAY's data for the event's target and says "condition clear —
  resolve with this as evidence" or "still over." Covers the warehouse-lever
  rules + cloud-services ratio + fail rate.
- Forecast backtest on Overview: retro-runs both engines at day 7/14/21 of
  the last 3 months vs actuals, shows per-engine mean absolute error, and
  names the engine that's been more reliable vs the one configured.
- Platform score history: 30-day retro score from facts + alert history
  (same weights), as a sparkline on the score card and a trend expander —
  the prerequisite for calibrating the admittedly-uncalibrated weights.
- Recurring cost patterns: the expensive-queries view now also groups the
  hour-share allocation by QUERY_PARAMETERIZED_HASH — $/day per pattern,
  where caching/materialization actually pays.
- New pure modules: logic/tuning.py, data/recheck_sql.py; 17 new unit locks;
  4 new canary registrations.

## 4.3.0 — UI performance + display pass, router fixes (2026-07-07)

Interaction latency:
- Fragments: Views popover, right-size what-if, statement export (alert
  drawer and Admin emergency already were) — widget moves rerun panels,
  not pages.
- pandas Styler capped at 1,500 rows; larger tables fall back to
  Arrow-native printf formats (commas traded for paint time, deliberately).
- run_batch adopted on Operations Queries + Contention (one async round
  trip on cold cache); spinners on the heavy scans (repeat-query, storage,
  expensive queries).
- spend_trend and the incident timeline embed their dataset ONCE per chart
  (was once per layer); hour heatmap capped at top-20 rows.

Display:
- Wide tables auto-pin the first column (8+ cols, runtime-guarded).
- Alert tables triage-sort: worst severity first, newest within.
- Display-timezone conversion is now CENTRAL in the table pipeline (naming
  convention on timestamp columns; explicit conversions kept for charts;
  double-conversion guarded by a frame marker). CSVs stay account time.
- Fresh-deploy setup gaps render as one calm info line, not red errors;
  CSV buttons drop to icon-only and skip tiny frames; ops KPIs get sparks.

Router/classifier audit (user-requested):
- FIXED: alert deep-links routed to the Cost page's PRE-consolidation
  section names (Spend/Optimization/Contract) — every COST_* Investigate→
  and fix jump crashed the section radio since design-system D. Renamed to
  the live labels; COST_DEPT_BUDGET_PACE now lands on Chargeback & AI.
- lazy_sections self-heals: a stale saved-view/deep-link section falls back
  to the first label instead of crashing the page.
- New test suite scrapes lazy_sections labels/keys from page source and
  asserts every navigate.py target, jump-box target, and all 26 seeded rule
  ids resolve — section consolidations can never strand deep links again.

## 4.2.0 — cost intelligence + trust batch (2026-07-07)

Migration V021 (RESOLUTION_KIND on ALERT_EVENTS, APP_QUERY_TELEMETRY + purge
task) — re-run snowflake/roles.sql after applying.

- Most expensive queries in allocated dollars (warehouse-hour credits split
  by execution-second share) — Cost → Optimization, canary-registered.
- Interactive right-size what-if: size step + auto-suspend together, shown as
  a bounded monthly range with stated assumptions — extends the sizing panel.
- Storage reclaim: ACCESS_HISTORY read-evidence joins the waste scan; "stale
  AND never read (90d)" shortlist; degrades honestly on Standard edition.
- Alert precision per rule (ACTIONED / NOISE / EXPECTED resolution kinds,
  new picker on resolve) — Alerts → Rules; pre-V021 deployments retry legacy.
- Mart reconciliation: fact totals vs live ACCOUNT_USAGE with drift bands
  (±2% noise / ±5% act) — Admin → Canary.
- Billing truth vs app model: org rate-card dollars for this account vs
  credits x configured rate, by month — Admin → Org spend.
- Fleet query telemetry: slow (≥2s) and failed fetches persisted from every
  viewer session (sampled, capped, fire-and-forget) — Admin → Performance.
- CI: mypy gate on the pure layers (zero findings) + floor-compat job pinned
  to the requirements minimums; devcontainer, Makefile, secrets.toml.example.
- New test files: test_v22_features (25 locks) + test_operator_gating
  (profile navigation via AppTest + lifecycle SQL state gates).

## 4.1.0 — feature waves V012–V020 + hardening pass (2026-07-07)

Everything shipped after the 4.0.0 rebuild, plus a 20-item review pass.

Feature waves (V012–V020, see FEATURES.md for the full map):
- Alert drawer with playbooks, AI explain, inline closed-loop fixes; webhook
  delivery in-chain with per-family routing; anomaly events pre-explained by
  grounded Cortex; morning AI digest.
- Saved views, default landing, per-user display timezone (USER_PREFS, V013).
- Change-impact regression tracker, fingerprint drift, incident correlation
  timeline, savings verifier (ESTIMATED → VERIFIED/REJECTED).
- Role-based Trexis user scoping via COMPANY_FOR_USER (V019);
  WH_TRXS_LINEAGE; CREDENTIALS expiry rule re-enabled on EXPIRATION_DATE (V020).
- Design system D: SVG nav, status bar, sparklines, section consolidation.

Hardening pass (2026-07-07):
- Row caps can no longer be disabled by a column/comment containing the word
  "limit" (word-boundary LIMIT detection in the query engine).
- Python-side "today" now uses the account timezone (America/Chicago) for MTD
  boundaries, forecasts, contract pace, and statement months — no more
  evening-hours day drift under SiS/UTC.
- Transient role-probe failures no longer pin the session to the ANALYST
  profile; the sidebar Refresh also re-resolves the role.
- Cortex COMPLETE now carries a 90s statement timeout; usage logging and the
  error sink write async (page switches and failure paths stop paying a
  blocking INSERT round trip).
- Exported executive-summary HTML escapes every field; sidebar strip escapes
  interpolated text; expired-session errors get a friendly "press Refresh"
  message; page-boundary captions name Python bug types explicitly.
- Altair theme registered via the altair ≥5.5 API (deprecation warning gone,
  altair-6-proof); ruff rule set widened (C4/SIM/PIE/PERF/RUF); CI gets
  concurrency-cancel, pip caching, and a 15-minute timeout; connection
  failures show the underlying reason on the not-connected screen.

## 4.0.0 — ground-up rebuild (2026-07-07)

Full rewrite in a new repo, driven by the 2026-07-07 hostile panel review of
the original OVERWATCH.

- 7 pages (Overview, Control Room, Alerts, Cost & Contract, Operations,
  Security, Admin) replacing 6 shells + ~30 zombie section modules.
- Pure, tested logic layer (formulas, anomaly, forecast, scoring, actions).
- Single SQL-safety module; blind-except ban enforced by ruff in CI.
- Query engine that never caches errors, shows truncation, keys cache by role.
- Mart-first data architecture with versioned migrations (V001–V005),
  dedicated XSMALL warehouse + resource monitor, chained hourly/daily tasks.
- Billed spend now applies `CREDITS_ADJUSTMENT_CLOUD_SERVICES`.
- Rates ($3.68 compute / $2.20 Cortex) moved to `SETTINGS`; admin-gated.
- No synthetic data anywhere: real series or honest empty states.
- ALFA/Trexis hardcoded scoping isolated to `app/companies.py` with
  `KEBARR1 → ALFA` override; code/seed sync covered by a unit test.
