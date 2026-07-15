# OVERWATCH_NEW — standing instructions (Claude Code auto-loads this file)

Streamlit-in-Snowflake cost/ops/security monitor for a shared ALFA+Trexis
account. Owner: Joe (jfreeze03). Everything lives in `DBA_MAINT_DB.OVERWATCH`
(schema SHARED with a previous app — never drop the schema/database).

Session-specific state (HEAD, deploy status, pending work) lives in
`docs/handoff/CODE_HANDOFF_2026-07-14.md` — read it, but trust `git log` over
its snapshot numbers. This file is the durable stuff: laws, history, owner
decisions.

## Gates (run before every commit)

```
python -m ruff check .
python -m mypy
python -m pytest -q          # needs Python 3.11+ (tests use datetime.UTC)
```
All three green or it doesn't ship. Migrations are run by JOE in Snowsight,
deliberately, off-peak — never by the agent. Agent Snowflake access, if
configured (snowsql `overwatch_ro`), is READ-ONLY: DESCRIBE/SHOW/SELECT to
validate assumptions before Joe deploys; never CREATE/ALTER/DROP/CALL/MERGE.

## House laws (each exists because something broke)

1. **Derivation law.** A migration that re-derives a proc/UDF re-emits the
   CURRENT definition byte-identically plus enumerated edits, via a
   forward-generation script in `outputs/gen_v0XX.py`; the matching
   `tests/test_v0XX_*.py` regenerates and byte-compares. Derive from the
   LATEST definition (V047 broke by deriving from V036 instead of V037).
2. **V030 shape law.** `COMPANY_FOR_*` UDFs apply to plain columns only,
   never inside aggregation. Since V044, classification is evidence-based
   BOTH ways: Trexis by mapping/prefix/role, ALFA by WH_ALFA_* names /
   ALFA%+ADMIN dbs / %ALFA%-or-DBA roles, residual = 'UNKNOWN' (never NULL —
   V048 lesson: additivity). COMPANY_SCOPE rows are the explicit override.
3. **Live-scan budgets** (`tests/test_perf_budgets.py`) count "ACCOUNT_USAGE"
   literals per hot page file. Lowering is welcome; raising needs a
   justification comment in the dict. Keep SQL in `app/data/*_sql.py`;
   `unit_costs.py` budget is 0.
4. **Every SQL builder gets a canary** (`app/data/canary.py`, default args,
   sqlglot-parses) and **every created object a teardown mention**
   (`tests/test_teardown_coverage.py`). Teardown keeps ALL destructive lines
   commented; operator data survives; never DROP SCHEMA/DATABASE.
5. **Migration guard + floor lockstep:** each V0XX opens with the not_ready
   guard and ends with the idempotent SCHEMA_VERSION insert; bump
   `snowflake/validate.sql` ("V001..V0XX applied") and admin's
   `_EXPECTED_MIGRATIONS` together (test enforces).
6. **Rebuild bundle is GENERATED** (`snowflake/rebuild/`): 02 = byte-concat of
   all migrations with `-- >>> name` banner sandwiches; 01/03/04/05 =
   banner + byte-copy of teardown/roles/backfill/validate. Regenerate after
   ANY edit to a source; never hand-edit.
7. **Task-graph ordering (V041 incident):** in migrations, task RESUMEs +
   SYSTEM$TASK_DEPENDENTS_ENABLE go BEFORE first-fill CALLs AND again at the
   end — a halted worksheet must never strand the tree suspended. Procs swap
   under the running graph (CREATE OR REPLACE needs no suspends).
8. **Honesty rules:** no fabricated zeros (coverage gates / pct=None when the
   denominator is empty); empty panels say "checked, clean" not blank; source
   labels say which path served (mart vs live fallback); mart-first with
   live fallback via `run_mart_first` (empty mart → fallback, never lying
   zeros). No `") or {}"` after run_batch (r8 lock). Qualify columns
   (alias-shadow rule). Declared-exception guards only.
9. **Identity:** owner's-rights SiS — `CURRENT_USER()` = app owner. Viewer
   identity via `app/core/identity.py` (`st.user`, CURRENT_USER() fallback)
   for prefs/usage/audit. Executor allow-list: one statement, OVERWATCH
   objects or warehouse levers only. Cache invalidation is domain-scoped.
10. **Formulas:** `app/logic/formulas.py` is the only place credits become
    dollars; `app/logic/metric_registry.py` is the semantic contract
    (BILLED/METERED/MEASURED/ALLOCATED/ESTIMATED + grain + lag). SQL builders
    return SQL strings only, day windows clamp via `bounded_days`, filters
    flow through `app/companies.py` clause builders exclusively.

## Owner decisions (do not relitigate)

- **Access = SNOW_ACCOUNTADMINS + SNOW_SYSADMINS, period** (2026-07-13). The
  monitor/operator role layer is retired; roles.sql grants direct + drops it.
  Audit tables keep append-only REVOKEs (accident-proofing).
- **Task monitoring STAYS** (2026-07-13 correction: "i meant getting rid of
  resource monitor, not task monitoring"). V045 restored it end-to-end.
- **Resource monitors are GONE** (same correction). OVERWATCH_RM was
  suspending the app warehouse mid-use. No monitor levers/deductions in the
  app; auto-suspend tracking stays. No hard cap on WH_ALFA_OVERWATCH — COST
  alert rules are the guardrails.
- **UNKNOWN classification is law** (V044/V048): unmapped entities surface on
  Cost → Chargeback ("Unmapped entities" worklist) instead of silently
  billing ALFA; KEBARR1 override → ALFA stands.
- **Cortex user attribution stays live-first, byte-exact v4.34.2 shape**
  (exact emails + timestamps; owner rejected the mart swap that lost them).
- **No monthly-budget KPI** on Overview; MTD-vs-prior-month pace instead.
- Deterministic prescriptive alert rules, dedupe-key pattern (19 scan arms:
  [01]-[17] + [18] SEC_NEW_ADMIN_NETWORK + [19] COST_EGRESS_SPIKE).
- Validate/loader worksheets are pasted by Joe; the app monitors the loader
  through APP_ERROR_LOG + SOURCE_FRESHNESS_STATE (loader-owned freshness).

## History in one paragraph each

- **v4.36 (V041)** loader-efficiency pass (staged QH extract, watermarks,
  exec board v2, xdim alloc, posture riders) — shipped with two defects:
  resumes after first-fills (stranded the task tree when the worksheet
  halted) and a mart swap that dropped cortex emails/timestamps. Incident
  review: `docs/reviews/V041_INCIDENT_REVIEW_20260712.md`.
- **v4.37** full-rebuild bundle (`snowflake/rebuild/`, backup clones
  `*_BAK_20260712` — Joe may drop them when satisfied) + hardened chain;
  rebuild executed clean.
- **v4.38 (V042)** Codex r22 ship-half: FACT_QUERY_DAILY, atomic extract with
  gated watermark, purge coverage, AI usage stamps. r23: telemetry-picked
  perf (batching, predicate-first).
- **v4.39-4.40** triage filter chips + 20-rec review; r24: Snowsight profile
  links on every QUERY_ID table, pace KPI, systemic post-action refresh.
- **v4.41 (r25)** security metrics Joe picked: new-network logins (90d
  baseline) + Egress section (DATA_TRANSFER_HISTORY + UNLOAD watch).
- **v4.42 (r26)** roles collapsed to the two SNOW_* roles; task monitoring
  removed on a misread ask ("task monitor" meant resource monitor).
- **v4.43 (V043/r27)** Codex r27 adjudication ship-list: loader task
  retirement (later reversed), r25 alert teeth, viewer identity, executor
  allow-list, domain cache salts, set-based bulk ack, admin access
  self-check/grouped errors/settings hygiene, docs rewrite + drift locks.
  Adjudication: `docs/reviews/CODEX_R27_ADJUDICATION_20260713.md`.
- **v4.44 (V044)** UNKNOWN classification (#18).
- **v4.45 (V045)** the correction: task monitoring restored (app from git
  history + loader re-derivation, 120d refill, 19 scan arms), OVERWATCH_RM
  dropped.
- **2026-07-14 (Code session)** cost audit F1-F4, Codex cost items 1-8,
  metric registry, FACT_OBJECT_COST_DAILY (V046-V048), ETL cost tags,
  Phase 4 account-time/DST/reconciliation locks. See
  `docs/design/COSTDB_VS_OVERWATCH_2026-07-14.md` and
  `docs/reviews/CODEX_REVIEW_ASSESSMENT_2026-07-14.md`.

## Working style

Joe wants concise and direct. Verify claims in code before adjudicating
external review items (ship/route/decline with evidence — see the
adjudication docs for the format). Small honest scopes; every round ends
with all gates green, a CHANGELOG entry, an APP_VERSION bump, and locks that
pin what shipped. When an external reviewer (Codex/CoCo) is right, say so;
when the code disproves a claim, show the line. Owner corrections get
recorded in locks/comments so the story survives (grep "owner" in tests/).

## Standing open items

- r28+ queue: proc-based atomic action layer (intent + idempotency),
  reconciliation v2 by dimension, evidence-grade savings verification
  (execute proof + snapshots), Action Queue as operating center (r29/r30
  product round), V049 write-target attribution (check residual share via
  `object_cost_by_arm` first — only build if material).
- `_BAK_20260712` clones: droppable whenever Joe is satisfied.
- Python floor is now 3.11 (datetime.UTC in tests) — pin in docs/CI if a
  3.10 environment ever shows up.
