# Cloud Services Driver Intelligence / Metadata Chatter Detection — Investigation Report

**Date:** 2026-09-23 · **Status:** Investigation only (no production code changed) · **Repo:** OVERWATCH_NEW
**Method:** 13-agent adversarial workflow (5 codebase archaeologists + 3 Snowflake-mechanics researchers → 3 design agents → 2 refute-by-default verifiers). Every SQL column was checked against live DDL or docs.snowflake.com; billing claims and "already-built" claims were adversarially audited.

> **One-line verdict:** The concept is technically valid and worth building, but it is **~60% already built** and must be delivered as a **focused extension of the existing Cost ▸ Spend cloud-services surface + a new driver/application-attribution cut**, not a new dashboard. Ship the pieces that are genuinely new (metadata classifier, compile‑vs‑CS join, application attribution, resize‑not‑indicated verdict, per‑entity anomaly baseline); **reuse** the shipped scorer/anomaly/advise machinery rather than cloning it.

---

## 1. Executive Summary

OVERWATCH already detects and partially explains cloud-services (CS) spikes. The panel in the screenshot — `Cloud-services drivers on WH_ALFA_QA — compile-heavy query families` — is **live today** on **Cost Intelligence ▸ Spend & Attribution ▸ "Cloud-services health by warehouse"** (`app/ui/pages/cost_parts/spend.py`), fed by `cost_sql.compile_heavy_families`. So the request is not "build CS analytics from scratch"; it is "turn the existing compile-heavy panel into a **driver-attributed, classified, baseline-aware, resize-aware** experience."

The single most important truth the investigation confirmed (and the design honors): **warehouse-level and query-level *billable* cloud services cannot be measured.** The ~10% free-allotment rebate is computed **per account, per day, in UTC**, against a shared pool that excludes serverless. Snowflake exposes it only at account+day grain (`METERING_DAILY_HISTORY.CREDITS_ADJUSTMENT_CLOUD_SERVICES` / `CREDITS_BILLED`). Every per-warehouse or per-family CS number is **gross USAGE**. Any warehouse/family billable figure is an *inference*, never a measurement — and OVERWATCH's current posture (show gross usage at warehouse grain, reserve billable to account+day) is architecturally correct and must be preserved.

The observed pattern is a **textbook metadata-chatter / compile-dominated discovery** case, **not** a warehouse-sizing problem. `SYSTEM$FBE_CAPTURE_FILE_REVISION_HISTORY` at 410 runs / 99.7% compile / ~550 ms is Snowsight **Workspaces** file-revision capture (FBE = *File-Based Entities*), server-issued, near-100% compile because its "work" is metadata/optimizer resolution in the cloud-services layer with ~0 warehouse execution. **Resizing WH_ALFA_QA would change none of it.** The remedy is behavioral/tooling, owned by app/BI/governance teams — exactly the distinction this feature should surface.

**Adversarial verdict:** the 14 SQL prototypes are **column-clean (zero hallucinated columns)** and **billing-honest** (never dollarizes per-family CS, never splits the account rebate to a warehouse). The defects are architectural, not factual, and are all fixable before build: a **dual-score identity clash**, a **CS>0 mart-censoring** gap for zero-credit storms, an **absent `SESSION_ID`** in the mart/extract that blocks application attribution, and **methodology overlap** with the already-shipped `query_opt` OOS scorer.

---

## 2. What OVERWATCH Already Supports

| Capability | Where | What it does | Source |
|---|---|---|---|
| **The observed compile-heavy panel** | `cost_parts/spend.py` `_spend_tab` L737–787 → `cost_sql.compile_heavy_families` L837 | Per-warehouse drill: groups `QUERY_HISTORY` by `QUERY_PARAMETERIZED_HASH`, `HAVING COUNT(*)>=5 AND AVG(COMPILATION_TIME)>500ms`, `ORDER BY SUM(COMPILATION_TIME) DESC LIMIT 25`. **This produces the exact Runs / Avg Compile / Avg Total / Compile% columns in the screenshot.** | live `ACCOUNT_USAGE.QUERY_HISTORY` |
| Account-wide twin of the above | `mart27_sql.family_compile_heavy` L290 | Same output contract, run-weighted, from a mart | `MART_QUERY_FAMILY_DAILY` |
| CS-ratio-by-warehouse (the selectable table above the drill) | `mart_sql.fact_cloud_services_ratio` L404 / live `cost_sql.cloud_services_ratio_by_warehouse` L802 | CS share = `(TOTAL−COMPUTE)/TOTAL`; STATUS `ELEVATED>20% / WATCH>10% / NORMAL` | `FACT_WAREHOUSE_DAILY` / `WAREHOUSE_METERING_HISTORY` |
| "CS credits by statement type" | `cost_sql.cs_by_query_type` L1078 + `mart_sql.cs_by_query_type_mart` L499 (byte-identical via `common.cs_by_query_type_projection`) | Surfaces SHOW/DESCRIBE metadata storms by `QUERY_TYPE` | `MART_CLOUD_SVC_DAILY` |
| CS credits by query shape & by user/role (V055 toggle drill) | `mart_sql.cloud_svc_top_shapes` L455, `cloud_svc_by_user` L480 | The closest existing thing to CS-credit-per-shape attribution | `MART_CLOUD_SVC_DAILY` |
| NL answerer "why did CS spike" | Ask intent `cloud_services_spike_by_query`, `ask/registry.py` L240 | Names biggest CS shape, heaviest user, elevated warehouses | reuses the 3 builders above |
| First-response playbook + alert | `playbooks.py` `COST_CLOUD_SVC_RATIO` L10; alert fires >20% | Frames "many tiny queries / metadata-heavy / compile-heavy" and points at the panel | `ALERT_CONFIG` |
| Per-family × warehouse × **day** CS+compile grain | **`MART_CLOUD_SVC_DAILY` (V055)** | `DAY × COMPANY × WAREHOUSE × USER × ROLE × QUERY_TYPE × QUERY_PARAMETERIZED_HASH`; cols `RUNS, CS_CREDITS, EXEC_SEC_SUM, COMPILE_SEC_SUM, CACHE_PCT_SUM` | `OW_QH_EXTRACT` |
| Robust anomaly engine | `anomaly.robust_zscores` (median/MAD 0.6745, mean-AD 0.7979) + server `SP_ANOMALY_SWEEP` (V097) + `EXPECTED_SPIKE_CALENDAR` | 28-day baselines, spike suppression | marts |
| Shipped opportunity scorer | `query_opt.score_opportunities` / `OOS` (percentile-rank impact, winsorized, "no naive multiply") + `query_advisor.advise` (spill/queue/compile_bound findings w/ per-run guards) | The methodology the new score would re-tread | `QUERY_HISTORY` fingerprints |
| Session→application attribution pattern | `app_cost_sql.py` L84–115 `_APP_EXPR` | `QUERY_HISTORY.SESSION_ID = SESSIONS.SESSION_ID` (deduped) → `CLIENT_ENVIRONMENT:APPLICATION` / `CLIENT_APPLICATION_ID` driver+version | `SESSIONS` |

**Net-new (does not exist today):** a semantic **driver classifier** (metadata-chatter taxonomy), a panel that joins **compile-time AND CS-credits at one grain** (the loaded-but-unread `MART_CLOUD_SVC_DAILY.COMPILE_SEC_SUM` column — grep-clean of any reader), **application/driver attribution of chatter**, **connection-churn** detection, a **RESIZE-NOT-INDICATED** verdict, and a **per-entity CS baseline** (vs. the fixed 10/20% threshold).

---

## 3. Current Data Lineage

```
ACCOUNT_USAGE.QUERY_HISTORY
   └─(single hourly scan, watermarked)→ OW_QH_EXTRACT  [transient, ~72h, V041/V055]
         ├→ FACT_QUERY_HOURLY / FACT_QUERY_DAILY          (user×db×wh grain; no family, no compile, no CS)
         ├→ MART_QUERY_FAMILY_DAILY                        (family×company; COMPILE_MS_AVG; no CS, no wh)
         ├→ MART_CLOUD_SVC_DAILY  ★                        (family×wh×user×role×qtype×DAY; CS_CREDITS + COMPILE_SEC_SUM; CS>0 only)
         └→ MART_COST_ALLOCATION_DAILY (exec-share INFERRED credits)

ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY → FACT_WAREHOUSE_DAILY (wh×day; gross CREDITS_TOTAL/COMPUTE; CS = TOTAL−COMPUTE, derived)
ACCOUNT_USAGE.METERING_DAILY_HISTORY     → FACT_METERING_DAILY  (account×service×day; the ONLY billable-CS netting: CREDITS_BILLED)
ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY  → MART_PATTERN_COST_DAILY (compute-attributed $; explicitly excludes cloud services)
```

★ = the natural integration point. The observed panel is **not** mart-backed today — it scans `QUERY_HISTORY` live (clamped to `MAX_LIVE_WINDOW_DAYS=90`, ~1m36s cold), so it cannot trend beyond 90 days.

Loaders: `SP_LOAD_QH_EXTRACT` (hourly) fills the extract + cascades `SP_LOAD_CLOUD_SVC_MART`; `SP_LOAD_DAILY_FACTS` (06:45 CT) fills metering. All `EXECUTE AS OWNER`, exception-isolated per arm. `SP_PURGE_FACTS` owns retention (hourly ≥90d, daily ≥365d).

---

## 4. Findings From the Observed WH_ALFA_QA Pattern

| Family | Runs | Compile % | Reading |
|---|---|---|---|
| `CALL SYSTEM$FBE_CAPTURE_FILE_REVISION_HISTORY` | 410 | 99.7% | **Snowsight Workspaces** file-revision capture (FBE), server-issued. Metadata-only; ~0 warehouse execution. **Resize irrelevant.** |
| `SYSTEM$GET_CLASSIFICATION_STATUS_WITH_ELIGIBILITY` | 13 | ~99.6% | Governance/Classification UI per-object probe. Benign. |
| `SYSTEM$CORTEX_MODEL_ACCESSIBLE` | 8 | ~99.8% | Cortex entitlement/capability probe. Benign. |
| `SELECT DISTINCT UPPER(COLUMN_NAME)… INFORMATION_SCHEMA.COLUMNS` | 7–10 | 3–4% (19–23 s) | BI/IDE **column discovery**. Low compile %, real row work → different class from the SYSTEM$ calls. |
| `show /* JDBC:DatabaseMetaData.getPrimaryKeys() */` | 6–12 | 48–80% | **JDBC driver** metadata call (comment is driver-injected). Pure metadata. |
| `SELECT $1 FROM @…EDW_STAGE/…/Parameter/ALFA_LIFT` | 5 | — | **ETL stage/parameter-file** read (metadata + cloud-services op). |

**Conclusion:** the elevation is dominated by metadata chatter and compile-heavy discovery (Snowsight tooling + BI/JDBC introspection + ETL stage reads), not under-sizing. The levers are behavioral (cache driver metadata, reduce catalog-refresh cadence, quiet chatty workspaces/tools) — owned by app/BI/data-eng/governance, **not** the DBA's sizing knob.

---

## 5. Snowflake Cloud Services Mechanics (verified)

- Compilation, metadata operations (SHOW/DESCRIBE/USE, `INFORMATION_SCHEMA`, DDL/clone), authentication, and access control **consume only cloud-services resources**, not virtual-warehouse compute.
- Therefore a metadata/optimizer statement has `COMPILATION_TIME/TOTAL_ELAPSED_TIME ≈ 100%`, `EXECUTION_TIME ≈ 0`, and frequently `WAREHOUSE_NAME IS NULL`. Near-100% compile is **expected**, not pathological.
- **Resizing scales only that warehouse's compute credits/hour.** It is independent of the cloud-services layer, so it cannot reduce a near-100%-compile family. Up/down-sizing WH_ALFA_QA does nothing to these rows.
- The three observed `SYSTEM$` functions are **real but undocumented** Snowflake internals (absent from the System Functions index); their callers are inferred from naming + the FBE framework, labeled as hypotheses (§14).

**Correlation vs. attribution vs. measurement:** run counts, compile%, elapsed times = **MEASURED**. Per-query CS credits = **MEASURED usage** (gross). Which user/tool/session emitted a family = **ATTRIBUTED** (self-reported, spoofable). Billable CS at warehouse/family grain = **INFERRED** (not observable). Never blur these.

---

## 6. Cloud Services Billing vs. Usage

**The load-bearing constraint.** Billable CS is defined at **account + day (UTC)**:

```
BILLABLE_CS(day) = MAX(0, daily_CS_usage − 0.10 × daily_virtual_warehouse_compute_credits)
CREDITS_ADJUSTMENT_CLOUD_SERVICES = −MIN(daily_CS_usage, 0.10 × daily_compute)   (serverless excluded)
```

| Grain | View | Gross CS usage | Billable CS |
|---|---|---|---|
| account × day | `METERING_DAILY_HISTORY` | `CREDITS_USED_CLOUD_SERVICES` ✅ | `CREDITS_ADJUSTMENT_CLOUD_SERVICES`, `CREDITS_BILLED` ✅ **(only place billable is a fact)** |
| warehouse × hour | `WAREHOUSE_METERING_HISTORY` | `CREDITS_USED_CLOUD_SERVICES` ✅ | ❌ **no adjustment / billed column exists** |
| query | `QUERY_HISTORY` | `CREDITS_USED_CLOUD_SERVICES` ✅ (gross, ~6h extra lag) | ❌ |
| query | `QUERY_ATTRIBUTION_HISTORY` | ❌ (compute+QAS only) | ❌ (docs explicitly exclude cloud services) |

**Why non-decomposable:** the 10% subtraction is one account-day threshold against a shared pool. A warehouse's share of the rebate is not observable; two warehouses' gross CS cannot be individually netted without arbitrarily splitting the free pool (allocation by gross-CS share vs. by compute share give different answers — the non-uniqueness *is* the proof it's inferred).

**OVERWATCH already implements the correct posture:** `FACT_METERING_DAILY.CREDITS_BILLED = COALESCE(CREDITS_BILLED, GREATEST(0, CREDITS_USED + CREDITS_ADJUSTMENT_CLOUD_SERVICES))` at account+day; warehouse grain shows only the gross ratio as a diagnostic. **Preserve this.** The UI must present three distinct things and never conflate them:

- **Measured CS Usage** (per warehouse/family — gross)
- **Estimated/Attributed CS Usage** (per driver/app — attributed, self-reported)
- **Account-Level Billing Impact** (account+day — the only authoritative billable figure, shown as a reconciling control)

---

## 7. Metadata Chatter Detection Design

A **12-class taxonomy**, precedence-ordered, using multiple signals (never pure text-match), each with a HIGH/MEDIUM/LOW confidence rule:

1. **SYSTEM GENERATED** — `IS_CLIENT_GENERATED_STATEMENT` (new column) + `SYSTEM$`/`EXECUTE STREAMLIT` signature + `QUERY_TAG LIKE 'OVERWATCH%'` + NULL warehouse + ~100% compile. (Reuses the shared self-noise/CALL exclusion set, *inverted* to surface rather than hide.)
2. **OBJECT/CORTEX/GOVERNANCE DISCOVERY** — `SYSTEM$GET_CLASSIFICATION*` / `SYSTEM$CORTEX_MODEL_ACCESSIBLE` / governance SHOW·DESCRIBE.
3. **JDBC/ODBC DISCOVERY** — `/* JDBC:DatabaseMetaData.* */` / `getPrimaryKeys|getTables|getColumns` driver comment + SHOW/DESCRIBE.
4. **INFORMATION_SCHEMA** — `QUERY_TEXT ILIKE '%INFORMATION_SCHEMA%'` (runs as `QUERY_TYPE='SELECT'`, so text is required).
5. **STAGE-FILE** — `@stage` path / `GET_FILES`·`LIST_FILES` / `SELECT $1 FROM @…`.
6. **APP INIT** — `USE`/`ALTER SESSION`/`SHOW_PARAMETERS`, correlated with `SESSIONS.CREATED_ON` (first-in-session).
7. **CONNECTION CHURN** *(session-dimension, not a family)* — high sessions/hour per (app,user), short session lifetimes, high login rate.
8. **METADATA CHATTER** — generic residual: NULL warehouse + exec≈0 + high compile% + meaningful RUNS.
9. **COMPILE HEAVY** — warehouse-**backed** work where the compile phase dominates (`COMPILE_RUN_PCT≥0.5`, elapsed≥1s) — mirrors `query_advisor.advise` `compile_bound`.
10. **HIGH FREQUENCY** — frequency-as-pathology: RUNS/hr far above the family's own baseline and/or tight periodic inter-arrival (polling).
11. **NORMAL** — warehouse-backed, exec-dominated, in-baseline (so the panel can say "fine").
12. **UNKNOWN** — `QUERY_PARAMETERIZED_HASH` NULL / text truncated / below floor. Bucketed explicitly, never dropped; its share is a **coverage metric**.

**Key signal columns** (all verified `exists=yes`): `IS_CLIENT_GENERATED_STATEMENT` (BOOLEAN, **net-new to repo**), `QUERY_TYPE`, `QUERY_TEXT`/`SAMPLE_TEXT`, `WAREHOUSE_NAME` (NULL ⇒ metadata), `COMPILATION_TIME`/`EXECUTION_TIME`/`TOTAL_ELAPSED_TIME`, `QUERY_PARAMETERIZED_HASH`, `QUERY_TAG`. Use **per-run guards** (`AVG(IFF(ratio>0.5,1,0))`), not `AVG(ratio)`, to avoid the bimodality trap the repo already fought (bug-hunt R1–R3).

---

## 8. Application / Driver Attribution

The join chain (existing pattern, `app_cost_sql.py` L84–115):

```
QUERY_HISTORY.SESSION_ID = SESSIONS.SESSION_ID   (dedup SESSIONS: QUALIFY ROW_NUMBER() OVER(PARTITION BY SESSION_ID ORDER BY CREATED_ON DESC)=1)
   → program = TRY_PARSE_JSON(SESSIONS.CLIENT_ENVIRONMENT):APPLICATION::STRING   (VARCHAR holding JSON — must TRY_PARSE_JSON)
   → driver  = SESSIONS.CLIENT_APPLICATION_ID   (family + trailing version, e.g. 'JDBC 3.13.30')
   → optional SESSIONS.LOGIN_EVENT_ID = LOGIN_HISTORY.EVENT_ID → REPORTED_CLIENT_TYPE
```

**Verified column reality:** `SESSIONS.{SESSION_ID, CLIENT_APPLICATION_ID, CLIENT_APPLICATION_VERSION, CLIENT_ENVIRONMENT, AUTHENTICATION_METHOD, LOGIN_EVENT_ID}` all exist. **`LOGIN_HISTORY.CLIENT_TYPE` does NOT exist — use `REPORTED_CLIENT_TYPE`.**

**Two blockers to fix first:**
- `SESSION_ID` is **absent from both `MART_CLOUD_SVC_DAILY` and `OW_QH_EXTRACT`** (the design's "SESSION_ID already present" was a false claim — corrected here). Application attribution requires either **adding `SESSION_ID` to `OW_QH_EXTRACT`** (it's on `QUERY_HISTORY`) and stamping `APPLICATION` at load via the deduped-SESSIONS join, **or** running the attributed cut **live** off `QUERY_HISTORY` (the `SESSION_ID` join is valid at that grain).
- **Latency mismatch:** `QUERY_HISTORY` ~45 min but `SESSIONS` ~3 h and billable `METERING` ~3 h (per-query CS ~6 h). Any SESSIONS-joined view must guard the served window and label recent-hours periods partial.
- Driver/app identity is **self-reported and spoofable**; expect sizeable `(unknown)/(not reported)` buckets (ODBC/legacy tools) — bucket them, don't drop them.

---

## 9. Cloud Services Driver Score

**Design:** a `CLOUD_SERVICES_DRIVER_SCORE` at `(QUERY_PARAMETERIZED_HASH × WAREHOUSE_NAME)` grain — a **weighted sum of 7 unitless ECDF percentile ranks** (no naive multiply):

```
Eligibility gate (HARD, before any ranking): SUM(RUNS) ≥ MIN_RUNS_FLOOR (20 acct / 5 per-wh) AND active_days ≥ 3;
   exclude CLOUD_SERVICES_ONLY pseudo-wh + OVERWATCH self-noise. Percentiles via SQL PERCENT_RANK() OVER the FULL eligible
   population (never a run()-capped pandas frame).

SCORE = 100 × ( 0.25·p_cs + 0.22·p_compile + 0.15·p_ratio_adj + 0.12·p_freq + 0.13·s_anom + 0.08·p_breadth + 0.05·p_burst )
```

- **p_cs** — gross CS credits usage (top weight, but labeled usage, never dollarized).
- **p_compile** — `SUM(COMPILE_SEC_SUM)` — the reducible cloud-services burden; the primary leg when CS≈0 (pure storms).
- **p_ratio_adj** — compile ratio with **empirical-Bayes shrinkage** toward the population median (`K=20`) so a 1-run 99%-compile query collapses to background — the statistical answer to "compile-ratio alone must be volume-gated."
- **p_freq** — `SUM(RUNS)` (the legitimizer of the ratio leg).
- **s_anom** — upside-only median/MAD modified z vs. 28-day baseline (separates "newly high" from "chronically high").
- **p_breadth** — Shannon entropy across users/apps (systemic ⇒ one fix kills chatter fleet-wide ⇒ higher priority).
- **p_burst** — CoV/Gini of the runs series (episodic ⇒ one fixable job).

**Worked example (FBE):** 410 runs, 99.7% compile, ~221 s total compile ⇒ score **≈ 55/100** (unmistakably compile-shaped but modest real burden, not anomalous, single-source) — correctly ranked **below** any family that moves real CS credits or just spiked. A naive `freq × ratio × compile × credits` product would have detonated on the 0.997 ratio and mis-ranked this benign Snowsight background job as #1. That inversion is exactly what the robust composite prevents.

**Why defensible:** percentile ranks are unitless (fixes seconds-vs-credits-vs-counts), bounded, outlier-robust; the anomaly leg reuses the shipped median/MAD engine; usage is never dollarized. **Caveats:** the 7-weight vector is a *justified default*, not uniquely optimal — expose it as tunable and validate against a labeled set. See §17 for the required fixes (dual-score clash, CS-censoring, breadth wiring).

---

## 10. Historical Baseline / Anomaly Detection

Replace the fixed 10/20% CS-ratio threshold with a **per-entity baseline**: baseline per `WAREHOUSE × QUERY_PARAMETERIZED_HASH × seasonal bucket (hour-of-day, day-of-week)` using the **existing** `anomaly.robust_zscores` (median/MAD 0.6745, mean-AD 0.7979) over a trailing 28-day window, matching `SP_ANOMALY_SWEEP` (V097). Score on **CS_CREDITS** *and* on **RUNS/runs-per-hour** (to catch metadata storms that carry ~0 CS but real chatter). Suppress benign recurring discovery via `EXPECTED_SPIKE_CALENDAR` + a new DRIVER_CLASS-keyed expected-workload list (SYSTEM GENERATED, governance/Cortex probes, nightly-ETL windows). Gate materiality on RUNS/CS-volume floors, **not** the $50-USD floor built for warehouse spend (sparse ETL-night families would never score). For trend-up (not just spike), reuse `forecast._robust_slope` (Theil-Sen on calendar-day offsets). This is exactly what stops a chronically compile-heavy warehouse like WH_ALFA_QA from re-firing every day (the alert-fatigue trap).

**Recommended method:** median/MAD modified z + per-entity/seasonal baselines + Theil-Sen trend — **all already in the codebase**; add a CS SERIES arm to `SP_ANOMALY_SWEEP`, no new statistics.

---

## 11. Warehouse Resize Relevance

Per family, decide from time-decomposition + contention, **not** cost:

- **RESIZE NOT INDICATED** — compile/metadata-dominated (high `COMPILE_PCT`, tiny `EXEC_PCT`) AND low `QUEUE_PCT` AND `SPILL_GB≈0`, and/or `WAREHOUSE_NAME IS NULL`. The cost lives in the cloud-services layer; resizing changes only compute credits. → behavioral/tooling owners. **The FBE family lands here.** *(Genuinely new verdict — keep it.)*
- **RESIZE MAY HELP** — `EXEC_PCT` dominant AND a real contention signal: spill above floor (⇒ more memory ⇒ larger) or `QUEUED_OVERLOAD` high (⇒ scale up / multi-cluster), with compile low. *(Should reuse `query_advisor.advise` spill/queue findings + per-run guards rather than re-deriving thresholds — see §17.)*
- **INSUFFICIENT EVIDENCE** — RUNS below floor, per-run guards contradict averages (bimodal), NULL warehouse for a work-claim, or spill/queue columns unavailable.

Evidence columns (verified): `COMPILATION_TIME, EXECUTION_TIME, TOTAL_ELAPSED_TIME, QUEUED_OVERLOAD_TIME, QUEUED_PROVISIONING_TIME, BYTES_SPILLED_TO_LOCAL_STORAGE, BYTES_SPILLED_TO_REMOTE_STORAGE, WAREHOUSE_NAME/SIZE, RUNS`. Never present a resize verdict as a billable-dollar saving.

---

## 12. Recommendation Engine

Each recommendation carries **evidence · likely cause · confidence · owner · investigation · expected effect**, and never auto-disables legitimate functionality:

| Owner | Trigger classes | Actions |
|---|---|---|
| **DBA (warehouse/cost)** | ELEVATED CS where drivers are COMPILE HEAVY (resize-not-indicated) or genuinely RESIZE MAY HELP | Reconcile to account+day billable **first**; resize **only** for spill/queue; tune auto-suspend for churn; route compile/metadata drivers to the owners below |
| **Application team** | APP INIT / SYSTEM GENERATED / HIGH FREQUENCY via an internal app/service driver | Pool/reuse connections (each connect re-runs the init+discovery batch), memoize/batch metadata calls, lengthen polling |
| **BI / IDE owner** | JDBC/ODBC DISCOVERY + INFORMATION_SCHEMA storms from a BI/IDE program | Enable driver metadata caching, lower schema-refresh cadence, scope catalog to used schemas, upgrade drivers |
| **Data Engineering** | STAGE-FILE listing / parameter-file reads from an ETL principal | Batch/cache stage listings, adopt directory tables, consolidate parameter reads |
| **Security / Governance** | OBJECT/CORTEX/GOVERNANCE probes | Usually benign UI/entitlement probes → add to an EXPECTED-workload suppression list; investigate only an unexpected principal or out-of-band spike |

---

## 13. Proposed UI (extend, don't duplicate)

**Placement — split by axis** (this resolves a real duplication risk the audit flagged):
- **Warehouse-grain CS-driver family table** (folds today's `compile_heavy_families` + `cs_by_query_type` into one, adds DRIVER_CLASS/CONFIDENCE/RESIZE verdict) → **stays on Cost ▸ Spend & Attribution**, repointed to the mart via `run_mart_first` (reaches 365d; kills the ~1m36s live scan).
- **Driver / application-attributed cut** ("which client/driver is generating the chatter", `SESSIONS × QUERY_HISTORY`) → **new, on Operations ▸ Queries** (`_queries_tab`). This is the genuinely-new axis and does **not** rebuild the Cost panel.

Sections, each mapped to an existing primitive:

| Section | Reuse |
|---|---|
| Header + data-derived health badge | `section_header` + `alarm_health` (from the CS anomaly result, not the fixed threshold) |
| Ranked CS-driver family table (class, confidence, runs, compile%, CS usage, resize verdict) | `run_mart_first` → `selectable_table` on the existing `_sel_wh` selection |
| Class rollup KPI band | `kpi_row` / `metric_card_html` (pass raw value+unit; durations humanize Hr/Min/Sec) |
| Driver/application attribution (Operations) | `selectable_table` + `section_filter_contract`, reusing `app_cost_sql._APP_EXPR` |
| Per-family / resize-verdict drill + Snowsight deep link | `result_caption` + `methodology_note` (audit-gated) + `snowsight_profile_column` |
| AI narrative | extend Ask intent `cloud_services_spike_by_query` to name the top DRIVER_CLASS + owner |
| Empty / USAGE-vs-BILLABLE disclosure | `empty_state` + caption stating CS figures are **gross usage before the ~10% account rebate**; disclose the mart's CS>0 filter + 72h left-censoring |
| Newly-elevated badge | `alarm_health` ← `anomaly.flag_anomalies` + `suppress_expected_spikes` |

Add `metric_registry.COLUMN_HELP` entries for `DRIVER_CLASS / COMPILE_PCT / CS_CREDITS / RESIZE_VERDICT / DRIVER / PROGRAM`.

---

## 14. Investigation of `SYSTEM$FBE_CAPTURE_FILE_REVISION_HISTORY`

- **FBE = File-Based Entities** — a documented Snowflake platform framework that manages files as native objects **and tracks git-like version/revision history**. It is the backbone of **Snowsight Workspaces** (file-based, with Git integration).
- **`SYSTEM$FBE_CAPTURE_FILE_REVISION_HISTORY` is real but undocumented** (absent from the System Functions index; grep-clean of the OVERWATCH repo — OVERWATCH does **not** emit it).
- **Hypothesis (high confidence):** it is **Snowflake-generated**, fired by the Snowsight Workspaces file editor / Git integration to capture a file's revision into FBE history. **410 runs** = active workspace editing (autosave / save / git-sync events) by a handful of users, with WH_ALFA_QA as their session/current warehouse. It is **not** client- or OVERWATCH-generated.
- **Near-100% compile is expected** (a `SYSTEM$` metadata call: work is in cloud services, execution is trivial). **Resize is the wrong lever.**
- **Is reducing frequency appropriate?** Only if that account's CS is actually **over** the 10% free tier (i.e. billable) *and* the activity is unnecessary. If CS stays under 10% of daily compute, the 410 runs are **free** and reduction is cosmetic. Confirm billable impact at account+day **before** treating it as a cost problem. Confirm the user/session/tool attribution via a live `QUERY_HISTORY` drill (USER_NAME/SESSION_ID/CLIENT_APPLICATION_ID) — labeled a hypothesis until then.

Same shape applies to `SYSTEM$GET_CLASSIFICATION_STATUS_WITH_ELIGIBILITY` (Snowsight governance UI probe) and `SYSTEM$CORTEX_MODEL_ACCESSIBLE` (Cortex entitlement probe) — real, undocumented, benign UI/entitlement probes.

---

## 15. SQL Prototypes (14) — status

All 14 exist in the design and were **column-verified against live DDL / docs** (zero hallucinated columns). Mart-backed where possible; live only where a column/row isn't in a mart.

| # | Prototype | Source | Mart-backed | Note |
|---|---|---|---|---|
| P1 | CS usage trend / warehouse | `FACT_WAREHOUSE_DAILY` | ✅ (365d) | gross usage; CS = TOTAL−COMPUTE |
| P2 | CS-to-compute ratio + 10% allotment (account/day) | `FACT_METERING_DAILY` | ✅ | `BILLABLE_CS`/`CREDITS_BILLED` authoritative; 10% test illustrative (excludes serverless) — **fix per §17** |
| P3 | Top compilation-heavy families | `MART_CLOUD_SVC_DAILY` | ✅ | compile% excludes queue (no `TOTAL_ELAPSED` in mart); CS>0 only |
| P4 | Metadata chatter detection | `QUERY_HISTORY` | live | uses `IS_CLIENT_GENERATED_STATEMENT` (net-new) |
| P5 | JDBC/ODBC discovery (driver-attributed) | `QUERY_HISTORY`+`SESSIONS` | live | driver self-reported; ~3h latency floor |
| P6 | INFORMATION_SCHEMA discovery | `QUERY_HISTORY` | live | text-match; 90d clamp |
| P7 | System-function discovery | `QUERY_HISTORY` | live | SYSTEM$ names are data values, undocumented internals |
| P8 | Query-family frequency | `MART_CLOUD_SVC_DAILY` | ✅ | gross usage |
| P9 | Application/user attribution | `QUERY_HISTORY`+`SESSIONS` | live | self-reported; `(unknown)` bucket |
| P10 | Session-level sequencing | `QUERY_HISTORY` | live | `LIMIT 2000` (transport-cap guard) |
| P11 | Per-warehouse CS baseline (28d median/MAD) | `FACT_WAREHOUSE_DAILY` | ✅ | **reuse `SP_ANOMALY_SWEEP`, don't re-implement — §17** |
| P12 | Per-warehouse CS anomaly (mod. z) | `FACT_WAREHOUSE_DAILY` | ✅ | same |
| P13 | CS Driver Score (composite) | `FACT_WAREHOUSE_DAILY`+`MART_CLOUD_SVC_DAILY` | ✅ | **⚠ conflicts with §9's 7-component score — §17** |
| P14 | Resize-relevance | `FACT_WAREHOUSE_DAILY`+`MART_CLOUD_SVC_DAILY` | ✅ | inferred verdict; 20% threshold matches app |

The prototype SQL bodies are in the workflow output; they are ready to lift **after** the §17 fixes.

---

## 16. Performance / Cost of OVERWATCH Itself

- **Mart-first, no new `ACCOUNT_USAGE` scan.** The recommended path extends `OW_QH_EXTRACT` (the single hourly `QUERY_HISTORY` scan already taken) and `MART_CLOUD_SVC_DAILY`; the classifier/score/resize verdicts are computed in app logic from the mart. New columns populate on the existing MERGE — **zero extra `ACCOUNT_USAGE` reads.**
- Mart-backed tiles reach 365d and **eliminate the observed panel's ~1m36s live per-warehouse `QUERY_HISTORY` scan**.
- **Transport cap:** `run()` truncates at `DEFAULT_MAX_ROWS=5000`. A per-family CS mart can have many rows/day — never derive a window total/min/max/SUM from a capped detail frame; use `SUM(...) OVER ()` / uncapped aggregates. Compute percentiles/entropy/CoV in SQL, not app-side over a capped feed.
- **Latency-aware:** guard the served window; label recent-hours (< ~3h for SESSIONS-joined, < ~6h for per-query CS) as partial.
- Live cuts (P4–P7, P9, P10) stay interaction-gated (toggle), 90d-clamped, as the current panel already is.

---

## 17. False-Positive Risks & Required Fixes (from the adversarial audit)

**Must fix before build:**
1. **Dual-score identity (HIGH).** The §9 7-component family×warehouse composite and prototype **P13**'s 3-component warehouse-grain `CS_DRIVER_SCORE` (0.50 CS-share + 0.30 compile + 0.20 runs, linear caps) are two different scores under one name. **Pick one** — keep the family×warehouse composite as canonical; rebuild P13 as a warehouse roll-up of it (or delete P13).
2. **CS>0 mart censoring (HIGH).** `MART_CLOUD_SVC_DAILY` filters `CREDITS_USED_CLOUD_SERVICES > 0` *before* GROUP BY, so **zero-credit metadata storms have no mart row at all** — the compile-time leg cannot "carry" them there, and RUNS/baseline/burstiness are computed on a CS-censored, gappy series. **Do not relax the shared mart's CS>0 filter** (it would break the byte-identical live twin `cs_by_query_type`). Instead serve zero-CS storms from the **live** path (P4) or a **new hourly sibling** `MART_CS_DRIVER_HOURLY` with the filter dropped; document that mart RUNS is CS-bearing-only.
3. **Absent `SESSION_ID` (HIGH).** Not in `MART_CLOUD_SVC_DAILY` **or** `OW_QH_EXTRACT`. Application/breadth attribution requires adding `SESSION_ID` (+ stamping `APPLICATION` at load via deduped SESSIONS) or running the attributed cut live. Correct the "already present" assumption.

**Should fix:**
4. **Self-noise gate location (MED).** The `QUERY_TAG`/`QUERY_TEXT` self-noise exclusion isn't evaluable on the mart (only `SAMPLE_TEXT`). Move it into the **loader** (reads `OW_QH_EXTRACT`, which has `QUERY_TAG` + `LEFT(QUERY_TEXT,200)`). Note: the observed `compile_heavy_families` builder applies **no** self-noise/CALL exclusion today — import the `ops_sql` triage set as a shared, test-locked constant.
5. **Reuse the shipped scorer (MED).** The composite re-treads `query_opt.OOS` methodology. Prefer adding a `metadata_chatter`/`compile_dominant_discovery` **Finding** to `query_advisor.advise` + `query_opt._PATHOLOGY` with a CS-credit/compile-sec impact leg, rather than a second divergent 0–100 scorer. (If the standalone composite is kept, justify why alongside OOS.)
6. **P2 10% test (MED).** `FREE_CS_ALLOTMENT`/`ALLOTMENT_STATUS` sum compute across all `SERVICE_TYPE` (serverless included) and read as authoritative. Derive OVER/WITHIN from the **measured** `CREDITS_ADJUSTMENT_CLOUD_SERVICES` / `CREDITS_BILLED`, or rename the column to mark it illustrative + serverless-excluded.
7. **C4 label (MED).** Relabel the top-weighted CS leg "attributable **gross** CS usage (not billable)"; co-display the account+day `CREDITS_BILLED` control **adjacent** to the ranked table, never as a per-family dollar.
8. **Anomaly/resize reuse (LOW).** Don't hand-roll median/MAD (P11/P12) — add a CS arm to the byte-locked `SP_ANOMALY_SWEEP`. Base RESIZE-MAY-HELP on `advise()`'s existing spill/queue findings + per-run guards; only RESIZE-NOT-INDICATED is net-new.

**Standing false-positive guards:** per-run guards not `AVG(ratio)`; hard frequency floor + Bayes shrinkage so a lone high-compile query never ranks; per-entity baseline so chronically-high ≠ newly-high; NULL `QUERY_PARAMETERIZED_HASH` bucketed as UNKNOWN, not dropped.

---

## 18. Snowflake Telemetry Limitations (state honestly)

- **Warehouse/query billable CS is not measurable** — only account+day. Any warehouse/family billable figure is inferred.
- **Driver/app identity is self-reported** (`CLIENT_APPLICATION_ID`, `CLIENT_ENVIRONMENT:APPLICATION`, `REPORTED_CLIENT_TYPE`) — spoofable, with real `(unknown)/(not reported)` tails.
- **`LOGIN_HISTORY.CLIENT_TYPE` does not exist** (it's `REPORTED_CLIENT_TYPE`).
- **Latency:** QUERY_HISTORY ~45 min, LOGIN_HISTORY ~2 h, SESSIONS ~3 h, METERING ~3 h, per-query CS ~6 h — SESSIONS-joined views are bounded by the slowest leg.
- The three observed `SYSTEM$` functions are **undocumented**; their callers are inferred (hypotheses), not confirmed by Snowflake.
- `MART_CLOUD_SVC_DAILY` is **CS>0-only** and left-censored (post-deploy, ~72h-freeze MERGE) — not a full historical CS ledger.
- Completeness of the very-lightest metadata ops in QUERY_HISTORY is not doc-guaranteed — treat as uncertain, not 100%.

---

## 19. Implementation Complexity

| Component | Effort | Notes |
|---|---|---|
| Repoint compile-heavy panel to the mart (fold in cs_by_query_type) | **S** | `run_mart_first`; unused `COMPILE_SEC_SUM` already loaded |
| Metadata classifier (12-way) + confidence | **M** | pure logic; reuse triage exclusion set; add `IS_CLIENT_GENERATED_STATEMENT` to extract |
| Resize-relevance verdict | **S–M** | reuse `advise` for MAY-HELP; NOT-INDICATED is new |
| Per-entity anomaly baseline | **M** | add CS arm to `SP_ANOMALY_SWEEP` (owner migration + byte-lock) |
| Application/driver attribution | **M** | needs `SESSION_ID` in extract (owner migration) or a live cut |
| Driver score | **M** | resolve dual-score; expose weights; validate vs. labeled set |
| Connection-churn detection | **M** | SESSIONS+LOGIN_HISTORY session-dimension view |
| Hourly sibling `MART_CS_DRIVER_HOURLY` | **M** (optional) | only if hour-of-day seasonality is required |

Every mart/proc change carries the repo's full migration lockstep (validate floor, `_EXPECTED_MIGRATIONS`, rebuild bundle regen, per-migration test, `SP_PURGE_FACTS`/retention, teardown/roles/canary, 4-pin version + CHANGELOG, floor-compat + mypy CI). Byte-locked procs (`SP_LOAD_QH_EXTRACT`, `SP_ANOMALY_SWEEP`, `SP_PURGE_FACTS`) must be re-derived from their **current** definition + enumerated edits (the V047-from-V036 / V148-from-V073 drift class).

---

## 20. Recommended Implementation Plan (phased, owner-gated migrations staged via runbox)

1. **Phase 0 — app-only, no migration (S):** repoint the Cost ▸ Spend compile-heavy panel to `MART_CLOUD_SVC_DAILY` via `run_mart_first` (surface the unused `COMPILE_SEC_SUM`), add the **metadata classifier** + **RESIZE-NOT-INDICATED** verdict + the USAGE-vs-BILLABLE in-panel disclosure. Delivers the "why + who-owns-it + resize-irrelevant" story immediately, live-fallback for zero-CS storms.
2. **Phase 1 — extend the extract (owner migration):** `ALTER OW_QH_EXTRACT ADD IS_CLIENT_GENERATED_STATEMENT, SESSION_ID, QUEUED_*_TIME, BYTES_SPILLED_*` (re-derive `SP_LOAD_QH_EXTRACT` from current + byte-lock); stamp `APPLICATION`/`REPORTED_CLIENT_TYPE` into `MART_CLOUD_SVC_DAILY` via deduped SESSIONS. Unlocks application attribution + connection-churn.
3. **Phase 2 — per-entity anomaly (owner migration):** add a CS SERIES arm to `SP_ANOMALY_SWEEP` + an `ALERT_CONFIG` rule mirroring `COST_CLOUD_SVC_RATIO`; replaces the fixed 10/20% threshold with newly-vs-chronically-high.
4. **Phase 3 — driver score (app):** implement the single canonical composite (or the `advise` Finding), weights tunable, validated against a labeled driver set; account+day `CREDITS_BILLED` shown as the reconciling control.
5. **Phase 4 (optional):** `MART_CS_DRIVER_HOURLY` if hour-of-day seasonality proves necessary.

---

## Per-Component Verdicts

| Component | Verdict | Why · Data required · Confidence · Effort · Value |
|---|---|---|
| **Metadata Chatter Detector (classifier)** | **GO** | *Why:* genuinely new (no semantic family classifier exists); the compile-vs-CS join over `MART_CLOUD_SVC_DAILY.COMPILE_SEC_SUM` is read by nothing today. *Data:* verified columns + `IS_CLIENT_GENERATED_STATEMENT` (add to extract). *Confidence:* High. *Effort:* M. *Value:* High — turns "CS spiked" into "which behavior." |
| **Resize-Relevance Detection** | **GO** | *Why:* RESIZE-NOT-INDICATED is net-new and directly answers the WH_ALFA_QA question. *Data:* compile/exec/queue/spill (verified). *Confidence:* High. *Effort:* S–M (reuse `advise` for MAY-HELP). *Value:* High — stops wrong sizing actions. |
| **Application / Driver Attribution** | **GO (after Phase 1)** | *Why:* the missing axis (who generates the chatter); reuses `_APP_EXPR`. *Data:* `SESSIONS`/`LOGIN_HISTORY` verified — but `SESSION_ID` must be added to the extract (or run live). *Confidence:* Med-High (self-reported, ~3h lag). *Effort:* M. *Value:* High — routes to the right owner. |
| **Anomaly Detection (per-entity baseline)** | **GO** | *Why:* replaces the fixed 10/20% threshold; kills alert fatigue on chronically-high warehouses. *Data:* `MART_CLOUD_SVC_DAILY` + existing `SP_ANOMALY_SWEEP`. *Confidence:* High (reuses shipped math). *Effort:* M. *Value:* High. |
| **Cloud Services Driver Score** | **MODIFY** | *Why:* sound math, but ships as **two contradictory scores** (§9 vs P13) and re-treads the shipped `query_opt` OOS methodology. *Fix:* one canonical composite (or an `advise` Finding), tunable weights, validated. *Confidence:* Med. *Effort:* M. *Value:* Med-High once deduped. |
| **Connection Churn Detection** | **GO (after Phase 1)** | *Why:* explains chatter volume that per-family compile% can't; net-new. *Data:* `SESSIONS`+`LOGIN_HISTORY` (verified). *Confidence:* Med. *Effort:* M. *Value:* Med-High for reconnect-loop cases. |
| **Billing Impact (account-level)** | **GO** | *Why:* the only *correct* CS billing surface; OVERWATCH already computes it (`FACT_METERING_DAILY.CREDITS_BILLED`). Show it as the reconciling control beside gross usage. *Data:* account+day (verified authoritative). *Confidence:* High. *Effort:* S. *Value:* High (prevents overstatement). |
| **Warehouse/Family *Billable* CS attribution** | **DO NOT IMPLEMENT** (as a measured number) | *Why:* provably non-decomposable — the rebate is account+day against a shared pool. Only ever expose as a clearly-labeled **inferred allocation** with the account total as control, never a measured column. *Confidence:* High (authoritative). |
| **Relaxing `MART_CLOUD_SVC_DAILY` CS>0 filter** | **DO NOT IMPLEMENT** | *Why:* would break the byte-identical live twin and silently change existing readers. Serve zero-CS storms from the live path or a new hourly sibling instead. *Confidence:* High. |
| **Standalone hand-rolled anomaly SQL (P11/P12)** | **MODIFY → reuse** | *Why:* re-implements byte-locked `SP_ANOMALY_SWEEP` math (drift risk). Add a SERIES arm instead. *Effort:* low delta. |
| **UI: new standalone CS dashboard** | **DO NOT IMPLEMENT** | *Why:* duplicates the live Cost ▸ Spend panel. Extend it; put only the new driver/application axis on Operations. *Confidence:* High. |

**Overall:** **GO to build**, as a phased **extension** — Phase 0 (app-only classifier + resize verdict + disclosure) delivers most of the value with no migration; the attribution/anomaly/score phases follow behind owner-applied migrations. The two hard "no"s are structural (non-decomposable billable CS; don't relax the shared mart filter), and the one "modify" that matters is de-duplicating the score.
