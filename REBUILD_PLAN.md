# OVERWATCH Rebuild Plan

Goal: full functional parity with the old app's six sections, rebuilt small,
honest, and tested. Every phase closes specific findings from the 2026-07-07
panel review of the old repo.

## Decisions (locked)

1. **Scope:** full parity — 6 sections + Admin (setup/health moved off the exec page).
2. **Tenancy:** ALFA/Trexis hardcoded (shared account), isolated in `app/companies.py`
   + `COMPANY_SCOPE` seed, sync-tested. `KEBARR1` → ALFA by override.
3. **Deploy target:** Streamlit-in-Snowflake primary (per-user roles = real access
   control). Community Cloud / local = dev only, documented.
4. **Data:** mart-first (tasks load compact facts hourly/daily; exec board mart for
   first paint). Live ACCOUNT_USAGE fallback exists but is bounded, aggregate-only,
   cached, and labeled.
5. **Rates:** $3.68 compute / $2.20 Cortex / $23 TB-mo seeded in settings.

## Phases

- [x] **P0 — Scaffold**: README, plan, architecture/deploy docs, ruff (BLE001 on),
      pinned deps, CI (lint + tests), gitattributes/ignore, Streamlit config.
- [x] **P1 — Logic layer** (pure Python, tested): formulas (credits→USD, deltas,
      allocation), anomaly (median/MAD robust z-score), forecast (month-end
      projection + band), scoring (evidence-based platform score with named
      drivers), actions (ranking + savings-ledger states).
- [x] **P2 — Data layer** (pure SQL builders, tested): single `sqlsafe` module;
      company scoping clauses; cost SQL (METERING_DAILY_HISTORY **with**
      cloud-services adjustment, warehouse metering, attribution, Cortex, storage,
      contract pacing); ops SQL (query/task/warehouse/contention); security SQL
      (MFA-with-login-evidence, failed logins, grants, DDL); mart readers;
      settings reader.
- [x] **P3 — Core runtime**: session (SiS-first, query tags, session-object-tracked
      timeouts), tiered cached query engine (raise-inside-cache: errors are never
      cached; role+scope in cache key; visible truncation), error boundary + sink,
      filter state + query-param navigation.
- [x] **P4 — Snowflake migrations**: V001 core (db/schemas/settings/company
      scope/schema_version/error log), V002 facts (6 fact tables, hourly/daily
      MERGE procs, WH_ALFA_OVERWATCH XSMALL + resource monitor, chained tasks),
      V003 marts (exec board + control-room snapshot + freshness view),
      V004 alerts (config/events/audit + scan proc + native templates),
      V005 actions (action queue + savings ledger). Roles + validate scripts.
- [x] **P5 — UI**: components (st.metric KPIs, freshness captions, truncation
      banners, honest empty states), Altair charts (dollar axes, budget rule,
      forecast band), 7 pages, role-filtered navigation with deep links.
- [x] **P6 — Verify & ship**: ruff clean, pytest green, commit sequence, push.

## Non-goals for this pass (deliberate)

- No Cortex Analyst / LLM features yet (the old app's "Ask OVERWATCH" name
  promised AI it didn't have; we won't repeat that — it ships when it's real).
- No Slack/PagerDuty delivery yet (alert tables + native ALERT SQL first).
- No multi-account / ORGANIZATION_USAGE.
- No auto-executing remediation. Generated SQL + audit rows only, executor is a
  human with the operator role (admin-gated in-app execution limited to
  settings updates and alert ack/resolve).

## Parity map (old section → new page)

| Old | New |
|---|---|
| Executive Landing | Overview |
| DBA Control Room | Control Room |
| Alert Center | Alerts |
| Cost & Contract | Cost & Contract |
| Workload Operations | Operations |
| Governance & Security | Security |
| (setup readiness, self-cost, settings — was scattered) | Admin |
