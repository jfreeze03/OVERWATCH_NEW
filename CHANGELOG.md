# Changelog

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
