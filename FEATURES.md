# OVERWATCH — Feature Index

One line per capability and where it lives. Guarded by tests (`test_p0_polish.py`) — update this file in the same commit as the feature. Two review rounds missed shipped
features; this is the map that makes that impossible. Deep detail: RUNBOOK.md.

**Section-by-section, metric-by-metric, column-by-column definitions (what does *95s*
mean here? what goes into *that* formula?): [FEATURE_GLOSSARY.md](FEATURE_GLOSSARY.md).**

## Watch (always-on, no humans required)
| Capability | Where |
|---|---|
| ~45 alert rules (cost, perf, pipeline incl. data quality, security, platform, warehouse) with editable thresholds | Alerts → Rules; catalogue in RUNBOOK §12 |
| Webhook delivery **in-chain** (V018): notify task after every scan, guarded auto-resume, live status chip, per-family routing | Alerts (chip at top; routes in Native delivery) |
| Anomaly sweep (robust-z per warehouse/service series) — **events arrive pre-explained by grounded Cortex** | daily task; hypothesis in event DETAIL |
| Contract-breach projection (weekly, CRITICAL ≤14d) | scan rule + Brief KPI + Cost → Contract |
| Fingerprint drift (p95/query-family, no change event needed, Mondays) | `PERF_FINGERPRINT_DRIFT` |
| Change-impact regression tracker (frozen 14d baselines, measured credits/call) | Operations → Change impact |
| Dept budget pace, org account creep, volume drops, COPY/DT failures, credential expiry | scan + sweep rules (break-glass role activity is evidence only on Security → Changes; no alert since V034) |
| Self-monitoring: weekly source sentinel (24 probes) + render-time SLA | `OPS_CANARY_FAIL` / `OPS_SLOW_RENDER`; Admin → Canary |
| Mart reconciliation — totals MATCH the source, not just fresh (±2%/±5% bands) | Admin → Canary |
| Fleet slow/failed fetch telemetry across all viewers (V021) | Admin → Performance |
| Morning AI digest (grounded, Cortex) | Overview expander; daily task |

## Investigate
| Capability | Where |
|---|---|
| Alert drawer: detail, rule, history, playbook, AI explain, Investigate→, **inline closed-loop fix** (warehouse rules), storm rollup toggle | Alerts → Open events |
| Incident correlation timeline (alerts + task failures + DDL, ±30 min drill) | Control Room |
| Day replay: cross-domain "what changed?" for any date, worst-first headlines | Control Room |
| Blast radius on every warehouse suspend/resize confirm (7d users/roles/tags) | Cost → Optimization; Alerts drawer |
| Object TCO: storage $ + reads/writes/last-touch for any reclaim candidate | Cost → Optimization |
| Contract steering: gap-to-commit $/day vs named levers | Cost → Contract |
| Price-a-pattern: $/run now and at ±size steps | Cost → Optimization |
| Monthly alert fire drill with streak scoring (opt-in alert_drill.sql) | Admin → Canary |
| Query-tag governance scoreboard (exec-time-weighted) | Cost → Chargeback & AI |
| Restated-days detector (numbers that moved after close) | Admin → Canary |
| Query drill-through, heaviest queries (row-click), pruning/compile/cache diagnostics | Operations → Queries; Cost → Optimization |
| Most expensive queries in **allocated $** (hour-share model, labeled) | Cost → Optimization |
| Per-rule alert precision from resolution kinds (ACTIONED/NOISE/EXPECTED) | Alerts → Rules |
| Threshold suggestions computed from resolution evidence (keeps ≥90% actioned) | Alerts → Rules |
| Live re-check: is this alert's condition still true right now? | Alerts → drawer |
| Forecast backtest: engine error vs actuals, last 3 months | Overview → forecast expander |
| Retro platform-score trend (30d) from facts | Overview → score card + expander |
| Recurring cost patterns by fingerprint ($/day) | Cost → Optimization |
| Task-graph DAG with failure overlay; pipeline SLAs; stream staleness | Operations → Tasks / Pipeline SLA |
| Attribution (exact per warehouse; labeled-allocated per user/db/role) + waterfall | Cost → Attribution / Chargeback |
| Global jump box (pages, DBs, warehouses, rules) | sidebar |
| Navigation-consistency suite: every routed page/section proven against page source | `tests/test_navigation_consistency.py` |

## Fix (guarded, audited, verified)
| Capability | Where |
|---|---|
| Guarded remediation: auto-suspend and resize execute behind a typed confirm with an audit row (the daily change scan books their saving; the app books ESTIMATED only where the scan can't); an off-hours suspend/resume schedule is a review-only script for a worksheet plus a one-click "Book estimated saving" (one ESTIMATED item per warehouse); retention: a review-only ALTER for a worksheet (the executor never runs CREATE TASK or ALTER TABLE) | Cost → Optimization |
| Interactive right-size what-if (size step + auto-suspend, bounded $ range) | Cost → Optimization |
| Cluster-cap check: a higher MAX_CLUSTER_COUNT is suggested only where a multi-cluster warehouse's queries reached its current maximum (hourly peak cluster, ≥35 days); otherwise "Size up or split" | Cost → Optimization (Idle & sizing) |
| Storage reclaim shortlist: stale AND never-read 90d (ACCESS_HISTORY), with a LEVER per table (Archive or drop / Cut retention; a stale table sharing storage with a clone is 'Check clones', unpriced) whose unread-table $/mo joins the de-duplicated Addressable $/mo (one saving per table) | Cost → Optimization |
| Savings verifier flips ESTIMATED → VERIFIED/REJECTED from actuals monthly | Cost → Savings ledger |
| Emergency levers: suspend/resume WH, warehouse statement timeout, cluster range, scaling policy, pipe pause/resume, task suspend/resume, disable/re-enable user, Cortex allowlist and account statement timeout (account-level: run as SNOW_ACCOUNTADMINS); validated SQL, type EMERGENCY, every execution audited to REMEDIATION_LOG (resource-monitor levers were removed in v4.45) | Operations → Emergency |
| Live query kill-switch (`SYSTEM$CANCEL_QUERY`, audited): each running query needs its own typed CANCEL, and the audit row and toast carry Snowflake's answer (FAILED with its message when the query was no longer running) | Operations → Emergency |

## Plan & report
| Capability | Where |
|---|---|
| Brief (phone-first: numbers, fires, asks, exhaustion date, **ROI: verified savings vs app cost**) | Brief |
| Month-end forecast: linear / seasonal / opt-in `ML.FORECAST` | Overview; `ml_forecast_option.sql` |
| Renewal planner (growth scenarios, recommended commit) | Cost → Contract |
| Department budgets + monthly statement exports | Cost → Chargeback |
| Company all-in showback (warehouse + serverless + Cortex Code + estimated storage by company; cloud-services adjustment and unattributed remainder tie to billed metering + estimated storage) | Cost → Chargeback & AI |
| Billing truth vs app model (org rate card vs credits x rate, monthly) | Cost → Contract & Forecast |
| Styled HTML executive summary; per-table CSV everywhere | Overview; all tables |
| Quarterly access-review export pack (grants matrix, unused roles, 90d diff) | Security → Access |
| Governance-drift + platform scores, both settings-tunable with named deductions | Security / Overview |

## Assurance
| Capability | Where |
|---|---|
| Injection fuzz suite: adversarial corpus through every filter-accepting builder (strip-literals invariant) + refusal checks | `tests/test_injection_fuzz.py` — the pen-test hand-off artifact |

## Platform
| Capability | Where |
|---|---|
| Mart-first facts + hourly/daily loaders; 365d backfill script; retention purge with floors | V002 + `backfill_365.sql` + `SP_PURGE_FACTS` |
| DR runbook: Time Travel undo (INSERT OVERWRITE ... AT / BEFORE), UNDROP, and manual TRANSIENT clones before a risky change. Scheduled operator backups (V015 weekly, V158 daily in `OVERWATCH_BAK`) were retired by V161 | RUNBOOK §16; `rebuild/00_backup_operator_data.sql` |
| Per-user prefs: operator/audit presentation mode (saved to USER_PREFS on toggle). A saved default landing view, display timezone and density are still applied at startup if a USER_PREFS row exists, but there is no in-app editor for them since v4.157.0 | Sidebar → Audit detail toggle; USER_PREFS (V013) |
| Usage analytics (page adoption + render ms) | Admin → Performance |
| Parallel batch fetch, lazy sections, SQL-keyed cache, fragments | core runtime |
| **Design system**: token layer, card variants, severity stripes, SVG icons, sparklines, persistent status bar, refined charts, responsive | app/theme.py, app/ui/icons.py, app/ui/components.py |

## Cost intelligence (v4.7–v4.9)
| Capability | Where |
|---|---|
| Unit costs: measured $ per query, $/call per stored proc (top 50 by measured spend; the per-call leader across every proc), AI $ by function/model with $/1M tokens; Cortex-Code fallback | Cost → Unit costs |
| Task-graph cost trends: $/run, success %, p95 wall, CHEAPER/PRICIER/FLAT per pipeline (db/schema filterable) | Cost → Unit costs (Task-graph pipeline costs) |
| Warehouse change scorecard (V024): snapshot-diff detection, frozen 14d baselines, WH_CHANGE_REGRESSION alerts | Operations → Change impact |
| Contract billing truth: ORGANIZATION_USAGE balance burn-down, runway, on-demand overrun (zero config) | Cost → Contract & Forecast |
| Spend tie-out: billed vs warehouse-exact vs Snowsight (storage/transfer) explained with live numbers | Cost → Spend expander |
| Fact-first hot paths + pinned live-scan budgets (CI fails on new ACCOUNT_USAGE scans on Brief/Overview/Control Room) | tests/test_perf_budgets.py |
| Teams-safe delivery (V026): JSON-escaped payloads + Workflows Adaptive-Card recipe | snowflake/webhook_delivery.sql · RUNBOOK §19 |

## Since v4.9 (July 2026 sprint)

| Capability | Where |
|---|---|
| Nine scheduled marts + tag coverage, fact-first panels with labeled live fallbacks | everywhere (V027-V031) |
| Incident object: declare/auto-declare/proposals, TTD/MTTA/MTTR/reopen/compression, lineage joins | Control Room, Brief (V032) |
| Change attribution: CHANGED_BY + MANAGED/MANUAL vs DEPLOY_ACTORS | Operations scorecard (V033) |
| Per-route company delivery filters (Teams = ALFA-only for now) | alert sender v4 (V034) |
| Measured proc costs: top 50 procedures by measured spend with $/call, a "Priciest procedure (per call)" KPI over every procedure CALLed in the window, price-a-CALL/session, trend-one-procedure by name | Cost -> Unit costs |
| Client driver/version inventory with BEHIND flags + Snowflake's support floor (UNSUPPORTED / nearing end of support, yours vs Snowflake-run; #34) | Security -> Clients |
| Masking / row-access / projection / aggregation policy coverage per database, tag-based masking, same-name environment grouping with each family's databases that have no column-level masking reference (#43) | Security -> Exposure |
| Cortex AI exceptions tracked as Action Center work items: one per user plus the all-users budget scope, through the shared idempotent Track write; a later, stronger signal raises the open item's severity (v4.605) | Cost -> Chargeback & AI |
| Optional panels fail by kind: needs setup only for a missing or ungranted object; schema drift, timeouts and other failed reads show "unavailable" with the error, and Setup progress marks them Unknown (v4.605; v4.606 extends it to Alerts, Overview, Brief and Proof: a failed read is never shown as $0.00, a zero count, an all-clear or "not installed") | Security, Cost, Operations, Admin, Control Room, Alerts, Overview, Brief, Proof |
| Delivery SLOs, alert fatigue, acceptance funnel, per-page cache-hit telemetry | Alerts -> History, Admin -> Performance |
| Flyway-readiness (ledger panel + adoption runbook) | Admin, docs/FLYWAY_ADOPTION.md |
| Partial-success batching (one bad member no longer drags siblings serial) | app-wide (v4.20) |
| KPI source badges (mart/live/stale) + legend popover | Brief first, opt-in everywhere (v4.21+) |
| Tuning queue: pain-ranked targets with click-through to the slow keys | Admin -> Performance (v4.21/4.23) |
| Lock waits marted (46-56 GB scans off page views) + spike watch + source caption | Operations, Control Room (V035/v4.23) |
| "Why stale?" freshness diagnostics (backfill/loader-error/task hints) | Admin -> Migrations & freshness (v4.23) |
