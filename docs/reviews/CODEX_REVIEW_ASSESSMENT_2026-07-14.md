# Assessment of Codex's cost review (2026-07-14)

Companion to `docs/design/COSTDB_VS_OVERWATCH_2026-07-14.md`. I verified Codex's
concrete claims against the code at the current tip (post F1-F4 + V046). Short
version: **the review is accurate and high-quality — adopt most of it.** Several
findings restate our own audit; three land on code shipped hours ago; two are
real but lower real-world severity on this account than the P0 label implies;
one recommendation conflates two deliberate lenses.

## Verified findings

| # | Codex | Verified in code | Real severity (Trexis/ALFA today) | Verdict |
|---|-------|------------------|-----------------------------------|---------|
| P0-1 | User/DB cost shifts when a DB filter is applied (owner-company vs warehouse-company scoping) | **Yes** — `mart27_sql.alloc_attribution` scopes `MART_COST_ALLOCATION_DAILY.COMPANY` (owner); `alloc_xdim_attribution` scopes `x.WAREHOUSE_NAME` (warehouse) | Low–Med (bites only cross-company users / shared DBs / shared WHs; our WHs are largely separated) | **Valid** |
| P0-2 | The two attribution paths don't share one formula (mart = EXEC_TIME/all-status/credit-weighted; live = success-only/ELAPSED/size-blind) | **Yes** — this is our own audit **F2**; I caveated it, did not unify | Med (visible mismatch on mart↔live fallback and any schema filter) | **Valid** (escalates F2) |
| P0-3 | Fact comparison windows unequal + caption falsely says both offset 24h | **Yes** — `mart_sql.fact_warehouse_window_vs_prior`: current = `DAY >= today-days` (days+1 dates) vs prior = days dates; live path offsets 24h, fact path does not; `spend.py` caption claims both do | Med–High (skews **every** warehouse comparison by ~1 partial day; cheap to fix) | **Valid** |
| P1 | "Billing truth"/"exact spend" too strong; WH `CREDITS_USED` includes unreduced CS; idle redistributed | Yes — labeling; our R2 caveat covers CS but the strong words remain; idle spread across active users is policy | Med (reporting-integrity, not a math bug) | **Valid** |
| P1 | Per-DB storage $ omits `FAILSAFE_BYTES`; N-day avg ≠ calendar billing month | **Yes** — `ai_chargeback.py:137` `TB = DB_BYTES/1024**4` (reader returns FAILSAFE too, unused); window avg is trailing-N, not MTD | Med (understates per-DB storage by the fail-safe share) | **Valid** (our new F1a code) |
| P1 | Measured query cost omits Query Acceleration | **Yes** — `insights_sql.measured_query_costs` and `V036` mart sum only `CREDITS_ATTRIBUTED_COMPUTE`, never `CREDITS_USED_QUERY_ACCELERATION`; latency labeled ~6h vs docs' ~8h | Low unless QAS is enabled here (verify) | **Valid, conditional** |
| P1 | Org reconciliation buckets on deprecated `USAGE_TYPE` string match | **Yes** — `cost_sql.org_account_month_usd` uses `LOWER(USAGE_TYPE) LIKE '%compute%'…` | Med (fragile classification; use structured dims) | **Valid** |
| P1 | Scope chips imply filters not applied; Environment only narrows the DB picker; residual objects still CASE'd to ALFA not UNKNOWN | Partial — Environment/scope-chip honesty confirmed; `database_visibility_clause` is correctly evidence-based, but a residual-ALFA default may linger in `company_case_sql` | Low–Med (UX honesty + a V044 tail) | **Partially valid — targeted check** |

## What Codex confirms we got right

- Account **billed** credits use the documented formula (`CREDITS_BILLED` → used + CS adjustment).
- Pseudo-warehouses excluded (`WAREHOUSE_ID > 0`).
- **Keep the `1024**4` divisor** — just label the unit **TiB**, don't change the math. This validates audit F3; the only change is a label.
- The July-11 (R1/R2/R5/R6/R7/R9/R10) adoptions landed.

## Where I'd push back or re-scope

- **"Replace the custom attribution engine"** overstates it. The app already uses `QUERY_ATTRIBUTION_HISTORY` for the **measured** lens (unit costs, pattern cost). The elapsed-share engine is the **allocated** lens — "who owns the warehouse bill, idle included," a deliberately different question. Don't replace it; **unify each lens's formula across its paths and label both**. (Measured = QAH compute **+ QAS**; Allocated = warehouse-hour credit share, one builder.)
- **P0 ranking vs real impact here.** P0-1's cross-company shift is real but small on separated Trexis/ALFA warehouses. **P0-3 (window off-by-one) is the higher-value fix** — it touches every comparison and is a two-line change. I'd do P0-3 first, then fold P0-1 and P0-2 into a single "one allocated path" change.

## Recommended sequence

**Now — correctness + honesty, small test-locked diffs (~1 day):**
1. **Equal windows + honest caption** (P0-3): make `fact_warehouse_window_vs_prior` use half-open equal windows and either offset the fact path 24h too or fix the caption to say only the live path is offset.
2. **One allocated path** (P0-1 + P0-2): route unfiltered user/DB through `FACT_COST_ALLOC_XDIM_DAILY` (warehouse-scoped, credit-weighted) so filtered and unfiltered agree; keep the live builder only as an empty-fact fallback and keep its size-blind caveat.
3. **Fail-safe in per-DB storage $** (P1, our code): `TB = (DB_BYTES + FAILSAFE_BYTES)/1024**4`; relabel storage units **TiB**.
4. **QAS**: add `CREDITS_USED_QUERY_ACCELERATION` to `measured_query_costs` and the V036 mart; correct latency copy to ~8h.
5. **Language**: downgrade "billing truth/exact spend" on warehouse/department to "exact **usage** (unadjusted CS, idle included)"; surface an explicit **IDLE** / **CLOUD_SERVICES_REBATE** note.

**Next — data modeling (~a few days):**
6. Rebuild org buckets on `BILLING_TYPE` / `RATING_TYPE` / `SERVICE_TYPE` / `IS_ADJUSTMENT` (retire `USAGE_TYPE` matching); document UTC + 72h-lag + month-close mutability.
7. Calendar-month storage cards: current MTD daily-average + prior completed month; label `STORAGE_USAGE`/table storage as estimate and point reconciliation at org `USAGE_IN_CURRENCY` / `STORAGE_DAILY_HISTORY`.
8. Make **Environment** a real filter (or stop showing it as an applied scope); replace hardcoded DB lists with inventory + `COMPANY_SCOPE`/tags; sweep any residual ALFA default → UNKNOWN in live CASE.

**Architectural — endorse, scope as a project (not this pass):**
9. **Metric registry** (grain, method ∈ {BILLED, MEASURED, ALLOCATED, ESTIMATED}, rate, timezone, latency, formula version). This is the root cure for "multiple semantic contracts" and worth doing before more granular features.
10. Additive **`FACT_OBJECT_COST_DAILY`** (measured compute + QAS, allocated idle, storage estimate, clustering, search-opt, MV refresh, pipes/tasks, explicit residual), with QAH×ACCESS_HISTORY object splitting kept additive and "influenced cost" as a separate non-additive metric.
11. Structured ETL query tags (pipeline, run_id, target_object, environment, cost_center) + ETL unit-cost KPIs ($/run, $/M rows, $/TiB, retry waste, idle %, attribution coverage, unknown %).
12. Filter-matrix + reconciliation + DST tests (shared warehouses, cross-company users, schema fallbacks, idle hours, QAS, unknown objects).

## Bottom line

Codex's central diagnosis is correct and matches ours: the data is fine; the
problem is **multiple semantic contracts** for the same number. Items 1-5 close
the concrete correctness/honesty gaps (two of which are in this week's code) for
about a day of work; 6-8 harden the data model; 9-12 are the real fix and should
be planned as a project with the metric registry first.
