# Metrics triage — 2026-07-29 (post-v4.65.0)

7-domain multi-agent sweep (spend/billing, attribution/chargeback, cloud-services,
efficiency/query, security/scorecards, forecast/contract, storage/pipeline), each
candidate adversarially filtered (refute-by-default) against the current code.
**13 findings survived: 12 CONFIRMED, 1 PLAUSIBLE.**

**Headline:** core billed-dollar and warehouse-spend metrics hold (house facts
intact). The systemic weakness is **mart-vs-live drift** — 8 of 13 are mart
builders that diverged from their live twin on the primary (`run_mart_first`)
served path — plus two exec-headline KPIs that fail **directionally toward hiding
problems** (month-end projection understated most of the month; platform score
overstated up to 21 pts). Fixes below are not yet applied — this is the triage.

Fix location: **APP** = Python builder/wiring (no migration); **MIG** = loader/mart
DDL (needs V059).

---

## HIGH

### 1. [CONFIRMED · APP] Month-end projection understated ~60–70% most of the month
- **Metric:** Projected month-end spend (flagship exec forecast KPI) · `app/ui/pages/overview.py:202`
- `month_end_projection` is fed the exec-board `DAILY_SPEND` frame, which
  `mart_sql.exec_board` windows to `WINDOW_DAYS` (default 7); `forecast.py` sums MTD
  only over rows in the frame, so past day-of-month ~8 the MTD base is a single week
  and the projection understates ~60–70% (can read below the neighboring full-month
  MTD KPI).
- **Fix:** project from a full-month frame — reuse the already-loaded 150d `_bt_hist`
  (build a DAY/USD frame, pass to `month_end_projection`), and the same for `mtd_now`
  in the `ml_forecast` branch (overview.py:184-186); fall back to `daily` only when
  `_bt_hist` is unusable.

### 2. [CONFIRMED · MIG] Task-graph pipeline cost reads ~$0 for proc-driven tasks
- **Metric:** Pipeline WH_CREDITS / pipeline spend / $-per-run · `SP_LOAD_MARTS_V27` arm [6]
- Arm [6] rolls `QUERY_ATTRIBUTION_HISTORY` by **bare QUERY_ID** while the live
  `graph_sql` builder uses `COALESCE(ROOT_QUERY_ID, QUERY_ID)` (the audit #10 fix that
  landed app-side in v4.60 but never reached this mart arm). The mart serves by
  default, so for `CALL`-body tasks the child compute is pruned and WH_CREDITS
  collapses to ~0; the live twin returns the true credits.
- **Note (honesty):** this is a pre-existing arm-[6] bug, but V058's CHANGELOG line
  "arm [6] already carries pipeline credits" was too generous — it carries them
  *wrong* for proc tasks. Correct that note.
- **Fix:** V059 re-derives arm [6] to mirror `graph_sql` (`COALESCE(ROOT_QUERY_ID,
  QUERY_ID)` in the attribution SELECT/prune/GROUP BY, join `a.ROOT_ID = h.QUERY_ID`)
  + a mart-arm test asserting the ROOT rollup.

---

## MEDIUM

### 3. [CONFIRMED · APP] Platform score silently drops two penalties (overstated up to 21 pts)
- **Metric:** Platform health score (0–100 exec KPI) · `app/ui/pages/overview.py:226`
- The live caller passes 7 signals and never `stale_sources` / `open_high_actions`
  (only tests do), so the Stale-telemetry (cap 12) and Owner-queue (cap 9) drivers +
  their `SCORE_PTS_PER_STALE_SOURCE`/`_PER_OPEN_ACTION` weights can never fire live —
  the score reads up to 21 pts high exactly when telemetry is stale or HIGH actions
  are open, contradicting `scoring.py:138-140`.
- **Fix:** wire `open_high_actions` (from the already-loaded ACTION_QUEUE) and
  `stale_sources` (freshness vs `THRESHOLDS.stale_fact_hours`) into the signals dict;
  or delete the two inert drivers + weights and fix the docstring/caption.

### 4. [CONFIRMED · MIG or APP] Idle-credit KPI spreads day credits uniformly across hours
- **Metric:** Idle spend / projected monthly idle ($ KPI + savings ledger) · `app/data/mart27_sql.py:178`
- `eff_idle_analysis` derives `IDLE_CREDITS = SUM(CREDITS_TOTAL * IDLE_PCT/100)` where
  `IDLE_PCT` is a pure hour-COUNT ratio (no per-hour credit info) — a uniform spread;
  the live builder sums metered `CREDITS_USED` of query-less hours. They diverge under
  non-uniform per-hour credits (multi-cluster scale-out, partial active hours).
- **Fix:** add a true `IDLE_CREDITS` column to `MART_WAREHOUSE_EFFICIENCY_DAILY`
  (anti-join like the live builder) and SUM it directly (**MIG**); or relabel the KPI
  as an hour-count approximation (**APP**).

### 5. [CONFIRMED · MIG] Compile-heavy families COMPILE_PCT/AVG_TOTAL_S use exec time, not elapsed
- **Metric:** COMPILE_PCT / AVG_TOTAL_S · `app/data/mart27_sql.py:234`
- Uses `SUM(TOTAL_EXEC_SEC)` (EXECUTION_TIME only) for both the AVG and the
  COMPILE_PCT denominator while live uses `TOTAL_ELAPSED_TIME`; for the compile-
  dominated families this view selects, total-compile / execution-only-time yields
  **COMPILE_PCT > 100%**.
- **Fix:** add a stored `TOTAL_ELAPSED_SEC` to `MART_QUERY_FAMILY_DAILY` (V027 loader)
  and use it as numerator + denominator (bounds 0–100%).

### 6. [CONFIRMED · APP] Rate-card reconciliation compares all-service credits vs compute-only $
- **Metric:** Model-vs-org rate-card DELTA_PCT · `app/ui/pages/cost_parts/contract.py:152`
- `model_by_month` sums `CREDITS_BILLED` from `fact_daily_spend(70)` with **no service
  filter** (FACT_METERING_DAILY carries AI/Cortex rows) priced at the compute rate,
  while `org_usd = COMPUTE_USD` excludes AI — so DELTA_PCT is biased up whenever the
  account uses Cortex, and the caption invites "fixing" the global rate (which would
  corrupt every dollar).
- **Fix:** a compute-only model builder (`CREDITS_BILLED` with `NOT (SERVICE_TYPE
  ILIKE '%CORTEX%'/'%AI%'/'%INTELLIGENCE%')`) for `model_by_month`.

### 7. [CONFIRMED · MIG or APP] Tag-coverage scopes company by USER (mart) vs WAREHOUSE (live)
- **Metric:** Query-tag coverage (tagged share / top untagged user) · `app/data/mart27_sql.py:433`
- Mart filters `COMPANY` stamped via `COMPANY_FOR_USER` (user-home, all warehouses);
  live filters `warehouse_clause` on WAREHOUSE_NAME — so for `company != ALL` the two
  aggregate different populations.
- **Fix:** add WAREHOUSE_NAME grain to `MART_TAG_COVERAGE_DAILY` stamped
  `COMPANY_FOR_WAREHOUSE` (**MIG**); or force the live warehouse-scoped path when a
  company filter is active (**APP**). Do NOT switch the live builder to user basis.

### 8. [CONFIRMED · APP] CS-ratio mart surfaces CLOUD_SERVICES_ONLY + near-idle warehouses
- **Metric:** Cloud-services ratio per warehouse (ELEVATED + drill trigger) · `app/data/mart_sql.py:230`
- `fact_cloud_services_ratio` has only `HAVING SUM(CREDITS_TOTAL) > 0` — missing the
  live builder's `WAREHOUSE_ID > 0` (CLOUD_SERVICES_ONLY exclusion) and the
  `>= 0.5` near-idle floor — so it surfaces a `CLOUD_SERVICES_ONLY` row at
  CS%=100/ELEVATED (sorted first) and spuriously triggers the compile-heavy drill.
- **Fix:** add `AND UPPER(WAREHOUSE_NAME) <> 'CLOUD_SERVICES_ONLY'` and change the
  HAVING to `>= 0.5` to match live.

### 9. [CONFIRMED · APP] Cloud-services 'Cache %' collapses to 0% or 1%
- **Metric:** AVG_CACHE_PCT (CS shape cache %) · `app/data/mart_sql.py:261`
- `AVG_CACHE_PCT = ROUND(SUM(CACHE_PCT_SUM)/NULLIF(SUM(RUNS),0), 0)` over V055's
  fraction (0.0–1.0, no ×100) rounded to 0 decimals → every row is 0 or 1 (a 60%-cache
  shape renders "1%").
- **Fix:** `... /NULLIF(SUM(RUNS),0)*100, 0)` (or `*100,1` with `%.1f%%`).

### 10. [PLAUSIBLE · APP] MFA-gap definition differs: governance HAS_MFA vs Access EXT_AUTHN_DUO
- **Metric:** MFA-gap user count (governance vs Access panel) · `app/data/security_sql.py:19`
- `governance_counts` + posture mart use `HAS_MFA`; `users_without_mfa[_live]`
  (Access "Users needing MFA now") filter `EXT_AUTHN_DUO` (Duo-specific), despite
  `governance.py:61` asserting one definition — a native-MFA (non-Duo) user is a false
  positive on the Access panel.
- **Fix:** standardize both on `HAS_MFA` (security_sql.py:19,:57), keeping the
  password-login evidence. *(PLAUSIBLE — confirm the account's MFA columns live.)*

---

## LOW

### 11. [CONFIRMED · MIG] Schema-hourly 'queued' excludes provisioning-queue time
- `FACT_QUERY_SCHEMA_HOURLY` loader uses `QUEUED_OVERLOAD_TIME` only, but the
  `query_window_summary` contract + sibling `FACT_QUERY_HOURLY` include OVERLOAD +
  PROVISIONING; a schema filter silently drops provisioning-queue seconds.
- **Fix:** loader arm → `OVERLOAD + PROVISIONING`, backfill affected days.

### 12. [CONFIRMED · APP] Query-window mart vs live use different anchors (CURRENT_TIMESTAMP vs CURRENT_DATE)
- `fact_query_window_summary`/`schema_window_summary` anchor rolling `days*24h`; live
  anchors midnight `days` ago — same labeled tile covers a different span (up to ~2×
  near end of day for days=1). `mart_sql.py:67`, `mart27_sql.py:357`.
- **Fix:** standardize the anchor (prefer `CURRENT_DATE` to match live + fact_warehouse_pressure).

### 13. [CONFIRMED · APP] 'Why totals differ' mislabels the account-wide warehouse total
- `spend.py:104` prints the account-wide, rebate-netted `wh_usd` as "Overview's
  company-scoped, warehouse-exact KPI" — mis-attributed on two axes inside the panel
  meant to reconcile them.
- **Fix:** reword to present `wh_usd` as the account-wide, rebate-applied warehouse
  portion of *this page's* billed spend (Overview ≤ it, and rebate-free).
- **Correction (v4.68.1 verify round):** the prescription's direction claim was
  itself wrong — Overview prices UNADJUSTED usage, so at company=ALL (or a dominant
  company) it reads *above* the rebate-netted `wh_usd`, by ~the rebate. The shipped
  wording states the different basis and both directions instead of an inequality.
  Also: `wh_usd` *includes* reader metering, so "reader" must not be listed in the
  remainder bucket.

---

## Suggested remediation split

- **App batch (no migration):** #1, #3, #6, #8, #9, #10, #12, #13 (+ #4/#7 if taken as relabel / force-live).
- **Migration V059:** #2 (arm [6] ROOT_QUERY_ID), #5 (TOTAL_ELAPSED_SEC column), #11 (schema queued); + #4/#7 if taken as true mart columns.
- Each fix to be adversarially verified before applying (per house discipline), same as Batch A/B.
