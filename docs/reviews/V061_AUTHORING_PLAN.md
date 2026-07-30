# V061 authoring plan — consolidate the deferred loader/mart fixes — 2026-07-29

**Status:** plan for review, not yet authored. V061 touches the live loaders
(`SP_LOAD_MARTS_V27`, `SP_ALERT_SCAN`, `SP_PURGE_FACTS`, `SP_NOTIFY_WEBHOOK`,
`backfill_365`, the nightly reconcile) that **Joe applies in Snowsight** — the
agent authors the SQL + lockstep and never runs it. Because a derivation slip
here corrupts facts on apply, this plan specifies the exact change, derivation
needle, and reconcile per fix before any SQL is generated.

Sources: `COST_ACCOUNTING_REVIEW_2026-07-29.md` (C1/C2/C5/C6),
`BUG_ROUND_2_2026-07-29.md` (B5/B9/B10/B11/B12/B24/B33/B34/B41),
`PERF_ROUND_2_SCOPE_2026-07-29.md` (T3.1–T3.4). Derivation law: each proc is
re-derived from its **current** definition via `outputs/gen_v061.py`
(`extract_proc` last-match + `apply`/`apply_once` asserting needle counts);
`tests/test_v061_*.py` byte-compares the generated proc against the migration.

---

## Recommended phasing

The 17 candidates split cleanly by risk and by whether they reshape the same
arms. **Author V061 as correctness-only; defer the perf restructures to V062.**

- **V061 (correctness):** C5, C6, C1-mart, C2-mart, B5, B9, B10, B11, B12, plus
  the safe LOWs B33, B41. These are additive column adds, predicate corrections,
  and window realignments — each independently verifiable, several needing a
  one-time reconcile heal pass.
- **V062 (perf loader):** T3.1 (efficiency arm → extract), T3.2 (TASK_HISTORY
  single-extract), T3.3 (WMH staging), T3.4 (reconcile double-load). These
  **restructure the same arms** C2/C5 touch; bundling them with correctness
  fixes makes the byte-compare derivation and the blast radius much harder to
  reason about. B34 (unguarded DELETE-then-INSERT, PLAUSIBLE) rides V062 with
  the arm restructures since it changes the same statement boundaries.

Rationale: keeping the correctness migration free of the perf restructures means
each `apply_once` needle is a small, obvious edit, and the reconcile passes
(below) are the only behavioral risk — not a wholesale arm rewrite.

---

## V061 fixes — per-proc derivation spec

### SP_LOAD_MARTS_V27 (re-derive from current = V060)

1. **C5 — AI arms day-align (boundary-day clobber, HIGH data-corruption).**
   The three AI arms are the only arms windowed on `CURRENT_TIMESTAMP()`
   (V060:655/:659/:694); every other daily arm uses
   `DATEADD('day', -:d, CURRENT_DATE())`. The MERGE `WHEN MATCHED UPDATE`
   overwrites a complete day-X row with the post-06:45 tail once X exits the
   window. Needle: the three `USAGE_TIME/START_TIME >= ... CURRENT_TIMESTAMP()`
   predicates → `>= DATEADD('day', -:d, CURRENT_DATE())`. `apply` count = 3.
   **Reconcile:** ship a one-time `CALL SP_LOAD_MARTS_V27('DAILY', <horizon>)`
   in the migration tail (horizon = damage window, ≥ retention floor) to heal
   rows already truncated. This is the same partial-window class V056 fixed for
   `FACT_QUERY_DAILY`.

2. **C2 — QAS in the V059 arm-[6] mart twin.** Arm [6] (proc/pipeline
   attribution, `MART_PROC_COST_*`) sums `CREDITS_ATTRIBUTED_COMPUTE` only; the
   app side already carries `+ COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)`
   (shipped v4.74.0). Needle: the arm-[6] attribution SUM. Mirror the two V010
   change-impact arms (V010:293, :328) — but those live in a **different proc**
   (see below). `apply_once` per site.

3. **T3 items (DEFER to V062):** arm [1]→extract routing, TASK_HISTORY
   single-extract, WMH staging. Not in V061.

### SP_ALERT_SCAN (re-derive from current = V060)

4. **C1-mart — MTD priced at the AI rate.** `COST_BUDGET_PACE` /
   `COST_FORECAST_BREACH` (V060:1017-1020/:1064-1067) dollarize
   `SUM(CREDITS_BILLED)` at the single `:credit_price`. Read
   `AI_CREDIT_PRICE_USD` from SETTINGS alongside `:credit_price` and build
   `MTD_USD` as the two-partition sum (`OTHER*rate + AI*ai_rate`) using the
   proven AI predicate. Mirrors app `formulas.blended_billed_usd`.

5. **C6 — seed `COST_AI_CREEP`.** Add a rule to `SP_ALERT_SCAN` + a row to
   `ALERT_CONFIG`: week-over-week growth of the canonical AI bucket from
   `FACT_METERING_DAILY`, priced at `AI_CREDIT_PRICE_USD`, optionally with an
   `AI_MONTHLY_BUDGET_USD` pace arm. Until it exists, AI-only spend spikes fire
   nothing (COST_DAILY_CREDITS is warehouse-only; the AI carve-out in
   COST_SERVERLESS_CREEP rests on this unwritten rule). Also **drop or correct**
   the "AI has its own rules" comment at V060:1257-1258.

6. **B41 — self-alert block count.** The scan-degradation self-alert says
   "of 17 alert rule block(s)" while the proc has 19 blocks (soon 20+ with
   COST_AI_CREEP). Make the count derive from the block list or update the
   literal. `apply_once`.

7. **B24 — undelivered-expired watchdog route scope.** The watchdog ignores
   family/severity route scope, logging permanent hourly noise for
   below-threshold events. Scope the watchdog query to routed families/severities.

### SP_NOTIFY_WEBHOOK (re-derive from current)

8. **B9 — truncated events marked delivered.** Events truncated out of the
   3000-char message are marked `NOTIFIED`. Only mark the events actually
   included in the payload delivered; leave the remainder for the next cycle
   (or raise the cap + paginate). `apply_once` on the mark-delivered UPDATE's
   scope.

### SP_PURGE_FACTS (re-derive from current = V055)

9. **B33 — three facts never purged.** `FACT_OBJECT_COST_DAILY`,
   `FACT_STORAGE_ACCOUNT_DAILY`, `MART_TASK_NODE_DAILY` are absent from the
   purge. Add them to the retention loop with the appropriate floor
   (daily-tier). Additive; low risk.

### backfill_365 + nightly reconcile (V056)

10. **B5 — `backfill_365('HOURLY', 90)` loads only 2 days (HIGH).** Four mart
    arms are `LEAST(:d, 2)`-capped, so a 90-day HOURLY backfill silently fills
    only 2 days for them. Either lift the cap for the backfill entry point or
    loop the backfill day-by-day so each capped arm sees a 2-day window walking
    the full range. **Interacts with B12** (below) — design together.

11. **B10 — reconcile boundary-hour undercount.** The nightly reconcile rebuilds
    hour-grain surfaces 3d deep from a 72h extract; the boundary hour is
    permanently undercounted. Widen the extract or the reconcile window by one
    hour so the boundary is fully covered.

12. **B11 — no catch-up path for FACT_WAREHOUSE_DAILY / FACT_QUERY_DAILY.** A
    multi-day task outage leaves permanent interior holes (the arms only load a
    trailing window). Add a gap-detection catch-up (re-load days with no row in
    the retention window) or a widened reconcile.

13. **B12 — backfill races the hourly extract.** A mid-backfill trim can wipe up
    to 87 days of ops-diag history. Serialize the backfill against the extract
    task (or have the backfill write to a staging table it swaps in) so a
    concurrent trim can't delete backfilled rows. **Design with B5.**

### V010 change-impact proc (re-derive from current)

14. **C2 — QAS in the two V010 arms** (`:293`, `:328`). Same
    `+ COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)` term as the app fix.
    `apply_once` ×2.

### Mart DDL (score + exec board)

15. **C1-mart — `FACT_PLATFORM_SCORE_DAILY` AI split.** Add `CREDITS_BILLED_AI`
    (additive column) to the score-inputs fact + its loader arm and the
    `score_inputs_daily` / `platform_score_inputs` readers, so
    `scoring.py:151`'s `budget_pct` prices AI at the AI rate (app-side follow-up
    after the column lands). Verify whether `MART_EXEC_BOARD.DAILY_SPEND`
    VALUE_USD mixes AI; if warehouse-derived (compute-only), no change needed —
    confirm before touching.

---

## Reconcile / heal passes required on apply (Joe runs)

| Fix | One-time heal after apply |
|-----|---------------------------|
| C5  | `CALL SP_LOAD_MARTS_V27('DAILY', <damage-horizon>)` to rewrite AI-arm rows truncated by the CURRENT_TIMESTAMP window |
| B10 | reconcile re-run for the affected boundary hours (or accept forward-only) |
| B11 | one gap-fill backfill over the retention window |
| C1-score | app-side `scoring.py` blend after the AI column backfills |

These belong in the migration tail (idempotent) and in DEPLOYMENT.md's V061
run-list, not as silent side effects.

---

## Lockstep checklist (every migration)

- [ ] `outputs/gen_v061.py` — re-derive each proc from its current definition;
      `apply`/`apply_once` with asserted needle counts (counts above).
- [ ] `snowflake/migrations/V061__ai_loader_alert_purge_fixes.sql` — generated.
- [ ] `snowflake/validate.sql` floor `V001..V060` → `V001..V061`.
- [ ] Admin `_EXPECTED_MIGRATIONS` bump; the generic floor test
      (`test_validate_sql_floor_tracks_the_latest_migration`) auto-tracks.
- [ ] `outputs/gen_rebuild_bundle.py` → regenerate `02_migrations_V001_V0NN.sql`.
- [ ] `tests/test_v061_*.py` — byte-compare each derived proc + assert each fix's
      predicate is present (AI day-align ×3, QAS terms, MTD two-partition,
      COST_AI_CREEP seeded, purge covers the 3 facts, webhook mark-scope,
      backfill uncapped).
- [ ] `tests/test_rebuild_bundle.py` — bundle includes V061.
- [ ] DEPLOYMENT.md + README run-lists + teardown mention.
- [ ] Adversarial verify pass (loader arm-by-arm; the reconcile passes are where
      the risk lives).

---

## Open questions for the owner before authoring

1. **Phasing** — V061 correctness-only + V062 perf (recommended), or one bundle?
2. **C5 damage horizon** — how far back to heal the AI-arm truncation? (Bounded
   by FACT retention; a full 365-day `SP_LOAD_MARTS_V27('DAILY', 365)` is the
   safe upper bound but a heavier one-time scan.)
3. **AI_MONTHLY_BUDGET_USD** — seed a pace arm in COST_AI_CREEP, or growth-only?
4. **B5/B12 backfill redesign** — day-by-day loop vs staging-swap; the
   staging-swap is safer against the concurrent-trim race but a bigger change.
