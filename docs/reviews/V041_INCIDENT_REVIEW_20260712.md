# V041 incident review + fix plan (2026-07-12, v4.36.1)

Owner reports after the v4.36.0 apply: cortex user table lost emails,
timestamps, and the column workflow built on them; validate.sql errored on
INFORMATION_SCHEMA.TASK_DEPENDENTS; task-graph failures stopped appearing;
Alerts went quiet. Baseline of trust: v4.34.2. This document is the root
cause for each, what shipped to fix it, the full-app review that followed,
and what stays queued.

## 1. Root causes

**RC1 — Cortex user attribution table (emails, timestamps, filters).**
The V041 R3 swap served the users tab from FACT_AI_USAGE_DAILY, which has
no EMAIL column and only day-grain dates. I shipped `NULL AS EMAIL` and
day-grain FIRST/LAST_USAGE as a "labeled degrade" — wrong call: the exact
who/when is the point of that table, and every column workflow on it broke
together. The design's R3 wording ("contract served from the fact") did
not license dropping contract columns.

**RC2 — Alerts quiet + task-graph failures missing.**
alerts.py is byte-identical to v4.34.2; the breakage is data-side. V041
placed every task RESUME + SYSTEM$TASK_DEPENDENTS_ENABLE AFTER seven
first-fill CALLs. A worksheet "Run All" halts at the first failing
statement — anything failing in those fills leaves BOTH roots' trees
suspended: no alert scans, no mart loads, no task-graph rows, frozen
board. This is precisely the "07-12 alert-outage class" the design doc
ordered made impossible; my ordering reintroduced it. Compounding it, the
new extract proc had no exception isolation (one flaky scan = failed task
= downstream SKIPPED every hour), and its task called the proc with a bare
NULL argument.

**RC3 — validate.sql error.**
The final task-monitoring block called INFORMATION_SCHEMA.TASK_DEPENDENTS,
which resolves only with a database context a bare worksheet run doesn't
have. Owner decision: task monitoring is not used — removed from the
script entirely (task-state diagnosis moved to loader_chain_check.sql).

**RC4 — Two quieter contract degrades of the same class as RC1** (found
in the review, not reported): the ops-diag mart kept hourly top-20s (the
unfiltered "top 50" panel became a near-sample, not the exact answer) and
FAIL_FAMILY's USERS_AFFECTED read as the peak-hour count (undercount).

## 2. Shipped in v4.36.1

- **Cortex users tab reverted byte-for-byte to v4.34.2** — live reads,
  probe semantics, exact EMAIL + FIRST/LAST timestamps. The degraded fact
  reader and its canary are deleted. A correct R3 (fact gains EMAIL +
  FIRST_TS/LAST_TS columns so day-sums keep exact stamps) is QUEUED as a
  design rider — not shipped until it can serve the full contract.
- **Task graph can no longer strand suspended:** the full resume +
  DEPENDENTS_ENABLE block now runs BEFORE the first fills AND again at the
  file end (V027 precedent resumes-then-fills; the repeat is belt and
  braces). Locked in tests: two enables per root, resume-before-fill order.
- **Extract loader isolated (V017 pattern):** the extract fill and the
  FACT_QUERY_HOURLY arm each carry their own EXCEPTION handler writing
  APP_ERROR_LOG — a flaky scan degrades to "consumers read the previous
  fill," never a failed task that SKIPs the hourly chain.
- **No bare NULL calls:** watermark mode is `DAYS_BACK <= 0`; the tasks
  and the nightly reconcile pass 0.
- **Posture SHOW guarded:** SHOW WAREHOUSES failure logs and skips ONLY
  the two monitor metrics (HAVING gates stop lying zeros); core posture
  metrics can never be taken down by it again.
- **Ops-diag corrected to exact:** top-50/hour (a member of the global
  top-50 is inside its own hour's top-50 by construction — the unfiltered
  panel is exact); USERS_AFFECTED becomes a mergeable HLL window
  approx-distinct (V037 precedent), labeled as such.
- **validate.sql:** task-monitoring block removed (owner decision);
  V001..V041 floor.
- **snowflake/loader_chain_check.sql** (new): one-run diagnosis for the
  frozen-chain class, with the likely fix (DEPENDENTS_ENABLE both roots)
  as step 0. **Run this on the live account NOW — before the rebuild —
  to bring alerts and marts back immediately.**
- **docs/FULL_REBUILD.md** (new): the safe full drop-and-reinstall
  runbook. The schema is SHARED with the previous app — the rebuild drops
  every OVERWATCH object by name and reruns V001..V041; it never drops
  DBA_MAINT_DB.

## 3. Review findings (full pass, v4.34.2 -> v4.36.1)

Scope: every file changed since the owner's trust baseline (642c5f7),
every V041 reader swap re-audited for contract drift, the migration
re-audited for fresh-install order, gates run end to end.

- **Since-baseline infra diffs are sound.** The three shared-code changes
  between v4.34.2 and v4.35.1 (batch-quarantine identity page+key+sql-hash,
  no-op query-param write suppression, settings-cache refresh salt) were
  read line-by-line: correct, and none can produce the reported symptoms.
  No revert of v4.34.3–v4.35.1 is needed or recommended.
- **Remaining V041 swaps hold their contracts exactly:** platform-score
  inputs (columns identical to the live aggregation), spend xdim
  (global-share law preserved; QUERY_COUNT absent but unused by the panel
  — same as the existing single-dim mart), operations first paint (now
  exact per above; any entity/schema filter still takes the live path).
  Every mart-first read keeps its labeled live fallback and coverage gate
  (young marts fall back rather than under-report).
- **Fresh-rebuild order verified:** V001..V041 apply clean in sequence
  (guards, IF NOT EXISTS collisions, first-fill dependencies — the
  extract fills before its consumers); teardown covers every V041 object;
  the sqlglot parse gate covers all 41 files + operational scripts.
- **Known, labeled grain caveats that predate V041** (unchanged, listed
  for completeness): eff-mart p95 is peak-daily; schema-window p95 is
  peak-hourly; family-mart averages are run-weighted. All carry source
  labels; none regressed.
- **Cost note:** the extract adds one hourly task run and ~3 days of
  QUERY_HISTORY projection in a transient table — well inside the
  OVERWATCH_RM 30-credit envelope; the pass exists to net scans DOWN.
- **Gates at ship:** ruff clean, mypy clean (39 files), full pytest
  green including page smokes, floor sim (sqlglot absent) green.

## 4. Queued (deliberately not shipped now)

- R3 done right: EMAIL + FIRST_TS/LAST_TS onto FACT_AI_USAGE_DAILY, then
  re-swap the users tab mart-first with zero contract loss.
- PERF_BACKLOG Tiers B–D unchanged.

## 5. Recovery order for the live account

1. snowflake/loader_chain_check.sql (step 0 alone likely restores alerts
   + marts within the hour).
2. Redeploy app v4.36.1 (restores the cortex table immediately).
3. Full rebuild per docs/FULL_REBUILD.md when you want the clean slate.
