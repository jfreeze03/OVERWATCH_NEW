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
snowflake/migrations/       versioned setup SQL (V001..V005) + SCHEMA_VERSION
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
