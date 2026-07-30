# Recommendations review — Codex 20 verified + independent recs — 2026-07-30 (main @ ea88ac6)

Each Codex rec adversarially verified against the current code (5 agents), plus 4 independent
finders, synthesized into a prioritized plan. Tally: **6 CONFIRMED, 12 PARTIAL** (several ~80%
already shipped), **1 REFUTED**. Headline: the real prize is a small cluster of **fail-open /
low-bias correctness bugs on headline exec KPIs** plus **three drill/trust dead-ends on the
morning surfaces** — most of the "add a big new panel / registry / consolidation" items are
either mostly built or over-engineered.

## Codex verdicts

| id | one-line | Verdict | Sev | Eff | Take |
|----|----------|---------|-----|-----|------|
| C1 | Platform score fail-open: outage → 100/Healthy | CONFIRMED | **high** | M | **Do first** — worst-when-degraded. `Incomplete` state + coverage gating + test. |
| C2 | Global date selector silently redefines the health KPI | CONFIRMED | med | M | Agree, **narrow**: pin health signals to a fixed window; keep account-wide inputs; fix false "company-scoped" caption. Reject "make everything company+window". |
| C3 | Never-loaded sources render fresh (NULL ts → 0) | CONFIRMED | med | S | **Do first** — bites at deploy. Add NOT_LOADED; count NULL ts. |
| C4 | KPI counts from capped 500 feed undercount in a storm | PARTIAL | low | S | Agree, cheap. True trigger is >500 open crit+high. One COUNT_IF; **joint with C7**. |
| C5 | Residual UNATTRIBUTED phantom / vanishes under company filter | PARTIAL | low | S | RESIDUAL_USD ~already exists. Drop phantom from object ranking + caption; **decline** the V050 migration. |
| C6 | health_strip scans freshness 2×/metering 3× | CONFIRMED | low | S | Marginal (tiny tables). Fold into next mart_sql touch, not standalone. |
| C7 | Alerts eager-loads 500 rows + SHOWs before section known | PARTIAL | low | M | lazy-sections already shipped; gap is header counts → solve via C4 aggregate. |
| C8 | SP_ALERT_SCAN re-reads config/facts per block → temp tables | PARTIAL | low | M | **Skip** — fights deliberate per-block EXCEPTION isolation; negligible on tiny marts. |
| C9 | Hourly scan re-runs daily-latency rules 24×/day | CONFIRMED | med | M | **Agree** — split into SP_ALERT_SCAN_DAILY after TASK_LOAD_DAILY. Wasted credits. **Owner migration.** |
| C10 | Sidebar refresh bumps one global salt | PARTIAL | low | M | **Skip** — only the clicking session; write-domain salts already exist. |
| C11 | Per-card scope/basis/window chips | PARTIAL | med | S | **Narrow**: add `account-wide` badge to the ~3 misleading Overview cards. No general framework. |
| C12 | Consolidate Brief+Overview+Control Room | REFUTED | low | L | **Decline** — fights the deliberate three-audience split (phone / exec / triage). Product call if owner disagrees. |
| C13 | Compact sticky command bar | PARTIAL | low | M | Compact pass shipped v4.65. Merge status into topbar; sticky is fragile in SiS. |
| C14 | Section pills → st.tabs + drawer | PARTIAL | low | M | **Skip redesign** — st.tabs fires all 7 section queries/rerun, defeating lazy_sections. CSS pill-wrap only. |
| C15 | A11y: labels, color+text, stable entity colors | PARTIAL | med | M | **Two wins**: label floor ≥~11px; stable entity→color on the 2 stacked charts. |
| C16 | Metric registry as enforceable semantic layer | CONFIRMED | low | L | **Way down**: add owner/scope/window/unit/sla fields + opt-in `registry_key`; skip global discovery. |
| C17 | Unified cost reconciliation waterfall | PARTIAL | med | L | Build a **basis-aware variance ladder** (not false-additive), behind a toggle. Backlog. |
| C18 | Expose collected task-node timing mart | PARTIAL | med | M | **Agree** — MART_TASK_NODE_DAILY loads but nothing reads it. p95/queue/late-start/fail table. Drop critical-path (no edges) + SLA (no target). App-only. |
| C19 | Reliability unit economics | PARTIAL | med | M | Much exists (WASTE_USD/RETRY_WASTE). New: $/successful-run + retry ratio + highest-cost-failing. Defer $/SLA. |
| C20 | Unify recommendation ROI scorecard | PARTIAL | low | M | ~80% built. Drop "viewed" (unmeasurable in Streamlit). Add time-to-action + forecast-accuracy. |

## Independent new recommendations (best-first)

| # | Rec | Sev | Eff | File |
|---|-----|-----|-----|------|
| **N1** | Forecasts average today's PARTIAL day into the daily-rate → systematic LOW bias on Projected month-end/year/renewal (same class as the pace/anomaly fixes; never propagated to forecast.py) | **high** | S | forecast.py:49-61; contract.py:126,428 |
| **N2** | "Did the 2am critical actually page anyone?" — undelivered-criticals is *measured* but buried in Alerts→History; morning surfaces show green while criticals paged no one | **high** | M | alerts.py:618; mart_sql.py:1085 |
| **N3** | Control Room triage queue is a read-only dead end — the DBA's one morning list is non-interactive and drops EVENT_ID/RULE_ID so nothing can link out | **high** | M | control_room.py:429; actions.py:94 |
| **N4** | Overview never adopted the run_batch first-paint batching (~9 serial reads) that Brief/Control Room/Cost already moved off | **high** | M | overview.py:175 |
| N5 | Score queue/spill thresholds are spike-sized vs window-cumulative inputs → near-constant deduction; retro sparkline is a different basis than headline | med | M | scoring.py:103; overview.py:250 |
| N6 | Boss chart current-month uses server wall clock (UTC) not account_today() → month-end evening jumps a month early | med | S | overview.py:361 |
| N7 | Exec MTD/Projected dollars silently exclude storage + data-transfer (invoice lines the app reads elsewhere); Overview/Brief don't disclose it | med | S | overview.py:92; contract.py:104 |
| N8 | Score-driver expander is diagnosis with no prescription — no "Investigate →" (the nav pattern already exists in the alert drawer) | med | S | overview.py:432 |
| N9 | Small-frame (4–200 row) tables re-serialize + ship full CSV every 30s rerun (large-frame path was fixed in T1.6; small never got it) | med | S | components.py:674 |
| N10 | Spend-anomaly triage conflates spikes with collapses (abs(z)) — a collapse is usually a stalled loader (a real outage signal) but reads as noise | med | M | actions.py:121 |
| N11 | Contract pace (lifetime-avg burn) contradicts the renewal planner (trailing-30d) on the same page; the more prominent number is the optimistic stale-basis one | med | M | forecast.py:119; contract.py:411 |
| N12 | App writes single-row telemetry/usage INSERTs on the shared XS warehouse (cost-of-the-app); buffer + one multi-row flush | med | M | query.py:93; main.py:279 |
| N13 | SQL month/day boundaries resolve in session TZ (forced UTC) vs marts stored naive Central vs account_today() → MTD windows slide ~5-6h; Brief vs Overview can disagree | med | M | mart_sql.py:523 |
| N14 | Stalest-source badge uses one flat 3h/26h threshold → daily facts render WARN permanently (sibling STALE_SOURCES arm is already cadence-aware) | low | S | mart_sql.py:513 |
| N15 | "Stalest: 7h" names an age but not the source | low | S | brief.py:87 |
| N16 | Renewal what-if prices new *compute* workload at the AI-blended rate (under-costs the scenario) | low | S | contract.py:422 |
| N17 | Chargeback never nets the account cloud-services rebate → dept statements sum above billed | low | M | ai_chargeback.py:250 |
| N18 | run() computes SHA1 over full SQL on every call (used ~2% of the time) — hash inside the should-persist gate | low | S | query.py:597 |

## Recommended sequence

**DO FIRST (app-only, cheap, highest trust/value):** C1 · N1 · C3 · N2 · N3 · N6 · C4+C7 (joint) · N8 · N9 · N15+N14.
**NEXT (medium):** C1/C2+N5 (one scoring rework) · N4 (Overview batch) · C18 (task-node panel) · C11-narrow + N7 · N10 · N12 · C15 (2 wins) · N11.
**BACKLOG:** C19 · C17 (variance ladder) · C16 (S-slice) · C20 · C5-app · C6 · C13 · N13 (careful, multi-file) · N16/N17/N18.
**SKIP/DECLINE:** C12 (design conflict) · C8 (isolation) · C10 (session-only) · C14 (defeats lazy_sections).
**OWNER MIGRATION:** C9 (SP_ALERT_SCAN_DAILY split) — clean credit win, owner-applied.

**Net:** the DO-FIRST block is ~10 cheap app-only changes fixing the two headline-KPI trust bugs
(fail-open score, low-biased projection) and the three morning-surface dead-ends (undelivered
criticals, triage actionability, score drill) — more owner trust than C16/C17/C20 combined.
