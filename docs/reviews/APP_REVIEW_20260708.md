# App review 2026-07-08 — consolidated from four automated runs

Four independent review branches (`cursor/app-review-*`, all branched from
v4.2.0) reached overlapping conclusions. This file consolidates every unique
finding, verified against the codebase, so the four branches could be deleted
without losing anything. Status reflects v4.4.x.

## Fixed

| Finding | Fixed in |
|---|---|
| CI red on every run: invalid YAML in ci.yml (unquoted colon in step name) | this commit |
| CI red on every run: bare `pytest` can't import `app` (worked only via `python -m`) | this commit (pytest.ini, `pythonpath=.`) |
| Alert → Cost deep links crash (pre-consolidation section names) | 4.3.0 |
| V021 skipped SCHEMA_VERSION guard/row + validate.sql coverage | this commit |
| Query cache keyed by role only — users sharing a role could serve each other's USER_PREFS frames for a TTL | this commit (user in cache scope) |
| Alert drawer lifecycle widgets shared global keys across events (state bled between rows) | this commit (per-event keys) |
| Alert DETAIL rendered as markdown (Snowflake-sourced text could inject formatting) | this commit (plain text) |

## Open — verified and worth scheduling

1. **Webhook multi-route delivery is first-route-wins** (docs promise per-family
   additive routing) — `webhook_delivery.sql` / notify task. Two runs found it
   independently. Also: one transient send failure suppresses retries up to 24h.
2. **`validate.sql` still validates pre-V019 user scoping** — passes on stale
   COMPANY_SCOPE seeds; should assert COMPANY_FOR_USER exists + spot-check it.
3. **Brief renders zeros when its queries fail** — violates the no-fake-numbers
   contract; should show the labeled error state like every other page.
4. **`CONTRACT_START_DATE` unset breaks the contract-breach projection**
   (pace math silently wrong rather than declining).
5. **`WINDOW_HOURS` on alert rules is display-only** — scan hardcodes windows;
   either honor the column or remove it from the editable surface.
6. **`THRESHOLDS` dict in config.py is mostly decorative** — several documented
   knobs are never read.
7. **Storage-movers COMPANY column mislabels TRXS_% databases as ALFA**
   (uses exact-list classify where pattern classify is needed).
8. **"MFA gap" has 2–3 different definitions across Security panels** — pick one
   definition (password-login evidence) and reuse the builder.
9. **Cortex per-user vs rollup 30d projections disagree** (different windows) —
   unify the projection basis.
10. **Fact loaders can permanently lose history** after an outage longer than the
    re-scan window — document the backfill runbook step, or auto-widen re-scan
    after gaps.
11. **"N days" means different windows in different builders** (CURRENT_DATE vs
    CURRENT_TIMESTAMP anchoring) — standardize on one convention.
12. **Lock-contention query zeroes the worst waits** (aggregation drops rows with
    NULL end-times).
13. **No dependency/vulnerability monitoring** — add Dependabot/`pip-audit` to CI.
14. **The same expensive scan runs twice on Optimization** (idle analysis feeds
    two panels with different cache keys) — share the key.

Items 1–4 are the sharp edges; 5–14 are consistency debt.
