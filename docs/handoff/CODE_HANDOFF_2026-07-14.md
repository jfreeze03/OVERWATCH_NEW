# OVERWATCH_NEW — handoff to Claude Code (2026-07-14)

Paste this whole file into a new Claude Code session as context. It captures
where the repo stands, the conventions you MUST follow, the one Snowflake setup
that closes our slow feedback loop, and the work still pending.

---

## 1. What this repo is

Streamlit-in-Snowflake app that monitors Snowflake cost/usage/security, read out
of `SNOWFLAKE.ACCOUNT_USAGE` / `ORGANIZATION_USAGE`. Repo:
`github.com/jfreeze03/OVERWATCH_NEW`, local `C:\Users\jfree\Documents\GitHub\OVERWATCH_NEW`.
Current HEAD: `d2b4085`. Test suite: **879 passed, 1 skipped** (`python -m pytest -q`).
Schema at **V048**.

Layout that matters:
- `app/data/*_sql.py` — SQL builders. **They return SQL strings only. No dollar
  math here.** Every builder gets a default-arg entry in `app/data/canary.py` and
  must `sqlglot`-parse (Snowflake dialect).
- `app/logic/formulas.py` — the ONLY place credits become dollars.
- `app/logic/metric_registry.py` — the semantic contract: every metric's method
  (BILLED / METERED / MEASURED / ALLOCATED / ESTIMATED), grain, source, latency.
- `app/ui/pages/` — Streamlit pages. Some are perf-budgeted (see §4).
- `snowflake/migrations/V0XX__*.sql` — ordered, idempotent, guarded migrations.
- `snowflake/rebuild/` — a GENERATED bundle (see §4); never hand-edit.
- `tests/test_v0XX_*.py` — a lock per migration; `tests/test_*` lock behavior.

---

## 2. What we did this session (newest first)

- `d2b4085` V048: residual `COMPANY` = `'UNKNOWN'` (not NULL) so residual credits
  stay additive to per-company sums.
- `f1fbcf9` V048: null-safe `OBJECT_FQN` — COALESCE every name part; Snowflake
  `||` returns NULL if any operand is NULL, which was aborting the load.
- `5c5da6b` V047 fix: re-derived from **V037** (DATABASE_NAME grain + `USERS_HLL`
  sketch), not the older V036 body. The V036 derivation referenced a dropped
  `USERS` column → "invalid identifier 'USERS'".
- `a4553f8` Phase 3: ETL cost tags + unit-cost KPIs (`app/data/etl_sql.py`,
  toggle-gated panel in `unit_costs.py`, `docs/design/ETL_COST_TAGS.md`).
- `cdcdf53` Phase 2: `FACT_OBJECT_COST_DAILY` additive object-cost ledger (V048).
- `1a154ac` Phase 1: metric registry.
- `435c2a3`..`cd7b6de` cost audit F1–F4 + Codex review items 1–8 (storage truth,
  equal windows, one allocation path, Query Acceleration in measured cost, honest
  usage-vs-billing language, org buckets on structured billing dims, calendar
  storage cards, honest Environment filter).

Design/review docs: `docs/design/COSTDB_VS_OVERWATCH_2026-07-14.md`,
`docs/reviews/CODEX_REVIEW_ASSESSMENT_2026-07-14.md`,
`docs/design/ETL_COST_TAGS.md`.

---

## 3. Deploy state (what's live in Snowflake vs the repo)

Applied in Snowsight and confirmed (SCHEMA_VERSION = 48):
- V046 (storage truth), V047 (corrected), V048 (`f1fbcf9` — null-safe FQN).

**Not yet applied:** the `d2b4085` residual→UNKNOWN change. The live table still
has ~15 residual rows (one/day since Jun 30) with `COMPANY = NULL`.
To land it: re-run the whole `V048__object_cost_ledger.sql` in Snowsight — it's
idempotent (the guard for v≥47 passes; `CREATE OR REPLACE` the proc; its
`CALL SP_LOAD_OBJECT_COST(14)` does `DELETE WHERE DAY >= today-14` then reloads,
converting all 15 rows to `'UNKNOWN'`; the `SCHEMA_VERSION` insert no-ops since 48
already exists). Or just run `CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_OBJECT_COST(30);`
against the already-corrected proc after pushing+pulling.

Migrations are DDL + heavy first-fill CALLs — **the user runs them in Snowsight,
deliberately, off-peak.** Do not run migrations from the agent.

---

## 4. Repo conventions you MUST keep green

1. **Full suite before every commit:** `python -m pytest -q` (deps:
   `pip install -r requirements-dev.txt`, Python 3.10+).
2. **Migration guard pattern:** each `V0XX` opens with an `EXECUTE IMMEDIATE`
   `DECLARE ... not_ready EXCEPTION (-200XX, ...)` that RAISEs if
   `MAX(VERSION) < XX-1`, and ends with an idempotent `INSERT ... SELECT XX ...
   WHERE NOT EXISTS`. Idempotent; safe to re-run.
3. **Byte-locked derivation:** when a migration re-derives a procedure from an
   earlier one, keep it byte-identical **except the one intended change** — and
   derive from the CURRENT definition, not an older one. (That was the V047 bug.)
   `diff` the proc body against the source migration; the diff should be only
   your intended line.
4. **Rebuild bundle is generated.** After editing ANY migration, regenerate
   `snowflake/rebuild/02_migrations_V001_V0XX.sql` (byte-concatenation of all
   migrations) or `tests/test_rebuild_bundle.py` fails. Also
   `01_teardown_rebuildables.sql`, `03_roles.sql`, `04_backfill_365.sql`,
   `05_validate.sql` are byte-identical copies of their `snowflake/*` sources —
   resync all of them together. Regenerator:
   ```python
   import pathlib
   mig=sorted(pathlib.Path("snowflake/migrations").glob("V0*.sql"))
   B=pathlib.Path("snowflake/rebuild/02_migrations_V001_V048.sql"); cur=B.read_text()
   ul=[l for l in cur.splitlines() if set(l)=={'-',' ','='} and l.startswith("-- =")][0]
   header=cur[:cur.index(ul+"\n-- >>> V001__core.sql\n"+ul+"\n")]
   B.write_text(header+"\n".join(f"{ul}\n-- >>> {m.name}\n{ul}\n{m.read_text()}" for m in mig))
   ```
   (Rename the bundle file when the max version changes, and update the count
   assertion in `test_rebuild_bundle.py`.)
5. **Validate gate:** `snowflake/validate.sql` asserts `V001..V0XX applied`;
   `app/ui/pages/admin.py` has `_EXPECTED_MIGRATIONS` through the latest version.
   Bump both when you add a migration.
6. **Perf budgets:** `tests/test_perf_budgets.py` caps live `ACCOUNT_USAGE` scans
   per hot page. `unit_costs.py` budget is **0** — keep every `ACCOUNT_USAGE`
   literal (even in `source=` labels) OUT of budgeted page files; put SQL in
   `app/data/*_sql.py` and gate expensive panels behind a `st.toggle`.
7. **Test locks:** update the matching `tests/test_v0XX_*.py` when you change a
   migration; add assertions that pin the fix so it can't regress.
8. Git identity is `jfreeze03 <jfreeze03@yahoo.com>`. In Code you can push
   directly — check `git log origin/main..HEAD` and push if anything is unpushed.

---

## 5. THE setup that makes Code worth it — read-only Snowflake validation

Every migration bug this session (V047 `USERS`, V048 NULL FQN) only surfaced when
the user deployed. Close that loop: give the agent **read-only** Snowflake access
so it validates column names / NULL behavior BEFORE the user deploys. Read-only —
the agent inspects; the user still runs migrations in Snowsight.

1. Install SnowSQL (or the Snowflake VS Code extension / `snowflake-connector-python`).
2. Use a read-only role that can `SELECT` on `SNOWFLAKE.ACCOUNT_USAGE` and
   `DBA_MAINT_DB.OVERWATCH`, and `DESCRIBE`/`SHOW`. (`ACCOUNTADMIN` or the app's
   existing monitoring role works; a dedicated `OVERWATCH_RO` is cleaner.)
3. Configure `%USERPROFILE%\.snowsql\config`:
   ```
   [connections.overwatch_ro]
   accountname = <your_account>
   username    = <your_user>
   authenticator = externalbrowser     # or password / key-pair
   rolename    = OVERWATCH_RO
   warehousename = WH_ALFA_OVERWATCH
   ```
4. Tell the agent it may run read-only checks like:
   ```
   snowsql -c overwatch_ro -q "DESCRIBE VIEW SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY;"
   snowsql -c overwatch_ro -q "SELECT * FROM DBA_MAINT_DB.OVERWATCH.FACT_OBJECT_COST_DAILY LIMIT 5;"
   ```
   Rule for the agent: **read-only only** — DESCRIBE/SHOW/SELECT to validate a
   migration's columns and assumptions; never CREATE/ALTER/DROP/CALL/MERGE.
   The user runs migrations in Snowsight.

---

## 6. Pending work

1. **Deploy** the corrected V048 (§3) to convert residual `COMPANY` NULL→UNKNOWN.
2. **Optional V049 — write-target attribution.** The V048 object split only uses
   `ACCESS_HISTORY.BASE_OBJECTS_ACCESSED` (reads). Write-only ETL — `COPY INTO`,
   `INSERT ... VALUES`, `CTAS` from constants — reads no base table, so its credits
   land in the `QUERY_COMPUTE_RESIDUAL` bucket instead of on the target table.
   For an ETL-heavy account that can be a lot of cost. Enhancement: also fold in
   `ACCESS_HISTORY.OBJECTS_MODIFIED` (write targets) into the equal-split so loads
   attribute to the tables they build; residual shrinks to genuinely
   unattributable compute. Additive; new migration V049 + reader/registry updates
   + lock. **Check the residual credit share first** (`object_cost_by_arm`) — only
   build if it's material.
3. **Phase 4** (architectural, was next before the deploy detour): filter-matrix
   tests + org-reconciliation tests + DST regression tests.

---

## 7. First moves in Code

```
cd C:\Users\jfree\Documents\GitHub\OVERWATCH_NEW
pip install -r requirements-dev.txt
python -m pytest -q            # expect 879 passed, 1 skipped
git log --oneline -6           # confirm HEAD = d2b4085
git log origin/main..HEAD      # push anything unpushed
```
Then set up §5 (read-only Snowflake) and pick up §6.
