# Architecture

## Layers

```
streamlit_app.py            single entry (SiS and local dev)
app/
  main.py                   shell: header, sidebar nav, page dispatch
  config.py                 constants, thresholds, defaults (pure)
  companies.py              ALFA/Trexis hardcoded scope + KEBARR1 override (pure)
  logic/                    business math — pure Python, no Streamlit, unit-tested
  data/                     SQL string builders — pure Python (prefs_sql/mart_sql reach core.identity), unit-tested
  core/                     runtime: session, cached query engine, errors, state
  ui/                       components, Altair charts, pages
snowflake/migrations/       versioned setup SQL (V001, V002, … one file per change) + SCHEMA_VERSION
tests/                      pytest: logic/data units, AppTest page smokes, migration + history locks
```

Dependency rule: `logic/` and `data/` never import `ui/` and never import
Streamlit directly. From `core/` they may import only the Streamlit-free
helpers `app.core.sqlsafe` and `app.core.result`, with one exception:
`data/prefs_sql.py` (at module level) and `mart_sql`'s last-visit read
(lazily) import `app.core.identity` for the viewer-identity SQL, and that
module imports Streamlit. Both layers are tested without a Snowflake
connection. CI installs Streamlit in both legs: `requirements-dev.txt` for the
main lint-and-test job, which runs the AppTest page smokes, and an explicit
`streamlit==1.52.2` floor pin for the floor-compat job. So the CI environment
does not enforce this rule: code review does, plus import-purity tests on a
few logic modules (client_support, policy_coverage, storage_waste,
savings_rollup, unread_maintenance).

## Data flow (mart-first)

1. Scheduled tasks load compact **fact tables** from ACCOUNT_USAGE. Hourly:
   the root `TASK_LOAD_HOURLY` runs `SP_LOAD_HOURLY_FACTS` (warehouse
   metering into `FACT_WAREHOUSE_DAILY`), then `TASK_QH_EXTRACT` runs
   `SP_LOAD_QH_EXTRACT` (stages QUERY_HISTORY into `OW_QH_EXTRACT` and loads
   `FACT_QUERY_HOURLY` / `FACT_QUERY_DAILY` and `MART_CLOUD_SVC_DAILY`).
   Daily: `SP_LOAD_DAILY_FACTS` (metering-daily with cloud-services
   adjustment, tasks, logins, storage). All MERGEs over bounded re-scan
   windows. RUNBOOK §4 has the full task table.
2. Chained AFTER `TASK_QH_EXTRACT` (V071), tasks refresh `MART_EXEC_BOARD`
   (the one first-paint aggregate) and run the alert scan, so both read
   freshly loaded query facts.
3. Pages read marts/facts first. When a mart object is missing or stale, pages
   fall back to **bounded live aggregates** (fixed short windows, GROUP BY
   pushdown, row caps, tier-cached) and label the source. Live detail drilldowns
   are explicit user actions, never first paint.
4. Each loader stamps its own row in `SOURCE_FRESHNESS_STATE`, the primary
   per-source freshness read (the `MART_SOURCE_FRESHNESS` view is the
   pre-V040 fallback); every page shows a source + freshness caption.
   ACCOUNT_USAGE latency (up to ~45 min for query history, up to 24h for
   metering-daily) is labeled, not hidden.

## Query engine (`app/core/query.py`)

- Tiers: `live` 30s / `recent` 300s / `hourly` 3600s / `historical` 3600s /
  `metadata` 14400s cache TTL, with matching statement timeouts
  (30/120/120/180/30s). The `hourly` tier serves mart/fact reads whose sources
  load hourly or daily (a 300s TTL re-paid them ~12x/hour).
- **Errors are never cached**: the `st.cache_data` functions raise; the public
  `run()` catches outside the cache and returns a typed `QueryResult`
  (`ok/error/truncated/source/fetched_at`). A transient failure can never pin
  an empty frame for the TTL (old-app finding H1).
- Cache keys are the SQL text (company, environment, date window and filters
  are baked into each builder's SQL) plus a scope of **current role**, the
  refresh/domain salts and, for per-viewer reads (`USER_PREFS`) only, the
  viewer (`query._cache_scope`; old-app C2 hygiene). Under owner's-rights SiS
  every viewer shares the owner's role, so account-wide reads are shared
  across viewers by design.
- Row caps fetch `max_rows + 1` and set `truncated`; the UI renders a banner.
  No silent LIMIT injection (old-app M1).
- Statement timeout and query tag are tracked **on the session object**, not in
  `st.session_state`, so a recycled connection cannot desync (old-app M4).
- Query tag: the app sends `OVERWATCH|page=<page>|tier=<tier>` (ALTER SESSION off-SiS, statement_params
  on SiS), but Streamlit-in-Snowflake overrides it with its own stamp on every app statement
  (`"StreamlitName":"DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP"`); self-cost attribution and every
  self-noise filter key on that stamp (`app.data.common.app_self_sql`).

## Error handling contract

- Every page renders inside `safe_page` (app/core/errors.py): exceptions are
  recorded to an in-session ring buffer and best-effort inserted into
  `APP_ERROR_LOG`, then a friendly error renders. Nothing is swallowed
  invisibly; the Admin page lists recent errors.
- Ruff `BLE001` bans blind `except Exception:` except (a) in the five
  sanctioned runtime modules listed in `ruff.toml` per-file-ignores
  (`app/core/errors.py`, `session.py`, `query.py`, `state.py`, `ai.py`), and
  (b) at line-level `# noqa: BLE001` sites. Those cover best-effort chrome and
  cosmetic paths (chart theming, table styling, telemetry, deep links) plus a
  few guarded fallbacks (`load_settings` falling back to code-default rates,
  `run_mart_first`'s `mart_accept` coverage probe falling through to the live
  path), where the page deliberately degrades instead of breaking. Most sites
  carry a reason comment; some still do not.
- **Empty/absent-state vocabulary (C25):** `components.empty_state(kind, ...)`
  is the one rendering of absence, so color carries meaning — `clean` =
  verified-clean compact green row, `needs_setup` = blue info (configure or
  install first), `no_data_yet` = quiet caption (a successful read returned
  zero rows), `unavailable` = red lead line with the full error one click away.
  `guard()` routes its empty branch through it (callers whose empty is the
  verified-good outcome pass `kind="clean"`) and its error branch renders as
  `unavailable` — except absence-of-setup (the missing-migrations error), which
  stays a calm `needs_setup`. `setup_hint` never renders on an empty read of any
  kind (a successful read proves setup exists, so empty-read guidance belongs in
  `empty_message`); on the error branch it renders under `needs_setup`, and under
  `unavailable` only for a setup absence or schema drift (`is_setup_absence` /
  `is_schema_drift`), never under a timeout or other failure. An empty state may carry its next best action (`action_label`
  + `on_action`, F56) so an empty panel is a doorway, not a dead end.
- **Presentation mode (C19):** `components.present_mode()` returns `operator`
  (default — the lean daily-triage surface) or `audit` (the full evidence
  chain), stored per-viewer as `PRESENT_MODE` in `USER_PREFS` and hydrated into
  `_ow_present_mode` exactly like the density pref. `result_caption()` always
  shows the SOURCE (the app's "show the source" ethos is load-bearing) but
  trims the per-panel fetched-at stamp and methodology note in operator mode;
  `audit_mode()` gates methodology / how-computed / reconciliation / backtest
  blocks (and `methodology_note(text)` renders in audit only). The pref-write
  path (`prefs_sql.upsert_pref_sql`, an identity-scoped allowlist-gated MERGE)
  returned with this — it had been retired with the density toggle in v4.157.
- **Master-detail layout (C42/C47):** `components.master_detail(df, *, key,
  id_col, list_render_fn, detail_render_fn, ...)` is the shared ranked-work-LEFT
  / selected-detail-RIGHT primitive (Action Center, Operations ▸ Optimize fix queue).
  It owns only the column split and the fragile positional-selection → stable-id
  → sticky-persistence dance (selection binds by identity via a rec29 seen-guard,
  so a re-sort can't rebind the detail to the wrong row; a deep-link preselect is
  one-shot and clears the sticky selection); each caller keeps its own table
  flavor and editor body. Every master-detail surface wraps its columns in a
  `st.container(key="ow_md_<name>")`, and one `@media(max-width:1180px)` rule in
  `theme.py` restacks all `st-key-ow_md_*` columns to full-width on narrow
  viewports (Streamlit columns don't auto-stack until unusably narrow).
- **Operator-write seam (C48):** `execute_statement` / `execute_action` /
  `execute_cancel_query` paint an in-flight spinner around the round-trip, and
  every write CLICK BLOCK pairs `components.write_gate_open(<key>)` (last
  condition of the click gate) with `components.stamp_write(<key>, ok)` (after
  the block's last write, before any `st.rerun`). The latch ARMS ON GATE-OPEN
  — a duplicate click on a non-fragment page preempts the running script after
  the write commits, before any end-of-block stamp could land — is
  run-sequence aware (`_ow_run_seq` bumps once per full script run; the queued
  duplicate always lands on the very next run, however long its cold render
  takes), per-key, and success-only (a failed write's entry is deleted so the
  retry re-executes). Keys scope by action/target wherever a fixed key would
  swallow a genuinely distinct action; fragment surfaces (where the run seq
  freezes) use scoped keys, and idempotent emergency actions short backstops.
- **Interactive-control visual grammar (F14/F24/F17, `theme.py`):** one
  keyboard-focus ring on every control (`:focus-visible` outline across buttons,
  selects, radios, inputs — F14); one locked-action treatment (F24) so a disabled
  control reads as locked, not unresponsive — 0.55 opacity, dashed ink-mute edge,
  no hover lift, and a `not-allowed` cursor set on the `.stButton` *wrapper*
  (a disabled `<button>` isn't a pointer target, so a cursor on it is ignored)
  WITHOUT `pointer-events:none` (which would kill the `help=` "why it's locked"
  tooltip); `:disabled` is excluded from the primary-button gradient so a disabled
  primary falls through to the locked look instead of reading as bright-but-faded.
  KPI cards (`.ow-card`) carry a min-height floor (F17) so a row stays even when
  one card has a sparkline — a flex-through-the-column stretch was tried and
  proven inert (`height:100%` dies against Streamlit's auto-height markdown
  wrappers), so the robust floor was adopted instead. The card sparkline itself
  tells the truth: `spark_svg` anchors its domain to include zero (F42) so a tiny
  wiggle doesn't fill the height like a doubling, and the card spark is colored by
  the delta's trend polarity via the shared `_delta_is_good` (F43) so it can never
  draw a calm line over a red delta — the delta chip and its spark read the same
  good/bad hue by construction.
- **One watch affordance (F59, `components.watch_*`):** "watched" reads as a filled
  star ★ on every surface — the Entity 360 toggle (`watch_toggle_label` → ★ Watching
  / ☆ Watch), the decision boards' WATCHED column (`watch_star` + `watch_star_column`
  → ★ or blank, not raw True/False), the Brief badge and the Watchlist board. The
  star is display-only and applied after each board's watched-first sort, so pinning
  and the watched counts stay on the underlying bool; a NaN reads as not-watched.
- **One confidence encoding (F60, `components.confidence_progress_column`):** a 0-1
  confidence renders as ONE bar in every decision-workbench TABLE (Operations ▸
  Optimize fix queue, Action Center, Proof ▸ Pipeline, Entity 360 work list), never a raw float;
  single-value surfaces (the Entity 360 header) keep `confidence_badge`. Authored
  confidence carries one shared help string (`AUTHORED_CONFIDENCE_HELP`) so its
  provenance wording (operator *or* recommendation engine) never drifts per surface.
- **Chart mark provenance (C38, `charts.PROVISIONAL_OPACITY` / `_provisional_opacity`):**
  a chart's marks encode how trustworthy the number is — measured-and-complete is
  solid; measured-but-provisional (newest metering day, in-flight month) dims to the
  one shared `PROVISIONAL_OPACITY`; the month-end spend forecast is a KPI, not a
  chart band. Dashed *rules* (budget, flagged days, F46 gates, F48 change dates) are
  a separate annotation category. Known gap: Cost▸Optimize's projected storage-growth
  bars aren't yet mark-distinguished from measured spend (labeled "Projected" in text).

## Cost formula contract

- Billed account spend: `METERING_DAILY_HISTORY` with
  `CREDITS_USED + CREDITS_ADJUSTMENT_CLOUD_SERVICES` (the adjustment is real
  money; the old app zeroed it).
- Warehouse spend: `WAREHOUSE_METERING_HISTORY` (exact, includes idle).
- User/database spend: allocated from query elapsed-time share (or
  `QUERY_ATTRIBUTION_HISTORY` when present) and always labeled **allocated**.
- Rates come from `SETTINGS` (seeded $3.68 compute / $2.20 Cortex /
  $23 TB-mo). The Admin page edits them (OVERWATCH admins: the `OPERATOR_USERS`
  allowlist or direct SNOW_PRI_GFR_PRD_ALFA_DSA members, type-to-confirm); code ships matching defaults only as offline
  fallback.
- All conversion math lives in `app/logic/formulas.py` and is regression-tested.

## Security model

- **The app runs owner's-rights under Streamlit-in-Snowflake.** Every viewer's
  statements execute as the app owner. Snowflake RBAC decides who can open the
  app (USAGE on the Streamlit object: today SNOW_ACCOUNTADMINS + SNOW_SYSADMINS,
  per `roles.sql`; the owner decision of 2026-10-05 names four roles, adding
  SNOW_PRI_GFR_PRD_ALFA_DSA and SNOW_PRI_GFR_PRD_ALFA_DTI, whose Snowflake side
  is a pending owner change). It does NOT limit data per viewer: every viewer reads with the
  owner's privileges. Viewer identity comes from `st.user`
  (`app/core/identity.py`).
- Company scoping (ALFA vs Trexis) is a shared-account *convenience filter*,
  hardcoded deliberately in `app/companies.py` and seeded to
  `COMPANY_SCOPE` (a pytest keeps code and seed in sync). It is not an
  isolation mechanism and the docs never claim it is.
- User classification: `TRXS_*` → Trexis; explicit override `KEBARR1` → ALFA
  (holds both companies' roles, treated as ALFA by policy).
- Page visibility and the admin gate depend on the viewer
  (`session.viewer_access`, owner decision 2026-10-05): a viewer on
  `config.OPERATOR_USERS` is an admin with no lookup (source `allowlist`); a
  direct USER grantee of SNOW_PRI_GFR_PRD_ALFA_DSA, read live with
  `SHOW GRANTS OF ROLE` run as the owner, is an admin with full parity
  (source `role`; a grant to a role is not expanded); everyone else, and an
  unidentified SiS viewer, gets the read-only MONITOR profile (Cost
  Intelligence + Operations; source `default`). The lookup is memoized per
  session (re-checked after 300 s; a failure, `lookup_failed`, or an empty USER
  set, `unverified`, is retried after 60 s) and fails closed. It filters
  *pages*, not data. Off-SiS, with no viewer identity, it falls back to the
  role → profile map.
- Admin actions are gated at each call site by `session.is_operator()`. The
  executors re-check entitlement themselves (`query._entitlement_refusal`) for
  the `ALTER WAREHOUSE/PIPE/TASK/USER` and `ALTER ACCOUNT SET` levers
  (`query._PRIVILEGED_PREFIXES`), query cancel, and every
  INSERT/UPDATE/DELETE/MERGE/CALL on OVERWATCH except the viewer's own
  self-service rows (USER_PREFS, USER_WATCHLIST, APP_USAGE,
  APP_QUERY_TELEMETRY, matched as the exact object token); a role-sourced
  admin is re-verified live at write time (memo at most 15 s).
  Operator and UI writes go through the executors (`query.execute_statement`,
  `execute_statement_async`, `execute_action`), whose allow-list admits one
  statement aimed at OVERWATCH objects or a lever. Two paths skip that
  allow-list: the best-effort `APP_ERROR_LOG` sink in `errors.py` inserts
  directly, and query cancel (`SYSTEM$CANCEL_QUERY`, a SELECT) has its own
  seam, `execute_cancel_query`, which regex-validates the query id and builds
  the statement itself. Write friction follows CLAUDE.md law 11: one
  click for reversible upserts to OVERWATCH's own tables (e.g. alert ACK),
  type-to-confirm (`confirm_gate`) for classifying or account-touching writes
  (alert RESOLVE, incident declare/close, warehouse levers) and for Admin
  settings edits.
- Local/Community-Cloud runs use one shared connection and are **dev-only**;
  `DEPLOYMENT.md` says so explicitly.

## What is deliberately absent

Synthetic/fallback chart data, keyword search branded as AI, self-executing
remediation, per-company credit-rate divergence (one contract rate until
finance says otherwise), and Dynamic Tables (ACCOUNT_USAGE is already delayed;
scheduled MERGE tasks are cheaper and more debuggable — same rationale the old
app documented, kept because it was correct).

## Performance model (July 2026)

Three rules keep the app fast without spending more on the warehouse:

1. **Lazy sections.** `st.tabs` executes every tab body on every rerun; pages
   use `components.lazy_sections` instead, so a page paint costs the active
   section only (Cost went from ~20 queries per load to 1-3).
2. **SQL is the cache key.** The tiered `st.cache_data` fetchers key on the
   SQL text plus `role|salt`. Filters are baked into each builder's SQL, so
   changing a filter refetches only the queries whose SQL actually changed.
3. **Facts before ACCOUNT_USAGE.** Hot paths (Ops query summary, Cost spend)
   read the hourly-loaded `FACT_*` tables and fall back to labeled live
   queries when a fact is empty or a dimension (e.g. schema) is missing.
   Heavy elective scans (dormant users, repeat-query fingerprints) run
   behind toggles.

Admin > Performance shows the app's own statement families on
`WH_ALFA_ADMIN` (p95, GB scanned, by parameterized hash) and the
session's approximate cache-hit rate — measure before optimizing further.

## Deliberate choices reviewers will ask about

**Custom alert scans instead of native `CREATE ALERT`.** An hourly scan
(`SP_ALERT_SCAN`), a daily scan (`SP_ALERT_SCAN_DAILY`), the anomaly sweep and
several single-purpose scanners (canary, change-impact, SLO breach, warehouse
change, ETL cycle, schema drift, cloud-services anomaly, sleep polling)
evaluate ~45 rules with shared dedupe keys, severity escalation, channel
routing, and rules-as-rows editable in-app. Native ALERTs would mean dozens of
separately billed schedules (one per rule) with no shared dedupe or routing and
config drift outside the app. `native_alert_templates.sql` ships for teams
that prefer them. This is a costed choice, not unfamiliarity.

**Scheduled MERGE facts instead of Dynamic Tables everywhere.** Loaders run as
scheduled tasks on the shared XSMALL `WH_ALFA_ADMIN` (the app's own warehouse,
60s auto-suspend): predictable cost, explicit procs covered by
teardown/canary/tests. There is no resource monitor: V045 dropped
`OVERWATCH_RM` because it was suspending the app mid-use, and COST alert rules
are the guardrail. A Dynamic Table refreshes on its own TARGET_LAG schedule
whether or not anything reads it, and DTs cannot source SNOWFLAKE share views
(no change tracking), so they cannot replace the ACCOUNT_USAGE loaders anyway.
`MART_SPEND_ROLLUP_DT` (V015) was a measured DT pilot; the 2026-08-17 audit
found nothing read it while it kept refreshing every ~6h on `WH_ALFA_ADMIN`, so
V090 dropped it and the app standardized on scheduled-task marts.

**String-built SQL with a safety layer instead of Snowpark binds.** Builders
are pure functions emitting complete statements the app also SHOWS to users
(review-before-execute is a feature). Every user input passes
`clean_filter_text` (whitelist), `contains_filter` (LIKE-metachar escaping,
`ESCAPE '~'`), `sql_literal`/`safe_identifier`, with injection tests in CI.
Snowpark binds would not remove the display/require-review path.

**Hardcoded company scope instead of row access policies.** Two companies,
one account, scope is convenience not a security boundary (the Streamlit
grant and the in-app admin check, `OPERATOR_USERS` plus direct
SNOW_PRI_GFR_PRD_ALFA_DSA members, are). RAPs
cannot bind SNOWFLAKE.ACCOUNT_USAGE itself, and policy sprawl across derived
objects buys admin burden without closing the actual exposure. Revisit on a
compliance driver.

**Webhook delivery IS wired.** `TASK_ALERT_NOTIFY` chains AFTER the scan and
sends via `SYSTEM$SEND_SNOWFLAKE_NOTIFICATION` through per-family
`ALERT_ROUTES`. The one manual step Snowflake requires — an ACCOUNTADMIN
creating the NOTIFICATION INTEGRATION holding the webhook secret — is
documented in `webhook_delivery.sql` and the RUNBOOK.

## Performance model (v4.8+)

Render is not the bottleneck; warehouse scans are. The standing rules:
1. **Fact-first with labeled live fallback** — hot panels read the hourly
   facts; the live ACCOUNT_USAGE path survives only as a fallback whose
   source label says so. Hot pages carry pinned live-scan budgets
   (`tests/test_perf_budgets.py`) — new live scans fail CI.
2. **Join-then-group for attribution** — never pre-aggregate all of
   QUERY_ATTRIBUTION_HISTORY; filter the driving window first (the 139s
   lesson).
3. **Tier-grouped batching** — independent same-tier reads go out in one
   `run_batch` (all five tiers: live, recent, hourly, historical, metadata);
   filter-scoped and fixed reads are never
   coupled in one batch cache. Serial cached paths remain as fallback.
4. **Telemetry closes the loop** — slow/failed fetches persist always, plus
   a ~2% sample of everything for the healthy baseline; `batch_fallback`
   events carry tier/size/keys/exception. The Admin → Performance fleet
   table is the optimization queue, ordered by evidence.
The V027 mart family (docs/design/V027_MART_FAMILY.md) shipped as the fact-first
backbone — its scheduled marts replaced the recurring live ACCOUNT_USAGE scans,
with the live builders kept as labeled fallback (the current migration tip is the
range named in `snowflake/validate.sql`'s first check).

