# Codex adjudication — 2026-07-27 (post-v4.50.0)

Every claim verified against `b0c03f6` by eight parallel verifiers reading the
cited code adversarially. Verdicts: **SHIP** (verified, scheduled), **ROUTE**
(real; owner decision or a later round), **DECLINE** (with the line). Standing
owner decisions applied: SNOW_ACCOUNTADMINS + SNOW_SYSADMINS only; additivity
is law (V048); migrations run by Joe in Snowsight; the r28+ queue from r27
stands.

Headline: Codex's thesis — "execution and trust: finding → owner → action →
financially defensible outcome is incomplete" — is **correct, and it is also
the house's own r28+ queue restated**. Of the 20 recommendations, five are the
already-adjudicated queue (1, 2, 3, half of 4, half of 19), one re-litigates a
standing owner decision (19's persona/viewer model), and the genuinely new
material is: the savings-verifier **routing gap** (screens promise settlement
by a verifier that can never select those rows), the **audit-actor remnant**
(every audit table still records the app owner), the **unconditional live
scans hiding under unit_costs' zero budget**, and the **read/write role split
+ one-pass loader** for the object ledger.

## Findings

| # | Claim | Verdict | Evidence / reasoning |
|---|-------|---------|----------------------|
| P1-A | "Verified savings is not consistently verified" | **TRUE — SHIP captions now, fold mechanics into r28** | Three disjoint regimes. can_verify = nonempty proof string + typed number, never executed (actions.py:49-63) — and four executed paths store the remediation ALTER as PROOF_SQL (optimize.py:236-242, 566-571, 660-667; alerts.py:303-310), one stores English prose (optimize.py:152). The monthly verifier (V007:289-318) *proposes only* — it never sets VERIFIED — and selects only `DESCRIPTION LIKE 'Auto-suspend tune: %'`, a description no executed path books. V038 autobook DOES settle rigorously (14d measured actuals, $5 floor) but only rows it booked itself (`SOURCE_CHANGE_ID` match, V038:96); app-booked rows have NULL SOURCE_CHANGE_ID and stay ESTIMATED forever while six screens promise "the monthly verifier proves or rejects" (optimize.py:219, 242, 595, 665; alerts.py:252, 267-268, 321-322). Core proof-run fix = r27 #19, already ROUTEd to r28; the selection gap and false promises are NEW and go with it. Caption honesty ships now. |
| P1-B | "Audit actor columns still record the app owner" | **TRUE — SHIP now (app-only)** | r27 #4 shipped viewer identity for prefs/usage/settings/ACK_BY/VERIFIED_BY — but every *audit* table still records the owner: both ALERT_AUDIT INSERTs omit ACTED_BY so the CURRENT_USER() default fires (alerts.py:80, 112 vs V004:59), and all six REMEDIATION_LOG INSERTs omit EXECUTED_BY (alerts.py:293-299; operations.py:850-856, 904-910; optimize.py:227-232, 559-564, 651-658 vs V012:40). Bonus defect: the comment at alerts.py:76-77 claims "the table has no actor column" — false, ACTED_BY exists. ~12 statement edits, zero migration; mart_sql.py:378 already SELECTs EXECUTED_BY so the panel lights up correct names immediately. |
| P1-C | "Granular costs additive but not decision-grade" | **TRUE — SHIP V050 (role split) + app-only ETL fixes; TCO routes** | (a) Equal split dilutes targets by construction: a MERGE reading S sources credits the target 1/(S+1) (V049:100-132) — deliberate additivity, but read-vs-write role is discarded at the dedup. (b) No STORAGE arm (7 arms, all compute credits) — deliberate; bytes aren't credits, and jamming a storage arm into the additive credits ledger would corrupt sums. (c) ETL $/M-rows denominator overcounts: every statement's ROWS_PRODUCED sums (etl_sql.py:36, 54), so a 3-stage pipeline counts logical rows up to 3×; run_id is optional — untagged-run credits inflate $/run (etl_sql.py:28, 51-53). Credits are NOT double counted (the reviewer's strongest phrasing overshoots). |
| P2 | "Performance gates can miss actual scans" | **TRUE — SHIP lint fix + honest telemetry; uphold r27 #15** | The literal-count is codified as a lint proxy (house law 3), so the *mechanism* is known — but the verifier found unit_costs' first render fires **unconditional live QAH×QH scans** (the measured-costs batch, unit_costs.py:53-64 → insights_sql.py:746) under a 0 budget: the pin is blind to builder-mediated scans and the blinding is even instructed ("keep the literal out of source labels"). Batch telemetry confirmed dishonest: per-member elapsed = wall/batch_size (query.py:488, 523) while each QueryResult carries the FULL batch wall (query.py:493, 527) — the two surfaces disagree about the same fetch. Runtime-in-CI stays DECLINED (r27 #15); the fix is a builder-level static gate + honest per-member timing. |

## The 20 recommendations

| # | Recommendation | Verdict | Reasoning |
|---|----------------|---------|-----------|
| 1 | Immutable proof runs | **ROUTE (r28 — already queued; Codex concurs)** | = r27 #19 verbatim + the CLAUDE.md queue entry. New from this pass: the proof-run round must ALSO fix verifier selection (typed link column so app-booked rows are selectable) and dedupe against V038's parallel $0 autobook row. |
| 2 | ACTION_QUEUE as operating center | **ROUTE (r29/r30 — already queued)** | = r27 #20. Codex adds hard evidence: the repo has exactly ONE INSERT path (ai_chargeback.py) and **zero UPDATE paths** — actions can never be claimed or closed from the app. Basic lifecycle (status/owner/due edits) is app-only and could pull forward if wanted. |
| 3 | Transactional action procedures + idempotency | **ROUTE (r28 — already queued)** | = r27 #10-full/#11/#12. The partial-success risk is real and self-documented (alerts.py's own docstring: "True single-transaction atomicity needs a stored proc — r28"). |
| 4 | Viewer identity everywhere | **PARTLY STALE — SHIP the remnant** | Blanket ask ignores that r27 #4 shipped (identity.py + seven wired paths, locked). The live remainder is exactly finding P1-B: ship it now. |
| 5 | Separate production cost from consumption influence | **SHIP (V050, combined with #18)** | Role-tag the dedup rows (WRITE wins on read+write collapse), emit QUERY_COMPUTE_READ / QUERY_COMPUTE_WRITE arms, still credits/N — additivity holds, no schema change, readers keep summing. One derivation-law re-derivation shared with #18. |
| 6 | Full object TCO ledger | **ROUTE (design round after V050)** | Right direction, wrong shape as stated: storage bills $/TB-month, not credits — the honest build is a parallel FACT_OBJECT_STORAGE_DAILY (TABLE_STORAGE_METRICS: active/TT/failsafe/clone-retained) joined at read time, never a "storage credits" arm. Method honesty: it would be ESTIMATED beside MEASURED arms. |
| 7 | Daily object-cost reconciliation | **SHIP (app-only slice now); persisted version → reconciliation-v2 (queued)** | Verified: nothing reconciles the object ledger today — SP_NIGHTLY_RECONCILE is a rebuild, not a check, and never touches FACT_OBJECT_COST_DAILY. Cheap honest slice: extend Admin's mart-vs-live recon panel with arms+residual vs QAH over a lag-safe window + per-maintenance-arm vs source history. The deploy runbook's residual query is the seed. |
| 8 | Correct ETL unit-cost formulas | **SHIP (app-only)** | Verified defects per P1-C(c). Rewrite: run-grain CTE first; rows from write-statement types (or the tag's units-produced) only; run_id-coverage KPI so partially tagged fleets degrade honestly instead of silently overstating $/run. Update the test_phase3_etl locks with it. |
| 9 | Unified measured-cost fact | **ROUTE (design round)** | Real but narrower than claimed (pattern/object/graph/AI grains already persisted; the graphs panel reads mart-first — Codex's target list was partly stale). The remaining win is a per-query credits fact, but it re-derives the byte-locked QH extract and changes watermark semantics for QAH's ~8h lag. Needs a design doc, not a sprint. |
| 10 | UI-to-SQL scope contract | **SHIP (app-only + lock)** | Both halves verified: no test asserts pages thread filters() into builder args, and the ETL panel genuinely ignores database/schema (builders lack the args entirely). Fix the builders + thread the args (auto-covered by the P4 filter matrix once the params exist), add a page-wiring lock for the worst offenders. |
| 11 | Executable metric registry | **SHIP light now; full → badges round** | Verified: the registry drives nothing (Admin display only) and its test can't discover an unregistered KPI. Ship: fix the overclaiming Admin caption. Route: threading registry keys through kpi_row (pairs with #13). |
| 12 | Effective-dated price book | **DECLINE for now (revisit trigger documented)** | Mechanically TRUE — one current rate reprices history everywhere. But the owner confirmed the effective rate IS 3.68 == the default (2026-07-15), the reconciliation band (test_p4_org_reconciliation) exists to catch drift, and the rate has never changed. Build it when a renegotiation is on the horizon; until then it is churn with zero benefit. The test module already documents this exact trigger. |
| 13 | Confidence badges beside every cost | **ROUTE (UX round with #11)** | "Nothing discloses trust" is wrong (result_caption ~40 sites, mart/live/stale KPI badges, method prose) — but no METHOD chip exists. Owner has twice rejected visual clutter; this is a taste call and belongs in a deliberate UX round, not a bolt-on. |
| 14 | Why-spend-changed decomposition | **ROUTE (analytics round)** | Compare already isolates movers at three grains; the causal bridge is real added value. With a flat rate the price term is degenerate — the meaningful axes are volume, pattern mix, idle share, size changes. Logic-layer work with the P4 lock pattern. |
| 15 | Budgets for pipelines/objects/environments | **PARTLY exists — ROUTE (owner decision on grains)** | Department budgets are end-to-end TODAY (DEPT_BUDGETS + editor + live pace alert arm [17]) and AI has a budget setting — Codex undercounts. Extending = migration + new scan arms per house dedupe-key pattern, and pipeline budgets depend on tag adoption that is still partial. Owner picks which grains earn alerts. Overview stays budget-KPI-free (standing decision; doesn't block this). |
| 16 | Decision-grade perf telemetry | **SHIP light (sfqid + honest batch time); columns → migration round** | Verified gaps: no Snowflake QUERY_ID captured, no queue/bytes, SQL_HASH can't join QPH. Ship app-only: capture sfqid, fix the wall/size division (P2-b). QUEUE_MS/BYTES columns ride the next migration. Cache-by-security-context: keep per-viewer for prefs-sensitive frames; fix the inaccurate comment; full redesign is not warranted by evidence. |
| 17 | Nightly live gates + compile-only canary | **SHIP compile-only mode; live gates ROUTE** | Canary today = 181 statements, on-demand, serial, post-aggregation row caps — compile-only (EXPLAIN) mode is app-only and cheap. Nightly LIVE gates: r27 #15 declined CI-runtime gates; as a Snowsight-scheduled task it's a different animal and folds into reconciliation-v2 — note a connections.toml exists on this machine (existence confirmed only); any agent-side use must first prove the role is read-only per handoff §5. |
| 18 | One-pass incremental object loader | **SHIP (V050, combined with #5)** | TRUE and understated: the effective proc scans ACCESS_HISTORY **4×** and QAH **2×** per run (V049 dedup + obj_q, both UNIONs; byte-identical qa CTEs). The house's own V041 staged-extract pattern is the template. One re-derivation delivers the staged bridge AND the read/write arms. |
| 19 | Entity-360 + executive/analyst personas | **Split: 360 → queued (r29/r30); personas → DECLINE (re-litigates)** | Entity-360 = r27 #20's own scope, already queued. The least-privilege owner/viewer model re-litigates BOTH the standing owner decision ("Access = SNOW_ACCOUNTADMINS + SNOW_SYSADMINS, period", 2026-07-13) and r27 #5 (ROUTEd to owner, never approved). Available any time the owner reverses — but it is an owner reversal, not a reviewer finding. |
| 20 | Generate docs from code | **SHIP the fix + lock; DECLINE CI generation** | Verified: DEPLOYMENT.md's run-list stops at V034 while the floor is V049 — the r27 #9 rewrite touched narrative, not the list, and its lock never covered migration lists. Fix the three docs, add a lock pinning the run-list floor to _EXPECTED_MIGRATIONS. CI-generated manifests stay declined per r27 #2/#9: for a one-owner repo, the lock IS the manifest. |

## Recommended sequencing (replaces Codex's "1-4 then 5-10")

Codex's tranche puts three already-queued design rounds first. The house
version orders by trust-per-effort:

- **Tranche A — v4.51, app-only, no migration (this week):** P1-B actor
  stamps (~12 edits + the false comment); savings caption honesty (stop
  promising the phantom verifier; alerts + optimize, 6 sites); #8 ETL formula
  fixes; #10 ETL scope threading + page-wiring lock; #20 doc fix + lock; P2
  honest batch telemetry + sfqid capture + builder-level scan lint (amend
  house law 3's text with it); #17 compile-only canary mode; #11 caption.
- **Tranche B — V050 migration round (Joe in Snowsight, AFTER the pending
  V048/V049 deploy):** #5 + #18 in one SP_LOAD_OBJECT_COST re-derivation —
  staged AH/QAH bridge (4×+2× scans → 1×+1×) + QUERY_COMPUTE_READ/WRITE
  arms; #7's recon slice reads it from Admin.
- **Tranche C — the r28 action layer (already queued; Codex concurs on
  priority):** #1 + #3 + #2-basic-lifecycle + the typed savings link that
  closes P1-A's selection gap. This was the queue before the review; the
  review's contribution is evidence that it is the right next big round.
- **Owner decisions parked:** #15 (which budget grains earn alerts), #19
  personas (requires reversing the access decision), #12 (price book —
  trigger: contract renegotiation), #6/#9 (design docs first).
