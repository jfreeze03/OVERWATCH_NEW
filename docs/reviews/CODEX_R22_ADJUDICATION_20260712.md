# Codex r22 adjudication (2026-07-12, v4.37.0)

Twenty items; every claim verified in code before a verdict. Eight ship
(V042 + app), ten route with their destination queue, two decline with
evidence. Ships land in V042__codex_r22.sql (derivations from V041's procs,
purge from V017's; locks in tests/test_v042_codex_r22.py).

## Shipped

- **#7 (the important one) — extract atomicity + gated watermark.**
  CONFIRMED as a real defect in v4.36.1's isolation: the extract arm could
  DELETE its overlap, fail the INSERT, swallow the error, and STILL advance
  the watermark — a hole in OW_QH_EXTRACT that every consumer MERGEd in
  until the nightly repair. Now: each arm is one transaction (ROLLBACK on
  failure — consumers genuinely read the previous fill), and the
  watermark/freshness tail runs only when the extract arm committed.
- **#1 — FACT_QUERY_DAILY.** Confirmed: the hourly query fact only accrues
  from install day and is deliberately not backfilled, so after a rebuild
  the exec board's 14/60/90-day windows and the platform score undercount
  for months. The new day-grain fact (same dims, 1/24th the rows) loads
  from the extract each cycle, backfills a full year, and the board +
  score procs read it. App-side readers of the hourly fact (activity
  sparklines, ops window summary) are ROUTED — short windows, small gap.
- **#2 — ops-diag backfill.** Confirmed: the 3-day clamp + no backfill call
  meant the default 7-day Operations first paint stayed on the 30-37s live
  path for ~4 days after install. Explicit calls may now run wide (the
  backfill fills the extract first); the recurring task still passes 2.
- **#10 — purge coverage.** Confirmed: SP_PURGE_FACTS predates V027 — not
  just the V041 tables but the ENTIRE mart family was outside retention.
  Sixteen tables join, hour-grain vs day-grain windows, same settings keys.
- **#15 (loader half) — AI fact gains EMAIL + FIRST_TS/LAST_TS** (exact
  per-day usage stamps, email from the USERS join the loader already
  pays). The users tab STAYS live-first — owner decision 2026-07-12 —
  until the fact demonstrably serves the full contract; the re-swap is
  queued with a side-by-side check, not shipped.
- **#14 — lazy AI users section.** Confirmed: opening "Chargeback & AI"
  paid the exact Cortex user scan ambiently. Now toggled, with the
  standing what-will-this-cost hint.
- **#17 — time-bounded drill.** Confirmed: query_detail() scanned the full
  365-day retention per click. Table-path drills now pass the row's
  START_TIME and scan a ±1-day window; the pasted-ID path keeps the broad
  scan deliberately (that reach is its purpose).
- **#20 (label half) — fleet board honesty.** Confirmed: p50/p95 over the
  exception-weighted telemetry sample read high. The panel now says so
  plainly. The full fix (persist reason + sampling weight, weighted
  percentiles) is a telemetry schema change — routed.

## Routed

- **#3 WMH extract** -> loader v2. Pattern is sound, pain unproven: the
  three WAREHOUSE_METERING_HISTORY scans are over a tiny table; no fleet
  evidence names them (the QH extract had 7-8 scans of a huge one).
- **#4 hour-scoped fact rebuild, #5 watermark-scoped mart refresh, #6
  incremental diag, #8 no-op MERGE suppression** -> loader v2, together:
  all four re-derive the same procs and shift the same
  delete-window/idempotence semantics; they should land as ONE reviewed
  re-derivation with the nightly repair as the safety net, not as four
  seams. Compute they save is now against the LOCAL extract, so the
  urgency died with V041.
- **#11 IS_TAGGED + #16 CS-credits-by-type in the extract** -> extract v2
  (both widen/narrow the projection and re-derive consumer arms).
- **#12 rank-then-enrich query families** -> loader v2 (same proc family).
- **#13 posture single-pass CREDENTIALS/GRANTS** -> standing fix-batch
  (small daily dimension scans; verbatim-derivation churn outweighs it
  alone — ride the next posture-arm edit).
- **#15 (app half) AI users mart re-swap** -> queued behind fact proof.
- **#19 percentile states** -> loader v2, flagged as its headline rider:
  APPROX_PERCENTILE_ACCUMULATE/COMBINE state on the hourly fact would
  retire every "peak hourly p95" caveat in one move. Right idea, too big
  to ride a hotfix.
- **#20 (stats half) weighted fleet percentiles** -> fix-batch (telemetry
  schema + writer + board SQL).

## Declined

- **#9 freshness counts are "growing cost" — no.** COUNT(*) and MAX(col)
  are metadata-served in Snowflake (micro-partition stats); the loader-
  owned freshness writes cost the same at a million rows as at a thousand.
  V040 existed because 19 aggregates ran per health-strip REFRESH per
  viewer; once per loader commit is the design working as intended.
- **#18 one-pass xdim reader — no.** The "second scan" is the global-share
  denominator over the scoped CTE (computed once; the coverage probe is a
  metadata MIN). Folding it into a post-filter window function invites the
  exact renormalization bug the v4.33.1 share law exists to prevent: the
  denominator must NEVER move with display filters. Correctness law beats
  a marginal plan improvement.

## Open verification item

- **V1 — EXECUTION_STATUS conventions.** V002-era loaders count failures
  as `= 'FAIL'`; the V027 mart arms count `= 'FAILED'`. Both cannot be
  right against ACCOUNT_USAGE.QUERY_HISTORY. One Snowsight probe settles
  it: `SELECT EXECUTION_STATUS, COUNT(*) FROM
  SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY WHERE START_TIME >= DATEADD('day',
  -7, CURRENT_TIMESTAMP()) GROUP BY 1;` — if the value is 'FAIL', the mart
  FAILS columns have been zero forever and need one enumerated-edit fix;
  if 'FAILED', the V002 facts undercount. Run it once; the loser gets a
  one-line fix in the next round.
