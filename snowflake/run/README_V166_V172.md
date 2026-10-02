# Runbox package: apply V162 -> V172 (app 4.609.0)

This package stages the eleven pending migrations. V162-V165 are the wave-4 set staged at runbox `b57d206e`, which has not been applied yet. V166-V172 are the round-2 bug-hunt fixes. The package also includes the read-only checks and the owner-run repairs that go with them.

All four SQL files go in `snowflake/run/` on the `runbox` branch:
- `RUN_NEXT.sql` replaces the current file.
- The other three are new.

`PREFLIGHT_WAVE4.sql` is already on the branch and stays there.

## The files

| File | What it is | Writes? | When |
|---|---|---|---|
| `RUN_NEXT.sql` | **Stage 1** is the existing wave-4 file, byte-identical: its header with the escalation-email choice, V162, V163, V164, V165, and its own PART B. **Stage 2** applies V166 -> V172. Each migration's text is byte-identical to `snowflake/migrations/` in 4.609.0. Stage 2 ends with a registry check that expects 11 rows. | Yes (DDL plus three bounded in-migration repairs) | The apply sitting |
| `PREFLIGHT_V166_V172.sql` | Each cluster's PREFLIGHT, in version order. The Central pin comes first. P166.x through P172.x preview exactly what the apply changes. | No (SELECT only) | Before stage 2 |
| `PART_B_V166_V172.sql` | Each cluster's PART B verify grids, in version order, with the Central pin first. The package adds ALL.1 (registry, 7 rows) and ALL.2 (next-day change-risk footprint). | No (SELECT only) | Right after the apply, then at the time each grid names |
| `OWNER_REPAIRS_V166_V172.sql` | The ordered owner-run heals and worklists, with the Central pin and a guard (refuses until V172) first. **PART 1** runs right after the apply. **PART 2** runs off-peak. | Yes, only inside guarded and bounded blocks. Optional writes are commented out. | After the apply |
| `README_V166_V172.md` | This file. | No | — |

## Order of operations

1. **Deploy app 4.609.0 first:** `snow streamlit deploy --replace`. RUN_NEXT's stage-1 header still says 4.602.0; the top note overrides it.
   - Every 4.609.0 read, caption and CALL that needs a new object is gated on its own migration (in this wave V166, V167, V169, V170, V171 and V172; V168 needs no gate), so the app is safe before the apply.
   - For example, the app CALLs the 5-arg `SP_INCIDENT_DECLARE` only once V170 is registered. Until it sees V170 it re-reads SCHEMA_VERSION every 30 s.
2. **Run the read-only previews and paste every grid back.**
   - Run `PREFLIGHT_WAVE4.sql` (V162-V165) and `PREFLIGHT_V166_V172.sql`.
   - **P166.4** is optional and heavy. It may time out on WH_ALFA_ADMIN (300 s per V002; the 2026-09-29 live probe saw 1800 s). Check first with `SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN WAREHOUSE WH_ALFA_ADMIN`, or skip it.
   - **P165.3** costs about 5 small Cortex calls.
   - Grids that preview V163-era rules (P169.7, the USER kinds in P170.1) show nothing until stage 1 is applied. That is expected.
3. **Choose the escalation email (owner question 1)** before the V164 section, as RUN_NEXT's stage-1 header describes:
   - **Option A:** set DEFAULT_RECIPIENTS on OVERWATCH_EMAIL in Snowsight.
   - **Option B:** go Teams-only by un-commenting the first seed.
   - Optionally, escalation off: un-comment the second seed.
   - Never type an address into a file.
4. **Run `RUN_NEXT.sql` top to bottom** as SNOW_ACCOUNTADMINS.
   - **Timing:** run it outside 06:30-07:30 Central (V166 needs 06:30-07:15 clear; V167 needs 06:40-07:30). Leave time for step 5 before the next 06:45 Central run.
   - **Stop at the first error.** Every migration's guard refuses until the previous one is registered, so V166+ will not run until V165 is applied. Every migration is idempotent: fix the problem and re-run from that banner.
   - **Run Stage 1's PART B "now" grids where they sit**, between V165 and V166.
   - **If the ~730 KB file is too large for one worksheet**, run stage 1, then paste from the `STAGE 2` banner down into a new worksheet. Stage 2 re-pins its own session.
   - **Expect** the stage-2 quick check to show 11 rows (162-172).
5. **Immediately: `OWNER_REPAIRS_V166_V172.sql` PART 1.** Run the pin, the role and the guard first.
   - **1.1, V167 step 1 (required):** re-keys Cortex Code days to Central, prunes the stale offset-keyed rows, and stamps the AI COVERAGE_FROM. Until it runs, the ~3 days around the apply double-count some evening Cortex Code usage. Run it before the next 06:45 Central DAILY marts run.
     - Unlike the other CALL blocks, it writes no APP_ERROR_LOG row of its own and does not catch a raised error. A `FAILED (nothing pruned)` pane changed nothing: fix the cause and re-run it.
     - Its loader runs `CREATE OR REPLACE TEMPORARY TABLE _OW_WH_MONITOR`. The Security page lists that as one MEDIUM DESTRUCTIVE change under your user. It is a session scratch table, not a real change (see step 8).
   - **1.2, R172.0 (recommended, commented):** one change-impact scan now, instead of at 06:50. It can raise PERF_CHANGE_REGRESSION events.
6. **`PART_B_V166_V172.sql` "now" grids** (the timing table is in the file header):
   - V166.1-2, V167.1-3, V168.1-2, V169.1-2, V170.1-5, V171.1-4, V172.1-2, ALL.1.
   - V167.4 runs after PART 1.
7. **`OWNER_REPAIRS_V166_V172.sql` PART 2**, off-peak, one block at a time. Read each pane before the next block.
   - **V166:**
     - R166.1 (optional probe).
     - R166.2: the 180-day security-fact heal. It suspends TASK_LOAD_HOURLY. **Always** run the RESUME pair after it, whatever the pane says.
     - R166.3: the gap grids.
     - R166.4: the storage-truth heal.
     - R166.5: the app-cost heal (at least 30 days; heavy).
     - R166.6: optional pointers to backfill_365.
   - **V167:**
     - Step 0: read-only probes. They set step 3's N.
     - Step 2: `SP_LOAD_PATTERN_COST(364)`.
     - Step 3: extract + HOURLY 90 inside a suspend window. Always run the RESUME lines.
     - Step 4: the atomic 364-day task-graph rebuild. Run it after step 3, outside the :07 slots 00/04/08/12/16/20 Central.
   - **V168:** R168.1 is optional and commented out; it is an owner decision.
   - **V169:** UI, except P169.4's next-day DQ_RECON_ERROR twins. For those, R169.1 is optional and commented out (an owner decision): it closes the older twin as SUPERSEDED, which the Alerts UI cannot set.
   - **V170:** worklists from PART B V170.4 / V170.5. The optional SQL is commented out. R2-028 defaults to no.
   - **V171:** optional ref-gap scan, commented out.
   - **V172:** R172.1-R172.4 worklists, after the next 06:50 and 07:00 Central runs.
8. **Run the later grids at the times each one names.** The bullets below are a summary. Each grid's own timing note decides (in `PART_B_V166_V172.sql`, and in RUN_NEXT's stage-1 PART B).
   - After the next :07 Central hourly chain: V168.3-4, plus stage 1's V162.5 and V164.3.
   - After the first escalation: stage 1's V164.3c (was the escalation email delivered?).
   - On the next Teams card: stage 1's V164.4 (a manual, by-eye check of the line format; its optional end-to-end drill is described there).
   - The next morning: V166.3-4, V169.3-4, V171.5-6, V172.3-4, plus stage 1's V163.3 and V165.3 (with 3b and 3c).
   - After the first manual declare from 4.609.0 made 30 s or more after the apply: V170.6. Before you declare, Control Room's SQL preview must end with the viewer as a 5th argument; if it shows 4, press Refresh data.
   - After 24 hours: stage 1's V162.6 (neither new rule auto-declared an incident; expect 0 rows).
   - The next day: ALL.2, plus stage 1's change-risk grid. Expect no DESTRUCTIVE, HIGH or CRITICAL row, with one exception:
     - When OWNER_REPAIRS PART 1 started within 30 minutes of V172's stamp, ALL.2 shows one MEDIUM DESTRUCTIVE `CREATE_TABLE_AS_SELECT` (or `CREATE_TABLE`) row. Stage 1's grid shows it too when PART 1 started within 30 minutes of V165's stamp. Its example reads `CREATE OR REPLACE TEMPORARY TABLE _OW_WH_MONITOR` (or `_OW_ALLOC_BASE`, if PART 2's V167 step 3 also ran that soon).
     - That row is the loader's session scratch table, which the security loader scores like any `CREATE OR REPLACE ... TABLE`. It is not a real change. ALL.2 labels it in its NOTE column; stage 1's grid cannot, because stage 1 stays byte-identical.

## Notes

- **Central everywhere.** The owner worksheet runs in UTC.
  - Every combined file pins `ALTER SESSION SET TIMEZONE = 'America/Chicago'` first.
  - RUN_NEXT re-pins at the start of stage 2, because stage 1 ends with UNSET.
  - SCHEMA_VERSION.APPLIED_AT, PART B V172.4, the V168/V169/V170 windows and the loaders' day keys all depend on the pin.
  - In a fresh worksheet, run a file's pin and role lines before any later grid.
- **Two stage-1 grids are superseded by design.** If you re-run them after stage 2, these read FALSE / FAIL because V168 and V169 replace the RETURN text:
  - V162.2 `RETURN_V162`.
  - V163.2 `alert scan daily v5 (V163:`.
  - Every other stage-1 needle still holds after V172 (checked against the merged migrations).
- **Warehouse / timeout (owner question 2).** The heavy repairs run on the worksheet's warehouse.
  - The heavy repairs are R166.2, R166.5 and V167 steps 1-4.
  - WH_ALFA_ADMIN is 300 s per V002; the live probe saw 1800 s.
  - A session-level STATEMENT_TIMEOUT_IN_SECONDS cannot raise a lower warehouse value.
  - On a timeout, every block except R166.2 either rolls back or is safe to re-run (a MERGE, or a DELETE + INSERT inside one transaction). R166.2 must be re-run until it reads ok.
  - PREFLIGHT P166.4's own section and the R166.2 note still say 300 s without the hedge; that wording comes from the generator.
- **Corrections the dashboards will show:**
  - Per-database storage KPIs and the showback STORAGE_DB line step **up** for re-created or clone-refreshed databases (V166).
  - ALL-scope pattern dollars step **down** on the twin days, roughly Jun 11 - Jul 13 2026 (V167).
  - Optimize idle reads less for warehouses whose jobs cross Central midnight. Idle days older than step 3's N keep the old overstated idle.
  - Alerts on databases with no COMPANY_SCOPE row move to company UNKNOWN (V172). They stop posting to an ALFA-only Teams route. To keep them posting, map the database (Cost Intelligence > Spend & Attribution > Unmapped entities) or add an ALL or UNKNOWN route.
  - The digest reports warehouse compute for the 7 complete days to yesterday (V171), a little below the old KPI.
  - No false COST_BUDGET_PACE alert on days 2-5 (V169).
  - COST_EGRESS_SPIKE counts true egress only (V169).
  - The first hourly scan after V168 auto-clears stranded OPEN PERF events (P168.1).
  - TASK_LOAD_APP_COST scans 33 days of SESSIONS instead of 10 (V166).
  - "Declared by" names the declaring DBA for a declare made 30 s or more after the V170 apply. Earlier incidents keep the app owner.
- **Rollback** (RUNBOOK section 12).
  - Each V166-V172 header names the base CREATE(s) to re-run. Roll back in reverse order.
  - V172's rollback has a second step. Right after V140's CREATE, and before the next change-impact scan, null the tracking TASK and PROCEDURE baselines (the UPDATE is in V172's header).
  - V167's twin DELETE is recoverable by Time Travel or `SP_LOAD_PATTERN_COST(N)`.
  - V162-V165: "Rolling back wave 4". Once V166-V172 are applied, V168, V169 and V171 re-derive procs whose bases are V162, V163 and V165. To undo both waves, go from V172 down to V162.
- **Integrity.**
  - Every file is UTF-8 with LF line endings. No file contains an email address.
  - Stage 2's migrations are byte-identical to `snowflake/migrations/` in the merged 4.609.0 tree, and each one regenerates byte for byte from its `outputs/gen_v1xx.py`.
  - Every PREFLIGHT and PART B section is that generator's output verbatim (run with its `*_OUT` variables).
  - Every OWNER_REPAIRS block for V166, V167, V168, V169 and V172 is copied verbatim from the generator's REPAIR output. The V170 and V171 blocks are written from the handoff text.

## Owner questions

**Blocking:**
1. **Escalation email for V164.** OVERWATCH_EMAIL has no DEFAULT_RECIPIENTS. Which address should V164 use? Or should it run Teams-only, or apply with escalation off? This blocks the whole chain, because V166+ guard on V165.
2. **Warehouse and STATEMENT_TIMEOUT for the heavy owner-run reloads.** These are R166.2, R166.5, V167 steps 1-4, and the optional backfill_365 CS-mart arm and `SP_LOAD_OBJECT_COST(365)`.

**Pending decisions, not built in 4.609.0:**
3. **R2-029.** When a declare hits a family that already has an open incident, should it attach to that blocking incident and return `ATTACHED`? This would need a later migration that re-derives both SP_INCIDENT_DECLARE overloads.
4. **R2-045.** Threshold suggestions for COST_BUDGET_PACE, COST_FORECAST_BREACH and DQ_RECON_ERROR. This needs METRIC_VALUE in threshold units plus an app cutover.
5. **R2-033, server half.** Should Pipeline SLA "met" mean "touched" or "rows actually loaded"? Today it means "touched", and the caption now says so. For "rows actually loaded", the recommendation is a per-table FRESHNESS_BASIS opt-in in a later migration.
6. **R2-011 Delta C.** After 3+ missed daily runs, should app-cost and storage-truth automatically reach further back? The proposal is a LOAD_TS-based floor, capped at 14 days.

**Defaults kept unless you say otherwise:**
7. **R2-093.** The STALE / ERR OPS_PIPELINE_DEGRADED sub-kinds stay separate proposals; they are not merged into one family incident.
8. **R2-034 add-on.** A multi-day PERF condition keeps one reminder event per day; it is not collapsed into one OPEN event.
9. **R2-042.** There is no "term ended, roll the contract settings" nudge after a contract term ends.
10. **R2-047.** COST_EGRESS_SPIKE counts true egress only (cross-region or cross-cloud).
11. **R2-024 / R2-023.** Unmapped databases stay UNKNOWN; they do not show in every scope. RESOLVED history is not re-stamped.
12. **R2-028.** No heuristic repair of historical "Declared by". OWNER_REPAIRS R170.3 has the preview if you want it.
13. **Historic false or duplicate events.** These stay as history unless you bulk-resolve them from the PREFLIGHT / PART B lists. That covers R2-035 carry-overs (R168.1), R2-020/R2-043 recon twins (R169.1), R2-041 pace days 2-5, R2-044 storage pairings, and R2-021/R2-025 change regressions.
14. **R2-039.** "New network" stays keyed on the first login of any outcome. The alternative is to key it on the first SUCCESSFUL login and lower failures-only events from HIGH.
15. **C10 session lookback width.** It is 30 days. 365 would cover all of SESSIONS' retention at a larger daily scan; P166.4 answers this.
16. **Optional pads, not done.** R2-015 could pad by 25 hours instead of one day (the spring-forward hour). R2-014 could pad by 2 days for graph runs longer than 24 hours.
