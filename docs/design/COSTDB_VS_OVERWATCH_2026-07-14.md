# COST_DB vs OVERWATCH — differences & cost-formula audit (2026-07-14)

Successor to `docs/design/COSTDB_RECONCILIATION.md` (2026-07-11). That note
compared the two apps and ranked fixes R1–R10. Since then OVERWATCH shipped
V039–V045 and adopted most of those fixes, and the Snowflake ETL went live —
so cost accuracy now has real money behind it. This document restates the
differences for three readers and re-audits the current formulas and filters
at commit `9d18935` (v4.45.0, 836 tests green).

- **COST_DB**: `github.com/jfreeze03/COST_DB` — a single `streamlit_app.py`
  (9,220 lines, "CODE VERSION 2025-03-04-A", 3 commits, no tests). The cost
  dashboard a Snowflake employee shared; it seeded our original cost metrics.
- **OVERWATCH_NEW**: this repo — modular app (`app/core|data|logic|ui`), 45
  SQL migrations, facts + marts + loaders, 836 automated tests.

---

## 1. For the executive

**Both apps answer "where are our Snowflake credits going." Ours answers "what
will the bill actually be, whose budget owns it, and what do we do about it" —
and it is built to be trusted with that answer.**

- **COST_DB is a meter; OVERWATCH is the finance system.** COST_DB reads usage
  and shows it. OVERWATCH converts usage into *billed* dollars (it applies the
  cloud-services rebate Snowflake gives us before invoicing, which COST_DB never
  does — so COST_DB reads high), attributes spend to a company/department, and
  drives alerts and actions.
- **Trust.** OVERWATCH has 836 automated checks guarding the money math;
  COST_DB has none and hard-codes the credit price (and in a couple of places
  prices the same credit inconsistently). For a number going to a budget owner,
  that difference matters.
- **What COST_DB still does better, and we're closing:** it covers a few
  services we don't itemize yet (notably the full spread of storage and some
  newer AI features). These are on the roadmap (items R3/R4 below) and are not
  where the money is on our account today.
- **Audit result:** no errors that would mis-state the bill were found. The
  core dollar math is sound and test-locked. Two refinements remain, both in
  areas already labeled "estimate" in the app: per-database *storage* cost and
  the finer-grained *who-used-it* split. Details below for the team.

**Bottom line:** keep OVERWATCH as the source of truth for spend and
accountability; treat COST_DB as a reference for a few extra service drills.

---

## 2. For the manager / boss

### What each tool is

| | COST_DB | OVERWATCH_NEW |
|---|---|---|
| Shape | 1 file, 9,220 lines, 3 commits, no tests | Modular app, 45 migrations, 836 tests |
| Purpose | Usage meter ("how many credits") | Billing + accountability + action |
| Billing accuracy | Usage only — **no** cloud-services adjustment | **Billed** basis (adjustment applied) |
| Pricing | Hard-coded ($2.00 default; some charts fall back to 2.0/3.0) | From SETTINGS ($3.68 compute / $2.20 AI) + rate-card reconciliation |
| Attribution | Account/warehouse only | Company + department + user/DB + per-query **measured** |
| Projection | Straight run-rate | Linear/seasonal/ML with backtest + contract pacing |
| Scope control | None | Company scoping (Trexis / ALFA / UNKNOWN), tested |

### Where we now stand vs the July 11 plan

Adopted since the last review (V039, v4.30.0): the pseudo-warehouse filter
(R1), the "unadjusted cloud services" caveat on per-warehouse dollars (R2),
correct service categorization (R5), cloud-services-by-statement-type drill
(R6), per-table clustering spend (R7), calendar-year projection (R9), and the
paired credit/$ formatting (R10).

Still open (deliberately deferred, not forgotten):

- **R3 — storage truth.** We track database + fail-safe storage only, and we
  price the *latest day's* size. Snowflake bills the *monthly average* of daily
  size, and also bills stage / hybrid / archive storage we don't yet show.
- **R4 — cost by client application** (what Tableau vs dbt costs us) — designed,
  not built.

### Audit bottom line

The formulas that turn credits into dollars are correct and protected by tests.
The issues we "noticed previously" (a share-renormalization bug, a company
scope leak) were fixed in the current code. The two remaining gaps are storage
accuracy and the coarseness of the live fallback for the user/database split —
both already shown to users as estimates, so this is refinement, not exposure.

---

## 3. For the developer (maintainer)

### 3.1 Architecture & data flow

COST_DB is one module: ~11 `get_base_query` analyzers over `ACCOUNT_USAGE`,
each paired with pandas + Plotly render code (the bulk of the 9,220 lines is
one repeated chart template). Pricing lives in `st.session_state['credit_price']`
with per-call fallbacks (`2.00`, and inconsistently `2.0`/`3.0` in the weekly
and hourly chart helpers — lines ~1826/1884). No separation of query, math,
and view; no tests.

OVERWATCH separates concerns:

- **`app/logic/formulas.py`** — the *only* place credits become dollars.
  `credits_to_usd`, `billed_credits` (used + CS adjustment, sign-guarded),
  `allocate_by_share` (largest-remainder in cents so allocations reconcile to
  the warehouse total with no penny drift), `account_today` (America/Chicago,
  fixes the UTC/CT MTD-boundary bug), `safe_float`/`safe_div`.
- **`app/data/*_sql.py`** — SQL builders. Dollarization never happens in SQL.
- **`snowflake/migrations/V0xx`** — facts (`FACT_WAREHOUSE_DAILY`,
  `FACT_METERING_DAILY`, `FACT_STORAGE_DAILY`), marts, and loaders
  (`SP_LOAD_*` in V041). Pages read marts first, live `ACCOUNT_USAGE` only as
  a bounded fallback.

### 3.2 The formula contract (what's correct — verified)

- **Account billed spend** — `cost_sql._BILLED` and `SP_LOAD_DAILY_FACTS`:
  `COALESCE(CREDITS_BILLED, GREATEST(0, CREDITS_USED + CREDITS_ADJUSTMENT_CLOUD_SERVICES))`.
  Matches Snowflake's documented billed formula; COST_DB has no billed number.
- **Warehouse-exact spend** — `warehouse_daily_credits`,
  `warehouse_window_vs_prior`, all with `WAREHOUSE_ID > 0`; both windows
  offset 24h for `ACCOUNT_USAGE` latency.
- **Pseudo-warehouse filter (R1)** — `WAREHOUSE_ID > 0` in the live builders,
  the loaders, and `backfill_365.sql`; `CLOUD_SERVICES_ONLY` name-filtered in
  the efficiency marts. Locked by `tests/test_v039_costdb_adoptions.py`.
- **Measured unit cost** — `unit_costs.py` + `insights_sql.measured_query_costs`
  / `procedure_costs_usd` / `mart27_sql.pattern_cost` (V036) read
  `QUERY_ATTRIBUTION_HISTORY` (idle excluded, children rolled to the CALL).
  This is exact per-object cost and COST_DB has no equivalent — keep it as the
  headline "what did this cost" lens.
- **No double-count** — `spend.py` shows account-billed (all services) and
  warehouse-exact as *different lenses* and never sums them; the "Why totals
  differ" expander states warehouse-exact is a subset of billed. Good.
- **AI priced at the AI rate** — `spend._categorize` → `ai_rate`;
  `cortex.py` uses `ai_rate_usd` (2.20). No rate mixing.
- **Allocation, mart path** — `mart27_sql.alloc_attribution` reads
  `MART_COST_ALLOCATION_DAILY.ALLOC_CREDITS` (a warehouse-hour *credit* share,
  size-aware); the global denominator avoids the July-11 renormalization leak.

### 3.3 Audit findings (current code, `9d18935`)

| # | Sev | Area | Finding | Fix |
|---|-----|------|---------|-----|
| F1 | **Medium** | Storage $ | `cost_sql.storage_by_database` takes `QUALIFY DAY = MAX(DAY)` — the **latest day's** bytes × rate. Snowflake bills the **monthly average** of daily bytes. Stage/hybrid/archive storage isn't tracked at all (R3). | `FACT_STORAGE_DAILY` already stores per-day average bytes — average `DB_BYTES`/`FAILSAFE_BYTES` over the billing window in the reader instead of snapshotting. Add stage/hybrid/archive arms (R3). |
| F2 | **Low–Med** | User/DB allocation | Dollarization is `ELAPSED_SHARE × window_usd` on every path (`spend.py`). On the **mart** path the share is credit-weighted (size-aware). On the **live fallback** (`cost_sql.allocated_attribution`) and the **schema-filtered** path (always live) it's **elapsed-time** share — warehouse-size-blind. A user concentrated on a large warehouse is under-billed (and vice-versa) whenever the live path is active. | Caveat the caption when serving the live path; or make the live fallback size-aware by joining hourly WMH credits; add a schema-grain mart arm. Also rename the mart's `ELAPSED_SHARE` column (it carries credits) for clarity. |
| F3 | Low | Storage unit base | Storage uses binary units (`GB/1024`, `bytes/1024⁴` = TiB) while `$/TB` pricing is defined by Snowflake on decimal TB (10¹²) for on-demand. ~10% swing. Internally consistent and the rate is a SETTING. | Confirm the seeded `$23/TB` matches the byte base you divide by; document the convention next to `DEFAULT_STORAGE_USD_PER_TB_MONTH`. |
| F4 | Cosmetic | Compute split | `warehouse_daily_credits`: `CREDITS_COMPUTE = COALESCE(CREDITS_USED_COMPUTE, CREDITS_USED)` — if compute is NULL it falls back to total (compute+CS), marginally overstating compute. Rare (compute is seldom NULL). | Use `COALESCE(CREDITS_USED_COMPUTE, 0)` for the compute column; keep the total column as-is. |

Non-issue worth knowing: account-billed (`METERING_DAILY_HISTORY`, UTC daily)
and warehouse-exact (`WAREHOUSE_METERING_HISTORY`, account-LTZ) use different
day boundaries, so their totals won't tie to the cent. They're presented as
separate lenses and never summed — the in-app "Why totals differ" note already
covers it. No change needed.

### 3.4 Suggested order of work

1. **F1 storage monthly-average** (highest accuracy-per-effort; the data is
   already in `FACT_STORAGE_DAILY`). Bundle the R3 stage/hybrid/archive arms.
2. **F2 live-fallback caveat** (cheap honesty fix now; size-aware fallback or
   schema mart later).
3. **F3/F4** alongside the next storage/loader touch.

Every change here should ship with a lock in `tests/` following the existing
`test_v039_costdb_adoptions.py` pattern (assert the predicate/derivation, not
just the output), and update `docs/design/COSTDB_RECONCILIATION.md`.
