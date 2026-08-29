# Architecture

## Layers

```
streamlit_app.py            single entry (SiS and local dev)
app/
  main.py                   shell: header, sidebar nav, page dispatch
  config.py                 constants, thresholds, defaults (pure)
  companies.py              ALFA/Trexis hardcoded scope + KEBARR1 override (pure)
  logic/                    business math — pure Python, no Streamlit, unit-tested
  data/                     SQL string builders — pure Python, no Streamlit, unit-tested
  core/                     runtime: session, cached query engine, errors, state
  ui/                       components, Altair charts, pages
snowflake/migrations/       versioned setup SQL (V001, V002, … one file per change) + SCHEMA_VERSION
tests/                      pytest over logic/, data/, companies, sqlsafe
```

Dependency rule: `logic/` and `data/` import nothing from `core/` or `ui/` and
never import Streamlit. That is what makes them testable in CI without a
Snowflake connection, and it is enforced by code review + the CI test matrix
(CI installs no Streamlit).

## Data flow (mart-first)

1. Scheduled tasks (V002) load compact **fact tables** from ACCOUNT_USAGE:
   hourly (`SP_LOAD_HOURLY_FACTS` — query/warehouse facts) and daily
   (`SP_LOAD_DAILY_FACTS` — metering-daily with cloud-services adjustment,
   tasks, logins, storage). All MERGEs over bounded re-scan windows.
2. After the hourly load, chained tasks refresh `MART_EXEC_BOARD` (the one
   first-paint aggregate) and run the alert scan.
3. Pages read marts/facts first. When a mart object is missing or stale, pages
   fall back to **bounded live aggregates** (fixed short windows, GROUP BY
   pushdown, row caps, tier-cached) and label the source. Live detail drilldowns
   are explicit user actions, never first paint.
4. `MART_SOURCE_FRESHNESS` exposes per-fact freshness; every page shows a
   source + freshness caption. ACCOUNT_USAGE latency (up to ~45 min for query
   history, up to 24h for metering-daily) is labeled, not hidden.

## Query engine (`app/core/query.py`)

- Tiers: `live` 30s / `recent` 300s / `historical` 3600s / `metadata` 14400s
  cache TTL, with matching statement timeouts (30/120/180/30s).
- **Errors are never cached**: the `st.cache_data` functions raise; the public
  `run()` catches outside the cache and returns a typed `QueryResult`
  (`ok/error/truncated/source/fetched_at`). A transient failure can never pin
  an empty frame for the TTL (old-app finding H1).
- Cache keys include company, environment, date window, filters, **and current
  role** — on SiS different users' role-scoped results never cross (old-app C2
  hygiene).
- Row caps fetch `max_rows + 1` and set `truncated`; the UI renders a banner.
  No silent LIMIT injection (old-app M1).
- Statement timeout and query tag are tracked **on the session object**, not in
  `st.session_state`, so a recycled connection cannot desync (old-app M4).
- Query tag: `OVERWATCH|page=<page>|tier=<tier>` for self-cost attribution.

## Error handling contract

- Every page renders inside `safe_page` (app/core/errors.py): exceptions are
  recorded to an in-session ring buffer and best-effort inserted into
  `APP_ERROR_LOG`, then a friendly error renders. Nothing is swallowed
  invisibly; the Admin page lists recent errors.
- Ruff `BLE001` bans blind `except Exception:` everywhere except the three
  sanctioned runtime modules that record what they catch.
- **Empty/absent-state vocabulary (C25):** `components.empty_state(kind, ...)`
  is the one rendering of absence, so color carries meaning — `clean` =
  verified-clean compact green row, `needs_setup` = blue info (configure or
  install first), `no_data_yet` = quiet caption (a successful read returned
  zero rows), `unavailable` = red lead line with the full error one click away.
  `guard()` routes its empty branch through it (callers whose empty is the
  verified-good outcome pass `kind="clean"`; a setup hint is suppressed under
  a clean empty — a successful read proves setup exists) and its error branch
  renders as `unavailable` — except absence-of-setup (the missing-migrations
  error), which stays a calm `needs_setup`. An empty state may carry its next best action (`action_label`
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
  / selected-detail-RIGHT primitive (Action Center, Decision Studio experiments).
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
  confidence renders as ONE bar in every decision-workbench TABLE (portfolio, Action
  Center, Decision Studio scenarios, Entity 360 work list), never a raw float;
  single-value surfaces (the Entity 360 header) keep `confidence_badge`. Authored
  confidence carries one shared help string (`AUTHORED_CONFIDENCE_HELP`) so its
  provenance wording (operator *or* recommendation engine) never drifts per surface.

## Cost formula contract

- Billed account spend: `METERING_DAILY_HISTORY` with
  `CREDITS_USED + CREDITS_ADJUSTMENT_CLOUD_SERVICES` (the adjustment is real
  money; the old app zeroed it).
- Warehouse spend: `WAREHOUSE_METERING_HISTORY` (exact, includes idle).
- User/database spend: allocated from query elapsed-time share (or
  `QUERY_ATTRIBUTION_HISTORY` when present) and always labeled **allocated**.
- Rates come from `SETTINGS` (seeded $3.68 compute / $2.20 Cortex /
  $23 TB-mo). The Admin page edits them with the operator role; code ships
  matching defaults only as offline fallback.
- All conversion math lives in `app/logic/formulas.py` and is regression-tested.

## Security model

- **The security boundary is Snowflake RBAC under Streamlit-in-Snowflake.**
  Each viewer's own role limits what data the app can read for them.
- Company scoping (ALFA vs Trexis) is a shared-account *convenience filter*,
  hardcoded deliberately in `app/companies.py` and seeded to
  `COMPANY_SCOPE` (a pytest keeps code and seed in sync). It is not an
  isolation mechanism and the docs never claim it is.
- User classification: `TRXS_*` → Trexis; explicit override `KEBARR1` → ALFA
  (holds both companies' roles, treated as ALFA by policy).
- Role → navigation profile mapping filters *pages*, not data. Data-changing
  actions (settings updates, alert ack/resolve) require the operator role and
  typed confirmation; everything else generates SQL for a human to run.
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

**Custom alert scan instead of native `CREATE ALERT`.** One task + one proc
evaluates ~26 rules with shared dedupe keys, severity escalation, channel
routing, and rules-as-rows editable in-app. Native ALERTs would mean ~26
separately billed serverless schedules with no shared dedupe or routing and
config drift outside the app. `native_alert_templates.sql` ships for teams
that prefer them. This is a costed choice, not unfamiliarity.

**Scheduled MERGE facts instead of Dynamic Tables everywhere.** Loaders run
on the dedicated XSMALL under a resource monitor — predictable cost, explicit
procs covered by teardown/canary/tests. DTs bill serverless refresh outside
that budget and cannot source SNOWFLAKE share views (no change tracking), so
they cannot replace the ACCOUNT_USAGE loaders anyway. `MART_SPEND_ROLLUP_DT`
(V015) is the measured pilot; more marts migrate if its cost proves out.

**String-built SQL with a safety layer instead of Snowpark binds.** Builders
are pure functions emitting complete statements the app also SHOWS to users
(review-before-execute is a feature). Every user input passes
`clean_filter_text` (whitelist), `contains_filter` (LIKE-metachar escaping,
`ESCAPE '~'`), `sql_literal`/`safe_identifier`, with injection tests in CI.
Snowpark binds would not remove the display/require-review path.

**Hardcoded company scope instead of row access policies.** Two companies,
one account, scope is convenience not a security boundary (roles are). RAPs
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
   `run_batch` (all four tiers); filter-scoped and fixed reads are never
   coupled in one batch cache. Serial cached paths remain as fallback.
4. **Telemetry closes the loop** — slow/failed fetches persist always, plus
   a ~2% sample of everything for the healthy baseline; `batch_fallback`
   events carry tier/size/keys/exception. The Admin → Performance fleet
   table is the optimization queue, ordered by evidence.
The next planned step is the V027 mart family (docs/design/V027_MART_FAMILY.md).

