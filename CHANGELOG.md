# Changelog

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
