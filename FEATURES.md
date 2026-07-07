# OVERWATCH — Feature Index

One line per capability and where it lives. Guarded by tests (`test_p0_polish.py`) — update this file in the same commit as the feature. Two review rounds missed shipped
features; this is the map that makes that impossible. Deep detail: RUNBOOK.md.

## Watch (always-on, no humans required)
| Capability | Where |
|---|---|
| 26 alert rules (cost, perf, pipeline, security, platform) with editable thresholds | Alerts → Rules; catalogue in RUNBOOK §12 |
| Webhook delivery **in-chain** (V018): notify task after every scan, guarded auto-resume, live status chip, per-family routing | Alerts (chip at top; routes in Native delivery) |
| Anomaly sweep (robust-z per warehouse/service series) — **events arrive pre-explained by grounded Cortex** | daily task; hypothesis in event DETAIL |
| Contract-breach projection (weekly, CRITICAL ≤14d) | scan rule + Brief KPI + Cost → Contract |
| Fingerprint drift (p95/query-family, no change event needed, Mondays) | `PERF_FINGERPRINT_DRIFT` |
| Change-impact regression tracker (frozen 14d baselines, measured credits/call) | Operations → Change impact |
| Dept budget pace, org account creep, volume drops, COPY/DT failures, credential expiry, break-glass use | scan + sweep rules |
| Self-monitoring: weekly source sentinel (24 probes) + render-time SLA | `OPS_CANARY_FAIL` / `OPS_SLOW_RENDER`; Admin → Canary |
| Morning AI digest (grounded, Cortex) | Overview expander; daily task |

## Investigate
| Capability | Where |
|---|---|
| Alert drawer: detail, rule, history, playbook, AI explain, Investigate→, **inline closed-loop fix** (warehouse rules), storm rollup toggle | Alerts → Open events |
| Incident correlation timeline (alerts + task failures + DDL, ±30 min drill) | Control Room |
| Query drill-through, heaviest queries (row-click), pruning/compile/cache diagnostics | Operations → Queries; Cost → Optimization |
| Task-graph DAG with failure overlay; pipeline SLAs; stream staleness | Operations → Tasks / Pipeline SLA |
| Attribution (exact per warehouse; labeled-allocated per user/db/role) + waterfall | Cost → Attribution / Chargeback |
| Global jump box (pages, DBs, warehouses, rules) | sidebar |

## Fix (guarded, audited, verified)
| Capability | Where |
|---|---|
| One-click remediation: auto-suspend, off-hours schedules, resize, retention — audit row + ESTIMATED savings | Cost → Optimization |
| Savings verifier flips ESTIMATED → VERIFIED/REJECTED from actuals monthly | Cost → Savings ledger |
| Emergency levers: suspend WH, timeouts, cluster caps, monitor quotas, pipe/task pause, disable user, Cortex allowlist | Admin → Emergency |
| Live query kill-switch (`SYSTEM$CANCEL_QUERY`, audited) | Admin → Emergency |
| Budget ↔ resource-monitor sync | Admin → Emergency |

## Plan & report
| Capability | Where |
|---|---|
| Brief (phone-first: numbers, fires, asks, exhaustion date, **ROI: verified savings vs app cost**) | Brief |
| Month-end forecast: linear / seasonal / opt-in `ML.FORECAST` | Overview; `ml_forecast_option.sql` |
| Renewal planner (growth scenarios, recommended commit) | Cost → Contract |
| Department budgets + monthly statement exports | Cost → Chargeback |
| Styled HTML executive summary; per-table CSV everywhere | Overview; all tables |
| Quarterly access-review export pack (grants matrix, unused roles, 90d diff) | Security → Access |
| Governance-drift + platform scores, both settings-tunable with named deductions | Security / Overview |

## Platform
| Capability | Where |
|---|---|
| Mart-first facts + hourly/daily loaders; 365d backfill script; retention purge with floors | V002 + `backfill_365.sql` + `SP_PURGE_FACTS` |
| Weekly zero-copy backups of operator tables + DR runbook | `TASK_BACKUP_OPERATOR`; RUNBOOK §16 |
| Saved views, default landing, display timezone (per user) | 💾 Views popover |
| Usage analytics (page adoption + render ms) | Admin → Performance |
| Dynamic Table pilot with measured cost | `MART_SPEND_ROLLUP_DT` |
| Parallel batch fetch, lazy sections, SQL-keyed cache, fragments | core runtime |
