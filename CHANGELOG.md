# Changelog

## 4.407.0 - Full-app adversarial hardening sweep (2026-09-01)

An 11-dimension adversarial sweep of the entire app (SQL-injection, entitlement, money/metric,
numerical, error-handling, caching, performance, Ask-grounding, input-validation, security-logic,
alerts/DQ), each finding independently refute-verified, then ground-truthed in the code by hand. Seven
dimensions came back clean; four real defects were confirmed and fixed:

- **[error-boundary] `run()` could re-raise on a driver exception with a hostile `__str__`, breaking its
  "never raises" contract.** In `run()`'s except path, `_classify_error` (query.py) runs FIRST and did a
  bare `str(exc)` — before the already-guarded `record_error` — and `format_snowflake_error` (errors.py)
  did the same. An exception whose `__str__` itself raises (the exact lazy-format driver case last batch
  hardened `record_error` against) would propagate out of the except block, turning a one-panel soft
  `ok=False` failure into a whole-page crash at `safe_page`. Both conversions are now guarded (classify →
  "other"; format → typed placeholder), completing the coverage `record_error` started.
- **[Ask grounding] The Cortex/AI answerer captioned a 90-day figure with the requested window (up to
  365d).** `cortex_model_costs` is a live ACCOUNT_USAGE builder clamped to 90 days, but `_analyze_cortex_
  by_model` used the raw `params.days` in its headline, source and `meta['days']` — so "what is driving
  cortex credits this year" scanned 90 days yet was labeled 365d, a grounding-honesty breach. It now
  computes the effective window like its two sibling answerers, labels everything with it, and adds a
  "capped to the live 90-day window (you asked for Nd)" bullet when clamped.
- **[Operations] The Queries tile labeled the selected window even when the live fallback served only
  90 days.** When the hourly mart is unavailable (fresh install, mart outage) or a schema+warehouse/user
  filter combo forces the live path, the tile fell back to a 90-day-clamped scan but still rendered
  "Queries (365d)" — up to a 4× understatement under a wrong scope label. The label now reflects the
  window actually served (`min(days, 90)` on the live path), gated on the existing `used_mart` flag.
- **[Security review] Per-user "Sensitive privileges" double-counted a role reached by multiple paths.**
  `SENSITIVE_PRIVILEGES` is a per-effective-role attribute in `effective_access` (joined on
  EFFECTIVE_ROLE), so it repeats on every access-path row; the summary summed raw path rows while
  EFFECTIVE_ROLES used `nunique`. A user reaching one admin-utility role via two direct roles (or diamond
  inheritance) showed double its true reach, misdirecting least-privilege review. A new pure helper
  `sensitive_privileges_by_user` collapses to distinct (user, effective role) before summing. (Fails safe
  — it over-counted, never hid exposure.)

Nine new tests pin all four (hostile-`__str__` through both helpers; cortex 90d cap; served-window label;
role-deduped sensitive count). Version lockstepped 4.406.0 → 4.407.0.

## 4.406.0 - Hardening pass on this session's new features (2026-09-01)

An adversarial hardening + smoke-test sweep of everything shipped this session (Ask USD/Cortex,
the scope-bar fix, the recovery boundary, entitlement). 18 findings triaged; the safe fixes land here.
The one "HIGH" (a Decision Studio ROI horizon mismatch) was ground-truthed and cleared as a false
positive — the code is dimensionally correct and already documented as a deliberate design (below).

- **[Ask hardening] The Cortex answerer's gate now needs an AI word AND a spend word.** It routed on a
  lone AI token, so "why is my llm task failing" could hijack it away from task-failures. The
  `require_all` is now two AND-groups (AI term × spend/driver term). Also: the answerer declares
  `account_wide` so the scope caption reads "account-wide" instead of a misleading `company=…`, and the
  evidence rate-note is labeled by each column's actual kind (new `is_ai_credit_column` helper) rather
  than by comparing the two configured rates — an admin may set the compute and AI rates equal.
- **[Security — write axis fails closed] `is_operator()` now returns `False` for an unidentified SiS
  viewer.** Under owner's-rights SiS every viewer runs as the app owner's role, so the role→profile
  fallback would have treated a viewer with no resolvable `st.user` as the owner-operator — an
  escalation to owner-privileged writes. It now mirrors `active_profile()`: identified → allowlist,
  unidentified-on-SiS → denied, off-SiS → the role fallback (local dev/tests unchanged).
- **[Error boundary robustness] `record_error` can no longer be defeated by a hostile `__str__`.** An
  exception whose `str()` itself raises would have aborted ref-minting and buffering, defeating
  `safe_page`. The message is now rendered once behind a guard (falls back to a typed placeholder), so
  the boundary always produces a stamped reference. The recovery caption also stops over-promising
  persistence — the off-box sink is best-effort, so it now says "in the persisted log when the
  connection was live."
- **[UI regression — compact density] The scope toolbar no longer re-clips in compact mode.** The
  compact-density stylesheet still set `.block-container` padding-top to `2.1rem`, overriding the tested
  `2.6rem` header clearance and reintroducing the clip fixed in 4.402 for compact viewers. Pinned to
  `2.6rem` with a regression comment.
- **[UI — task graph] A Highlight status with zero nodes no longer greys the whole graph.** Selecting
  e.g. "Suspended" when nothing is suspended dimmed every node (nothing left to highlight), reading as a
  blank/broken chart. Those options are now disabled at render, mirroring the fit-button guards.
- **[Investigated — NOT a bug] Decision Studio's "pays for itself" ratio was checked for a horizon
  mismatch and cleared.** A finder flagged `_proof_signals` dividing a QTD numerator (`VERIFIED_QTD_USD`)
  by a trailing-30-day denominator (`APP_CREDITS_30D`) as inflating the flagship ROI multiple. Ground-
  truthing overturned it: every savings item is a **monthly-magnitude** value (booked as `monthly_usd` in
  `savings_rollup.py`; Decision Studio impact is "normalized to 30 days"), so the QTD sum is the
  *accumulated monthly savings rate*, and dividing a monthly rate by the monthly run cost is dimensionally
  consistent — monthly ÷ monthly, exactly the "same horizon on both sides" the scorecard tooltip claims.
  This was already the deliberate 2026-08-30 fix, documented at `mart_sql.py:1190-1197`. Residual worth an
  owner's awareness (not a code defect): the QTD numerator accumulates the monthly rate across the quarter,
  so a verified saving that later lapses would overstate the current rate until the quarter rolls — a
  savings-durability modeling question, not a horizon error.

## 4.405.0 - Defer-tier triage: fix the one real defect (2026-09-01)

- **[#3, defer tier] Re-triaged all 28 deferred Wave-2 items against current code; fixed the one that
  was a real defect.** A 5-agent pass ground-truthed each deferred item. Result: **27 of 28 correctly
  stay deferred** — they're cosmetic, low-ROI, would-regress, or already done — confirming the original
  adjudication. Two premises turned out stale in a good way: Security **already** has a page verdict
  (`security_center.py`, via `domain_posture()`), so the "missing Security verdict" was not missing; and
  Decision Studio's verdict (#8) is done but its Prove/Decide/Track relabel is pure cosmetic regrouping.
  The single genuine defect — **#47** — is fixed: the disconnected screen rendered the OVERWATCH wordmark
  **twice** (the sidebar already brands it, plus a redundant `st.title`), so `main.py` now drops the
  duplicate and lets the "No Snowflake connection" error lead. Everything else stays deferred by design.

## 4.404.0 - Ask Cortex answerer (CoCo rate) + contract-line wrap (2026-09-01)

- **[#1] Ask OVERWATCH now answers Cortex/AI-spend questions — and the $2.20 rate fires.** A new
  answerer ("which model is driving AI spend", "what is driving cortex credits") ranks Cortex models
  by token credits from `ACCOUNT_USAGE.CORTEX_FUNCTIONS_USAGE_HISTORY`, with the same outlier / honest
  no-data / account-wide-scope discipline as the other answerers. Its evidence column is named
  `AI_CREDITS`, so the USD helper prices it at the **CoCo/AI rate ($2.20)** automatically (the intent is
  also registered in `_AI_INTENTS`) — completing the per-question rate logic we built earlier: compute
  questions show $3.68, this one shows $2.20. Routes on a single AI/Cortex gate at priority 1, so it
  never steals plain-spend or cloud-services questions. New tests in `test_ask_registry.py`.
- **[#4] Scope-contract line no longer runs to the edge.** The long "Applies: …" sentence had no
  measure cap, so it stretched the full 1360px content width on wide monitors. Capped `.ow-filter-
  contract` to `max-width:80ch` so it wraps into a tidy readable block.

## 4.403.0 - Note the scope-bar/header fix against regression (2026-09-01)

- **Documented the scope-bar-clip fix so it can't silently come back.** The v4.402.0 fix (keep the fixed
  header transparent + enough `.block-container` top padding) is confirmed working. Added a "DO NOT paint
  the header opaque" warning in `theme.py` explaining why (the scroll container runs under the fixed
  header, so an opaque header hides the top of the scope toolbar), and named the regression lock
  accordingly (`test_ask_usd_wiring_and_scope_bar_not_clipped_by_header`) so re-opacifying the header or
  shrinking the padding fails CI. No behavior change.

## 4.402.0 - Triage-bar clip: the real root cause (opaque header) (2026-09-01)

- **Scope bar top clipped — actual root cause found (thanks to the "scroll bar" hint).** The scroll
  container extends up under Streamlit's fixed header, and v4.385's dark-surface change had painted that
  header **opaque** (`--ow-bg`), so it covered the top of the scope toolbar — the clipped "SCOPE" and
  the empty space at the top of the scrollbar track. Fix: the header is now **transparent** again (the
  dark app surface behind keeps the look dark, so #45's intent holds), and `.block-container`
  `padding-top` is raised (1.1→2.6rem, compact 0.6→2.1rem) so the content clears the header controls.
  The earlier label-level tweaks (single-line "Scope · Account view" label) stay — they're correct — but
  the clip itself was the header, not the label. Reverted the speculative toolbar `overflow` override.

## 4.401.0 - Triage-bar clip: single line + toolbar overflow (2026-09-01)

- **Scope/triage bar clip — the actual root cause.** CSS on the label alone (line-height, min-height)
  never fixed it because the clip came from the **bordered toolbar container**, not the label box: a
  constrained height on the border wrapper sheared the top off the two-line "SCOPE" label and showed a
  scrollbar. Two changes: (1) the scope label is now a single line ("Scope · Account view", two spans)
  so it's exactly the height of the single-line selectboxes and can't overflow the row; (2) the
  toolbar's border wrapper is forced to `overflow:visible; height:auto; max-height:none` so it sizes to
  its row and can never clip or scroll it. (CSS is re-injected every run, not cached, so this takes
  effect on redeploy.)

## 4.400.0 - Triage-bar clip: real fix (min-height) (2026-09-01)

- **Scope/triage bar still clipped — fixed properly.** The v4.399.0 line-height tweak wasn't enough:
  the two-line "Scope / Account view" label still rendered taller than the fixed `height:2.28rem`,
  spilled above the box, and the bordered toolbar clipped the top of "SCOPE". Switched the label from a
  fixed `height` to `min-height:2.28rem` (plus a little vertical padding) so the box grows to contain
  the label — nothing can spill past it, so nothing clips — while still matching the 2.28rem
  selectboxes when the label is short.

## 4.399.0 - Ask USD estimates + triage-bar clip fix (2026-09-01)

- **Ask OVERWATCH — estimated $ next to credits.** Every Ask evidence table now inserts a `<col>_USD`
  column beside each credit-quantity column (ALLOC_CREDITS, CS_CREDITS, …), formatted as dollars, with
  a caption naming the rate used. The rate is chosen **per column**: a column whose name carries an
  AI/Cortex segment (CORTEX_CREDITS, TOKEN_CREDITS, …) prices at the CoCo rate ($2.20), everything else
  at the compute rate ($3.68) — so a warehouse/cloud-services credit column is never mispriced, and a
  ratio (CREDIT_SHARE) or rate (CS_PER_1K_RUNS) is never dollarized. Rates come from Admin → Settings
  (`CREDIT_PRICE_USD` / `AI_CREDIT_PRICE_USD`). Today all four Ask answerers report compute credits, so
  you'll see $3.68; the $2.20 path is wired for a future Cortex answerer (register its intent in
  `_AI_INTENTS`) or any AI-named credit column. New pure `app/logic/ask/pricing.py`, unit-tested.
- **Triage/scope bar clip fix.** The scope strip's two-line "Scope / Account view" label spilled past
  its fixed 2.28rem row at the browser-default line-height, and the bordered toolbar clipped the
  overflow — the "cut-off triage bar" seen on every page. A compact `line-height` on the label makes
  the two lines fit inside the row.

## 4.398.0 - Caption sweep: second re-audit of the remaining sections (2026-08-31)

- **[#1, remaining-sections re-audit #2] Reverted Overview's last conversions; it now hides nothing.**
  A second independent re-audit of Overview, Brief, Security, and Admin found two more Overview
  over-conversions (Brief/Security/Admin all confirmed clean in both directions). Both bundled a scope
  caveat with the basis: the MTD/Projected note ("storage/transfer/currency are separate — org rate
  card is billing truth", a not-the-whole-bill scope cue on the on-screen KPIs) and the AI-digest note
  ("Account-wide narrative — does not change with the company filter"). While fixing those, the last
  remaining Overview conversion — the serverless/AI note whose "separate from the warehouse-compute
  drivers above" is a don't-double-count reconciliation cue — was reverted too. Overview now converts
  **nothing**: every one of its captions carries a scope cue, misread caveat, legend, action, or data
  value, so all stay visible. `overview.py` no longer imports `methodology_note`. App-wide the sweep
  is down to ~15 deliberate conversions.

## 4.397.0 - Caption sweep: second Cost re-audit (2026-08-31)

- **[#1, Cost re-audit #2] A second independent re-audit of the Cost files caught two over-conversions
  the first pass missed.** Both carried a misread caveat buried in otherwise-methodology prose, so the
  first re-audit let them through: `optimize.py`'s repeat-query-profiles note (its closing clause
  "Avoidable $/30d … an estimate, **not** the hour-share allocation the panels above use" is the exact
  "X, not Y" caveat kept elsewhere on the same page), and `unit_costs.py`'s parameterized-hash note
  ("cheap-but-constant often out-bills expensive-but-rare" is an interpretation takeaway). Both reverted
  to plain captions. Net: Cost conversions drop from 8 to 6, and `unit_costs.py` no longer imports
  `methodology_note`. Two independent re-audits now agree the rest of Cost is correct.

## 4.396.0 - Caption sweep: full cross-section re-audit (2026-08-31)

- **[#1, full re-audit] Ran the both-directions re-audit across every remaining section.** After the
  Cost re-audit, the same audit+verify (wrongly-converted and missed) was run over all other pages:
  the conversion-bearing ones (Overview, Brief, Security, Operations, Admin) and the zero-conversion
  operational pages (Control Room, Alerts, Decision Studio, Ask, Workbench). Result: **two more
  over-conversions found, both on Overview** (swept with the earliest, weaker rubric) and reverted to
  plain captions — the score-defaults note (it decodes the on-screen `(capped)` token and points to
  the `SCORE_PTS_*` settings) and the score-trend note ("judge the trend, not the level" — an
  interpretation/misread caveat on a chart that also renders in operator mode). Everything else was
  confirmed correct in both directions, including one Admin caption the audit flagged as a missed
  conversion but the verify correctly kept (its "batch-wall telemetry excluded from totals" clause is
  a scope cue on the failure-rate figures above it). Overview conversions drop from 5 to 3. The #1
  caption sweep is now audited clean across the entire app.

## 4.395.0 - Caption sweep: Cost correctness re-audit (2026-08-31)

- **[#1, Cost re-audit] Reverted five over-conversions caught on a thorough second pass.** A dedicated
  re-audit of all seven Cost files (both directions: wrongly-converted and missed) found that the
  first Cost pass was too aggressive on "X, not Y" misread caveats and where-to-go wayfinding. Five
  captions that had been hidden in Audit mode are now plain `st.caption` again because the operator
  needs them: the chargeback `"allocated, never attributed"` caveat (cost.py), the size-scenario
  `"bounded range — not a promise"` and the `"Measured, not allocated … reads below the allocated one"`
  reconciliation notes (optimize.py), the measured-price-tags line whose second sentence is wayfinding
  to the allocated/pipeline views, and the `$0-days` note that decodes the literal `$0.0000` the trend
  table shows (unit_costs.py). No missed conversions were found. Net: Cost conversions drop from 13 to
  8, and `cost.py` no longer imports `methodology_note`. Locks updated to the new exact counts.

## 4.394.0 - Operator/Audit caption sweep: remaining pages (2026-08-31)

- **[#1, remaining pages] Caption sweep across Operations, Control Room, Alerts, Decision Studio,
  Admin, Ask, and Workbench.** A conservative pass (bias-to-KEEP rubric) classified all 184 captions
  on these files. Only six were pure audit-reproduction detail and moved to `methodology_note`:
  Operations' elapsed-time sort note and its two change-diff table descriptors, and Admin's self-cost
  measurement provenance plus the two `_SCAN_NOTE` (first-load scans directly, caches one hour) lines.
  **Control Room, Alerts, Decision Studio bodies, Ask, and Workbench converted nothing** — their
  captions are overwhelmingly display legends, action hints, conclusions, scope disclaimers, and
  data-bearing lines that the operator needs at the table. This completes the operator/audit caption
  sweep (#1) across every page. Total across the whole app: ~440 candidate captions reviewed, and a
  deliberately small set converted, keeping every legend and don't-misread-this caveat visible.

## 4.393.0 - Operator/Audit caption sweep: Cost/Optimize (2026-08-31)

- **[#1, Cost part 2] Caption sweep on `optimize.py` — the largest Cost file (58 captions).** Seven
  pure basis/model/provenance captions moved to Audit mode via `methodology_note`: the scenario-replay
  and mechanical-scenario model notes, the forecast-gating rule, the measured-basis note, the
  recommendation-ranking methodology, and the self-booking-changes provenance. The other proposed
  conversions were **kept visible** on review — they're things the operator needs at the table, not
  audit detail: the `Actionable $` column definition, the `"eligibility is utilization, not a
  dollarized saving"` misread caveat, the cost-arm label legend (`QUERY_COMPUTE_WRITE` = building the
  object), and the "no ETA is intentional" blank-column explainer. This finishes the Cost caption sweep
  (with `ai_chargeback.py` and `compare.py` having had no pure-methodology captions to convert). Across
  all of Cost the workflow proposed 20 conversions; ground-truthing landed on 13, keeping the other 7
  as legends and don't-misread-this caveats.

## 4.392.0 - Operator/Audit caption sweep: Cost, part 1 (2026-08-31)

- **[#1, Cost part 1] Caption sweep across the smaller Cost files.** Four of the Cost page's files
  (`cost.py`, and the Spend / Contract / Unit-costs parts) had their pure basis/provenance captions
  moved to Audit mode via `methodology_note`: the chargeback allocated-vs-attributed caveat, the
  notebook-runtime source label, the org-billing provenance/lag line (freshness stays on the
  independent `result_caption`), and three measured-basis notes on Unit costs. Everything else stayed
  visible — the sweep deliberately **kept** display legends (the task-tree indentation rule, the
  `"n/a" = Cortex Code` column key), misread caveats (`$/run is allocated, not per-run metered`), scope
  disclaimers, and every data-bearing line. A scope→verify workflow classified all 79 captions across
  these files; ground-truthing then pulled several proposed conversions back to KEEP where the caption
  was actually a legend or a don't-misread-this caveat the operator needs at the table. `optimize.py`
  (the largest Cost file) is the next pass.

## 4.391.0 - Operator/Audit caption sweep: Brief & Security (2026-08-31)

- **[#1, increment 2] Caption sweep continues to Brief and Security.** The standing operator/audit
  sweep (pure how-computed / provenance captions render only in Audit mode via `methodology_note`,
  so operator mode leads with conclusions) reached two more pages. Every `st.caption` on Brief (6)
  and Security (43) was classified; only the genuinely pure-methodology ones convert: Brief's
  billing-basis note (the same disclosure Overview already hides), and Security's alert-provenance
  note (`SEC_CRED_EXPIRY` cadence) and a sort-order note. Everything else stayed visible —
  Security's captions are mostly conclusions, scan-cost warnings, scope disclaimers, and
  interpretation caveats the operator needs. One caption the scoping pass proposed converting
  (Security's "behavioral flags, not verdicts — the point is that someone looks") was **kept** on
  review: hiding that in operator mode risks over-reacting to a flag, exactly like the sibling
  heuristic-score caveat that's already a plain caption. Cost (~159 captions across 8 files) remains
  its own future increment.

## 4.390.0 - UI review Wave 2: DAG status & neighborhood filters (2026-08-31)

Completes Wave 2. The interactive task graph offered name search only; it now supports the
core "why did it break?" workflow directly on the graph.

- **[#37] Status dimming + neighborhood isolation on the task DAG.** A new toolbar control
  highlights one status (failed / suspended / critical path) and dims the rest, and clicking a
  node isolates its **upstream + downstream lineage** (dimming everything off that node's blast
  radius); clicking the root again, the Fit button, double-click, or the `0` key clears back to
  the full graph. The data plumbing that makes it possible: nodes now carry a canonical
  `data-node-id` and edges carry canonical `data-source` / `data-target`, so the client builds an
  adjacency map and walks it with a cycle-guarded BFS. A `suspended` node class was added
  (mutually exclusive with `failed`, so the positional `failed`-class lock is preserved). Every
  highlight/dim class is toggled at **runtime via classList** — never emitted in the built markup —
  so the `edge` / `edge edge-critical` class-count locks stay byte-stable. Still fully
  self-contained (no external scripts); the dim/isolate interaction wants a live SiS eyeball.

## 4.389.0 - UI review Wave 2: standardized failure recovery (2026-08-31)

- **[#46] One consistent recovery affordance on a page-render failure.** The `safe_page` error
  boundary showed a prose "the failure was logged" note with no way to act on it. It now renders a
  standard recovery block: a **copyable error reference**, a **Retry** (bumps the read salt and
  re-runs the page), and an **Open error log →** jump that appears only for a viewer entitled to
  Admin (so a non-admin isn't stranded — `request_navigation` clamps Admin→Overview otherwise).
  `record_error` now mints that reference (timestamp + a short hash of the error), stamps it on the
  in-session buffer entry, prepends it into the persisted `APP_ERROR_LOG.CONTEXT`, and returns it —
  so the ref an operator copies matches its log row. The Admin error table shows the ref column.
  Scoping caught the over-reach the first pass proposed (adding Retry to the panel-level
  `empty_state('unavailable')`, which fires on *every* failed panel app-wide) and left it out; all
  recovery-block imports stay lazy so the `query → errors` module cycle is preserved.

## 4.388.0 - UI review Wave 2: object-cost reconciliation coverage (2026-08-31)

- **[#30] Reconciliation coverage on the top-objects cost table.** The `reconciliation_footer`
  primitive (honest shown/canonical/coverage, never a fabricated ratio) was wired to only three
  tables. The top-objects cost table (Cost → Optimize) now discloses what fraction of
  object-attributed spend its top-N covers. The canonical parent is the by-**arm** aggregation
  minus the non-object residual arm (`QUERY_COMPUTE_RESIDUAL`) — an *independent* read from the
  by-object frame, and since the residual is exactly the `UNATTRIBUTED` rows the top-objects query
  already excludes, the shown rows are a true subset of the parent. Scoping caught two traps: the
  first-pass parent (`_adf` including the residual) mixed universes and would have understated
  coverage; and the app×user measured table was **deliberately left footer-free** because its only
  candidate parent is summed from the same frame — a tautological 100%, which the app's
  reconciliation design law forbids.

## 4.387.0 - UI review Wave 2: drill + provenance adoption (2026-08-31)

Two adoption sweeps that wire existing primitives onto surfaces that opted out.

- **[#25] Entity-drilling on the Security user tables.** Four user-identity tables rendered as
  inert `styled_table`s while ~15 other identity tables drill to Entity 360. The MFA-gaps,
  single-factor-login, failed-login, and dormant-reawakening tables now use `entity_nav_table`
  (a row click opens that user in Control Room → Entity 360), mirroring the dormant-users table
  right beside them. `failed_logins` was included after ground-truthing its builder (`GROUP BY
  USER_NAME`, one row per user) — the scoping skeptic caught it as a target the first pass wrongly
  excluded.
- **[#1] Operator/Audit caption sweep — Overview (first increment).** `methodology_note` (audit-mode
  only) was built but used nowhere. Five pure how-computed / provenance captions on Overview (the
  credit-spend-model note, the serverless/AI basis note, the score-defaults and score-trend caveats,
  the AI-digest provenance) now render only in Audit mode, so operator mode leads with conclusions.
  The account-wide **scope disclaimer** on the runway bar was deliberately left a plain caption —
  hiding it would strand the bar with no cue that it isn't narrowed by the company filter (a
  skeptic-caught over-reach). This is the standing sweep's first page; more follow one per session.

## 4.386.0 - UI review Wave 2: Decision Studio verdict (2026-08-31)

First Wave-2 item — the "should I worry?" line now leads Decision Studio too, closing the
last major page without a page-open verdict (pairs with Wave-1 #7 on Operations).

- **[#8] Decision Studio page verdict.** DS opened straight into its section bar with the
  prove-it conclusion buried inside the Scorecard section. It now opens with a severity-striped
  verdict line, **derived from the same `proof_verdict`** the Scorecard banner shows — so the
  hoisted line can never disagree with the scorecard below it. New pure
  `verdict.decision_studio_signals` maps the proof level to the page vocabulary and preserves
  the honest **"unproven"** state (an account with no verified outcomes reads *Watch — not enough
  verified outcomes yet*, never a false green). The verdict and the Scorecard now read/compute
  through one shared `_proof_signals` helper (cache-keyed, so no extra I/O), wrapped in the same
  `st.status` cold-load line as #48.

## 4.385.0 - UI review Wave 1: chart, theme & cold-load (2026-08-31)

The final Wave-1 batch — chart clarity, one committed theme, and named cold-loads.

- **[#31] Budget burndown as a real chart.** The Overview budget-pace panel was a bare `st.line_chart` of
  cumulative-actual vs the budget line — no currency formatting, no legend semantics. It now renders through
  a dedicated `charts.budget_burndown_chart`: USD-formatted tooltips and axis, an accent actual line against
  a muted dashed budget guide, so over/under-pace reads at a glance.
- **[#36] Frame failures / critical path on the task graph.** The interactive DAG could pan and zoom but an
  operator opening a large graph still had to hunt for the failures. Two toolbar buttons now jump the
  viewport to just the failed tasks or just the critical path (a shared subset-bbox fit that reuses the
  whole-graph fit math); each disables itself when its subset is empty. Failed nodes carry a `failed` class
  derived from the same red hue they already paint, so the framing can't disagree with what's on screen.
- **[#45] Commit to the dark surface.** A half-finished light path let some table cells adapt toward light
  while their cards stayed dark — a "light inside dark" contradiction under a light Streamlit base theme.
  The app now forces its own surfaces dark (`theme.py`) and tables no longer adapt (`_theme_is_light()` →
  False); the light palette is parked, not deleted, for a future complete-light effort.
- **[#48] Name the heaviest cold-loads.** Operations (verdict input reads) and Decision Studio (the full proof
  ledger) wrap their heaviest first-paint reads in a `st.status` line, so a slow cold load reads as progress
  rather than a hung page (both `hasattr`-guarded for older Streamlit).

## 4.384.0 - UI review Wave 1: shell & verdict (2026-08-31)

Three shell/verdict Wave-1 items.

- **[#7] Operations page verdict.** Operations was the largest page yet the only major one without the
  "should I worry?" line. It now opens with the same `page_verdict` pattern, fed by the platform-score
  input mart (query/task failures, warehouse queueing, spill) + stale sources — both mart-backed /
  shell-shared, so it stays cheap under the lazy sections. New pure `verdict.operations_signals`
  (test-locked; thresholds mirror the platform score's penalty onset).
- **[#9] Persistent Case File tray.** Case additions used to vanish into a bottom-of-Brief expander;
  the running count now rides every page in the sidebar (session-only, no query) with a one-click jump
  to open it on Brief.
- **[#16] Read-only badge.** A viewer with no operator entitlement (the READER tier, or any
  non-operator) now sees an explicit "🔒 Read-only" badge in the sidebar instead of a silently trimmed
  surface.

## 4.383.0 - UI review Wave 1: table & metric polish (2026-08-31)

Three table primitives from the Wave-1 set (all in `components.py`, so every table benefits at once).

- **[#22] Magnitude-aware table dollars.** `_USD` / `_PRICE` columns now humanize like the KPI cards
  (cents for small, whole for thousands, `$x.xxM` for millions), so a table figure matches the card of
  the same metric instead of reading `$1,234,567.89` beside a card's `$1.23M`. NaN stays blank; big
  tables keep the `$` via a `$%.2f` large-frame fallback; `df.to_csv` keeps the raw numeric.
- **[#26] Co-pin the status column.** Wide (≥8-col) tables pinned only the identity column, so severity
  scrolled off on a horizontal scroll. The first status/severity column is now pinned alongside it.
- **[#24] Consistent clickable-row hint.** Added a shared `row_select_hint` + an optional `hint=` on
  `selectable_nav_table` / `entity_nav_table`, so drillable tables can announce it the same way instead
  of each page hand-rolling its own wording.

## 4.382.0 - UI review Wave 1: colour-semantics correctness (2026-08-31)

First of the Wave-1 items from the adjudicated UI review — the two that are correctness bugs, not
polish (colour is supposed to carry meaning in this app).

- **[#28] Metric-specific table delta colours.** Table delta cells were hard-coded up = red /
  down = green, so a positive **cache-rate / coverage / verified-savings / throughput** delta was
  wrongly reddened. `delta_css` now takes the column and inverts polarity for good-up metrics
  (`delta_up_is_good`), so a positive good-up delta reads green like a negative cost delta. KPI cards
  were already polarity-aware; only tables lied. CSV export unaffected.
- **[#14] Neutral company scope chip.** The company chip was styled `"ok"` (green success), but
  company is scope *context*, not health — it now renders neutral.

## 4.381.0 - Self-review fixes for the alert additions (2026-08-31)

An adversarial review of the v4.376–v4.380 additions (4 finders + verify) found 3 confirmed issues;
this fixes them. The V117 revision is in place (it was never applied), so the pending set stays
V115–V117.

- **[med] V117 revised from resolve → carry-forward.** The v4.380 sweep *resolved* each day's
  snooze re-raise, leaving a `SNOOZE_SUPPRESSED` row occupying that day's `DEDUPE_KEY`. After a
  **mid-day wake**, the current band couldn't re-mint (key occupied), so the operator saw only the
  **stale-numbers original**. V117 now **carries the snooze forward**: it snoozes the fresh re-raise
  (inheriting the wake time) and resolves the superseded older row, so exactly one snoozed row — the
  latest band, with **current data** — survives and reopens once on wake. `ev.RAISED_AT > s.RAISED_AT`
  also leaves a pre-existing *untriaged* OPEN sibling in the queue for a human (was over-broadly swept).
- **[low] Clear-queue receipt no longer overstates a duplicate.** A same-minute retry hits the
  idempotency key → the proc returns `DUPLICATE` (0 rows) which `execute_action` reports as success;
  the receipt used to still say "cleared N". It now says "No change — already applied moments ago."
- **[low] Comment clarity:** the undelivered-critical anti-join is *deliberately* unbounded in time
  (unlike `route_backlog`'s 7-day send window) — noted so the "as in route_backlog" isn't over-read.
- ⚠ The V117 carry-forward (`UPDATE…FROM` + `TRY_TO_DATE` identity strip) still **needs a live
  smoke-check in SiS** — structurally byte-derived, all shape tests pass, but unverifiable read-only.

## 4.380.0 - V117: snooze-suppress sweep (multi-day snooze finally holds) (2026-08-31)

Last deferred alert-hunt item (#2). A per-event snooze sets `STATUS='SNOOZED'` but keeps the event's
**date-banded** `DEDUPE_KEY` (`RULE|entity|<date>`). The re-raise guard matches the exact key, so when
the day/week band rolled the next day the scan minted a **brand-new OPEN** event for the same
rule+entity despite the snooze — a "1 week" snooze silenced nothing past the first day.

- **V117** re-derives `SP_ALERT_SCAN` from V115 (byte-identical except one new post-raise sweep) that
  resolves any OPEN event whose **band-independent identity** (its `DEDUPE_KEY` minus a trailing
  `|YYYY-MM-DD` token) matches an **active** snooze (`SNOOZED`, wake time in the future) for the same
  rule. The re-raise is minted then resolved *within the scan*, so the separate webhook sender never
  sees the transient OPEN; `RESOLUTION_KIND='SNOOZE_SUPPRESSED'`.
- The trailing-date strip uses `TRY_TO_DATE` (not a regex — avoids Snowflake string-escape ambiguity).
  All date/week bands render as a bare ISO date; **entity-only keys** (IP, grant timestamp) don't, so
  they're never stripped — their snooze already worked and is unchanged.
- `SNOOZE_SUPPRESSED` joins `SUPERSEDED`/`AUTO_CLEARED` as a **machine close** excluded from the
  read-path human-resolution metrics (precision, MTTR, RESOLVED counts) in `mart_sql.py`.
- **Residual (documented):** on the wake scan the reopened stale original and a fresh re-raise can both
  be OPEN for that one scan; a clean fix needs a wake-step change (weekly-band tradeoffs) and is deferred.
- Proc only, no schema change. **Owner applies V117 in Snowsight after V116.**
- ⚠ **Needs a live smoke-check in SiS** — the sweep SQL (correlated `EXISTS` + `TRY_TO_DATE` strip)
  couldn't be validated read-only. It's structurally byte-derived and all shape tests pass.

## 4.379.0 - V116: atomic scope-based alert clear (2026-08-31)

Second deferred alert-hunt item (#4). The drawer/bulk lifecycle already route through
`SP_ALERT_LIFECYCLE` (audit + status change in one transaction, idempotency-keyed). The clear-queue
was the exception — it ran the audit `INSERT` and the status `UPDATE` as two separate auto-committed
statements, so a mid-op failure (transient/lock error) + retry could leave events OPEN with duplicate
`ALERT_AUDIT` rows.

- **V116** adds `SP_ALERT_CLEAR_SCOPE`, which does the audit + status change atomically over a
  `STATUS + company` scope (not enumerated ids, so it still clears past the feed cap) and records the
  idempotency key in `OW_ACTION_INTENTS` so a double-click/retry is a `DUPLICATE` no-op. Mirrors
  `SP_ALERT_SNOOZE` exactly (`BEGIN TRANSACTION … COMMIT`, `EXCEPTION → ROLLBACK`).
- The app calls it via `execute_action()`, with the existing two-statement builder as the pre-V116
  **legacy fallback** — so behaviour is unchanged until V116 is applied, then it becomes atomic.
- New proc only, no schema change. **Owner applies V116 in Snowsight after V115.**

## 4.378.0 - Deferred #6: undelivered-critical is now route-level (2026-08-31)

First of the three deferred alert-hunt items. The undelivered-critical banner (health_strip) and SLO
card (delivery_slo_summary) flagged a CRITICAL as undelivered only when it had **no delivery row at
all** across any route. But `SP_NOTIFY_WEBHOOK` fans a CRITICAL out to every matching route — so a
CRITICAL that reached a sibling info-route but **failed its paging route** had a delivery row and read
**green**, while on-call was never paged.

- Both surfaces now compute undelivered at the **route level**: a CRITICAL is undelivered if an
  **eligible enabled route** (the sender's family/company predicate, mirroring `route_backlog`) has no
  delivery for it. A config-gap safety `UNION` keeps flagging a CRITICAL that matches no route at all
  (it can never be delivered), so the check never flags *fewer* criticals than before.
- Implemented as a JOIN-based anti-join (`und_crit` / `undc` CTEs) — the codebase-safe pattern, not a
  nested correlated subquery. Filtered to OPEN criticals, so it's a cheap subset scan, not a full
  re-scan.
- **Latent on a single-route account** (event-level == route-level with one catch-all route), so this
  is a no-op today and takes effect once a second route with a distinct severity floor exists.
- ⚠ **Needs a live smoke-check in SiS** — the route-level SQL couldn't be validated against Snowflake
  read-only. It compiles and all shape tests pass; confirm the banner/SLO tiles still render once
  deployed.

## 4.377.0 - V115: alert escalation-supersede includes ACK (2026-08-31)

Migration-bearing fix for the alert-hunt's highest-impact confirmed finding (v4.376.0 shipped the
app-only ones). `SP_ALERT_SCAN`'s escalation-supersede sweep (V067 #40) collapses a lower-band alert
when its higher-band sibling exists — but it only fired when **both** sides were `STATUS='OPEN'`.

The open-count convention is `STATUS IN ('OPEN','ACK')` — an acknowledged alert still counts as open.
So an incident that was **acknowledged and then escalated** (e.g. a WARN gets ACKed, burn worsens, a
CRIT raises as a new banded event) was never collapsed: the same incident double-counted as **two open
alerts** and penalized the platform score twice — exactly what the sweep exists to prevent.

- **V115** re-derives `SP_ALERT_SCAN` from V110 (byte-identical except the sweep) and broadens both
  sides of the supersede match to `STATUS IN ('OPEN','ACK')`. Manual `RESOLVED` / active `SNOOZED` stay
  excluded.
- The sibling **auto-clear** sweep stays `OPEN`-only by design — there an ACK means a human is actively
  working the alert, so a below-threshold condition must not auto-close it.
- Proc only, no schema change. **Owner applies V115 in Snowsight after V114**; the next hourly
  `SP_ALERT_SCAN` heals existing double-counts on its next run.

## 4.376.0 - Alert-layer bug hunt: app-side fixes (2026-08-31)

Adversarial hunt across the alert layers (raise/scan, lifecycle, counts, precision/SLO, routing) —
5 finders, each finding adversarially verified. 7 confirmed, 4 refuted. The app-only fixes ship here;
the migration-bearing and higher-risk items are tracked below.

**Fixed (app-only):**
- **[med] Route-failures KPI counted hourly retry rows, not distinct failures.** `SP_NOTIFY_WEBHOOK`
  logs one `route_send_failed` row per failing route *per hourly run*, so one route broken for a month
  read as ~720 "failures" on the History ▸ Delivery-health tile. `delivery_slo_summary` now counts
  distinct `(route, day)` from `CONTEXT` (which carries the route id + integration), matching the
  once-per-day grain of the sibling expired-undelivered metric. Tile help updated.
- **[low] Clear-queue "Acknowledge" receipt overstated what changed.** The receipt/toast reported
  `_open_total` (OPEN+ACK) for both verbs, but ACK moves only the OPEN rows (already-ACK stay ACK) and
  keeps them counting as open — so it overstated the ACK count and wrongly said "cleared". RESOLVE keeps
  the accurate count; ACK now reports the action honestly without a misleading number.
- **[low] Clarified the undelivered-critical banner vs. SLO-card divergence.** health_strip's always-on
  banner is unbounded (correctly still red for a 40d+ stuck critical); `delivery_slo_summary`'s
  `UNDELIVERED_CRITICALS_30M` is a windowed report. They legitimately diverge only past the window — a
  comment now records that so the two aren't mistakenly "reconciled".

**Confirmed, handled separately (not in this release):**
- **[med] `SP_ALERT_SCAN` escalation-supersede skips ACK'd events** → double-counts an
  acked-then-escalated incident as two open alerts. Migration-bearing; shipping as V115.
- **[med] Multi-day snooze defeated by date-banded dedupe** — a per-event snooze keeps the event's
  date-banded `DEDUPE_KEY`, so the next day mints a fresh OPEN despite the snooze. Deferred: needs a
  snooze-aware scan (invasive, all rule blocks) and can't be validated read-only.
- **[med] Undelivered-critical uses event-level "any delivery"** — a CRITICAL delivered to a sibling
  info route but failing its paging route reads green. Latent (no-op on a single-route account);
  deferred pending a per-route anti-join mirroring `route_backlog`.
- **[low] Clear-queue is non-atomic/non-idempotent** — split audit+update statements, no transaction or
  idempotency key; a mid-op failure + retry can duplicate `ALERT_AUDIT` rows. Deferred: needs a
  set-based atomic proc.

## 4.375.0 - Control Room: resolve open incidents in one step (2026-08-31)

Adds an operator control on **Control Room ▸ Incidents & triage** to resolve every open
(OPEN/MITIGATED) incident in the current company scope in one action — the incident-side companion to
the Alerts "clear the open queue", for resetting the board after pre-production validation.

- Fixes the reported issue where, after clearing the alert queue, Brief and Overview still read
  **"Attention needed"**: that verdict is a composite, and its remaining driver was **open incidents**
  (declared/auto-declared from the test alerts) — which clearing *alerts* does not resolve. The alert
  counts themselves were updating correctly; the incident count wasn't being reset because there was no
  bulk path for it.
- The panel uses the uncapped `incident_metrics.OPEN_NOW`, so it covers the whole open set, not just the
  50-row feed. Forward-only (a reopen is still a new incident with `REOPENED_FROM`); the chosen root
  cause + note is stamped on each row exactly as a single close records it. Typed-confirm
  (`CLEAR RESOLVE`), operator-gated, duplicate-click latched.
- Because the `INCIDENTS` write bumps the `incidents` cache domain, Brief and Overview's open-incident
  count — and their page verdict — refresh on the next read. App-only; no migration.

## 4.374.0 - Read-only tier for non-admins (ETL team) (2026-08-31)

Adds a per-viewer **page-visibility** tier so non-admins can see the monitor without any write or
admin access. Same owner's-rights Streamlit-in-Snowflake blind spot as the operator fix: page
visibility was resolved from `CURRENT_ROLE()`, which is the app **owner's** role for every viewer, so
everyone got the owner's full DBA page set. Now `main.py` resolves the profile from the **viewer**
(`st.user`) via the new `session.active_profile()`, mirroring `is_operator()`.

- New **`READER`** profile — Brief, Overview, Control Room, Cost & Contract, Operations, Decision
  Studio, Security. Everything **except Admin, Alerts, and Ask**. Read-only: the ETL team is *not* in
  `OPERATOR_USERS`, so `is_operator()` is False for them and every write control (incl. the Operations
  emergency levers — cancel query, warehouse resize/suspend, scans) stays hidden.
- `VIEWER_PROFILES` maps the 5 admins (`H21427`, `E22292`, `KEBARR1`, `CLROY`, `N22514`) → `DBA` and
  the 4 ETL users (`GRTHOMP1`, `SUDEVAX`, `TV5073`, `VS4229`) → `READER`. Any identified-but-unmapped
  SiS viewer falls to `READER` (least-privilege), **never** the owner's DBA.
- `OPERATOR_USERS` extended from just `H21427` to all 5 admins (write entitlement — a separate axis
  from page visibility).
- Defense-in-depth: the two `state.py` nav clamps (jump/saved-view/deep-link) now use `active_profile`
  (they were inert on SiS), and `main.py` adds an explicit re-clamp right before render dispatch so a
  page outside the viewer's set can never reach a renderer. `is_sis()` fails an unresolved-identity SiS
  viewer *closed* to `READER` rather than inheriting the owner's DBA surface. Config-only; deploys with
  an app code redeploy + restart (no Snowflake migration).

## 4.373.0 - Entitle the first operator (2026-08-31)

Adds the account owner's Snowflake username (`H21427`) to `OPERATOR_USERS`. Under owner's-rights
Streamlit-in-Snowflake every viewer executes as the app owner's role, so operator gating keys on the
**viewer's username** (`st.user.user_name`), never `CURRENT_ROLE()` — the allowlist had shipped empty
(secure default), which meant *no one* was entitled and every operator write control (ack/resolve, the
new clear-the-queue panel, savings verification, incident declare, snooze, settings) was hidden for
everyone. This is a config-only change; RBAC in Snowflake remains the real server-side boundary.

## 4.372.0 - Alerts: one-click "clear the open queue" (2026-08-30)

Adds an operator-only bulk action on **Alerts ▸ Open events** to ack or resolve *every* open event in
the current company scope in one step — for resetting the queue after validating that alerts flow
correctly, rather than ticking hundreds of checkboxes (the existing bulk panel only acts on selected
rows, which the ~500-row feed cap can't cover).

- A collapsed **🧹 Clear the open queue** expander shows the true open count and severity mix in scope
  (from the same `open_alert_severity_counts` the KPI tiles use, so it clears exactly what they show).
- **Resolve** (zeroes the open counts) or **Acknowledge** (marks seen — an ACK event still counts as
  open). Resolve defaults to an **untagged** resolution, which drops out of the per-rule precision
  score — the honest choice for a setup/validation clear, so test alerts don't skew the proof metrics;
  the three real kinds (ACTIONED/NOISE/EXPECTED) remain selectable.
- Operator-gated, typed-confirm (`CLEAR RESOLVE` / `CLEAR ACK`) showing the affected count, and every
  change is written to `ALERT_AUDIT`. Matched by `STATUS` + company (not enumerated event ids), so it
  clears the whole queue past the feed cap; snoozed events are deliberately left alone.

## 4.371.0 - Data-loader/ETL bug hunt: 2 app-side fixes + V113 + V114 (2026-08-30)

Adversarial pass over the data-loader/ETL layer (6 finders: incremental merge/dedup, window/watermark,
mart-vs-reader derivation, metering arms, loader robustness, schedule/freshness). Eight surfaced, six
confirmed, two refuted (FACT_AI_USAGE freshness staleness and TASK_INCIDENT_AUTODECLARE chaining are
both working as designed). Four fixes ship here; three are deferred with a documented rationale.

- **[MED] backfill_365 collapses task auto-retries** — the one-time year backfill's `FACT_TASK_DAILY`
  arm aggregated raw `TASK_HISTORY` with no terminal-attempt dedup, while the standing loader
  `SP_LOAD_DAILY_FACTS` was fixed to collapse retries in V101. So a task that failed then succeeded on
  an auto-retry was backfilled as `RUNS=2 / FAILED=1` (phantom failure) on historical days while the
  trailing V101-loaded days showed `RUNS=1 / FAILED=0` — a drift across the day boundary that inflated
  YoY task-failure trends. The backfill now applies the same terminal-attempt `QUALIFY` CTE. (Repo
  script; not a numbered migration.)
- **[LOW] ops_diag mart readers are day-aligned** — `ops_diag_top_queries` / `ops_diag_failures`
  windowed on the rolling `CURRENT_TIMESTAMP()` while their live twins use the day-aligned
  `CURRENT_DATE()`, so the mart-first path covered a shorter window than the live fallback. Aligned to
  `CURRENT_DATE()` (as `role_hourly` / `schema_window_summary` already are).
- **V113 — [MED] incident-timeline TASK_FAIL uses COMPLETED_TIME** (deferred from the incident hunt):
  the `MART_INCIDENT_TIMELINE` TASK_FAIL arm in `SP_LOAD_MARTS_V27` selected and bounded on
  `QUERY_START_TIME`, but the live reader `incident_timeline` uses `COMPLETED_TIME`, so the same task
  failure appeared at different instants on the 48h live vs 7d mart correlation-timeline paths (shifted
  by the run duration), which can invert cause/effect ordering. Re-derived from V103 with the arm on
  `COMPLETED_TIME` (the failure's instant, matching the reader).
- **V114 — [LOW] anomaly sweep runs after the daily loader** — `TASK_ANOMALY_SWEEP` fired at 06:40,
  five minutes *before* `TASK_LOAD_DAILY` (06:45), so `SP_ANOMALY_SWEEP`'s account/service arm scanned
  yesterday's `FACT_METERING_DAILY` and detected a credit spike a day late. Cron moved to 07:00.

**Deferred (documented, for a focused follow-up):**
- **[MED] `SP_LOAD_APP_COST` / [LOW] `SP_LOAD_STORAGE_TRUTH` DELETE+INSERT transaction-wrapping** — a
  mid-INSERT failure erases the reloaded window and leaves the fact empty until the next scheduled run
  repopulates it. The correct fix (wrap in `BEGIN TRANSACTION … COMMIT` with `EXCEPTION … ROLLBACK`,
  mirroring `SP_LOAD_OBJECT_COST`) introduces new control-flow SQL that CI cannot validate, so it
  warrants a tested pass; the tables self-heal on the next load in the meantime.
- **[MED] warehouse-efficiency `IDLE_PCT` hour-vs-credit weighting** — the mart stores an hour-count
  idle fraction while the live sizing/idle twins compute a credit-weighted one, so a multi-cluster
  warehouse's right-sizing verdict can flip mart-first vs live. The correct fix stores a
  credit-weighted idle column in the mart (a coordinated schema + loader + reader change) and is
  deferred to its own pass; both current values are bounded idle-share estimates.

## 4.370.0 - Cross-surface reconciliation audit: 3 app-side fixes (2026-08-30)

Cross-surface reconciliation audit (6 finders pairing surfaces that should agree). Eight surfaced,
six confirmed (three distinct), two refuted (the Decision Studio "two verified-savings" figures are
the deliberate QTD-vs-all-time pair; the Operations lock-wait mart/live scope is a documented
account-wide fallback). All three app-side.

- **[MED] Brief "Open incidents" reads the true open count** — the Brief KPI was `len()` of the
  `open_incidents(5)` feed (shared with the detail below it), so it saturated at 5 while Control Room
  showed the true count for >5 open incidents. It now reads the uncapped `incident_metrics.OPEN_NOW`
  (the same builder Control Room uses), added to the Brief's existing prefetch batch; the LIMIT-5 feed
  is kept only for the row detail.
- **[MED] Overview spend help no longer over-promises reconciliation** — the flagship "Spend, {window}
  ({company})" tile sums the full selected window, but Cost ▸ By warehouse (exact usage) clamps to its
  182-day vs-prior half-window, so at the 365-day / current-year selection the two legitimately differ
  (~2×). The Overview help claimed unconditional reconciliation with that Cost table; it now states the
  reconciliation holds up to 182 days and explains the clamp beyond that. (Each number is correct for
  its stated window; only the claim was wrong.)
- **[LOW] Brief vs Overview MTD credit spend blend from the same precision** — the Brief blends
  `health_strip`'s MTD credit partitions, which were rounded to whole credits *before* the blend, while
  Overview blends raw credit sums, so the two "MTD credit spend (account)" headlines could differ by up
  to ~$3 on small/dev accounts (where cents render). `health_strip`'s `MTD_AI`/`MTD_OTHER` are now raw,
  rounded only at the display edge; `MTD_ALL` stays whole for its credit-count display.

## 4.369.0 - Incident-layer bug hunt: 5 app-side fixes (2026-08-30)

Adversarial pass over the incident-management layer (5 finders: auto-declare, membership/lineage, TTM/
timeline, RCA/routing, narrative/UI). Nine surfaced, seven confirmed (six distinct), two refuted (the
V099 auto-declare guard is company-scoped by design; the RCA 20% entity-match weight is not inert).
Five land app-side here; the sixth (incident-timeline TASK_FAIL timestamp parity, a `MART_INCIDENT_
TIMELINE` loader fix) is migration-bearing and ships in the migrations release.

- **[MED] manual incident-declare guard is now entity-aware** — V072 makes `INCIDENT_PROPOSALS`
  entity-aware (one proposal per family/company/entity) and the members-insert scopes to the chosen
  entity, but the family-already-open guard correlated on family + company only. So once any incident
  of a family was open, declaring a *distinct* entity's incident in that family made the INCIDENTS
  insert a 0-row no-op — and `execute_statement` returns OK for a 0-row insert, so the UI reported
  "Incident declared with members linked" while nothing was created and the second entity's alerts
  stayed orphaned. The guard now carries the same entity predicate the members-insert uses.
- **[MED] RCA auto-investigation task-failure feed covers the onset window** — the incident RCA
  synthesis asks each feed for the incident's onset-covering window (up to 30 days), but
  `task_failure_details` clamped to 14 days, so an onset-time task failure older than 14 days was
  silently dropped from the ranked cause while the sibling change/grant/warehouse feeds covered it.
  Cap raised to 30 (SCHEDULED_TIME pruning keeps the scan bounded; the Operations 7-day caller is
  unaffected).
- **[LOW] spend collapses are no longer candidate causes** — `candidates_from_anomalies` used `abs(z)`,
  so a spend *collapse* (z < 0 — a downstream effect, e.g. a stopped pipeline) entered RCA as a
  full-magnitude candidate and could headline as a high-confidence "cause". Non-positive-z anomalies
  are now excluded (only spikes are cause candidates).
- **[LOW] incident Gantt "now" line** — the projected-now reference line was derived from
  `ENDED.max()`, which with no open incident is the latest *past* resolution, dropping the line into
  the past. It now anchors to `account_now()` passed from the caller.
- **[LOW] blast-radius observed-consumer half surfaces truncation** — the observed-consumer fetch was
  silently LIMIT-capped (200), so the measured-vs-unmeasured split over-stated "unmeasured" at the cap.
  Raised to 500 and the "Observed in last 30d" KPI now shows "(lower bound)" at the cap, mirroring the
  Declared-dependents truncation note.

## 4.368.0 - Alerting-layer bug hunt: V110 + V111 + V112 (2026-08-30)

Adversarial pass over the alerting layer (6 finders: rule thresholds, dedupe/refire/snooze, routing/
delivery, severity/escalation/self-alert, alert readers/workflow, security arms). Thirteen surfaced,
nine confirmed (deduping to six distinct bugs), four refuted (the route COMPANY_FILTER uses a
controlled two-value vocabulary so there's no organic case divergence; the drawer history count is
cosmetic; `SEC_BREAK_GLASS_USE` is a retired/deleted config row so its arm is unreachable; the egress
14-day average is honestly labeled). All six are migration-bearing, shipped as three owner-gated procs.

- **V110 — four `SP_ALERT_SCAN` fixes (re-derived from V107):**
  - **[MED] `SEC_NEW_ADMIN_NETWORK` now watches the built-in `ACCOUNTADMIN`** — the arm built its
    watched-admin population from `ROLE IN ('SNOW_ACCOUNTADMINS','SNOW_SYSADMINS')`, omitting the
    built-in `ACCOUNTADMIN`, so a user granted `ACCOUNTADMIN` *directly* (a pattern the
    `BREAKGLASS_GRANTS_30D` posture metric tracks) was absent from the join and their first login from
    a new IP fired no alert — a false all-clear on the account's top-privilege credential. Added
    `ACCOUNTADMIN` to the role set (the canonical set every sibling path uses).
  - **[MED] cred-expiry EXPIRING→EXPIRED supersede is no longer a no-op** — V104 made the
    `SEC_CRED_EXPIRY` dedupe band a *terminal* token (`…|NAME|EXPIRING`), but the escalation-supersede
    sweep still matched `'|EXPIRING|'` (trailing pipe), which never occurs, so an expired credential
    stranded a stale EXPIRING event beside the new EXPIRED one forever. Now matches the terminal token.
  - **[LOW] contract-breach CRIT/WARN→EXHAUSTED supersede** — V108 added the EXHAUSTED band without a
    matching supersede arm, so a contract escalating to exhausted left the stale CRIT event open. Added
    `|CRIT|→|EXH|` and `|WARN|→|EXH|` arms.
  - **[LOW] `PERF_QUERY_FAIL_PCT` DETAIL** interpolated the editable `WINDOW_HOURS` but the counts are
    over a hardcoded 24h window; the DETAIL now hardcodes "in last 24h" to match the aggregation.
- **V111 — [LOW] `COST_BUDGET_PACE` completed-days pace window (`SP_ALERT_SCAN_DAILY`, from V108):**
  the [08] account budget-pace allowance counted today as a fully elapsed day while `MTD_USD` covers
  only completed days, understating the pace ratio by ~1/D and letting a real early-month overspend
  stay silent — the account-level sibling of the V107 department-pace fix. Allowance now uses
  `(DAY_OF_MONTH − 1) / DAYS_IN_MONTH` with a day-1 guard; `MTD_USD` stays month-to-date for the
  forecast arm.
- **V112 — [LOW] daily digest skips paging routes (`SP_DAILY_DIGEST`, from V070):**
  `DELIVER_DIGEST` defaults TRUE and the digest cursor had no severity filter, so a paging route added
  via the documented CRITICAL → PagerDuty recipe would receive the executive morning digest (paging
  on-call for a non-incident). Snowflake can't `ALTER` a column default to a literal, so the cursor now
  also excludes CRITICAL-only routes.

## 4.367.0 - Operations-layer bug hunt #4: 2 app-side fixes + V109 (2026-08-30)

Fourth adversarial pass over the operations layer (6 finders: query performance/pruning, task/pipeline
health, warehouse concurrency, lock contention, data-quality/volume, change-impact). Four surfaced,
three confirmed, one refuted (V027's dead `'FAILED'` mart token was already re-derived by V057). Two
land app-side, one is an owner-gated migration (V109).

- **RCA no longer pages for auto-retried tasks that ultimately SUCCEEDED (MED)** — `task_failure_details`
  filtered `STATE = 'FAILED'` in the WHERE clause with no terminal-attempt dedup, while every sibling
  task-outcome builder (`task_runs`, `task_recent_states`, `release_task_compare`, …) collapses retries
  to the terminal attempt. So a task with `TASK_AUTO_RETRY_ATTEMPTS > 0` that flapped on attempt 1 then
  succeeded surfaced its FAILED attempt as a standalone root-cause — inflating the "Failures (7d)" KPI,
  painting the header alarm, and routing a phantom incident to the owner for a run whose final state
  was SUCCEEDED. Now keeps only the terminal attempt (`QUALIFY ROW_NUMBER() … PARTITION BY db,schema,
  name,scheduled_time ORDER BY completed_time DESC`) before filtering FAILED.
- **Pipeline SLA forecast requires a trustworthy cadence (MED)** — `pipeline_sla_forecast` treated any
  non-null median refresh gap as the table's "typical cadence", ignoring the `REFRESHES` interval count
  the builder already carries, so a table with a single observed gap (e.g. a one-time backfill burst of
  two DML events) got a High "Overdue" leading-indicator even when it was comfortably within SLA. Now
  gated on `REFRESHES >= 3` (mirroring `task_freshness_sla`'s `INTERVALS >= 3`); sparse tables fall back
  to the runway-proximity check.
- **V109 (owner-gated): warehouse change-scan failure axis uses the real status domain (MED)** —
  `SP_WAREHOUSE_CHANGE_SCAN` (defined only in V024, never re-derived) counted query failures with
  `COUNT_IF(EXECUTION_STATUS = 'FAILED')`, but `QUERY_HISTORY.EXECUTION_STATUS` is
  `SUCCESS`/`FAIL`/`INCIDENT` — never `FAILED` — so `BASELINE_FAIL_PCT` and `AFTER_FAIL_PCT` were a
  constant 0 and the post-change regression fail axis (`AFTER >= BASELINE + 5`) could never fire: a
  setting change that broke a warehouse's queries read a false all-clear (`fail 0->0%`). V109 re-derives
  the proc from V024 with `COUNT_IF(EXECUTION_STATUS <> 'SUCCESS')` on both arms (the same dead token
  V057 fixed in `SP_LOAD_MARTS_V27`); byte-identical otherwise, no schema change, no task re-creation.
  Owner applies after V108.

## 4.366.0 - Decision Studio bug hunt #2: 5 app-side fixes (2026-08-30)

Second adversarial pass over the Decision Studio layer (6 finders: portfolio/prioritization,
experiments, cost-truth, SLO editor/config, action-verify/triage, narrative round 2). Nine surfaced,
five confirmed, four refuted (the experiment-detail notes render in the master table; the error-budget
dual-signal is deliberate SRE practice; the triage severity path is unreachable given
`ALERT_EVENTS.SEVERITY NOT NULL` plus canonical seeds; and the "Pays for itself" ratio is genuinely
horizon-consistent). All five fixes are app-side — no migration.

- **"Needs validation" KPI counts the VALIDATE lane, not a re-derived confidence threshold (MED)** —
  the Portfolio exception panel derived its tally as `portfolio[CONFIDENCE < 0.5]`, but
  `prioritize_workloads` also forces `LANE='VALIDATE'` for families with no behavioral evidence
  (`~has_behavior`), whose run/day/cost `CONFIDENCE` is routinely ≥ 0.5. Those blind-but-costly
  families — exactly what the KPI should size — were silently omitted, undercounting versus the table
  and the per-lane cost subtotal. Now counts `LANE == 'VALIDATE'`.
- **Experiments "Verified"/"Verified value" read an uncapped aggregate (MED)** — `_experiments`
  computed the Verified count and dollar headline over the LIMIT-300 display frame, so once an account
  held > 300 experiments the oldest settled VERIFIED rows (which sort past the active-first cap) fell
  off both totals silently, contradicting the canonical ledger. A new uncapped
  `experiment_verified_totals()` aggregate now backs those two headlines; the browsable table keeps its
  300-row cap.
- **Cost Truth MEASURED excludes the UNATTRIBUTED residual (MED)** — the MEASURED basis summed
  `FACT_OBJECT_COST_DAILY` where `COST_ARM LIKE 'QUERY_COMPUTE%'`, which also matched the synthetic
  `QUERY_COMPUTE_RESIDUAL` row (query compute that touched no base object, `COMPANY='UNKNOWN'`). That
  overstated the "Object-attributed" coverage and, because the residual's company is UNKNOWN, made the
  basis scope-variant (kept on ALL, dropped per-company) so per-company slices didn't reconcile to the
  ALL total. Now excludes `OBJECT_FQN='UNATTRIBUTED'`, matching `object_cost_top`.
- **SUCCESS_PCT SLO target is capped at 100 (LOW)** — the target `number_input` had no `max_value`
  (unlike the sibling error-budget input), and `create_slo_objective_sql` wrote it unclamped. A
  fat-fingered target > 100 on a percentage metric can never be met (`CURRENT_VALUE ≤ 100`), producing
  a permanent, un-clearable false BREACH on a healthy warehouse. The UI now caps success targets at 100
  and the builder clamps a `*_SUCCESS_PCT` target to `[0, 100]` defensively.
- **Acceptance funnel top counts all booked-in-window (LOW)** — the funnel's top term
  (`SAVINGS_ESTIMATED`) counted only rows in the current `STATE='ESTIMATED'` snapshot, disjoint from
  verified/rejected, so a "N estimated → M verified" funnel could show verified exceeding estimated.
  Every ledger item enters as an estimate, so the top now counts all items booked in the window.

## 4.365.0 - Decision Studio bug hunt #1: 5 app-side fixes (2026-08-30)

First adversarial pass over the Decision Studio layer (6 finders: action-queue economics, ROI/
realization/proof, scenario projection, DS scoping/freshness, ranking/surfacing, narrative
consistency). Six findings confirmed, zero refuted (two finders independently surfaced the same
`slo_summary` stale-burn bug). All five distinct fixes are app-side — no migration.

- **Proof verdict surfaces the "precision not trustworthy" caveat (MED)** — `proof_verdict` composes
  its four low-signal checks (ROI, realization, precision, acceptance) by appending a reason *and*
  setting `level='watch'`, but the high-unlabeled-share check appended its reason without the
  `level='watch'`. Because the green "good" headline is built from `bits` (which cites the precision
  number) and never renders `reasons`, a high unlabeled share alone left the flagship verdict
  headlining the precision figure as proof while silently dropping the caveat that the precision
  itself isn't trustworthy. It now downgrades to `watch` like every other signal, so the caveat
  reaches the one-line go/no-go verdict.
- **SLO worst-burn alarm respects the STALE verdict-withholding (MED)** — `slo_summary` computed
  `worst_burn` and `has_burn` over *all* objectives, but the cockpit SQL still emits a stale
  objective's last-known `BURN_MULTIPLE` even though its MET/BREACH verdict is deliberately withheld
  (>2-day-old evidence). So a stalled loader whose last reading was below target fired a red "error-
  budget breach" reliability alarm off evidence the same panel labels STALE and refuses to judge —
  sending on-call to chase a phantom breach. `worst_burn`/`has_burn` are now scoped to MET/BREACH
  rows only.
- **"Consumer reach" is labeled honestly (MED)** — the DS ▸ Products KPI summed each product's
  `DISTINCT_CONSUMERS` (a per-product `COUNT(DISTINCT USER_NAME)`) and labeled the result "Consumers
  served — distinct accounts", but summing per-product distinct counts double-counts any account that
  reads more than one product (3 products × the same 100 analysts → "300"). Relabeled to "Consumer
  reach" with help stating it is the sum of each product's distinct readers (can exceed the count of
  unique accounts); `$/consumer`, computed per-product, was already correct and is unaffected.
- **Scenario projection distinguishes "unpriced" from "$0.00" (LOW)** — `_scenarios` gated its dollar
  KPIs on `candidates > 0`, so a queue of eligible-but-unpriced actions (security decisions carry no
  dollar estimate) rendered "Gross authored estimate: $0.00 / Expected capture: $0.00" — reading as
  "worth nothing" when the dollars are unquantified. The KPIs now show "Unpriced" when candidates
  exist but the gross estimate is zero.
- **Scenario projection discloses the 500-action fetch cap (LOW)** — `_scenarios` projects from
  `action_center(..., 500)` but showed no cap banner, so a >500-action queue's projection under-counted
  silently (the sibling Portfolio board already discloses its cap). A caption now fires at the cap.

## 4.364.0 - Cost-layer bug hunt #6: V108 (contract-breach false all-clear) (2026-08-30)

Sixth adversarial pass over the cost layer, this round aimed at the least-swept slices (storage
lifecycle, contract/commitment pacing, forecast math, cloud-services rebate, reader-scoping matrix,
cost-alert rules). Five of six finders came back clean; one confirmed finding, zero refuted — the
layer is well-hardened after five prior passes.

- **V108 (owner-gated): COST_CONTRACT_BREACH fires once the contract is already exhausted (MED)** —
  the [16] arm of `SP_ALERT_SCAN_DAILY` projected `DAYS_LEFT = CEIL((CONTRACT_CREDITS − CONSUMED) /
  trailing-30-day burn)` and fired only when `DAYS_LEFT BETWEEN 0 AND THRESHOLD_NUM`. As soon as
  `CONSUMED` crossed `CONTRACT_CREDITS`, `DAYS_LEFT` went negative and fell outside the band, so the
  alert went **permanently silent in the over-contract state** — the most expensive one, billing
  on-demand overage at premium rates — and no other scan proc covered the gap. (Not the deferred
  ACCOUNT_USAGE-lag class; a pure logic guard.) V108 re-derives `SP_ALERT_SCAN_DAILY` from V079 with
  the guard relaxed to `DAYS_LEFT <= THRESHOLD_NUM` so the over-contract state (`DAYS_LEFT <= 0`) also
  fires, with a distinct `Contract EXHAUSTED: N credits over` CRITICAL title/metric and an `EXHAUSTED`
  dedupe band so the WARN → CRIT → EXHAUSTED crossings each re-fire. A healthy account far from breach
  (`DAYS_LEFT` large-positive `> THRESHOLD_NUM`) still does not fire; the `p.TOTAL > 0 AND
  p.DAILY_BURN > 0` gates and every other arm are byte-identical. Proc only, no schema change; owner
  applies after V107.

## 4.363.0 - Cost-layer bug hunt #5: 3 app-side fixes + V107 (2026-08-30)

Fifth adversarial pass over the cost layer (6 finders: credit/USD math, chargeback/budget,
warehouse/serverless/storage, savings/ROI ledger, cortex/AI cost, cost-UI consistency). Six
findings confirmed, two refuted (the Overview vs-prior delta is a documented, per-window-labeled
choice; the action-queue CPR de-overlap preserves its documented "queued set = scope total once"
invariant). Three land app-side, two are one owner-gated migration (V107); one is deferred.

- **Wasted-spend total rounds once, not per row (LOW)** — Operations ▸ wasted-spend scan built
  `WASTED_USD` as `round(credits_to_usd(c, rate, round_cents=False), 2)` per fingerprint (defeating
  the internal cent-rounding, then re-imposing it), and summed that already-rounded column for the
  "Wasted spend" and "Monthly-ized" KPIs — the round-then-sum path `credits_to_usd`'s own docstring
  warns against, which zeroes fractional-cent allocations. Now sums the unrounded per-row USD and
  rounds once at the KPI edge; the table cell keeps per-row cents.
- **Exact-usage window is disclosed at the served cap, not the mart cap (MED)** — Cost ▸ Spend &
  Attribution ▸ "By warehouse (exact usage)" falls back to a 90-day live builder when the mart is
  not loaded, but the disclosure caption computed the window from `resolve_effective_window(days)`
  (the mart's 182-day cap) unconditionally, so a 90-day live table under a 365-day page scope was
  labeled "last 182 days". The caption now derives its window from the actual served cap
  (`MAX_LIVE_WINDOW_DAYS` when the live fallback served), mirroring the adjacent allocation-pool
  re-derivation.
- **ROI economics read the whole ledger, not the newest 500 rows (MED)** — `savings_ledger()` capped
  at `LIMIT 500 ORDER BY CREATED_AT DESC`, and Decision Studio ▸ ROI and the Scorecard fed that
  capped frame into `ledger_totals` / `savings_by_month` / `savings_by_lever`. So "Verified savings
  (all time)", realization, the 12-month run-rate, and the page's own "Verified this quarter" were
  computed over only the 500 newest-*created* rows — the total silently *shrank* as the ledger grew,
  and the ROI page's QTD disagreed with the uncapped `savings_summary_quarter` mart the Brief and
  Scorecard cite for the same quarter. `savings_ledger(limit=None)` now serves the full ledger to the
  economics reads (the browsable detail table keeps its 500-row cap); the ledger's QTD is a full-table
  `VERIFIED_AT >= quarter-start` sum, so it can no longer diverge from the mart.
- **V107 (owner-gated): COST_DEPT_BUDGET_PACE department join + pace window (MED + LOW)** — two fixes
  in the same `SP_ALERT_SCAN` [17] arm. (1) The department join `m.DEPARTMENT = b.DEPARTMENT` was
  case-sensitive on a free-text string written verbatim on both sides, so a case drift ('Etl' vs
  'ETL') made the join miss, folded `MTD_USD` to 0, and the rule silently never fired while Cost ▸
  Chargeback showed the department over budget — the sibling of the warehouse-name case-fold V106
  fixed, left on the other join key. (2) `MTD_USD` summed today's partial row while `TIME_SHARE`
  counted today as fully elapsed, understating early-month `OVER_PCT` (a 2×-pace department read
  on-pace on day 2). V107 re-derives `SP_ALERT_SCAN` from V106 with `UPPER(m.DEPARTMENT) =
  UPPER(b.DEPARTMENT)` and completed-days-only pace math (`f.DAY < CURRENT_DATE()`, `TIME_SHARE =
  (DAY(CURRENT_DATE()) - 1) / DAY(LAST_DAY(...))`); byte-identical otherwise. Proc only, no schema
  change; owner applies after V106.
- **Deferred (LOW, analyzed): cortex 30d-projection short-window over-projection.** The projection
  divides credits spanning `days`+1 inclusive calendar dates (including today's partial row) by a
  divisor capped at `days`, over-projecting the run-rate in the short calendar-month presets. Not
  shipped: the "up to 1.5×" case assumes today is a *full* day of data, but cortex is ACCOUNT_USAGE-
  sourced with hours of lag, so today's row is typically empty — and when today is empty the current
  divisor is *exact*. A naive `days+1` fix would regress that common case; the only robust fix
  (fractional-day divisor) adds wall-clock coupling disproportionate to a guarded, display-only KPI.

## 4.362.0 - Cost-layer bug hunt #4: 5 app-side fixes + V106 (2026-08-30)

Fourth adversarial pass over the cost layer (finders: attribution/tagging, chargeback/budget,
warehouse/credit math, ROI/verified-savings, self-cost/mart-vs-live, storage/egress/replication +
serverless). Storage/egress/replication and the serverless dimension came back clean. Six findings
confirmed (0 refuted); five land app-side now, one is an owner-gated migration (V106).

- **Short-window Cortex breach no longer reads as a false all-clear (MED)** — `enrich_user_rollup`
  clamps `OBSERVABLE_DAYS` to `[1, window_days]` to keep the per-day *rate* denominator honest, but
  `classify_exceptions` / `rollup_summary` then reused that *clamped* number as the min-*tenure* trust
  gate (`>= 4 observable days`). On a short window — the "Current month" preset early in a month
  resolves to `window_days = 2` or `3` — `OBSERVABLE_DAYS` is clamped small for *everyone*, so the
  gate tripped for genuinely long-tenured heavy spenders too and skipped them, blanking the whole
  scope into a false all-clear during a live breach. Split the two concerns: a new uncapped
  `TENURE_DAYS` (true days-since-first-usage, floor 1) drives the min-tenure gate; `OBSERVABLE_DAYS`
  (still window-clamped) stays the rate denominator. A veteran heavy spender viewed through a short
  window is now evaluated against the budget ladder instead of mistaken for a first-day user, and the
  per-day projection is unchanged. `TENURE_DAYS` falls back to `OBSERVABLE_DAYS` for old-shape frames.
- **Per-object top-N drops the synthetic residual row (MED)** — `object_cost_top` ranked objects by
  attributed credits but included the `UNATTRIBUTED` residual bucket, which on an account with heavy
  un-attributable spend could occupy a top-N slot and push a real object off the list. Excluded
  `OBJECT_FQN <> 'UNATTRIBUTED'` from the ranked query (the residual is still surfaced separately as
  coverage), so the "top objects by cost" list is all real objects.
- **Tag-coverage + untagged-executions now scope by user, not warehouse (MED)** — `tag_coverage` and
  `untagged_executions_for_user` filtered the query population with `warehouse_clause(company)`, but
  these read `QUERY_HISTORY` keyed on `USER_NAME`; a company whose users run on shared/other-company
  warehouses had its tag coverage computed over the wrong population. Switched both to
  `user_clause(company, "USER_NAME")` (= `COMPANY_FOR_USER(USER_NAME) = 'X'`), matching how every
  other user-keyed cost surface scopes.
- **ROI "pays for itself" compares same horizons (MED)** — the verified-savings numerator is a
  monthly-magnitude rate, but the denominator (`app_cost_quarter`) summed quarter-to-date app run
  cost, so early in a quarter the multiple was inflated (tiny QTD denominator) and late in a quarter
  deflated. Renamed the builder `app_cost_last_30d` (trailing 30 complete days, alias
  `APP_CREDITS_30D`) so both sides of the ratio use a 30-day/monthly window. Brief + Decision Studio
  labels and help updated to say so.
- **Verified-savings-by-month drops the partial current month (MED)** — `savings_by_month` grouped
  realized savings by calendar month including the in-progress current month, so the most recent bar
  was always a partial-month undercount read as a month-over-month drop. Excluded the current
  `YYYY-MM` before `.tail()`, so every plotted month is complete.
- **V106 (owner-gated): COST_DEPT_BUDGET_PACE alert join is case-insensitive (LOW)** — the [17] arm
  of `SP_ALERT_SCAN` joined `FACT_WAREHOUSE_DAILY` to `DEPARTMENT_MAP` with a one-sided fold
  (`f.WAREHOUSE_NAME = UPPER(m.NAME)`). Because the fact preserves the raw case of a quoted mixed-case
  warehouse identifier, such a warehouse failed the join, folded `MTD_USD` to 0 via `COALESCE`, and
  the department never tripped the over-budget gates — while the Cost > Chargeback screen (which
  uppercases both sides) showed it over budget. V106 re-derives `SP_ALERT_SCAN` from V104 with
  `UPPER(f.WAREHOUSE_NAME) = UPPER(m.NAME)`; everything else byte-identical. Proc only, no schema
  change; owner applies after V105 and the next hourly `SP_ALERT_SCAN` heals it.

## 4.361.0 - Operations-layer bug hunt #3: 7 app-side fixes (2026-08-30)

Third, deeper adversarial pass over the operations layer (6 finders: query-opt/pruning, lock
contention, task freshness/SLA/streaks, warehouse concurrency/cache, volume/DQ/adaptive, change-
impact/wiring). Two candidates refuted (the repeat-query cache-basis was already fixed by the
v4.360.0 live-route; a triage mis-rank is unreachable since a >50GB scan implies >100 partitions on
internal tables). Seven app-side fixes, no migration.

- **Lock-wait "spike" no longer double-counts today (MED)** — `lock_wait_spikes` summed *yesterday +
  today* into "last day" (`c.DAY >= today-1`) but compared it to a strictly per-single-day baseline,
  inflating the ratio/count up to ~2× and firing false spikes on later-in-day views. Now `= today-1`
  (the newest complete day), matching the baseline grain.
- **Task freshness stops false-alarming on scheduled idle (MED)** — a weekday-only or business-hours
  cron was flagged Stale/High every Monday / every night because silence was judged against 2× the
  *median* gap while its real gaps include the weekend/overnight idle. It now judges against the p90
  *longest normal gap*; uniform-cadence tasks are unchanged, and a task silent past even its longest
  scheduled gap still fires.
- **Result-cache hit % is account-wide (MED)** — a zero-scan answer has `WAREHOUSE_NAME = NULL`, so a
  warehouse-company scope dropped the entire numerator and collapsed the hit % to ~0 under any
  company filter. Computed account-wide, matching the metric's nature.
- **Volume-drop stops false-FAILING weekday tables (MED)** — a Mon-Fri table read 100% FAILED every
  Monday (yesterday = Sunday = 0 rows). It now suppresses the alarm when the same weekday last week
  was also empty (the table doesn't load that day); a genuine stop on a normally-active weekday still
  fires.
- **proc-SLA rollup surfaces fully-broken procs (LOW)** — a 100%-failing proc had `P95 = NULL` so its
  `CALLS × P95` rank collapsed to 0 and it was truncated out of the very panel meant to show it; it
  now ranks first.
- **DQ row-volume caps tables, not rows (LOW)** — a `LIMIT 8000` on the per-(table, day) series cut
  alphabetically-late tables out entirely (a silent clean all-clear for them); it now caps distinct
  tables so each keeps its complete series.
- **Change-impact "Changes tracked" count is untruncated (LOW)** — the KPI read `len(df)` off a
  `LIMIT 200` frame; it now reads a `COUNT(*) OVER ()` window total, with a "showing latest 200"
  caption.

## 4.360.0 - Repeat-query panel routed to the live builder (2026-08-30)

Resolves the two `family_repeat_fingerprints` mart-vs-live divergences deferred from cost hunt #3.
Ground-truthing changed the fix: the repeat-query / materialization-candidate panel is a toggle-gated
opt-in scan, the live builder (`insights_sql.repeat_query_fingerprints`) is definitionally correct
(bytes-weighted cache excluding result-cache runs; `SUCCESS`/`SELECT`/non-OVERWATCH population) *and*
uniquely carries the priced Avoidable-$ column (the family mart has no size grain), and the mart
can't be filtered because its other reader (`family_compile_heavy`) needs the broad population. So
the panel now calls the live builder directly instead of mart-first — a smaller, strictly-more-
correct fix than the planned schema migration, with no owner-gated apply.

- The default (mart) rendering had counted every result-cache run as 0% cache, dragging well-cached
  families onto the ≤25% materialization gate as false candidates; and had included failed /
  non-SELECT / OVERWATCH-tagged queries the live filter excludes, inflating the "Compute in repeats"
  hours and the candidate count. Both are gone — the panel is now always the correct live scan, and
  the Avoidable $/30d column always renders.
- Window note: this panel's max scan is now the live builder's ~90-day cap (was 400 via the mart) —
  appropriate for "is this a current materialization opportunity", and the mart's longer window was
  serving wrong numbers anyway. The `family_repeat_fingerprints` mart reader is retained for the
  canary and other potential use.

## 4.359.0 - Cost-layer bug hunt #3: 3 app-side fixes (2026-08-30)

Third, deeper adversarial pass over the cost layer (6 finders: metering/attribution arithmetic,
compare/trend, forecast/capacity, peers/coverage/allocation, mart-vs-live, optimize/unit-cost). One
candidate refuted (the linear vs seasonal month-end projection — both engines drop a gap-day
symmetrically). Two coordinated `family_repeat_fingerprints` mart-vs-live divergences (cache-basis
and candidate-population) are deferred as a scoped loader+reader migration follow-up. Three
app-side fixes:

- **Resize-saving booking no longer overstates busy warehouses (MED)** — booking a downsize saving
  scaled the *whole* monthly bill by the size step (`monthly * (1 − 2^steps)` = the optimistic
  everything-halves ceiling), ~12× too high for a busy, low-idle warehouse. It now scales the idle
  share only (`idle * (1 − 2^steps)`), matching the tab's own conservative model (rec#13: only idle
  reliably shrinks when the per-hour rate halves) and the `POTENTIAL_MONTHLY_SAVING_USD` rollup.
- **Compare-tab "Warehouse spend" KPI reflects all warehouses (MED)** — the LEVEL total summed the
  delta-ranked, top-100-movers frame, so with >100 active warehouses (company=ALL) the largest
  *steady* spender was truncated out of both period totals and the Δ%. It now reads account/company-
  wide totals carried on the single-row `cov` CROSS JOIN, which survive the `LIMIT 100`; the movers
  table/chart still show the top-100 by delta.
- **"Attributed (warehouse)" help matches the number (LOW)** — the help claimed the figure includes
  reader-account metering, but the computation excludes it (reader carries no company key and is in
  the unattributed gap); reworded to match.

## 4.358.0 - Change-risk: CREATE OR REPLACE scored destructive (V105 + live) (2026-08-30)

Completes the one finding deferred from the security-layer hunt #2 (v4.357.0). A
`CREATE OR REPLACE TABLE` that drops and rebuilds a live table was classified as a benign CREATE
(RISK ~40), so it entered neither the destructive-events breakdown nor the RISK≥70 change-risk
queue — a false all-clear on a genuinely destructive change.

- **Loader (V105, owner-gated)** — `SP_LOAD_SECURITY_FACTS` re-derived from V100 so both reload arms
  mark a table create (`CREATE_TABLE` / `CREATE_TABLE_AS_SELECT`) whose text contains `OR REPLACE`
  as `CHANGE_KIND='DESTRUCTIVE'` with `RISK_SCORE` base **55** (the ALTER band, not 90).
- **Scoping that avoids re-flooding the de-noised panel** — base 55 plus the existing PROD/admin
  bumps means only a PROD replace by an actual admin role reaches the ≥70 queue (55+10+10=75). The
  ETL/service roles that drove the historical destructive flood (V080's TF_* / Glue / Informatica
  roles) are never `ACCOUNTADMIN`/`SNOW_ACCOUNTADMINS`, so a service-role replace tops out at 65 and
  stays out of the queue; the V080 exception-queue role exclusion remains a further backstop.
  `CREATE OR REPLACE VIEW` is left alone (definition churn, no data loss).
- **Live builder** — `recent_ddl_changes` gets the matching base-55 bump so the "Who changed what"
  feed shows a `CREATE OR REPLACE TABLE` at the same elevated risk.

Owner applies V105 in Snowsight after V104, then re-runs `SP_LOAD_SECURITY_FACTS(90)` to re-stamp
trailing `FACT_SECURITY_CHANGE` rows.

## 4.357.0 - Security-layer bug hunt #2: 7 app fixes + V104 (2026-08-30)

Second, deeper adversarial pass over the security layer (7 finders: login/MFA, credentials,
grants/least-privilege, escalation/posture, change-risk/DDL, egress, tags/trust). Two candidates
were correctly refuted (the egress-NEW-branch has no volume floor — an intentional
security-conservative choice; a same-region egress duplicate was already covered). One confirmed
finding (CREATE-OR-REPLACE change-risk classification) is deferred as a scoped follow-up because a
safe fix needs live validation to avoid re-flooding the de-noised change-risk panel.

- **Login fact-coverage now measures day density, not calendar span (HIGH)** — the login coverage
  gate used `MIN..MAX` span, so a gappy `FACT_SECURITY_LOGIN_DAILY` (from a past loader outage the
  standing task never backfills) passed as COMPLETE, silently switching the page onto a hole-ridden
  mart that undercounts failed/new-network logins — an attack in the gap window went invisible. Now
  `COUNT(DISTINCT DAY)`, matching `access_evidence_days`, so an interior gap keeps the page on the
  complete live `LOGIN_HISTORY` path. (Also resolves the `fact_coverage_complete` density gap.)
- **Account-takeover "breakthrough" now anchors to a real burst (MED)** — it flagged
  `SUCCEEDED_AFTER` whenever any success followed the *first* failure in the window, so a routine
  login after an isolated typo, and a locked-out terminal burst, both read as breaches. It now
  requires a success preceded by a dense burst (≥ min_failures within 6h).
- **Cross-week credential-expiry double-count fixed (MED, V104)** — the `SEC_CRED_EXPIRY` dedupe key
  appended the ISO week, so a credential in the 10-day horizon raised a new OPEN alert each week and
  V096's `EXPIRING→EXPIRED` supersede only matched a same-week sibling. The key drops the week token
  (`SP_ALERT_SCAN` re-derived from V096); the supersede now matches regardless of raise week.
- **Day-replay DDL covers identity/role/policy changes (MED)** — `day_ddl`'s type list had drifted
  from `recent_ddl_changes`, so a `DROP_ROLE`/`ALTER_USER`/masking-policy change was invisible in
  the day replay; aligned it, dropped warehouse suspend/resume noise, and hardened the row cap.
- **Egress baseline scores only true egress (MED)** — `egress_baseline` counted same-region internal
  transfers its sibling `egress_daily` excludes, false-flagging benign internal movement as a new
  outbound destination; added the matching true-egress predicate.
- **Dormant/reawakening tables sort worst-first (LOW)** — a High-by-role-count row no longer sorts
  below longer-gap Medium rows.
- **`new_network_logins_fact` volume bounded to 90d (LOW)** — matches the live sibling; the mart's
  180-day retention no longer inflates the login/success counts on a source flip.

## 4.356.0 - Operations-layer bug hunt #2: 5 app fixes + V103 (2026-08-30)

Second, deeper adversarial pass over the operations layer (7 finders: sizing, pipeline SLA,
warehouse pressure, contention/optimization, task graph, clustering/cache, page panels). Three
candidates were correctly refuted (a pipeline-SLA cadence claim that misread `TABLE_DML_HISTORY`'s
hourly granularity; a version-diff truncation unreachable under Snowflake's 1000-task DAG cap; a
company-scoped cache-hit ratio that is the documented V044 UNKNOWN law). Five app fixes plus one
owner-gated forward migration.

- **V103 (owner-gated, HIGH): warehouse-efficiency idle % is now span-based** — the mart loader
  counted only a query's *start* hour as active (`COUNT(DISTINCT DATE_TRUNC('hour', START_TIME))`),
  so every later hour of a multi-hour query looked idle: a nightly 3-hour MERGE read `IDLE_PCT`
  ≈ 66.7%. Because the right-sizing panel reads the mart first, that flipped busy batch/ELT
  warehouses from KEEP to SUSPEND and inflated the idle-$ KPI. `SP_LOAD_MARTS_V27` re-derived from
  V102 so the `wh_eff` arm expands each query across the hours it spans (matching the live
  `_active_hours_cte` fixed back on 2026-07-31). Owner applies after V102, then re-runs
  `SP_LOAD_MARTS_V27('HOURLY', d)` to re-stamp trailing rows.
- **Blast-radius warning counts the full radius (MED)** — the suspend/resize confirmation warning
  sourced its user and query totals from the `LIMIT 25` display frame, understating both on shared
  warehouses and dropping exactly the light service/automation accounts an operator most needs to
  know they'd interrupt. It now carries untruncated window totals (`COUNT(*) OVER ()` /
  `SUM(COUNT(*)) OVER ()`) and notes when the table is a top-25 subset.
- **Task-run "dispatch delay" no longer over-fires on wide graphs (MED)** — the signal and KPI
  used the *summed* per-task queue, which grows with fan-out and isn't comparable to the single
  wall-clock span (it could even print larger than wall time). Both now use the run-level *max*
  per-task queue (`MAX_QUEUE_SEC`), the actual dispatch latency.
- **Release "task regressions" stops greening on an empty AFTER window (MED)** — a task with
  BEFORE runs but no AFTER runs yet was folded into a verified "no regression" all-clear; it's now
  marked undecided, and the panel shows "no data yet" (mirroring its query-health sibling) rather
  than a false green right after a deploy.
- **proc-regression keeps "faster but failing" procs (LOW)** — the `LIMIT 200` ordered by signed
  p95-delta truncated exactly the High-severity procs that now error out *fast* (negative delta,
  big fail-jump); it now ranks by the stronger of slowdown and fail-jump.
- **Clustering "recoverable" discloses its real span (LOW)** — the figure covers ≥30 days but was
  labeled with the ambiguous "/window" while the page's picker can be 7 days; it now names the span.

## 4.355.0 - Cost-layer bug hunt #2: 3 app-side fixes (2026-08-30)

Second, deeper adversarial pass over the cost layer (7 finders: pricing/unit-math, SQL builders,
spend/storage, ROI, contract/forecast, AI/allocation). The credit-arm, spend/storage and
contract/forecast lenses swept clean; two candidates were correctly refuted (a CS-ratio anchor
difference that is documented house convention, and a dedup-whitespace gap unreachable because
`create_action_sql` strips on ingest). Three app-side fixes (no migration).

- **Proc cost-trend total no longer contradicts the $/call leaderboard** — the "Trend one
  procedure" drill summed per-day USD that had *already* been rounded to cents (round-then-sum),
  so a cheap-but-frequent proc (~$0.004/day) read `$0.00` total while the leaderboard beside it
  (which sums-then-rounds one window total) showed the true ~$0.11. The day-grain series now
  dollarizes with `round_cents=False` before summing; KPIs round at display.
- **AI budget: the "(all users)" aggregate no longer double-counts its constituents** — the
  scope-aggregate exception row's `PROJECTED_30D_USD` *is* the sum of the per-user projections, so
  queuing it beside the per-user rows double-counted those dollars in the action-acceptance rollup
  (`DONE_USD` / `ESTIMATED_OPEN_USD` sum `ESTIMATED_USD` with no de-overlap). The aggregate is now
  queued with the **incremental** exposure not already itemized (scope total − Σ other queued
  projections, clamped ≥0), so the queued set sums to the scope total once; its detail still shows
  the true scope total.
- **Sub-cent unit-cost columns show real precision** — the measured-$, priced-CALL, and call-tree
  `$%.4f` columns dollarized with the cents-rounding default, quantizing real sub-cent values to
  `$0.0000`; they now use `round_cents=False`, matching the `$/call` sibling.

## 4.354.0 - Operations-layer bug hunt: 8 app-side fixes + V101/V102 (2026-08-30)

Adversarial hunt over the operations layer (task/warehouse/pipeline SQL builders, release-compare
logic, Operations UI tabs, monitor coverage). Eight app-side fixes plus two owner-gated forward
migrations, all keyed on one recurring family: **Snowflake task auto-retries emit multiple
TASK_HISTORY rows for one scheduled run, and several read paths counted attempts instead of runs.**

- **F1 (HIGH): graph-run node view collapses auto-retries** — `task_graph_run_nodes` listed every
  attempt of a retried task, so the node grid double-showed a task that failed then succeeded on
  retry. Added the terminal-attempt `QUALIFY` the sibling readers already use.
- **F3 (MED): dynamic-table failure count includes terminal-failure states** — `dynamic_table_health`
  counted only `STATE='FAILED'`, missing `UPSTREAM_FAILED` / `CANCELLED`, so a DT that never
  refreshed because its upstream failed showed green. Widened the failure predicate.
- **F6 (MED): monitor-coverage panel no longer paints a false green** — when the resource-monitor
  probe failed or the account scope was active, the coverage KPI/warning rendered an affirmative
  all-clear; now neutral with a "coverage unknown" caveat.
- **F7 (MED): release-compare per-query normalization** — `release_query_compare` compared raw
  summed queued-time and remote spill between releases of different query volume; both are now
  per-query (`/ NULLIF(COUNT(*),0)`), and the metric labels/verdicts follow.
- **F8 (LOW): task-error classifier catches "cancelled"/"aborted" spellings.**
- **F9 (LOW): duration-drift / predicted-SLA-miss detectors disclose short windows** — both need
  ≥7 active days; below that an empty result is structural, so the headers render neutral with a
  "needs ≥7 days" caption instead of a green all-clear.
- **F10 (LOW): failure-timeline all-clear discloses its mart basis** — the 7-day short-circuit reads
  the hourly `FACT_TASK_DAILY` mart, which lags the live scan by ~1h; the clean message now says so.
- **F11 (LOW): release verdicts gain a minimum-absolute-delta floor** so a metric that moved a
  hair in relative terms but is materially flat no longer reads as a regression.
- **V101 (owner-gated): `FACT_TASK_DAILY` retry-collapse** — `SP_LOAD_DAILY_FACTS` re-derived from
  V064 so the task rollup aggregates over a terminal-attempt CTE, matching the live readers. The
  mart-first Task Health panel, `day_task_failures` drill and `PIPE_TASK_FAILURES` alert stop
  over-reporting failures a task recovered from on retry. Self-heals on the next hourly run.
- **V102 (owner-gated): task-mart retry-collapse** — `SP_LOAD_MARTS_V27` re-derived from V095 so
  `MART_TASK_GRAPH_DAILY` and `MART_TASK_NODE_DAILY` collapse retries too, so the Pipeline Health
  graph board and per-node timing board match the live drill. Owner re-runs
  `SP_LOAD_MARTS_V27('HOURLY', d)` to re-stamp trailing history.

## 4.353.0 - Cost-layer bug hunt: 3 app-side fixes (2026-08-30)

Adversarial hunt over the cost layer (pricing core, ~45 SQL builders, 6 UI tabs, ROI logic).
The layer swept clean — the pricing / allocation / compare / AI-egress lenses returned no
defects — leaving 3 app-side fixes (no migration).

- **MTD storage no longer skews MoM negative early in the month** — the month-to-date storage
  KPI divided each database's bytes by the *full* elapsed period, so a mid-month day the daily
  loader hadn't delivered yet counted as zero storage, understating the daily average. The
  sibling "Prior full month" value is watermark-corrected, so the MoM delta read a false drop
  (e.g. `-42% MoM` on flat storage in the first week). The MTD total now gets the same
  account-level loader-gap backfill (rescale by elapsed/observed days when the watermark proves
  an under-loaded tail), leaving per-database lifecycle zeros intact.
- **`savings_by_lever` realization % matches `ledger_totals`** — it summed verified $ over every
  verified item in a lever but only estimate-carrying items in the denominator, so a verified
  item booked with no estimate inflated the ratio past 100% (e.g. 130% vs the correct 80%). The
  numerator is now restricted to estimate-carrying items too; the `VERIFIED_USD`/`ITEMS` columns
  keep the full-lever totals.
- **The year-end projection flags thin trailing history** — it extrapolated a full-year total
  from as little as one trailing day (early January, a fresh account, or a loader gap), so one
  busy day ballooned the forecast. It now marks the figure provisional (`~`, low-confidence
  caveat) when fewer than 7 trailing days back it.

## 4.352.0 - Security-layer bug hunt: 9 app-side fixes + V100 (2026-08-30)

Adversarial hunt over the security layer (logic, ~45 SQL builders, 8 UI tabs, migrations)
confirmed 10 distinct defects. Nine are fixed app-side here; the tenth is the owner-gated
**V100** migration (apply in Snowsight after V099). Also fixes a repo-wide `V0*.sql` glob
boundary that would have excluded migration **V100** from the rebuild bundle and several
migration guards (now `V[0-9]*.sql`).

**V100 — security change-fact reload gap** [HIGH, owner-gated]: `SP_LOAD_SECURITY_FACTS(3)`
(the standing task) reloaded `FACT_SECURITY_CHANGE` from the 3-day scratch extract
`OW_QH_EXTRACT`, but deleted the full calendar window (`DAY >= -d days`) while the extract
retains only a rolling ~72h — so `[midnight(D-3), now-72h)` was deleted but never re-inserted,
and once that day left the delete window the loss was permanent. The CHANGE RISK exception-queue
arm then read near-empty for change events older than ~2 days (a `GRANT ACCOUNTADMIN` / `DROP`
on PROD / `CREATE_USER` disappeared). Re-derived from V075 so the `d<=3` branch deletes only the
window the extract can refill (`EVENT_TS >= MIN(extract.START_TIME)`); the `d>3` full-backfill
branch (reads `QUERY_HISTORY` directly) keeps the whole-window delete. Byte-locked to
`outputs/gen_v100.py`.

**App-side:**
- **Break-glass activity panel now reads all statements live** — it selected the change-only
  `FACT_SECURITY_CHANGE` twin when CHANGE RISK coverage was complete, but that fact can't see
  SELECT/COPY/CALL, so an admin doing routine work under `ACCOUNTADMIN` read as a green "hugs
  zero". Always reads live `QUERY_HISTORY` (all statement types) now.
- **Domain posture no longer scored off a display-limited cross-domain frame** — a
  Terraform-driven CHANGE RISK flood could evict every IDENTITY/PRIVILEGE finding past the
  global top-100, scoring the starved domains 100/Healthy (a false all-clear). The exception
  queue now caps **per domain** (`QUALIFY ROW_NUMBER() PARTITION BY DOMAIN`).
- **Overview won't paint green on a read failure** — when the exception queue errored but the
  coverage contract loaded, every COMPLETE domain scored 100/Healthy above the "did not resolve"
  notice. The queue-fail guard now runs before any scoring/verdict/KPI.
- **Auditor export MFA sheet proves an empty mart against live** — a deployed-but-empty
  `FACT_LOGIN_DAILY` wrote an empty `mfa_gaps` CSV (a manufactured clean bill of health in a
  compliance artifact); it now falls back to live `LOGIN_HISTORY` like the Access tab.
- **Access-tab MFA panel is honest when the live fallback itself fails** — shows "unconfirmed",
  not a green "no gaps".
- **Admin-grant off-hours/weekend flag is account-local (Chicago)** — it computed `HOUR()` /
  `DAYOFWEEKISO()` on the SiS session clock (UTC/LA), so a normal-hours deploy grant read as
  off-hours and a real off-hours elevation read as within-hours. Now wraps
  `CONVERT_TIMEZONE('America/Chicago', ...)`, matching the egress detector.
- **`day_grants` scopes GRANTS_TO_USERS by the grantee, not the granted role name** — a shared
  admin role (`SNOW_SYSADMINS`) granted to an in-company user was dropped from that company's
  day-replay; now uses `COMPANY_FOR_USER(GRANTEE_NAME)`, matching the access-changes feed.
- **`failed_login_reasons` uses the same coarse `ERROR_CATEGORY` buckets as its fact twin**, so
  the panel is path-invariant across a window flip.
- **Unused-roles export recommends REVIEW, not REVOKE** — a role exercised only via inheritance
  never appears as an executing role, so "unused by any query" over-claimed; now flags the
  inheritance caveat.

## 4.351.0 - Incident-management bug hunt: 9 app-side fixes + V099 (2026-08-30)

Adversarial hunt over the incident-management layer (logic, SQL readers, triage UI,
migrations) confirmed 10 distinct defects. Nine are fixed app-side here; the tenth is the
owner-gated **V099** migration (apply in Snowsight after V098).

**V099 — SP_INCIDENT_AUTODECLARE family-open guard scoped by company** [HIGH, owner-gated]:
the proc groups per (FAMILY, COMPANY) but its family-already-open guard correlated only on
the family, never `i.COMPANY = c.COMPANY`. Because ALFA and Trexis share rule families, a
CRITICAL for one company was silently *not* auto-declared whenever the other company had an
open incident of the same family — a cross-company coverage gap. Re-derived from V098 with
the company correlation added. Byte-locked to `outputs/gen_v099.py`.

**App-side (Control Room / Alerts / RCA / charts):**
- **Manual declare no longer duplicates an already-open family** — `_incident_declare_sql`
  gained the same family-already-open guard SP_INCIDENT_AUTODECLARE carries (per company),
  and its members INSERT only fires if the incident row was actually created; it also now
  returns a statement **list** so the declare is never string-split on `;` (a `;` in an
  alert-derived title used to break the INSERT mid-literal).
- **Dropped the "Change-correlated" incident KPI** — it counted `INCIDENT_MEMBERS` of kind
  `WH_CHANGE`/`DEPLOY`, but no writer ever persists those kinds, so it was a permanent,
  misleading `0%`. Change correlation lives in the Control Room RCA panel.
- **RCA stops advertising the entity-match factor** — the production caller never passes
  entity context, so the `0.20·entity-match` term was a constant that couldn't differentiate
  candidates; the caption and the per-hypothesis "why" no longer claim it.
- **`incident_timeline` 7d fallback brought to parity with the 48h mart** — it had dropped
  the warehouse-change arm and half the DDL types, omitted COMPANY/REF_ID, and relabelled the
  replay lanes, so toggling 48h→7d silently thinned the same window. Now identical KIND labels
  (`ALERT`/`TASK_FAIL`/`DDL`/`WH_CHANGE`), full DDL set, COMPANY + REF_ID, and the WH_CHANGE arm.
- **`incident_gantt` lanes by unique INCIDENT_ID, not the shared TITLE** — auto-declares all
  titled `Auto: <family>` collapsed onto one row; plus a subtle 'now' reference line so an
  open incident's projected right edge reads as reaching now, not a measured resolution (C38).
- **Declare confirm/latch keys scoped per proposal** — a typed DECLARE no longer re-arms the
  button after switching to a different proposal (mirrors the close flow's per-incident keys).
- **Incidents exception-summary won't false-all-clear** — a failed incident-metrics /
  critical-count / health read collapsed to 0 and read as "nothing wrong"; it now surfaces a
  partial-telemetry signal so green never means "nothing loaded".

## 4.350.0 - Default landing opens on Brief for DBAs (2026-08-30)

The DBA profile's page tuple listed `Ask` first, so `pages[0]` — the default landing
when there is no saved default view and no `?page=` deep link (app/main.py
`current = _ow_page or pages[0]`) — resolved to **Ask OVERWATCH** for DBAs, even though
the sidebar display had already moved Ask to its own group below Govern. Reordered
`PAGES_BY_PROFILE["DBA"]` so **Brief** is first and Ask trails last, matching the nav
display order — DBAs now open on Brief. Ask is unchanged in availability (still DBA-only,
still its own nav group). Locked by `tests/history_locks/test_brief_landing.py` (every
profile lands on Brief; DBA still sees Ask, last).

## 4.349.0 - V096/V097/V098: alert-scan dedupe/clear key hardening (2026-08-30)

Alerting-layer hunt (3 owner-gated forward migrations). Each defect stems from a dedupe/clear
key not encoding the state or band it needs, or a proc missing a guard. Owner applies in
Snowsight in order after V095; all forward-healing (no backfill — auto-clear/escalation start
working on the next scan; pre-existing stranded/double-counted events may need a one-time manual
resolve or age out).

**V096 — alert-scan dedupe/clear keys** (re-derives two procs):
- `SP_ALERT_SCAN` (from V091): (1) [MED] the auto-clear sweep matched
  `DEDUPE_KEY LIKE '%|' || CURRENT_DATE()` (today only), but the three opted-in rules measure
  over a trailing-24h window, so a condition that drops below CLEAR the *next* day never
  auto-cleared and stranded OPEN. Now matches a `RAISED_AT >= 48h` recency window, keeping the
  `>=1h` dwell and the below-CLEAR hysteresis NOT-IN; the ENABLED+AUTO_CLEAR_ENABLED rule scope
  still excludes fact-day rules. (3) [LOW] the supersede sweep OR-list gains
  `|EXPIRING|→|EXPIRED|` so a SEC_CRED_EXPIRY credential isn't counted as both an open HIGH and
  an open CRITICAL. (2b) it also gains `|HIGH|→|CRIT|` for the SLO burn band below.
- `SP_SLO_BREACH_SCAN` (from V085): (2) [MED] the dedupe key was `RULE_ID|SLO_ID|<date>` with no
  burn band, so a same-day HIGH→CRITICAL escalation collided with the earlier HIGH on the
  identical key and the NOT EXISTS guard suppressed the CRITICAL page. Now appends a burn band
  token `IFF(COALESCE(BURN_MULTIPLE,0)>=2,'CRIT','HIGH')` (mirroring V066 PIPE/BUDGET), so HIGH
  and CRIT get distinct keys and the supersede sweep resolves the superseded HIGH.

**V097 — SP_ANOMALY_SWEEP mean-AD fallback** [MED]: the COST_ANOMALY_SWEEP arm hard-filtered
`WHERE l.MAD > 0`, so a majority-idle/intermittent series whose median-absolute-deviation
collapses to 0 silently dropped even a large material spike. Re-derived from V076 to port the
app twin's estimator (`app/logic/anomaly.py` `robust_zscores`): a `meanad` CTE
(`AVG(ABS(CREDITS-MED))`) and a MAD-first / mean-AD-second robust-z denominator
(`0.6745/MAD` else `0.7979/MEAN_AD`, matching `_MAD_K`/`_MEANAD_K` and gate order), with the
hard filter replaced by a `SIGNED_Z IS NOT NULL` guard. Collapse suppression and the
materiality gates ($50 floor, ≥10 active days) are unchanged.

**V098 — SP_INCIDENT_AUTODECLARE re-link guard** [LOW]: the member INSERT re-scanned
ALERT_EVENTS with no anti-membership guard, so an alert already a member of one incident (e.g. a
still-OPEN CRITICAL whose incident was resolved without resolving the alert) could be re-attached
to a second incident and double-counted. Re-derived from V032 with the same
`NOT EXISTS INCIDENT_MEMBERS` guard the `crit` CTE already carries added to the member INSERT.

Each byte-locked to `outputs/gen_v0NN.py`; full migration lockstep (validate tip 95→98,
`_EXPECTED_MIGRATIONS`, run-lists, rebuild bundle, tip tests).

## 4.348.0 - V095: evidence-based ROLE company classification (2026-08-30)

Data-loader hunt (MED). `SP_LOAD_MARTS_V27`'s cost-allocation arm stamped the COMPANY
of `MART_COST_ALLOCATION_DAILY`'s **ROLE** dimension with an inline
`CASE WHEN UPPER(ROLE_NAME) LIKE '%TRXS%' THEN 'Trexis' ELSE 'ALFA' END` — defaulting
every non-TRXS role to ALFA and **never emitting `UNKNOWN`**, bypassing the V044
evidence-based classification law that its USER / DATABASE / SCHEMA siblings already
honor via `COMPANY_FOR_USER` / `COMPANY_FOR_DATABASE`. Shared roles
(PUBLIC / SYSADMIN / ACCOUNTADMIN) inflated the ALFA ROLE total, and the app's
first-class UNKNOWN company pill returned zero ROLE-dim rows while USER/DATABASE
populated it.

**V095** (owner-gated; owner applies in Snowsight after V094, then re-runs the
`SP_LOAD_MARTS_V27(HOURLY)` backfill to re-stamp trailing ROLE history):
- New `COMPANY_FOR_ROLE(R)` scalar UDF — classifies a role NAME by the SAME evidence the
  live V044 `COMPANY_FOR_USER` role predicates and `app.companies.role_clause` use:
  `%TRXS%` → Trexis, `%ALFA%` or the two DBA roles (`SNOW_ACCOUNTADMINS` /
  `SNOW_SYSADMINS`) → ALFA, else `UNKNOWN` (NULL-safe via COALESCE).
- `SP_LOAD_MARTS_V27` re-derived from V082 so the ROLE arm calls
  `COMPANY_FOR_ROLE(ROLE_NAME)` — making the ROLE dim structurally identical to its
  siblings and centralizing the role-company law server-side (the buggy inline CASE was
  copy-pasted across ~19 historical, now-superseded migrations).
- Sibling arms byte-identical to V082; new UDF + proc, no schema change, no data reload.
- `teardown.sql` drops the new UDF; byte-locked to `outputs/gen_v095.py`.

## 4.347.0 - Alerting-layer bug hunt: 3 app-side fixes (2026-08-30)

Adversarial alerting-layer hunt (7 lenses over the scan/delivery procs + app alert
builders/logic) confirmed 8 defects. The 3 app-side ones are fixed here; the 5
migration-bearing ones (scan/escalation/anomaly/incident procs) are tracked for
owner-gated forward migrations.

* **[med] Delivery-backlog panels undercounted starved CRITICAL alerts.**
  `route_backlog` and `last_delivery_health` gated eligibility on a flat 24h window,
  but the sender (`SP_NOTIFY_WEBHOOK`, V064) keeps a CRITICAL event eligible to send
  for 7 days — so a CRITICAL stuck past 24h (integration outage) showed BACKLOG=0 /
  "Eligible to send now: 0" / "expired" while the drainer was still retrying it. Both
  builders now mirror the sender's severity-aware window (7d CRITICAL, 24h otherwise)
  via a shared `_SEND_ELIGIBLE_SINCE`.
* **[med] MTTA/MTTR headline was a mean-of-weekly-means.** The tiles averaged
  `alert_mttr`'s already-per-week averages unweighted, so a 1-event week counted the
  same as a 100-event week (a {1@100min, 100@10min} pair read 55 min vs the true ~11).
  Now event-weighted by the per-week ACKED/RESOLVED counts.

Also surfaced (tracked, not fixed — owner-gated forward migrations): V091 auto-clear
never matches next-day-cleared conditions (events strand OPEN); PERF_SLO_BREACH and
SEC_CRED_EXPIRY escalations swallowed / double-counted (dedupe key lacks the band);
SP_ANOMALY_SWEEP misses spikes on majority-idle series (no mean-AD fallback); and
SP_INCIDENT_AUTODECLARE can re-link an already-membered alert.

## 4.346.0 - V094: fix the FACT_QUERY_HOURLY boundary-hour duplicate (2026-08-29)

Data-loader hunt over the migration `SP_LOAD_*` procs found a **HIGH** silent
fact-corruption bug. `SP_LOAD_QH_EXTRACT`'s `FACT_QUERY_HOURLY` refresh (V062)
DELETEs on the hour-truncated `HOUR_TS` against a *non*-truncated `-48h` instant, but
re-INSERTs `DATE_TRUNC('hour', START_TIME)` filtered by that same instant — so the
boundary hour is never deleted yet is re-inserted *partial* each run. Two rows then
accumulate per grain for every hour older than ~48h, permanently, and any reader that
SUMs the grain over a window ≥2 days double-counts (~2× `QUERY_COUNT` / `FAILED_COUNT`
/ elapsed / queued / spill; P95 is MAX-based, unaffected; the 24h/48h boards are
clean). The sibling `SP_LOAD_OPS_DIAG` already truncates both bounds (the B10 fix) —
this arm was the one it missed.

* **V094 (owner-gated migration)** re-derives `SP_LOAD_QH_EXTRACT` from V062
  byte-identically plus two edits: the DELETE and INSERT bounds are both hour-truncated
  (`DATE_TRUNC('hour', DATEADD('hour', -48, …))`), so the boundary hour is deleted and
  fully rebuilt each run. The watermark first-run fallback is unchanged. Plus a one-time
  `INSERT OVERWRITE … QUALIFY ROW_NUMBER()` dedup keeping the highest-`QUERY_COUNT` row
  per grain (the complete hour), since `SP_NIGHTLY_RECONCILE` does not rebuild this fact.
  Generated by `outputs/gen_v094.py`, byte-locked. **Owner applies in Snowsight after
  V093** (no backfill needed). Full lockstep bumped.

Also surfaced (not fixed here — tracked): a MED cost-allocation ROLE-dimension
mis-classification in `SP_LOAD_MARTS_V27` (bypasses the V044 UNKNOWN law) and a LOW
mixed-timezone day-grain join in `SP_LOAD_PLATFORM_SCORE` (only bites if the account
timezone is not UTC).

## 4.345.0 - Logic-layer bug hunt #2: 3 fixes (verdict, ranking, dedup) (2026-08-29)

Second adversarial logic-layer hunt (7 module-cluster lenses, each finding executed
to confirm). Three confirmed:

* **[med] Release-compare silenced from-zero regressions.** `compare_release_periods`
  reported "n/a" when a lower-is-better metric's BEFORE value was 0 — so a fail rate
  going 0%→8% (or queued 0→5m, spill 0→10GB) showed "n/a" and dropped off the
  Operations release-compare, rendering a green "no regression" banner on the most
  dangerous deploy (clean→broken). The percentage is undefined at a zero baseline but
  the direction is not; the verdict now judges by the sign of the change. (Was B18 in
  a 2026-07-29 review, never fixed.)
* **[med] RCA could headline a LOW-capped untimed candidate over a HIGH cause.** The
  ranking sorted by raw score with only a HIGH-vs-not tiebreak, but the LOW band-cap
  for untimed/after-onset candidates lowers the band, not the score — so an untimed
  candidate whose score edged a timed one took position 0 and `rca_summary` headlined
  it as the lead. Ranking now keys on band strength first, then score.
* **[low] ROI projection collapsed distinct blank-key actions.** `scenario_projection`
  built its dedup id from `TYPE:KEY` and only fell back to the unique ACTION_ID when
  the combined string was ≤1 char, so a blank key ("WAREHOUSE:" → "WAREHOUSE") merged
  every blank-key action of that type into one entity, under-counting the candidate
  and gross-estimate KPIs. It now falls back on ACTION_ID whenever the key is blank.

## 4.344.0 - V093: seed the 17 unseeded DEFAULT_SETTINGS keys (2026-08-29)

Completes the v4.343.0 SETTINGS fix. The Admin ▸ Settings writer already upserts, so
edits to unseeded keys persist — but 17 keys had no row until first edited, so the
Settings table view was incomplete and the seed set had drifted from
`DEFAULT_SETTINGS`.

* **V093 (owner-gated migration)** MERGE-seeds the 17 keys (9 `SCORE_PTS_*`
  platform-score weights, 5 `GOV_PTS_*` governance-drift weights, `FORECAST_ENGINE`,
  `EXPECTED_SPIKE_CALENDAR`, `DATA_TRANSFER_USD_PER_TB`) with their code-default
  values, `WHEN NOT MATCHED` only — it never overwrites an operator's edited value.
  Data-seed only (no schema/proc/view/reload). **Owner applies in Snowsight after
  V092.**
* Lockstep bumped: `_EXPECTED_MIGRATIONS`, `validate.sql` (V001..V093), DEPLOYMENT/
  README run lists, the regenerated rebuild bundle (`02_migrations_V001_V093.sql`),
  and the migration tests that pin the validate tip.
* New recurrence guard (`test_v093_seed_default_settings.py`): every editable
  `DEFAULT_SETTINGS` key must be seeded by some migration, so a future key added to
  config.py can't silently go unseeded again.

## 4.343.0 - SQL-layer bug hunt: 3 fixes (parity, upsert, NULL-safety) (2026-08-29)

Adversarial SQL-layer hunt (7 lenses over `app/data/*_sql.py` + migrations:
injection/identifier, scoping, aggregation-grain, join/NULL/3VL, window/date/
pruning, mart-vs-live, migration-integrity). Three confirmed:

* **[med] Boss chart gained/lost a phantom CLOUD_SERVICES_ONLY segment by path.**
  `monthly_spend_by_warehouse` (mart) excludes the CLOUD_SERVICES_ONLY pseudo-
  warehouse but its live fallback `fact_monthly_spend_by_warehouse` did not, so on
  the ALL view the segment and every month's total shifted depending on which path
  served (the fallback fires until the efficiency mart accrues 12 months). The
  fallback now applies the same exclusion.
* **[med] Admin settings edits were silently lost for 17 unseeded keys.** 17
  `DEFAULT_SETTINGS` keys (`SCORE_PTS_*`, `GOV_PTS_*`, `FORECAST_ENGINE`,
  `EXPECTED_SPIKE_CALENDAR`, `DATA_TRANSFER_USD_PER_TB`) are never seeded, and the
  Admin writer was UPDATE-only — a 0-row UPDATE that Snowflake does not error on, so
  the UI reported success while nothing persisted. The writer is now a MERGE upsert
  that inserts the row when absent.
* **[low] `org_all_in_window_usd` OTHER_USD dropped NULL-RATING_TYPE rows.** The
  residual used a bare `UPPER(RATING_TYPE) NOT IN (...)`; under three-valued logic a
  NULL row yields NULL (not TRUE), so its dollars were counted in `TOTAL_USD` but
  excluded from every bucket, breaking the buckets-sum-to-total invariant. Now
  `COALESCE(UPPER(RATING_TYPE), '')`, matching the monthly builder.

## 4.342.0 - Round-5 UI-layer bug hunt: 2 help-text corrections (2026-08-29)

Fifth hunt over `app/ui/*` with seven not-yet-run lenses (exhaustive per-builder
charts, C25 empty-honesty, help-vs-computation, boundary/thresholds, column-config,
account-time, cross-file drift). Five came back clean; the only two survivors were
help strings that misdescribed a correct computation:

* **[low] Incident "Reopen rate" help claimed a 14-day owner-set window the metric
  never applies.** `incident_metrics(90).REOPEN_PCT` counts any resolved incident in
  the 90-day window later reopened via `REOPENED_FROM`, with no 14-day bound
  (`INCIDENT_REOPEN_DAYS` is dead config). The help now describes the actual basis.
* **[low] Overview "Pace vs budget calendar" help formula was off by one day.** The
  card correctly shows expected-to-date over *completed* days (today excluded, since
  metering lags), but the help wrote the formula as `… x day_of_month`, one day
  higher. The help now says `x completed days — today excluded`.

Both are documentation-only (the displayed numbers were correct). After five rounds
the UI layer's numeric/visual correctness is well-swept — this round found no wrong
number, crash, or broken visual, only two captions.

## 4.341.0 - Round-4 UI-layer bug hunt: 3 fixes (regression audit clean) (2026-08-29)

Fourth adversarial hunt over `app/ui/*`, including a regression self-audit of this
session's ~40 changes (which came back clean — no fix introduced a new bug). Four of
seven finders were empty; three confirmed, each fixed and locked:

* **[med] Entity 360 re-drill to an already-seeded entity was swallowed.**
  `_seed_entity_context` deduped on a persistent `_ow_entity_context_applied`
  signature that was only ever set, never consumed — so after the viewed key diverged
  (via the catalogue picker or a typed key), a repeat drill to the same entity was
  ignored and the operator was left on the wrong entity. It now consumes the entity
  identity from the nav context (mirroring Action Center's `action_id`), so a drill
  delivers once per arrival and a re-drill always re-seeds.
* **[med] `bar_usd` flattened sub-dollar spend to "$0"/"$1".** The whole-dollar axis
  and always-visible bar labels rounded a $0.58 per-user Cortex-token bar to "$1" (and
  $0.30 → "$0"), contradicting the chart's own cents-showing tooltip/caption. Both
  `bar_usd` and `clickable_bar_usd` now keep cents on a sub-dollar chart, the same
  magnitude-aware format `daily_stacked_usd` already uses.
* **[low] Alerts events-by-day tooltip showed "…, 12:00:00 AM".** The day tooltip was
  an unformatted `Day:T` on day-grain data; it now uses the F40 `_DAY_TIP_FMT`
  ("Aug 14, 2026") and the adaptive day axis, matching the sibling day-grain charts.

## 4.340.0 - Round-3 UI-layer bug hunt: 11 correctness/UX fixes (2026-08-29)

Third adversarial multi-agent hunt over `app/ui/*` (per-file-cluster deep audits +
write-path / cache-identity / nav lenses), each finding independently verified. 11
confirmed, each fixed and locked:

* **[med] Admin "Diagnose stale sources" flagged freshly-loaded empty marts as
  "never filled."** Staleness now keys on load AGE only (never `ROW_COUNT`), and the
  backfill hint fires only when a source has genuinely never loaded — "missing
  measurement != measured 0".
* **[med] Decision Studio "Realization rate" delta contradicted its own %.** It
  rendered all-verified totals while the % is computed only over verified items that
  carried a positive estimate; `ledger_totals` now exposes that restricted
  numerator/denominator and the delta renders those.
* **[med] Security effective-access graph could drop the self-escalation path it
  flagged.** The per-user `head(80)` (RISK_SCORE order) could truncate a low-scoring
  MANAGE-GRANTS-only path; escalation-critical paths are now floated in first (stable
  sort) so the evidence for the flag is never cut.
* **[med] "Priciest procedure (per call)" KPI read a row sorted by TOTAL credits.**
  It now selects the genuinely priciest-per-call procedure.
* **[med] Overview spend-tile vs-prior delta was mislabeled.** The builder clamps to
  the 182-day half-window, but the label printed the unclamped day count; it now
  labels the real comparison window, and Cost▸Contract's by-warehouse table discloses
  the same clamp.
* **[med] Brief/home "never loaded" badge never fired.** The `-1` sentinel arrives as
  the string `"-1.0"`; both surfaces now compare numerically.
* **[med] Ask bullets rendered raw SQL / error text through markdown.** Untrusted
  `SAMPLE_TEXT` / `LAST_ERROR` are now wrapped as inline code, so `*`/`_`/`$$` render
  literally instead of mangling or forming a LaTeX span.
* **[low] Change-risk "Destructive events" count summed only the top-200 groups.** A
  pre-LIMIT window total now backs the headline; shares stay of the shown groups, with
  a disclosure when truncated.
* **[low] Contract remaining-balance chart hardcoded "$".** It now follows the org
  rate-card currency (title + ticks), USD only when the account bills USD.
* **[low] Warehouse-resize booking used the RECOMMENDATION string, not the chosen
  target.** It now books the credit-model saving from the actual target vs the current
  size, and only on a confirmed downsize (no phantom saving on a same-size/upsize).
* **[low] Resize "Resize to" widget used a fixed key** that leaked the target across
  warehouses; the key is now warehouse-scoped.

## 4.339.0 - C38: projected storage-growth bars get a modeled mark (2026-08-29)

The Cost▸Optimize "Storage growth movers" panel drew `GROWTH_USD_30D` — a
least-squares **projection** — as solid gradient bars identical to measured spend,
violating the C38 data-provenance mark grammar (a guess drawn like an observation).

* `charts.bar_usd` gains an optional `modeled: bool`. When set, the bars render as
  the C38 **MODELED** mark: a hollow (flat fill at `MODELED_FILL_OPACITY = 0.20`),
  dashed-outline (`_MODELED_DASH = [5, 3]`) bar, and the tooltip reads "Projected
  $/mo". This rides a dashed stroke + hollow body — two channels a *provisional* bar
  never uses — so it is never confused with the 0.45 "measured-but-incomplete"
  dimming (a different provenance state). Measured `bar_usd` callers are unchanged
  (solid gradient, default `modeled=False`).
* The Cost▸Optimize storage-growth call site passes `modeled=True`; the measured
  object-cost bars on the same page stay solid.
* **Visual change** — the mark encoding is spec-verified, but its on-screen
  appearance needs a live Streamlit-in-Snowflake eyeball.

## 4.338.0 - V092: Action lifecycle clear signals (un-assign / un-defer) (2026-08-29)

Closes the v4.318 honesty gap in the Action Center. `SP_ACTION_LIFECYCLE` (V074)
COALESCE-keeps `OWNER` and `DEFER_UNTIL`, so a blank owner or a toggled-off defer
was a silent no-op — v4.318 had to *remove* "unassign" and "clear the defer" as
savable effects because the proc couldn't perform them, leaving operators no in-app
way to reassign to nobody or resume a deferred item now.

* **V092 (owner-gated migration)** re-derives `SP_ACTION_LIFECYCLE` from V074
  byte-identically plus two enumerated edits: new `P_CLEAR_OWNER` / `P_CLEAR_DEFER`
  BOOLEAN parameters, and the owner/defer assignments wrapped
  `IFF(:P_CLEAR_x, NULL, <old COALESCE-keep>)`. Both flags FALSE (an ordinary edit)
  is the old behaviour byte-for-byte. The 8→10 arg signature change drops the old
  overload first. Generated by `outputs/gen_v092.py`; byte-locked by
  `tests/migrations/test_v092_action_lifecycle_clear.py`. **Owner applies in
  Snowsight after V091.**
* **App rewire** (ships now): `action_transition_sql` passes the clear flags;
  `_render_action_detail` derives them from the form diff (a set owner blanked / a
  set defer toggled off) and restores the "unassign the owner" / "clear the defer
  (resume now)" effect lines.
* Lockstep bumped: `_EXPECTED_MIGRATIONS`, `validate.sql`, DEPLOYMENT/README run
  lists, and the regenerated rebuild bundle (`02_migrations_V001_V092.sql`).

## 4.337.0 - UI-layer bug hunt round 2: seven correctness/UX fixes (2026-08-29)

Second adversarial multi-agent hunt over `app/ui/*` — per-file-cluster deep audits
plus a degenerate-input crash sweep, each finding independently verified. Seven
confirmed, each fixed and locked:

* **[high] Control Room RCA blanked its flagship panel for manual incidents.**
  `onset = STARTED_AT or DETECTED_AT` — but a manually-declared incident has a NULL
  `STARTED_AT` (→ `pd.NaT`), and `bool(pd.NaT)` is True, so the `or` returned NaT and
  the `pd.isna` guard skipped the whole "Auto-investigation — ranked root cause"
  section. Now falls back on `DETECTED_AT` NA-safely.
* **[med] Alerts snoozed-events tray hid on an empty open feed.** The "💤 Snoozed
  (n)" tray rendered inside the `if guard(events, …)` block, so when the open feed
  was empty the page showed a green "found nothing over threshold" all-clear while a
  snoozed CRITICAL sat invisible until auto-wake. The tray now renders outside the
  guard.
* **[med] Entity 360 catalog editor could write ownership onto the wrong entity.**
  Its nine edit widgets used fixed keys, so switching entities left the prior
  entity's values in the form (Streamlit ignores `value=` once a key exists) and Save
  MERGE'd them onto the newly-selected entity. Keys are now scoped per entity.
* **[med] Overview budget burndown faked an "under pace" gap.** It was fed the
  company-scoped, `days`-windowed `daily_complete` (default 7 days) while the
  straight-line budget reflected the full elapsed month, so past ~the 8th the
  cumulative actual covered only the last week. Now fed the account-wide full-month
  frame (`proj_daily`), today's partial excluded.
* **[med] Compare "Incomplete coverage" warning fired on fully-loaded data.** It
  compared active-day `COUNT(DISTINCT DAY)` against the calendar span, but
  `FACT_WAREHOUSE_DAILY` is sparse (no date spine), so a weekend-suspended workload
  tripped a false banner on correct month-over-month data. The check now judges
  loader reach: a new `LOADED_THROUGH` (the scope's global last loaded day) vs each
  window's end, so idle days never read as unloaded days.
* **[low] Operations Queries sparklines ignored active warehouse/user/schema
  filters.** The trend feed honors only company+database, so a filtered KPI number
  was paired with a company-wide trend. The sparkline is now suppressed when one of
  those filters is active (database is honored, so it doesn't suppress).
* **[low] Operations failure-timeline header alarmed over a clean body.** The "(7d)"
  header alarm was driven by the window-wide failure count (up to 365d) while the
  body scans a fixed 7 days, so a >7-day-old failure painted an amber header over a
  verified-clean body. The alarm now reflects the actual 7-day failures.

## 4.336.0 - UI-layer bug hunt: three presentation corrections (2026-08-29)

Adversarial multi-agent hunt over `app/ui/*` (six diverse finder lenses, each
finding independently verified — chart specs compiled, helpers executed, code
paths traced) surfaced three confirmed bugs, each fixed and locked with a test:

* **Chart takeaway captions rendered `$` amounts in serif-italic math font.** The
  three USD "Top: X $Y (Z% of $total)" lead-with-the-conclusion captions
  (`bar_usd`, `daily_stacked_usd`, `monthly_stacked_usd` — the boss chart)
  emitted two runtime `$` into `st.caption` without escaping, pairing into an
  inline LaTeX span (the "creditspend" font bug). Wrapped each in the house
  `md_dollars()` escaper. The count (`dollars=False`) takeaways are unaffected.
* **The Alerts events-by-day chart drew INFO events as an unmapped black bar.**
  Its severity color scale listed only `[CRITICAL, HIGH, MEDIUM, LOW]`, so any
  INFO-severity day (INFO is a first-class alert severity everywhere else)
  rendered with no fill and no legend entry. Extended the domain/range to the
  full 5 levels via the shared `SEV_COLORS`, matching `operational_replay` and
  `incident_gantt`.
* **Operations ▸ Tasks ▸ Health ignored the Schema filter it promised to honor.**
  The default Health view's mart read (`mart_sql.fact_task_daily`) omitted the
  `schema_contains` predicate, so its failure KPIs and task table over-counted
  across all schemas while the live fallback (`ops_sql.task_runs`) correctly
  scoped — the same filter yielded two different failure counts by path. Added
  the `SCHEMA_NAME` predicate to the mart builder and threaded the filter
  through, so the mart path now matches the contract and the live path.

## 4.335.0 - Logic-layer bug hunt: three scoring/measurement corrections (2026-08-29)

Adversarial multi-agent hunt over the logic layer (`app/logic/*.py`) surfaced
three confirmed correctness bugs, each now fixed and locked with a test:

* **Privilege-escalation score double-counted MANAGE grants.** `escalation_flags`
  seeded its base from `RISK_SCORE`, but that SQL score already weights
  `MANAGE_GRANTS` (`OWNERSHIP*10 + MANAGE_GRANTS*25 + SENSITIVE*20`); adding the
  self-escalation bump on top counted manage privilege twice, so a manage-only
  principal scored 65 instead of 40. The base is now rebuilt from object-privilege
  exposure (ownership + sensitive) alone, with depth/reach/self-escalation added
  once. `RISK_SCORE` is unchanged where it's displayed standalone.
* **Root-cause analysis could headline an *untimed* candidate as high-confidence.**
  A candidate with no timestamp (`when=None`/`NaT`) was given a neutral proximity
  of 0.3, which let it escape the LOW cap and present as the "most likely cause
  (high confidence)" while its own reason read "timing unknown". Untimed
  candidates are now capped to LOW.
* **Product-retirement flagged products it couldn't actually see.** A left-merge
  miss against the reads mart turned into `fillna(0)`, indistinguishable from a
  measured zero, so a costly product simply *absent* from the reads data was
  flagged `RETIRE_CANDIDATE`. Merge-misses now route to `INSUFFICIENT_DATA`;
  only products present in the reads mart with genuinely collapsed usage retire.

## 4.334.0 - Nav order + reconciliation-footer font fix (2026-08-29)

* **Ask OVERWATCH moved below Govern** in the sidebar nav order (was the top group).
* **The reconciliation footer's label rendered in a serif-italic math font.** The
  `credit spend (Nd)` label (and the `Σ ...` totals caption) sat between two
  `format_usd` dollar signs, which Streamlit markdown pairs into an inline LaTeX
  math span — so the between-text rendered as serif italic with collapsed spaces
  ("creditspend(7d)"). Both footers now wrap their string in the house
  `formulas.md_dollars()` escaper, so the label matches the caption font.

## 4.333.0 - SQL bug hunt fixes (2026-08-29)

* **Two repeat-query / release-health panels under-reported per-company activity.**
  `insights_sql.repeat_query_fingerprints` and `release_query_compare` scoped
  QUERY_HISTORY by warehouse company AND user company in one hard-AND — the exact
  intersection the C10 fix removed elsewhere. It dropped cross-company activity (a
  Trexis/UNKNOWN principal — e.g. a service account — running on an ALFA warehouse
  vanished from BOTH the ALFA and Trexis scoped views, appearing only under ALL), so
  the scoped repeat-query (cache-candidate) list and post-release query-health compare
  silently under-counted. Now scoped by WAREHOUSE company only, matching
  `ops_sql._query_scope`, the V082 mart twin, and the C10 doctrine.
* Doc: the rebuild runbook said "all 90 migrations" — there are 91 (V001..V091).

## 4.332.0 - Bug hunt round 3 fix (2026-08-29)

* **The active tab's accent text color was a silent no-op.** F5 set the accent ink
  on the tab BUTTON, but a tab label renders as a markdown `<p>` whose own declared
  ink-soft color beats an inherited `!important` from the button — the same cascade
  trap the pills already guard against. The accent is now forced onto the tab's text
  node too, so the selected tab's label actually turns accent (its underline and bold
  weight already worked). Every F5 active variant now carries the matching text-node
  force.

## 4.331.0 - Bug hunt round 2 fixes (2026-08-29)

* **The boss chart's top total label could clip off the top.** F44 added a
  stack-total label above each month's bar, but the bars' y-scale had no headroom —
  so when the tallest month's total landed on/near a round axis tick, its label
  rendered above the plot rectangle and got clipped (proven by compiling the actual
  SVG). The bars' y-scale now pads to `1.12×` the max stacked total, mirroring
  `bar_usd`, so the tallest month's headline number always has room.
* **Robustness on NULL values** (found via edge-case fuzzing; not yet reachable but
  the code's own comments claimed the safety): `_fmt_metric_value` now returns an
  em-dash for `None`/`pd.NA`/`NaT` (previously only float `nan`, so a null metric
  rendered the literal "None"/"&lt;NA&gt;"/"NaT"); and `_is_watched` (the watch-star
  helper) is now truly NULL-safe — `bool(pd.NA)` raises "ambiguous" and `bool(NaN)`
  / `bool(NaT)` are both `True`, so a NULL in a WATCHED column now correctly reads
  as not-watched instead of crashing or showing a spurious star.

* **Disabled primary buttons were illegible.** The F24 change gated the primary
  button's accent-gradient BACKGROUND on `:not(:disabled)` but left the companion
  dark-ink (`#0f172a`) TEXT rule ungated — so a disabled primary kept near-black ink
  after its bright background was removed, rendering dark-on-dark. The ink rule now
  carries the same `:not(:disabled)` guard, so a disabled primary inherits the F24
  locked treatment's faded light ink like a disabled secondary. Hit the common
  default-state gated controls (Save-with-nothing-dirty, type-to-confirm Execute).
* Removed a dangling `charts.event_timeline` call left in the (skipped-by-default)
  `test_stress.py` after that function was deleted in the dead-code cleanup.

* Removed eight functions with zero callers, verified individually by grep and by a
  green suite + clean lint after removal: `charts.waterfall_usd` (deliberately cut
  from the UI per rec12 — it plotted the same top-10 twice; also carried a latent
  `KeyError` proving it was untested in practice), `charts.daily_count_bars`,
  `charts.event_timeline`, `components.budget_kpi`, `components.min_covered_days`,
  `overview._board_metric`, `main._scope_is_active` (a redundant wrapper superseded
  by `_active_filter_count`), and `query.telemetry_dropped_counts`. Their now-dead
  tests were removed and the few source-anchored tests updated. Deliberately KEPT:
  `sizing._unused_guard` (an intentional import keep-alive, `# pragma: no cover`),
  `query._batch_member_cache_clear` (a documented test/hot-reload hook), and
  `companies.classify_warehouse` / `insights.idle_suspend_sql` (small self-contained
  APIs a future feature could wire).

* **F5** "this is the current one" was said four different ways — the section pills
  filled with an accent gradient, the sidebar nav used a left rail, the tabs used
  BaseWeb's own default (no app accent), and the Window pills had no active-fill rule
  at all. They're now one accent-driven system: two shared tokens
  (`--ow-active-tint` / `--ow-active-bar`) feed every "active" cue, and each control
  keeps its shape-appropriate indicator — a filled segment for the pills (both the
  modern `segmented_control` and the radio fallback), a left rail for the nav list,
  an underline for tabs. Adversarial review caught a real legibility bug the fill
  exposed: the dark ink never reached the option's text node (a direct global
  `p,span` colour beat the label's inherited colour), leaving near-white text on the
  bright accent fill — the same pale-on-accent failure the primary-button rule was
  written to prevent; the dark ink is now forced onto the text node. Review also
  confirmed the fill was silently no-op'ing on the modern `segmented_control` (it
  only styled the radio fallback), now fixed. *Eyeball note: the segmented_control
  active-segment selector is BaseWeb-version-specific and can't be verified headless.*

* **C38** the chart mark grammar for data *provenance* is now named and reusable.
  A chart's marks say how trustworthy the number is: measured-and-complete renders
  solid; measured-but-provisional (the newest metering day, an in-flight month —
  the window closed but the data hasn't) dims to one shared `PROVISIONAL_OPACITY`
  via `_provisional_opacity(flag_field)`, so every chart's "partial, not a drop"
  reads identically (the magic 0.45 now lives in one place, applied at the spend
  trend and the monthly boss chart). This is a byte-identical refactor — the
  compiled Vega spec is unchanged. Adversarial review corrected an over-claim in
  the new grammar docs: it isn't true that *no* projected value is ever charted —
  Cost▸Optimize's storage-growth bars draw a least-squares projection
  (`GROWTH_USD_30D`) solid, told apart from measured spend only by their
  "Projected"/"Estimate" text; the docs now say so, and a background task tracks
  giving those bars a distinct modeled mark.

* **F60** a 0-1 confidence was shown three ways — a ProgressColumn bar on some
  decision boards, a raw float column on others, and a chips+caption badge on the
  Entity 360 header. The TABLE encoding is now one shared bar
  (`confidence_progress_column`): the Decision Studio portfolio + scenarios tables,
  the Action Center, and the Entity 360 "Work and outcomes" list all render
  confidence as a 0-1 ProgressColumn; single-value surfaces keep the
  `confidence_badge`. The authored-confidence help is one shared constant
  (`AUTHORED_CONFIDENCE_HELP`) across every surface that shows it, so its wording —
  and its honesty about provenance (operator *or* recommendation engine; a promoted
  security finding isn't literally operator-authored) — never drifts. Adversarial
  review confirmed the scenarios/work-list confidence is genuinely 0-1 (both write
  paths clamp to [0,1]) so the bar is correct, and the extraction is
  behavior-preserving; the Entity 360 work-list gap and the help-text precision were
  both caught and fixed from that pass (0 confirmed defects).

* **F59** "Watch" spoke three languages — a text button in Entity 360
  (Watch/Unwatch), a raw True/False column on the decision boards, and an 👁 eye
  badge on the Brief. It's now ONE filled-star affordance: **★ = watched,
  everywhere**. A cell/badge shows ★ only when on (a column of hollow stars is
  noise — the point is to spot the few watched rows); the interactive Entity 360
  toggle shows both states (★ Watching / ☆ Watch). Shared helpers
  (`watch_star` / `watch_toggle_label` / `watch_star_column`) drive the Decision
  Studio portfolio + scenarios tables, the Entity 360 toggle, the Brief badge, and
  the Watchlist board. The star conversion is display-only and happens after each
  board's watched-first sort, so pinning and the watched counts are untouched;
  the helper treats a NaN as not-watched (guarding `bool(nan) is True`). Adversarial
  review: 0 confirmed (7 findings, all out-of-scope polish or non-issues).

* **F46** the Decision-Studio portfolio scatter (impact × confidence, colored by
  lane) now draws the **confidence-axis lane gates** so a dot's vertical position
  helps explain its lane, not just its color. Only the confidence axis has
  axis-aligned gates — ACT NOW keys off a PRIORITY_SCORE percentile, **not** an
  impact threshold — so no vertical impact quadrant is drawn (it would misrepresent
  the lane). The gate values are shared named constants (`LANE_CONF_FLOOR`,
  `LANE_ACTNOW_CONF`) that the lane logic itself uses, so guides and colors can
  never drift. Adversarial review caught a real trap pre-ship: because confidence
  is built from run/cost evidence only, a high-cost family with missing behavioral
  evidence is forced to VALIDATE at *high* confidence — an amber dot sitting high on
  the axis. Fixed by renaming the axis to "Run/cost confidence" (it collided with
  the "Validate evidence" next-move), making the gate labels necessary-not-sufficient
  markers, and adding a caption that states the full lane rule (VALIDATE = low
  confidence *or* missing behavioral evidence).
* **F45 declined** — the waterfall chart it targeted (`waterfall_usd`) is dead code:
  it was deliberately removed from the UI (per "rec 12"; see the note at
  `cost_parts/spend.py`) because it plotted the same top-10 twice and its cumulative
  form misled. A background task tracks deleting the leftover function.

* **F44** the monthly "boss chart" (spend stacked by warehouse) now labels each
  month's **stack total** above the bar, so the primary per-period number is
  legible without hovering every segment and summing by eye. The label uses the
  house `_usd_fmt` (compact SI — "$742k"/"$1.24M" — once the total clears $10k,
  exact dollars below, which also keeps d3's milli-suffix away from any sub-dollar
  total), and the in-flight (partial) month's total dims to match its bar so a
  running total never reads brighter or more finished than the bar it caps. Daily
  stacked charts are deliberately excluded — 28+ bars would collide, and they
  already lead with a takeaway caption. Adversarial review: 0 confirmed (6 findings,
  all cosmetic; the `_usd_fmt` switch + partial-dim were adopted from that feedback).

* **F41** `daily_metric_line` tooltips, peak caption, and y-axis now carry the
  metric's unit. A dollar line's tooltip showed "742389.5" (no `$`, no separators)
  and a percent line "93.1" (no `%`) — the tooltip is the one place a reader reads
  the exact number, so it must be unit-spelled. A `unit` argument
  (`usd`/`credits`/`pct`/`sec`/`count`) drives the formatting across all ten call
  sites; omitting it keeps a bare number. Negative dollars read "-$1,234" (sign
  before the symbol), and a missing/degenerate day reads as an em-dash, never
  "nan"/"inf".
* **F48** the optional reference rule (e.g. an object's change date) is now
  labeled *on* the chart with a small text mark, instead of a bare dashed vertical
  explained only by a caption underneath — the two Operations change-impact charts
  drop their "Dashed line marks…" captions in favor of the on-chart label. A NaT
  change-date is guarded (`pd.notna`) so it draws no rule at an invalid position.
  Adversarial review (find→verify, 0 confirmed) — the NaT, non-finite, negative-$,
  and non-unique-index edges were all hardened pre-ship.

* **F42** the KPI-card sparkline no longer exaggerates tiny changes. It scaled to
  the series' own min..max, so *every* series filled the full height — a 99.1→99.4
  wiggle drew the same dramatic climb as a doubling. The domain now includes zero,
  so amplitude is proportional to the real change: a small change relative to the
  values reads as nearly flat, a genuine doubling still rises across the height.
* **F43** the KPI-card sparkline is now colored by its delta's trend polarity
  (matching the delta chip), not the card severity. Before, the spark took the
  severity hue, so a cost card trending up (a red delta) could draw a calm blue
  line *over* that red delta. One shared polarity source (`_delta_is_good`) now
  feeds both the delta chip and the spark, so they can never disagree; a card with
  no labeled delta keeps the severity tint (nothing to contradict). Adversarial
  review (find→verify, 0 confirmed) proved the refactor behavior-preserving and
  the chip/spark hues identical, including the `off`-delta neutral (`#94a3b8`).

* **F24** a gated action now READS as locked. A disabled control (a type-to-confirm
  Execute whose text doesn't match yet, or a dirty-check Save with nothing to save)
  dims to 0.55, takes a dashed ink-mute edge, drops its hover lift, and shows a
  not-allowed cursor — instead of looking like a live button that just ignores the
  click. Adversarial review caught two real bugs pre-ship: (1) a **disabled primary**
  button kept its full-saturation accent gradient (the primary rule's `border:none`
  + gradient beat F24 on source order at equal specificity), so it read as bright-
  but-faded — fixed by excluding `:disabled` from the primary styling so it falls
  through to the locked treatment; (2) `cursor:not-allowed` set on the disabled
  `<button>` is ignored by browsers (a disabled control isn't a pointer target), so
  it now also sits on the `.stButton` wrapper — deliberately WITHOUT
  `pointer-events:none`, which would have suppressed the `help=` tooltip that
  explains why the control is locked.
* **F17** KPI cards in a row no longer read bottom-ragged when one card carries a
  sparkline or extra delta line and its siblings don't — the card now has a
  min-height floor. (The first attempt used a flex-through-the-column stretch;
  adversarial review proved it INERT — `height:100%` resolves against Streamlit's
  auto-height markdown wrappers and does nothing — and a min-height floor was
  adopted as the robust, render-verifiable alternative.)

* **F9** a "Group ▸ Page ▸ Section" breadcrumb kicker above the page title, so a
  deep-link landing (from the palette or a cross-page drill) says where it is
  above the fold — not just the page name. The section comes from the page's
  remembered section label, so it matches the pills below it.
* **F6** the ACCOUNT_USAGE lag caption now prints only on the metering surfaces
  that actually read lagging data (Cost, Operations, Security, Overview, Control
  Room, Brief) instead of on every page — so it registers where it matters
  instead of being ignorable chrome everywhere.

Adversarial review confirmed 2 findings, both fixed pre-commit:

* **MED** F6 had dropped the note from Control Room (Pulse/Triage live-fallback
  to QUERY_HISTORY / TASK_HISTORY) and Brief (its headline MTD credit spend is
  FACT_METERING_DAILY, up to 24h behind) — both surface lagging data
  prominently, so they were added back to the gated set.
* **MED** F9's section segment was one rerun stale — it read the ?section= query
  param that lazy_sections writes AFTER page_header (and shares across pages), so
  a fresh nav/drill showed the previous page's section or dropped it. It now
  reads the page's own remembered section session key (authoritative before the
  bar renders), defaulting to the first section on a fresh visit.

## 4.318.0 - UI/UX Wave 2: write polish (F54 + F58) (2026-08-28)

* **F58** a plain-English effect line above every operator write's SQL preview,
  derived from a diff of the form against the current row: the Action Center
  "Save work item" names its status / owner / due / defer / comment changes,
  and the Decision Studio experiment save names its ledger consequence ("book
  $X to the savings ledger").
* **F54** that same diff IS the dirty check: the Action Center Save is disabled
  when nothing changed and no comment is entered, so a no-op save can no longer
  write an empty audit row ("No changes to save yet — edit a field or add a
  comment").

Adversarial review confirmed the effect line was making PROMISES the stored
proc can't keep — all fixed pre-commit so the line only claims what the write
actually does:

* SP_ACTION_LIFECYCLE uses COALESCE-keep semantics, so a BLANK owner and a
  toggled-OFF defer are silent no-ops — the line no longer offers "unassign" or
  "clear the defer" as savable effects (a follow-up migration will add explicit
  clear paths so those operator actions actually work).
* An undated row's Due field defaults to today+7; the save now sends NULL for a
  previously-undated row the operator didn't set, instead of stamping a
  fabricated due date the caption never mentioned.
* The experiment effect line claims a status move only on a real transition, and
  reserves "— audited" for the settle path (VERIFIED/REJECTED/ROLLED_BACK write
  an activity row; an in-flight save is a plain UPDATE with no audit trail).

## 4.317.0 - UI/UX Wave 2: table-layer polish (F32/F33/F35/F36) (2026-08-28)

Four low-risk refinements at the shared table layer (`_render_table`):

* **F32** true-zero numeric cells are muted (present-but-quiet grey) so the
  non-zero values carry the eye on a sparse operational table — the em-dash
  NULL treatment, applied to 0. Skips status/delta columns (they own their
  color) and the progress-bar column.
* **F33** column width by name convention — status / severity / short-ratio
  columns stay small, free-text columns (detail, title, note, sample) get
  room — so wide tables stop laying out raggedly.
* **F35** small (4-10 row) ranked tables keep their order/window provenance
  ("by $ desc · last 30d") — the row count is dropped (redundant on screen),
  the ordering context is not.
* **F36** every download button states the CSV-is-raw contract, so nobody
  reconciles a rounded on-screen figure against the exact CSV.

Adversarial review confirmed 3 findings, all fixed pre-commit:

* **MED** F32 was a SILENT no-op — st.dataframe paints cells on a canvas that
  can't resolve `var(--ow-ink-mute)`; every Styler color path uses a literal
  hex. Now returns `palette.INK_MUTE` (the same idiom as status/delta color).
* **MED** F33 mapped `*_ID` to small, which ellipsis-truncated the 36-char
  Snowflake QUERY_ID / SESSION_ID that are the deliberate drill targets (and
  the pinned lead column on the heaviest-queries table). Dropped `_ID` from
  the small rule — short numeric ids auto-size fine.
* **LOW** F35 printed the ordering basis twice on decision boards (they carry
  their own adjacent caption) — `decision_rows` now suppresses the auto caption.

## 4.316.0 - UI/UX Wave 2: desktop master-detail for Alerts (C42) (2026-08-28)

The alert triage surface joins the master-detail layout: the open-events feed
renders LEFT and the selected event's drawer RIGHT (was stacked top-to-bottom).

* The open-events `@st.fragment` now wraps its feed + drawer in the shared
  `st.container(key="ow_md_alerts")` + `st.columns((1.15,1.0))`, so it restacks
  on narrow viewports with every other master-detail surface (the C47 CSS
  already covered it). A pure render-location split — every F51/C44/C48 guard
  (identity binds, nonce resets, deep-link arming, next-up queue, write latches)
  is unchanged session state, verified by an adversarial review that confirmed
  correct nesting depth, preserved scope, and intact guards.
* The bulk panel and the snoozed tray stay full-width below the columns; the
  storm/rollup path still renders full-width (it early-returns before the split).

Review polish, all fixed pre-commit: the drawer's leading `st.divider()` (a
stacked-flow separator) became an orphaned rule at the top of the right pane —
removed (the column edge separates now); the empty-drawer placeholder showed
"select an event" even in bulk mode where there is no drawer — gated to single
mode; and the feed no longer duplicates the "open its drawer" prompt (the drawer
pane owns it, the feed keeps only the bulk tip).

## 4.315.0 - UI/UX Wave 2: shared master-detail (C47) (2026-08-28)

Ranked work LEFT, the selected item's editor RIGHT — a desktop-console layout
that replaces the stacked list-then-detail on Action Center and Decision Studio
experiments:

* **`components.master_detail`** is the shared primitive: it owns the column
  split and the fragile positional-selection → stable-id → sticky-persistence
  dance, while each caller keeps its own table flavor (list_render_fn) and
  editor body (detail_render_fn). Selection binds by IDENTITY (id_col) via a
  rec29 seen-guard, so a re-sorting list can't rebind the detail to the wrong
  row; a deep-link preselect is one-shot and clears the sticky selection.
* **Action Center** and **Decision Studio experiments** now render through it
  (DS upgrades from raw positional selection to EXPERIMENT_ID identity). The
  C48 write latches still key off the row id; rec17 "no silent row-0" is
  preserved by the empty-detail hint.
* **Narrow-viewport restack**: every master-detail surface wraps its columns in
  a `st.container(key="ow_md_*")`, and one `@media(max-width:1180px)` rule in
  theme.py restacks them full-width (Streamlit columns don't auto-stack until
  unusably narrow). This same convention is ready for the Alerts feed/drawer
  split (C42, next).
* Docs: ARCHITECTURE.md carries the master-detail + restack contract.

Adversarial review (3 dimensions) confirmed 4 selection defects, all fixed
pre-commit — the HIGH: the sticky positional selection re-emits every rerun, so
an unconditional position→id resolve rebound the detail to whatever row landed
at that index after a re-sort (defeating the primitive's own identity claim).
The rec29 seen-guard now resolves only on a genuine new click; a deep-link
preselect wins and clears the sticky selection; the deep-link is consumed
one-shot so a manual click sticks and a repeat nav to the same id re-focuses it.

## 4.314.0 - UI/UX Wave 2: Operator vs Audit presentation modes (C19) (2026-08-28)

A per-viewer presentation mode, stored like the density pref:

* **Operator** (the default) keeps the daily-triage surface lean; **Audit**
  shows the full evidence chain for reproducing or defending a number.
* One seam does most of the work: `result_caption()` always shows the SOURCE
  (the app's "show the source" ethos is load-bearing) but trims the per-panel
  fetched-at stamp and methodology note in operator mode. `audit_mode()` /
  `methodology_note()` gate the how-computed / reconciliation / backtest
  blocks (4 expanders gated: spend "Why totals differ" + "Cost coverage
  ladder", overview "Forecast accuracy", admin "Object-cost ledger
  reconciliation").
* Toggled from the sidebar ("Audit detail"), persisted per-viewer as
  `PRESENT_MODE` in `USER_PREFS`. The pref-write MERGE
  (`prefs_sql.upsert_pref_sql`, identity-scoped + allowlist-gated) returned for
  it — it had been retired with the density toggle in v4.157.
* Docs: ARCHITECTURE.md + CLAUDE.md carry the presentation-mode contract.

Adversarial review (3 reviewers independently) caught a destructive bug fixed
pre-commit: reading the toggle in the render body and diffing against the mode
latched a pre-hydrate "operator" into the widget key, then OVERWROTE a
returning auditor's saved "audit" pref back to operator on the hydration race.
Persistence now fires only via on_change (a genuine user flip), and the
hydrate seeds the toggle's widget key so a late "audit" hydration flips it
instead of being clobbered.

## 4.313.0 - UI/UX Wave 2: finish the command palette (C3) (2026-08-28)

The sidebar "Jump to" palette becomes a real command palette:

* **Enter-to-go** - picking a destination navigates immediately; the separate
  "Open destination" button is gone. The box remounts empty under a bumped
  nonce after every jump, so a retained selection can never re-fire and a jump
  to the page you're already on can't strand a stale selection.
* **Recents strip** - the destinations this session jumped to render as
  one-click buttons above the box (session-only, capped, filtered to
  still-valid options).
* **ID lookup folded in as a mode** - the Investigate type + field lives in the
  same palette and navigates on Enter (value change), no separate button.

Adversarial review (3 probes) confirmed and fixed pre-commit: switching the
lookup Type while the field held leftover text re-fired navigation against the
stale value (now dispatches on a VALUE change only, under the current kind);
the page-to-self no-op left an un-re-jumpable stale selection (the nonce
remount + explicit rerun clears it). Also fixed a stray control character in
the dedupe key the build caught.

## 4.312.0 - UI/UX Wave 2 (7): the quick cluster (C37 + C15 + C16 + C18) (2026-08-28)

Four small orientation/trust wins in one batch:

* **C15** the header scope chip shows RESOLVED calendar boundaries - "Current
  month (Aug 1 - Aug 28)" instead of the raw day-offset chip that read "27d"
  for a month window (lossy AND wrong-looking). Rolling windows keep "30d".
* **C16** section pills badge decision-bearing counts with ZERO extra queries:
  a section parks its computed count in a scope-keyed stash the bar reads -
  Operations "Tasks (n)" (failure streaks + late-vs-cadence), Security
  "Decision queue (n)" (open exceptions), Decision Studio "Experiments (n)"
  (running/observing).
* **C18** the Cost "since your last visit" opener is a shared component on
  FIVE surfaces (Cost, Alerts, Security, Decision Studio, Control Room >
  Action Center) from one cached query, now with profile-gated one-hop jumps
  to the changed items (Open events / Action Center), skipping the page you
  are on.
* **C37** additive cost tables carry a reconciliation footer (visible sum ·
  expected parent · variance · coverage) - the spend service-coverage table
  reconciles against the section's own billed KPI; sum-only where no
  independent parent exists (never a fabricated ratio). The chargeback
  department table's totals caption upgraded into the footer.
* Docs: FEATURE_GLOSSARY updated for the opener's five surfaces, the jump
  buttons, and the footer.

Adversarial review (3 find dimensions, 17 agents) confirmed 12 findings
(deduped to 8), all fixed pre-commit:

* **MED** the badge stash keyed only (company, days) while the Operations
  Tasks count honors database/schema filters too - a filter flip served the
  badge stale. Each stash now DECLARES the dims its count varies with
  (Tasks: all four; Security queue: company only; Experiments: none), fixing
  both staleness and needless invalidation, plus a 900s TTL and a post-write
  sweep (a write that drains a counted queue drops every badge so the next
  paint renders unbadged, never one run behind).
* **LOW** the Tasks badge required BOTH feeds (half the evidence must not
  badge); the actions-only overnight had a dead Action Center doorway (jumps
  now gate on quiet, not severity); two self-referential footers (parent
  summed from the SAME frame = tautological 100%) became sum-only; the spend
  attribution table's duplicate sum line removed; the C25 ceiling for cost.py
  tightened to its new count.

## 4.311.0 - UI/UX Wave 2 (6): the empty-state conversion (C25 + F56) (2026-08-28)

Absence now speaks ONE vocabulary app-wide, so color carries meaning:

* **empty_state gained 'unavailable'** (red lead line, full error one click
  away via a detail expander) and an optional NEXT BEST ACTION (a small button
  under the message) - an empty panel is a doorway, not a dead end.
* **guard() routes BOTH branches through the vocabulary**: the empty branch
  defaults to the quiet no_data_yet caption (a successful zero-row read is not
  a setup prompt - the old blue st.info banner said otherwise) with
  kind="clean" for verified-good empties; the error branch renders as
  'unavailable' (absence-of-setup stays a calm needs_setup). One seam change
  converts ~145 guard callers.
* **131 raw st.info/st.success absences converted** across 16 UI files by a
  10-agent classify-and-convert sweep (69 verified-clean green rows, 36 quiet
  captions, 26 setup prompts); 31 receipts/context notes deliberately stayed
  raw; 8 guard callers marked verified-clean.
* **F56**: the Watchlist empty offers "Browse the catalog" (jumps to the
  Entity 360 catalog) and the Experiments empty offers "Open Action Center"
  (operator-gated - viewers get accurate read-only wording).
* **Docs now carry the contracts**: ARCHITECTURE.md's error-handling contract,
  CLAUDE.md laws 8/11, and AGENTS.md document the empty-state vocabulary AND
  the v4.310 C48 write seam (latch pairing rule, arm-on-open, run-seq,
  scoped keys); FEATURE_GLOSSARY's Brief render descriptions updated.

Adversarial review (4 find dimensions, 23 agents) confirmed 15 findings
(deduped to 10), all fixed pre-commit:

* **MED** guard now SUPPRESSES the setup hint under a verified-clean empty - a
  successful read proves setup exists, so "verified clean" + "not installed
  yet" was a contradiction on the alerts triage surface.
* **MED** two kind="clean" miscalls reverted: the compile-heavy-families probe
  (renders under "Why is it elevated?" while the anomaly is still open - a
  failed diagnosis is not a green all-clear) and the account-wide pattern-
  movers leg (its empty also covers a not-yet-loaded mart; clean is now
  reserved for the live per-warehouse scan).
* **MED** Ask's no_data headline reverted to info weight - it IS the answer to
  a typed question (conditional feedback), not a panel absence.
* **MED** admin's failed drill-history read now renders as 'unavailable'
  instead of a calm blue info with the raw error inline.
* **LOW** the blast-radius empty split: the zero-rows fact keeps the quiet
  caption, but the safety caveat ("not proof nothing depends on it, not a
  'safe to ALTER' verdict") keeps its info weight - it gates a destructive
  decision.
* **LOW** the Experiments doorway is operator-gated; doc wording gained the
  receipts/answers/notes carve-out; the raw-absence regression test now pins
  ALL 16 swept files at their post-sweep counts.

## 4.310.0 - UI/UX Wave 2 (5): C48 app-wide in-flight action state (2026-08-28)

Every operator write in the app now shows an honest in-flight state and is
protected against duplicate-click double-execution. Built from a 10-agent
audit that cataloged all 42 execute_* call sites (32 click blocks) and their
duplicate-click exposure - 12 HIGH (double-booked SAVINGS_LEDGER rows,
duplicate ACTION_QUEUE items, a second incident under a fresh uuid).

* **In-flight state at the one seam every write crosses**: execute_statement /
  execute_action / execute_cancel_query wrap their round-trip in a spinner
  ("Executing write..." / "Executing action..." / "Cancelling query...") - the
  initiating button freezes for the round-trip and the spinner is what the
  operator watches instead.
* **Duplicate-click latch on all 32 write click blocks**: write_gate_open(key)
  as the click gate's last condition + stamp_write(key, ok) after the block's
  last write (always before any st.rerun). A swallowed duplicate gets an
  explanatory toast ("That action just ran - click again to repeat it") -
  never a silent nothing, never an error banner.
* Bonus surgical fix from the audit: the declare-incident loop now stops at
  the first failed statement (INCIDENT_MEMBERS never runs after a failed
  INCIDENTS insert).

Two adversarial review rounds hardened the mechanism (18 + 3 agents; 10 + 9
confirmed findings, all fixed pre-commit):

* **HIGH (round 2)** a duplicate click on a non-fragment page PREEMPTS the
  running script at its next yield point - after the Snowflake write committed
  but before any end-of-block stamp - so an arm-at-end latch never armed in
  exactly the scenario it exists for. The gate now ARMS THE LATCH ON OPEN
  (before the write); stamp_write settles it: refreshed on success, DELETED on
  failure so a failed write's natural retry re-executes.
* **HIGH (round 1)** wall-clock grace alone loses to the write's own global
  cache invalidation (the queued duplicate lands on the NEXT run however long
  its cold render takes) - the latch is run-sequence aware: main() bumps a
  full-run counter (fragment reruns deliberately do not), and the seq clause
  is the load-bearing swallow with wall-clock as the impatient-reclick
  backstop and a 120s stale-latch bound.
* **HIGH (round 2)** the emergency tab runs inside a fragment/dialog where the
  run seq never advances - a bare "emg" latch key would have locked out EVERY
  subsequent emergency action for 2 minutes after one success. The lever key
  now scopes by lever+target, and both emergency surfaces carry a short 15s
  backstop (idempotent levers/cancels, back-to-back actions under pressure).
* **MED** fixed keys swallowed genuinely distinct actions: the alerts decide
  bar now scopes by event+action (ack -> investigate -> resolve on one event
  flows freely), the kill-switch by query id, budget/mapping/company-scope
  saves by target, the un-snooze by selection, the AI-hypothesis save by
  content, and the entity watch toggle encodes its direction in the WIDGET key
  (the label flip drops a queued phantom click at element-identity level)
  plus a direction+entity latch key.
* **MED** stamps are success-only and the latch is a per-key dict (an
  interleaved write on another block can never evict a pending swallow);
  malformed entries from older sessions parse defensively.
* Accepted residuals (documented): a mid-write preemption that loses the
  aborted run's audit/ledger follow-ons while the primary write stands; a
  deliberate same-key re-click on the immediately-next run is swallowed once
  (the toast names the recovery); the declare-incident INCIDENTS insert stays
  unguarded (a NOT EXISTS guard would orphan members on the shared-uuid
  no-op path).

## 4.309.0 - UI/UX Wave 2 (4): alerts triage momentum (2026-08-28)

Fourth Wave-2 batch of the UI/UX master list (C44 + F50 + F57 + F52), app-only -
the alert drawer becomes a triage QUEUE, not a series of separate visits:

* **C44** a RESOLVE/SNOOZE advances to the NEXT open event automatically: the
  write queues the next event by IDENTITY (never position) and the receipt names
  it ("... - next: [HIGH] ..."); the queued event rides the existing deep-link
  arming machinery, so every F51 protection (identity guard, nonce reset,
  armed-per-arrival) applies unchanged.
* **F50** the live re-check verdict now PERSISTS per event (it used to vanish on
  the next rerun, the moment the operator touched the decide bar) - and a CLEAR
  verdict gains a one-click "Resolve as ACTIONED with this evidence" that
  prefills the decide bar (RESOLVE + ACTIONED + the measured numbers as the
  audit note), applied at the fragment top before widgets mount.
* **F57** the drawer's four supporting reads (rule config, deliveries, 90-day
  history, prior resolutions) submit as ONE async batch under a named spinner
  ("Assembling event context...") instead of four serial round-trips.
* **F52** a snooze is a visible timer: the picker shows the computed wake time
  ("- wakes ~Wed Sep 04, 09:15 account time") and the snoozed tray gains a
  WAKES_IN countdown column, soonest wake first.

Adversarial review (3 find dimensions - 15 verify agents) confirmed 14 findings
(deduped to 7 fixes), all fixed pre-commit:

* **HIGH** the C44 advance was DEAD after any deep-linked arrival: the nav
  context's event_id is read without consume and nothing ever cleared it, so it
  shadowed the queued next-up on every later run while the receipt kept
  promising an advance. RESOLVE/SNOOZE now consumes the spent drill identity
  (ACK keeps it - the drawer intentionally stays open).
* **MED** re-queuing the same event id after a nonce bump stranded the advance
  (the applied-gate only re-arms on signature CHANGE) - the next-up signature
  now folds in the arrival epoch (the selection nonce).
* **MED** the persisted verdict had no expiry and a time-of-day-only stamp - a
  days-old CLEAR resurfaced as fresh with a live one-click writing the stale
  measurement into the audit note. The verdict now dies with the write, stores
  the full datetime, and the one-click is freshness-gated at 30 minutes (older
  CLEARs render "re-check again before resolving").
* **MED** a NULL CURRENT_VALUE (idle warehouse, zero-row window) coerced to
  0.00 and rendered "Condition clear: 0.00" with the one-click offering to
  audit the fabricated number - NULL now routes to the not-evaluable branch,
  plus a render-side NaN guard for stale entries.
* **LOW** queue hygiene: a missed funnel (real deep-link superseding it, the
  queued event leaving the feed, bulk mode, or a page switch) now POPS the
  queue instead of leaving it to surprise-open a drawer arbitrarily later.
* **LOW** multi-day snooze copy: the wake caption showed only "%a %H:%M" (a
  1-week snooze lands on the SAME weekday - it read as later today) and
  WAKES_IN rendered "167h 59m"; the caption now carries the date past a day
  out and WAKES_IN rolls into day grain ("6d 23h").
* **LOW** the persisted re-check ERROR verdict claimed "unavailable right now"
  indefinitely - now stamped and rendered "as of HH:MM".

## 4.308.0 - UI/UX Wave 2 (3): Snowsight links everywhere they belong (2026-08-28)

Third Wave-2 batch of the UI/UX master list (C35 + F27), app-only:

* **C35** the table layer AUTO-attaches the Snowsight query-profile link to any
  frame carrying real QUERY_IDs - the owner's most-praised affordance stops
  depending on per-site wiring (the ~17 manual call sites no-op, the CSV gains the
  same PROFILE column they already export, and blank-id/unresolved-context frames
  degrade to no link, never a dead one).
* **F27** warehouses, databases and 3-part table objects get an outbound "Open in
  Snowsight" jump on Entity 360 - the native-console complement to the in-app
  drill. Other kinds (fingerprints, tasks, users) have no stable Snowsight page
  and render no link.
* The org/account context probe is now ONE shared helper serving both link kinds,
  and the focused review's two findings are fixed: a failing probe backs off for
  five minutes instead of re-firing (and error-logging) per table per rerun
  app-wide (probe=True keeps APP_ERROR_LOG clean; R3-3's never-pin-a-failure
  intent survives as a retry window), and every URL path segment is
  percent-encoded with the Entity-360 link rendered via st.link_button - a
  quoted identifier or hostile entity key can neither break the URL nor smuggle
  live markdown into the operator console.

8 regression tests in test_uiux_wave2_links.py; 2 relocated locks updated.

## 4.307.0 - UI/UX Wave 2 (2): ranked tables read at a glance (2026-08-28)

Second Wave-2 batch of the UI/UX master list (F26 + C34's rank half), app-only and
central - every ranked table in the app benefits:

* **F26** the primary dollar column of a RANKED table draws a native in-cell
  magnitude bar, so "where's the weight" reads at a glance instead of from a wall
  of flat numbers. The picker is deliberately conservative: USD-first (a credits
  column can be non-monotonic against the declared "$ desc" order when the AI and
  standard credit rates coexist - the review caught the Spend coverage table doing
  exactly that), and it never bars rates ($/TiB, credit price), signed movement
  columns, sparse settlement columns whose leading rows are NULL (an Experiments
  board's VERIFIED_USD would have rendered active work as empty zero-looking
  bars), or mixed-time-base tables carrying a PERIOD column (a shared 0..max bar
  is exactly the cross-row comparison those boards forbid). The bar's format
  leaves the Styler map so the two never fight over the cell, and the
  which-dollar-is-this column help survives.
* **C34 (rank half)** a ranked table shows a display-only pinned `#` ordinal -
  "the 3rd heaviest" becomes nameable (the owner's own pain: "I don't know the
  query id to select"). The CSV keeps raw columns and row order is untouched, so
  positional selections stay valid; the wide-table identity pin now counts the
  ordinal toward display width (the review caught a 7-raw-column table freezing
  rank without identity).
* The Decision Studio cost-truth "lenses" table explicitly opts out - its four
  rows are non-comparable bases ("do not add"), so it takes neither an ordinal
  nor a credits race bar, preserving the DS #4 No-evidence discipline.

Focused adversarial review confirmed 5 findings, all fixed pre-commit. 11
regression tests in test_uiux_wave2_tables.py.

## 4.306.0 - UI/UX Wave 2 (1): alert decide-bar, bulk mode, verdicts, one-hop return (2026-08-28)

First Wave-2 batch of the UI/UX master list (F49 / C43+F53 / C17 / C9), app-only:

* **F49** the DECIDE bar leads the alert drawer - Investigate/Generate-fix +
  ack/resolve/snooze + note + Execute render directly under the event's title and
  detail; the six evidence panels (playbook now collapsed, re-check, rule history,
  prior resolutions, closed-loop Respond, AI) follow under a "Supporting evidence"
  heading. Triage stops requiring a full-drawer scroll per event.
* **C43+F53** bulk acknowledge/resolve runs off in-table selection: an operator-only
  "Bulk select" toggle flips the feed into checkbox multi-row mode (single-click
  drawer triage stays the default gesture) and the selected rows arm a bulk panel
  showing the severity mix and the exact rows above the typed confirm gate; the old
  duplicate multiselect picker is deleted. `selectable_table` grew a clamped
  `multi=` mode.
* The adversarial review of the batch confirmed 9 findings, all fixed pre-commit:
  the bulk SET now binds by identity like the drawer (a feed shift under checked
  rows disarms the typed gate instead of silently re-targeting the write), bulk is
  never promised to viewers who cannot execute it, the re-check copy points at the
  decide bar above, the empty-frame multi return honors the list contract, and
  bulk-mode state can never leak across mode flips (mode-suffixed table key).
* **C17** data-derived "should I worry?" verdict lines: Alerts renders one at page
  level from the uncapped severity counts (before the section bar, never from a
  failed read), and Security composes one from the decision-queue posture - whose
  section header severity is now also data-derived instead of static amber.
  (Control Room / Brief / Cost / Overview already had verdicts; Decision Studio's
  scorecard carries proof_verdict; Operations needs a page-level aggregate first.)
* **C9** one-hop contextual return: a cross-page drill captures its origin (page +
  active section), and the destination's header offers "<- Back to Alerts - Open
  events" - valid only while on the jump's destination (wandering off drops it),
  consumed on use, and the return itself never creates a boomerang origin. The
  origin section's own drill selection survives naturally in session state.

## 4.305.0 - UI/UX Wave 1 leftovers: data-derived severity, quieter chrome (2026-08-28)

Completes Wave 1 of the UI/UX master list (C23 / C13 / C24 / F31), app-only:

* **C23** section-header severity now derives from the section's OWN data via a new
  `components.alarm_health()` (amber only when there ARE findings, verified-clean
  green, neutral when the read didn't resolve — never a false alarm or a false
  all-clear). Rewired: Security MFA gaps + single-factor logins, Operations failures
  by error / RCA timeline / file-load failures / duration drift / predicted SLA miss
  / failure streaks / task freshness / resource-monitor coverage (calm under an
  account-level cap), Cost unmapped entities ("empty is the goal state" now reads
  green when it is), Brief Fires. Toggle-gated TOOLS (optimization triage,
  stored-proc regression, wasted spend) go neutral; the kill-switch and Emergency
  levers stay DELIBERATELY amber (dangerous controls, not findings counts).
* **C13** the blue filter-contract banner renders only when it prevents a misread -
  a sharp active filter (warehouse/database/schema/user) the section ignores or
  applies panel-dependently; otherwise the contract is a quiet caption. Company/days
  always carry a value, so they never trigger the banner alone (the judgment
  `section_scope_note` already codified).
* **C24** verified-clean renders as the compact `.ow-exception--ok` row instead of a
  full-width green banner - a page of clean sections reads as a quiet checklist,
  while clean stays visually distinct from unavailable.
* **F31** routine row-cap truncation is a quiet caption, not a yellow alarm over a
  table working as designed.

## 4.304.0 - UI/UX Wave 1: severity, a11y, and chart legibility (2026-08-25)

The first wave of the UI/UX master list (docs/reviews/UIUX_MASTER_LIST_2026-08-25.md
- Codex's 50 recommendations ground-truthed against the code + 60 fresh, triaged
into waves). This batch = the highest payoff-to-effort visual/severity/a11y/chart
items, app-only:

* **F13** the resting KPI stripe is neutral on BOTH KPI surfaces, so a colored
  stripe always MEANS severity (the default accent gradient looked semantic on
  every card); one KPI value size (1.55rem) on both.
* **C22** hover elevation removed from non-interactive cards - motion no longer
  implies clickability nothing has.
* **F14** one keyboard-focus grammar on every interactive control (buttons, nav
  radios, selects, inputs, toggles) - Tab users kept the indicator only on the
  section pills before.
* **C28/F21** the KPI help badge is a 24px target and its tooltip right-anchors +
  clamps to the viewport (it clipped exactly on the right-edge $ cards).
* **C4/F22** the brand dot's pulse now MEANS "connected to Snowflake" (static grey
  when disconnected), and the gradient wordmark carries a solid-ink fallback behind
  an @supports gate (it could render as a transparent hole).
* **F19** section-header severity reaches the icon and count badge, not just the
  3px stripe. **F16** one severity hue map: INFO is slate in tables AND charts
  (blue stays the in-motion lifecycle hue). **F29** decorative sparklines are
  aria-hidden.
* **F37/F38** the task DAG highlights critical-path EDGES (the longest path reads
  as one continuous accent route, not scattered dashed boxes) and gains a static
  color legend - hues substituted FROM the palette so the key cannot drift.
* **F39** dollar axes compact to SI ("$1.24M") once magnitude crowds the gutter;
  tooltips keep cent precision. **F40** day-grain hovers show the day, not
  "12:00:00 AM". **F47** the hour heatmap pins all 24 clock columns, so idle hours
  read as gaps in their true position instead of sliding the shape left.
* **F28** byte-column headers stop contradicting their self-humanizing cells (a
  "(GB)" header over "512 MB" cells). **F25** metric-registry column help now
  reaches caller-configured columns - billed/measured/allocated were exactly the
  ones losing their "which dollar is this" hover.
* **F1** sidebar nav labels and page H1s now read identically (Brief, Security) -
  the click target is the "you are here" anchor.

App-only, no migration. 24 new regression tests across three Wave-1 test files.

## 4.303.0 - Alert drawer binds by event identity (2026-08-25)

UI/UX master list **F51** — an interaction-correctness fix, not polish. The
open-events drawer selected by a sticky POSITIONAL `st.dataframe` index; when the
live feed shrank or reordered under it (a resolve here, the V091 auto-resolver, a
filter change), the same index silently rebound the drawer to a DIFFERENT event —
whose ACK/RESOLVE/SNOOZE controls were then live against the wrong target. Now the
drawer binds by EVENT IDENTITY (pure `_stale_rebind` drops a same-index/changed-event
rebind), and EVERY selection/write widget in the fragment — the feed, the storm-rollup
selection, the drawer's action/note/kind/snooze/confirm, the bulk picks — mounts under
a generation NONCE, because popping session keys is not a reset (the frontend re-sends
values by element id). An adversarial review of the first cut confirmed 13 follow-on
defects, all fixed: the receipt/feed-shift notice render BEFORE the guard (resolving
the LAST event shows its receipt instead of stranding it to resurface stale); the
deep-link identity fallback is armed per arrival and disarmed by a write's nonce bump
(it used to re-fire every paint, reopening the acted drawer) and bypasses the
positional-staleness guard; a guard trip parks its notice and RERUNS so the fresh
table mounts in the same interaction; an orphaned bind never outlives its selection;
ACK keeps the drawer open (the event stays in the feed — triage continues to resolve)
while RESOLVE/SNOOZE reset it; and every write triggers the full rerun `notify()`'s
contract already required, so the re-read feed is the durable receipt. App-only, no
migration.

## 4.302.0 - Cost-per-consumer & retirement candidates (2026-08-25)

Upgrade Board P1 #28. Decision Studio ▸ Products gains a "Cost per consumer &
retirement candidates" block below the coverage KPIs: for each data product, its
object-attributed cost, the distinct consumers who actually read it, cost-per-consumer,
the reads trend (recent window vs the equal window before), and an advisory
RETIREMENT_VERDICT. New builder `workbench_sql.product_consumer_reads` (consumer reach
+ reads trend from ACCESS_HISTORY.BASE_OBJECTS_ACCESSED — readers, not writers/ETL —
mapped to products through the catalog exactly as object cost is; company-scoped via
the catalog CTE, never a per-row COMPANY_FOR_USER on ACCESS_HISTORY) and pure logic
`insights.product_retirement` (COST_PER_CONSUMER guards 0 consumers to NA not inf;
verdict is evidence-gated — INSUFFICIENT_DATA when usage can't be measured or is too
sparse, RETIRE_CANDIDATE only for costly products with usage gone or collapsing).
Enterprise-only (ACCESS_HISTORY) and probe-gated: on Standard the block degrades to
INSUFFICIENT_DATA rather than a false "unused". App-only, no migration.

## 4.301.0 - Object lineage downstream blast radius (2026-08-25)

Upgrade Board P1 #19. Entity 360 gains a "Downstream blast radius" panel for OBJECT
entities (inside the evidence gate): for a chosen table/view, its DECLARED downstream
dependents (ACCOUNT_USAGE.OBJECT_DEPENDENCIES — the objects that reference it,
transitively) paired with the OBSERVED consumers who actually touch those dependents
(ACCESS_HISTORY, last 30d), answering "if I ALTER this — or it breaks — what depends
on it?". New builders `graph_sql.object_dependency_edges` (flat edge list) +
`object_blast_consumers` (per-object queries/users/read-write split), and a new pure
module `app/logic/lineage.py` doing the transitive BFS (`downstream_dependents`,
cycle-safe, depth-capped), the consumer merge (`build_blast_radius` — an un-queried
dependent is NOT-MEASURED, never a measured 0) and headline counts (`blast_summary`).

Honesty by construction: the declared graph misses stored procs / dynamic SQL, so it
is deliberately paired with the observed half — the panel says "a count of what
depends on this", never a "safe to ALTER" verdict. Both sources probe-gated
(OBJECT_DEPENDENCIES unverified on this account; ACCESS_HISTORY Enterprise-only) and
degrade independently. App-only, no migration.

## 4.300.0 - Object-tag governance coverage (2026-08-25)

Upgrade Board P1 #20. Security ▸ Decision queue ▸ Operational governance gains an
"Object-tag governance coverage" panel: for each governance tag
(COST_OWNER / SENSITIVITY / SERVICE_TIER / APP_OWNER), the share of in-scope base
tables that carry it, a pooled 0-100 coverage score (Healthy ≥90 / Watch ≥75 / Act),
a per-tag breakdown, ranked coverage gaps, and an untagged-table worklist (biggest
first). New builders `security_sql.object_tag_coverage` / `untagged_objects` /
`object_tag_probe` (ACCOUNT_USAGE.TABLES as the verified denominator LEFT-joined to
TAG_REFERENCES; TABLE domain only — this account exposes no warehouse/database
inventory view) and pure scorer `governance.tag_coverage_score` (empty inventory
reads "No data", never a false 100/Healthy). Probe-gated so an account/edition
without TAG_REFERENCES degrades to an honest hidden panel. This is object-tag
governance, distinct from the existing query-tag coverage on Cost. App-only, no
migration.

## 4.299.0 - Exfiltration composite behavioral score (2026-08-25)

Upgrade Board P1 #18. Security ▸ Egress gains a toggle-gated "Score unload events
for exfiltration risk" panel. New per-event builder `security_sql.unload_risk_events`
(sibling to `unload_activity` — keeps each COPY INTO <location> as its own row with
the account-local hour/weekday and a per-user `MEDIAN(GB_OUT)` baseline) feeds a new
pure scorer `insights.egress_exfil_severity`. Four auditable sub-signals — unusual
volume (absolute floor OR user-relative spike), off-hours, personal/ad-hoc
destination, human (non-service) principal — fuse into a transparent 0-100 SCORE +
High/Medium/Low SEVERITY + a REASON enumerating every factor that fired. The
human/service split is a GATE, not a weight: an ETL/service role is capped at Low, so
a routine nightly bulk export can never rank as an incident. App-only, no migration;
same company/db/schema scoping and canary coverage as the sibling builder.

## 4.298.0 - Budget burndown chart (2026-08-25)

Upgrade Board P1 #57. Overview gains a "Budget burndown" chart: cumulative actual
spend this month vs the straight-line budget target per day-of-month — the
CFO-friendly "are we pacing over budget" view, alongside the existing pace-variance
KPI. New pure `formulas.budget_burndown(daily, budget, today)` returns
DAY / CUM_ACTUAL_USD / BUDGET_LINE_USD for the current calendar month; budget-gated,
reuses the daily-spend frame already loaded (no new scan). App-only, no migration.

## 4.297.0 - Auto-clustering SUSPEND RECLUSTER candidates (2026-08-25)

Upgrade Board P1 #31. Cost ▸ Optimize ▸ Storage & waste ▸ automatic-clustering
scan now surfaces, for the CHURNY tables (paying clustering credits to recluster
~0 TB), an estimated recoverable $/window and generated `ALTER TABLE … SUSPEND
RECLUSTER` candidates (review-only). `insights.flag_clustering_churn` gains a
RECOVERABLE_USD column (the churny spend a suspend would stop; 0 for non-churny)
and a new `insights.suspend_recluster_sql` helper produces the qualified-name-safe
ALTER. App-only, no migration.

## 4.296.0 - Proven-fix transfer engine (2026-08-25)

Upgrade Board P1 #34. Cost & Contract ▸ Optimize ▸ Idle & sizing gains a
"Replicate a proven fix" panel: a fix already VERIFIED to save on one warehouse
is suggested on OTHER warehouses that the idle/sizing advisors independently flag
for the same fix but haven't had it applied — evidence-backed quick wins.

- New `mart_sql.verified_wins()` builder reads VERIFIED SAVINGS_LEDGER rows and
  recovers the fix type + warehouse for autobooked rows (which leave
  FINDING_TYPE/TARGET_OBJECT NULL) by joining WAREHOUSE_CHANGE_REGISTRY on
  SOURCE_CHANGE_ID (SETTING → fix type, WAREHOUSE_NAME → target). VERIFIED-only,
  realized dollars only (never ESTIMATED).
- New pure `app/logic/proven_fix_transfer.py` cross-references verified wins with
  the in-scope idle_advisor (AUTO_SUSPEND) and size_recommendations (RESIZE)
  frames. Each suggestion carries the proven warehouse's realized $ as EVIDENCE
  and the candidate's OWN measured figure as an ESTIMATE. Dedup excludes the
  proven warehouse, already-tuned warehouses, and any under an open experiment.

App-only, no migration — read-only over existing SAVINGS_LEDGER +
WAREHOUSE_CHANGE_REGISTRY + the existing idle/sizing/experiment readers.

## 4.295.0 - Operator Case File: cross-section handoff builder (2026-08-25)

Upgrade Board P0 #10. A session-only, cross-section case builder. A reusable
`＋ Add to Case` control (`components.add_to_case_button`) on loaded evidence
snapshots the section, scope (company+window), source, freshness, a short summary,
a next action, and a few preview rows into `st.session_state`. The accumulated
items assemble into ONE Markdown handoff document — reviewed and exported on Brief
(▸ Operator Case File) as a `.md` download plus a raw copy-out block (a working
path even when the SiS download button is inert) — the cross-section artifact a
per-section CSV export cannot produce.

New pure module `app/logic/case_file.py` (Streamlit-free, fully unit-tested) holds
all validation, dedup (re-adding identical evidence is a no-op), preview capping,
Markdown-cell escaping (untrusted Snowflake values can't break the table), and
document assembly. Wired into four high-value v1 sites: an open Alert, the
Operations optimization triage, a Security failed-login burst, and the Overview
spend tile (which passes its as-of watermark). Session-only, no migration.

## 4.294.0 - Task duration-drift: full-grain key + per-day dedup (2026-08-25)

Fixes a latent grain bug in `insights.task_duration_anomalies` (the retrospective
"Duration drift" panel), surfaced by the F5 review of the sibling forecast. It
keyed tasks on `DATABASE_NAME.TASK_NAME`, so a task name reused across schemas (or,
under company='ALL', across companies) interleaved multiple series into one — a
corrupted robust-z baseline that could flag or clear the wrong task. Now keyed on
the full grain (DB, schema, name, company when present) and deduped to one AVG_SEC
per calendar day before the robust-z pass (so a duplicate mart row can't inflate
the active-day count either). SCHEMA_NAME is carried through to the Operations ▸
Tasks ▸ Health drift table. Mirrors the fix already applied to
`duration_sla_forecast` (4.293.0). App-only, no migration.

## 4.293.0 - Predictive SLA miss: tasks trending slower (2026-08-25)

Operations ▸ Tasks ▸ Health gains a "Predicted SLA miss — tasks trending slower"
panel (Upgrade Board P0 #5), the leading half of the duration signal beside the
existing retrospective "Duration drift". A new pure function
`insights.duration_sla_forecast` reuses the FACT_TASK_DAILY frame already loaded
(no new scan): for each task with a real baseline (median >= 30s, >= 7 active
days) it flags one whose last few days are climbing (the latest day is the recent
peak and above the recent start) and materially above the task's own median —
"At risk" at >= 1.3×, "Predicted miss" at >= 2×. Complements the drift, which
flags a task already slow on some day.

Scope note: ACCOUNT_USAGE only sees completed runs (TASK_HISTORY lags ~45min), so
this is a trailing-window trend forecast, not a live in-flight "still running past
p95" detector (which would need INFORMATION_SCHEMA table functions and is subject
to SiS owner's-rights scoping). App-only — no migration.

## 4.292.0 - Billed-vs-attributed gap panel (2026-08-25)

Cost & Contract ▸ Spend gains a "Billed vs attributed" panel (Upgrade Board P0
#12) right under the existing "Cost drill coverage" block. From the metering
frame already loaded, it splits billed dollars into company-attributable
warehouse spend (the only service carrying a company key via
COMPANY_FOR_WAREHOUSE) vs the unattributed gap (serverless, AI/Cortex,
replication, storage credits — billed but not chargeable to a company):

- a coverage KPI ("Attributable to a company") + the unattributed $ and its
  share of the bill;
- a per-service breakdown of what makes up the gap;
- a daily unattributed-share % line as a new-workload canary (complete days only).

Two new pure functions in `app/logic/cost_coverage.py`: `attribution_gap`
(totals + coverage% + per-category gap breakdown) and `attribution_gap_trend`
(daily gap share). This is a DIFFERENT axis from the adjacent "Cost drill
coverage" KPI, which measures object-level drillability — the caption states the
distinction so the two percentages don't read as a contradiction. App-only — no
new SQL builder (reuses the loaded frame), no migration.

## 4.291.0 - Stored-procedure regression advisor (2026-08-25)

Operations ▸ Queries gains a toggle-gated "Stored-procedure regression" section
(Upgrade Board P0 #7). Two new live `ops_sql` builders over
`ACCOUNT_USAGE.QUERY_HISTORY` CALL rows:

- `proc_sla_rollup` — per-proc calls / success-only avg / p95 / max / total-minutes,
  ranked by SLA impact (frequency × duration).
- `proc_regression` — this window's success-only p95/avg vs the prior equal-length
  window, per proc, as a percent change, so a proc that got slower surfaces even
  if it's cheap. Both windows are scoped by one `_query_scope` call (over 2× the
  days) so current and prior partition identically; a proc must clear a min-call
  floor in BOTH windows before it's compared.

Latency is success-only (a proc that starts failing fast must not read as
"faster"); the fail-rate delta is surfaced so a new pure classifier
(`app/logic/proc_regression.py`) can flag "Faster but failing" alongside
Regressed / Slower / Stable / Improved. Verdict + `P95_DELTA_PCT` auto-tint via
the shared status palette. Rides the same query-stats surface as the per-query
optimization advisor (4.288.0). App-only — no migration.

## 4.290.1 - CI floor-compat green: raise pinned floors to match 1.52 runtime (2026-08-25)

Hotfix for the red CI gate on 4.290.0. The `width='stretch'` migration raised the
requirements streamlit floor to `~=1.52`, but the CI `floor-compat` job still
hard-pinned `streamlit==1.45.0` — where `st.button(width=...)` does not exist —
so it failed with `ButtonMixin.button() got an unexpected keyword argument 'width'`
(14 failed). Root cause: a floor pin that no longer equalled the requirements
floor. Fixes, all kept in lockstep:

- `.github/workflows/ci.yml` floor-compat: `streamlit==1.45.0` -> `1.52.2` (the
  exact SiS runtime), `altair==5.4.0` -> `5.5.0`.
- Surfaced a second latent conflict: streamlit 1.52.2 declares
  `altair!=5.4.0,!=5.4.1` (the whole 5.4 line; no 5.4.2 exists), so it is not
  co-installable with `altair==5.4.0`. Raised the altair floor `~=5.4` -> `~=5.5`
  in both `requirements.txt` and `requirements-dev.txt`; bumped the dev streamlit
  floor `~=1.45` -> `~=1.52` to match.
- Verified against an exact-floor venv (streamlit 1.52.2 + altair 5.5.0 +
  pandas 2.2.0 + jinja2 3.1.2): full suite 2791 passed, 2 skipped.
- The `_APPTEST_BUTTONGROUP_OK` guard (< 1.55.0) still fires on 1.52.2; refreshed
  its stale `1.45.0` comment reference.

## 4.290.0 - Migrate use_container_width -> width='stretch' (2026-08-25)

Streamlit is removing the deprecated `use_container_width` parameter (already gone
from `st.image`/`plotly_chart`/`vega_lite_chart` in 1.57/1.61). Migrated all ~51
call sites to the replacement `width` parameter (`use_container_width=True` ->
`width='stretch'`, `False` -> `'content'`) across `st.dataframe`, `st.button`,
`st.download_button`, and `st.altair_chart`, plus the `export_button` wrapper and
the shared table helper. No visual change.

- `requirements.txt` streamlit floor raised `~=1.45` -> `~=1.52`: the `width`
  string values don't exist before ~1.52, and SiS (`environment.yml`) runs
  streamlit **1.52.2**, so 1.52 is the real minimum now.
- Verified: full suite on streamlit 1.58.0 (2792 passed); the `width='stretch'`
  render of every migrated component AND the full page-render AppTest suite pass
  on streamlit **1.52.2** (the SiS version).
- Also merged dependabot: `snowflake-snowpark-python ~=1.30 -> ~=1.53` (app uses
  only stable core Snowpark; no breaking change).

## 4.289.0 - Auto-resolve cleared alerts (V091, owner-applied) (2026-08-25)

Alerts whose condition has cleared now close themselves, so the operator stops
manually resolving stale "over threshold yesterday" rows.

- **Migration V091** (owner applies in Snowsight after V090): `ALTER ALERT_CONFIG
  ADD AUTO_CLEAR_ENABLED + CLEAR_THRESHOLD_NUM`; seed the three `FACT_QUERY_HOURLY`
  live-window rules (`PERF_QUERY_FAIL_PCT`, `PERF_QUEUED_MINUTES`, `PERF_SPILL_GB`);
  `SP_ALERT_SCAN` re-derived from V087 with one final `[auto-clear sweep]` arm that
  RESOLVEs today's still-OPEN events (`RESOLUTION_KIND='AUTO_CLEARED'`) once the
  scope drops below the CLEAR hysteresis floor (default 0.9 × THRESHOLD_NUM).
  OPEN-only (never ACK/RESOLVED/SNOOZED); ≥1h dwell + hysteresis prevent flapping;
  today's-bucket only, so a historical day-stamped exceedance is never rewritten;
  the sweep is wrapped so a failure never breaks alerting. The three rules' dedupe
  guard ignores `AUTO_CLEARED` rows so a same-day **recurrence re-alerts** (a manual
  resolve still suppresses re-raise) — caught in adversarial review. Byte-derived
  from V087.
- **App read-path**: `AUTO_CLEARED` is excluded from per-rule precision, MTTR, and
  resolved-counts exactly like the existing machine-close `SUPERSEDED`, so machine
  closes never distort operator metrics. Auto-cleared events simply drop off the
  open feed.

## 4.288.0 - Per-query optimization advisor (2026-08-25)

The Operations ▸ Queries drill now shows a deterministic optimization advisor
below any drilled query: a 0-100 "optimize me first" badness score plus concrete
plain-English fixes, computed at ZERO AI cost from the query row already loaded.

- New pure `app/logic/query_advisor.py` (`advise(row) -> (findings, score)`),
  mirroring `sizing.py`'s named-threshold pattern and `scoring._cap`'s capped
  composite so no single driver saturates the score. Rules: remote spill,
  local spill, poor partition pruning, large cold scan, compile-bound, queued,
  and expensive-empty-result — with thresholds identical to
  `ops_sql.query_optimization_triage` so the advisor never contradicts the
  triage table that links into the drill.
- Findings render most-impactful-first as chips + plain-English sentences.
- OPTIONAL per-query Cortex rewrite (`ai_prompts.query_optimization_prompt` via
  the existing `ai_evaluation_panel`): button-gated, credit-warned, grounded in
  exactly this query's stats + the findings, told to invent no tables/columns —
  never auto-runs, spends nothing until clicked. No migration.

## 4.287.0 - Ask OVERWATCH: warehouse-waste + task-failure answerers (2026-08-25)

Two more grounded answerers on the Ask page, same pattern (existing tested builders,
real numbers, honest refusal / good-news paths):

- **"which warehouse is wasting credits"** -> `insights_sql.idle_warehouse_analysis`
  ranks warehouses by idle credits (hours billed with no queries); names the top
  waster with its idle share and idle/metered hours, and the account-wide idle total.
  Live builder, so the window is labeled at its effective 90-day cap. Zero idle
  returns honest good news, not a false "no data".
- **"which task is failing most"** -> `mart_sql.fact_task_daily` aggregated by task:
  names the task with the most failures over the window, its failure rate, and its
  latest error message. No failures returns honest good news.

Routing adds `wast`/`fail` prefix stems; the four answerers stay disjoint (a
warehouse-waste question needs a waste/idle term, a task question needs a task +
failure term), so none steals another's intent.

## 4.286.0 - Ask OVERWATCH: grounded answerer registry (2026-08-25)

A narrow, grounded question-answering page — NOT generic text-to-SQL over semantic
views. A plain-English question is routed to one of a small set of hand-built
"answerers", each of which runs EXISTING tested builders and returns a result where
every number is real query output; unmapped questions get an honest refusal, never a
plausible guess. DBA-only for now; leads the sidebar in its own "Ask OVERWATCH" nav
group as the ask-first front door.

- **Two answerers** wired to existing builders (no new marts, no migration):
  `spend_spike_by_user` (`mart27_sql.alloc_attribution` USER + `robust_zscores` —
  names the top spender, flags a true peer-outlier, drops unattributed NONE/UNKNOWN)
  and `cloud_services_spike_by_query` (`cloud_svc_top_shapes` / `cloud_svc_by_user` /
  `fact_cloud_services_ratio` — names the driving query shape, heaviest user, elevated
  warehouses).
- **Router** (`app/logic/ask`): deterministic word-boundary keyword matching with an
  honesty gate; no strong match → refusal. Company scope always comes from the page
  filter, never the free text.
- **Optional Cortex phrasing** only rewords the already-grounded result (off by
  default, changes no number). A query FAILURE renders an honest refusal, never a
  false "no data" (branches on `run().ok`).
- Built additively (`app/logic/ask/` + `app/ui/pages/ask.py`), hardened by a
  5-dimension adversarial review, and covered by `tests/test_ask_registry.py`.

## 4.285.0 - Performance: per-distinct-user company scope in live security builders (2026-08-24)

Cross-repo review finding #15. `companies.user_clause` emits `COMPANY_FOR_USER(col) = 'X'` — one
UDF call PER SCANNED ROW — applied directly on raw ACCOUNT_USAGE views, the documented worst
live-scan pattern on a scoped session.

- **`companies.user_scope_subquery(...)`** returns the equivalent membership predicate that runs
  `COMPANY_FOR_USER` once per DISTINCT user (the proven `mart27_sql.ai_code_daily` shape):
  `col IN (SELECT c FROM (SELECT DISTINCT c FROM <source> WHERE <window>) WHERE COMPANY_FOR_USER(c)='X')`.
  Leak-safe by construction (the inner UDF filter admits only company-X users, so a wrong window
  can only under-include, never leak) and **byte-exact**: under the live V044 UDF
  `COMPANY_FOR_USER(NULL)='UNKNOWN'`, so the `UNKNOWN` scope re-admits `OR col IS NULL` while
  Trexis/ALFA correctly exclude NULL both before and after. Injection-guarded via `safe_identifier`
  / `sql_literal` / `assert_no_control_tokens` (the assembled SELECT is never passed whole to the
  control-token gate). `user_clause` itself is unchanged.
- Rewrote the 13 in-scope LIVE builders (reducing UDF invocations from O(rows) to O(distinct
  users)): `security_sql` failed_logins, single_factor_logins, login_takeover_candidates,
  failed_login_reasons, admin_role_activity, client_drivers, dormant_reawakening, unload_activity,
  recent_grant_changes (user arm), effective_access; `insights_sql` repeat_query_fingerprints,
  release_query_compare; `cost_sql` allocated_attribution (USER dimension). Skipped the
  already-one-row-per-user USERS scans, the FACT/mart readers, the cortex user rollups, and the
  GRANTS_TO_ROLES role arm. Adversarially security-reviewed (3 reviewers): 0 issues. (`app/companies.py`,
  `app/data/security_sql.py`, `app/data/insights_sql.py`, `app/data/cost_sql.py`)

## 4.284.0 - Performance: mixed-tier run_batch gather path (2026-08-24)

Cross-repo review finding #58. `run_batch` requires all members share one tier (its tuple-level
`st.cache_data` has one fixed TTL), so a panel mixing a live + recent + historical read had to
issue several round-trips. Added a sibling gather path.

- **`query.run_batch_mixed(specs, page)`** submits members of DIFFERENT tiers async on ONE session
  in ONE round-trip. Each member's telemetry, `QueryResult.tier`, and per-member cache entry are
  stamped with ITS OWN tier, so the tier-keyed member cache + per-tier TTLs stay exact and a member
  is cache-hit-reusable by the same member in any batch of that tier. The statement timeout is the
  MAX over the members' tiers (the safe direction — no member gets less time than its solo run()).
  Same contract as `run_batch`: always returns every key; failures quarantine + fall back per-key
  through `run()`. Single-tier `run_batch` is unchanged (its tuple cache is a real optimization a
  mixed path can't keep); the only existing-code change is a backward-compatible `timeout_s` kwarg
  on `_execute_batch`/`_execute_batch_bounded`. (`app/core/query.py`)
- **Control Room day-replay** adopts it: the recent 4-member + historical 2-member batches merge
  into ONE cross-tier round-trip; the original serial fallback is preserved.
- Adversarially reviewed — caught and fixed a telemetry double-count (the mixed wall/fallback
  rows now reuse the `batch_wall:`/`batch_fallback:` prefixes the fleet-seconds aggregators
  exclude) before shipping. 4 new unit tests (per-member tier, max-tier timeout, member-cache
  keying, telemetry prefix).

## 4.283.0 - Performance: one wide FACT_METERING_DAILY read shared by every daily-spend caller (2026-08-24)

Cross-repo review finding #46. Each daily-spend caller emitted a distinct `fact_daily_spend(N)`
window (14/30/45/70/150) across two tiers, so FACT_METERING_DAILY was scanned up to 5× per session
and never warm-shared across pages. Now every caller reads ONE wide 150d frame through a shared
accessor and slices client-side.

- **`components.daily_spend_wide(page)`** reads `fact_daily_spend(150)` at tier `hourly` with a
  single telemetry key. Cache identity is (tier, SQL-text, scope) — the caller key is
  telemetry-only — so every caller lands on ONE cache entry, shared across Brief/Overview/Contract
  and across pages within a session.
- **`formulas.daily_spend_last_n(df, n)`** slices the wide frame to the last N days. Because
  FACT_METERING_DAILY is daily-grain, a smaller window is a strict suffix — row-for-row equal to
  fetching N days (pinned by a new unit test). Safe on empty/failed/missing-DAY frames.
- Rerouted all six callers: Overview (150d backtest + 45d MTD fallback), Contract (70d AI recon +
  two 30d planner-burn reads, each sliced at use), Brief (the 14d spark left the `recent`
  run_batch for the shared hourly read). The `fact_daily_spend` builder and its canary are
  untouched. Adversarially reviewed: 0 regressions. (`mart_sql.py`, `formulas.py`, `components.py`,
  `overview.py`, `brief.py`, `contract.py`)

## 4.282.0 - Performance: batch the Operations Pipeline SLA tab's four serial scans (2026-08-24)

The flagship follow-up to the v4.281.0 perf batch (cross-repo review finding #6). The Operations
Pipeline SLA tab issued its four independent `recent` reads serially, so cold section latency was
the SUM of four ACCOUNT_USAGE scans on one XS warehouse.

- **Operations ▸ Pipeline SLA**: COPY-load failures, volume deltas, registered-product
  row-volume, and dynamic-table refresh health now prefetch in ONE `run_batch`; cold latency
  drops to ~MAX(scan) instead of SUM(scans). All four reads were unconditional at their call
  sites and share the `recent` tier, so exactly the same scans fire and the render order is
  unchanged. The row-volume helper (`_dq_row_volume_panel`) gained a `preloaded` param so its
  read joins the batch while keeping its run() fallback. (`app/ui/pages/operations.py`)

Perf-budget pin +4 (operations.py): the four batch-spec source labels duplicate the reads'
existing `ACCOUNT_USAGE` labels — no new scans (reachable-table set unchanged per test_v451_trust).

## 4.281.0 - Performance: batch serial ACCOUNT_USAGE reads + cache-tier + settings memo (2026-08-24)

The S-effort performance batch from the cross-repo review. Every change is behavior-preserving —
only reads that were UNCONDITIONAL at their call site are batched, so exactly the same scans fire
and the render order is unchanged; a pure latency win. Adversarially reviewed: 0 regressions.

- **Alerts ▸ History**: the five unconditional `recent` reads (event history, MTTA/MTTR, incident
  lifecycle, delivery SLO, alert fatigue) now prefetch in ONE `run_batch` instead of five serial
  round-trips, and the conditional route/backlog pair batches inside the `slo.usable()` block.
  (The page previously had 29 `run()` calls and zero `run_batch`.) (`app/ui/pages/alerts.py`)
- **Admin ▸ access self-check**: the six `SELECT … LIMIT 1` probes fetch in one parallel
  `metadata` batch (SHOW WAREHOUSES stays solo); a BLOCKED source is still detected via
  run_batch's per-key fallback. (`app/ui/pages/admin.py`)
- **Operations ▸ adaptive-candidacy**: the hour-of-day activity read and the heavy
  idle-warehouse QUERY_HISTORY+METERING join batch as one `hourly` fetch. (`app/ui/pages/operations.py`)
- **Security ▸ role revoke-safety drill**: `role_holders` + `role_privileges` batch as one
  `historical` fetch, halving click-to-render. (`app/ui/pages/security.py`)
- **load_settings**: the `DEFAULT_SETTINGS` + `iterrows` merge is memoized per refresh-salt scope
  (`_merged_settings_cached`) instead of re-running at each of ~23 call sites per rerun; a
  transient SETTINGS failure still falls through to code defaults uncached, and cache_data's
  copy-on-return keeps callers mutation-safe. (`app/ui/components.py`)
- **health_strip cache tier**: promoted from `live` (30s) to `recent` (300s) at all four call
  sites (sidebar shell, Brief, Control Room, Overview) — it reads a mart that loads hourly, so a
  30s TTL re-paid the scan up to 120×/hour on every page shell. Ack/resolve writes still
  invalidate it immediately via the domain salt and Refresh forces an instant re-read, so
  interactive freshness is unchanged; the sidebar badge auto-refreshes every 5 min instead of 30s.
  (`app/main.py`, `app/ui/pages/brief.py`, `control_room.py`, `overview.py`)

Held (not clean S-effort drop-ins): Alerts Rules, Operations monitor-coverage, and Security
Trust-Center reads are conditionally interleaved or use `SHOW`/`probe=True`, which `run_batch`
can't batch without changing which scans fire.

## 4.280.0 - Chargeback & AI bug-hunt round 4: role-share / audit-stamp / display (2026-08-24)

A fourth sweep of the last lighter-covered surface — the role allocation lens, the DEPARTMENT_MAP /
DEPT_BUDGETS admin writes, statement export, and per-user display (5 findings, all confirmed; the
charts and caching angles came back clean). The adversarial fix-review then caught a regression in
the role-share fix, which was corrected and independently re-verified before shipping.

- **The role allocation lens over-attributed dollars under a short-retention role fact.** When the
  owner sets `FACT_RETENTION_DAYS_HOURLY` below the window, `FACT_QUERY_ROLE_HOURLY` holds only
  recent days while the credit pool (`FACT_WAREHOUSE_DAILY`, 365d floor) spans the full window, so a
  short-window elapsed-share multiplied a full-window pool — allocating a recent-only role its
  warehouse's entire year of spend. `role_share` now carries a coverage gate that abstains to the
  live leg (which clamps to 90d and rematches the pool) ONLY on a material retention shortfall
  (measured pool-vs-role with a 7-day slack, so a young account, a short window, or the routine
  1-day ingestion-boundary offset still serve the mart). (`app/data/mart27_sql.py`)
- **New admin writes credited the app owner, not the operator.** The `DEPARTMENT_MAP` and
  `DEPT_BUDGETS` MERGE `WHEN NOT MATCHED` inserts omitted `UPDATED_BY`, so it defaulted to
  `CURRENT_USER()` — the app owner under owner's-rights SiS — and a newly-added mapping or budget
  showed the owner as its author until the next edit. Both inserts now stamp `identity_sql()`.
  (`app/ui/pages/cost_parts/ai_chargeback.py`)
- **A statement summary split one department into several rows.** `00_summary.csv` grouped by
  `[DEPARTMENT, DEPT_OWNER]`, but a department can span warehouses with different owners, so it
  emitted the department multiple times with partial totals while one statement file (grouped by
  department alone) held its full spend. The summary now groups by department, folding distinct
  owners into one cell. (`app/ui/pages/cost_parts/ai_chargeback.py`)
- **The "Cost by user" chart merged namesakes.** It grouped by `DISPLAY_NAME` ("First Last"), so two
  distinct logins that share a name collapsed into one bar that disagreed with the login-keyed detail
  table beneath it. It now groups by the unique login and disambiguates a shared display name with
  the login. (`app/ui/pages/cost_parts/ai_chargeback.py`)

## 4.279.0 - Chargeback & AI bug-hunt round 3: chargeback / writeback / export (2026-08-24)

A third sweep, targeting the department-chargeback, Action Queue writeback, and statement-export
surface rounds 1-2 didn't deeply hunt (7 findings, 5 unique). The adversarial fix-review then
found one of the five fixes was itself wrong and it was reverted — so four ship.

- **A duplicate / case-variant `DEPARTMENT_MAP` row double-counted a warehouse's credits.** The
  case-insensitive map join fans out 1→N on a warehouse mapped twice (legal under the table's
  case-sensitive PK, e.g. a hand-seeded lower-case row), inflating the chargeback total and
  over-billing the finance statements. The join now collapses the map to one row per
  `UPPER(NAME)` (latest `UPDATED_AT` wins). (`app/data/chargeback_sql.py`)
- **An AI exception queued from the all-companies view was mis-attributed and could duplicate.**
  The Action Queue writeback stamped a per-user breach with the view's filter (`'ALL'`) rather
  than the user's real company, so the same breach re-queued from a company scope inserted a
  SECOND open row (double-counting its projected spend on the Workbench KPI). Per-user rows now
  stamp `COMPANY_FOR_USER(user)`; the scope-aggregate row stays under the view's scope.
  (`app/ui/pages/cost_parts/ai_chargeback.py`)
- **A statement-zip filename collision silently dropped a department's CSV.** Two department
  names sanitizing to the same file (`R&D`/`R/D` → `R_D`) overwrote each other on extraction
  while the summary still listed both. Zip arcnames are now de-duplicated.
  (`app/ui/pages/cost_parts/ai_chargeback.py`)
- **The cross-company CoCo leak had a second trigger the round-2 fix missed.** A scoped company
  whose credit rows fall OUTSIDE the selected window (present within the 365d fetch but cut by
  the shorter window) emptied the rollup and fell back to the account-wide token population,
  listing other companies' users. `coco_efficiency` gained a `scoped` flag that refuses the
  account-wide fallback for a company view; the panel shows an honest note instead.
  (`app/logic/wave2.py`, `app/ui/pages/cost_parts/ai_chargeback.py`)

Reverted before shipping: a fifth fix armed `coverage_gate=True` on the account-wide Cortex-spend
KPI. The fix-review showed the AI-metering series is naturally sparse (no row on idle days), so
the dense-series coverage contract would reject a good mart on the common case and degrade to the
90d-clamped live fallback — undercounting long windows. Left unarmed (with a guard comment).

## 4.278.0 - Chargeback & AI bug-hunt round 2: small-cohort / young-data edges (2026-08-24)

A second adversarial sweep of the same section (7 findings confirmed, 0 refuted), then an
adversarial review of the fixes themselves that caught 2 regressions before commit. All 9 are
locked by regression tests.

- **`ai_costs_by_model` lacked the coverage gate its Code-only siblings enforce.** A young /
  backfilling `FACT_AI_USAGE_DAILY` returned ~30 days of credits under an "AI spend (365d)"
  label. Added the ALL-source coverage gate (this KPI serves Code + Functions) so a fact that
  can't cover the window emits zero rows and the KPI falls back to the honestly-labeled live
  Functions reader. (`app/data/mart27_sql.py`)
- **`OBSERVABLE_DAYS` floored one day low.** A first-usage timestamp carries a time-of-day, so a
  bare `.dt.days` subtraction landed a day short of `effective_window_days`' inclusive count —
  over-projecting the 30d figure ~33% and suppressing a real 4-day breacher a day longer.
  Normalize to date grain. (`app/logic/cortex.py`)
- **CoCo review flag was arithmetically unreachable for any 1-2 user company scope.** A
  whole-population median includes the user being tested, so the heaviest user's ratio to the
  midpoint was < 2 and could never clear the gate. Switched peer/session multiples to
  leave-one-out, positive-only medians (a zero-credit user isn't a spending peer, and can't drag
  the baseline to 0). (`app/logic/wave2.py`)
- **A new user's first-day burst fired a false Critical "AI budget breach (all users)".** The
  scope-aggregate projection had no small-N guard (the per-user ladder does), so a 1-day burst
  extrapolated 30x inflated the scope total into a breach no per-user row explained. Added a
  guarded scope total on the same per-USER re-projection basis `classify_exceptions` uses — which
  also catches a breach distributed across users who each added a young secondary source (a
  per-row drop would have missed it). The headline KPI stays the full column sum. (`app/logic/cortex.py`)
- **CoCo panel leaked other companies' users under a company filter** in three places: the "Raw
  token grain" expander rendered the account-wide frame; a company view whose daily scan didn't
  resolve fell back to the account-wide population; and "Fleet cache-hit 0.0%" contradicted a
  "caching is high" caption when no shown user had token grain. Scoped the expander to the shown
  users, early-return with an honest caption for scoped views, and show cache-hit as n/a when
  there's no token grain for the shown users. (`app/ui/pages/cost_parts/ai_chargeback.py`)

## 4.277.0 - Chargeback & AI bug-hunt round 1: same-metric reconciliation (2026-08-24)

A focused bug sweep of the Cost & Contract ▸ Chargeback & AI section (7 findings, 5 unique
after collapse) fixing five same-metric-divergence / mislabeled-window defects. Each is
locked by a regression test asserting the reconciliation invariant, not a hard-coded number.

- **Headline 30d projection under-projected a new heavy user.** `rollup_summary` divided
  scope spend by the scope-wide window, spreading a brand-new heavy user's burst over the
  oldest user's long tenure — so the KPI could read far below the sum of the `Proj. 30d $`
  column in the detail table right beneath it. The KPI now equals that column's sum (each
  user projected over their own observable window). (`app/logic/cortex.py`)
- **A new second source's first-day burst tripped a false Critical budget breach.**
  `classify_exceptions` summed per-source 30d projections, each dividing by its own (possibly
  1-day) window, so a mature user adopting a second source today saw that one day extrapolated
  30x into a false breach. It now re-projects the user's summed credits over their most
  reliable (max) observable window. (`app/logic/cortex.py`)
- **CoCo efficiency windowed off the data's max date, not "today".** With metering lag, the
  latest usage date predates the account date, so the CoCo window silently drifted back and
  disagreed with the AI-users tab. `coco_efficiency` now accepts `as_of` and the panel passes
  `account_today()`, reconciling the two. (`app/logic/wave2.py`, `ai_chargeback.py`)
- **"Cortex spend, 365d" over a 90-day live fallback.** The spend KPI labeled the raw ask
  even when the live fallback clamped the scan to 90 days; it now labels the window actually
  served (K1 contract). (`app/ui/pages/cost_parts/ai_chargeback.py`)
- **"AI spend (365d)" understated the window on the unit-costs KPI.** When the Functions-only
  live fallback served this KPI, the label showed the raw ask though the query clamps to
  `MAX_LIVE_WINDOW_DAYS`; the label and help now disclose the 90-day cap. (`unit_costs.py`)

## 4.276.0 - AI chargeback: Total Requests as a whole number (2026-08-24)

- `Total Requests` rendered as a raw float (`233.000000`) in both the AI user-attribution
  detail table and the Exceptions table — request counts are whole numbers, so both now
  format the column as an integer (`%d`).

## 4.275.0 - CoCo efficiency review: neutral wording + tie to the Window filter (2026-08-24)

Refines the v4.274.0 CoCo efficiency panel from owner feedback:

- **Neutral, professional wording.** Dropped the "let CoCo do the work" / "crutch" framing
  throughout. The flag is now 🚩 **Review** (was 🚩 Coach), the KPI is **Flagged for review**,
  and the caption describes a "high-intensity usage pattern worth reviewing … a pattern to
  review, not a verdict — confirm against delivered work before acting." Same signals, softer voice.
- **Tied to the page Window filter.** The panel was hardcoded to 30 days; it now tracks the
  page's `days` filter (`_token_economics_panel` takes `days`), so filtering for a quarter or a
  year is reflected in the token scan, the credit window, the `Credits (Nd)` header, and the
  caption — no more silent 30-day mismatch. `cortex_code_token_types` now honors the long window
  (up to 365d) like the sibling Cortex Code scans (low-volume per-user telemetry), and its cache
  key includes the window so switching the filter re-reads rather than serving a stale frame.

## 4.274.0 - CoCo efficiency & coaching flags (2026-08-24)

Cost & Contract ▸ Chargeback & AI ▸ the token-economics panel now surfaces *which* Cortex
Code users are leaning on CoCo as a crutch rather than a supplement — the signal an owner
needs to justify coaching a heavy user instead of just raising their 15-credit/day cap.

- **Reframed the panel.** Cache-hit % is near-uniform (>97%) across the fleet, so it isn't the
  efficiency lever — the old "low cache-hit is the lever" caption pointed at a dead end. The
  panel now leads with a peer-relative **CoCo efficiency** table and a **🚩 Coach flag**.
- **New logic** `wave2.coco_efficiency()` merges the TOKENS_GRANULAR cache grain with per-user
  daily credits (`cortex_code_user_daily`) into: **avg cr/day**, **peer ×** (vs fleet-median
  daily), **cr/request** & **session ×** (long autonomous sessions), **days over the base
  allowance**, **cache-write %** (context churn at full price), and **read-amplification**
  (context re-read per token of new conversation). The 🚩 flag trips when a user hits ≥2 of the
  peer-relative crutch signals — heavy consumer, long sessions, or chronically over cap.
- **Configurable cap.** Days-over-cap reads `COCO_DAILY_CAP_CREDITS` (default 15), so the users
  granted the 30/day exception light up as chronically over the *standard* allowance.
- Peer-relative by design so one heavy-but-productive user can't be flagged in isolation, and
  the panel says plainly to verify against shipped work before acting. The raw token grid moves
  under an expander. Degrades honestly when the daily scan or TOKENS_GRANULAR isn't available.

## 4.273.0 - Bug-hunt round 4: watch-monitor hygiene, graph retry-collapse, AI-KPI source (2026-08-24)

Round 4 hunted new angles (predicate logic, sort/rank, type coercion, metric consistency,
alert/severity, deep dives) and confirmed 5 bugs — all P2, read-only, no migration. 3
cluster in the newer watch-monitor code, which never inherited the anomaly hygiene the
mature Cost surfaces have. Also folded in a zero-risk defensive one-liner from a refuted
finding. 5-agent adversarial review pending.

- **Watch badge previews the wrong movers** (`workbench.render_watch_badge`) — `head(4)` on a
  frame sorted only by the ATTENTION bool showed the first-added watched entities, not the
  most severe; a real spend spike could be dropped below soft health-watches. Rank
  warn > watch before truncating.
- **Stale spike re-fires as "moved" daily** (`watch_monitor.watched_status`) — the cost sweep
  flagged any spike in the 30-day window, so a weeks-old spike kept firing every day. Restrict
  cost attention to the MOST RECENT complete day (as the Cost decision surfaces do).
- **Month-end close flagged as an anomaly** (`watch_monitor.watched_status`) — the sweep
  skipped `suppress_expected_spikes`, so a known month/quarter-end spike flagged the watch
  while every other surface labeled it "expected". Thread EXPECTED_SPIKE_CALENDAR through.
- **Graph pipeline retry over-count** (`graph_sql.graph_daily_costs`) — a 5th TASK_HISTORY
  retry site: a retried-then-succeeded task inflated TASK_RUNS and flagged the healthy run as
  a run-with-failures. Collapse to the terminal attempt for the counts; keep credits summed
  over all attempts (each retry really billed).
- **AI-spend KPI understates on the fallback** (`unit_costs`) — the mart source covers Cortex
  Functions + Code but the live fallback is Functions-only, so if the mart is unavailable the
  KPI silently drops Cortex Code spend while claiming "Account-wide". Disclose "Functions only"
  when the fallback serves it.
- Defensive (refuted finding): `cost_sql` org-currency breakdown wraps the OTHER residual in
  `COALESCE(UPPER(RATING_TYPE),'')` so a NULL rating lands in OTHER and the buckets stay
  additive to TOTAL, regardless of whether NULL-rating rows ever appear.

## 4.272.0 - Bug-hunt round 3: charts, units, RCA, anomaly-share, state (2026-08-24)

Round 3 hunted new angles (chart encoding, cross-page state/caching, formatting/units,
degenerate-data, analytical math, deep dives) and confirmed 7 bugs — all P2, all read-only,
no migration (~46% of raw findings correctly refuted):

- **Top-share % of the wrong total** (`charts.bar_usd`/`bar_count`) — the "% of $total"
  takeaway used the `head(top_n)` sum as the denominator, overstating the leader's share and
  mislabelling the universe. Use the full-frame total (as waterfall_usd already does).
- **Credit sub-line undercount** (`overview` MTD-pace card) — the "cr" sub-line back-solved
  `mtd_usd / rate`, but USD blends AI credits at the AI rate, so it undercounted credits by
  the AI portion (~13% on AI-heavy spend). Sum billed CREDITS directly over the same window.
- **RCA post-onset "high confidence"** (`rca.rank_root_causes`) — the causation gate capped
  only beyond-window candidates (proximity 0), so a change that happened AFTER onset could
  still headline as "most likely cause (high confidence)". Cap after-onset changes at LOW.
- **Anomaly share blows past ±100%** (`anomaly_explain`) — on an offsetting/redistribution
  day the net delta is tiny while per-WH deltas are large, so `delta/net` rendered e.g.
  "+3000% of the move". Suppress the share when the net move doesn't dominate the churn.
- **Rate caption LaTeX garble** (`contract` rate reconciliation) — a two-`$` caption wasn't
  wrapped in `md_dollars`, so Streamlit rendered the prose between the `$`s as a math span.
- **Trend box clobbered** (`unit_costs`) — the sticky leaderboard selection overwrote the
  proc-name text-input every rerun, reverting what the user typed. Change-detection sentinel.
- **Doubled object identifier** (`workbench_sql.entity_recent_changes`) — the change-feed
  re-prefixed DB.SCHEMA onto `OBJECT_NAME`, which is already the FQN, showing
  `TASK DB.SCH.DB.SCH.NAME`. Display `OBJECT_NAME` directly.

## 4.271.0 - Bug-hunt round 2: root-cause the sticky-selection crash + 5 SQL/logic fixes (2026-08-24)

Round 2 (cross-cutting defect-pattern finders + deep-file dives, adversarially verified)
surfaced 11 confirmed bugs — 6 more sticky-selection `.iloc` crash sites plus 5 correctness
bugs. All read-only, no migration:

- **Sticky-selection crash — fixed at the ROOT** instead of site-by-site: `selectable_table`
  now clamps a stale sticky selection to the current frame and returns `None` when its index
  is past the end, so every caller's `frame.iloc[sel]` is always in-bounds. This immunizes
  all ~40 drill sites at once (the round-2 finders had surfaced 6 more unguarded ones —
  Control Room incidents, Operations emergency-cancel + object change-impact, Optimization
  right-size, Admin tuning-targets, Unit-costs procedures — and there were likely others).
- **task_graph_recent_runs** (`ops_sql`) — collapse TASK_HISTORY auto-retries to the terminal
  attempt (QUALIFY ROW_NUMBER over graph-run + task + SCHEDULED_TIME) so TASKS/FAILED_TASKS
  count tasks, not attempts. (Fourth builder of the class; three were fixed in 4.270.0.)
- **budget_pace_variance** (`formulas`) — measure the straight-line target over COMPLETED days
  (`day - 1`) to match the today-excluded MTD actual (metering lags ~24h), so an on-pace
  account no longer reads ~one day's budget "behind" every day.
- **object_run_history** PROCEDURE drill (`change_impact_sql`) — anchor the CALL target to the
  whole final identifier segment so `RUN_SP_LOAD` no longer blends into `SP_LOAD`'s run history.
- **proc_cost_trend** (`insights_sql`) — escape LIKE metacharacters + `ESCAPE '~'` so an
  underscore in a proc name stays literal (was matching `SPZLOAD` for `SP_LOAD`).
- **governance_counts** MFA-gap KPI (`security_sql`) — `COALESCE(DISABLED, FALSE) = FALSE` so a
  NULL-DISABLED active user is counted, matching the Access-panel list the KPI reconciles with.

## 4.270.0 - Bug-hunt fixes: sticky-selection crashes, task-retry over-count, NaT/prompt/selectbox (2026-08-24)

An adversarial bug-hunt (7 finders → independent verification, ~45% of raw findings
refuted) surfaced 10 confirmed runtime bugs. All fixed — read-only, no migration:

- **Sticky-selection IndexError guards (×4)** — drill sites did `df.iloc[sel]` on a
  positional selection Streamlit re-emits after the frame shrinks (row resolved, scope
  narrowed, window toggled), red-tracebacking the page. Added the `0 <= sel < len(frame)`
  guard already used elsewhere: Control Room incident timeline, Alerts open-events drawer,
  Alerts rule rollup, Operations warehouse-change registry.
- **TASK_HISTORY auto-retry over-count (×3)** — task builders counted each retry attempt as
  a separate run (auto-retries share one SCHEDULED_TIME), inflating RUNS/FAILED and making a
  healthy deploy look like it broke tasks. Collapse each scheduled run to its terminal
  attempt (the `QUALIFY ROW_NUMBER()` pattern already in `task_recent_states`):
  `ops_sql.task_runs`, `insights_sql.release_task_compare`,
  `change_impact_sql.object_run_history`.
- **Contract-runway "NaT"** — `formulas._coerce_date` returned `pd.NaT` (a datetime subclass)
  for a NULL date, so the countdown bar rendered "exhausts NaT · decide by NaT". Guard
  NaT/NaN before the isinstance branch.
- **Effective-access dead selection** — clicking a user row couldn't move the access-path
  graph because the selectbox read its own widget key and ignored the recomputed `index=`.
  Drive the selectbox by its own key.
- **AI prompt grounding** — `ai_prompts._assemble` put the TASK after the evidence then
  truncated at 6000 chars, so a wide evidence set dropped the task entirely. Order the
  instruction blocks first and budget the evidence tail.

## 4.269.0 - Drillable sweep, MODERATE wave: scope-the-detail-to-my-selection (2026-08-20)

The MODERATE tier of the UX gap sweep — 8 drills that fetch per-entity detail on a row
click (7 new live builders + 1 reuse + 1 Entity-360 deep-link), read-only, no migration:

- **Cost ▸ Compare** — click a warehouse mover → its pattern-movers scoped to that warehouse
  (`compare_pattern_costs_by_warehouse`, live QUERY_HISTORY×QUERY_ATTRIBUTION_HISTORY).
- **Cost ▸ Unit costs ▸ ETL** — click a pipeline → its non-SUCCESS runs + errors
  (`etl_failed_runs_for_pipeline`).
- **Cost ▸ Chargeback ▸ Query-tag governance** — click a user → their top untagged statement
  types (`untagged_executions_for_user`).
- **Cost ▸ Optimization** — storage-growth mover → the database's per-table storage (reuses
  `table_storage_breakdown`); object-cost-ledger row → that object's Control Room ▸ Entity 360.
- **Security ▸ Exposure** — click an outbound share → the objects it exposes (`SHOW GRANTS TO
  SHARE`, identifier-quoted).
- **Security ▸ Access ▸ Unused roles** — click a role → who holds it + what it grants
  (`role_holders` + `role_privileges`), the confirm-before-revoke context.
- **Control Room ▸ Lock-wait spikes** — click an object → its recent lock-wait events
  (`lock_wait_object_detail` over LOCK_WAIT_HISTORY).

Built by fanning out one agent per code-file group, then an adversarial review of the whole
diff (3 confirmed, fixed): the Compare drill's `RUNS` counts distinct executions vs the mart's
attribution-row count (caption now says so); the untagged-executions drill is a live 90-day
scan while the parent can span longer (caption + docstring now disclose the subset); and the
ETL/tag drill keys gained `database`/`schema_contains` so a sticky selection can't drill the
wrong entity after a filter change. **#7 (attribution scope-to-warehouse) was dropped** — a
selectable main table loses its additive totals and the scoped split introduced a second
attribution formula path, both of which the attribution-math invariants (correctly) forbid.

## 4.268.0 - Make-it-drillable sweep: 17 tables now click-to-drill (2026-08-20)

A proactive UX gap sweep (6-cluster adversarial pass over every page) surfaced the exact
pattern the owner kept hitting via screenshots — static tables whose rows ARE entities
you'd want to click, and account-wide panels that should scope to a selection. 17 verified
gaps, now fixed (each a drop-in to an existing primitive; no new data, no migration):

- **Click → Control Room ▸ Entity 360** (`entity_nav_table`): Overview Top-movers (warehouse),
  Operations Tasks-Health / Node-timing / Release-regressions / Failure-timeline (task, keyed
  on the composed DB.SCHEMA.TASK FQN — `fact_task_daily` gains `SCHEMA_NAME`), Operations
  Cost-per-query outliers (warehouse), Security Dormant-users (user), Decision Studio Scenarios
  action plan (its source entity).
- **Click → the drill/detail** : Operations ▸ Queries Optimization-triage row now loads the
  query drill-through (mirrors Heaviest-queries); Brief Fires and Control Room incident members
  open the specific alert; Alerts Rule-precision and Alert-fatigue rows show that rule's recent
  resolutions/events inline; Admin SLO-scorecard shows a failing page's slow keys, and the
  error-family row filters the raw error rows.
- **Scope-to-selection**: Security ▸ Least-privilege — click an over-broad grant scope and the
  per-table revoke shortlist (and its REVOKE script) filters to that scope. Overview's open-
  crit/high KPI gained a one-click path to the alert queue.

Built by fanning out one agent per file-group, then an adversarial review of the whole diff
(6 clusters, 1 confirmed finding, fixed): the Least-privilege scope filter compared an
UPPER-cased prefix against the original-case `OBJECT_NAME` FQN, so a quoted lowercase
database/schema would silently filter the shortlist to empty — now matched case-insensitively.

## 4.267.0 - Per-warehouse "why is IT elevated?" cloud-services drill (2026-08-20)

Cost ▸ Spend ▸ Cloud-services health by warehouse: the ratio table is now **selectable**
— click a warehouse and the "Why is it elevated?" compile-heavy query families and the
cloud-services-by-statement-type tables scope to *that* warehouse, so a DBA can see and
target the specific queries driving its cloud-services pressure (previously both were
account-wide and couldn't be filtered per warehouse).

- `cost_sql.compile_heavy_families` / `cs_by_query_type` gain a `warehouse` filter (exact
  `WAREHOUSE_NAME`, `sql_literal`-escaped); the family/CS marts aren't warehouse-grained,
  so a selection uses a live per-warehouse `QUERY_HISTORY` read (interaction-gated on the
  row click — not first paint). `compile_heavy_families` also takes a `min_runs` floor,
  relaxed to 5 for the scoped drill (one warehouse has fewer runs per family) while the
  account-wide default keeps the 20-run floor.
- Nothing selected → the existing account-wide (mart-first) view, with a hint to click a
  warehouse. The bounds-guarded selection can't `IndexError` after a window-narrow shrinks
  the frame.

Adversarial review (4 agents, 2 raised → 2 confirmed, both fixed): the scoped builders
re-applied the hardcoded company predicate (`WH_ALFA_%` / Trexis tuple) on top of the exact
`WAREHOUSE_NAME` filter — so a warehouse mapped to a company via the runtime `COMPANY_SCOPE`
table (the in-app unmapped-mapper does exactly this) but not in the hardcoded list would
return a false-empty drill for a warehouse the same page flagged ELEVATED; the company
predicate is now dropped when an exact warehouse is given (a single warehouse is a complete
scope). And the V055 drill's `index=`-to-selection default was removed — Streamlit ignores
`index` once a keyed selectbox has a stored value, so it would silently diverge from later
selections; the deeper drill stays independent.

## 4.266.0 - Repo-review EASY-win leftovers (2026-08-20)

Five more gaps mined from the external `snowmonitor` reference repo — all read-only,
no migration, mostly pure logic over data already fetched:

- **Operations ▸ Tasks ▸ Health — Systemic errors** (#14): one error family hitting 3+
  distinct tasks is surfaced above the raw failure list ("one revoked grant / dead
  source, not N bugs"). `logic.insights.cluster_failures_by_family` over the failure
  timeline already built; counts DISTINCT tasks over the composite DB.SCHEMA.TASK key.
- **Operations ▸ Tasks ▸ Health — Duration drift** (#15): a task 100% successful but
  quietly slower than its own baseline. Feeds FACT_TASK_DAILY `AVG_SEC` (already
  fetched) into the robust-z engine (`task_duration_anomalies`), slow-side only, with
  BASELINE_SEC and SLOWER_X, gated ≥30s / 7+ active days.
- **Cost ▸ Optimization — Auto-clustering churn** (#6): names the poor-cluster-key
  tables — `flag_clustering_churn` adds `CREDITS_PER_TB_RECLUSTERED`, `SPEND_USD`, and a
  `CHURNY` flag (material credits reclustering ~0 TB — the sharpest "paying to
  reorganize nothing" case), which the raw spend table couldn't name.
- **Security ▸ Access — Single-factor logins (MFA-bypassed)** (#16): the behavioral
  counterpart to the config-anchored MFA lens — a successful PASSWORD login with an
  empty `SECOND_AUTHENTICATION_FACTOR`. Unlike `HAS_MFA=FALSE`, it surfaces an *enrolled*
  user who still landed single-factor (a real bypass/misconfig); the USERS join sorts
  enrolled-bypass to the top. `security_sql.single_factor_logins`.
- **Overview — Pace vs budget calendar** (#11): a signed variance of MTD spend vs the
  budget's OWN straight-line expected-to-date (`budget_pace_variance`), isolating
  "ahead of the budget calendar right now" from the structural "will we end over". A new
  KPI beside MTD/Projected; budget-gated (no `MONTHLY_BUDGET_USD` → no card); no query.

Adversarial review (17 agents, 13 raised → 2 confirmed, both fixed): the #11 pace card's
delta line rendered a red up-arrow even when *under* budget (the prose delta has no
leading sign, so a colored arrow always pointed one way) — now a neutral delta with the
severity stripe carrying good/bad. And #16's "(bypass) — a real MFA bypass/misconfig"
label overclaimed: `HAS_MFA` is a current snapshot joined to 30d of history, so a login
made *before* the user enrolled (or MFA caching, or a temporary admin bypass) isn't a
true bypass — relabeled "Enrolled yet single-factor" with a "candidate, verify enrollment
timing" caption. (The sharp dismissals: #15's mean-AD masking only fires on bit-identical
data real `AVG(DATEDIFF)` never produces; #11 counting today as a full day is intentional,
matching the adjacent MTD card's basis.)

## 4.265.0 - Compute-pool → per-user cost drill (2026-08-20)

Cost ▸ Spend ▸ Compute pools & notebooks: the pool table is now selectable — click a
compute pool to see the **users driving its cost**. Per-user attribution comes from
`NOTEBOOKS_CONTAINER_RUNTIME_HISTORY` (the only user-grain SPCS feed), aggregated by
`USER_NAME` for the selected pool (`logic.spcs.compute_pool_user_costs`): credits, USD
at the configured rate, execution time, sessions, notebooks.

Honest about the platform limit: `SNOWPARK_CONTAINER_SERVICES_HISTORY` meters pools at
the POOL level with no user column, so a **native-app pool** (e.g. a Posit Workbench
pool) has no per-user split in ACCOUNT_USAGE — selecting it shows a clear note that its
cost is metered at the pool level and per-user sessions live in the app's own admin
console, rather than a fabricated attribution. No new query (drills the notebook feed
already fetched for the section); no migration.

Adversarial review (6 agents, 4 raised → 3 confirmed, all fixed): the empty-drill note
now distinguishes its three real causes — a genuine native-app pool, a user-owned
non-notebook pool (`Unassigned` → non-notebook Snowpark services), and a simply-empty
notebook feed this window — instead of branding every empty result a "native-app pool";
a bounds guard (`0 <= sel < len(pool_df)`) stops Streamlit's sticky selection from
red-tracebacking the page after a window-narrow shrinks the frame; and the pure helper
honors its "never raises" contract when a frame lacks the `CREDITS` column.

## 4.264.0 - Repo-review wave-2 flagship leftovers (2026-08-20)

Four genuine gaps mined from the external `snowmonitor` reference repo (a 6-cluster
adversarial review classified 70 of its capabilities → 45 already adopted/exceeded,
so these are the real un-adopted lenses). All read-only, no migration:

- **Operations ▸ Tasks ▸ SLA (new sub-tab)** — two "task health beyond run counts"
  lenses on live TASK_HISTORY (neither task mart preserves ordered per-run STATE):
  - *Actively broken tasks* — per-task leading FAILED streak since the last SUCCEEDED
    (`ops_sql.task_recent_states` + `logic.insights.task_failure_streaks`). Tells a
    one-off failure from a task stuck in a retry loop (streak ≥ 2 = "actively broken")
    — sharper than `task_runs`' newest-state-only view.
  - *Task freshness (silent-stop)* — each task's own cadence (median scheduled-gap via
    LAG) vs minutes since its last success, classified On-time / Late / Stale
    (`ops_sql.task_freshness_sla` + `logic.insights.task_freshness_status`). Catches a
    task that quietly stops being *scheduled* — invisible to run/failure views. A
    ~45-min lag buffer keeps a task merely inside the ACCOUNT_USAGE window from
    flagging.
- **Operations ▸ Queries ▸ Optimization triage** — the specific statements an optimizer
  should fix, ranked remote-spill → poor-pruning → large-cold-scan, filtered to "real
  work" (drops CALL wrappers and the app's own queries; requires spill or > 50 GB
  scanned). `ops_sql.query_optimization_triage`; each row carries a Snowsight PROFILE
  link. Distinct from the elapsed-only "Heaviest queries" list.
- **Security ▸ Access ▸ Account-takeover candidates** — the brute-force *breakthrough*
  the count-only failed-logins lens can't express: a per-user burst of failures
  FOLLOWED BY a success (`security_sql.login_takeover_candidates` +
  `logic.insights.takeover_severity`). SUCCEEDED_AFTER is the dangerous signal; a burst
  with no later success is a locked-out user. Event-grain, so it reads live
  LOGIN_HISTORY; toggle-gated like the other heavy security scans; read-only (no alert
  raised).

Mapped each attach point with parallel scouts (reuse existing builders/columns, no
duplication), verified pure scorers on frames + SQL builders by shape.

Adversarial review (19 agents, 15 raised → 5 confirmed, all fixed at the root). The
three most valuable shared one root cause I'd missed — **auto-retry attempts in
TASK_HISTORY share one SCHEDULED_TIME**:
- `task_recent_states` now collapses each scheduled run to its terminal attempt (latest
  COMPLETED_TIME), so the streak counts failed *runs* not attempts and a FAILED retry
  can't sort ahead of the run's SUCCEEDED final attempt.
- `task_freshness_sla` takes its cadence median over positive gaps only
  (`MEDIAN(IFF(GAP_MIN > 0, ...))`), so a retry-heavy task can't get a 0-minute median
  that would make it structurally unflaggable; and orders `NULLS FIRST` so a task with
  no success in the window (highest-severity "silent") survives `LIMIT`, not truncated.
- `task_freshness_status` / `takeover_severity` now honor their "never raises" contract
  on absent columns; added the untested FAIL_IPS≥3 → High escalation case.

## 4.263.0 - Grounded-AI incident narrative (2026-08-20)

Control Room ▸ Auto-investigation now offers a plain-English root-cause narrative
on top of the ranked hypotheses. When the auto-investigation produces a ranked
field, a button-gated **AI evaluation — root cause** panel composes a Cortex prompt
from *exactly those ranked hypotheses* (`incident_narrative_prompt` in
`app/logic/ai_prompts.py`) and returns a short narrative for the on-call responder.

Grounding is strict by construction:
- The prompt embeds only the hypotheses the DBA can already see in the table — no
  hidden data, no second fetch. One reused `ai_evaluation_panel` (the same
  button-gated, credit-warned, model-from-SETTINGS primitive as Alerts/Operations/
  Optimize) with its "grounded in the on-screen evidence only · verify before acting"
  caption and audit "Show grounding prompt" popover.
- Instructions forbid inventing warehouses/tasks/users/times/numbers, demand an
  honest "signals are inconclusive" when the top lead is only LOW confidence, and
  **forbid remediation SQL/DDL** — the RCA feature explains, it never acts, so the
  narrative stays read-only end to end (the shared `_SYSTEM_RULES`, which invites a
  fix statement, is deliberately not used here).
- Offered only when there is a ranked hypothesis to narrate; an empty field yields
  an empty prompt, so the model is never asked to confabulate a cause the
  deterministic ranker couldn't find. Never auto-runs — no Cortex credits until the
  responder presses the button.

Hardening (surfaced by the adversarial review of this feature):
- `sql_literal` now escapes backslashes as well as single quotes. Snowflake honors
  backslash escapes inside single-quoted string literals, so a value ending in a
  lone backslash (e.g. a quoted-identifier warehouse/task name like `MY_LOAD\`) used
  to make the closing `\'` read as an escaped quote → unterminated literal → compile
  error, and an embedded `\n`/`\t` silently became a newline/tab. The narrative
  feature newly routes attacker-influenceable entity names into this shared helper,
  so it is fixed at the root — a strict correctness gain for every `sql_literal`
  caller (no regex-via-literal or backslash-bearing builder call sites exist, so no
  behavior change for legitimate SQL).

## 4.262.0 - Token-economics counts render as integers (2026-08-20)

Follow-up polish to the now-working token-economics panel (Cost ▸ Chargeback & AI):
the Input / Output / Cache Read / Cache Write / Total columns are whole token counts,
but Streamlit painted the float64 columns with its raw default (`2044782.000000`).
Each now carries an explicit integer `NumberColumn` format (`%d`), so the table reads
as clean token counts instead of six trailing zeros. Display-only — the frame stays
numeric, so sort and CSV export keep the exact values.

(A global `_TOKENS` format suffix was considered and rejected: rate columns like
`USD_PER_1M_TOKENS` also end in `_TOKENS` and would have mis-rounded to `0`.)

## 4.261.0 - Fix: Cortex token-economics panel read all zeros (2026-08-19)

Cost ▸ Chargeback & AI ▸ "Load token economics" showed 0 tokens / 0% cache-hit for
everyone. Root cause: `TOKENS_GRANULAR` on this account is nested **by model** — each
model name maps to an object of token-type→count (`input`, `output`, `cache_read_input`,
`cache_write_input`) — but the builder flattened one level and read `F.VALUE:token_type`
/ `F.VALUE:tokens`, keys that don't exist there (the first flatten yields `KEY=model`,
`VALUE=the type object`), so every count extracted to null → zero.

Fix: `cortex_code_token_types` now does a **RECURSIVE flatten** and keeps the numeric
leaves keyed by `F.KEY` — pulling the token-type→count pairs regardless of nesting, so
it's resilient whether the granular blob is model-nested (this account) or a flat
type→count map. And `token_economics` learned the real leaf keys (`cache_read_input` /
`cache_write_input`) alongside the older aliases so a future shape drift can't re-zero it.
(The plain `TOKENS` column is populated, so the "Cost by user" totals were never affected.)

App version 4.261.0.


## 4.260.0 - Incidents that investigate themselves (2026-08-18)

Open an incident and the investigation is already done. Control Room ▸ Incidents & triage,
when you select an open incident, now assembles a read-only **Auto-investigation — ranked
root cause**: the change / task-failure / spend-anomaly / grant-change signals around the
incident's onset, ranked as competing hypotheses for what caused it.

- **Ranked hypotheses** — each candidate scores on `0.45·timing + 0.35·magnitude +
  0.20·entity-match`: a change 12 min before onset outranks a day-old one (a cause
  precedes its effect — a change outside the ~48h pre-onset window can't be a confident
  cause however large), a REGRESSED verdict / big robust-z / root-cause task failure /
  p95 blow-up lifts magnitude, and touching the incident's own entity lifts match. Banded
  HIGH / MEDIUM / LOW, strongest first, with a transparent `why` per row so it's auditable.
- **Hypothesis types**, each from an existing builder (no new evidence source): object
  deploy/DDL change (`change_registry`), warehouse config change (`warehouse_change_registry`),
  upstream task failure root-vs-cascade (`build_failure_timeline`), spend anomaly
  (`flag_anomalies`/`anomaly_summary`), privilege/grant change (`recent_grant_changes`).
- A one-line **verdict** ("Most likely cause (high confidence): …" / "Best available lead
  (low confidence): …" / "No clear trigger in the signals …" — an honest blank, never a
  false accusation).
- **Read-only**: it explains and stops — never writes an INCIDENT_MEMBERS row, never
  executes a remediation. The signal reads run only on incident-select (a deliberate drill,
  same scan class as day-replay).

New pure `app/logic/rca.py` (per-signal adapters + the ranker + summary; 11 tests) wired
into the incident-selection block via one signal-assembly batch that reuses existing
builders. Composes what exists — no new scan source, no duplication.

App version 4.260.0.


## 4.259.0 - Prove-it scorecard: does OVERWATCH earn its keep? (2026-08-18)

The gate the owner set before going autonomous — one director-facing surface that proves
the advising features are correct and pay for themselves. New **Decision Studio ▸
Scorecard** section (the page's flagship landing), composing five trust/value signals:

- **Pays for itself** — verified savings this quarter as a multiple of OVERWATCH's own
  warehouse run cost (`savings_summary_quarter` ÷ `app_cost_quarter × rate`). ≥1× = it
  pays for itself.
- **Realization** — of what verified items were estimated to save, how much measured out
  (existing `ledger_totals.realization_pct`).
- **Acted on** — of the recommendations the team DECIDED on (90d), the share acted on
  (DONE) vs dismissed (DROPPED). **New** `mart_sql.action_acceptance` — the honest
  acceptance rate the acceptance_funnel couldn't give.
- **Alert precision** — when a rule fires and resolves, how often it was real (ACTIONED)
  vs noise, account-wide, with an unlabeled-share trust caveat. **New** account-wide
  roll-up of the existing per-rule `rule_precision`.
- **On solid evidence** — mean `EVIDENCE_COVERAGE` (existing) — how much advice rests on
  complete signals.

Plus a one-line **verdict** (earning its keep / providing value but watch: … / not yet
proven) and the recommended→executed→verified→$ funnel. Deep-links to the ROI track
record and Alerts ▸ Rules precision rather than duplicating them. `None` (not 0%) when
there's no labeled outcome yet — an empty ledger never reads as "0% precise".

New `app/logic/proof.py` (pure, 11 tests) + one mart read; everything else composes
existing telemetry (no new scan, no duplication). Read-only.

App version 4.259.0.


## 4.258.0 - Audit cleanup wave 3: redundancy consolidation (2026-08-17)

Removed/consolidated duplicated panels found by the audit. Cross-page duplicates keep
a compact glance + a deep-link to the single owner page (owner-approved ownership).

Intra-page duplicates removed:
- **Alerts**: the account-wide delivery banner + card rendered twice — dropped the copy
  from the company-scoped Open events section (also a scope-mix); kept on Native delivery.
- **Overview**: "Top warehouse driver" KPI (== the "Top driver" caption) and the
  "Spend, 14d" sparkline (== the spend-trend chart above it) removed.
- **Control Room**: the standalone "Open incidents" KPI (already in the exception summary).
- **Decision Studio**: the Portfolio "Act now" KPI (already in the exception banner).
- **Cost**: the Idle & sizing "Idle spend"/"Projected monthly idle" tiles (== the section
  headline); the Spend storage-table drill is now READ-ONLY (the retention ALTER + booking
  live once, on Optimization); the Idle & sizing idle-suspend copy-expander (the booking
  lives once, on Remediation & ledger).

Cross-page consolidations (owner → the other becomes a glance + link):
- **Egress** and **CoCo/AI spend**: Cost owns the $ story; Security keeps its security
  lenses + a deep-link.
- **Pulse KPIs**: Operations owns the definitions; Control Room keeps the since-yesterday
  glance + a link.
- **Freshness board**: Admin owns the full table; Control Room keeps the stale-count + link.
- **Spend movers**: Cost owns the full table; Control Room keeps a top-3 strip + link.
- **Savings/ROI ledger totals**: Decision Studio ▸ ROI owns the track record; the Cost
  Savings tab keeps the verify workflow + a link.
- **Access "Role grants in window"**: dropped — the Changes ▸ "Recent grant changes" feed
  is a superset (adds revokes + privilege changes).

Three audit "duplicates" were found on review to be distinct-purpose panels and KEPT:
the Admin session-telemetry vs fleet-telemetry tables (different populations), and the
Cost measured-vs-allocated query + pattern lenses (exact QUERY_ATTRIBUTION_HISTORY, a
deliberate contrast — rec#41 — not a copy).

App version 4.258.0.


## 4.257.0 - Audit cleanup wave 2: correctness bug fixes (2026-08-17)

22 confirmed defects from the 9-page audit, verified + fixed (each reviewed against
the code; false positives and documented limitations skipped):

- **Overview**: idle account (0 queries, read succeeded) no longer reads platform
  score "Incomplete"; per-day queue/spill divisor now shares the SQL's UTC/CURRENT_DATE
  clock (was account-time, ~1 day off in the evening); window-spend fallback stays
  complete-days-only (was silently today-inclusive); score help scope wording corrected.
- **Security**: `_change_kind` now buckets every DROP_*/TRUNCATE_* (DROP_ROLE/SCHEMA/USER)
  as "Drop / truncate" so the change chart agrees with the CRITICAL risk score; egress
  builder filters to true cross-region/cross-cloud (same-region/internal no longer
  counted as "egress", matching the empty-state).
- **Control Room**: incident-Gantt open-incident duration now measures against
  account-time now (passed minute-rounded), not the server's UTC CURRENT_TIMESTAMP()
  (unset account TZ = a false multi-hour bar); triage caption corrected to "last 3 days".
- **Alerts / mart_sql**: incident REOPEN_PCT rewritten so the numerator is a subset of
  the denominator (bounded ≤100%); "Eligible to send now" headline counts DISTINCT
  events (was summing per-route, double-counting an event on multiple routes);
  `fact_daily_activity` window aligned to CURRENT_DATE(); `mart_vs_live_recon` gained a
  UNIT column (credits vs statements); `app_cost_quarter` uses config APP_WAREHOUSE.
- **Decision Studio**: "Verified value" sums VERIFIED_USD only over VERIFIED rows (was
  all rows). **actions.realization_pct** restricted to items with a positive estimate.
- **Admin**: "Diagnose stale sources" now flags never-loaded (NULL/0-row) sources (was
  silently dropped); "App queries (14d)" counts the INTERACTIVE workload only.
- **Operations**: standalone (no graph-group) task failures are each their own root
  cause (were collapsed, dropping all but one from incident routing); release compare
  keys tasks by SCHEMA_NAME too; sizing "Idle $" KPI relabeled to what it sums.
- **Cost**: cloud-services rebate priced at the compute rate (not the AI rate) for AI
  rows; AI-spend tile window made explicit in its label.

App version 4.257.0.


## 4.256.0 - Audit cleanup wave 1: formatting + label fixes (2026-08-17)

First wave of a comprehensive audit (9-page deep pass + structural audit). Safe,
self-contained formatting and label corrections:

- **Brief**: "Oldest unacked critical" KPI renamed "Oldest open critical" — the feed
  is STATUS IN ('OPEN','ACK'), so an acknowledged critical still drove the tile; the
  "unacked" label was wrong.
- **Decision Studio**: per-lane cost caption wrapped in md_dollars() — two `$` amounts
  in one caption were rendering the text between them as LaTeX math (the exact bug
  md_dollars exists to prevent).
- **Byte humanization**: Security "GB written out" (unload) and Control Room Entity-360
  GB metric cards now use humanize_gb() (MB/GB/TB scale) instead of a raw ".2f GB" —
  matching the sibling humanized KPIs and the auto-humanized table columns.
- **Percent columns**: Cost ▸ Spend "Share %" (coverage) and "Cloud svc %" tables now
  render the `%` sign (were bare numbers).
- **Duration/units**: Operations heaviest-queries Elapsed/Queued columns labeled "(s)";
  Control Room freshness "Age" labeled "Age (h)".
- **Help/code agreement**: Operations volume-drops help said "≥4 of the prior 7 days"
  but the SQL gates `DAYS_ACTIVE_7D >= 3` — help corrected to ≥3.

App version 4.256.0.


## 4.255.0 - Watch automation: a watched entity now watches itself (2026-08-17)

Answers the owner's question — "when I click Watch in Entity 360, does it do
something behind the scenes, or is it waiting for me?" It was waiting: watching
wrote a row and only showed an SLO badge when you opened the Watchlist tab. Now
each watched **warehouse** is evaluated for a cost move and a health drop, and the
result is surfaced where you land — no external push, no migration, read-only.

- **Cost spike/drop** — the same robust median/MAD anomaly sweep the Cost page runs
  (materiality-gated: >=$50 day, >=10 active days), over FACT_WAREHOUSE_DAILY. Signed
  z carries the direction ("spend spike" vs "spend drop").
- **Health-grade drop** — the per-warehouse health chip (0-100 grade). A grade below
  Healthy reads as "health: Watch/Degraded/At risk"; Degraded/At risk escalate the
  row to warn.
- **Brief badge** (landing page): "N of M watched entities have moved: ..." with a jump
  straight to the Watchlist; a quiet one-line "steady" caption when nothing moved;
  nothing at all when you have no watchlist.
- **Watchlist tab**: a STATUS column per entity plus a lead banner naming what moved.
- Cost + health are warehouse-grain, so a watched query family / product / user passes
  through un-flagged here (the pin + SLO badge still cover it). Cross-company by design
  (a personal watchlist spans companies), so it ignores the page's company filter.
- Cheap + degradation-safe: cost is a mart read; health uses the efficiency MART only
  (probe -- never the heavy live sizing scan); an absent source degrades to cost-only.
- New app/logic/watch_monitor.py (pure, 10 tests); wired via workbench.watched_attention
  / render_watch_badge.

App version 4.255.0.


## 4.254.0 - Wave-3 remainder: idle-waste KPI, per-WH health chip, peer z-score (2026-08-17)

The last three repo-review efficiency lenses, all reusing already-cached warehouse
reads (no new scans). Scouted + designed by a 3-agent workflow; the two novel scorers
adversarially reviewed pre-commit.

- **Idle-credit-waste headline** (Cost > Optimize, above the sub-tabs): the single
  account/company "$ burned in zero-query warehouse-hours" number, priced, with idle
  share and a monthly projection. Promotes what was buried in the Idle & sizing sub-tab.
- **Per-warehouse health chip** (Operations > Warehouses): a transparent 0-100 grade =
  100 − capped penalties for queueing / remote spill / long p95 runtime / low utilization
  (evidence-gated), naming which penalty fired. New app/logic/wh_health.py.
- **Cost-per-query peer z-score** (Operations > Warehouses): cross-sectional (within-
  company) robust z-score of $/query vs the fleet — "this warehouse costs 3.4x the
  fleet median per query". New app/logic/cost_peers.py; a materiality floor drops
  trivial warehouses.
- Review fixes: an integration test now pipes real size_recommendations output through
  the health chip (locks the column contract so a rename can't silently disarm a
  penalty); peer table sorts by multiple-of-median as a small-fleet fallback (z needs
  >=5); disclosed the mart peak-daily p95 and the >=5-warehouse z requirement.

App version 4.254.0.


## 4.253.0 - Wave-3: adaptive-compute candidacy score (2026-08-17)

Repo-review "What to Borrow" wave 3 (efficiency lenses). Which warehouses would
benefit from adaptive / auto-scaling compute? A new Operations > Warehouses panel
scores each warehouse 0-100 from the hour-of-day load profile it already reports
(no new scan).

- Score = burstiness (peak-to-mean of the daily credit profile) x material volume
  x an idle discount. Bursty + material-volume warehouses gain from adding clusters
  at the peak and dropping them off-peak; a flat warehouse is fine at a fixed size;
  a heavy-idle one is flagged **auto-suspend first** (the steadier lever).
- Pure logic app/logic/adaptive.py; KPIs (scored / strong / consider) + a ranked
  table with the peak-to-mean, credits/day, idle %, and a rationale per warehouse.
- Adversarial review pre-commit caught a verdict-inverting bug: the metering feed
  is SPARSE (no row for a suspended hour), so a metered-hours mean read a
  nightly-batch (maximally bursty) warehouse as flat. The mean now divides the
  metered sum by a full 24 hours; a production-shaped sparse test locks it. Also
  fixed the too-weak idle discount (now a hard override at >=50% idle) and the low
  volume floor (2 -> 10 cr/day, so a trivial single-blip can't score 100).

App version 4.253.0.


## 4.252.0 - Reconciliation audit: non-USD currency guards on the billing panels (2026-08-17)

Two org-billing panels compared or labeled USAGE_IN_CURRENCY dollars as USD without
checking the currency (the audit's currency-mismatch findings). Latent on this USD
account; correctness for any non-USD one. Both now guard, matching org_accounts_spend.

- **Rate-card reconciliation** (the "billing truth vs app model" panel): the org side is
  USAGE_IN_CURRENCY, the model side is credits x the USD rate. On a non-USD account the
  Model-vs-org %, AI-model-vs-org %, and Effective $/cr are now suppressed (they'd be
  FX-corrupted), a warning names the real currency, and the $/cr header/format drop '$'.
- **Contract balance / burn / runway**: `remaining_balance_summary` now surfaces the
  ledger currency; the panel formats balance/burn/on-demand in that currency (not a
  hardcoded '$') and the title reads the real currency.

App version 4.252.0.


## 4.251.0 - Decision Studio flagship: the ROI / realization story, front and center (2026-08-17)

The savings ledger already tracked estimated->verified with proof, but the realization
"here's the money we saved" story was buried at the bottom of the Scenarios section. It's
now a first-class **ROI** section (the DS landing), turning four buried KPIs into a
director-facing narrative.

- **New "ROI" section** (first in Decision Studio): a hero line — "OVERWATCH has verified
  $X across N items, realizing Y% of estimates, closing the loop in Z days" — over KPIs for
  verified savings (all-time + this quarter), realization rate, and the **open pipeline**
  (estimated $ still awaiting proof, the opportunity ahead).
- **Run-rate trend** — verified savings by month (proof the loop keeps closing) — and a
  **by-lever breakdown** (FINDING_TYPE) showing where the realized money comes from, each
  lever's verified $ and its own realization rate.
- Pure logic: `savings_by_month` + `savings_by_lever` in actions.py (verified-only,
  estimate-vs-actual). `savings_ledger` builder gains `FINDING_TYPE`. Scenarios now points
  to ROI instead of duplicating the block. No new data source.

App version 4.251.0.


## 4.250.0 - Error-log fixes: 3 SQL-compilation errors from the live log (2026-08-17)

The owner pulled the full `APP_ERROR_LOG` messages. Three distinct SQL-compilation
failures, two of them recent regressions from my own work — all fixed.

- **Alerts `000904` invalid identifier RESOLUTION_NOTE** (×13, the persistent one):
  `resolutions_for_rule` selected `RESOLUTION_NOTE` from `ALERT_EVENTS`, but that column
  was added to `ACTION_QUEUE` (V074), not alerts. The alert resolution note lives in
  `ALERT_AUDIT.NOTE` — now joined on `EVENT_ID` (latest resolve/remediate/note).
- **Cost `001072` "Lateral View cannot be on the left side of join"** (my wave-2
  regression): the `TOKENS_GRANULAR` token-economics builder put `LATERAL FLATTEN` in a
  comma-join left of a `LEFT JOIN USERS`. FLATTEN moved into its own CTE; the join is now
  on the CTE.
- **Alerts `002031` "Unsupported subquery type cannot be evaluated"**: `incident_metrics`
  nested a scalar subquery inside `NULLIF` inside a scalar subquery (COMPRESSION/CHANGE_PCT).
  The incident count + both numerators are hoisted into single-row CTEs and cross-joined;
  the ratios are now plain column arithmetic. Same output columns.

Two remaining log entries are lower-priority: the Cortex-Guardrails `START_TIME` identifier
(wrong column on the optional view — degrades to "not available", now probe-suppressed) and
a single Operations `090234` (current-user-in-proc) that couldn't be located in any builder.

App version 4.250.0.

## 4.249.0 - Snowsight reconciliation: humanize every byte surface (2026-08-17)

A 7-agent audit checked 48 user-facing numbers against their Snowsight equivalents:
**zero high-severity gaps** (the data-transfer bug was the worst of its kind), but the
same unit-hiding root cause recurred on ~10 byte surfaces. Fixed systemically.

- **`_auto_formats` humanizes every `_GB`/`_TB`/`_MB` table column** (token-based, so
  `SPILL_REMOTE_GB`, `TB_SCANNED`, and bare `GB` all match; a `PER` token excludes rates
  like `SPILL_GB_PER_DAY`). Display-only (the frame stays numeric — sort/CSV keep the raw
  value), matching the Snowsight MB/GB/TB scale. Verified all 28 matched columns are byte
  magnitudes, no false positives. New `humanize_gb` covers the KPI values.
- **Sites fixed:** Security ▸ Egress KPI + table; Operations query-drill "Scanned"/"Spill"
  + the window remote-spill KPI; the poor-pruning `TB_SCANNED` and `TOTAL_TB_SCANNED`
  tables; and **Control Room's Pulse — which literally flagged "Remote spill: 0.0 GB"** as
  an exception (fires on `>0` but rounded to `0.0`); now shows e.g. "30.7 MB".
- **Per-table storage `$`** now shows cents (`$%.2f`, was `$%.0f` hiding sub-dollar tails).

The audit's remaining findings are all medium/low (labeling + latent non-USD currency
guards on the rate-card panel) — deferred, not shipped here.

App version 4.249.0.

## 4.248.0 - Live-screenshot fixes: grant noise, data-transfer units, DS self-rank (2026-08-17)

Three app-only fixes from owner screenshots (the backup-clone fix is V089; the Alerts
000904 needs its full error text).

- **Grant-changes feed: object-lifecycle noise removed.** Every object a stored proc
  creates (TMP_* stages, file formats, procedures) records an `OWNERSHIP` grant by the
  creating role to *itself* — that flooded the feed (500 rows of `TF_..SYSADMIN →
  TF_..SYSADMIN OWNERSHIP ON STAGE TMP_*`). Now excluded on both role arms (NULL-safe);
  a real ownership **transfer** (different grantor) still shows. Disclosed in the caption.
- **Data transfer reconciles to Snowsight.** The Egress panel led with *billable* transfer
  (genuinely ~$0) in **TB at 3 decimals**, so 891 MB read `0.000 TB`. New `humanize_bytes`
  (MB/GB/TB, Snowsight scale) + a **Total transferred** KPI (all bytes, billable + free)
  that ties to Snowsight ▸ Cost Management ▸ Data Transfer; the table shows a humanized
  Volume (raw bytes kept for sort/CSV).
- **Decision Studio stops ranking itself.** The workload portfolio's #1 ACT-NOW was
  OVERWATCH's own runtime (`execute streamlit … OVERWATCH_APP()`). The app's Streamlit
  family is now excluded (COALESCE-guarded so a family-mart join miss stays in).

App version 4.248.0.

## 4.247.0 - Repo-review wave 2: second opinions, coverage maps, token economics (2026-08-17)

Four adoptions from the external-repo review, every read probe-gated with an honest
degrade (all four sources are optional or schema-uncertain on this account), plus the
mart-recon false-drift fix and review hardenings.

- **Cost ▸ Spend — native anomaly second opinion** (toggle). Snowflake's managed ML
  cost-anomaly feed (`SNOWFLAKE.LOCAL.ANOMALY_INSIGHTS`) beside our z-score sweep:
  both-flag = high confidence; native-only = what the z-score misses; ours-only = a
  tuning candidate. Raw render on purpose (schema-uncertain).
- **Operations ▸ Queries — Snowflake-authored query insights.** `QUERY_INSIGHTS` rolled
  up by insight type — the engine's own repeated improvement suggestions beside our
  family heuristics.
- **Operations ▸ Warehouses — resource-monitor coverage.** Which warehouses run with NO
  spend cap (and each monitor's % consumed). Understands ACCOUNT-level monitors (an
  account cap suppresses the "uncapped" alarm) and the SHOW-privilege caveat.
- **Cost ▸ Chargeback & AI — token economics** (toggle). `TOKENS_GRANULAR` flattened to
  input / output / cache-read / cache-write per user, with a token-weighted **fleet
  cache-hit %** and per-user cache efficiency — the CoCo cost lever raw totals can't show.
- **Mart-recon false drift fixed.** The owner's +93% "BAD" query-count drift: the fact
  side now excludes both warehouse-less conventions (`NULL` and the string `'NONE'`);
  root cause honestly documented as unconfirmed — if drift persists, investigate the loader.
- **Review hardenings:** new `missing_column` error kind so probe-gated optional-COLUMN
  reads degrade quietly instead of error-logging every render; the second-opinion panel
  is a toggle (not an expander) per the Cost page's own deferral rule; the grant-changes
  feed moved to the long-TTL cache tier (owner telemetry: 1–2 min scans).

App version 4.247.0.

## 4.246.0 - AI guardrails, known-spike calendar, critical veto (2026-08-17)

Repo-review wave 1 (owner: "we need them now — CoCo spend is 13% of total and growing").
All three adversarially reviewed pre-commit; the review caught two real window-math bugs
(both fixed + regression-locked) and drove four honesty hardenings.

- **Security ▸ AI guardrails** (new section). Two halves: **AI usage behavior** — per-user
  Cortex Code flags computed in pandas from the SAME cached user-day scan Cost already
  pays for (zero new scans): velocity ≥3× the user's own prior-28d baseline (with a
  ≥20-requests/7d noise floor), 7d-token outliers (robust z vs the active population),
  weekend-share, and new+heavy adopters; KPI row leads with CoCo 7d spend. **Guardrail
  flags** — the optional `CORTEX_AI_GUARDRAILS_USAGE_HISTORY` view, probe-gated with an
  honest not-enabled state and a validate-before-trusting caption on the response-count
  heuristic. Review fixes: the 7d window is half-open (excludes today's partial day; the
  8-day span inflated velocity ~14%), and baselines divide by the days a user actually
  existed (a 10-day-old steady user no longer reads ~10× and false-flags for weeks).
- **Known-spike calendar.** `EXPECTED_SPIKE_CALENDAR` SETTINGS string
  (`MONTH_END:<n>; QUARTER_END:<n>; YYYY-MM-DD..YYYY-MM-DD:<label>`) — predictable
  spikes are labeled "expected (month-end)" instead of anomalous, on **all four** anomaly
  surfaces (Cost sweep, Overview trend markers, Operations warehouses, Control Room
  triage — review caught the single-surface gap). Collapses (z<0) are never suppressed;
  malformed rules never break the sweep.
- **Critical veto on the platform score.** Weighting-then-averaging could read
  "Healthy 94" with an open critical. Any open critical now caps the score below the
  Healthy band (84) with an explicit "Critical veto" driver — a critical is a verdict,
  not a weight. Retro trend divergence (facts carry raised-that-day, not open counts)
  documented in `score_history`.

App version 4.246.0.

## 4.245.0 - Codex #20: product-mapping coverage (2026-08-17)

Decision Studio's Products board showed mapped product $ but no denominator — an
operator couldn't tell whether product economics covered 90% or 12% of spend, and
the unmapped residual was invisible.

- New `workbench_sql.product_mapping_totals` returns the WHOLE-account object cost and
  catalog-entity counts (same DAY/company basis as the product view; the TOTAL drops the
  `DATA_PRODUCT` predicate the mapped view applies).
- The Products board now leads with a **coverage** KPI row: **Product-mapped object cost**
  (as a % of account object cost, red under 50%), **Unmapped object cost** (the residual —
  invisible to the board), and **Entity coverage** (mapped/total catalog entities), plus a
  caption to read the per-product totals as a floor until coverage is high.

App version 4.245.0.

## 4.244.0 - Codex wave-1: verify-proof gate, storage label, filter-test coverage (2026-08-17)

Three confirmed items from the ground-truthed Codex review (#22, #17, #37).

- **#22 — experiment verify requires proof.** A `VERIFIED` experiment books
  `SAVINGS_LEDGER` and feeds the director-facing "Verified savings / Realization"
  headline, but the Save button accepted a blank result note, `$0`, and an open
  observation window. It's now gated on: non-empty result evidence, verified `$ > 0`,
  and the observation window having closed — with a message naming what's missing.
  (Proc-level enforcement in `SP_VERIFY_EXPERIMENT` is a recommended follow-up.)
- **#17 — storage label accuracy.** `AVERAGE_DATABASE_BYTES` already includes Time
  Travel, so the per-DB card + `metric_registry` "active + fail-safe" wording
  under-counted; now reads "active + Time Travel + fail-safe."
- **#37 — filter-matrix auto-discovery.** `test_p4_filter_matrix` hand-listed 13 SQL
  modules and silently omitted 5 (`workbench_sql`, `dq_sql`, `app_cost_sql`,
  `directory_sql`, `alert_evidence_sql`) — including Decision Studio's company-taking
  builders. It now globs every `app/data/*_sql.py`; the newly-covered builders all pass
  the scoping/injection/clamp invariants (test count +18).

App version 4.244.0.

## 4.243.0 - Fix: portfolio FAILS coalesced to 0 defeated evidence-presence (Codex #1) (2026-08-17)

Ground-truthing a 50-item external review confirmed its flagship defect. Decision
Studio's portfolio logic (`decision.py:28-40`) is *built* to treat a missing behavioral
measurement as absent (NULL), not a measured 0 — it tracks presence via `notna()` on
`FAILS`/`AVG_CACHE_PCT`/`P95_SEC` so a blind family can't reach an ACT-NOW call. But the
SQL (`workbench_sql.py:351`) `COALESCE(f.FAILS, 0)` on the family-mart LEFT JOIN meant
`FAILS` was **never** NULL, so `has_fails`/`has_behavior` were always true and
`EVIDENCE_COVERAGE` floored at 0.33 for a genuinely-unmeasured family — inflating the
"Evidence coverage" KPI and silently disarming the `~has_behavior` guards.

- **Fix:** emit `f.FAILS` raw (NULL on a join miss), matching its already-raw siblings
  `AVG_CACHE_PCT`/`P95_SEC`. `decision.py` already tolerates it (`fillna(0)` keeps
  `FAIL_PCT` correct; `notna()` now reads true absence). One-line SQL change; the logic
  gate was already tested — added a builder test so it can't regress.

App version 4.243.0.

## 4.242.0 - Honest account-wide scope notes (company-filter audit, bucket B) (2026-08-17)

The company-filter audit (v4.241.0) found the *scopable* sections and fixed them; this
closes the honesty gaps on the sections that are account-wide **by necessity** (no
company grain), so a picked company never makes one look narrowed. One-line disclosure
notes added to: Overview **Contract runway**, **Morning AI digest**, **Forecast-accuracy**
backtest; Control Room **"reached nobody"** banner (now says "account-wide"); Cost
**Egress**; **Savings ledger** "Verified savings" ("the number to quote" — now states it's
account-wide); Unit-cost **AI spend** KPI; Security **Unused roles** header ("(account-wide)");
Security **Change-risk noise** diagnostic; Decision Studio **Realization**. No data changes —
these sections have no company dimension to filter by; the fix is to say so.

App version 4.242.0.

## 4.241.0 - Company filter: make triage filters actually apply (2026-08-17)

Owner ask: "we need to filter between company — the triage filters need to apply …
scan the app so we're selecting appropriately." A 7-agent audit swept every page:
**zero silent-mislead bugs** (the app already declares account-wide scope where data
has no company grain). The real work was making the *scopable* sections honor the pill.

- **Owner/triage `ACTION_QUEUE` now honors company.** `mart_sql.action_queue` gains a
  `company` param (that company's actions PLUS account-level 'ALL' ones; 'ALL' = no-op,
  backward compatible). Wired into every page that reads it: Overview "Top actions"
  (contract updated `applies=("company",)`), Control Room "Action Center" fallback,
  Decision Studio "Scenarios" fallback, and the Brief. So picking ALFA/Trexis narrows
  the work queue everywhere it appears.
- **Recent grant changes** (v4.240.0 feed) now scopes by grantee — user grants via user
  classification, object/role grants via the `%TRXS%` role heuristic.
- **Operations ▸ Pipeline SLA**: the file-load-failure panel now narrows to the company
  (the builder already supported it, hardcoded 'ALL'); the contract declares the partial
  scope honestly. (Volume/DT/row-volume panels stay account-wide pending a follow-up.)
- The remaining audit findings are genuinely account-wide sections (metering, org billing,
  posture, savings ledger) where the correct fix is a disclosure note, not fake filtering —
  itemized for the owner to direct.

App version 4.241.0.

## 4.240.0 - Security: recent grant-changes feed (2026-08-17)

Owner ask: "show the most recent grant changes to roles/users/objects — who changed
what to whom and at what time, ordered by recent." The Changes section had an
*aggregated* DDL/DCL view (statements/day, by user) but no granular, time-ordered
grant log, and the one existing builder covered only role→user grants.

- **`Security ▸ Changes ▸ Recent grant changes`** (top of the section). A newest-first
  feed where each row is ONE change event: **When · Change (granted/revoked) · Type
  (role→user / privilege→role) · Granted by · To (grantee) · What (the role, or the
  privilege on an object)**. Unions `GRANTS_TO_USERS` (role→user) and `GRANTS_TO_ROLES`
  (privilege→role on objects); a grant and its later revoke are two rows at their own
  timestamps. A 7/30/90/180-day window selector; KPIs for total / granted / revoked.
- Honest attribution: `GRANTED_BY` is the acting **role** Snowflake records natively;
  the DDL/DCL panel below still shows the **user** who ran a GRANT statement.
- New builder `security_sql.recent_grant_changes`. The Changes section is not first
  paint and the read is hourly-cached; live-scan budget +1 (justified).

App version 4.240.0.

## 4.239.0 - V088: change-risk exclusion, data-validated (2026-08-17)

The security change-risk diagnostic (v4.236.0) did its job: the owner's screenshot
ground-truthed the DESTRUCTIVE flood as **94% `TF_*` roles, 0% unattributed, ~0% on
the app's own DB** — proving what two adversarial reviews couldn't assume. So V088
(shelved pending that evidence) is now finalized. Generated by `outputs/gen_v088.py`,
byte-locked by `tests/test_v088_...`; owner applies **V088 in Snowsight after V087**.

- **`TF_*` pattern, evidence-backed.** Re-derives `V_SECURITY_EXCEPTION_QUEUE` (from
  the V075 base, same as V080) with the CHANGE RISK exclusion generalized to the
  Terraform service-role convention (`ROLE_NAME LIKE 'TF_%'`) instead of V080's fixed
  18-role list the flood outran. Clears ~94% of the noise; the non-`TF_*` remainder
  (real signal) still surfaces.
- **App-DB carve-out narrowed to `PUBLIC`** (reviewer fix): drops the
  `DBA_MAINT_DB.PUBLIC` scratch churn but keeps `DBA_MAINT_DB.OVERWATCH` — so a
  DROP/TRUNCATE of OVERWATCH's own audit tables stays visible (no self-blind spot).
- Still `CHANGE_KIND='DESTRUCTIVE'`-only (GRANT/REVOKE/POLICY surface), NULL-safe via
  `COALESCE`, rows stay in `FACT_SECURITY_CHANGE` for audit. Supersedes V080.

App version 4.239.0.

## 4.238.0 - Number formatting: dollars as $, durations humanized (2026-08-17)

Owner ask: "make sure costs are dollars and time is in Hr/min/sec/ms." A parallel
audit swept the metric-heavy UI; six real mis-formats fixed (rest already correct).

- **Costs render as `$`, not `N USD` / raw floats.** Spend ▸ "All-in billed"
  (`4,775 USD` → `$4,775`) and its help; the Replication fallback KPI (`229.33` →
  `$229.33`) and its `Spend` table column (raw 6-decimal floats → `$%.2f`); the
  data-transfer reconciliation caption. All currency-aware — `$` for USD, currency
  code preserved for a non-USD org rate card.
- **Per-run dollars formatted.** The query-pattern and pipeline "Daily detail"
  tables gave `USD_PER_RUN` no column config, so it rendered as a bare `0.0034`
  beside a `$`-formatted `USD` column; both now carry `$/run` (`$%.4f`).
- **p95 change KPI humanized.** The warehouse-change before/after KPI showed the
  p95 metric as a bare second count (`1800 → 5400`); it now humanizes only that
  metric (`30m → 1h 30m`) while credits/queue/spill/fail keep their units.
  `wh_change.change_deltas` gained a `col` field so the UI formats by native unit.

App version 4.238.0.

## 4.237.0 - Storage: table-level drill with dollars + retention candidates (2026-08-17)

Owner ask: "a breakdown of what's driving the storage costs … drill down to the
table level and calculate … adjust time travel options or purge tables." The
table-level view existed only on Optimization & Savings, disconnected from the
Storage tab.

- **`Cost ▸ Spend ▸ Storage ▸ Drill to tables`.** Under the storage detail toggle,
  the per-database bars now drill to the table level: pick a database and see its
  tables (top 50 by on-disk size) with **active / time-travel / fail-safe priced
  in dollars** at the configured $/TiB, plus total and reclaimable columns, current
  `RETENTION_DAYS`, and a STALE flag (no DML in 90 days = the clearest
  reduce-retention / purge candidate). Headline KPIs: total table storage, the
  time-travel + fail-safe portion, and how much of that sits on STALE tables.
- **Act on it.** Pick a table and a target retention to get the exact
  `ALTER TABLE … SET DATA_RETENTION_TIME_IN_DAYS` (copy-run; the tracked execute +
  savings-ledger booking stays on Optimization & Savings). Captions state the honest
  mechanics: this is a point-in-time snapshot (won't sum to the MTD-average card
  above), lowering retention frees time-travel, dropping recovers active +
  time-travel, and fail-safe (permanent tables) only ages out over 7 days.
- New builder `insights_sql.table_storage_breakdown` (total-ordered, the driver
  lens — distinct from `storage_waste`'s waste ordering). Cost page scan surface
  gains TABLE_STORAGE_METRICS / TABLE_DML_HISTORY / TABLES (trust pin updated).

App version 4.237.0.

## 4.236.0 - Security: change-risk noise diagnostic (2026-08-17)

Bug follow-up (owner "still have that destructive issue"): V080 excluded the
CHANGE RISK DESTRUCTIVE flood from a fixed 18-role list and it still floods, so
the driving roles aren't in the list. Rather than broaden the exclusion on an
unverified guess — adversarial review found a `TF_*` pattern both risks
over-suppression (a claimable class; blinds OVERWATCH's own audit tables) and
may not even match the real drivers — this ships the **evidence first**.

- **`Security ▸ Change-risk noise` diagnostic.** A new expander on the Security
  decision queue reads `FACT_SECURITY_CHANGE` directly and groups exactly the rows
  the CHANGE RISK arm counts (`DESTRUCTIVE`, `RISK_SCORE>=70`, 7d) by role /
  database / schema, flagging whether each role matches the Terraform service
  convention (`TF_*`). Headline KPIs: total events, % by `TF_*` roles, % with no
  attributed role, % on the app's own DB — so it's immediately visible whether a
  `TF_*` exclusion would clear the flood or whether the drivers are something else.
- The precise exclusion (a follow-up migration) is deferred until this diagnostic
  identifies the actual roles — no blind change to security monitoring.

App version 4.236.0.

## 4.235.0 - AI evaluation grounded in per-alert-family evidence (2026-08-17)

Bug (owner report): the Alerts "Explain with AI" evaluation summarized data
unrelated to the selected alert. Root cause — every COST_*/PERF_* alert was fed
ONE evidence pack (query families by elapsed-hours on a title-scraped warehouse),
so a cloud-services-ratio, Cortex-spend, serverless-creep, or fingerprint-drift
alert got query-latency rows that don't explain its metric (and, with no warehouse
in the title, went account-wide).

- **Per-family evidence.** A new resolver (`app.logic.alert_evidence`) maps each
  alert to the evidence shape that explains it, and a dispatcher
  (`app.data.alert_evidence_sql`) assembles it: cloud-services ratio → CS credits
  by query shape on the warehouse; AI creep → daily Cortex credits; serverless
  creep / metering anomaly → the named service's daily credits; fingerprint drift
  → that exact family's p50/p95 latency history (matched by the statement sample,
  which — being real SQL — bypasses the keyword-stripping UI filter); queued
  minutes → the warehouse's worst queueing hours. Query-latency anomalies without
  a bespoke pack keep the original elapsed-hours pack.
- **Matching framing.** `alert_evidence_prompt` frames each pack for its metric so
  Cortex reasons about credits/latency/queueing as appropriate — still evidence-only,
  still "never invent numbers." When scope can't be resolved, the affordance is
  withheld rather than showing off-topic evidence.

App version 4.235.0.

## 4.234.0 - Unmapped entities: $ at stake + one-click company mapping (2026-08-17)

Live-app punch-list, data batch. Cost ▸ Spend & Attribution ▸ Unmapped entities.

- **Cost on the worklist.** The unmapped table gains an "Est. $ (window)" column and a
  headline "Unmapped spend, billed blind" KPI — warehouse credits × contract rate — so the
  dollar exposure of UNKNOWN-stamped compute is visible, not just a credit count. DATABASE/
  USER rows (counts, no direct $) stay blank.
- **Map an entity → company.** The static INSERT hint becomes an interactive mapper: pick an
  UNKNOWN entity + a company and get a ready-to-run, idempotent `COMPANY_SCOPE` upsert (MERGE);
  an operator applies it in place, everyone else copies it to Snowsight. GRAIN maps to the
  right SCOPE_TYPE (USER → USER_OVERRIDE). The caption states the backfill contract: go-forward
  stamps immediately, the nightly reconcile re-stamps the trailing 3 days, older history waits
  on a loader backfill re-run.

App version 4.234.0.

## 4.233.0 - Sidebar polish, $-font fix, visible query drill-through (2026-08-17)

Live-app punch-list, UI batch (app-only). Chrome + Operations.

- **Sidebar, pronounced.** OVERWATCH now renders as a prominent wordmark (1.55rem, tracked,
  gradient) over a "Snowflake Command Center" subtitle. The `· v{APP_VERSION}` line and the
  "Connected · {role} view" caption are gone from the sidebar — version already lives on
  Admin ▸ App version; a quieter "last refreshed" note replaces the role caption.
- **$-font fix.** The Cost ▸ Spend root-cause waterfall narrative carries several dollar
  amounts; unescaped, Streamlit rendered everything between two `$` as serif-italic LaTeX.
  Wrapped in `md_dollars()` (the escape the rest of the page already uses).
- **Visible query drill-through.** Operations ▸ Queries "Heaviest queries" now shows the
  `QUERY_ID` column, and clicking a row fills the "Paste any query ID" box with that id — the
  click now has a visible effect and the drill target is always identifiable.

App version 4.233.0.

## 4.232.0 - Decision Studio: portfolio coverage % + per-lane cost (2026-08-17)

CoCo DS review (§6), cheap wins #2 + #1. App-only, Portfolio tab.

- **#2 — evidence-coverage KPI.** A new "Evidence coverage" KPI shows the portfolio-wide
  average share of the three evidence signals (cache, latency, fail-rate) present per family
  (amber below 80%), so it's visible at a glance how much of the board's recommendations rest
  on complete vs partial evidence — the per-row `EVIDENCE_COVERAGE` shows which.
- **#1 — per-lane cost subtotals.** A caption under the chart breaks the measured 30-day cost
  across the ACT NOW / PLAN / VALIDATE lanes, so the cost concentration reads at a glance —
  labeled as observed cost, not promised savings.

App version 4.232.0.

## 4.231.0 - Decision Studio: stale-planning flag (2026-08-17)

CoCo DS review (§6), cheap win #34. App-only.

- **Open actions that have gone stale now stand out.** The Scenarios action table gains a
  **STALE** flag (open action whose `UPDATED_AT` is 30+ days old — the plan was made and then
  forgotten, so its estimate is decaying) plus a banner counting them, a prompt to re-estimate,
  act, or close. New pure `logic.workbench.stale_planning`. App version 4.231.0.

## 4.230.0 - Decision Studio: experiment duration (2026-08-17)

CoCo DS review (§6), cheap win #24. App-only.

- **The Experiments tab now shows how long each experiment has been active.** A new `AGE_DAYS`
  column (days since `CREATED_AT`) plus an **"Oldest active"** KPI (the longest-running
  planned/running/observing experiment, amber at ≥ 30 days) make a stuck experiment visible —
  "RUNNING 38d" reads as a prompt to verify or close it, not silent progress. New pure
  `logic.workbench.experiment_age_days`. App version 4.230.0.

## 4.229.0 - Decision Studio: the realization / ROI story (2026-08-17)

CoCo Decision-Studio review (§6), the flagship (#40 + #31 + #19). App-only — no
migration.

- **Decision Studio now surfaces the savings ledger's track record, not just a verified
  total.** The Scenarios tab gains a **"Realization — the savings track record"** panel: verified
  savings all-time, **verified this quarter**, the **realization rate** (verified dollars as a
  share of what those verified items were originally estimated to save — the honest
  estimate-vs-actual, never mixing estimated and verified dollars), and **average time to
  verify** (booking → verification). This is the director-facing proof that the decision loop
  closes, and the data was already in the ledger — `realization_pct` shipped for Cost #9;
  `logic.actions.ledger_totals` now also computes `verified_qtd_usd` (current calendar quarter,
  from `VERIFIED_AT`) and `avg_days_to_verify`. A failed ledger read shows a no-data state, and
  realization reads "—" until something is verified — never a misleading healthy $0. The old
  single "Verified separately" projection KPI is replaced by this fuller panel. App version
  4.229.0.

## 4.228.0 - V087: security finding → monitored rule (2026-08-17)

CoCo review, Tier-3 (Sec35) — the last of the alert-migration set. New owner-applied
migration `snowflake/migrations/V087__security_posture_rule.sql` (generated by
`outputs/gen_v087.py`, byte-locked) **plus an app change** — a generate-INSERT UI on
Security. Owner applies V087 in Snowsight (after V086) before deploying the app.

- **A security posture finding can now become a rule that re-alerts when the posture
  degrades again.** The Security decision queue turns a finding into a work item, but not
  into something monitored. V087 makes the SECURITY posture family data-driven: `ALERT_CONFIG`
  gains a `METRIC_NAME` naming a `MART_SECURITY_POSTURE_DAILY` metric (`MFA_GAP_USERS`,
  `EXPIRED_CRED`, `UNUSED_ROLES_90D`, `BREAKGLASS_GRANTS_30D`, … — all counts of problems, so
  higher = worse). `SP_ALERT_SCAN` is re-derived from V086 with **one generic arm [21]** that
  raises *every* enabled rule carrying a `METRIC_NAME` when its newest posture reading is at or
  over the rule's `THRESHOLD_NUM` (stale posture > 2 days old is excluded; daily-deduped). Rules
  are operator-created — the Security decision queue gains a **"Monitor as a posture rule"**
  generator (`logic.security.posture_alert_rule_sql`, an upsert `MERGE` — re-running for the same
  metric updates its threshold/severity rather than silently no-op-ing) — so the single arm is the
  shared raiser for all of them. The only `SP_ALERT_SCAN` edit vs V086 is the arm and the
  rule-block count 15→16 (a test reverses both to reproduce V086 byte-for-byte). App version
  4.228.0.

  Adversarially reviewed (2-lens): no correctness defects; the generic arm, the higher=worse
  comparator (every posture metric is a problem count), and the alert-consistency guards all
  check out. Caught + fixed the WHEN-NOT-MATCHED-only silent-no-op on a threshold change (now an
  upsert).

## 4.227.0 - V086: per-event alert snooze (2026-08-16)

CoCo review, Tier-3 (Alerts29). New owner-applied migration
`snowflake/migrations/V086__alert_snooze.sql` (generated by `outputs/gen_v086.py`,
byte-locked) **plus an app change** — a Snooze action on the Alerts page. Owner applies
V086 in Snowsight (after V085) before deploying the app.

- **An operator can now silence one specific alert until a wake time — "handle this Monday"
  — without acking or resolving it.** Per-event grain (your pick). A snoozed event moves to
  `STATUS='SNOOZED'` with a server-computed wake time, so it leaves the OPEN/ACK triage feed
  on every page (Alerts, Brief, Control Room) with **zero read-path change** — the feed reads
  are byte-identical whether or not V086 is applied, so there's no ordering hazard. The
  still-present row keeps the scanner's dedup from re-raising the same condition while it
  sleeps. A wake step spliced into the hourly `SP_ALERT_SCAN` (re-derived from V084; the only
  edit is the wake block, so reversing it reproduces V084 byte-for-byte) returns expired
  snoozes to their **true prior status** (ACK if the event was acked, else OPEN — so a woken
  event never strands a stale ACK on an "open" row) so they self-resurface within the hour.
  `SP_ALERT_SNOOZE` sets the snooze atomically with an `ALERT_AUDIT` row and `OW_ACTION_INTENTS`
  idempotency, mirroring `SP_ALERT_LIFECYCLE`; `hours=0` un-snoozes early. The Alerts page gains
  a **Snooze** action beside Ack/Resolve with duration presets (1h → 1 week) — one click
  (reversible, auto-wakes) and audited — plus a **Snoozed** view listing sleeping alerts with a
  **Wake now** control, so a snooze is never a black hole. App version 4.227.0.

  Adversarially reviewed (3-lens + focused re-verify): the critical STATUS-sweep confirmed
  `SNOOZED` drops off every feed via the existing positive `STATUS IN ('OPEN','ACK')` allow-lists
  (zero read-path change) and caught the ACK-on-wake inconsistency (fixed above); a follow-up
  verify caught a fallback-only audit-ordering slip (fixed).

## 4.226.0 - V085: SLO-breach proactive alert (2026-08-16)

CoCo review, Tier-3 (Control Room #16, the evaluate+notify half). New owner-applied
migration `snowflake/migrations/V085__slo_breach_alert.sql`. **No app-code change —
owner applies V085 in Snowsight (after V084); breaches then raise server-side and show
up on the existing Alerts page.**

- **A breaching SLO objective now pages, not just badges the watchlist.** v4.223.0 shipped
  the read half of CR16 — the watchlist shows a "crossed threshold" badge by reading the
  already-evaluated SLO objectives. V085 is the evaluate+notify half: a new raiser
  `SP_SLO_BREACH_SCAN` evaluates the configured `SLO_OBJECTIVES` against the warehouse/task/
  family marts (the same logic the app's `slo_cockpit` renders) and raises a HIGH alert for
  each objective whose measured value is in **BREACH**. It seeds a PERFORMANCE rule
  `PERF_SLO_BREACH` via the house idempotent MERGE, and runs from a new task
  `TASK_SLO_BREACH_SCAN` serialized *after* the hourly mart load (fresh marts; the V075
  task-graph law). STALE / NO_DATA objectives are excluded, so a stalled loader never
  manufactures a breach; deduped per objective per day — an SLO breach is a persistent
  condition, so a daily "still breaching" reminder, not a discrete event. The scanner is not
  fired at apply time and self-corrects on its schedule. This is the SLO-alerting migration
  of the set. App version 4.226.0.

## 4.225.0 - V084: SEC_NEW_EXPOSURE proactive alert (2026-08-16)

CoCo review, Tier-3 (Security #36). New owner-applied migration
`snowflake/migrations/V084__security_new_exposure_alert.sql` (generated by
`outputs/gen_v084.py`, byte-locked). **No app-code change — owner applies V084 in
Snowsight (after V083); the alert then fires server-side and shows up on the existing
Alerts page like any other rule.**

- **A new grant to PUBLIC now pages, instead of waiting to be noticed.** A privilege
  granted to PUBLIC is inherited by every role in the account, so a new one is a real
  widening of the blast radius. The app already showed PUBLIC grants passively (Security →
  Access); V084 turns that into proactive alerting. It seeds a SECURITY rule
  `SEC_NEW_EXPOSURE` (HIGH, threshold 1, 24h) via the house idempotent MERGE, and adds arm
  `[20]` to `SP_ALERT_SCAN` that raises a HIGH alert once per new grant to PUBLIC in the last
  24h — reading `ACCOUNT_USAGE.GRANTS_TO_ROLES`, deduped on grant identity (PRIVILEGE ·
  GRANTED_ON · CREATED_ON) so distinct same-day grants each page and an already-alerted grant
  never re-pages as it ages through the rolling window; a batch `GRANT ON ALL` collapses to one
  event counting its objects. `SP_ALERT_SCAN` is re-derived from V079; the only edits vs V079 are
  the new arm and the rule-block count 14→15 (a test reverses both to reproduce V079 byte-for-
  byte). No table or task change; the scanner is not fired at apply time and self-corrects on
  its hourly schedule. This is the alert half of the security-alerting migration set. App
  version 4.225.0.

## 4.224.0 - Predictive pipeline-SLA on Operations (2026-08-16)

CoCo review, Tier-3 (Operations #10). New `data.insights_sql.pipeline_sla_forecast`
+ `logic.insights.pipeline_sla_forecast`.

- **The pipeline-SLA tab now warns before a miss, not only after.** The freshness SLA view
  was purely reactive — `SLA_MET` is a right-now verdict. A new reader joins every registered
  table to its typical refresh cadence (median gap between DML events over 14 days, from
  `TABLE_DML_HISTORY`) beside its runway to the deadline (`MAX_AGE_HOURS − HOURS_SINCE`), and a
  pure scorer folds them into a forward-looking tier: **Overdue** (meets SLA now, but it has
  been more than ~1.5× its own median refresh gap since the last update — a stalled pipeline is
  the likely cause), **At risk** (the deadline sits within one typical refresh cycle, so a
  single skipped refresh breaches it; tables with no cadence history fall back to a
  runway-proximity check), plus Breached/On-track. A "Trending to miss" KPI and a forecast
  table surface the tables sliding toward a miss while they still meet SLA. The tab's read moved
  from the reactive `PIPELINE_SLA_STATUS` mart to the cadence-joined reader (Operations live-scan
  budget 26→27; `TABLE_DML_HISTORY` already reachable; nested non-first-paint tab, tier=recent).
  App version 4.224.0.

## 4.223.0 - Crossed-threshold badge on the watchlist (2026-08-16)

CoCo review, Tier-3 (Control Room #16, the surfacing half). New pure
`logic.workbench.watchlist_threshold_status`.

- **The watchlist now shows which watched entities have crossed a threshold.** CoCo's CR16
  asks to turn the passive watchlist into per-entity threshold alerts — the full feature
  needs a threshold table + evaluation job + notify path (an owner migration,
  `SP_SLO_BREACH_SCAN`, tracked separately as CR16-push). The read-only surfacing half is
  buildable today: `SLO_OBJECTIVES` already *is* a per-entity threshold table and
  `slo_cockpit` already evaluates it to MET/BREACH/STALE/NO_DATA. A new pure joiner annotates
  each watched entity (by ENTITY_TYPE + ENTITY_KEY, worst-status wins) with a `THRESHOLD`
  badge — **⚠ Crossed threshold** when an objective is breaching — plus a banner counting how
  many watched entities have crossed. Probe-gated: degrades to the plain list before V074 or
  when no objective covers a watched entity; reads core marts (no ACCOUNT_USAGE, no budget
  impact) on the nested non-first-paint Watchlist sub-tab. App version 4.223.0.

## 4.222.0 - Admin-grant timing check on Security (2026-08-16)

CoCo review, Tier-3 (Security #2, the time-context half). New
`data.security_sql.admin_grant_context` + `logic.security.grant_anomaly_flags`.

- **Admin-role grants now carry their timing context, not just a count.** A count of
  admin grants is volume; the delta a reviewer acts on is *who* and *when*. A new
  toggle-gated Access panel reads admin-role grants over 90 days and flags two things the
  bare grant list can't: a **first-ever elevation** (the grantee has no prior grant of that
  role on record — computed from the same `GRANTS_TO_USERS` history, which retains revoked
  rows, so a grant→revoke→re-grant is *not* miscounted), and an **off-hours / weekend grant**
  (landed outside business hours account-local, i.e. outside a normal deploy window).
  Surfaced as First-ever / Off-hours KPIs plus a severity-ranked table with a plain-English
  reason. Evidence only; no alert fires. Read-only, off first paint. Complements v4.221.0's
  structural self-escalation scoring — that answers "who *can* escalate," this answers "who
  just got a suspicious grant." App version 4.222.0.

## 4.221.0 - Self-escalation scoring on effective-access paths (2026-08-16)

CoCo review, Tier-3 (Security #2). New `logic.security.escalation_flags`;
`data.security_sql.effective_access` now publishes `REACHES_ADMIN`.

- **The effective-access graph now scores escalation, not just static privilege.** `RISK_SCORE`
  counts object privileges but is blind to admin-role membership, so a path that inherits
  ACCOUNTADMIN while holding few listed grants can score low despite being god-mode. A recursive
  path that inherits an admin role (`ACCOUNTADMIN` / `SECURITYADMIN` / `SNOW_*ADMINS`) is now
  flagged `REACHES_ADMIN`, and a new pure scorer (`logic.security.escalation_flags`) folds path
  depth and admin-reach into an `ESCALATION_SCORE`. A path whose effective role holds MANAGE
  GRANTS — the privilege that can grant any role (including admin) to anyone, i.e. grant itself
  higher — is flagged `SELF_ESCALATION` and counted in a new "Can self-escalate to admin" KPI.
  (Self-escalation is judged per path on MANAGE GRANTS, not AND-ed with admin-reach, so a
  non-admin who can grant themselves admin is still caught.) The user list now ranks by escalation
  risk, and the path graph marks admin-reaching roles and self-escalation users in red. Read-only,
  toggle-gated, off first paint. App version 4.221.0.

## 4.220.0 - Incident lifecycle Gantt on Control Room (2026-08-16)

CoCo review, Tier-3 (Control Room #5). New `data.mart_sql.incident_gantt` + `ui.charts.incident_gantt`.

- **Recent incidents now read as a Gantt, not just an open-queue table.** The Control Room
  Incidents section gains a 14-day lifecycle chart — one severity-colored bar per incident from
  detected to resolved (an open incident's bar reaches now), so the shape of the week (how long
  things ran, what overlapped, what's still open) is visible at a glance. Includes resolved
  incidents (the open-queue table shows only open/mitigated). App version 4.220.0. Note: ack/
  mitigate timestamps aren't consistently written, so the bar is the detected→resolved span.

## 4.219.0 - Dormant-then-active detection on Security (2026-08-16)

CoCo review, Tier-2 (Security #5). New `data.security_sql.dormant_reawakening` +
`logic.insights.reawakening_severity`.

- **A long-dormant account that suddenly logs in now surfaces on Security.** `LAST_SUCCESS_LOGIN`
  structurally can't show this — `ACCOUNT_USAGE.USERS` keeps only the most-recent login, so a
  woken account reads as freshly active. A new toggle-gated Access panel reads successful
  `LOGIN_HISTORY` and flags a user whose recent login followed a long silence — a real gap between
  consecutive logins (LAG), or a first-in-window login for an account created long before (the
  deep-dormant case, measured from creation since login history retains ~365 days). Ranked by gap
  length + roles held; read-only (service accounts log in rarely by design, so no auto-alert).
  Off first paint. App version 4.219.0.

## 4.218.0 - Warehouse utilization & quiet-hours on Operations (2026-08-16)

CoCo review, Tier-2 (Operations #12/#13). Zero new SQL/logic — reuses the Cost-side readers.

- **The utilization and quiet-hours analytics that lived only on Cost now read on Operations too.**
  The Warehouses section gains two toggle-gated diagnostic blocks: (1) utilization & right-sizing —
  per-warehouse idle %, queue/spill, p95 and a size verdict (reusing the sizing profile + logic),
  and (2) quiet-hours — the hour-of-day credits-vs-activity heatmap plus a per-warehouse "defensible
  4h+ quiet window" verdict (reusing `propose_quiet_window`). Both are read-only and point to Cost →
  Optimize for execution; both stay off first paint. App version 4.218.0.

## 4.217.0 - Experiment state on the savings cards (2026-08-16)

CoCo review, Tier-2 (Cost #10). New pure `logic.workbench.experiment_state_by_key`.

- **A saving already under test no longer reads as a fresh opportunity.** The Cost →
  Optimization addressable-savings table now shows an **Experiment** column — the most-active
  optimization experiment (PLANNED/RUNNING/OBSERVING/VERIFIED) on that warehouse, matched
  case-insensitively by entity key, reusing the `experiments` reader. The column appears only
  when a listed warehouse is actually under test, and degrades away when V074 isn't applied.
  App version 4.217.0.

## 4.216.0 - Vs-prior-period deltas on KPIs (2026-08-16)

CoCo review, Tier-2 (Overview #19 / Control Room #3). Reuses `logic.formulas.pct_delta`.

- **The headline KPIs now say which way they moved.** The Overview spend tile gains a
  vs-prior-window delta (mart-only, from `fact_warehouse_window_vs_prior`; spend up = red), and
  the Control Room "Queries" pulse tile gains a day-over-day delta computed from the activity
  frame it already loads — no new query on either. Both hide the delta when the prior period is
  zero (no fabricated 0%) and use complete-day bases so a partial day never fakes a swing.
  App version 4.216.0.

## 4.215.0 - As-of watermark on the Overview dollar KPIs (2026-08-16)

CoCo review, Tier-2 (Overview #15). `ui.components.metric_card_html` gains an optional `as_of`.

- **The Overview dollar figures now say what day they're through.** Metering lags up to 24h, so a
  $ KPI without a date invites "is this today's number?". The window-spend card and the MTD /
  projected cards now carry a muted "as of <date>" watermark — the last complete/metering day
  behind the figure, read from frames already in memory (no query). App version 4.215.0.

## 4.214.0 - "Since your last visit" on the Cost page (2026-08-16)

CoCo review, Tier-2 (Cost #3). New `data.mart_sql.since_last_visit` +
`logic.actions.since_last_visit_summary`.

- **The Cost page now greets you with what changed while you were away.** A one-line opener under
  the verdict reads the viewer's own `APP_USAGE` trail to find their last visit (their most recent
  activity before a 30-minute gap, matched to the same identity the app stamps), then counts the
  alerts (by severity) and actions raised since — "Since your last visit (2 hours ago): 3 new
  alerts (1 critical); 2 new actions", or a calm "nothing new" when it was quiet. All timestamps
  compare as account-time NTZ so the buffered/stored stamps line up; reads OVERWATCH's own tables
  (no Account Usage scan, no migration — `APP_USAGE` has existed since V016). App version 4.214.0.

## 4.213.0 - Anomaly markers on the spend trend (2026-08-16)

CoCo review, Tier-2 (UI #15 / Overview #5). New `logic.anomaly.anomaly_markers` +
`ui.charts.spend_trend` markers overlay.

- **Anomalous spend days are now marked right on the trend.** The spend chart showed the shape
  but not which days were flagged, so a spike and "was that an anomaly?" lived on different
  surfaces. `spend_trend` gains an optional `markers` overlay (dashed vertical rules, hover for
  what). Operations → Warehouses marks the anomalous warehouse-days it already scored (no new
  work); Overview flags its own daily spend with the same robust-z detector (pure, scale-
  invariant, no scan) and marks those. A new pure `anomaly_markers` helper collapses flagged
  rows to one labelled marker per day. App version 4.213.0.

## 4.212.0 - Click a day on the spend trend to break it down (2026-08-16)

CoCo review, Tier-2 (UI #23). `ui.charts.spend_trend` gains an optional click drill.

- **The spend trend is now a drill-in, not just a picture.** Pass a `key` and its bars become
  clickable; a click returns that day's date so the caller can break it down. On the Operations →
  Warehouses tab, clicking a day now shows that day's spend by warehouse — and each warehouse row
  drills to its Entity 360 (reusing the UI22 helper), from the frame already loaded, so no extra
  scan. The click-read + sticky-re-emit guard is extracted into one shared `_read_click_selection`
  used by both this and the existing clickable bar chart. Degrades to a plain chart where the
  runtime lacks `on_select`. App version 4.212.0.

## 4.211.0 - Universal entity drill into Entity 360 (2026-08-16)

CoCo review, Tier-2 (UI #22). New `ui.components.entity_nav_table`.

- **Any table of entities can now open the picked one in Entity 360 with one call.** The
  drill-into-Entity-360 pattern existed (Decision Studio, Action Center, Operations tasks) but
  each site hand-wrote its own selectable-table + navigation handler, and many entity tables had
  no drill at all. A shared `entity_nav_table(df, key, key_col=…, entity_type=…)` wraps the
  selectable-table + navigation so a homogeneous table (every row a warehouse, user, task, …) or
  a mixed one (per-row `type_col`) becomes a deep-link in one line. First adopters: the
  Operations → Warehouses concurrency-peaks and queue/spill tables now open a warehouse's Entity
  360 on row select. (Per-cell hyperlinks stay infeasible in Streamlit-in-Snowflake's
  canvas-rendered dataframes; row-select is the supported affordance.) App version 4.211.0.

## 4.210.0 - Credit overlay on the incident-correlation timeline (2026-08-16)

CoCo review, Tier-2 (Control Room #9). New `data.cost_sql.hourly_credits`.

- **The replay timeline now shows spend alongside the events.** The Control Room incident-
  correlation timeline plotted alerts, task failures, and DDL on one axis but not the money —
  so "did that change cost anything?" meant leaving for the Cost page. An hourly-spend bar panel
  now layers above the events on one shared time axis (48h or 7d), so a cost spike and the events
  around it line up on first paint. The credit timestamps are derived exactly like the events'
  (`::TIMESTAMP_NTZ`) and localized the same way, so the two axes cannot drift; idle hours stay
  gaps (bars, not an interpolated area) rather than reading as sustained spend. The scan reuses
  `WAREHOUSE_METERING_HISTORY` (already reachable from this page) on the lazy Timeline & movers
  section — no first-paint cost. Control Room live-scan budget 3→4. App version 4.210.0.

## 4.209.0 - Recent-change panel on Entity 360 (2026-08-16)

CoCo review, Tier-2 (Control Room #15). New `data.workbench_sql.entity_recent_changes`.

- **Entity 360 now shows what recently changed on the thing you're looking at.** The panel's
  own summary promised "ownership, work, changes, savings, evidence" but had no changes
  section. It now surfaces recent tracked changes for the entity — warehouse setting deltas
  (`WAREHOUSE_CHANGE_REGISTRY`) for a warehouse, proc/task deploys (`OBJECT_CHANGE_REGISTRY`)
  for a database, task, or object — the same registries the Operations → Change impact tab
  reads, so a stat regression and the change that caused it read together. Entity types with
  no change feed (users, roles, query fingerprints, data products) show a precise note rather
  than an empty table. App version 4.209.0.

## 4.208.0 - Auto-detect deploys for Release compare (2026-08-16)

CoCo review, Tier-2 (Ops #16). New `data.insights_sql.detect_release_days` +
`logic.insights.rank_release_candidates`.

- **Release compare no longer starts with a blank date guess.** The Operations → Release
  compare tab required the owner to remember the deploy date. It now auto-detects candidate
  deploy days from schema-change DDL (CREATE/ALTER/DROP) in `ACCOUNT_USAGE.QUERY_HISTORY` —
  excluding CTAS/session ops and OVERWATCH's own maintenance — and offers them in a picker,
  labelled with the change count and top actor, newest notable deploy pre-selected. Manual
  date entry stays as the escape hatch and the fallback when nothing deploy-like is found.
  A heuristic, not a definitive release log, and disclosed as such. App version 4.208.0.

## 4.207.0 - RECOMMEND column on the auditor export pack (2026-08-16)

CoCo review, Tier-2 (Sec #17). New `logic.least_privilege.recommend_for_sheet`.

- **The access-review pack now says what to do, not just what exists.** The ten CSVs were raw
  evidence; the actionable sheets (unused roles, dormant users, MFA gaps, expiring credentials,
  break-glass holders) now lead with a RECOMMEND column — revoke / review / enable MFA / rotate
  — so an access review completes in hours instead of re-deciding every row. Evidence-only
  sheets stay raw. App version 4.207.0.

## 4.206.0 - Effort tier on the addressable-savings rollup (2026-08-16)

CoCo review, Tier-2 (Cost #8). New `logic.savings_rollup.effort_tier`.

- **Savings opportunities now show effort, so quick wins stand out.** The rollup ranked by
  confidence × dollars, but the highest-dollar lever isn't always the best next action. Each
  row now carries an **Effort** tier derived from its source — LOW is a single ALTER (idle
  timer / size), HIGH is a costly re-cluster — so a DBA can sort the sortable table by Effort
  and bank the easy savings first. App version 4.206.0.

## 4.205.0 - Savings realization rate on the Optimization ledger (2026-08-16)

CoCo review, Tier-2 (Cost #9). `logic.actions.ledger_totals` gains a realization rate.

- **The savings ledger now shows its track record, not just two totals.** It reported Verified
  and Estimated dollars separately but never “of what we verified, how much of the estimate
  actually measured out.” A new “Realization rate” tile — verified $ as a share of those items'
  original estimates (e.g. “$8.4k of $12k = 70%”) — calibrates trust in a fresh estimate.
  App version 4.205.0.

## 4.204.0 - Show how long the oldest undelivered critical has waited (2026-08-16)

CoCo review, do-first wave (Control Room #4, duration not count). `health_strip`
gains `UNDELIVERED_OLDEST_MIN`.

- **The “reached nobody” banners now show the age, not just the count.** A count says a
  critical failed to route; the age says whether it's been minutes or hours. The shared
  health strip now carries the oldest undelivered-critical's age, so both the Control Room
  and Brief banners read “… (30+ min, no delivery, oldest 4h 12m)” — with zero extra queries
  (the same strip both pages already load). App version 4.204.0.

## 4.203.0 - Alert drawer shows how the rule was resolved last time (2026-08-16)

CoCo review, do-first wave (Alerts #26). New `mart_sql.resolutions_for_rule`; wired
into the alert drawer.

- **Opening an alert now shows how the same rule was closed before.** The drawer listed raw
  recent events but not the resolution. It adds a “How this was resolved before” panel — the
  last few RESOLVED events for the rule with their kind (ACTIONED / NOISE / EXPECTED) and
  note, from `ALERT_EVENTS` — a playbook this alert has earned from your own history.
  Rendered as a table so a note can't inject formatting. App version 4.203.0.

## 4.202.0 - Persistent contract-runway bar on Overview and Brief (2026-08-16)

CoCo review, do-first wave (Overview #20 / Cost #4). New pure
`logic/formulas.contract_runway` + shared `components.contract_runway_bar`.

- **The most important committed-spend number now rides above the fold.** Overview had no
  contract element at all; both Overview and the Brief now show a thin, colour-banded bar —
  “% of contract consumed · N days left (exhausts DATE · decide by DATE)” — from the
  `contract_exhaustion` mart. The “decide-by” date backs a 30-day procurement lead time off
  the exhaustion date, so renewal talks start before the runway ends. The Brief reuses its
  existing read (no new query); Overview adds one cheap cached read. App version 4.202.0.

## 4.201.0 - Chart takeaways on the boss chart and metric lines (2026-08-16)

CoCo review, do-first wave (UI #14). Extends the existing `_share_note`
“lead-with-the-conclusion” idiom to two bare charts.

- **The boss chart and daily metric lines now state their conclusion.** `monthly_stacked_usd`
  (monthly spend by warehouse) captions its top spender (“Top: WH_X $Y (Z% of total)”), and
  `daily_metric_line` names its peak day — matching the takeaway the other charts already
  carry. A chart with a conclusion is information, not homework. App version 4.201.0.

## 4.200.0 - Oldest-unacked-critical age on the Brief (2026-08-16)

CoCo review, do-first wave (duration, not count). New pure
`logic/verdict.oldest_open_hours`; wired into the Brief.

- **The Brief now shows how long the oldest critical has been open, not just how many.**
  A count measures volume; an age measures responsiveness. From the alert events the Brief
  already loads (no new query), it surfaces an “Oldest unacked critical” KPI (red past 24h)
  and folds the age into the verdict line (“2 open critical alert(s), oldest 47h”). The
  helper is reusable for the Control Room and Operations duration signals next. App version
  4.200.0.

## 4.199.0 - Generate REVOKE SQL from the least-privilege shortlist (2026-08-16)

CoCo review, do-first wave (Security #11). New pure `logic/least_privilege.revoke_statements`;
wired into the Security “Least privilege” tab.

- **The unused-grant shortlist can now emit copy-paste REVOKEs.** The tab already found
  table privileges no query exercised, but “reported; revoked nothing.” It now offers a
  reviewed `REVOKE <priv> ON TABLE <obj> FROM ROLE <role>;` script built from the shortlist
  rows — the DBA's bottleneck was writing the statements, not knowing they were needed. The
  app still executes nothing; malformed rows (unknown privilege, blank field) are skipped.
  App version 4.199.0.

## 4.198.0 - Verdict line on Overview, Cost, and Control Room (2026-08-16)

CoCo review, do-first wave (finding #1, continued). The page-verdict primitive
(v4.197.0) now leads four surfaces.

- **Overview, Cost & Contract, and Control Room now open with a computed verdict.** Each
  composes a worst-first Healthy / Watch / Attention-needed line from signals it already
  had on hand — Overview from the platform-score band, open alerts, and budget pace;
  Control Room from the health strip (open criticals, undelivered criticals, stale sources)
  at zero extra queries; Cost from contract runway (one cheap mart read). Reuses the shared
  `page_verdict` / `page_verdict_line` primitive. App version 4.198.0.

## 4.197.0 - Computed “should I worry?” verdict line on the Brief (2026-08-16)

CoCo review, do-first wave (finding #1). New pure `logic/verdict.py` + shared
`ui.components.page_verdict_line`; wired first into the Brief.

- **The morning read now opens with a one-line verdict.** The Brief led with a static
  description; it now computes a worst-first **Healthy / Watch / Attention needed** line
  above the numbers — from open criticals, open incidents, and contract runway, all
  signals already on the page (no new query). The shared primitive (`page_verdict` +
  `page_verdict_line`) is built to extend to Overview, Cost, and Control Room next.
  App version 4.197.0.

## 4.196.0 - Decision Studio: label the time basis of action estimates (V083) (2026-08-16)

Decision Studio review, Wave 2 (finding #7). Migration V083 + app rewire.

- **Action estimates now carry a time basis.** `ACTION_QUEUE.ESTIMATED_USD` mixed
  clocks — the AI-chargeback queue insert stored a 30-day (monthly) projection while the
  Action Center create form stored an operator-typed figure with no stated basis — and the
  Workbench "Estimated opportunity" KPI and the Decision Studio scenario projection summed
  them as if identical. V083 adds a nullable `PERIOD` column (MONTHLY / ANNUAL / ONE_TIME;
  NULL = unspecified). The app stamps it at both writers (AI chargeback = MONTHLY; the
  Action Center create form takes an operator-chosen basis) and surfaces it in every reader —
  the Action Center table + KPI help, the Decision Studio actions table + projection caption,
  the Overview top-actions table, and the Brief — so estimates on different bases are no
  longer read as one number.
- **Scope note.** Normalizing the scenario projection's math across bases (how to fold a
  one-time saving into a monthly run-rate) is left as a follow-up modeling decision; the
  projection now discloses that it sums estimates at face value.
- Owner applies V083 in Snowsight after V082 **before deploying the app** — the
  action-queue readers select `PERIOD` by name. App version 4.196.0.

## 4.195.0 - Decision Studio: real data-product detail view (2026-08-16)

Decision Studio review, Wave 2 (finding #14). App-only; new
`data/workbench_sql.product_detail`, Entity 360 branch in `ui/workbench.py`.

- **Opening a data product now shows what it's made of.** DATA_PRODUCT is an
  ENTITY_CATALOG *attribute*, not an entity type, so clicking a product from the Products
  board fell into Entity 360's catalog-record path and rendered an empty "no ownership
  record / no metric snapshot" page. Entity 360 now detects DATA_PRODUCT and renders a
  real detail view: a rollup (entity count, most-severe criticality, owner — flagged when
  the product spans several) plus the product's constituent catalog entities
  (objects/warehouses/tasks) with their ownership and criticality, most-severe first.

App version 4.195.0. Gates green: ruff --no-cache, mypy, pytest.

## 4.194.0 - Decision Studio: honest scope + truncation disclosure (2026-08-16)

Decision Studio review, Wave 2 (findings #13, #15). App-only; `ui/pages/decision_studio.py`
and `ui/decision_studio.py`.

- **Per-section filter contracts.** The page carried one blanket "Company + Window"
  contract, but SLOs and Experiments ignore both (objectives keep their own windows;
  experiments are account-wide) and Scenarios scopes by Company only. Each section now
  declares exactly which page filters it honors, so the scope note stops overclaiming. (#13)
- **Top-N truncation is disclosed.** The Portfolio board caps at the top 200 query
  families by measured credits, but the app's row-truncation banner only fires at the
  5000 fetch cap — so the smaller cap truncated silently. The board now says "showing the
  top 200… more exist" when the cap is hit, so "N families" isn't misread as the whole
  population. (#15)

App version 4.194.0. Gates green: ruff --no-cache, mypy, pytest.

## 4.193.0 - Decision Studio: product criticality rank + owner conflict (2026-08-16)

Decision Studio review, Wave 2 (finding #12). App-only; `data/workbench_sql.py`
`data_product_economics`, Products board in `ui/decision_studio.py`.

- **A critical data product no longer reads as standard.** The product rollup took
  `MAX(CRITICALITY)` across a product's catalog entities — but that's *lexical*, and
  alphabetically `STANDARD > CRITICAL`, so a product containing a CRITICAL entity showed
  as STANDARD. It now ranks by **severity** (CRITICAL > HIGH > STANDARD > LOW) and reports
  the most-severe.
- **Ambiguous ownership is flagged, not hidden.** When a product's entities carry
  different owners, `MAX(OWNER_NAME)` silently picked one. A new `OWNER_CONFLICT` flag +
  KPIs surfaces those products so ownership gets resolved in the catalog (the displayed
  `OWNER_NAME` is one of several).

App version 4.193.0. Gates green: ruff --no-cache, mypy, pytest.

## 4.192.0 - Decision Studio: SLO cockpit trust fixes (2026-08-16)

Decision Studio review, Wave 2 (findings #9, #10, #11). App-only; `data/workbench_sql.py`
`slo_cockpit`, `logic/decision.py` `slo_summary`, and the SLOs board in
`ui/decision_studio.py`.

- **A stalled loader no longer reads as MET.** An objective was evaluated off whatever
  the mart last held, so a metric board that stopped loading could still show MET/BREACH
  from days-old data. The status now returns **STALE** when the newest mart day (`AS_OF`)
  is more than 2 days old — the verdict is withheld rather than asserted off stale
  evidence — with its own KPI and exception. (#11)
- **Latency objectives show n/a for burn, not 0.00x.** Error-budget burn only applies to
  success-rate objectives; latency/P95 objectives carry no burn. The "Worst burn" KPI now
  reads **n/a** when no objective has an applicable burn, instead of a misleading 0.00x
  (via a new `has_burn` signal in `slo_summary`). (#10)
- **P95 objectives are disclosed as worst-daily-P95.** The P95 metrics are `MAX` of the
  daily P95 over the window — a day-granular check (holds only if *every* day stayed under
  target), not a single window percentile. The board now says so, so the value isn't
  misread. (#9)

Gates green: ruff --no-cache, mypy, pytest.

## 4.191.0 - Decision Studio: query-family company regrain (V082) (2026-08-16)

Decision Studio review, finding #3 (company-scope leaks). Owner migration **V082**
(apply in Snowsight after V081) + app rewire of `data/workbench_sql.py` and
`data/mart27_sql.py`.

- **Query families are now scoped by the real company, not a database guess.**
  `MART_QUERY_FAMILY_DAILY` was the only loader arm with no company attribution: its
  grain was `(DAY, QUERY_HASH)` with an `ANY_VALUE(DATABASE_NAME)` representative, so a
  family that ran under both companies collapsed to one account-wide row. The app then
  faked company scope off that representative database — `workload_portfolio` joined the
  family mart on `DATABASE_NAME`, and `family_compile_heavy` / `family_repeat_fingerprints`
  filtered with `database_clause` — a lossy heuristic that dropped or mixed a company's
  families. V082 re-derives `SP_LOAD_MARTS_V27` (from V078) with the query-family arm
  regrained to per-company rows: `COMPANY = COMPANY_FOR_WAREHOUSE(WAREHOUSE_NAME)`,
  `GROUP BY (DAY, QUERY_HASH, COMPANY)`, top-2000/day **per company**, `MERGE` keyed on
  `+COMPANY`. All other loader arms are byte-identical to V078. The three app readers now
  scope on the exact `COMPANY` column.
- **One-time grain reset + backfill.** The mart gains a `COMPANY` column and is cleared
  once (the grain changed, so old account-grain rows would double-count under any GROUP
  BY reader). It's fully rebuildable from `OW_QH_EXTRACT`. **Owner: after applying, re-run
  the backfill** — `CALL SP_LOAD_QH_EXTRACT(90)` then `CALL SP_LOAD_MARTS_V27('HOURLY', 90)`
  (widen to 365 for full history). Until then the family boards read empty or fall back to
  their live `QUERY_HISTORY` twins. No alert scan reads this mart, so there's no
  alert-layer ripple; `SP_NIGHTLY_RECONCILE` deletes by DAY only and is untouched.
- Authored via the derivation-law generator (`outputs/gen_v082.py`, byte-identical regen
  test — the ~790-line proc is re-emitted with a single-point arm swap) and reviewed by two
  adversarial skeptics. The first caught a P2 (the qfam arm originally grouped *by* the
  `COMPANY_FOR_WAREHOUSE` UDF — a correlated subquery in a GROUP BY key, the exact shape
  that logged `mart_load_failed` hourly after V027 until V029); it now derives COMPANY in an
  inner projection and groups the outer on the plain column, and the one-time DELETE is
  guarded so a re-apply can't wipe a backfilled mart. Disclosed limits: ALL-view
  `MAX(USERS)/MAX(WAREHOUSES)` in the family boards become per-company peaks (understate a
  cross-company family's distinct counts — a pre-existing peak-day proxy), and
  `workload_portfolio` has no live fallback, so its family metrics read as no-evidence
  between the DELETE and the backfill (routed to the VALIDATE lane) and the board errors if
  the app is deployed before V082 is applied. **The app depends on V082's `COMPANY` column
  — apply V082 before deploying.**

## 4.190.0 - Decision Studio: unified experiment verify (V081) (2026-08-16)

Decision Studio review, finding #5. Owner migration **V081** (apply in Snowsight
after V080) + app rewire of `logic/workbench.update_experiment_sql`.

- **Settling an experiment now reconciles the ledger and its action — atomically, both
  ways.** Settling an optimization experiment used to write
  `OPTIMIZATION_EXPERIMENTS.VERIFIED_USD` only: it never touched `SAVINGS_LEDGER` and
  never closed the `ACTION_QUEUE` row the experiment came from, so the two "verified
  savings" totals drifted apart and a verified experiment left its action OPEN forever.
  New procedure **`SP_VERIFY_EXPERIMENT`** settles a terminal outcome in one transaction:
  **VERIFIED** records the verdict, MERGEs the savings ledger keyed on `ACTION_ID`
  (discriminated by `FINDING_TYPE='EXPERIMENT'` so it upserts exactly the experiment's
  own row, never an auto-booked / remediation row), and closes the source action;
  **REJECTED / ROLLED_BACK** clear the verdict and — if the experiment had actually
  booked a verified row — reverse that ledger row and reopen the action, so a
  rollback-after-verify can't leave the ledgers diverged or the action stranded. Every
  path writes an audited `ACTION_ACTIVITY` entry. Modelled on `SP_ACTION_LIFECYCLE`:
  `EXECUTE AS OWNER`, rollback on any error, `REQUEST_KEY` idempotency.
- **Scoped and non-regressing.** The ledger + action legs run only when the experiment
  carries an `ACTION_ID` (a standalone experiment still records its verdict); only the
  terminal outcomes (`VERIFIED`/`REJECTED`/`ROLLED_BACK`) route through the procedure —
  in-flight transitions (`PLANNED`/`RUNNING`/`OBSERVING`) stay a plain status update.
  Known limits (disclosed): the ledger row keys on `ACTION_ID`, so multiple experiments
  on one action share a single ledger row (experiments are ~1:1 with actions) — a
  consequence being that rejecting one of several experiments on the same action can
  reverse another's still-valid booking; and the "verified savings" headline sums all
  verified ledger rows without cross-source dedup (pre-existing).
- Authored via the derivation-law generator (`outputs/gen_v081.py`, byte-identical
  regen test) and reviewed by two adversarial skeptics — the second caught the
  rollback-after-verify divergence, which the compensating REJECTED/ROLLED_BACK leg now
  closes. **The app's verify button requires V081 to be applied first** (it CALLs the
  new procedure); apply V081 before deploying.

Gates green: ruff --no-cache, mypy, pytest.

## 4.189.0 - Operations: incident routing for task failures (2026-08-16)

Cost/metric gap audit, Wave 6 (finding #27). App-only; new pure `logic/incident.py`,
Operations "Failure root-cause timeline" panel.

- **Task failures now arrive with an owner and a first move.** The pieces existed but
  sat disconnected — `classify_task_error` names the error family and
  `build_failure_timeline` marks root-cause vs cascade, yet nothing attached a
  remediation or an owner. A new **Incident routing** panel (under the failure timeline)
  stitches each root-cause failure to a first-response remediation keyed to its error
  family (permission, timeout, missing object, data quality, …) and to an owner/on-call
  resolved from ENTITY_CATALOG (the task's own TASK entry, else the database it lives in),
  so an incident carries a name and a next step. Cascades are excluded so only the failure
  to fix pages someone; repeated failures collapse to one incident with a count; severity
  comes from the owning entity's catalog criticality (CRITICAL/HIGH/STANDARD/LOW, a notch
  lower for a cascade).
- **Honest routing.** `ROUTED_TO` reads `unassigned` when the task isn't in the catalog
  (with a prompt to register it), and the panel reuses the already-loaded failure
  timeline — no new failure scan, just one catalog read.
- Deferred to the owner-migration half: actually opening a routed ACTION_QUEUE item /
  Teams mention, and ack-timeout escalation from an ON_CALL_SCHEDULE rotation table.

Gates green: ruff --no-cache, mypy, pytest.

## 4.188.0 - Operations: robust-z row-volume data-quality monitor (2026-08-15)

Cost/metric gap audit, Wave 6 (finding #26). App-only; new pure `logic/dq.py`,
`data/dq_sql.product_row_volume`, Operations "Pipeline SLA" panel.

- **Data-quality monitoring beyond the 50% cliff.** The only volume signal was the
  PIPE_VOLUME_DROP alert (yesterday <50% of a 7-day average). A new **Row-volume
  anomalies** panel scores each table's most recent load of rows-added against its own
  prior loads with a **robust z-score** (median / MAD, floored at 15% of the median so a
  majority-modal baseline can't blow within-range jitter up to max severity; threshold
  3.5), so it catches both partial/duplicate loads and upstream volume shifts, resists a
  single prior outlier, and needs no fixed percentage cliff. Scoped to catalog-registered
  data products (via ENTITY_CATALOG, resolving an OBJECT entity then its DATABASE) so
  scans stay bounded and every finding carries its OWNER_NAME for routing.
- **Business-day safe scope, with disclosed blind spots.** Only days that actually added
  rows count as loads, so a table is scored against its own load history and is never
  falsely flagged just because the newest data lands on a weekend. A table is scored only
  with ≥10 prior loads and a ≥100 rows/day baseline median; a DAYS_STALE column and note
  mark rows scored on an old load. Whether a load was *expected but didn't run*, a load
  that ran and inserted 0 rows, and seasonal-but-nonzero tables are left to (or delegated
  from) the freshness-SLA and Volume-drops panels beside it — all disclosed in the panel.
  KPIs count monitored tables, low- and high-volume loads, and affected products.
- Deferred to the owner-migration half: the DQ_BREACH alert routed to the entity owner,
  plus the null-rate-spike and schema-drift monitors (both need a stored baseline).

Gates green: ruff --no-cache, mypy, pytest.

## 4.187.0 - Security: least-privilege grant review (2026-08-15)

Cost/metric gap audit, Wave 6 (finding #24). App-only; new pure
`logic/least_privilege.py`, `data/security_sql.grant_scope_usage` +
`unused_table_grants`, Security "Least privilege" tab.

- **Held table grants are now measured against what queries actually used.** The
  Security page counted grants but never asked whether they were exercised. A new
  **Least privilege** tab joins each role's *observable* table data-privileges
  (SELECT / INSERT / UPDATE / DELETE — the ones ACCESS_HISTORY records) to the objects
  queries actually read or modified (ACCESS_HISTORY), rolled up per role×schema and
  labelled **UNUSED** (touched none of its granted tables — revoke the scope),
  **OVER-BROAD** (used a third or fewer — narrow it), or **FOCUSED**. A toggle reveals
  the per-table revoke shortlist.
- **Built around three correctness traps.** Access is matched through the numeric object
  id (TABLE_STORAGE_METRICS.ID), not the `DB.SCHEMA.TABLE` string, so mixed-case /
  quoted identifiers can't fake a "never used" verdict (the D4 lesson); usage is
  attributed at the OBJECT level (touched by *any* query), so a grant exercised only
  through role inheritance still counts as used; and usage is collapsed to the table
  NAME (touched if *any* of its object ids was touched), so a drop→recreate that leaves
  two live ids can't surface an in-use table as a revoke candidate. The analysis can
  under-report an unused grant but never falsely flags a used one.
- **Coverage safeguard (density + recency).** Verdicts are gated on how many of the
  last 90 days actually carry ACCESS_HISTORY activity (distinct-day density, so a stray
  old row can't fake depth) *and* on how fresh the newest row is (recency, so history
  that stopped populating after an edition change can't make recent grants read as
  unused). No history, thin history (<28 active days), or stale history (>7 days quiet)
  each suppresses verdicts with a warning; the covered window is disclosed on the panel
  and in the shortlist captions. Privileges ACCESS_HISTORY can't observe (REFERENCES via
  FK DDL, TRUNCATE) are excluded entirely, and the method panel discloses that FUTURE /
  schema / database grants and hybrid-table reads are out of scope.
- Deferred to the owner-migration half: emitting revoke items to ACTION_QUEUE and the
  SEC_UNUSED_GRANT alert.

Gates green: ruff --no-cache, mypy, pytest.

## 4.186.0 - Security: outbound data-exposure inventory (2026-08-15)

Cost/metric gap audit, Wave 6 (finding #25). App-only; new pure
`logic/exposure.py`, `data/security_sql.show_shares_sql`, Security "Exposure" tab.

- **Who can see our data is no longer invisible.** The Security page had no view of
  outbound shares or listings — "which objects are exposed to whom" and "which shares
  reach many accounts" were unanswerable on a shared account. A new **Exposure** tab
  reads `SHOW SHARES` (one metadata query, mirroring the existing SHOW WAREHOUSES /
  DATABASES inventory) and classifies each share by reach: **LISTING** (published to
  the marketplace/org, broad by construction), **BROAD** (3+ direct consumer
  accounts), **DIRECT** (>=1 consumer), **NO CONSUMERS** (outbound but granted to
  nobody yet), and **INBOUND** (data we consume, shown only so the surface count is
  honest). KPIs count outbound shares, distinct consumer accounts, listings, and broad
  shares; the table is sorted most-exposing first.
- **Honest degradation.** A missing `to` column reads as unknown reach (DIRECT), never
  a false "no consumers"; column names are matched case-insensitively; an account with
  no shares reads as "nothing exposed outbound".
- Deferred to follow-up slices: the per-object `SHOW GRANTS TO SHARE` drill, the
  ORGANIZATION_USAGE.LISTING_* last-access staleness lens, and the SEC alert on
  *new/broadened* exposure (owner migration).

Gates green: ruff --no-cache, mypy, pytest.

## 4.185.0 - Decision Studio: Watch pins actions in the Action Center (2026-08-15)

Decision Studio review, Wave 1 (finding #1, slice 2). App-only;
`logic/workbench.py`, `decision_studio.py`.

- **Watched entities now surface in the Scenarios action queue too.** New pure
  `mark_watched_pairs` matches each action's own `(SOURCE_ENTITY_TYPE,
  SOURCE_ENTITY_KEY)` against the watchlist, so an action on a watched warehouse /
  query family / product gets a `WATCHED` flag and is pinned to the top *within its
  severity band* (a watched LOW never jumps a CRITICAL), with a caption counting the
  pinned actions. Same graceful degradation as slice 1. Watch-rules (notify on state
  transitions) remain the last Watch slice, queued.

Gates green: ruff --no-cache, mypy, pytest.

## 4.184.0 - Decision Studio: Watch surfaces on the Portfolio (2026-08-15)

Decision Studio review, Wave 1 (finding #1, first slice). App-only;
`logic/workbench.py`, `decision_studio.py`.

- **Watch is no longer an inert bookmark.** Adding a query family to your watchlist
  only ever populated a buried Entity-360 sub-tab — nothing else read it. Now the
  Portfolio (the primary decision table) reads your watchlist, flags watched
  families with a `WATCHED` column, counts them in a new "Watching" KPI, and pins
  them to the top *within their lane* — so a watched family surfaces first without
  burying an unwatched ACT NOW item beneath a watched PLAN one. New pure
  `mark_watched` helper does the case-insensitive, type-scoped match (empty/absent
  inputs degrade to all-false, never a raise), and the watchlist read degrades
  gracefully when USER_WATCHLIST is unavailable. USER_WATCHLIST is an app object,
  so no ACCOUNT_USAGE scan-surface change.
- **Next for Watch:** the same pin on the Action Center, and watch-rules that notify
  on state transitions — both queued.

Gates green: ruff --no-cache, mypy, pytest.

## 4.183.0 - Decision Studio: Product Economics honors Company (2026-08-15)

Decision Studio review, Wave 1 (finding #4-part / #3 app-only half). App-only;
`workbench_sql.py`.

- **Products, entity counts, and task health are now company-scoped.** Only the
  object/warehouse dollar CTEs were company-filtered; the driving `catalog` CTE and
  `task_health` were account-wide, so a selected company still showed other
  companies' products, inflated "Data products / Catalog entities" KPI counts, and
  cross-company task-run/failure numbers. The `catalog` CTE is now filtered on
  `ENTITY_CATALOG.COMPANY` — because it drives the product list, the object/database
  maps, warehouse cost, and task health, scoping it there is the only way to
  company-scope tasks (`MART_TASK_NODE_DAILY` has no COMPANY column). The fact-level
  filters remain as belt-and-suspenders. (The lexical-`MAX(CRITICALITY)` and the
  account-grain family mart are separate findings.)

Gates green: ruff --no-cache, mypy, pytest.

## 4.182.0 - Decision Studio: Cost Truth stops fabricating zeroes (2026-08-15)

Decision Studio review, Wave 1 (finding #4). App-only; `decision_studio.py`.

- **An absent cost basis now reads "No evidence", not $0.00.** `cost_truth` is four
  un-grouped scalar aggregates, so it always returns four rows even over an empty
  window — an empty basis arrives as NULL credits, and `safe_float` turned that into
  a measured-looking `$0.00`. Most misleading on a per-company view, where the three
  company-scoped bases (metered / measured / allocated) can be legitimately empty
  while account-wide Billed shows real dollars — reading as verified-zero cost. The
  render now tracks presence from the raw column and shows "No evidence" per absent
  basis, and the metered-ratio caption is skipped unless the bases are actually
  present. The AI-aware billed dollarization and the non-additive labels are
  unchanged.

Gates green: ruff --no-cache, mypy, pytest.

## 4.181.0 - Decision Studio: Portfolio stops acting on missing evidence (2026-08-15)

Decision Studio review, Wave 1 (finding #2). App-only; `decision.py`, `decision_studio.py`.

- **A blank measurement is no longer treated as a measured zero.** In
  `prioritize_workloads`, a NULL cache % / P95 (a query family that missed the
  family mart via a join miss or its 2000/day cap) was coerced to `0.0`, so a
  high-cost family with zero behavioral evidence could reach `CONFIDENCE=1.0`,
  `ACT NOW`, and "Cache or materialize" — with the cache cell rendered blank. Now
  presence is tracked from the raw columns: the caching and latency recommendations
  are gated on the measurement actually existing, a family with no behavioral
  evidence at all is held at `VALIDATE` (never ACT NOW) with a "Validate evidence"
  next move, and a new `EVIDENCE_COVERAGE` column (0–1) surfaces on the Portfolio
  table so a blank cell reads as missing data, not a measured zero. The lane-rule
  caption states the gate. (The deeper cause — the account-grain family mart that
  produces the join misses — is finding #3, a migration, and stays queued.)

Gates green: ruff --no-cache, mypy, pytest.

## 4.180.0 - Feature: fleet consolidation recommender (2026-08-15)

Gap-audit Wave 6 (rec #20). App-only; new `logic/consolidation.py` + `optimize.py`.

- **The fleet is now scanned for warehouses that could share one.** The sizing
  simulator only scaled a warehouse up/down in isolation; nothing looked across the
  fleet. New pure `consolidation_candidates` pairs same-size-class, same-scope
  warehouses whose hour-of-day active windows barely overlap (no concurrency
  collision) — the two workloads plausibly fit on one warehouse — retiring the
  mostly-idle one and saving its idle tail. Candidates are ranked by saving and
  assigned greedily so each warehouse appears in at most one merge. A toggle-gated
  "Fleet consolidation candidates (review-only)" panel on Optimize > Idle & sizing
  reads hour-of-day activity and reuses the section's idle-tail $ and SHOW
  WAREHOUSES size class (no new ACCOUNT_USAGE table). Strictly review-only: it names
  the keep/retire pair and a conservative estimate, proposes nothing.
- **Scope:** the multi-cluster scale-in half of this rec (lowering MAX_CLUSTER_COUNT
  on rarely-saturated warehouses) needs cluster-count-over-time data that
  WAREHOUSE_LOAD_HISTORY does not cleanly expose, so it stays queued rather than
  shipping an ambiguous recommendation.

Gates green: ruff --no-cache, mypy, pytest.

## 4.179.0 - Feature: de-duplicated addressable-savings rollup (2026-08-15)

Gap-audit Wave 6 (rec #16). App-only; new `logic/savings_rollup.py` + `optimize.py`.

- **One honest "total addressable savings" headline.** Each advisor estimated
  recoverable dollars independently; nothing summed the OPEN opportunities, and a
  naive sum double-counts because idle-tune and size-down on the SAME warehouse
  recover the same idle credits. New pure `rollup_savings` de-duplicates overlapping
  opportunities on the same target (keeps the larger of an idle/resize pair, drops
  the other) and ranks the rest by confidence x dollars. A "Total addressable
  savings (de-duplicated)" panel on Optimize > Idle & sizing now shows the net
  monthly headline, the opportunity count, and how many overlaps were removed —
  collecting the idle-timer and right-sizing estimates already computed in that
  section (no new queries). The failed-query Wasted-spend, Serverless-ROI, and
  storage/clustering levers plug into the same rollup next (disclosed in the panel).

Gates green: ruff --no-cache, mypy, pytest.

## 4.178.0 - Feature: serverless ROI board — Query Acceleration (2026-08-15)

Gap-audit Wave 6 (rec #6). App-only; `cost_sql.py`, new `logic/serverless_roi.py`,
`optimize.py`.

- **Query Acceleration now shows ROI, not just spend.** Serverless features were
  tracked as spend only; the benefit side was never read (`QUERY_ACCELERATION_ELIGIBLE`
  was absent from the whole app). New `qas_roi` builder FULL-OUTER-JOINs QAS credits
  spent (`QUERY_ACCELERATION_HISTORY`) against the eligible acceleration workload
  (`QUERY_ACCELERATION_ELIGIBLE`) per warehouse, and a pure `classify_qas_roi` labels
  each: **drop candidate** (paying for QAS with little eligible workload — it rarely
  helps), **enable candidate** (eligible workload with QAS off — a possible speedup),
  working, or minimal. A toggle-gated "Serverless ROI — Query Acceleration" panel on
  Optimize > Idle & sizing shows the spend, drop/enable counts, and per-warehouse
  verdicts. Eligibility is honestly framed as a utilization signal, not a dollarized
  compute saving. Optimize reachable-table pin gains the two QAS views (deliberate);
  not canaried (QAS eligibility can be edition-sensitive — the panel degrades
  gracefully).
- **Scope:** this ships the QAS arm — the one serverless feature with a real
  Snowflake benefit signal. Search-opt / MV-refresh / auto-clustering credits are
  already tracked under Storage & waste and the object ledger; their true benefit
  needs query-level correlation and stays queued.

Gates green: ruff --no-cache, mypy, pytest.

## 4.177.0 - Feature: anomaly root-cause auto-explain (2026-08-15)

Gap-audit Wave 6 (rec #5). App-only; new `logic/anomaly_explain.py` + `spend.py`.

- **Flagged spend days now explain themselves.** The per-warehouse anomaly panel
  detected outliers but only told you which warehouse and how many z — not WHY.
  New pure `explain_by_warehouse` decomposes the flagged day's total spend delta
  across warehouses: each warehouse's flagged-day spend minus its trailing
  robust-median baseline, ranked by contribution, with the deltas summing to the
  day's total move by construction (nothing hides in a residual). A brand-new
  warehouse contributes its whole spend; one that went silent contributes a
  negative delta. The strongest flag now renders a one-line narrative ("Spend on
  D was $X — $Y above the recent median; top driver: WH_A, $Z over its usual, 80%
  of the move") and an expandable contribution waterfall, so triage jumps from
  "something spiked" to "this warehouse did, that much." Pure and additively
  self-checking; no new queries (reuses the loaded FACT_WAREHOUSE_DAILY frame).

Gates green: ruff --no-cache, mypy, pytest.

## 4.176.0 - Feature: wasted-spend board for failed/killed queries (2026-08-15)

Gap-audit Wave 6 (rec #17). App-only; `insights_sql.py`, `operations.py`.

- **Failed / killed / aborted queries that consumed compute now surface as $
  wasted.** Failure waste was computed only for tagged ETL pipelines; there was no
  account-wide view of money spent on runs that produced nothing. New
  `wasted_query_spend_usd` builder allocates warehouse-hour credits to non-success
  queries by the same execution-time hour-share as `expensive_queries_usd` (the
  denominator counts every query in the hour, so a failed query's share isn't
  inflated), then rolls up by parameterized fingerprint so a broken query on a
  retry loop surfaces as repeat waste. A toggle-gated "Wasted spend" board on the
  Operations Queries tab shows the window total, a monthly-ized figure, repeat
  offenders (5+ failures), and per-fingerprint waste with the error code. Priced at
  the compute rate (warehouse metering is compute). Builder canaried; Operations
  reachable-table pin gains WAREHOUSE_METERING_HISTORY (deliberate).

Gates green: ruff --no-cache, mypy, pytest.

## 4.175.0 - Consistency: ops metric definitions align (2026-08-15)

Gap-audit Wave 5 (rec #49). App-only; `operations.py`, `mart27_sql.py`.

- **Fail-rate tile now uses the 2% materiality threshold.** The Operations
  fail-rate KPI warned on any single failed query, while Control Room (`> 0.02`)
  and the platform score use a 2% threshold — so the same account read "warn" on
  one page and "ok" on another. The tile now warns only above 2%.
- **Ops-diag hourly windows are day-aligned.** `role_hourly` / `schema_hourly`
  anchored `HOUR_TS` on `CURRENT_TIMESTAMP` (rolling 24h) while the live query
  summary anchors `CURRENT_DATE` (midnight-aligned); the two disagreed by up to a
  day. The diag marts now anchor `CURRENT_DATE` to match.
- **Failure taxonomy was already aligned in V062** (not a new fix): both the live
  summary and the mart `FAILED_COUNT` count `EXECUTION_STATUS <> 'SUCCESS'`. The
  audit's suggestion to switch the app to `= 'FAIL'` would have *broken* that
  parity, so it was deliberately not applied; a test now locks the `<> 'SUCCESS'`
  parity on both sides.

Gates green: ruff --no-cache, mypy, pytest.

## 4.174.0 - Consistency: AI dollars reconcile to org AI_USD (2026-08-15)

Gap-audit Wave 5 (rec #32). App-only; `contract.py`.

- **The billing-truth reconciliation now models the AI/Cortex bucket too.** The
  "Billing truth vs app model" table checked the compute bucket against org
  COMPUTE_USD but only *displayed* org AI_USD — the app's AI dollars (AI credits x
  the AI rate) were never checked against it, so a wrong AI rate or missed AI spend
  produced a silent error. Two columns are added — modeled AI $ and AI drift % vs
  org AI_USD — aligned to the same monthly grain as the compute recon (so a
  boundary month partly outside the window drifts identically, no window mismatch).
  A steady AI drift now flags that AI_CREDIT_PRICE_USD is mis-set, the same way the
  compute drift flags CREDIT_PRICE_USD.

Gates green: ruff --no-cache, mypy, pytest.

## 4.173.0 - Cost completeness: egress is dollarized (2026-08-15)

Gap-audit Wave 4b (rec #11). App-only; `cost_sql.py`, `formulas.py`, `spend.py`,
`config.py`.

- **Data transfer / egress now has a priced Cost-page drill.** `DATA_TRANSFER_HISTORY`
  was tracked only as a bytes signal on Security and never dollarized. A new
  "Egress / data transfer" section (under Cost > detailed service attribution)
  breaks transfer down by source/target cloud+region and transfer type, with a
  BILLABLE flag (cross-region / cross-cloud is billed; same-region is free and
  priced at $0), priced from the org rate-card implied $/TB and reconciled to the
  billed `TRANSFER_USD`. New `DATA_TRANSFER_USD_PER_TB` setting is the fallback
  when org billing truth isn't usable.
- **Verified with a 2-skeptic workflow before commit.** Both skeptics confirmed a
  P1 window mismatch (org bill was read over a fixed 30d while egress bytes span
  the page window) — fixed by reading org truth over the same `days` window via
  the verified `RATING_TYPE='DATA_TRANSFER'` builder (`org_all_in_window_usd`), not
  a `SERVICE_TYPE` match. Also fixed: non-USD org currency was rendered with a `$`
  (now reconciliation engages only when the bill is USD), and an implausibly high
  implied $/TB (from a BILLABLE under-count) now falls back to the setting instead
  of presenting a five-figure rate as billing truth. The `transfer_egress_priced`
  SQL itself was verified clean (no lateral-alias bug, correct cross-boundary
  logic, binary-TiB units).

Gates green: ruff --no-cache, mypy, pytest.

## 4.172.0 - rec #10 already fixed in V064: stale docstring corrected (2026-08-15)

Gap-audit Wave 5 (rec #10). App-only; `mart_sql.py` (docstring) + regression test.

- **No V081 was needed — the COST_CONTRACT_BREACH burn was aligned in V064.** The
  audit re-flagged the paging alert's `DAILY_BURN` as still dividing by a literal
  `/30`, but the authoritative alert proc (`SP_ALERT_SCAN_DAILY`) was fixed to the
  canonical `SUM / NULLIF(COUNT(DISTINCT DAY), 0)` over `today-30 .. today-1` back
  in **V064** ("rec20a"), and every later re-derivation (V065/V066/V079, all
  applied) carries it. The false positive came from (a) the immutable `/30` in the
  pre-V064 migration history and (b) a stale `contract_exhaustion` docstring still
  claiming the alert "carries the OLD math — align it in the V065 owner migration".
  That docstring is corrected, and a regression test now locks the authoritative
  burn to `COUNT(DISTINCT DAY)` so a future proc re-derivation can't reintroduce
  the `/30`.

Gates green: ruff --no-cache, mypy, pytest.

## 4.171.0 - Drill honesty: measured query costs on the Optimize panel (2026-08-15)

Gap-audit Wave 4b (rec #41). App-only; `optimize.py`.

- **The exact measured lens now sits beside the allocated estimate.** The
  Optimize "most expensive queries" panel priced queries only by hour-share
  allocation (`expensive_queries_usd`) — which spreads the whole warehouse-hour
  bill, idle included, and distorts on an idle-heavy warehouse — while the exact
  `QUERY_ATTRIBUTION_HISTORY` attribution (`measured_query_costs`, idle excluded)
  was siloed on the separate Unit-costs tab. A new toggle-gated "Most expensive
  queries (measured $, exact attribution)" panel offers the measured lens right
  there, clearly labelled measured-vs-allocated (measured reads below allocated
  because idle time is excluded), honoring the same database / schema / warehouse
  filters. Reuses the existing canaried builder — no new SQL.

Gates green: ruff --no-cache, mypy, pytest.

## 4.170.0 - Formula honesty: blended-fallback badge (2026-08-15)

Gap-audit Wave 3g (rec #28). App-only; `overview.py`.

- **The MTD credit-spend KPI discloses when it can't split AI from compute.**
  When a refresh carries only the bare `CREDITS_BILLED` total (a pre-split cache
  or a live shape without the AI/OTHER columns), `_billed_usd_series` prices every
  credit at the compute rate — overstating AI/Cortex-heavy spend by
  `(compute_rate - ai_rate) x AI credits`. That fallback was silent. A new shared
  `_billed_split_available` helper now drives a `flat-rate est.` method badge (in
  place of `billed`) and a help note on the Overview MTD KPI whenever the split is
  missing, so the degraded number is disclosed rather than trusted as billed.
  Every mart builder emits the split, so the badge only appears on the degraded
  path.

Gates green: ruff --no-cache, mypy, pytest.

## 4.169.0 - Formula honesty: year-strip prorates today's remainder (2026-08-15)

Gap-audit Wave 3f (rec #40, year-strip only). App-only; `contract.py`.

- **The calendar-year projection no longer runs low all day.** The year strip
  projected `YTD + trailing-burn x days-after-today`, but YTD already includes
  today's partial while today's REMAINING hours were never re-estimated — so the
  year-end figure crept up all day as today filled in. It now adds today's
  prorated remainder (`burn x fraction-of-day-left`), which makes the projection
  constant through the day: today's partial-so-far plus its prorated remainder
  equals exactly one full projected day at the trailing burn. (The `contract_pace`
  off-by-one in the same rec stays declined — ambiguous inclusive/exclusive
  END_DATE semantics, pending owner confirmation.)

Gates green: ruff --no-cache, mypy, pytest.

## 4.168.0 - Formula honesty: capacity channels + growth window (2026-08-15)

Gap-audit Wave 3e (rec #39). App-only; `capacity.py`.

- **Queue and remote-spill are forecast as separate channels.** The pressure
  index was `max(queue-min/30, spill-GB/1)` with a single Theil-Sen slope fit to
  that composite — when the two channels cross over the window the slope (and the
  ETA) matched neither. Each channel is now forecast independently to its own 1.0
  intervention line, and the ETA comes from whichever breaches *soonest*, with the
  driving channel named in the row's basis. `CURRENT_PRESSURE_INDEX` stays the
  `max()` of the two (how close to a threshold you are right now).
- **The workload-growth gate reads a recent sub-window.** `_growth_pct` compared
  the head vs the tail of the *entire* (up to 365-day) history, so a warehouse
  whose demand grew months ago and has since gone flat still corroborated an ETA.
  It now compares the last 30 days against the prior 30, so only growth that is
  actually happening now clears the gate.

Gates green: ruff --no-cache, mypy, pytest.

## 4.167.0 - Formula honesty: month-end forecast bands (2026-08-15)

Gap-audit Wave 3d (formula-correctness). App-only; `forecast.py`.

- **Month-end bands no longer over-narrow (rec #15).** Four honesty fixes to
  `month_end_projection`: (1) the seasonal engine now needs >= 4 weeks and reads a
  6-week baseline, so each day-of-week mean rests on ~4-6 samples instead of the 2
  a 14-day window gave; (2) the "Linear" engine finally carries a *robust*
  (Theil-Sen) daily trend instead of a flat mean — the label was a promise it never
  kept; (3) bands use `ddof=1` residuals against the fitted line and are inflated
  for parameter uncertainty and within-week autocorrelation (daily spend is not
  i.i.d.), so the interval stops pretending future days are independent draws; and
  (4) the minimum history for any projection rose from 3 days to a full week. A
  perfectly flat or perfectly linear history still yields a zero band — there is
  nothing uncertain to widen for.

Gates green: ruff --no-cache, mypy, pytest.

## 4.166.0 - Storage honesty: per-database excludes hybrid/stage/archive (2026-08-15)

Gap-audit Wave 4b (disclosure). App-only; `spend.py`.

- **Per-database storage disclosure (rec #30 / #33).** The per-database storage
  panel prices ACTIVE + FAIL-SAFE bytes only, so a hybrid-table-heavy database
  reads materially low there (hybrid / stage / archive carry no per-database split
  and live on the account-by-tier panel). The help now discloses that exclusion
  and reiterates the estimate-vs-org-truth basis, so the number isn't misread as a
  full per-database storage bill. (The full logic — adding hybrid bytes to the
  per-DB estimate and reconciling both panels to org STORAGE_USD — remains queued.)

Gates green: ruff --no-cache, mypy, pytest.

## 4.165.0 - Drill honesty: the coverage table now tells the truth (2026-08-15)

Gap-audit Wave 4 (drill-honesty core). App-only; `cost_coverage.py`.

- **Downgrade drills with no backing query (rec #42/#43/#47).** The coverage table
  advertised native drills that 404: `WAREHOUSE_METERING_READER`, `PIPE`,
  `SNOWPIPE_STREAMING`, `QUERY_ACCELERATION`, and `REPLICATION` all claimed a
  drillable grain with no reader/pipe/streaming/QAS/replication query wired (and
  replication bills on the DR account). All now read "Service total only". The
  residual `AI_SERVICES` line no longer inherits the Cortex "Drill ready" grain
  (it aggregates Analyst / Search / Document AI / Fine-tuning, none per-user
  drillable), and the Cortex grain dropped the phantom "warehouse" axis.
- **Credit the drills that DO ship (rec #44).** `SERVERLESS_TASK`, `AUTO_CLUSTERING`,
  `MATERIALIZED_VIEW`, `SEARCH_OPTIMIZATION` each have an exact native per-object
  `*_HISTORY` key AND are materialized in the object cost ledger — a real drill.
  New "Object-ledger drill" status; `drill_ready_spend_share` now counts it, so a
  serverless-heavy account no longer reads artificially low.
- **CoCo is drillable (rec #48).** Cortex Code / CoWork is drilled to user grain by
  the AI Chargeback tab (`FACT_AI_USAGE_DAILY`), so `_coverage_for` marks it "Drill
  ready" instead of the un-drillable default it fell to.

Gates green: ruff --no-cache, mypy, pytest 2142 passed / 1 skipped (coverage lock
tests updated for the deliberate status corrections). Wave 4 continues: storage
hybrid caption #30/#33, query-drill measured-vs-allocated #41, egress #11, Cortex
source rewire #17.

## 4.164.0 - Formula honesty: Cortex per-user projection window (2026-08-15)

Gap-audit Wave 3c. App-only.

- **Cortex budget projection per-user window (rec #38).** `enrich_user_rollup`
  divided every user's credits by the SCOPE window (the oldest user's history),
  so a heavy user new to a mature 30-day scope was projected at ~2 days of credits
  / 30 (~15x too low) and the budget ladder never flagged the real new breacher.
  It now projects each user against their OWN `OBSERVABLE_DAYS` (days since that
  user's first request, clamped to the window), and `classify_exceptions` applies
  a small-N guard (>= 4 observable days) so a brand-new user's first afternoon
  can't inflate into a false breach. New test locks both directions.

Gates green: ruff --no-cache, mypy, pytest 2142 passed / 1 skipped. (Wave 3
continues: forecast bands #15, blended fallback #28, pressure channels #39,
year-strip proration #40.)

## 4.163.0 - Formula honesty: capacity confidence + leading budget signal (2026-08-15)

Gap-audit Wave 3b. App-only.

- **Capacity ETA confidence on raw residuals (rec #14).** `capacity.py` fit the
  pressure trend on a 7-day-median SMOOTH series AND measured R2 / residual MAD /
  holdout MAE against that same smoothed line — inflating R2, shrinking the band,
  and overstating ETA confidence. It now fits the slope on the smooth series (for
  a stable trend) but scores fit quality and the holdout error against the RAW
  pressure index, so the `r2 >= 0.35` gate and the uncertainty band reflect real
  forecast error and the ETA range widens honestly.
- **Budget penalty is a leading signal (rec #37).** The platform score penalized
  budget only when cumulative MTD crossed 100% — late in the month, after the
  overrun was locked in. It now drives off the PROJECTED month-end vs budget, so
  an account on pace for 200% is penalized now. Driver renamed "Over budget" ->
  "Budget pace" ("Tracking to N% of the monthly budget"); `_SCORE_DRIVER_NAV`
  updated. (The retro score sparkline stays on the cumulative basis for now.)

Reviewed but NOT changed: rec #40 contract-pace off-by-one — hinges on ambiguous
END_DATE inclusive/exclusive semantics and would make an on-pace contract show a
spurious small overage; a hand-verification test documents the current
convention. Flagged for owner to confirm END_DATE meaning before touching.

Gates green: ruff --no-cache, mypy, pytest 2141 passed / 1 skipped. (Wave 3
continues: forecast bands #15, blended fallback #28, Cortex window #38, pressure
channels #39, year-strip proration #40.)

## 4.162.0 - Formula honesty: size-down saving is a range (2026-08-15)

Gap-audit Wave 3 (first formula fix). App-only.

- **Size-down "potential saving" (rec #13).** `sizing.py` reported every
  size-down candidate's saving as `MONTHLY - 0.5*MONTHLY = 0.5*MONTHLY` — assuming
  a smaller warehouse halves every credit. It doesn't: halving the per-hour rate
  reliably halves only the IDLE portion, while a compute-bound query on a smaller
  warehouse runs ~2x longer so its busy credits stay ~flat (the module's own
  `simulate_scenario` already bounds this). Now `SAVING_LOW_USD = 0.5*idle`
  (conservative floor) and `SAVING_HIGH_USD = 0.5*bill` (optimistic), the headline
  `POTENTIAL_MONTHLY_SAVING_USD` is the floor, `sizing_summary` carries both, and
  the Optimization KPI shows the range with honest help. Stops overselling the
  size-down bet against the SLA risk it carries.

Gates green: ruff --no-cache, mypy, pytest. (Wave 3 continues: capacity/forecast
confidence #14/#15, blended fallback #28, budget-pace #37, Cortex window #38,
pressure channels #39, year/contract off-by-one #40.)

## 4.161.0 - Financial truth: all-in invoice tile + contracted rate (2026-08-15)

Gap-audit Wave 2 (second batch). App-only; both new reads degrade quietly when
ORGANIZATION_USAGE is not visible.

- **All-in invoice tile (rec #8).** The Spend headline "Credit spend" is the
  metering lens only — it structurally omits storage, transfer, and marketplace.
  New `cost_sql.org_all_in_window_usd(days)` sums `USAGE_IN_CURRENCY_DAILY` for
  this account over the same window, and a new "All-in billed (org rate card)"
  tile sits beside the credit-spend tile with the storage / transfer / other
  breakout in its help, so the headline reconciles to the invoice.
- **Contracted rate from RATE_SHEET_DAILY (rec #9).** Every dollar keyed off two
  hand-entered constants; the actual contracted rate was never read. New
  `cost_sql.org_rate_sheet()` reads `RATE_SHEET_DAILY.EFFECTIVE_RATE`, and the
  Contract rate-card reconciliation now shows the contracted compute rate beside
  the configured SETTINGS rate with a drift %. Read-only reconciliation input —
  pricing still uses the admin-configured rate by design (adopting the contract
  rate as the pricing source stays an explicit Admin action). NOTE: RATE_SHEET_DAILY
  column/USAGE_TYPE spellings are account-specific; confirm against Snowsight.

Gates green: ruff --no-cache, mypy, pytest. (Egress dollarization #11 moves to
Wave 4 as a transfer-type drill panel.)

## 4.160.0 - Financial truth: CoCo label, org residual, taxonomy canary (2026-08-15)

Gap-audit Wave 2 (first batch). App-only.

- **CoCo tile is a non-additive subset (rec #7).** The Spend tab's "CoCo spend"
  tile was labeled "billed separately from the metering line" — false since
  V079: CoCo bills as `SNOWFLAKE_COCO_SNOWSIGHT` *inside* `METERING_DAILY_HISTORY`,
  so it is already in the Credit-spend / Total-credits tiles. Relabeled
  "— of which CoCo" with a "do not add to the totals" note.
- **Org rate-card reconciles to TOTAL (rec #29).** `org_account_month_usd` matched
  `RATING_TYPE` case-sensitively and had no residual, so a differently-cased or
  new rating type dropped from every named bucket while still landing in TOTAL.
  Now `UPPER(RATING_TYPE)` + an explicit `OTHER_USD` (everything not COMPUTE / AI /
  STORAGE / TRANSFER — marketplace, priority-support/VPS, new types), so the named
  buckets + OTHER sum to TOTAL exactly. `ADJUSTMENT_USD` documented as an
  orthogonal flag, never a 6th additive slice (Codex #5).
- **Taxonomy keys + unknown-service canary (rec #31 / #21).** Added `LOGGING`,
  `TRUST_CENTER`, `DATA_QUALITY_MONITORING` to `SERVICE_CATEGORY` (they were
  decaying into "Other"). New `cost_coverage.material_unmapped_services()` flags
  material "Other" spend at runtime, and `tests/test_cost_coverage_taxonomy.py`
  is the CI counterpart that fails when a KNOWN service type goes unmapped — the
  guard that was missing when the v4.158.0 PIPE/AUTO_CLUSTERING typos shipped.

(Wave 2 continues: all-in invoice tile #8, RATE_SHEET_DAILY pricing #9, egress
dollarization #11 land next.)

## 4.159.0 - Metric parity: three P0 cross-surface fixes (2026-08-15)

Gap-audit Wave 1. Three headline metrics read as different numbers depending on
the page; all three now agree. App-only.

- **Company warehouse spend (rec #1) + per-day average (rec #36).** Overview's
  "Spend, last N days" summed the exec-board DAILY_SPEND panel *including* today's
  still-filling row, while the Cost page's "By warehouse (exact usage)" excludes
  today (`common.resolve_effective_window`). Overview now drops today's partial
  from both the window total and the "Average per observed day" denominator, so
  the two pages reconcile; the trend spark keeps the full series for continuity.
- **MTD account $ month boundary (rec #2).** The sidebar/Brief health strip
  truncated the month with `DATE_TRUNC('month', CURRENT_DATE())` (Snowflake
  session tz) while Overview's MTD anchors `account_today()` (America/Chicago),
  so near midnight the two picked different day sets. New
  `common.account_month_start_sql()` anchors the strip's MTD to the account tz.
- **Open criticals (rec #3).** The sidebar badge counted `STATUS='OPEN'` only and
  account-wide, while every page/score counts `STATUS IN ('OPEN','ACK')` — so an
  acknowledged critical vanished from the sidebar but stayed on the pages. The
  strip's `OPEN_CRITICAL_N` now counts OPEN+ACK to match; the undelivered-critical
  signal stays OPEN-only (an ACK'd critical was seen, not a silent routing miss).

## V080 (owner migration) - Security change-risk ETL exclusion (2026-08-15)

Stops the Security **Change Risk** queue from flooding on routine ETL truncate-
and-reload DDL. V075 classifies every DROP/TRUNCATE as a CRITICAL "DESTRUCTIVE"
change and `V_SECURITY_EXCEPTION_QUEUE` surfaces it (RISK_SCORE >= 70). But on
this account that volume is automated ETL — three service-role families run
`DROP TABLE IF EXISTS` / `TRUNCATE` on EDW tables thousands of times a day
(confirmed from `ACCOUNT_USAGE.QUERY_HISTORY`, owner 2026-08-15) — which buried
real signal and forced the domain score to 0/100. No app version bump (owner
migration + lockstep only).

- Re-derives (derivation law, `outputs/gen_v080.py`, byte-compare-locked)
  **V_SECURITY_EXCEPTION_QUEUE** (from V075) with ONE addition to the CHANGE RISK
  arm's `WHERE`: exclude `CHANGE_KIND = 'DESTRUCTIVE'` events performed BY the 18
  confirmed ETL-engine roles — the three families `TF_SFR_<env>_GLUE`,
  `TF_SFR_<env>_INFORMATICA`, `TF_O_<env>_ALFA_SYSADMIN`, each expanded across the
  six environments (PRD, MGM, SAN, SEA, DEV, PHX).
- **Deliberately scoped so real signal still flows:** GRANT / REVOKE / POLICY
  changes by those same roles are STILL surfaced (privilege escalation is real);
  a DESTRUCTIVE drop by any OTHER (human / interactive) role, even in the same
  EDW schemas, is STILL surfaced. Only automated truncate-and-reload by the named
  engines is de-noised.
- **Audit-safe:** the rows are NOT deleted — they remain in
  `FACT_SECURITY_CHANGE` for drill-down and audit; only the exception QUEUE (and
  the domain score that reads it) stop counting them. Adding/removing a role later
  is a one-line re-derivation of this same view.
- **NULL-safe:** the match uses `COALESCE(ROLE_NAME, '') IN (…)`, not a bare
  `ROLE_NAME IN (…)`. `ROLE_NAME` is nullable in `FACT_SECURITY_CHANGE` (the base
  view already `COALESCE`s it), and a bare `IN` under `NOT (…)` would go three-
  valued and silently drop a DESTRUCTIVE event with an unattributed (NULL) role —
  precisely the event a security queue must keep. A NULL role now surfaces.
- View-only: no data reload, no new objects, teardown unchanged. Fixes both the
  queue display and the domain score at once (same view). Guarded `IF (v < 79)`;
  owner applies in Snowsight after V079.
- Lockstep to floor 80: `validate.sql` (6 spots), `admin.py` `_EXPECTED_MIGRATIONS[80]`
  + Setup-progress tuple, `02_migrations_V001_V080.sql` bundle regen, run-lists
  (DEPLOYMENT / README / FULL_REBUILD / rebuild README), floor-pin tests bumped.
- Tests: `tests/test_v080_change_risk_etl_exclusion.py` (5) — byte-identical
  regen, one guarded view swap, the 18 ETL roles excluded scoped to DESTRUCTIVE
  (and `SNOW_PRI_GFR` deliberately absent), reverse-derivation proving the ONLY
  change vs the V075 base is the exclusion block, and sqlglot parse.

## V079 (owner migration) - AI predicate historical split (2026-08-15)

Follow-up to v4.158.0's app-side reconciliation: the app DISPLAY splits AI vs
compute at read time (correct for all history), but three LOADER procs still
MATERIALIZED the split with the narrow predicate, so stored columns / thresholds
priced Cortex Code / CoWork as compute. V079 re-derives them with the broadened
predicate. No app version bump (owner migration + lockstep only).

- Re-derives (derivation law, `outputs/gen_v079.py`, byte-compare-locked) four
  procs: **SP_LOAD_PLATFORM_SCORE** (from V061 →
  `FACT_PLATFORM_SCORE_DAILY.CREDITS_BILLED_AI`), **SP_ALERT_SCAN_DAILY** (from
  V066 → `COST_BUDGET_PACE`/`COST_FORECAST_BREACH` MTD_USD + `COST_AI_CREEP`),
  **SP_REFRESH_EXEC_BOARD** (from V073 → `MART_EXEC_BOARD` IS_AI + DRIVER_LABEL)
  broaden the AI positive predicate with `%COCO%`/`%COWORK%` (byte-equal to
  `common.ai_service_predicate()`); and **SP_ALERT_SCAN** (from V067) rewrites
  `COST_SERVERLESS_CREEP`'s hardcoded `NOT IN (…,'AI_SERVICES')` carve-out to the
  canonical `not_ai_service_predicate()` so CoCo is excluded from the serverless
  scan — otherwise it would double-alert (AI-creep *and* serverless-creep) on
  the same credits. AI and serverless stay mutually exclusive.
- **Rate-attribution only** — total `CREDITS_BILLED` unchanged; only the
  AI/OTHER partition and blended USD move. CoCo now prices at the AI rate
  ($2.20) not compute ($3.68): budget-pace/forecast alerts soften slightly,
  AI-creep sharpens (CoCo joins the AI bucket); serverless stays mutually
  exclusive with AI.
- Ends with a re-fill of the two materializing loaders
  (`SP_LOAD_PLATFORM_SCORE(120)`, `SP_REFRESH_EXEC_BOARD()`) to re-attribute
  recent history at apply time. `SP_ALERT_SCAN_DAILY` is **not** called (it
  would fire alerts) — it self-corrects on its next scheduled run.
- No new objects (three `CREATE OR REPLACE PROCEDURE` only); teardown
  unchanged. **Owner applies in Snowsight after V078.**

## 4.158.0 - Metric reconciliation: CoCo/AI, serverless labels, replication (2026-08-15)

App-side fixes from the "reconcile every metric vs org-currency truth" audit
(owner: "we need to do this for every metric"). Corrects the DISPLAY today; a
follow-up owner migration re-derives the historical mart split.

- **Canonical AI-service predicate.** The SQL predicate identifying Cortex/AI
  service types had drifted from `cost_coverage._is_ai_family` and excluded
  `SNOWFLAKE_COCO_SNOWSIGHT` (Cortex Code / CoWork). One shared
  `ai_service_predicate()` / `not_ai_service_predicate()` in `data/common.py`
  now feeds every metering builder (`_AI_SERVICE_PRED`, the compute complement,
  `fact_cortex_daily_spend`, `cortex_daily_spend`, `compare_billed`), so:
  - the "Cortex / AI spend (account-wide)" chart no longer drops the largest AI
    line (CoCo was ~100% of real AI spend, rendering ~$0.01), and
  - the billed AI-vs-OTHER split prices CoCo at the AI rate ($2.20), not the
    compute rate ($3.68), across MTD / Cost Truth / YTD / Compare / Decision
    Studio headlines. The SQL and Python classifications can no longer diverge.
- **Serverless service-type labels.** `METERING_DAILY_HISTORY` names Snowpipe
  `PIPE` and auto-clustering `AUTO_CLUSTERING`; the category map keyed the old
  spellings (`SNOWPIPE`, `AUTOMATIC_CLUSTERING`), so both fell to "Other" and
  were flagged as coverage gaps. Added the real keys (old kept as aliases).
- **Replication detail — org-currency fallback.** The native
  `DATABASE_REPLICATION_USAGE_HISTORY` view is current-account only, but
  replication + data transfer bill on the secondary/DR account — so the panel
  claimed "no replication recorded" while the org rate card showed real spend
  (e.g. PRIMARY_DR REPLICATION). It now falls back to
  `USAGE_IN_CURRENCY_DAILY` (billing truth, currency as-is) with a note.

Not in this release: the loader's historical MTD_USD split (V061/V062/V064)
still classifies past CoCo credits as compute — an owner migration re-derives
that as a follow-up.

## 4.157.0 - Triage/AI cleanup + jump removal + filter accuracy (2026-08-15)

App-only UI pass from the live-screenshot review. No migration. (Cost
completeness — CoCo/Replication understatement vs org-currency truth — is a
separate reconciliation release.)

- **Triage strip slimmed.** Removed the **Legend** and **Views & display**
  popovers (owner: unused in daily operation). Saved default views, compact
  density, and display timezone still hydrate at startup from `USER_PREFS`
  (`_apply_default_landing`) — only the in-strip editors are gone. **More**
  (warehouse/user/schema contains-filters) stays.
- **Status strip on Brief + Overview only.** The persistent Open-criticals /
  Undelivered / Telemetry-age / MTD-spend strip is orientation for the two
  morning surfaces; the drill/govern pages below no longer repeat it.
- **In-page "JUMP TO" chips removed.** They rendered scroll JS inside a
  cross-origin `components.v1.html` iframe, so `window.parent.document` was a
  SecurityError and nothing ever scrolled in Streamlit-in-Snowflake — dead since
  they shipped. The sidebar "Open destination" jump (which works) stays; the
  test that pinned the dead markup is replaced with a removal assertion.
- **Cost & Contract KPI swap.** The two static rate tiles (Compute rate /
  Cortex rate — just SETTINGS echoes) become **Total credits** (free from the
  frame already read) and **CoCo spend** (Cortex Code $, priced at the AI rate
  via the formulas layer, batched off `FACT_AI_USAGE_DAILY`).
- **Duplicate AI panel consolidated.** The "AI Functions usage" breakout moved
  out of the per-user AI-users panel to a drill-down under **Cortex / AI spend**
  — one home for account AI spend.
- **Sub-dollar money reads honestly.** `daily_stacked_usd` uses a
  magnitude-aware axis and `_share_note` shows cents below $100, so a genuinely
  pennies total renders `$0.01`, not `$0` / "58% of $0".
- **`n/a` model name clarified.** A caption explains Cortex Code bills by token
  with no per-model grain — expected, not missing data.
- **More filters — accuracy.** Operations → Queries' failure-family panel now
  honors the warehouse/user contains filters it advertised (it scoped by
  company/db/schema only). Three filter-contract declarations (Unit costs,
  Change impact, Optimization) now list the contains filters they actually
  apply, so the banner stops falsely warning "ignored".

## 4.156.3 - Compact triage command strip + reliable jumps (2026-08-14)

App-only UI/navigation fix; no migration.

- Replaced the three-level triage filter panel with one operator command strip:
  Scope, Company, Window, Database, More, Legend, Views, and Reset. Advanced
  contains-filters live in a count-badged popover; long-window disclosure remains.
- Sidebar Jump-to now uses an explicit Open destination action instead of relying
  on selectbox rerun timing.
- In-page Jump-to links now render as CSP-safe buttons that scroll the real parent
  section header inside Streamlit-in-Snowflake, with a plain-anchor fallback.
- Cost → Compute pools & notebooks resolves each notebook login through the shared
  user directory and shows exact First name / Last name columns beside User name.

## 4.156.2 - Warehouse runtime reset guard (2026-08-14)

Deployment-only fix; no data migration.

- Added an owner-run worksheet that unsets retained container-only
  `ARTIFACT_REPOSITORIES` before explicitly selecting
  `SYSTEM$WAREHOUSE_RUNTIME` on `WH_ALFA_ADMIN`.
- Locked `snowflake.yml` to the warehouse-runtime shape and documented the
  Snowsight save failure. Git omission alone does not clear persisted
  Streamlit object properties.

## 4.156.1 - Complete no-evidence and credit-label contracts (2026-08-14)

App-only follow-up from the completed v4.156 architecture cross-check. No migration.

- Control Room cannot render “Checked / Clear” when query count has no denominator.
- Decision scenarios show `No evidence` when confidence filtering leaves no eligible entities.
- The shell and metric registry explicitly label modeled credit spend/runway, reserving billing
  truth for organization currency. Security Clients keeps one self-identifying built-in CSV.

## 4.156.0 - Metric trust, decision surfaces, and hot-path performance (2026-08-14)

App-only implementation round from the v4.155 metric/visual/performance audits.
No migration; the owner-deferred loader scan consolidation remains deferred.

- **Headline trust.** Brief and Control Room alert counts now use the same
  company-plus-account-level scope as the queues they open; Overview wording
  matches that scope. Zero query/task/SLO denominators and failed savings-ledger
  reads render as `n/a`, `No evidence`, or `Unavailable` instead of healthy zeroes.
- **Billing terminology.** Configured-rate credit spend and credit-commitment
  runway no longer claim to be the full invoice. Organization currency/rate-card
  panels remain the explicitly labeled billing truth.
- **Decision-first surfaces.** Security's queue/governance overview is now its
  own default lazy section, so Access/Changes/Clients/Egress/Trust Center do not
  pay for it. Warehouse contention ranks average queue per query. Idle,
  right-sizing, and warehouse-change tables lead with the decision and reveal
  full evidence only after selection—never silently row zero.
- **Hot-path performance.** Operations → Queries batches its hourly summary,
  activity, top-query, and failure-family marts while preserving filtered live
  fallbacks. Alerts selects its lazy section before loading the 500-row open
  feed. Table styling and eager CSVs use cell budgets, and Security Clients no
  longer serializes a duplicate CSV.

## 4.155.0 - UI theme and section readability polish (2026-08-14)

App-only visual pass from the live screenshot review. No migration.

- **Softer command-center palette.** Replaced the high-contrast navy/cyan chrome
  with warmer slate surfaces and a calmer blue/teal accent pair, mirrored through
  `palette.py`, `theme.py`, `.streamlit/config.toml`, chart chrome, and table
  status fills so cards, charts, native controls, and embedded task graphs agree.
- **Cleaner section hierarchy.** Added intentional scope-chip layout, removed the
  duplicate chip CSS block, moved dense Operations change-impact explanations
  into help popovers, and added jump strips/anchors for long Operations and Cost
  sections.
- **Small design-system fixes.** Added the missing Decision Studio target icon,
  routed the default broad table height through `sizing.py`, and rendered the
  Legend with the same semantic chip vocabulary used elsewhere.

## 4.154.0 - Topology first-paint + error-family coverage (2026-08-13)

Two app-only fixes from the 2026-08-13 live-screenshot review. No migration.

- **Pipeline topology ~21s first paint (rec34 regression).** v4.150's
  failures-first root sort added the first-ever live `TASK_HISTORY` scan to two
  formerly metadata-only queries (`task_graph_roots` and `task_graph_nodes`
  carried identical CTEs), and the secure-view expansion dominated the paint.
  Both now read the same day-grain `FAILED` counts from `MART_TASK_NODE_DAILY`
  (V058, already loaded and read on this page) via a shared CTE helper. Honest
  relabel: `FAILURES_24H` → `RECENT_FAILURES` end to end (SQL, DAG badge, KPI,
  picker label/help) — the window is now today + yesterday at day grain, not
  rolling 24h. An empty or lagging mart degrades the sort to node-count order
  through the existing LEFT JOIN + COALESCE, never an error. The root picker
  also moves `recent` → `hourly` cache tier (day-grain counts don't need 5-min
  freshness).
- **Task-failure "Top error family: Other (73%)".** Three new classifier
  families for the live account's dominant fall-throughs: **Concurrency /
  live version** ("There is already a live version. Please commit it first." —
  929 fails, the single largest error), **Session not set up** ("session does
  not have a current database"), and **Metadata not ready** ("not yet
  available"). Ordered before Missing object so nothing is misbucketed; all
  existing family pins unchanged.
- **V078 (owner migration, follow-up): AI usage loader unbreak.** The owner's
  365-day backfill surfaced why `FACT_AI_USAGE_DAILY` never covers: the ai_code
  arm of `SP_LOAD_MARTS_V27` failed on *every* run — `CORTEX_CODE_*` views
  expose `USAGE_TIME` as `TIMESTAMP_TZ`, the fact's `FIRST_TS`/`LAST_TS` are
  `TIMESTAMP_NTZ`, and MERGE refuses the coercion. V078 re-derives the proc
  from V066 (generator `outputs/gen_v078.py`, byte-compare-locked) with exactly
  two `::TIMESTAMP_NTZ` casts, and ends with a DAILY/365 first-fill so the
  apply itself heals the mart depth. Once applied, the app's AI coverage gate
  passes and the ~20s live Cortex fallback on Chargeback & AI → AI users stops
  firing. **Owner-applied in Snowsight after V077.**

## 4.153.0 - Fix Entity 360 Watchlist infinite-rerun (2026-08-13)

Owner-reported: clicking a watchlist row made the app refresh indefinitely,
forcing a disconnect. App-only fix, no migration.

- **Root cause.** `render_watchlist` fed a raw sticky `selectable_table`
  selection straight into `request_navigation`. A same-page jump carrying a
  section + context is deliberately not a no-op, and the Entity 360 nested
  sub-tab stayed on "Watchlist" — so each rerun re-rendered the very component
  that re-fired the navigation. Infinite loop.
- **Fix.** The established seen-guard (as in `selectable_nav_table` /
  `decision_rows`): navigate only on a CHANGED selection. The click also sets a
  one-shot `_ow_entity_view_pending` flag that the Entity 360 dispatch consumes
  before `nested_sections` renders, switching to the "Entity" sub-tab — the
  drilled entity actually shows, and the watchlist leaves the render path.
- **Re-arm on unselected render** (adversarial-review refinement): the drill
  unmounts the table, so a stale remembered index would silently swallow the
  first click on whatever row later occupies it; an unselected mount clears the
  guard (the `clickable_bar_usd` re-arm), so every fresh click lands and a
  same-row re-drill works. A `None` selection never fires, so no loop path.
- Regression locks in `tests/test_v4153_watchlist_guard.py` (guard shape,
  re-arm-before-guard order, pre-render flag consumption order).

## 4.152.0 - Cost by application × user (V077 owner migration) (2026-08-12)

Answers "which program (and which user) drove the credits" so a misconfigured
tool is findable — the missing first-class dimension in the cost survey. Adds an
owner-applied migration + a Cost-page panel.

- **V077 (owner migration): FACT_APP_COST_DAILY.** A new daily fact + loader
  (`SP_LOAD_APP_COST`) + scheduled task that joins `SESSIONS`
  (`CLIENT_ENVIRONMENT:APPLICATION`, else the `CLIENT_APPLICATION_ID` driver
  family) → `QUERY_HISTORY` (`SESSION_ID`) → `QUERY_ATTRIBUTION_HISTORY`
  (`QUERY_ID`) into measured cost by (day, application, user, company). Each query
  maps to exactly one session/app/user/warehouse, so credits are additive.
  Self-trimming (400d); standalone off-peak task on `WH_ALFA_ADMIN`; company via
  `COMPANY_FOR_WAREHOUSE`. **Owner-applied in Snowsight; smoke-test the loader
  after applying.**
- **"Cost by application × user (measured)" panel** (Cost & Contract → Spend &
  Attribution, behind a toggle). Mart-first: reads `FACT_APP_COST_DAILY` once V077
  is loaded, else runs the live 3-way join so it works day-one. A bar of measured
  $ by application + an app × user table. MEASURED warehouse compute only
  (excludes idle, serverless, storage, AI); `(unknown)` = a session that reported
  no application or couldn't be joined.

Lockstep: migration floor → V077 (validate.sql, admin `_EXPECTED_MIGRATIONS`,
rebuild bundle regenerated, teardown, run-lists). New builders canaried.

## 4.151.0 - Decision Studio as its own page + Entity 360 catalog picker (2026-08-12)

App-only, no migration. The final two review recs, unblocked now that V074/V075
are applied and the workbench surfaces carry data.

- **Decision Studio is its own Analyze page (rec8).** It was nested inside Control
  Room — three navigation levels (page → section → sub-tab) — and shared a roof with
  the daily triage console despite being a weekly planning studio. Promoted to a
  top-level page under Analyze, with its six sections (Portfolio, SLOs, Products,
  Cost Truth, Scenarios, Experiments) as the primary section bar. Control Room is now
  the pure triage console (Action Center · Pulse · Incidents · Timeline · Freshness ·
  Entity 360). The section bodies did not move — only the page shell — so deep links
  and the cross-jumps into Entity 360 (which stays in Control Room) keep working.
- **Entity 360 catalog picker (rec12).** The entity input required typing a key — a
  QUERY_FINGERPRINT is a hash nobody memorizes. It now offers a catalog-seeded
  selectbox per entity type, keeping the free-text box as the escape hatch for
  entities not yet catalogued (and as the drill target). Degrades to the text box
  when the catalog isn't loaded.

## 4.150.0 - Review wave 2: triage depth, delivery visibility, onboarding (2026-08-12)

App-only, no migration. Second batch from the adjudicated Cursor UI/UX review.

- **Root picker orders failures-first (rec34).** The task-graph root list was
  ordered by name/size, burying a small failing tree under big healthy ones.
  `task_graph_roots` now sums 24h `TASK_HISTORY` failures per graph and orders
  failures-first; the picker label shows `⚠ N failed (24h)` and the 500-row cap
  keeps the most-failing graphs (never truncated away).
- **Per-event delivery state in the Alerts drawer (rec38).** The drawer can now
  answer "did THIS page reach anyone?" — a new `deliveries_for_event` reader joins
  `ALERT_DELIVERIES` to `ALERT_ROUTES` and the drawer lists the integrations + send
  times, or says it hasn't been delivered yet.
- **Duplicate work-item guard (rec19).** Action Center and Security both create
  into `ACTION_QUEUE` with no dedupe. Both now warn (not block) when the entity
  already has an open/in-progress item, reusing `related_actions`.
- **Arrival note for filter-applying jumps (rec24).** An alert "Investigate →"
  reshapes the global filters; the destination now announces "Filters applied from
  alert […]" once on arrival (in `page_header`), so the scoped view doesn't read as
  user-set. Shown only when the jump actually applies filters; drill identity
  (event_id/query_id) is preserved.
- **Cost Truth in dollars (rec29).** The four basis KPIs led with credits while the
  rest of the app leads with dollars. Now dollars-primary / credits-secondary; the
  compute-clean bases (metered/measured/allocated) convert at the compute rate and
  **BILLED** prices its AI/Cortex share at the AI rate via a new `billed_split`
  reader + `blended_billed_usd` — a flat rate would overprice AI credits (house
  rule d). Measured/allocated coverage ratios move to the caption.
- **Admin "Setup progress" panel (rec44).** One onboarding checklist consolidating
  the scattered "Apply VNNN" walls: database migrations (floor + V074/V075/V076
  presence), marts loading, and budget/contract/route config — each row reads live
  install state and names the fix. Read-only; applies nothing.

## 4.149.0 - Triage routing + decision honesty (2026-08-12)

App-only. First do-now batch from the adjudicated Cursor UI/UX review (verified
against the tree, house-decisions honored). No migration.

- **Triage rows carry their identity (rec20/21/22).** The Control Room morning
  queue used to compute a row's `EVENT_ID`/`RULE_ID`/warehouse and then navigate
  with page+section only. Now an **alert** click hands its `event_id` to the
  Alerts drawer (which already self-selects), a **task failure** lands on
  Operations → **Tasks** (the owning section, not the page default), and a
  **spend anomaly/collapse** lands on Operations → **Queries** scoped to the
  offending warehouse via `warehouse_contains` — the section that actually
  consumes that filter (a Cost section, or the Warehouses section, would silently
  no-op it).
- **Write-friction policy (rec14 + rec1), now a house law.** Friction matches
  consequence, not the table: a reversible upsert to OVERWATCH's own tables is
  one click (SQL preview still shown); a classifying/account-touching write keeps
  the type-to-confirm gate. Applied first to alert **ACK** — one click now, while
  **RESOLVE** (which feeds per-rule precision) still types to confirm.
- **`notify()` receipt split (rec48).** Success is a toast (the write's own
  rerun re-renders the changed table as the durable receipt); a **failure** keeps
  a persistent inline error so it can't auto-dismiss before it's read.
- **Exception-first Control Room + pill badge (rec10/11).** The Incidents & triage
  section now leads with the house `exception_summary` (open criticals / open
  incidents / stale sources, from numbers already in hand), and the Incidents pill
  badges its open-critical count — both zero extra queries (health strip).
- **No silent wrong-target writes (rec17/18).** Experiments no longer render (and
  let an operator Save) row 0 when nothing is selected; Scenarios shows an
  empty-state instead of sliders projecting a silent $0 over an empty queue.
- **Which-number honesty (rec28/32).** The two "Confidence" columns are now
  labelled by epistemics — **Confidence (evidence)** (portfolio heuristic) vs
  **Confidence (authored)** (a stated belief); the Products table's two dollar
  columns state in their help that they are separate, non-additive lenses (totals
  stay absent).
- **Honesty captions (rec26/49).** Entity 360 names the entity types that have no
  metric snapshot (so the absent KPI block doesn't read as broken); the Security
  caption now says it writes only to OVERWATCH's own work queue (operators only).

## 4.148.0 - Operations + Cost polish: volume-drop robustness, topology unblock, formatting (2026-08-12)

App-only, from a live-app review (screenshots + code verification):

- **Volume-drop false positives (Pipeline SLA).** Truncate-reload and per-run staging tables
  (dated `_YYYYMMDD_` names, `*_STAG`/`*_STG` schemas, the SNOWFLAKE internal DB) read as 0 rows /
  100% drop. `volume_deltas` now excludes those and requires a **steady baseline** (≥1,000 rows/day
  AND written on ≥3 of the prior 7 days), surfaces `DAYS_ACTIVE_7D`, and the panel help spells
  out the timing (yesterday = prior full calendar day; overnight batches book rows on the day
  they run).
- **Pipeline topology unblock.** `>500 root task graphs` hard-errored with no way to narrow.
  It now shows the 500 largest by task count plus a name filter, and (since Graph is account-wide)
  a notice that the Database filter scopes Health/Runs, not topology — the source of the
  "clicking ALFA_EDW_PRD shows other environments" confusion (the DB-scoped panels do filter).
- **Formatting.** Concurrency-peaks Peak Running/Queued, Storage-by-tier TiB/$/USD, and
  Release-compare Before/After (durations now humanize to Hr/Min/Sec) no longer render as
  6-decimal floats.
- **Anomaly drill.** The daily-anomaly warning now names the flagged day and points to the
  investigation path (By-warehouse table → Operations → Queries).
- **Contract effective rate.** Contract pacing gains an **Effective $/cr** column (org compute ÷
  billed credits) — the realized rate to reconcile `CREDIT_PRICE_USD` against; the prior-month
  gap indicates the configured $3.68 is ~$0.30 high.
- **Contention.** Warehouse pressure adds **Avg queue** (queued ÷ query count) so a low-volume
  warehouse that stalls every query surfaces above a busy one with trivial per-query waits.

## 4.147.0 - Cost-signal correctness: CoCo rate, canary predicate, anomaly + Cortex thresholds (2026-08-07)

Four cost/metrics fixes from a live-app review (screenshots + code verification). App-only,
no schema change.

- **CoCo priced at the AI rate.** `SNOWFLAKE_COCO_SNOWSIGHT` (Cortex Code / CoWork) matched
  neither a CORTEX token nor an AI prefix, so it fell to "Other" and priced at the compute
  rate ($3.68) instead of the AI rate ($2.20) — over-stating that ~$1.3k row ~67% and
  violating the never-inline-AI-rate rule. `service_category` now recognizes CoCo/CoWork as
  AI-family; the drill stays honestly "Service total only" (CoCo compute has no per-user
  usage history to drill).
- **Query-count canary predicate aligned.** The mart-vs-live reconciliation compared unequal
  populations — the live side filtered `WAREHOUSE_NAME IS NOT NULL`, the fact side summed all
  rows (incl. warehouse-less USE/SHOW/DESCRIBE) — reading a permanent ~+97% false BAD that
  masked any real drift. Both sides now count warehouse-bound queries.
- **Anomaly materiality floor.** The per-warehouse daily-spend anomaly scored a huge modified-z
  on any active day of a usually-idle warehouse, firing false "investigate" on trivial dollars
  ($49 at z+20). `flag_anomalies` gained `min_value` ($50) + `min_active_days` (10) gates,
  applied on the Cost / Control Room / Operations panels. The robust z stays the shape signal;
  the gate demands real money AND a real baseline.
- **Cortex "cost per request spike" is now relative.** It was a flat > 0.10 cr/req cut
  mislabeled a "spike" — every heavy-but-normal user of a pricier model was permanently flagged
  High. It now scores each user/source's cr/req against a robust median/MAD cohort baseline
  (z >= 3.5), keeping the >=20-request / >=$10-projected materiality floors.

Follow-ups: the server-side mirror shipped as owner migration **V076** (re-derives
`SP_ANOMALY_SWEEP` so the `COST_ANOMALY_SWEEP` alert path carries the same $50 / 10-active-day
/ spike-vs-collapse gate — the alert path now matches the display). Still owner-side: resume
`TASK_ALERT_NOTIFY` in Snowsight (ops) for the stranded non-critical alerts.

## 4.146.0 - Security page trimmed to read-only posture (2026-08-05)

Security was over-expanded into a full operating console (policy editors, access-review
campaigns) that "broke the current model." This trims the app back to the **read
improvements only** — posture overview, faster fact-backed panels, and effective-access
drill — and removes the policy/review write surfaces from the UI, logic, data and cache
layers. It also slims the V075 migration and its full lockstep (rebuild bundle, teardown,
validate proc contract, roles audit seal) to match: the 6 policy/review tables, the two
access-review procs, and the exception-queue's access-review arm are gone. V075 is
unapplied, so this is an in-place edit — the migration number stays V075. No behavior
change to any surviving panel.

## 4.145.0 - Decision Studio trust pass: name measured vs heuristic (2026-08-04)

Decision Studio computes correct dollars, but its `CONFIDENCE`, `LANE` and priority
scores are evidence-weighted **heuristics** that looked like measurements — so the
numbers were hard to trust. This makes the basis explicit at the point of display
(the `metric_registry` ethos, applied to decision scores).

- `decision_rows` gains `impact_help` / `confidence_help` so each surface states what
  its Impact $ and 0-1 Confidence actually are — they look identical across surfaces
  but are not the same thing.
- **Portfolio:** Impact is labeled measured ("observed cost, not promised savings");
  Confidence is labeled an evidence heuristic (run recency + active-day coverage +
  cost presence), explicitly "NOT statistical confidence"; and a caption states the
  exact lane rule (ACT NOW = top-20% priority AND confidence ≥ 0.65; VALIDATE =
  confidence < 0.5; otherwise PLAN) and which columns are measured vs heuristic.
- **Action Center:** its Impact is labeled an authored ESTIMATE (modeled, not billed;
  de-duplicated in scenarios, never mixed with verified savings) and its Confidence an
  authored belief, not a measurement.

No behavior or math changed — display-only labeling. Gates green: ruff, mypy,
**pytest 2056 passed / 1 skipped** (new `tests/test_decision_studio_trust.py`).

## 4.144.3 - CI guard against unauthorized warehouse provisioning (2026-08-04)

- Added `tests/test_no_unauthorized_warehouse.py`: fails CI if any migration or the rebuild
  bundle creates a warehouse other than the owner-sanctioned `WH_ALFA_ADMIN`, or if
  `config.APP_WAREHOUSE` is repointed off it. Provisioning a new billable warehouse — or
  switching the app's compute — is an owner infrastructure decision, not an app/agent change.
- Motivated by the `WH_OVERWATCH_APP` creation that was buried inside the 678-line V075
  migration and reverted by hand (v4.144.1). This guard makes a repeat impossible to merge
  regardless of the size of the change it hides in.

## 4.144.2 - Compile-safe Security change evidence (2026-08-04)

- Replaced the correlated `EXISTS` used to classify DDL/DCL change registration with
  a set-based registry join in both the V075 fact reader and live fallback. Snowflake
  no longer rejects Security -> Changes with `Unsupported subquery type`.
- Preserved account-level `NOT_APPLICABLE` handling and the 24-hour registry-match rule;
  multiple matching registry records collapse back to one change group.
- Moved actor/object company resolution after DDL grouping, reducing role-backed company
  UDF evaluation from every raw statement to once per displayed change group.

## 4.144.1 - Keep the app on the existing warehouse (2026-08-04)

- Removed the proposed `WH_OVERWATCH_APP` warehouse from V075, deployment configuration,
  grants, validation, teardown, rebuild instructions, and active application logic.
- Restored Streamlit and loader/task work to `WH_ALFA_ADMIN`; interactive performance
  attribution remains available through the existing `OVERWATCH` query tag and app telemetry.
- Kept the V075 security operating model unchanged apart from compute provisioning. V075
  remains owner-applied; no Snowflake statements were executed by the app or agent.

## 4.144.0 - Security operating model and responsive query execution (2026-08-04)

- Reframed Security around an exceptions-first six-domain posture, policy-owned identity,
  client and egress decisions, effective-access investigation, Trust Center movement,
  risk-ranked changes, and durable access-review campaigns with immutable decision history.
- Added owner-applied V075 with bounded login/change facts, Trust Center snapshots, a local
  security exception queue, and policy and review records.
- Added query-profile evidence links, Entity 360 and Action Center drills, coverage-aware
  confidence states, an export manifest that records failed evidence sheets, and explicit
  loading gates for expensive posture, stale-source, warehouse-setting, and review history.
- Bounded batch concurrency at four reads and added role/user/scope-aware per-member caching,
  so a changed panel reuses unchanged siblings without creating an unbounded query burst.
- Added a measured app-performance SLO, split interactive app cost from loader cost, and
  extended canaries, teardown, backup, backfill, validation, deployment, and rebuild surfaces.

Snowflake access remained read-only; V075 was authored but not applied.

## 4.143.1 - Snowflake button compatibility hotfix (2026-08-03)

- Removed the newer `icon_position` button argument from persistent status-bar actions.
  Snowflake's deployed Streamlit runtime does not accept it, which caused every page to fail
  before its content rendered; the default icon placement remains fully functional.
- Added an older-runtime behavioral regression test for the complete status-bar button path.

No Snowflake migration is required. Snowflake access remained read-only.

## 4.143.0 - Decision-readable operating surfaces (2026-08-03)

- Added exception-first summaries to Action Center, Control Room Pulse, task-run analysis,
  workload prioritization, and the SLO cockpit. Healthy scopes collapse to one explicit
  checked-clear row instead of competing with the decisions that need attention.
- Added a compact decision-row contract and adopted it for the action queue and workload
  portfolio, keeping impact and confidence numeric while making the decision, rationale,
  owner/status, and next step scan together.
- Promoted confidence to a shared evidence-quality treatment and documented each dense
  workbench's summary/evidence read contract. Task-run and Entity 360 evidence now load only
  through explicit, contract-labeled gates.

No Snowflake migration is required beyond owner-applied V074. Snowflake access remained read-only.

## 4.142.0 - Decision Studio (2026-08-03)

- Added one Control Room decision surface for six connected workflows: a measured workload
  portfolio, configurable SLO/error-budget cockpit, data-product economics, basis-aware Cost
  Truth, confidence-haircut scenarios, and optimization experiment follow-through.
- Ranked recurring query families by normalized measured impact, evidence confidence,
  reliability, and a users-plus-databases effort proxy. Rows drill into their persistent
  query-fingerprint Entity 360 profile.
- Kept economic grains explicit: product object-attributed and warehouse-metered costs remain
  separate non-additive columns; Cost Truth keeps billed, metered, measured, and allocated
  credits as separate lenses; scenarios de-duplicate estimates by entity and never mix in
  verified savings.
- Added operator-gated SLO creation and experiment result updates with SQL previews. SLO burn
  is calculated only for success-rate objectives; latency objectives report target status
  without inventing an event-level error budget.

No Snowflake migration is required beyond owner-applied V074. Snowflake access remained read-only.

## 4.141.0 - Investigation workbenches and task-run analysis (2026-08-03)

- Added universal investigation routing for query, alert, incident, action, warehouse,
  database, object, task, query-fingerprint, user, and role identities. Destination pages
  consume the exact identity without leaking it into global metric filters.
- Added task-DAG Run analyzer and Version compare views. Recent graph executions expose
  dispatch delay, failures, critical path, downstream impact, query-profile evidence, and
  task-level Entity 360 links; historical versions identify added, removed, and rewired nodes.
- Made large interactive DAGs searchable and click-focusable, with semantic detail levels
  while zooming and selected-run critical-path coloring. Added a Graphviz fallback for the
  run overlay as well as the topology.
- Expanded Entity 360 with basis-labeled mart metrics, wired repeat-query candidates into
  persistent fingerprint profiles, and upgraded the Control Room timeline to a brushable
  multi-lane operational replay.

No Snowflake migration is required beyond owner-applied V074. Snowflake access remained read-only.

## 4.140.0 - Operating workbench foundation (2026-08-03)

- Promoted the persistent action queue into Control Room's first section, with exact-row
  navigation, owner/status/due/defer controls, comments, confidence, and audited lifecycle
  transitions. The pre-V074 queue remains available as an explicit read-only fallback.
- Added typed evidence relationships, an entity ownership and service catalog, personal
  watchlists, optimization experiments, and SLO objective records around the existing
  telemetry and savings ledgers. Entity 360 connects those records without loading its
  heavier evidence history until requested.
- Added owner-applied V074 with additive operator tables and an idempotent atomic action
  lifecycle procedure. Updated migration canaries, teardown coverage, validation floors,
  rebuild backups, deployment docs, and regression locks for real non-empty queue shapes.

Snowflake access remained read-only; V074 was authored but not applied.

## 4.139.0 - Recommendation-engine guardrails (2026-08-03)

- Split measured idle from settings-verified auto-suspend opportunity. Already-tuned
  and unknown-setting warehouses remain visible, but no longer inflate actionable KPIs,
  contract steering, generated SQL, AI advice, or savings-ledger estimates.
- Added recurrence-normalized repeat-query and measured-pattern gates, active-day evidence
  for right-sizing, and stale-fact refusal for capacity forecasts. Low-evidence resizing and
  storage-growth projections are now review items rather than ranked actions.
- Hardened alert-threshold tuning against invalid metric values, selected off-hours schedules
  by recoverable value with sparse/full-day safeguards, and made retention remediation default
  to no change while requiring a verified strict reduction before SQL can execute.

No Snowflake migration is required. Snowflake access remained read-only.

## 4.138.0 - Calendar triage date ranges (2026-08-03)

- Added `Current month` and `Current year` to the global triage date filter. Both
  resolve from the account-time calendar boundary, not rolling 30/365-day aliases.
- Preserved calendar selections through saved views, cross-page navigation, Reset,
  and the legacy integer-day view format. Page headers and the security export manifest
  now disclose the selected calendar range.
- Added owner-applied V073 so `MART_EXEC_BOARD` materializes deduplicated MTD/YTD
  offsets alongside the seven rolling windows. Long live scans retain the existing
  90-day safety cap and disclose it in the filter strip.

Snowflake access remained read-only; V073 was authored but not applied.

## 4.137.0 - Query-profile investigation links (2026-08-03)

- Added Snowsight query-profile links to measured expensive queries, every statement in
  a priced procedure CALL tree, this-session query telemetry, and fleet slow-fetch rows.
- Added the exact slowest representative query to both Admin statement-family tables, so
  a performance hotspot can open directly into its Snowflake plan, scan, queue, and spill
  evidence. Aggregate links are explicitly labeled `Slowest profile`.
- Hardened the shared profile-link helper for null and cache-only IDs. Blank-only tables
  no longer render dead links or spend a metadata read resolving Snowsight URL context.

No Snowflake migration is required. Snowflake access remained read-only.

## 4.136.0 - Scope, pulse navigation, and duration consistency (2026-08-03)

- Removed the Environment control and made its retained saved-view field a fixed `ALL`
  compatibility value. Database inventory and validation are now company-only, so an old
  preference cannot leave an invisible PROD/NONPROD constraint behind.
- Replaced the global pulse's raw query-string links with native Streamlit actions routed
  through `request_navigation`. Open criticals, undelivered criticals, telemetry freshness,
  MTD spend, and the health-error fallback now open their owning section without bypassing
  profile or pre-widget navigation guards.
- Extended display-only duration inference across millisecond, second, minute, and hour
  columns, including embedded units such as `QUEUED_MIN_PER_DAY`, while explicitly avoiding
  false matches such as `MIN_CLUSTER_COUNT`. P95, queue, elapsed, delivery, freshness, and
  response-time cards now share `humanize_duration`; numeric sorting and raw CSV values stay
  unchanged.
- Filled out Control Room Pulse below its five KPI cards with query and failure trends from
  the 14-day activity frame already fetched for sparklines. The render verifies the actual
  `DAY / QUERIES / FAILS` shape before any column access and adds no Snowflake query.

No Snowflake migration is required. Snowflake access remained read-only.

## 4.135.0 - UI/UX wave 1: task graph workbench and metric hierarchy (2026-08-03)

- Rebuilt Operations task topology around one coherent current
  `ROOT_TASK_ID + GRAPH_VERSION` snapshot. The graph no longer mixes per-task latest
  versions or silently stops at 300 rows; incomplete, duplicate, dangling, and cyclic
  shapes are refused before rendering.
- Added a Tasks workbench with Health, Graph, and Runs views. Graph offers a root picker,
  topology KPIs, a dependency-free SVG renderer with pan, zoom, fit, full-screen, keyboard
  controls and SVG export, plus Graphviz and DOT fallbacks.
- Reorganized Overview into Company economics and Account risk & contract bands, followed
  by actions, drivers, and historical context. Section-level filter contracts now state
  which global dimensions apply, are panel-dependent, or are ignored across every major
  page.
- Consolidated duplicate shell health readouts into one clickable global pulse linking
  criticals, delivery, freshness, and spend to their owning views.
- Added semantic page/section headings, decorative-icon screen-reader hiding, wrapped and
  focus-visible segmented controls, clickable status-card focus states, and pinned the
  Streamlit-in-Snowflake runtime target to `1.52.2`.

No Snowflake migration is required for this release. Snowflake access remained read-only.
Coverage includes pure graph-shape tests, SQL/source-shape locks, renderer-control checks,
scope-contract assertions, and shell accessibility/runtime guards.

## 4.134.0 - Cost attribution, evidence, and executive presentation (2026-08-03)

- Added a measured cost-drill inventory plus lazy native detail for replication by database,
  Snowpark compute pools, the non-additive notebook subset, and paid Marketplace charges in
  native currency. Hybrid request credits are explicitly labeled historical.
- Added parent-aware stored-procedure execution trees, selective chart takeaways, larger help
  and kicker text, and one shared executive export model for projector/print HTML, slide
  bullets, and CSV on Overview and Morning Brief.
- Added a conservative warehouse-capacity forecast using complete days, robust trend,
  workload corroboration, recent-change suppression, and holdout-error gating. Weak or
  unstable evidence produces no ETA.
- Added V072 entity-aware incident proposals with exact change/failure evidence and confidence;
  declaration remains human-confirmed and links only matching entity members.

Migration note: the owner must apply
`snowflake/migrations/V072__entity_aware_incident_proposals.sql` in Snowsight. The app did not
execute Snowflake DDL or lifecycle writes.

## 4.133.0 — Readability wave 3: totals and MTD credits (2026-08-03)

- **rec33 — additive totals outside sortable tables.** `styled_table(totals=…)`
  renders a compact Σ caption above the table while leaving the dataframe numeric,
  sortable, selectable, and unchanged for CSV. Department chargeback now declares
  its total spend; warehouse attribution declares current- and prior-window totals.
  Percentage deltas are deliberately excluded because they are not additive.
- **rec28 — MTD dollars plus credits.** Every populated MTD-pace card now carries
  credits beneath the dollar headline. The value is derived from `mtd_usd / rate`,
  never from a dataframe credit column, so the fallback, no-prior-history, and paced
  paths share the same column-independent contract.
- **rec30 — task-failure sort declaration.** Operations → Tasks labels the table
  "by failed runs desc" only after `sort_values` has executed; frames without a
  `FAILED` column remain unlabeled and unsorted.

Coverage: behavioral totals-caption and MTD-card tests plus source-shape locks for
both cost call sites, the column-independent MTD formula, and sort-before-label order.
Gates green: ruff, mypy, **pytest 1869 passed / 1 skipped**.

## 4.132.1 — Hotfix: Overview KeyError 'CREDITS_TOTAL' (2026-08-03)

rec28 (v4.132.0) computed the window-spend card's credits from
`daily["CREDITS_TOTAL"]`, but the render-scope `daily` frame is only `[DAY, USD]`
(line 108 returns that projection; the board-panel path rebuilds it the same way) —
so on real, non-empty data the Overview page failed to render with
`KeyError: 'CREDITS_TOTAL'`. The AppTest render suite stubs run() to *empty* frames,
so it never hit the line. Fix: derive credits from `window_spend / rate` — exact
(the card's value IS credits × rate) and column-independent, so it works on every
path. Added a source regression guard. Gates green: ruff, mypy, **pytest 1863
passed / 1 skipped**.

## 4.132.0 — Readability wave 2: dollars + credits on the spend card (2026-08-03)

- **rec28 (partial) — dollars-primary, credits-secondary.** `metric_card_html` gains
  an optional `sub` slot (a muted secondary line under the value, reusing
  `.ow-card__meta` — zero new CSS; absent renders nothing). Wired on Overview's
  headline "Spend, last Nd" card so the dollar value now carries "12,300 cr" beneath
  it — reconciling against Snowsight's credit numbers no longer needs mental math.
  The MTD-pace card is deferred (its credits need a slice matching the pace window).
  +1 card test. Gates green: ruff, mypy, **pytest 1862 passed / 1 skipped**.

## 4.131.0 — Readability wave 2: active-filter count (2026-08-03)

- **rec25 — active-filter count on the collapsed scope strip.** The strip's border
  glow said *that* a filter was live but not *which* or *how many*. `_scope_is_active`
  is refactored into `_active_filter_count()` (the bool derives from it, so the glow
  and Reset are unchanged); the kicker now reads "Triage filters · 2 active" and the
  "More filters" expander label carries "(N active)" so a hidden `user_contains` can't
  silently shape every number on the page. Gates green: ruff, mypy, **pytest 1862
  passed / 1 skipped**.

## 4.130.0 — Readability wave 2: relative age (2026-08-03)

- **rec27 — "Age" companion column.** Triage timestamps (RAISED_AT) answer "when",
  not "how stale". New pure `formulas.humanize_age(value, now)` renders a compact
  age — "just now" / "5m ago" / "3h ago" / "2d ago" — added as an **Age** column
  next to RAISED_AT on the Alerts → Open events feed and the Control Room triage
  queue. Display-only: the real timestamp column stays for sort + tz-conversion, and
  `assign` preserves row order so the positional row-selection still maps back
  correctly. `now` is caller-supplied (`account_now()`, account-naive) so the
  formatter is pure and deterministic.
- **rec30 (partial) — declared sort.** `sort_label` now forwards through
  `selectable_table` / `selectable_nav_table`; the Alerts feed declares
  "severity, then newest" in its size caption.

New `tests/test_humanize_age.py` (buckets, future/skew, NaT/missing-now, string &
Timestamp inputs). The pinned Control Room triage-shape test was updated to the new
`_qdisp` (queue + Age) shape and strengthened. Gates green: ruff, mypy, **pytest
1862 passed / 1 skipped**.

## 4.129.1 — Readability wave 1 fix: large-frame duration unit (2026-08-03)

Adversarial-review fix on v4.129.0 (found before push). rec26 humanizes durations
only on the ≤400-row Styler path; on the large-frame (>400 row) Arrow path a
`NumberColumn` can't render "1h 30m", so the humanizer callable was skipped and the
value rendered raw — while rec31's header *still* dropped the unit token, leaving a
unitless bare number (reachable e.g. on Operations → Tasks `AVG_SEC` over 90–365d).
Fix: on the large-frame path a duration column now carries its unit in the value
("1800.0 s" / "850 ms") via `_duration_display_format`, so the header token-drop stays
correct on both paths. Also collapsed the sub-0.5ms zero glyph ("-0ms"/"0ms" → "0s").
The review separately **confirmed** CSV-keeps-raw, numeric-sort, and no-crash all hold.
Gates green: ruff, mypy, **pytest 1858 passed / 1 skipped**.

## 4.129.0 — Readability wave 1: humanize the numbers (2026-08-03)

Reading-the-numbers pass on the table display layer (`components.py` + one pure
formatter). All display-only — the underlying frame is untouched, so tables still
sort by real values and every CSV keeps raw numbers. From the 2026-08-03 review.

- **rec26 — humanized durations.** Raw duration columns read as arithmetic ("5,400"
  seconds). New `formulas.humanize_duration()` renders them compact — "1h 30m",
  "5m 12s", "45s", "850ms" — wired into `_auto_formats` as a **Styler callable** for
  `_MS`/`_SEC`/`_S` columns. Because it formats the *display* (not the data), the
  column still sorts by the real value and the CSV export keeps the raw number; the
  large-frame (>400 row) Arrow path renders raw-numeric, unchanged.
- **rec31 — unit-aware headers.** `_prettify_header` now maps a trailing unit token
  to a parenthesized display unit — "Spill Remote (GB)", "Hit (%)", "Stalest (h)" —
  and *drops* the token on humanized duration columns (the value carries the unit
  now), so "Elapsed S" becomes "Elapsed" over "1h 30m" values.
- **rec30 — declared sort order.** `styled_table(sort_label=…)` adds the ordering to
  the size caption — "142 rows · by $ desc · last 30d" — so a reader never has to
  infer whether a list is ranked or arbitrary.

Coverage: new `tests/test_humanize_duration.py` (7 cases across units/magnitudes/NaN/
sign); the pinned `_auto_formats`, `_prettify_header`, and printf-coverage tests were
updated to the new shape and strengthened. Gates green: ruff, mypy, **pytest 1856
passed / 1 skipped**.

## 4.128.0 — Cursor design review: deferred-items pickup (2026-08-02)

The remaining deferred design items, all app-only. Closes the Cursor 50-rec review: 43 shipped
(rec43 declined for correctness; rec3/6/8 skipped as owner-blocked/low-value).

- **rec13 — sidebar MTD in dollars.** The always-visible health strip now leads with a month-to-date
  dollar figure ("$X MTD · N cr"). **AI-aware:** the strip already carries the AI/OTHER credit split,
  so it prices Cortex/AI credits at the AI rate via `blended_billed_usd` exactly like the Brief (the
  C1 house rule) — not the flat compute rate — with a flat-rate fallback only when a stale cache
  predates the split. Rates come from Sidebar settings, never inlined.
- **rec7 — count badges on section pills.** `lazy_sections` takes an optional `counts` map and a
  `format_func` that renders "Label (N)" on both `st.segmented_control` and the `st.radio` fallback,
  while the option *value* stays the base label so section dispatch is unchanged. Wired on Alerts →
  Open events.
- **rec40 — click-through cost bars.** New `charts.clickable_bar_usd`: an altair bar with an
  `on_select` point selection; clicking a warehouse in Overview → Top cost drivers jumps to
  Operations → Warehouses pre-filtered to it. Guarded to fire once per new click (re-arms on an empty
  selection so a repeat drill-in isn't a dead click), **degrades to a plain bar** on runtimes without
  altair `on_select`, and is **gated to profiles that actually have Operations** — an executive (no
  Operations page) gets a plain bar instead of a dead click that would leak a cross-page scope filter.
- **rec5 — in-section jump strip.** `section_header` gained an optional `anchor`; new `section_toc`
  renders a compact "Jump to" chip row over the seven-panel Security → Access tab. Degrades to plain
  orientation labels where the runtime/iframe doesn't honor in-page anchors.
- **rec20 — one export affordance.** New `export_button` (⬇ glyph + `on_click="ignore"`) replaces the
  four ad-hoc page-level download buttons across Overview, Security (×2), and AI chargeback.

A 3-lens adversarial review (correctness / SiS-degrade / consistency) then verified each finding.
Six confirmed → four distinct defects, all **fixed before commit**: the rec40 executive scope-leak
(profile gate above), the rec40 dead-repeat-click (guard now re-arms on an empty selection), the
rec13 AI-rate violation (blended pricing above), and the untested `clickable_bar_usd` (new
`tests/test_rec40_clickable_bar.py` pins extraction / guard / degrade). Two findings refuted (rec5
anchor scroll is a benign degrade; the export row's widgets are uniform). `test_codex_r19` updated
(3→4 `on_click="ignore"`) and strengthened to pin the export migration.

Gates green: ruff, mypy (config-scoped pure layers), **pytest 1849 passed / 1 skipped**.

## 4.127.0 — Cursor design review Wave 5: write actions & feedback (2026-08-02)

Write-action ergonomics + loading/refresh feedback (the last implementation wave). App-only.

- **rec42 — one `confirm_gate()` component.** 11 hand-rolled type-to-confirm sites across Alerts,
  Operations, Control Room, Admin, and Optimization now go through one helper (renders the confirm
  input + action button, returns True only on click AND match). Object names (warehouse/table)
  match **case-insensitively** — typing `wh_alfa_admin` now satisfies a `WH_ALFA_ADMIN` gate (the
  exact bug the review named); action verbs stay exact-case. Each gate is keyed per-object/-event.
- **rec44 — `st.dialog` for the account-level emergency lever** (hasattr-gated, inline fallback);
  the SQL preview + confirm + audit move into a modal so "you are about to change prod" is isolated.
- **rec45 — typed Admin → Settings editors.** An explicit per-key type map drives
  `selectbox`/`date_input`/`number_input` (enum/date/number) — not `isinstance` (the score/gov/
  retention settings are string-typed numbers). Persistence is unchanged: values convert back to
  their stored string form before the upsert.
- **rec46** the AI-chargeback dept-budget shows its SQL before the button (SQL-always-shown-first);
  **rec47** Control Room + Spend wrap their cold first-paint reads in a collapsing `st.status`;
  **rec48** the Refresh button toasts and the sidebar note is relabelled "Session refreshed" (it
  tracks the session, not data freshness); **rec18** the alert drawer's action row is reflowed so
  the note input is full-width.
- **rec43 (st.form) NOT applied** — both candidate forms (the Operations SLA register and the Admin
  settings editor) carry a **live SQL preview / conditional reveal** that `st.form` would freeze
  until submit, breaking the see-the-SQL-before-you-run-it contract. Declined for correctness.
- **Wave 4 chart fixes folded in** (from Wave 4's review): the heat ramp no longer collides with
  the page background, the hour/waterfall takeaways are crash-proof and zero-total-guarded,
  `_share_note` omits a nonsensical >100% share, and month ticks carry the year.

Implemented as 5 parallel agents on disjoint files + a shared `confirm_gate` helper. A 3-lens
adversarial review caught the SLA-form preview-freeze (reverted), a lost per-event confirm key
(restored), an overstated loading label, an unguarded SQL preview, and two Admin edge cases (float
rounding + date bounds) — all fixed. **rec40 (chart click-through) remains deferred.**

Gates green: ruff, mypy, **pytest 1843 passed / 1 skipped**.

## 4.126.0 — Cursor design review Wave 4: charts (2026-08-02)

Chart-helper polish (all in `app/ui/charts.py`), app-only.

- **rec35 — takeaway lines.** `daily_stacked_usd`, `daily_stacked_count`, `events_by_day`,
  `hour_heatmap`, and `waterfall_usd` now emit one computed conclusion (top category + share,
  worst day, hottest cell) via an opt-out `takeaway=True`, applying the "lead with the conclusion"
  pattern where the data already is. Suppressed on the org-currency contract chart (would print `$`).
- **rec36 — honest empty states.** Chart helpers `st.caption("No plottable rows…")` on an early
  return instead of rendering blank space (house rule 8) — spend_trend and the five above.
- **rec37 — adaptive x-axis ticks.** New `_day_axis(day_series)` picks day/week/month ticks from
  the data span, so labels stay predictable at 180/365 (fixed `tickCount="day"` + `labelOverlap`
  dropped them unevenly). Applied to every daily chart.
- **rec38 — one heatmap ramp.** `hour_heatmap`'s one-off `scheme="orangered"` → a named
  `_HEATMAP_RANGE` (orange = intuitive "hotness"), shared with the theme's registered heatmap range.
- **rec39 — legend-placement rule.** New `_legend(wide=…)` encodes it once: wide multi-category
  stacks at the bottom, few-series comparisons at the top (not a blanket "always top").
- **rec41 — dollar-format parity.** `paired_bars` formats its axis + tooltip as `$` when the unit
  is dollars (was plain `,.2f`), matching every sibling; `waterfall_usd` was already correct.

**rec40 (chart click-through) deferred** — it's M-effort and needs caller wiring + a SiS-gated
`on_select` selection idiom; a focused follow-up. A caller audit confirmed the new takeaways don't
duplicate any existing conclusion caption.

Gates green: ruff, mypy, **pytest 1843 passed / 1 skipped**.

## 4.125.0 — Cursor design review Wave 3: component polish (2026-08-02)

Third wave — component consistency + trust. All app-only.

- **rec22 — one heading system.** Bold-markdown panel TITLES migrated to `section_header`
  (stripe + icon + severity badge) across Brief, Operations (17), and Security (15); body/data
  lines left as-is.
- **rec24 — segmented section pills.** `lazy_sections` renders `st.segmented_control` (the pill
  look was CSS-on-radio keyed off version-fragile aria-labels), with the radio kept as the
  `hasattr` degrade path and an `on_change` guard so single-select's deselect-to-None never leaves
  a page section-less. A valid section is now seeded before the widget so the bar is never blank.
- **rec25 — unified AI surfaces.** `ai_evaluation_panel` gained an optional `on_save` hook; the
  alert drawer's hand-rolled AI-explain is deleted and routes through it, so every AI answer now
  carries the same model+rate cost caption and grounding-prompt popover (save SQL + operator gate
  unchanged).
- **rec19 — collapsed the native-delivery SQL walls** into download-expanders with a public-trust
  caution (this panel renders repo SQL to every viewer — a secret pasted there would leak).
- **rec26 — themed the task-graph DAG** (transparent bg, status-palette fills red/gray/green, ink
  fonts) so it matches every other surface instead of light-theme pastels on the dark canvas.
- **rec28 — panel_help coverage.** Grounded "what is this / when red do X" popovers added to
  Overview, Admin, Spend, Unit costs, and Compare lead panels (they had none).
- **rec32 — registry-driven column help.** `metric_registry.COLUMN_HELP` + `_render_table` hangs
  a "which dollar is this?" help on ambiguous BILLED/MEASURED/ALLOCATED/USD headers.
- **rec33 — signed deltas.** Delta columns always show a leading `+`/`−` so direction survives
  red-green color-blindness (the delta color was the only cue).
- **rec34 — tz note once per page** (was above every converted table); **rec30** CSV button
  `⬇` → `⬇ CSV`; **rec27** one no-value glyph (em-dash) across cards, tables, sparklines, Brief.

Items 22/26 + brief polish, 25/19, and the panel_help sweep ran as three parallel agents on
disjoint files; the shared `components.py` cluster (24/27/30/32/33/34) was done inline. A 3-lens
adversarial review returned zero findings on the AI-panel/SQL-walls, one MEDIUM (segmented pills
showed no highlight on first paint — fixed by always seeding a valid section), and two minor
cosmetic notes (tz-note-in-fragment documented; a Brief placeholder left as-is). 3 tests pinning
exact code shape updated to the new shape.

Gates green: ruff, mypy, **pytest 1843 passed / 1 skipped**.

## 4.124.0 — Cursor design review Wave 2: navigation & shell (2026-08-02)

Second wave of the Cursor design review — nav/IA and shell chrome. All app-only.

- **rec1 — Control Room section pills.** The ~370-line single-scroll render is now four
  deep-linkable sections (Pulse · Incidents & triage · Timeline & movers · Freshness & replay)
  via `lazy_sections`, with `"Control Room": "cr_section"` added to `PAGE_SECTION_KEYS`. The live
  prefetch batch moved into its sole-consumer section, so off-screen sections stop paying for it.
- **rec2 — Optimize & Savings inner pills.** The ~13-panel divider wall in `optimize.py`
  `_optimization_tab` is now a nested second-level pill row (Idle & sizing · Queries & patterns ·
  Storage & waste · Remediation & ledger), each rendering/querying independently. Uses
  `lazy_sections(..., deep_link=False)` so the nested row does not clobber the page-level
  `?section=`; `cost.py` gates the savings ledger under the "Remediation & ledger" subgroup.
- **rec4 — Jump-to sections + central label map.** New `navigate.PAGE_SECTION_LABELS` is the
  source the Jump-to palette and deep-link resolver enumerate; the box now offers
  `Section · Page → Label` targets. `tests/test_section_labels.py` drift-guards the map against
  each page's `lazy_sections` call. Added a `deep_link` flag to `lazy_sections` (rec2's enabler).
- **rec9 — segmented Window control.** `st.select_slider` → `st.segmented_control` (all seven
  windows visible, one click), with the slider kept as the `hasattr` fallback and an `on_change`
  guard so single-select's deselect-to-None never empties the window filter.
- **rec12 — display prefs.** Popover renamed "Views & display"; **density now persists** to
  `USER_PREFS` (was session-only, reset every reload) via the same machinery as the timezone.
- **rec10 / rec14 / rec15 / rec16 — chrome.** Dropped the redundant "Scope" status-bar cell
  (it restated the filter toolbar directly above); `initial_sidebar_state="auto"` (collapses on
  phone, where Brief lives); the 🛰️ emoji favicon is now a rendered brand-mark PNG
  (`app/assets/favicon.png`, with an emoji fallback so a missing asset can't break load); the fake
  "More…" jump option is a real "Load all warehouses & alert rules" button.

Items 1 and 2 (the two big independent restructures) were implemented by parallel agents on
disjoint files; the coupled `main.py` shell items were done inline. A 3-lens adversarial review
(restructures / shell / nav-foundation) returned zero findings on the restructures and nav, and
one minor UX NIT on the Window deselect-restore target, which was fixed. Eight tests that pinned
the old shape (Scope cell, popover name, jump option, exact panel indentation) were updated to the
new shape — behavior verified preserved, none weakened.

Gates green: ruff, mypy, **pytest 1843 passed / 1 skipped**.

## 4.123.0 — Cursor design review Wave 1: the consistency spine (2026-08-02)

First wave of the Cursor Streamlit-design review (50 items adjudicated: 35 CONFIRMED /
15 PARTIAL / 0 refuted). Wave 1 is the shared-primitive backbone every later wave builds on —
all app-only, no migrations.

- **rec50 — one palette source.** New `app/ui/palette.py` is the single home for the semantic
  hues + chrome tokens; `main.py` (`_STRIP_COLORS`), `components.py` (`_SEV_HEX`, `_delta_html`,
  spark defaults), `charts.py` (`SEV_COLORS`/accents/category range), and `status_colors.py`
  (`delta_css`, `_MUTED`) now import from it instead of re-typing hex. `.streamlit/config.toml`
  is realigned to the tokens (`#0b1220→#0a0f1c` etc. — the one intended visual change, so native
  widget chrome matches the cards). `tests/test_palette_drift.py` fails CI if palette, the
  `--ow-*` theme tokens, or config.toml diverge, **and** if a consumer re-hardcodes a severity hue.
- **rec17 — height tokens.** New `app/ui/sizing.py` (`TABLE_H_*`/`CHART_H_*`); the scattered
  literals in charts/brief/overview/admin/blast-radius reference them, so a density change is one edit.
- **rec21 — eight raw `st.dataframe` → `styled_table`** (operations, control room, ai-chargeback,
  contract ×2, admin, spend, optimize): they now get status tinting, delta sign-coloring, tz
  conversion, prettified headers, and the CSV export they silently lacked.
- **rec23 — `empty_state(kind=…)`** codifies the empty-state vocabulary; fixed the real house-rule-8
  violation where Brief rendered "Alerting not installed yet" in **green** (now blue `needs_setup`).
- **rec29 — `selectable_nav_table()`** bakes in the sticky-selection guard (st.dataframe re-emits its
  selection every rerun); Overview top-actions and the Control-Room triage queue use it. The two
  Control-Room drill-down tables correctly stay `selectable_table` (they render, they don't navigate).
- **rec31 — tables declare their size.** `styled_table` prints `"{n} rows"` (+ `last Xd` when the
  caller passes a window) — but only for tables that **scroll** (>10 rows, where the total is hidden),
  with a `size_note=False` opt-out for the three callers that already print their own count.
- **rec49 — error walls truncated.** `guard()` leads with the first line and tucks the full
  (often hundreds-of-chars) Snowflake compile error into an "Error detail" expander.
- **rec11 — removed the duplicate ACCOUNT_USAGE lag note** from the sidebar (it lives once per page
  in `page_header`).

Adjudicated by an 8-agent verification workflow, then the implemented diff was run through a
3-lens adversarial review that caught the rec31 caption-duplication (now gated + opt-out) and the
residual rec50 hue literals (now migrated + a consumer-drift test added). Four source-grep tests
that pinned the old selection/palette shape were updated to the new shape (strengthened, not weakened).

Gates green: ruff, mypy, **pytest 1841 passed / 1 skipped**.

## 4.122.0 — Codex-R2 Wave 3: CI isolation + a validate contract with teeth (2026-08-02)

Wave 3 hardens the *deploy plumbing* — the CI smoke, the post-install contract, and a task-drift
check — so the migration chain that Waves 1–2 built can be applied and verified safely.

- **#1 CI isolation (`.github/workflows/ci.yml`).** The optional Snowflake smoke cloned the prod
  DB but then applied SQL that is **fully-qualified as `DBA_MAINT_DB.OVERWATCH.*`** — and 16
  migrations even issue `USE DATABASE DBA_MAINT_DB`, which overrides `--database`. Applied
  verbatim it would have redefined prod procs, disabled routes, and run loaders against
  production regardless of the clone. Fix: a **"Rewrite SQL onto the clone"** step copies every
  `.sql` the smoke runs into a temp dir and rewrites the identifier `DBA_MAINT_DB → $CLONE_DB`
  (schema `OVERWATCH` and `SNOWFLAKE.ACCOUNT_USAGE` preserved), followed by a **fail-closed
  guard** that greps the copies and aborts *before applying* if any `DBA_MAINT_DB` reference
  survives. Layer 2 (defense-in-depth) is a dedicated no-write `SNOWFLAKE_CI_ROLE` the workflow
  now selects — **owner must create it** (contract documented inline). Job stays opt-in +
  `continue-on-error` until both hold; the gating-flip steps are documented, not flipped.
- **#49 validate contract with teeth (`snowflake/validate.sql`).** Alongside the human-readable
  per-check rows, a bottom `EXECUTE IMMEDIATE` block re-checks each load-bearing invariant and
  **RAISEs a specific exception** (`-20011..-20018`) on the first failure, so `snow sql -f
  validate.sql` exits nonzero and CI can go red: migration floor V001..V071, credit rate
  positive + `= 3.68` (unless a deliberate `CREDIT_PRICE_OVERRIDE` flag), daily-fact freshness
  (3-day dead-man), no enabled route with a blank integration, `ALERT_CONFIG >= 29` rules
  (verified: 30 seeded − 1 retired = 29 exactly), and the 4 key procs present.
  - **Deploy-order fix:** validate.sql runs at the documented step *right after migrations,
    before the first load*, where the daily facts are legitimately **empty**. The freshness
    assertions now treat an empty fact as **PASS** (nothing loaded yet) and only bite on a
    *populated-but-stale* fact (a loader that ran and then stopped — the real dead-man). Without
    this, every fresh deploy would have aborted on `-20014/-20015`.
- **#48 task-drift check (`snowflake/task_audit.sql`).** Because tasks are created with
  `CREATE TASK IF NOT EXISTS`, a later change to a task's schedule/warehouse/predecessor leaves
  the live task on its OLD definition silently. This diffs live `SHOW TASKS` against an
  operator-maintained expected set encoding the **post-V071 topology**, with an **exact** single-
  parent predecessor match (replacing a `%LIKE%` that missed extra parents and false-matched
  `TASK_ALERT_SCAN` vs `TASK_ALERT_SCAN_DAILY`). Read-only; prints one verdict per task.
- `tests/test_migrations_parse.py` now also parses `validate.sql` + `task_audit.sql` (each
  contributes one sqlglot-verified statement). Rebuild bundle regenerated (`05_validate.sql`).

**Owner queue (unchanged + new):** create the no-write `SNOWFLAKE_CI_ROLE`
(`OVERWATCH_CI_SMOKE`); reconcile `task_audit.sql`'s expected set to the real deployment before
trusting its OK rows. Gates green: ruff, mypy, **pytest 1838 passed / 1 skipped**.

## 4.121.0 — Codex-R2 Wave 2b: V071 task-graph re-chaining (fixes the sibling races) (2026-08-01)

Theme A part 2 — the forward migration that fixes the task-graph topology defect (Codex
#3/#4/#43/#42). Snowflake runs sibling child tasks in **parallel**, and the DAG had grown by
hanging readers off the root loaders instead of behind the extract/reconcile that feed them, so
readers raced their own data (mixed-generation dashboards, up to 1h alert latency).

- **#3 (hourly):** `TASK_REFRESH_EXEC_BOARD` and `TASK_ALERT_SCAN` are re-pointed from
  `AFTER TASK_LOAD_HOURLY` to `AFTER TASK_QH_EXTRACT`, so they read the query facts the extract
  refreshes (V041 had moved the mart/diag loaders but missed these two). `TASK_ALERT_NOTIFY`
  rides along with its parent.
- **#4 (daily):** `TASK_LOAD_MARTS_V27_DAILY`, `TASK_PLATFORM_SCORE_DAILY`, and
  `TASK_ALERT_SCAN_DAILY` are re-pointed from `AFTER TASK_LOAD_DAILY` to
  `AFTER TASK_NIGHTLY_RECONCILE`, so they read reconciled facts instead of racing the reconcile's
  delete+reload.
- **#43:** `TASK_AUTO_RETRY_ATTEMPTS = 1` + `SUSPEND_TASK_AFTER_NUM_FAILURES = 10` on both roots.
- **#42:** the `SCHEMA_VERSION.DESCRIPTION` widen runs at the top of V071 unconditionally, so any
  install advancing the chain gets it (closes the manual-preflight gap).

Done via `ALTER TASK ADD/REMOVE AFTER` (no `CREATE OR REPLACE TASK` — every task body/schedule/
warehouse is preserved), inside a suspend → re-point → `SYSTEM$TASK_DEPENDENTS_ENABLE` window.

**Adversarial review caught a HIGH defect in the first cut and it was fixed before commit:** the
original idempotency wrappers swallowed *all* errors, so a first run interrupted by an in-flight
task instance (`SUSPEND` doesn't stop an already-running one) could `REMOVE` a predecessor, fail
the `ADD`, and leave a task with **no predecessor and no schedule — a silent alert/exec-board
outage recorded as a green migration.** Rebuilt with a **state check**: each re-point snapshots
the task's predecessors from `SHOW TASKS`, issues `ADD` (before `REMOVE`) only when the new
predecessor is absent and `REMOVE` only when the old one is present, with **no exception handler**
— so any genuine `ALTER` failure aborts loudly *before* `VERSION 71`, leaving the graph suspended
(a loud, self-healing outage) rather than silently orphaned. Idempotent, re-runnable.

**⚠ Owner smoke test required** (re-points live tasks; a byte-compare can't prove runtime): after
apply, `SHOW TASKS` and confirm the new predecessors and that all tasks are `started`. Deferred:
#5 finalizer (green-on-failure observability) and #49 validate DAG-assertion — both to a follow-up.

Gates green: ruff, mypy, **pytest 1836 passed / 1 skipped**. Rebuild bundle → `V001_V071`.
**Apply order: V062 → … → V070 → V071.**

## 4.120.0 — Codex-R2 Wave 2a: reconcile/webhook/loader robustness (before-apply) (2026-08-01)

Theme A part 1 — the before-apply generator edits to the built-not-applied V064/V066, all
adversarially reviewed CLEAN:

- **V064 #9 — lease can't wedge delivery.** `SP_NOTIFY_WEBHOOK` gained an outer
  `EXCEPTION WHEN OTHER` that does the same `HOLDER = CURRENT_SESSION()`-fenced lease release,
  logs `webhook_run_failed`, and re-raises — so a mid-proc error fails the task loudly instead
  of leaving `HELD=TRUE` and wedging delivery until the 1h stale-reclaim.
- **V064 #6 — reconcile stops blaming unrelated failures.** `APP_ERROR_LOG` has no run-id column,
  so the reconcile's failure count is now narrowed to `PAGE IN ('ExtractLoader','DailyFacts',
  'MartLoader')` — exactly the three pages its swallowing children write. The other two children
  don't swallow (a failure there aborts the proc directly), so no swallowed failure escapes the
  net; the only residual is a concurrent hourly-extract failure in the same window, which
  over-reports (safe direction) and points at a real log row.
- **V064 #10 (slice) — undelivered criticals get 7 days.** The send-eligibility floor is now
  `CRITICAL → 7d, else 24h`, so an undelivered CRITICAL older than 24h can still be sent (oldest-
  first drain, per-route dedup, and capture-once all intact). The full dead-letter/replay machine
  stays deferred.
- **V064 #7 — reconcile atomicity DEFERRED (correctly).** A reconcile-owned transaction can't wrap
  the delete+reload: the child loaders own their own `BEGIN TRANSACTION`/DDL, which would commit
  it early and break the per-source watermark rewind. Documented with a `TODO`; the #9 verdict now
  makes a mid-reconcile child failure loud so a gap can't pass silently.
- **V066 #37 — invalid SCOPE fails loudly.** `SP_LOAD_MARTS_V27` raises a declared exception when
  `SCOPE NOT IN ('HOURLY','DAILY')` instead of silently loading nothing and returning "MARTS OK".
- **V066 #23 — AI freshness no longer green on a half-load.** The per-source freshness stamp for
  `FACT_AI_USAGE_DAILY` now uses `HAVING COUNT(*) = COUNT_IF(loaded)`, so it stamps fresh only when
  **both** AI arms (`ai_code` + `ai_functions`) succeeded — a single-arm success leaves the prior
  generation standing.

Gates green: ruff, mypy, **pytest 1824 passed / 1 skipped**. Rebuild bundle regenerated. (V071 —
the live-task re-chaining that pairs with this — follows.)

## 4.119.0 — Codex-R2 Wave 1: delivery/telemetry/cost app fixes + V070 digest atomicity (2026-08-01)

A second 50-item review (41 confirmed) — much of it the bill for last round's optimistic ships.
Wave 1 clears the app-code + digest items; the task-graph and CI themes follow.

- **Delivery card (#32/#14/#15):** the banner no longer reads "Delivery LIVE" when there's no
  enabled route (or when the integration `SHOW` failed — that now says "unable to verify");
  "stuck" keys on the **oldest eligible event's** age, not the last send's recency (so a fresh
  event after a quiet week isn't false-stuck and an old backlog isn't masked by a recent send);
  and expired-undelivered events + each route's latest failure survive the 24h cutoff, so a
  stranded route can no longer read "Quiet".
- **Telemetry (#27/#28/#29/#30):** the write-shape flag is set **only after** the column list
  resolves (a transient describe failure no longer pins the wrong shape and drops telemetry all
  session); failures get a reserved persist budget ahead of the 60-row cap (with a dropped-rows
  counter); batch members carry `sql_hash` + inferred `cache_hit`; and the last unweighted Admin
  cache-hit metric now weights by `1/SAMPLE_PROB` like the fleet query.
- **Cost/coverage (#17/#18/#19/#20/#16/#34):** the allocation coverage gate requires the exact
  lower bound **and** a distinct-day count (a ~14%-incomplete week no longer passes); live shares
  and their dollar pool use one explicit calendar window; storage freshness reads a loader
  watermark + active-database inventory (a dropped DB no longer forces live fallback forever);
  prior-month storage preserves lifecycle zeros (a 1-day DB isn't inflated ~30× to a full month);
  Compare returns a per-side coverage contract so a partial backfill can't show false 100% moves.
- **Forecast/contract (#24/#35/#36):** the forecast adds today's remaining spend and discloses its
  compute-rate basis; contract burn/day divides balance deltas by elapsed calendar days (a 3-day
  gap is no longer one huge daily burn); a sole partial-today row now yields "insufficient
  complete history" instead of a projection.
- **Security docs (#22/#31):** corrected comments that overstated the safety net — under
  owner's-rights SiS, `OPERATOR_USERS` is the **sole** authorization boundary (Snowflake RBAC is
  not a server-side backstop), and the per-tier statement timeouts collapse to the warehouse's
  300s (the owner must set `STATEMENT_TIMEOUT_IN_SECONDS`). No gating logic changed.
- **V070 digest (#39/#11/#12, before apply):** `SP_DAILY_DIGEST`'s DELETE+INSERT is wrapped in a
  transaction (a crash can no longer blank today's digest); a new additive
  `ALERT_ROUTES.DELIVER_DIGEST` flag stops the digest broadcasting to every route (future
  PagerDuty/tactical routes won't get exec prose); and an all-failed digest is now loud
  (`digest_undelivered` + `[UNDELIVERED]`) instead of a false success. Adversarially reviewed CLEAN.
- **#44:** the alert-rule consistency test's allowlist wrongly marked `PIPE_TASK_FAILURES` retired
  — it's live (raised by `SP_ALERT_SCAN_DAILY`, enabled). Removed; that's twice this session I
  reached "retired" by checking only one scan proc, which is exactly what the test now guards.

Gates green: ruff, mypy, **pytest 1818 passed / 1 skipped**.

## 4.118.2 — CORRECTION: PIPE_VOLUME_DROP is LIVE + alert-rule consistency test (2026-08-01)

**v4.118.1 was wrong** and this reverses it. That entry claimed the PIPE_VOLUME_DROP alert was
"retired (scan arm removed at V043) … pages nothing." It is not retired — it **fires**: the
literal only ever lived in `SP_ANOMALY_SWEEP` (not `SP_ALERT_SCAN`), whose latest definition
(V023) still `INSERT`s a HIGH, PROD-scoped, >50%-drop `PIPE_VOLUME_DROP` event into
`ALERT_EVENTS`, gated on `ALERT_CONFIG.ENABLED`; `TASK_ANOMALY_SWEEP` (06:40 CT daily) is
resumed and never suspended, and the config row is never disabled by a migration. The v4.118.1
investigation searched `SP_ALERT_SCAN`, found it absent there, and wrongly concluded "nothing
raises it" — describing a live HIGH alert as dead, which could lead an operator to dismiss a
real PROD volume collapse.

- `operations.py` / `ops_sql.py`: the "Volume drops" copy now states the truth — the alert
  fires past a 50% PROD drop via `SP_ANOMALY_SWEEP`/`TASK_ANOMALY_SWEEP`, is gated on
  `ALERT_CONFIG.ENABLED` (so it can be turned off), and the panel is the account-wide
  informational view (non-PROD + the 30-50% WATCH band don't page). The truncate-and-reload
  caveat is kept, with the correct remedy: **disable the rule in `ALERT_CONFIG`** for a
  truncate-reload account (the owner `UPDATE … SET ENABLED=FALSE` genuinely works — the arm
  honors the flag).
- **New guard `tests/test_alert_rule_consistency.py`** — cross-references three static sets:
  rule ids the latest alert-raiser procs (`SP_ALERT_SCAN`, `SP_ALERT_SCAN_DAILY`,
  `SP_ANOMALY_SWEEP`, …) actually raise; rule ids enabled in `ALERT_CONFIG`; and rule ids the
  app presents as LIVE vs RETIRED. Guard A: a "fires" claim must be backed by a raiser. Guard B:
  a "retired"/"pages nothing" claim must not name a still-raised, still-enabled rule (this fails
  on the exact v4.118.1 text — verified). Guard C: no ENABLED config row without a raiser. Plus
  a non-vacuous self-check so the classifier can't silently stop matching. Neither the migration
  byte-compare tests nor sqlglot could catch this class; this closes the gap.

## 4.118.1 — Volume-drop alert: correct the stale "this alert fires" claim (2026-08-01)  [SUPERSEDED — see 4.118.2; its claim that the alert was retired is WRONG]

Owner asked to remove the volume-down alert (they truncate-and-reload nightly, which the rule
reads as a collapse). Investigation found the `PIPE_VOLUME_DROP` alert's scan arm was **removed
from `SP_ALERT_SCAN` back at V043** (during the PIPE_TASK_FAILURES retirement) and no version
since — including the live V061 scan and the pending V062–V070 — raises it. So the rule was
already dormant; what lingered was old open events plus **stale app copy** claiming the alert
still fires. The Operations "Volume drops" panel and its builder now state plainly that the
alert was retired, that the panel is informational (pages nothing), and that truncate-and-reload
tables will always show a large "drop" here by design. (Owner-side: disable the dormant
`ALERT_CONFIG` row and resolve any old open `PIPE_VOLUME_DROP` events — SQL in the handoff.)

## 4.118.0 — Codex-R Waves 4 & 3: V070 delivery routing + deploy hardening (2026-07-31)

**V070__delivery_routing_teams_only.sql** — a forward migration (V012/V018 are already applied)
fixing the Teams-only delivery defects, adversarially reviewed CLEAN:
- **#23** `SP_DAILY_DIGEST` was hardcoded to the retired Slack integration and swallowed the send
  error, so on a Teams-only account the morning digest had **never** been delivered while the
  proc reported "attempted". Re-derived to walk the enabled `ALERT_ROUTES` and send through each
  route's integration, ledgering per-route failures to `APP_ERROR_LOG('digest_send_failed')`; the
  in-app digest write still happens even if every send fails; returns `digest written; sent N/M`.
- **#25** the V012-seeded dead Slack default route (which failed every sender cycle and buried
  real errors) is disabled idempotently — any route whose integration isn't in
  `SHOW NOTIFICATION INTEGRATIONS` is set `ENABLED=FALSE`.
- **#24** `TASK_ALERT_NOTIFY` auto-resume now keys on *any* enabled route naming a live
  integration (evaluated after #25), not the literal Slack integration name.

**Deploy hardening (Wave 3):**
- **#2 (P0)** `webhook_delivery.sql` is no longer self-destructive: a placeholder guard aborts a
  copy-paste run before `CREATE OR REPLACE` can overwrite the live Teams integration and drop its
  grants, and the trailing `ALTER TASK RESUME` only fires when the integration actually exists.
- **#4/#5** `roles.sql` gains explicit Streamlit `USAGE` re-grants (which `CREATE OR REPLACE
  STREAMLIT` wipes, no `COPY GRANTS`) with a hard-fail grantee check, and future-grant protection
  + a validation query so recreating an audit table can't silently reopen `UPDATE`/`DELETE`.
- **#38** the committed-secret guard now enumerates the whole tracked tree via `git ls-files`
  (root files, `.github/workflows`, `tests/` were previously unscanned) with a `gitleaks`
  history-scan TODO for CI.
- **#32** an opt-in, secrets-gated, non-blocking Snowflake integration-smoke CI job (zero-copy
  clone → migrations → validate → loader → route-ledger → teardown); floor-compat CI now installs
  `sqlglot` so collection can't fail.
- **#6** new `snowflake/task_audit.sql` diffs `SHOW TASKS` (state/warehouse/schedule/predecessor)
  against an expected set, catching a stale `CREATE TASK IF NOT EXISTS` at deploy.
- **#45** `app/core/query.py` onboarded to mypy (3 errors → a minimal, documented per-module
  override for two false-positives).

Gates green: ruff, mypy, **pytest 1807 passed / 1 skipped**. Rebuild bundle `V001_V070`. **Owner
apply order is now V062 → … → V070.** This completes the Codex-R implementation (44 confirmed:
Wave 1 generators, Wave 2 app, Wave 3 deploy, Wave 4 V070). Deferred by design: #8 staging-swap
reconcile, #28 dead-letter, #18 dual-company, #15 estimator. **Owner-only: #1 rotate the webhook.**

## 4.117.0 — Codex-R Wave 2: app-code batch (delivery, telemetry, scope, coverage, auth) (2026-07-31)

The 24 confirmed app-code findings, four clusters:

- **Delivery card, per-route (#29/#41/#42):** `last_delivery_health()` returns one row **per
  enabled route** — a healthy route no longer masks a dead sibling, and one dead route no
  longer reddens the whole card. `FAILING_NOW` compares latest-failure vs latest-success (a
  recovered endpoint clears instead of staying red 24h); a never-sent route with an eligible
  backlog reads BROKEN immediately instead of "queued for next run". The card aggregates the
  worst route state and names the offender.
- **Telemetry (#30/#43/#44):** the async write-shape is now resolved **deterministically** (a
  synchronous observed probe, cached per session) so the 12-col INSERT can't silently fail
  against the live 10-col table and drop rows; popped rows are re-queued on a failed flush.
  Batch members now carry each async job's `query_id` for a real `QUERY_HISTORY` correction.
  Cache-hit% is weighted by `1/SAMPLE_PROB` to undo the tail-sample skew, and the false
  "tail-complete" docstring is corrected.
- **Scope threading (#19/#33/#34/#35/#36/#48):** object-cost panels, lock contention, unload
  activity, and query-tag governance now honor the active Database/Schema filter (or route to
  the db/schema-predicated live path when the mart can't scope, and say so). Top-object labels
  are derived deterministically (`COMPANY_FOR_DATABASE`) instead of nondeterministic
  `ANY_VALUE`.
- **Coverage gates (#14/#16/#20/#21):** allocation coverage is computed from the **scoped**
  frame (one company's old data no longer makes another's window look covered); a shared
  `coverage_contract()` helper lands in components for `run_mart_first` (opt-in, no behavior
  change for existing callers); storage freshness/coverage take the **stalest/least-covered**
  database instead of the rosiest peer; prior-month storage gets a completeness guard, live
  fallback, and observed-day divisor so MoM stops inflating.
- **Auth & labeling (#3/#50/#37/#22/#49):** operator gating resolves the **viewer** identity
  against an allowlist (`session.is_operator()`) instead of `CURRENT_ROLE()` — which is the app
  owner's role for every viewer under owner's-rights SiS — wired across all 7 call sites; the
  retro score drops its leading partial month; the Control Room anomaly caption stops claiming
  parity with the configurable server threshold; the session timezone is set to
  America/Chicago (aligns off-SiS runs; the reviewer's "forced to UTC" mechanism was a no-op in
  SiS); and the Environment/Compare help no longer promises an env-vs-env lens that isn't built.

Deferred (disclosed already / L-effort follow-up): #15 estimator unification.
Gates green: ruff, mypy (pure layers), **pytest 1797 passed / 1 skipped**.

## 4.116.0 — Codex-R Wave 1: fix-before-apply migration generators (V064/V066/V067/V069) (2026-07-31)

A 50-item external review was adjudicated against the real code (44 confirmed, 6 partial, 0
declined). Wave 1 fixes the defects in migrations that are **built but not yet applied** — done
in the generators, so they are corrected before the owner's Snowsight apply rather than via a
forward migration. Every fix went through adversarial review, which caught two more defects in
the first V064 pass (below).

- **V064 — #27 per-route expiry:** the expiry watchdog keyed on `ALERT_EVENTS.NOTIFIED_AT`
  (stamped after the *first* route delivers), so an event that reached route A but never route B
  was never flagged expired for B. Now evaluated per (event,route) via `NOT EXISTS` a delivery
  for *that* route, on eligibility byte-identical to the send path.
- **V064 — #40 expired-log inflation:** the aggregate `undelivered_expired` row was re-inserted
  every sender run while a backlog persisted (a 30d KPI counting runs, not events). Now one row
  per (event,route) episode, gated on no prior same-key row in 24h.
- **V064 — #9 reconcile verdict *(review-corrected)*:** `SP_NIGHTLY_RECONCILE` ignored child
  loader outcomes and always returned success. The first fix parsed child return strings — but
  adversarial review found 4 of 5 children swallow arm failures and return benign "loaded"
  strings, so it detected only 1 of 5. Corrected to count `APP_ERROR_LOG` `%_failed%` rows
  written *during the run* (the signal every child emits), with the return-string check as a
  secondary catch. Emits `RECONCILE OK` / `RECONCILE WITH ERRORS: n`.
- **V064 — #26 single-flight lease *(review-corrected)*:** send-before-ledger is kept (the
  correct at-least-once bias for paging — a claim-before-send outbox would risk a *lost* page),
  guarded by a new `OW_SENDER_LEASE` sentinel so two runs can't overlap. Review caught that the
  release was unconditional, letting a >1h-stalled run clear a *successor's* reclaimed lease →
  concurrent senders; the release is now fenced with `HOLDER = CURRENT_SESSION()`.
- **V066 — #10 false success:** `SP_LOAD_MARTS_V27` swallowed per-arm failures but always
  reported loaded. Now tracks required vs optional arm failures and returns
  `MARTS OK` / `MARTS WITH ERRORS: n required, m optional`.
- **V066 — #11 freshness on failure:** the per-scope freshness MERGE stamped every source in a
  group fresh, including a source whose arm just failed. Now stamps only sources whose arm
  actually succeeded this run (token-gated), so a failed source keeps its prior generation.
- **V067 — #17 residual company:** unattributed object compute was hardcoded to
  `COMPANY='UNKNOWN'`, so company-filtered object totals couldn't reconcile. Now resolves the
  executing warehouse's company via `COMPANY_FOR_WAREHOUSE`, `UNKNOWN` only as fallback.
- **V069 — #12/#13 driver reconciliation *(this session's own bug)*:** v4.114's exec-board
  work injected serverless/AI rows into the warehouse `COST_DRIVER` panel, breaking
  reconciliation with the warehouse headline KPIs and poisoning the "% of warehouse compute
  spend" denominator. Serverless/AI now emit on a distinct `COST_DRIVER_SVC` panel, rendered as
  its own table in `overview.py` with a billed-$ basis label; the warehouse panel is
  warehouse-only again and reconciles.

Also: **#31** (the floor-compat CI job would fail at collection because a new test imported
`sqlglot` unconditionally — now `importorskip`) and the stray unused `_ACCEPTED_EXPOSURE_FILES`
in the secret guard, both cleaned. Deferred to a follow-up (L-effort redesigns): #8 staging-swap
reconcile, #28 dead-letter retry, #18 dual-company object cost.

Gates green: ruff, mypy (pure layers), **pytest 1791 passed / 1 skipped**. Rebuild bundle
`V001_V069` + teardown updated for `OW_SENDER_LEASE`. **Owner: hold the Snowsight apply of
V062–V069 until this is pulled — these generator fixes must precede the apply.**

## 4.115.1 — Scrub the committed webhook credential + CI guard; rotation/route runbooks (2026-07-31)

`snowflake/webhook_delivery.sql` carried a **real Power Automate trigger URL — including its
`sig=` bearer token** — tracked in git and pushed to GitHub from 2026-07-08 to 2026-07-31.
Anyone with read access to the repo could post into the Teams channel. The file's own header
had always said the secret "cannot ship in git"; nothing enforced it.

- **Scrubbed** to placeholders, with the split explained (`WEBHOOK_URL` prefix vs
  `SECRET_STRING` suffix) and a note that current Power Automate URLs carry a
  `/direct/cu/NN/workflows/` cluster segment the prefix must keep, or every send 404s.
- **`tests/test_no_committed_secrets.py`** fails the build if a credential *shape* returns —
  Power Automate `sig=`, Slack incoming-webhook, AWS key id, private-key block, GitHub PAT.
  It reports the shape and location, never the value, and keeps a deliberately short
  placeholder allowlist (every entry is a hole in the check).
- **Two owner runbooks in the file:** the rotation sequence — including the step people miss,
  that `CREATE OR REPLACE` on an integration **destroys its grants**, and `SP_NOTIFY_WEBHOOK`
  runs `EXECUTE AS OWNER`, so a missing `USAGE` silences Teams with only `route_send_failed`
  rows to show for it — and the one-statement retirement of the dead V012 Slack route.

**Rotation itself remains the owner's to perform**: it needs the Power Automate UI plus a
credential write in Snowflake. Deleting the line here does not undo the exposure — the value
is in git history — so invalidating the old URL at the provider is the only real remedy.

## 4.115.0 — "Last alert delivery" card + the integration probe that showed a false red (2026-07-31)

From a live incident: the owner saw no Teams alerts for a day and **had no way to tell a
healthy quiet stretch from a dead pipe**. The app owned both facts and surfaced neither
usefully. Two fixes, one principle — *silence is not a fault signal*.

- **New card on Alerts: Last alert delivery / Eligible to send now / Send failures (24h).**
  The sender is a 24h-windowed, per-key digest — alert scanning dedupes on keys that mostly
  carry the day, so a chronic condition raises **once** and quiet days are legitimately
  empty. Silence therefore proves nothing on its own. What separates quiet from broken is
  whether anything is **waiting**, so `mart_sql.last_delivery_health()` reports the last
  confirmed send (from `ALERT_DELIVERIES` — a send Snowflake accepted, not a raise) beside a
  count that **mirrors the sender's own eligibility predicate** (open, inside the 24h window,
  config-joined, matching an enabled route's family/company/severity, absent from that
  route's ledger). The card states the verdict outright: *nothing waiting* → "no news really
  is no news"; *waiting but nothing sent* → "delivery looks stuck"; *send failures present* →
  "the endpoint is refusing — check the integration, not the alert rules".
- **Fixed a banner that lied.** `_delivery_status` probed a hardcoded
  `LIKE 'OVERWATCH_WEBHOOK'` — the **Slack placeholder** from `webhook_delivery.sql`. On a
  Teams-only account that integration never exists, so the page rendered a red *"No webhook
  integration — alerts stay in-app only"* while Teams was delivering normally. It now
  resolves the integrations the **enabled routes actually name** and checks for any of them.
  As a bonus it now calls out routes pointing at a missing integration — which fail on every
  run and bury real errors in the log (exactly the dead V012 Slack route on this account).

Gates green: ruff, mypy (pure layers), **pytest 1781 passed / 1 skipped** (+7 locks).

## 4.114.0 — Perf round 3 (live telemetry) + recommendation engines Tiers B–E + V069 (2026-07-31)

The full plan from the owner's live Admin→Performance screenshots and the 14-engine
recommendation audit, implemented across five disjoint file groups plus one owner migration.

### The pain board was measuring itself wrong
`query.py` gave **every batch member the whole batch's wall time**, so `batch:q`/`batch:p`
reported identical inflated P50/P95 and Cost & Contract's "25× worse" pain was partly an
artifact of one batch counted twice. Members are now timed individually (via a ContextVar —
the durations can't ride the return value, which is `st.cache_data`-cached and would replay a
miss's timings on a hit), and the batch wall time is logged once as `batch_wall:{tier}`. The
Admin board now ranks on **`EST_WAIT_S`** (sample-weighted total wait, excluding the
`batch_wall:` superset rows) instead of exception-weighted p95 × slow-count — which also
removes the sub-2s blind spot.

### Perf
- **AI-users panel is fact-first** — `FACT_AI_USAGE_DAILY` already existed at exactly the
  right grain (V061 arm [9]); the panel was scanning live regardless. Two new mart readers
  with column-for-column matching contracts, a coverage gate so a young fact can't silently
  under-report a 180/365d window, and — since the Cortex scans are **window-flat** (cost is
  secure-view + subscription-probe overhead, not rows) — the live fallback is now **one 365d
  superset fetch** sliced in pandas instead of two. Kills the two heaviest slow-key families.
- **`health_strip`** — 8 UNION arms → 3 single-pass CTEs unpivoted via `LATERAL FLATTEN`
  (not 8 `FROM strip` arms: Snowflake doesn't guarantee CTE materialization, which would
  have silently undone the fix), each source scanned exactly once, behind a 120s TTL. It
  runs on the shell of every page.
- **user_directory** 24h cache · **unit-costs** ≤30d window cap · **`cs_types`** mart-first ·
  **t_rca** tier honest to a ~45-min source lag · **ACCOUNT_USAGE probe** hits a metadata view
  instead of scanning `QUERY_HISTORY` · **app statement stats** served from the app's own
  telemetry with the history scan behind a toggle.

### Recommendation engines
- **C1 (systemic)** — live builders clamp to 90d while the picker goes to 365, and four
  engines divided by the *requested* window: idle, sizing, quiet-window and what-if all read
  **~4× low** on a 365d view. Fixed once via `components.served_days(result, days)`, now the
  documented divisor rule for the whole file.
- **B1/B3/B4** — quiet-window savings monetized metered days × 30 *calendar* days (~3.5×
  over); the idle ledger booked 100% of idle as recoverable when Snowflake's 60s resume
  minimum makes that unattainable (now books recoverable idle, resume-tail deducted); the
  retention-fix estimate counted failsafe that drains in 7d regardless.
- **C3** — repeat-pattern $ violated the house attribution law (the hash filter sat in the
  denominator CTE, so hour credits were split across only the hashed queries, inflating every
  pattern), and the engine's actual recommendation text + LAST_RUN were computed but never
  rendered.
- **C2/C4/C7/C8** — duplicate anomaly rows with *contradicting* severities between the app and
  the server sweep; contract levers summing unrealizable savings (now with explicit
  realizability haircuts, so "room to spare" can't fire on optimism); a Cortex "High" on a
  single request plus a new aggregate-budget signal (ten users at 20% each no longer reads
  "no users over 25%"); evidence scores that sawtoothed with wall-clock.
- **D1–D7 / E1–E6** — $-blind action ranking, spill-vs-queue unit mismatch, storage growth
  ranked by TB instead of $, and caption honesty throughout.

### V069 — exec-board serverless/AI cost drivers
`SP_REFRESH_EXEC_BOARD` re-derived from V054: the `COST_DRIVER` panel read **only**
`FACT_WAREHOUSE_DAILY`, so serverless and AI spend could never appear as a cost driver while
the same page's KPIs claimed to cover them. The new arm reads `FACT_METERING_DAILY` with the
correct two-rate split (AI at `:ai_credit_price`, rest at `:credit_price`, both from SETTINGS)
and fills the identical column contract — no app change. Deliberately **not** fanned across
the ALFA/Trexis pills: `FACT_METERING_DAILY` has no `COMPANY` column, and splitting
account-level metering would invent attribution.

Gates green: ruff, mypy (pure layers), **pytest 1774 passed / 1 skipped**. Six test pins
reconciled — five were stale source-shape assertions (an `except` that slid past an arbitrary
2000-char window, `days`→`uc_days`, two probe reads becoming one superset), and one was a
genuine style regression (a raw `st.dataframe` converted back to `styled_table`).

## 4.113.0 — Recommendation engines Tier A: three engines were advising in the WRONG DIRECTION (2026-07-31)

A full audit of every in-app recommendation engine (14 engines, one adjudicator each)
found three that emit **executable** advice pointing the wrong way. A wrong number on a
dashboard misleads; a wrong `ALTER` makes the account worse — so these ship first, with
behavioural (not source-lock) tests.

- **A1 — threshold tuning inverted on inverse-metric rules.** `logic/tuning.py` assumed
  every rule fires when `METRIC_VALUE >= THRESHOLD`. But `SEC_CRED_EXPIRY` (days-until-expiry)
  and `COST_CONTRACT_BREACH` (projected days-left) fire when the value is **≤** the
  threshold — so the engine's "raise it to cut noise" suggestion **widened** firing, the
  exact opposite of its own stated basis. Added `LOWER_IS_WORSE`, threaded `rule_id`
  through `suggestions_by_rule`, and mirrored the whole computation (noise **p10** vs the
  actioned **ceiling**; pure-noise moves **down**). Also replaced the `METRIC_VALUE > 0`
  filter with a finite-only filter — on an inverse rule, `0` (expires today) and negatives
  (already expired) are the *most* actionable evidence and were being silently discarded.
- **A2 — quiet-window proposer mixed timezones.** `data/insights_sql.py`
  `warehouse_hourly_activity` took `HOUR(START_TIME)` from `ACCOUNT_USAGE` (TIMESTAMP_LTZ →
  **session** tz, i.e. UTC off-SiS) and joined it against `FACT_QUERY_HOURLY.HOUR_TS`
  (naive **account** time). The winning "quiet" hours were then pasted into an
  America/Chicago `CRON` by the schedule advisor — proposing a SUSPEND **up to 5–6 hours
  early, into the morning ramp**. Both sides now use
  `CONVERT_TIMEZONE('America/Chicago', …)`, including the `DAYS_SEEN` day-count.
- **A3 — idle advisor ignored the current `AUTO_SUSPEND`.** The recommendation hardcoded
  "e.g. 60s", so a warehouse already tuned to 30s was told to **raise** it, and
  fully-tuned warehouses still ranked #1 with nothing to distinguish them. The advisor now
  takes the live `AUTO_SUSPEND` (already fetched on the page), targets `min(current, 60)`,
  re-words already-tuned warehouses as *"residual is resume overhead, not a tuning gap"*,
  ranks them below fixable ones, and the **generated `ALTER` skips them entirely**.

Gates green: ruff, mypy (pure layers), pytest 1695 passed / 1 skipped (+12 Tier-A locks).

*Remaining audit findings (Tier B–E: ledger double-counting, the systemic live-fallback
window mismatch, ranking-proxy quality, caption honesty) and the perf plan (P1 health_strip,
P2 AI-users mart-first, P3 batch telemetry) are queued — see the session plan.*

## 4.112.0 — Live-screenshot fixes: $-in-markdown math-font bug (house-wide) + V068 freshness stamps (2026-07-31)

Two defects the owner spotted in the deployed app (screenshots, v4.111.0):

- **The serif-italic "weird font" in captions is a `$`-pairing LaTeX bug — fixed
  house-wide.** Streamlit's markdown treats a bare `$…$` pair as a math span, so any
  caption holding two dollar tokens (two `format_usd()` amounts, an amount + `USER$`, two
  literal rates) rendered the text between them as math. A sweep found **9 always-live
  sites** (the two screenshotted Spend & Attribution captions, the "Why totals differ"
  expander, the Admin rates caption + missing-migrations banner, the Contract renewal
  basis line, the Optimize pattern caption, the ETL unit-costs intro, and an expander
  *label*), 3 data-dependent ones (AI narrative, Brief action titles, ledger
  descriptions), and an ad-hoc `chr(92)` patch from July 11 proving the class regresses.
  Fix: one `formulas.md_dollars()` escape helper applied at every live site and inside
  the house sinks (`result_caption`, `panel_help`, the AI panel), the ad-hoc patch
  retired, and an **AST-based CI lock** (`tests/test_md_dollars.py`) that fails any new
  markdown-sink call carrying 2+ unescaped `$` tokens — the bug class cannot silently return.
- **`MART_LOCK_WAIT_DAILY` 448.5h stale → V068 owner migration.** The loader chain is
  fine; the freshness **stamp** was orphaned: when V041 retired the 10-minute
  `SP_SNAPSHOT_FRESHNESS` sweep and moved stamping into each loader, the two marts with
  their own standalone tasks (`SP_LOAD_LOCK_WAIT_MART`, `SP_LOAD_PATTERN_COST`) never got
  one — their `SOURCE_FRESHNESS_STATE` rows froze at apply time (~Jul 9, exactly 448.5h
  before the screenshot; pattern-cost equally frozen, just hidden behind the worst-source
  card). `V068__standalone_mart_freshness_stamps.sql` re-derives both loaders with the
  standard V041-R6 freshness MERGE, stamping `LAST_LOAD_TS = CURRENT_TIMESTAMP()` (a
  **run** stamp, not `MAX(LOAD_TS)` of the mart) so an account with **zero lock waits
  reads fresh, not stale-forever** — and the migration tail CALLs both procs so the rows
  heal immediately. **Owner applies V068 in Snowsight after V062 → … → V067.** Lockstep:
  `validate.sql` floor → 68, admin `_EXPECTED_MIGRATIONS[68]`, rebuild bundle
  `V001_V068`, DEPLOYMENT.md + README run-lists.

## 4.111.0 — V067 owner migration: alert attribution + serverless onset + escalation supersede + object-cost honesty (Codex review) (2026-07-31)

The behavioral proc fixes from the Codex 50-rec review, shipped as a new owner migration
(`V067`) re-deriving `SP_ALERT_SCAN` (from V066) and `SP_LOAD_OBJECT_COST` (from V062) via
`outputs/gen_v067.py` + count-asserted needle edits.

- **#22 — alerts attribute unknown databases correctly.** `COST_STORAGE_SURGE` and
  `PIPE_COPY_FAILURES` classified company with a raw `IFF(name LIKE 'TRXS%', 'Trexis',
  'ALFA')` (an unknown DB → ALFA). Both now use the canonical `COMPANY_FOR_DATABASE` UDF,
  which honors `DEPARTMENT_MAP` overrides and returns UNKNOWN like the rest of the app.
- **#20 — new serverless services trigger the creep alert.** `COST_SERVERLESS_CREEP`
  divided by `NULLIF(prior_week, 0)`, so a service with zero prior spend produced `NULL`
  growth and the row silently dropped. It now emits the finite **999%** onset sentinel
  (the same pattern V061 gave `COST_AI_CREEP`), so a brand-new serverless workload fires.
- **#40 — severity escalation no longer leaves a duplicate open alert** (the V066
  follow-on). A global post-scan sweep in `SP_ALERT_SCAN` resolves a WARN/MED event when
  its CRIT/HIGH sibling — the same dedupe key with only the band token swapped — is also
  open, tagging it `RESOLUTION_KIND='SUPERSEDED'` (which the per-rule precision score, that
  counts only ACTIONED/NOISE, ignores). The band tokens occur only in the three banded
  keys, so it's a no-op for every other rule.
- **#10 — object-cost load failures are honest.** `SP_LOAD_OBJECT_COST` rolled back + logged
  on failure, then refreshed the freshness snapshot and returned `'OK'`. It now returns a
  **non-OK** string after a rollback, so the failure is observable in the proc/task output.
- **#40 companion (app-side, from the adversarial review):** the sweep sets `RESOLVED_AT` on
  the superseded event, so the operator **MTTR panel** (`alert_mttr`) and the rule-precision
  **resolved count** now exclude `RESOLUTION_KIND='SUPERSEDED'` — a machine dedupe close is
  not a human resolution and must not pollute MTTR or the resolved tally.

No new objects, no data heal, no app runtime change — deterministic alert logic + a proc
return-string change, byte-verified by `tests/test_v067_alert_attribution.py` (no owner
smoke test). **Owner applies V067 in Snowsight after V062 → … → V066.** Lockstep:
`validate.sql` floor → 67, admin `_EXPECTED_MIGRATIONS[67]`, rebuild bundle `V001_V067`,
DEPLOYMENT.md + README run-lists.

*Deferred to a later migration (each more involved): the alert forecast/pace partial-day
parity (#16-mirror/#19), task return-value gating (#5), AI-predicate centralization (#21),
a new `SP_INCIDENT_DECLARE` proc (#43), and per-target transactions in `SP_NIGHTLY_RECONCILE`
(#4).*

## 4.110.0 — Codex 50-rec review: app-first correctness batch (2026-07-31)

An external (Codex) 50-item review was adjudicated against the real code (26 CONFIRMED,
21 PARTIAL, 3 DECLINE; every claimed P0 downgraded once judged against the SiS
owner's-rights + two-role USAGE deployment — no reproducible privilege escalation). This
ships the app-only confirmed fixes; the behavioral alert/proc fixes go in V067, and the
`GRANT USAGE ON STREAMLIT` codification (#3) is an owner deploy-script change.

- **#23 / #24 — open CRITICALs can no longer hide from the score.** `action_queue` now
  orders by **severity before** its newest-N cap (an old open CRITICAL could be truncated
  out), and the platform-score owner-queue penalty counts **CRITICAL** as well as HIGH (an
  open CRITICAL action was scoring zero).
- **#26 — savings-verify is conditional.** The ledger verify `UPDATE` gained
  `AND STATE = 'ESTIMATED'`, so a stale/concurrent page can't overwrite a settled amount.
- **#6 / #7 / #8 — the operator-action seam is stricter.** `execute_action` only treats an
  error as "proc not deployed" when it **names the CALLed proc** (a generic in-proc identifier
  error no longer silently runs legacy DML), success is an **allowlist** (`OK`/`VERIFIED`/
  `DUPLICATE`, not "anything but BLOCKED"), and the legacy fallback **stops at the first
  failure** instead of running a later mutation or the audit row after an error.
- **#9 — alert-note lifecycle SQL is a structured list**, so a `;` inside a note can't
  fracture the legacy statements.
- **#39 — AI-exception queueing is idempotent** (`INSERT … WHERE NOT EXISTS` on the natural
  key), so a re-click / partial-retry no longer duplicates action rows.
- **#16 — the month-end forecast estimates today's remainder** (projects from complete-day
  actuals over *today + remaining* days), instead of counting today's partial and never
  filling the rest of today — it no longer reads low all day.
- **#32 — allocation coverage caption reconciles with its chart** (both from the shown top-10).
- **#27 / #33 / #35 / #36 / #45 — hardening.** `sql_number` rejects NaN/inf; a raising mart
  coverage-probe **fails to live** instead of silently accepting the mart; timestamp
  localization converts already-aware columns instead of skipping them; the sparkline
  normalizer rejects infinities; and the cache-domain token uses the real `DEPARTMENT_MAP`
  name (was `DEPT_MAPPING`, matching nothing → every mapping write bumped the global cache).
- **#47 — corrected a stale caption** that claimed the budget alerts still price all credits
  at the compute rate (V061 split AI pricing).

**Declined:** #28 (manual verify is human attestation by design), #13 (documented day-grain
convention; today's partial lowers the bar, never inflates the KPI), #38 (cost-tool "up =
worse" is intentional; the cited column doesn't exist).

Gates green: ruff, mypy (pure layers), pytest 1663 passed / 1 skipped (+11 new locks).

## 4.109.0 — V066 owner migration: alert escalation + serverless window + timeline atomicity (bug round 6) (2026-07-31)

The 5 migration-proc findings from bug round 6, shipped as a new owner migration
(`V066__alert_escalation_serverless_window_timeline_atomicity.sql`) re-deriving three procs
from their latest defs (`SP_ALERT_SCAN` + `SP_LOAD_MARTS_V27` from V062, `SP_ALERT_SCAN_DAILY`
from V065) via `outputs/gen_v066.py` + count-asserted needle edits.

- **#1 / #11 / #2 — alert escalation restored.** Three rules computed a CRITICAL/HIGH
  severity but keyed their dedupe on `RULE_ID | scope | date-bucket` with **no severity
  band** — so once the day's (or week's) lower-severity event existed, the SAME row crossing
  the escalation threshold was blocked by `NOT EXISTS` and never re-paged at the higher
  urgency until the bucket rolled. Each dedupe key now carries a band that flips on the
  rule's own threshold: `PIPE_COPY_FAILURES` (#1, ≥10 files → CRIT), `COST_DEPT_BUDGET_PACE`
  (#11, ≥3× over pace → HIGH), `COST_CONTRACT_BREACH` (#2, ≤14 days left → CRIT). Mirrors the
  `SEC_CRED_EXPIRY` EXPIRED/EXPIRING discriminator. (`COST_AI_CREEP` shares the bare weekly
  key but has no within-bucket escalation, so it's left untouched.)
- **#6 — `COST_SERVERLESS_CREEP` week-over-week no longer inflated.** `THIS_WK` spanned 8
  days (`USAGE_DATE >= today-7` **includes today's** partial metering row) vs `PRIOR_WK` 7;
  the scan window now excludes today, matching what V065 did for `COST_AI_CREEP`.
- **#3 — incident timeline can't blank mid-incident.** `MART_INCIDENT_TIMELINE` arm [8] ran
  a 48h-window `DELETE` then a 4-way `UNION` `INSERT` under AUTOCOMMIT — a transient INSERT
  failure left the trailing 48h **empty** until the next hourly rebuild. The pair now runs
  inside one `BEGIN TRANSACTION … COMMIT` with `ROLLBACK` on error (the B34 wrap pattern
  already in this file).

No new objects, no data heal, no app runtime change — deterministic alert logic + one
atomicity wrap, byte-verified by `tests/test_v066_alert_escalation.py` (no owner smoke test).
**Owner applies V066 in Snowsight after V062 → … → V065.** Lockstep: `validate.sql` floor →
66, admin `_EXPECTED_MIGRATIONS[66]`, rebuild bundle `V001_V066`, DEPLOYMENT.md + README run-lists.

## 4.108.0 — Bug round 6: 10 app-only correctness fixes (2026-07-31)

A find → adversarially-verify → rank pass across six domains returned 15 confirmed
findings (0 dropped). The 10 app-only ones ship here; the 5 migration-proc findings ship
in V066. Several were fresh instances of patterns caught in earlier rounds (sticky
selection, window-mismatched allocation pools, missing `delta_color`).

- **#4 (MED) — health/pulse bar no longer vanishes on a read error.** `_health_values`
  conflated a failed read with an empty one (both → `{}`), so a transient error blanked the
  sidebar pulse and status bar entirely — an operator could read "nothing red" while
  criticals were open. It now returns `None` on error (distinct from `{}`), and the callers
  render a muted "Health check unavailable" instead of blank chrome.
- **#5 (MED) — stale spend anomalies age out.** The MAD-based anomaly keeps a one-off spike
  at high |z| for the whole trailing-30-day frame, and the triage row hardcoded
  `RAISED_AT=None` — so the same 30-day-old spike re-fired as an undismissable, dateless
  HIGH every morning. `anomaly_summary` now carries each hit's day, the triage row stamps it,
  and the Control Room feed keeps only the most recent complete day's anomalies.
- **#7 (MED) — spend-trend "pace vs prior week" excludes the partial day.** The trailing-7
  mean included today's `PROVISIONAL` (partial) bar the chart itself dims, printing a
  phantom negative pace on flat spend. It now compares the last two complete 7-day windows.
- **#8 / #12 (MED/LOW) — chargeback role allocation.** On the live fallback (cold role mart)
  a ≤90-day share was multiplied by an up-to-365-day pool; the pool is now rebuilt over the
  share's clamped window (mirrors the Spend `_alloc_pool` fix). And `wh_usd` now aggregates
  to warehouse grain (`groupby.sum`) instead of collapsing multi-company rows to the last.
- **#9 (MED) — jump-to a cross-company database keeps its filter.** Picking a Trexis DB while
  scoped to ALFA was silently dropped by the company guard; the jump now carries the DB's
  owning company (derived from the same sets that populate the box).
- **#10 (MED) — Operations query drill acts only on a new row selection.** The sticky
  `st.dataframe` selection overwrote the manual/picked query-ID every rerun and silently
  repointed the detail after a table reload; guarded with `_ops_top_sel_last` (as Overview).
- **#13 (LOW) — failed-login reasons share the 30-day cap** of the failed-logins table above
  them, so the two panels reconcile under one window scope.
- **#14 / #15 (LOW) — neutral `delta_color` on cost cards.** The Contract "Consumed" (95%
  near-exhaustion) and Storage-MTD cards rendered a reassuring green up-arrow on a rising
  cost; both now route through the neutral token like their siblings.

Gates green: ruff, mypy (pure layers), pytest 1642 passed / 1 skipped (+13 new locks).

## 4.107.0 — V065 owner migration: alert run-rate window fixes (bug round 5, ranks 2–3) (2026-07-31)

The two migration-proc findings from bug round 5, shipped as a new owner migration
(`V065__alert_run_rate_windows.sql`) rather than a hand-edit of the pending V064. Both
are **pre-existing** bugs (not V064 regressions) in `SP_ALERT_SCAN_DAILY`, re-derived
from V064's definition via `outputs/gen_v065.py` + count-asserted needle edits.

- **Rank 2 (MEDIUM) — budget-breach forecast under-projected.** `COST_FORECAST_BREACH`
  computed its daily run-rate as `MTD$ / DAY(CURRENT_DATE())` — month-to-date dollars,
  which include **today's partial day**, divided by the full day-of-month count. That
  **understates** the rate → understates the month-end projection → the alert could
  **fail to fire** on a real budget breach. Now a **complete-days-only** rate:
  `SUM(billed WHERE DAY < today) / COUNT(DISTINCT complete DAY)`, with `MTD_USD` (today's
  partial included) kept as the additive base. The identical dead-column rate in the
  `COST_BUDGET_PACE` CTE is fixed too, for consistency. (On day 1 of a month there's no
  complete day yet → NULL rate → no forecast alert that day, which is safer than
  projecting off a single partial day. The on-screen Overview forecast was already
  correct — this repairs the automated alert backstop.)
- **Rank 3 (LOW) — AI week-over-week inflated.** `COST_AI_CREEP` compared `THIS_WK`
  (`DAY >= today-7`, which **includes today** → 8 days) against `PRIOR_WK` (7 days). The
  scan window now excludes today (`AND DAY < CURRENT_DATE()`), so both weeks are equal,
  today-excluded **7-complete-day** windows.

No new objects, no data heal, no app runtime change — deterministic alert logic,
byte-verified by `tests/test_v065_alert_windows.py` (no owner smoke test). **Owner applies
V065 in Snowsight after V062 → V063 → V064.** Lockstep: `validate.sql` floor → 65, admin
`_EXPECTED_MIGRATIONS[65]`, rebuild bundle `V001_V065`, DEPLOYMENT.md + README run-lists.

## 4.106.0 — Bug round 5: Overview rerun loop + two attribution/table fidelity fixes (app-only) (2026-07-31)

A find → adversarially-verify → rank pass returned five confirmed findings; the three
app-only ones are fixed here (the two migration-proc findings are tracked for the next
owner migration). **Three of the five were regressions from the recent design waves —
the adversarial pass earning its keep.**

- **Rank 1 (HIGH) — Overview "Top actions" infinite rerun for EXECUTIVE.** The rec 10
  clickable Top-actions table fired `request_navigation("Control Room")` on *any*
  selection. For an EXECUTIVE (no Control Room page) the jump clamped back to Overview
  and reran; `st.dataframe`'s selection is **sticky** and re-emitted every rerun, so it
  re-fired forever — the page spun. Fixed at two layers: (1) `request_navigation` now
  **clamps the target to the viewer's profile and no-ops a self-jump** (same page, no
  section/filters) instead of queuing a rerun; (2) the Overview handler acts only on a
  **new** selection (`_ov_actions_last`), so a sticky value can't re-fire on unrelated
  reruns.
- **Rank 4 (LOW) — allocation caption vs. pool mismatch.** The "By user and database"
  intro caption stated the **full-window** pool, but a **cold allocation mart on a
  >90-day window** serves the dims **live** (a 90-day pool). The two dims now **pre-fetch**
  (cached, not doubled) so the caption reads the pool off the path that actually served —
  the stated dollars always match the bars.
- **Rank 5 (LOW) — wide-table identity column lost its pin.** The rec 13 header
  prettifier occupied the first column's config slot, so `_auto_pin` saw it as
  caller-configured and **skipped pinning** on ≥8-column tables. The pin and the pretty
  label are now built into the **same** `Column`, so a wide table keeps both.

Gates green: ruff, mypy, tests.

## 4.105.0 — Fix: sidebar nav multi-select / stuck navigation (app-only) (2026-07-31)

The grouped sidebar nav (Watch/Analyze/Govern) could show **two groups selected at
once** and refuse to navigate — clicking e.g. Cost & Contract wouldn't take because a
stale selection in another group blocked it.

- **Root cause.** Each group's `st.radio` used a **persistent** key (`_ow_nav_{group}`),
  so the widget remembered its own selection and **ignored the `index`**; the sibling
  group kept its stale highlight, and the `_nav_pick` callback's attempt to *pop* the
  sibling widget keys was unreliable (a failed pop left the nav stuck).
- **Fix.** Each group's radio key is now **scoped to the current page**
  (`_ow_nav_{group}_{current}`): when the page changes the keys change, so every radio
  re-seeds cleanly from `index` (derived from the single source of truth `_ow_page`) —
  exactly one page is ever highlighted, no popping, no stale keys.
- Also restored the `_ow_page` write + marked the remembered `?page=` as "seen", so the
  stale query-param echo can't clobber a fresh click one rerun later.
- Regression-tested via AppTest: a Watch→Analyze→Govern hop navigates and leaves exactly
  one group selected.

Gates green: ruff, mypy, tests (+1 nav regression lock; 1618 passed).

## 4.104.0 — Design scannability wave — Codex visual-review NEXT (app-only) (2026-07-31)

The NEXT items from the design assessment (`docs/reviews/DESIGN_REVIEW_2026-07-31.md`),
led by the two added recs the Codex review missed.

- **A3 — color the Δ columns** (the primary scan target of a cost command center). Any
  `DELTA_*` / `D_*` / `Δ` column now renders sign-colored in every table (increase =
  red/worse, decrease = green/better) — a +$40k and −$40k no longer differ only by a
  minus sign. Text-color only, so a table of deltas stays scannable.
- **A2 — colorblind-safe severity.** The event-timeline dots (the one hue-only severity
  surface) carry a redundant **shape** per severity (circle/diamond/triangle/square/cross)
  merged into one legend, so severity survives red-green color-blindness.
- **rec 11 — provenance de-noised.** The `ACCOUNT_USAGE` lag note printed verbatim under all
  ~76 panels; it now shows **once per page** in the header. `Source:`/`fetched` stay per panel.
- **rec 1 + rec 5 — header decluttered.** Scope no longer prints twice (caption + chips —
  the chips are now the single in-header scope statement); the per-page "OVERWATCH" kicker
  is gone (the sidebar brand + browser tab are the anchor).
- **rec 13 — human table headers.** SQL-shaped `UPPER_SNAKE` columns display as Title Case
  (`WAREHOUSE_NAME` → "Warehouse Name"); unit tokens (USD, TiB, p95) preserved; the CSV keeps
  raw names.
- **rec 6 — heading consistency.** Control Room's six top-level sections use the
  `section_header` stripe, matching Overview and Cost.
- **rec 15 / A5 — chart polish.** The Overview cost-drivers bar leads with its conclusion
  (top driver + its share); the boss chart's dollar axis matches the shared "Spend (USD)"
  spelling.

Deferred: **rec 7** (hide the sidebar radio dot) — DOM-fragile; a wrong selector could hide
nav labels, so it needs live-DOM verification, not a blind CSS guess.

Gates green: ruff, mypy, tests (+18 wave locks; 1617 passed).

## 4.103.0 — Design "scannability wave" — Codex visual-review DO-FIRST (app-only) (2026-07-31)

The DO-FIRST items from the Codex visual/design review assessment
(`docs/reviews/DESIGN_REVIEW_2026-07-31.md`; 7 CONFIRM / 7 PARTIAL / 6 DECLINE). All
app-only, no migration.

- **rec 14 — self-identifying CSV exports.** `_render_table` now names downloads
  `overwatch-{page}-{table}-{YYYYMMDD}.csv` (was `overwatch_table_3.csv`); `styled_table`
  / `selectable_table` take an optional `slug`. A folder of downloads is finally readable.
- **rec 2 — Brief order.** The AI morning narrative moved below the numbers/fires/asks and
  is collapsed by default, restoring the page's own "numbers first, fires second" contract.
- **rec 18 — micro-label floor.** Metric/card/stat labels bumped (0.66–0.72 → 0.72–0.76rem);
  tracking and the a11y-audited `.ow-help`/contrast (v4.96) left as-is.
- **A1 — one traffic-light palette** (Codex missed this). The sidebar health strip and two
  charts drifted (`#22c55e`/`#ef4444`/`#f97316`/`#f87171`); they now read the same
  `#34d399`/`#fb7185`/`#fbbf24` / `SEV_COLORS` as every other surface, so a state is one hue.
- **rec 10 — clickable action surface.** Overview "Top actions" is now a `selectable_table`;
  a row click jumps to the Control Room queue (was a dead read-only wall).
- **rec 4 — Top actions hoisted** above the boss chart / spend trend — an executive landing
  page leads with the work that needs an owner, not two charts.
- **rec 16 — readable boss chart.** `monthly_stacked_usd` default `top_n` 8→5 (≤6 colors),
  plus a companion "top movers MoM" table (Δ$ / Δ% per warehouse) that answers "who moved."
- **rec 20 — executive export.** `exec_summary_html` now embeds a self-contained trend
  sparkline (pure inline SVG) and a `@media print` stylesheet, so the downloaded summary
  prints as a clean one-pager.

Deferred/declined per the assessment (rec 3/8/9/12/17/19 undo deliberate recent work or
over-engineer a desktop-first SiS tool); NEXT items (1, 5, 6, 7, 11, 13, 15; A2/A3/A5)
remain queued.

Gates green: ruff, mypy, tests (+9 wave locks; 1608 passed).

## 4.102.0 — Allocated-attribution window alignment (r4 deferral resolved, app-only) (2026-07-31)

- **allocated share × pool window mismatch** (`cost_parts/spend.py`) — the r4 review's
  one deferred finding. The live-share fallback scans only ≤90 days of `QUERY_HISTORY`
  (`MAX_LIVE_WINDOW_DAYS`, a deliberate cost guardrail we **keep**), so on a >90-day
  window its shares are 90-day-scoped — yet they were multiplied by the full 182-day
  warehouse pool, zeroing entities active only in the older half of the window while
  their dollars stayed in the pool. A **live-served** dimension now gets a pool matched
  to its own clamped window (a cheap mart read, fetched once and only when a live dim
  needs it); mart-served dimensions keep the full-window pool (already aligned). The
  90-day live-scan cap is unchanged — the fix aligns the *pool* to the share, not the
  other way. The section and per-dimension coverage captions use the matched pool.

Gates green: ruff, mypy, tests (+1 lock; 1599 passed).

## 4.101.0 — Bug + design review round 4 (app-only) (2026-07-30)

Found by a find → adversarially-verify → synthesize workflow (13 confirmed, none
dropped); the high-ROI ones implemented. Bugs:

- **credits_to_usd cent-rounding** (`formulas.py`) — added `round_cents=False`. The
  per-day-per-pipeline USD series is now summed at full precision, so a sub-cent
  pipeline reads its real ~$0.54 instead of $0.00; the sub-cent unit costs ($/call,
  $/run, $/M-rows, $/TiB) no longer fake 4-decimal precision they'd already destroyed.
- **contract runway understated** (`contract_planner.py`) — burn averaged over
  drop-days only, excluding idle/weekend days and inflating burn. Now draw-down ÷
  non-top-up days (idle 0-days counted, only genuine renewal rises excluded), aligning
  with the canonical `metric_registry.contract_runway`.
- **neutral KPI delta contrast** (`components.py`) — the "off" delta hardcoded the
  retired pre-a11y `#6b7a90` (~3.9:1); routed through the WCAG-AA `--ow-ink-mute` token.
- **grouped-nav highlight desync** (`main.py`, r14 regression) — the radio index seeded
  off the lagging `?page=` query param, so a cross-group click could light two groups.
  `_ow_page` is now the authoritative highlight source; a deep link overrides only when
  it changes.
- **telemetry transient-error cascade** (`query.py`) — a single network blip latched the
  shape downgrade (dropping `SAMPLE_PROB`); now requires two consecutive flush failures.

Design:

- **5-KPI orphan** (`components.py`) — a 5th card (Overview's flagship Platform score)
  was marooned in one quarter-width column. Rows now rebalance evenly (5→3+2) and fill.
- **severity color** (`status_colors.py`) — HIGH was pixel-identical to CRITICAL in every
  table; HIGH is now a distinct orange (red→orange→amber→slate ramp).
- **KPI trust chips** (`theme.py`/`components.py`, r13 regression) — `float:right` was dead
  on a flex child, cramping long $ labels; chips now ride a wrapping right-aligned group.
- **boss chart colors** (`charts.py`) — monthly-spend-by-warehouse now colors by stable
  entity identity (the C15 contract) so a warehouse keeps its color month to month;
  size-ordered stacking preserved via an explicit order.
- **section headings** (`overview.py`) — the two top-level Overview sections use the
  `section_header` stripe, matching Cost.
- **change-impact table** (`operations.py`) — dropped the duplicate raw id column,
  collapsed baseline/after pairs into signed deltas, moved the long verdict rationale to
  the row drill, and labelled/formatted columns.
- **registry partial-day** (`metric_registry.py`, r16 gap) — `department_chargeback`
  declared `rolling-daily` + `excluded` (a contradiction); fixed, and `validate()` now
  catches window↔partial-day contradictions.

Deferred: the allocated-share vs pool window mismatch (live 90d × pool 182d) — the 90-day
cap deliberately bounds a live QUERY_HISTORY scan, so the correct fix is a perf/cost call
best made deliberately, not force-fixed here.

Gates green: ruff, mypy, tests (+13 r4 locks; 1598 passed).

## 4.100.0 — Codex R2 backlog: executable metric registry + cost coverage ladder (rec 16/17, app-only) (2026-07-30)

Completes the Codex-R2 backlog (and the whole review).

- **rec 16 (executable metric contract)** — the metric registry was descriptive metadata.
  Each `Metric` now carries an EXECUTABLE contract: `window` (from a `WINDOWS` vocabulary),
  `partial_day`, `unit`, `filters` (which global chips it honors), `required_sources`
  (structured), `coverage`, and `owner`. A new `validate()` asserts every metric declares a
  complete, enum-valid contract; a `get(key)` accessor lets code key off the registry. The
  missing **`contract_runway`** metric was added and CI-**drift-guarded**: a test asserts its
  declared window (trailing-30-**complete**-days, today excluded) matches the actual
  `contract_exhaustion()` SQL — revert that SQL to the old `/30` partial-day form and the
  build breaks, not just the alert. This is the drift class this session repeatedly
  hand-patched (rec 4/5/11/20). Wiring every panel to read its registry key at runtime
  remains the larger follow-on; the contract + CI guard land now.
- **rec 17 (cost coverage ladder)** — a single read-only "Coverage ladder" expander on the
  Spend attribution tab consolidates, in one place, how much of the bill each grain explains
  and where its residual is proven: billed/metered pool (warehouse-exact) → allocated
  user/db (estimate, residual = "Other") → measured-query/object grain (Object ledger
  RESIDUAL + the additive-contract recon) → billed-vs-model (rate-card recon). Reuses the
  in-scope `window_usd`; no new marts or queries.

Gates green: ruff, mypy, tests (+9 rec 16/17 locks incl. the runway drift-guard).

## 4.99.0 — Codex R2 backlog: workflow-grouped sidebar (rec 14, app-only) (2026-07-30)

- **rec 14 (Watch / Analyze / Govern nav)** — the sidebar was a flat page list. It now
  groups pages by operator workflow: **Watch** (Brief, Overview, Alerts) · **Analyze**
  (Control Room, Cost & Contract, Operations) · **Govern** (Security, Admin). `st.radio`
  has no native section headers, so each group is its own single-select radio under a
  caption header; a pick in one group clears the others (the `on_change` pops the sibling
  widget keys) so exactly one page highlights across the three. `NAV_GROUPS` is
  ordering-only — role visibility is still `PAGES_BY_PROFILE`, and any allowed page not
  listed in a group trails under "More" so a new page is never hidden by omission. The
  off-profile navigation guard and the AppTest nav suite were updated to the grouped
  structure (they exercise the callback at runtime).

Gates green: ruff, mypy, tests (+2 rec 14 locks; the nav AppTest suite validates the
grouped runtime).

## 4.98.0 — Codex R2 NEXT: telemetry re-weighting + route-backlog observability (rec 18/19, app-only) (2026-07-30)

The app-side payoff of the V064 telemetry columns, plus the webhook backlog view —
completing the Codex-R2 NEXT tier.

- **rec 18 (telemetry re-weightability)** — `telemetry_by_page` now reports
  `EST_TRUE_FETCHES` = `SUM(1/SAMPLE_PROB)`, restoring the healthy baseline the ~2%
  sampler undercounts ~50× (persisted `FETCHES` is not a census). `fleet_query_stats`
  exposes `SLOWEST_QUERY_ID` (`MAX_BY(QUERY_ID, ELAPSED_MS)`) so a slow row deep-links
  to `ACCOUNT_USAGE.QUERY_HISTORY`. Pre-V064 rows read `SAMPLE_PROB` NULL → weight 1.
- **rec 19 (route-backlog observability)** — new `route_backlog()` surfaces, per
  enabled route, the count of OPEN eligible-but-undelivered events and the age of the
  oldest, using the **same send-eligibility predicate** as `SP_NOTIFY_WEBHOOK` (so the
  panel and the drainer agree on "what's pending"). `delivery_slo_summary` now reads the
  proc's own `undelivered_expired` signal (`EXPIRED_UNDELIVERED`) instead of only the
  app's re-derived 30-min count. Both render in the Alerts → Native delivery section; a
  rising oldest-backlog age while the notify task runs is the starvation signal rec 8's
  oldest-first drain fixes.

Gates green: ruff, mypy, tests (+5 rec 18/19 locks). Requires V064 applied for the new
columns; the persist path and views degrade cleanly (NULL → weight 1) until then.

## 4.97.0 — V064 owner-migration + telemetry persist (Codex R2 rec 7/8/20-alert/18) (2026-07-30)

The Codex-R2 NEXT-tier owner-migration bundle (`snowflake/migrations/V064__…`),
authored via `outputs/gen_v064.py` under the derivation law and gated by an
adversarial-review workflow (1 major + 3 minor findings, all resolved).

- **NUMBERING** — this ships as **V064**, not V065. The contiguity invariant
  (`validate.sql` requires `COUNT(DISTINCT VERSION) BETWEEN 1 AND N = N`) forbids
  skipping the unbuilt T3-perf slot, so the deferred T3 work becomes V065.
- **rec 8 (webhook oldest-first bounded drain)** — `SP_NOTIFY_WEBHOOK` was a
  newest-first single shot per route/run, so under sustained volume the oldest
  alerts never fit the 3000-char message and eventually crossed the 24h window
  `undelivered_expired` — backwards from urgency. Now each route drains in batches
  **oldest-first** (bounded at 6/run; capture-once per batch preserves B9), so the
  backlog clears forward and the oldest never starve. The expired-detection now
  shares the send eligibility (family+company+severity). **Owner smoke test.**
- **rec 7 (per-source daily watermarks)** — `SP_LOAD_DAILY_FACTS` held one shared
  `DAILY_FACTS` mark, so any one table's failure re-read all four sources next run.
  Now each source keeps its own mark (metering advances after its MERGE — guarded so
  a watermark lock can't starve siblings; task/login/storage advance atomically
  inside their transaction). `SP_NIGHTLY_RECONCILE` rewinds the four new keys in
  lockstep. A review-found **cold-start seed** inherits each new key from the
  retained `DAILY_FACTS` position so a cutover during an outage re-covers the held
  backlog instead of dropping it. **Owner smoke test.**
- **rec 20-alert (contract-breach burn)** — the `COST_CONTRACT_BREACH` block in
  `SP_ALERT_SCAN_DAILY` burned `SUM(CREDITS_BILLED)/30` over a partial-current-day
  span, biasing burn low → overstating days-left → could **suppress** the breach.
  Now trailing-30-**complete**-days (`DAY BETWEEN today-30 AND today-1`, divided by
  the actual complete-day count) — the same canonical burn the app mart and
  `contract.py` already use.
- **rec 18 (telemetry re-weightability)** — `APP_QUERY_TELEMETRY` gains
  `SAMPLE_PROB` + `QUERY_ID` (additive, idempotent). The app persist path now writes
  both (1.0 for the must-persist stream, the sample rate for the healthy stream) via
  a 3-level shape downgrade so it degrades cleanly against an older schema. The
  weighted-percentile Admin view + rec 19 route-backlog observability follow.

Lockstep: `validate.sql` floor, admin `_EXPECTED_MIGRATIONS[64]`, rebuild bundle
`02_migrations_V001_V064.sql`, `DEPLOYMENT.md` + `README.md` run-lists and V064
smoke-test runbook. Gates green: ruff, mypy, tests (+14 V064 locks).

## 4.96.0 — Codex R2 NEXT wave C: accessibility floor (rec 15, app-only) (2026-07-30)

**rec 15 (MEDIUM)** — a11y polish on the dense KPI/status surfaces:

- **WCAG-AA muted contrast** — `--ow-ink-mute` lifted `#6b7a90` → `#8593a8`. The old
  value computed ~4.35:1 on the app background — under the 4.5:1 AA floor for the small
  (0.62–0.72rem) labels that use it. The new value clears 4.5:1 on all three surfaces it
  lands on (bg 6.1, surface 5.7, raised 5.4). One token, every muted label fixed at once;
  a self-computing test asserts the ratio so it can't silently regress.
- **Keyboard/touch KPI help** — the metric card's help was a hover-only `title=` on the
  whole card: invisible on touch, unreachable by Tab. Replaced with a focusable `?`
  affordance carrying `aria-label` (screen-reader announced) + a CSS tooltip that fires on
  hover **and** focus. CSP-safe (no JS, `content:attr(data-help)`); native `title=` kept as
  a pointer fallback.
- **Wrapping segmented controls** — the Section/Window radiogroups used
  `overflow-x:auto; flex-wrap:nowrap`, hiding options behind an off-screen scroll edge
  (a keyboard trap, invisible to touch). Now `flex-wrap:wrap` so every option stays
  reachable.
- **Label size floor** — raised the two smallest info-bearing labels off the floor
  (`.ow-stat__k` 0.62→0.66rem, metric/card titles 0.70→0.72rem).

Gates green: ruff, mypy, tests (+4 wave-C locks, incl. a self-verifying WCAG ratio).

## 4.95.0 — Codex R2 NEXT wave B: distinct card trust tokens + section scope (rec 13/11, app-only) (2026-07-30)

- **rec 13 (MEDIUM)** — the KPI card had ONE chip slot, so scope and freshness competed
  for it: C11 crammed `account-wide` into the freshness `badge`, and a card could show
  scope OR freshness, never both. The card now renders **three distinct trust tokens** —
  freshness (`badge`: mart|live|stale), method (`billed`|`metering`), and scope
  (`account-wide`|`company`) — each as its own chip with its own color. The Overview
  billed MTD/Projected cards carry `method: billed` + `scope: account-wide`; the company
  window-spend card carries `method: metering` + `scope: company`. New theme styles for
  the `--method` / `--scope` chips.
- **rec 11 (MEDIUM)** — the Overview headline KPIs are account-/company-scoped and do NOT
  honor the warehouse/schema/user/database dimension chips (those bite on the detail
  pages). New `section_scope_note(filters)` surfaces a single honest line naming the
  active dimension chips a section ignores — but **only when one is set**, so there is
  zero clutter on the common no-filter path. Shares the rec 13 scope vocabulary
  (`_SCOPE_DIM_LABELS`) as one source of truth.

Gates green: ruff, mypy, tests (+5 wave-B locks).

## 4.94.0 — Codex R2 NEXT wave A: score-read batching + single allocation chart (app-only) (2026-07-30)

- **rec 9 (MEDIUM)** — finish the Overview first-paint batching for the score path.
  The two hourly score-health reads (`score_throughput` + `score_tasks`) are
  independent and company-scoped, so they now fetch in one `run_batch` round trip
  (member-level fallback preserved). The board and 150-day reads stay unbatched by
  design (filter-scoped + fixed cold-start each other, Codex #4); `health_strip`
  stays on the shared shell cache; the live alert/action reads batch separately.
- **rec 12 (MEDIUM)** — the user/database allocation panel rendered a waterfall AND a
  bar of the identical top-10 (both defaulted to `top_n=10`), and the waterfall's
  cumulative form falsely implied full reconciliation. Now ONE sorted contribution
  bar with an explicit **"Other / not shown"** row (= scoped pool − shown
  contributors), so the chart accounts for 100% of the pool, not just the top rows.

Gates green: ruff, mypy, tests (+2 wave-A locks).

## 4.93.0 — Codex R2 wave 5: one effective cost-allocation window (rec 4, app-only) (2026-07-30)

**rec 4 (HIGH)** — per-entity cost attribution mis-reconciled because the warehouse
dollar **pool** and the allocation **shares** used divergent windows: the pool
(`fact_warehouse_window_vs_prior`) clamped 365→182 and **excluded** today, while the
share denominator (`alloc_xdim_attribution`) clamped 365→400 and **included** today —
so a 365-day share (with today's partial) was multiplied by a 182-day, today-excluded
dollar total, mis-allocating each user's/database's cost.

- **Added rec #3**: a new `common.resolve_effective_window(days)` is the single source
  of truth — it clamps to the vs-prior half-window cap (`MAX_MART_WINDOW_DAYS // 2` =
  182) and returns the **half-open `[today - eff_days, today)`** fragment (today
  excluded). The dollar pool, the mart allocation share denominator, **and** the live
  fallback denominator (`cost_sql.allocated_attribution`, kept at its 90-day
  live-scan cap) all resolve through it, so the numbers reconcile on every path.

Gates green: ruff, mypy, tests (+2 wave-5 locks). This closes the Codex R2 DO-FIRST
block (A-score-1, rec 5, A-score-2, rec 20, rec 4 + the A-score-3/rec-10 riders).

## 4.92.0 — Codex R2 wave 4: canonical contract runway (rec 20, app-side) (2026-07-30)

**rec 20 (HIGH)** — N11 fixed the contract projection on the Contract page, but the
Brief's runway (and the `COST_CONTRACT_BREACH` alert) still read
`mart_sql.contract_exhaustion()`, whose `DAILY_BURN` summed a **31-date span that
included today's partial metering and divided by a literal 30** — biasing burn low,
overstating days-left, and able to **suppress the breach alert**. It now uses the
canonical **trailing-30-COMPLETE-days** burn: `SUM(billed) over DAY BETWEEN today-30
AND today-1` (today excluded) divided by the count of complete days actually present
— the same basis the renewal planner uses (N11), so the Brief runway can no longer
contradict the Contract page.

Added rec #2: the docstring's self-referential *"Same math as the
COST_CONTRACT_BREACH scan block"* — which was the drift signal, and already stale
since N11 — is removed and replaced with the canonical definition.

**Owner-migration follow-up (V065):** the `COST_CONTRACT_BREACH` alert block (now in
`SP_ALERT_SCAN_DAILY`, V062 C9) still carries the old math and must be aligned to the
same trailing-30-complete-days window when V065 ships.

Gates green: ruff, mypy, tests (+1 wave-4 lock).

## 4.91.0 — Codex R2 wave 3: every score input fails closed (A-score-2, app-only) (2026-07-30)

**A-score-2 (HIGH)** — C1 made the platform score fail closed when the two REQUIRED
sources (throughput, alerts) didn't load, but the other four penalty-bearing inputs
still fell through as silent 0 penalties: a failed **task**, **freshness**,
**owner-queue**, or **budget/MTD** read zeroed its deduction and *raised* the score —
the exact cardinal sin C1 was built to kill.

- New pure `scoring.degraded_sources(results)` classifies each read: a genuine
  **outage** (`ok is False` AND `error_kind != 'absent'` — timeout / unknown_function
  / other) is degraded; an **absent** mart (simply not installed) stays a legitimate
  zero, so a partial deployment is not permanently `Incomplete`.
- `platform_score` gains a `degraded` gate alongside `available`: any degraded
  penalty source → `Incomplete`. Overview builds the set from `_tk` (task), `_hs`
  (freshness), `actions_res` (owner-queue), and — only when a budget is configured
  and the MTD read didn't resolve — the metering read (budget).
- A golden test matrix (added rec #5) locks the classification: outage kinds fail
  closed, `absent`/ok stay zero-penalty, keeping a freshly-provisioned deployment
  from oscillating between `Incomplete` and a falsely-healthy score.

Backward-compatible (the `degraded` param defaults to `None`).
Gates green: ruff, mypy, tests (+4 wave-3 locks).

## 4.90.0 — Codex R2 wave 2: honest executive downloads (rec 5, app-only) (2026-07-30)

**rec 5 (HIGH)** — the executive summary export (HTML + .txt) now tells the same
truth the screen does:
- An **Incomplete** platform score exports as "Incomplete — health inputs
  unavailable", not a real-looking `0/100` (which read as a catastrophic score).
- **MTD & Projected** carry a `· account-wide` scope tag; **Window spend** is
  labelled company‑scoped warehouse metering — so account‑wide figures no longer
  sit silently under the company heading.
- The footer no longer **blanket‑claims** the cloud‑services adjustment for every
  number; it now states that MTD/Projected are billed credits (adjustment applied,
  account‑wide) while window spend is warehouse metering (credits × rate, no
  adjustment), and that an Incomplete score means required health inputs didn't load.
- Timestamps use **account time** (America/Chicago), matching the app.

Gates green: ruff, mypy, tests (+2 wave-2 locks).

## 4.89.0 — Codex R2 wave 1: uncapped Overview score + honest window + cache tiers (app-only) (2026-07-30)

The first cluster from `docs/reviews/RECS_REVIEW_2026-07-31.md`, all residuals of
this session's own work. App-only.

- **A-score-1 (CRITICAL) — the platform score no longer undercounts criticals in a
  storm.** C4/C7 uncapped the alert counts on the Alerts page but left Overview's
  "Open critical/high" KPI **and the platform score** counting from a 500-row feed,
  so a >500 open-alert storm would undercount criticals into the score — inflating
  the headline number exactly when it must be trusted. Overview renders no alert
  *list*, so it now counts from the uncapped `open_alert_severity_counts` aggregate
  (same source as the Alerts KPI); the 500-row fetch it discarded is gone. A parity
  test locks the two surfaces to the same source so the split can't recur.
- **A-score-3 (MEDIUM) — the score window is labelled honestly.** The C2/N5 fixed
  window is midnight-aligned (yesterday 00:00 → now), i.e. the **previous + current
  calendar day** (24h at midnight, ~48h by end of day), deliberately aligned to the
  live ops tiles — not a rolling 24h. The source labels and KPI help said "fixed
  24h"; they now say "previous + current calendar day" and note the day-grain task leg.
- **rec 10 (LOW) — score/task-node reads match their source cadence.** The score
  throughput/task reads, the retro `score_inputs`, and the C18 per-node timing panel
  sat on the 5-minute `recent` tier though their facts refresh hourly/daily — re-paying
  unchanged reads up to ~11×/hour. Moved to the `hourly` tier (refresh-salt
  invalidation retained). Alert counts stay `live` (30s).

Gates green: ruff, mypy, tests (+6 wave-1 locks in `tests/test_codex_r2_wave.py`).

## 4.88.0 — V063 owner migration: webhook capture-once + daily-facts fail-guard (2026-07-30)

The two correctness/robustness fixes deferred from V062 after its adversarial
review (`wf_0ae6f51b`), now authored + re-reviewed. Migration-only (no app-code
change); generated byte-exact by `outputs/gen_v063.py`. **Joe applies in Snowsight
and runs the B9 smoke test (DEPLOYMENT.md).**

- **B9 — `SP_NOTIFY_WEBHOOK` capture-once.** The V062-era attempt re-derived the
  fitting-event set *twice* (message vs ledger) straddling the network send, so a
  concurrent `ALERT_EVENTS` insert (e.g. `SP_ALERT_SCAN_DAILY`) or `CURRENT_TIMESTAMP`
  crossing the 24h window mid-send could mark an unsent event delivered / re-send a
  sent one. Now the fitting `EVENT_ID`s (VARCHAR(80)) are frozen **once** into an
  array (`fits_ids`); the message `LISTAGG`, the `ALERT_DELIVERIES` ledger, and the
  `NOTIFIED_AT` update all read that one immutable set via `ARRAY_CONTAINS`, so the
  sent set == the recorded set by construction. Non-fitting events keep
  `NOTIFIED_AT = NULL` and re-drain next run; each event line is ≤~300 escaped chars
  so a single event can never exceed 3000 alone (no starvation). *Runtime `ARRAY`
  binding can't be byte-proven → **owner smoke test is the gate**.*
- **B34 — `SP_LOAD_DAILY_FACTS` fail-guard.** V062's per-table transaction wraps were
  correct, but a failed table's handler swallowed the error and the proc still
  advanced the `DAILY_FACTS` watermark and returned success — a partial-failure run
  read as success. Now a `failed_any` flag (set in each of the 3 handlers) gates the
  watermark advance and flips the `RETURN` to non-success, so the failed day
  self-heals on the next run's watermark re-read. Per-table isolation preserved
  (siblings still load; no re-raise added).

**Deferred to V064:** the T3.1–T3.4 perf-loader restructures, isolated by risk
(T3.1's `d<=2` gate is the single highest-corruption-risk edit — a wrong gate would
silently overwrite D-3 query-mart rows).

Lockstep: `validate.sql` floor → 63, admin `_EXPECTED_MIGRATIONS[63]`, rebuild
bundle, DEPLOYMENT.md + README run-lists (incl. the B9 smoke-test runbook). No
teardown change (no new objects). Gates green: ruff, mypy, tests (+7 V063 locks).

## 4.87.0 — V062 owner migration: loader robustness + correctness + alert split (2026-07-30)

The V062 owner-applied migration (`snowflake/migrations/V062__loader_robustness_alert_split_webhook.sql`,
generated byte-exact by `outputs/gen_v062.py`) plus the paired app-side R3-4 legs
that ship with this release. **Joe applies the migration in Snowsight; the app
redeploys together (R3-4 parity).** Authored via a spec workflow (needle counts
self-verified) and an adversarial logic review (`wf_0ae6f51b`, 6 skeptics).

Migration fixes (all re-derived from their latest base via the derivation law;
`tests/test_v062_loader_robustness.py` byte-compares):
- **R3-4 — query "failed" predicate standardized `= 'FAIL'` → `<> 'SUCCESS'`**
  (fail+incident) across `SP_LOAD_QH_EXTRACT` (×2), `SP_LOAD_OPS_DIAG` (×1),
  `SP_LOAD_MARTS_V27` (×4), `backfill_365.sql`, **and the 4 paired app live-fallback
  reads** (`ops_sql` ×2, `insights_sql`, `mart_sql`) — they must move together or the
  "Failed" tiles disagree mart-vs-live (the same trap V057 taught us).
- **B5 — backfill day-cap fix.** The four `LEAST(:d, 2)`-capped `SP_LOAD_MARTS_V27`
  arms clamp to `GREATEST(-:d, :ext_lo)` so a wide backfill (`'HOURLY', 90`) loads the
  full window instead of silently 2 days; normal ops stay extract-bounded.
- **B10 — reconcile boundary-hour clamp.** `SP_LOAD_OPS_DIAG` (×3 bounds) and
  `SP_LOAD_MARTS_V27` (×2 hourly bounds) clamp their lower bound to the extract's first
  *whole* hour, so the boundary hour rebuilt from a ~72h extract is no longer
  undercounted (DELETE window == rebuild window).
- **B11 — hourly-facts watermark catch-up.** `SP_LOAD_HOURLY_FACTS` reads/advances an
  `OW_LOAD_WATERMARKS('HOURLY_FACTS')` mark and loads from it (not a fixed −3d), and
  `SP_NIGHTLY_RECONCILE` resets it, so a >3-day loader outage self-heals interior holes.
- **B12 — backfill/task race.** `backfill_365.sql` now `SUSPEND`s `TASK_LOAD_HOURLY`
  around the extract-fed section so the minute-7 watermark trim can't shrink the
  90-day extract mid-backfill.
- **B34 — transaction-wrapped DELETE+INSERT.** `SP_LOAD_DAILY_FACTS` (3 units) and
  `SP_LOAD_OBJECT_COST` (1 unit; DDL stages hoisted above the transaction) can no
  longer leave a fact half-empty on a mid-run crash.
- **C9 — daily alert blocks split out.** The 6 daily-cadence rule blocks
  (`[06][07][08][09][13b][16]`) moved from the hourly `SP_ALERT_SCAN` (which scanned
  them 24×/day over stale rows) into a new `SP_ALERT_SCAN_DAILY` chained *after*
  `TASK_LOAD_DAILY` (own `|DAILY|` dedupe key; no renumber of the 14 surviving hourly
  blocks).

**Deferred to V063** (documented in `DEPLOYMENT.md`): **B9** webhook truncation-delivery
— the adversarial review found the authored fix re-derived the fitting event set twice
(message vs ledger) straddling the network send, so a concurrent `ALERT_EVENTS` insert
or a `CURRENT_TIMESTAMP` 24h crossing mid-send could mark an unsent event delivered; the
correct capture-once-`ARRAY` fix needs runtime smoke-testing. Also deferred: the B34
partial-failure observability refinement and the T3.1–T3.4 perf-loader restructures.

Lockstep updated: `validate.sql` floor → 62, admin `_EXPECTED_MIGRATIONS[62]`, rebuild
bundle (`02_migrations_V001_V062.sql`), `teardown.sql` (new task + proc),
DEPLOYMENT.md + README run-lists (incl. the deferral note). Gates green: ruff, mypy,
1528 tests (+13 V062 locks).

## 4.86.0 — NEXT wave 4: contract-pace/planner reconciliation + a11y (app-only) (2026-07-30)

N11 + C15 from `docs/reviews/RECS_REVIEW_2026-07-30.md`. App-only; no migration.
Completes the NEXT-tier block.

- **N11 (MEDIUM) — the prominent contract projection stops contradicting the
  planner.** Cost & Contract showed two forward projections on the same page:
  the headline "Projected term total" used a **lifetime-average** burn (optimistic
  on any accelerating account), while the renewal planner and year strip below it
  used **trailing-30d** burn. `contract_pace()` gains an optional
  `trailing_daily_credits`; when supplied it projects **booked consumption +
  remaining days at recent burn** (same basis as the planner, today's partial day
  excluded per N1), so the most prominent number is now the conservative,
  page-consistent one. The lifetime pace/share ratios stay as honest actuals
  (are-you-ahead-of-the-clock); the 5-arg lifetime fallback is preserved for
  backward compatibility.
- **C15 (LOW) — two accessibility wins.** (1) A **stable entity→color** map
  (`crc32(name) → fixed palette`) on the daily stacked charts, so a given
  warehouse/company keeps the same color across renders instead of being recolored
  whenever the set of series changes. (2) A **≥11px label floor** — the one
  sub-floor chart label (the 10px budget-rule annotation) is bumped to 11 (axis
  and legend labels were already 11 in the theme).

Gates green: ruff, mypy (pure layers), 1514 tests (+7 wave-4 locks). This closes
the NEXT-tier app-only block (C2+N5, N4, C18, C11+N7, N10, N12, N11, C15).

## 4.85.0 — NEXT wave 3: Overview first-paint batching + telemetry write buffer (app-only) (2026-07-30)

N4 + N12 from `docs/reviews/RECS_REVIEW_2026-07-30.md`. App-only; no migration.

- **N4 (MEDIUM) — Overview adopts first-paint batching.** Overview was the last
  data-heavy page still issuing its live first-paint reads serially. The two
  independent live reads — open alerts and the owner-action queue — now fetch in
  one `run_batch` round trip (each key preserved, so warm-cache hits are
  unchanged). `health_strip` stays out (the shell already fetched it — shared
  cache, r15 #14) and the filter-scoped exec board stays out (its own key,
  Codex #4).
- **N12 (MEDIUM) — telemetry/usage writes are buffered.** Three per-render write
  paths (`_persist_telemetry`, `_log_usage`, `log_ui_event`) each fired their own
  single-row INSERT on the shared warehouse — one round trip apiece, on the app's
  own dime. They now enqueue row fragments into a session buffer keyed by target
  table + column shape, flushed once per rerun as ONE multi-row
  `INSERT … SELECT … UNION ALL SELECT …` per shape (auto-flush at 25 rows;
  a top-of-rerun flush drains rows a prior `st.rerun()` cut short). The flushed
  statement is still a single allow-listed write — routing telemetry through the
  allow-list is a hardening bonus. Best-effort semantics preserved: a V027-shape
  flush failure downgrades to the old column shape, a hard failure turns the
  producer off.

Gates green: ruff, mypy (pure layers), 1509 tests (+8 wave-3 locks).

## 4.84.0 — NEXT wave 2: anomaly collapse-vs-spike + per-node timing panel (app-only) (2026-07-30)

N10 + C18 from `docs/reviews/RECS_REVIEW_2026-07-30.md`. App-only; no migration.

- **N10 (MEDIUM) — a spend collapse is no longer read like a spike.** The anomaly
  detector already flagged both directions (|z| ≥ threshold), but the triage queue
  labeled every one a generic "Spend anomaly" and the Operations table sorted by
  raw z (burying large-negative collapses at the bottom). A collapse (z < 0) is
  usually a stalled loader / dead pipeline — a real outage signal. Now: the triage
  queue gives collapses a distinct **"Spend collapse"** kind + "possible stalled
  workload / dead pipeline" detail (spikes keep "Spend anomaly"); the Control Room
  triage routes both to Cost & Contract; the Operations anomaly table ranks by
  |z| so collapses surface next to spikes; and the Spend & Attribution warning
  distinguishes collapse from overspend. Detector math unchanged (signed z already
  propagated).
- **C18 (MEDIUM) — the per-node loader-timing mart finally has a reader.**
  `MART_TASK_NODE_DAILY` (shipped V058) loaded but nothing read it. A new
  Operations → Tasks "Per-node timing" panel surfaces per-node dispatch-queue
  delay (SCHEDULED→START — warehouse contention / late start) and exec p95/max
  (START→COMPLETE) that the coarse `FACT_TASK_DAILY` summary can't show, ranked by
  p95 dispatch queue. Mart-only via plain `run()` (no live-parity builder computes
  the dispatch delay, so a fallback leg would diverge); scoped by DATABASE_NAME
  (the mart has no COMPANY grain); failure-rate derived at read.

Gates green: ruff, mypy (pure layers), tests (+3 wave-2 locks).

## 4.83.0 — NEXT wave 1: platform-score honesty on Overview (app-only) (2026-07-30)

C2 + N5 + C11 + N7 from `docs/reviews/RECS_REVIEW_2026-07-30.md`, scoped by the
verification workflow. App-only; no migration.

- **C2 + N5 (MEDIUM) — the platform score no longer moves when you change the
  spend window.** The score's throughput/pressure signals (queries, failures,
  queued minutes, remote spill, task runs/failures) were read from the exec board,
  which is windowed to the user's 7/30/90-day spend scope. Over a long window the
  cumulative `QUEUED_MINUTES`/`SPILL_GB` dwarf the spike-sized 10-min/5-GB
  thresholds and trip a near-constant deduction (13× larger at 90d than 7d), and
  the retro sparkline (per-day basis) was incomparable to the headline. Those six
  signals now read a **fixed 24h window** (`fact_query_window_summary(1)` +
  `fact_task_daily(1)`), matching the retro's per-day basis; budget/alerts/stale/
  owner-queue stay account-wide. The required-source key for C1's fail-open gating
  is repointed `board → throughput` (and added to the coverage set in the same
  change, so a throughput-read outage still reports `Incomplete`, not a false
  green). The score's help and the retro caption now state the blend honestly
  (the old caption falsely called the headline "the company-scoped number").
- **C11 (MEDIUM) — account-wide badges.** MTD and Projected month-end are
  account-wide (metering-daily has no company grain); they now carry an
  `account-wide` badge so the numbers aren't misread as company-scoped under a
  company filter.
- **N7 (MEDIUM) — storage/transfer disclosure.** Overview and Brief now disclose
  that MTD/Projected cover credit-billed services (compute, serverless, AI) and
  that storage + data-transfer bill separately (Cost & Contract → org rate card).
  Disclosure only — folding them in would break the credits × rate contract and
  double-count against the rate-card panel.

Gates green: ruff, mypy (pure layers), tests (+4 wave-1 locks in
`tests/test_dofirst_wave.py`).

## 4.82.0 — DO-FIRST wave: KPI-trust + morning-surface actionability (app-only) (2026-07-30)

The ten cheapest, highest-trust fixes from the recommendations review
(`docs/reviews/RECS_REVIEW_2026-07-30.md`): the two headline-KPI trust bugs
(fail-open score, low-biased projection) and the three morning-surface dead-ends
(undelivered criticals, triage actionability, score drill). All app-only /
read-side — no migration.

- **C1 (HIGH) — platform score no longer fails OPEN.** An outage that suppressed
  the health signals used to remove their penalties, so a degraded platform could
  read 100/Healthy — the cardinal monitoring sin. `platform_score` now takes an
  `available` coverage set; when a required source (exec board, alerts) did not
  load it returns `Incomplete`/0 with an "Inputs unavailable" driver instead of a
  false green. `overview.py` computes coverage from the actual reads and renders
  the KPI as *Incomplete* when gated.
- **N1 (HIGH) — forecasts stop averaging today's PARTIAL day into the rate.**
  Month-end (`forecast.py`), the year projection, and the renewal planner
  (`cost_parts/contract.py`) built their forward daily rate over a window that
  included today's incomplete spend, biasing every projection low. MTD keeps
  today's actual; the forward rate now uses complete days only (`DAY < today`).
- **C3 / N14 / N15 (MEDIUM) — freshness tells the truth at deploy.** A
  never-loaded source (NULL `LAST_LOAD_TS`) used to render "0.0h, fresh". The
  Control Room board now shows a distinct **NOT LOADED** status (sorted to the
  top, counted, error-banner), the score's `STALE_SOURCES` count includes NULL-ts
  sources, and the sidebar/Brief "stalest telemetry" badge is **cadence-aware**
  (a daily fact at 7h is on time, an hourly fact is not) and **names the source**
  instead of only its age.
- **N2 (HIGH) — undelivered criticals surface on the morning surfaces.** A
  critical that is 30+ min old with no delivery row (it paged nobody) was buried
  in Alerts → delivery SLO. The always-on `health_strip` now carries
  `UNDELIVERED_CRITICAL`, and the sidebar, Brief, and Control Room show a
  one-click "N critical(s) reached nobody →" banner into the delivery view.
- **N3 (HIGH) — Control Room triage queue is actionable.** The morning list was a
  read-only wall that dropped `EVENT_ID`/`RULE_ID`. The queue now carries those
  ids and is a `selectable_table`: selecting a row jumps to the page that owns it
  (alerts → Alerts, task failure → Operations, spend anomaly → Cost & Contract).
- **C4 + C7 (LOW) — alert KPI tiles count uncapped.** The Open critical/high/total
  tiles were derived from the 500-row feed, undercounting in exactly the storm
  they exist to flag. A single `open_alert_severity_counts` `COUNT_IF` aggregate
  now feeds the tiles (feed-derived counts remain the fallback).
- **N6 (MEDIUM) — boss chart current-month uses `account_today()`** (America/
  Chicago) instead of server UTC wall-clock, so it no longer jumps a month early
  on a month-end evening.
- **N8 (MEDIUM) — score-driver expander is now a prescription.** Each deduction
  gets an "Investigate →" button routing to the page that can act on it
  (`_SCORE_DRIVER_NAV`), reusing the existing navigation plumbing.
- **N9 (MEDIUM) — small-frame CSV serialized once, not every rerun.** Tables of
  4–200 rows re-encoded a full CSV on every 30s rerun (the large-frame path was
  fixed in T1.6; the small path never was). Now memoized by content fingerprint,
  one-click UX preserved.

Gates green: ruff, mypy (pure layers), 1492 tests (+20 wave locks in
`tests/test_dofirst_wave.py`).

## 4.81.0 — rename the app warehouse WH_ALFA_OVERWATCH → WH_ALFA_ADMIN (rebuild required) (2026-07-30)

Owner renamed the app's dedicated warehouse in Snowflake. Renamed every reference
across the codebase (93 occurrences / 54 files): `app/config.py` `APP_WAREHOUSE`, the
app-side SQL/labels (`mart_sql`, `remediation` task template, `admin`/`brief` sources,
`canary` smoke tests), all migrations (V002 `CREATE WAREHOUSE` + `ALTER` + task
bindings, and the task-warehouse bindings in V003/V004/V007/V008/V010/V012/V014-V016/
V018/V021/V024/V027/V032/V033/V035/V036/V038/V040/V041/V045/V046/V048), `roles.sql`,
`teardown.sql`, `validate.sql`, `backfill_365.sql`, the standalone alert/ML helpers,
the deploy config (`.streamlit/secrets.toml.example`, `snowflake.yml` `query_warehouse`,
`flyway.toml.example` JDBC), `outputs/gen_v045.py`, and the current-state docs. The
rebuild bundle was regenerated; the derived-migration byte-compare tests and the
warehouse-name assertion tests all pass.

**This is a teardown + rebuild change** (the warehouse rename orphans the app's
task→warehouse bindings), so the historical migrations were edited in place rather than
fixed-forward — the whole point is a from-scratch rebuild. Operational steps for the
apply:
1. Run `snowflake/teardown.sql` (drops the app tasks/rebuildables; the shared warehouse
   itself is preserved — the DROP WAREHOUSE lines stay commented).
2. Re-apply the migrations (or `snowflake/rebuild/`), which `CREATE WAREHOUSE IF NOT
   EXISTS WH_ALFA_ADMIN` (a no-op post-rename) and rebind every task to it.
3. Redeploy the Streamlit-in-Snowflake app so `snowflake.yml`'s new `query_warehouse`
   takes effect; update any local `secrets.toml` / Flyway config.

Dated historical records (`CHANGELOG` prior entries, `docs/reviews/*`, `docs/handoff/*`)
keep the old name intentionally — they describe the system as it was.

## 4.80.0 — bug round 3 fixes (app-only) (2026-07-30)

Six of the seven CONFIRMED bugs from the bug-round-3 campaign
(docs/reviews/BUG_ROUND_3_2026-07-30.md); R3-4 is deferred (see below).

- **R3-1 (MEDIUM) — Control Room "nothing to triage" hid a failed spend source.** When
  `FACT_WAREHOUSE_DAILY` failed to load *and* there were no open alerts/task failures, the
  green all-clear showed while the spend-anomaly scan never ran. `sources_ok` now also
  requires `wh_daily.ok`, and a failed read demotes the banner to the incomplete-inputs info.
- **R3-2 (MEDIUM) — query-inspector cache % was off by 100×.** `query_detail` selected
  `PERCENTAGE_SCANNED_FROM_CACHE` raw (0-1) so an 85%-cache query read "1%". Now `* 100 AS
  CACHE_PCT` (matching the sibling read and the mart readers).
- **R3-3 (LOW) — snowsight profile links died for the session on a transient probe.**
  `snowsight_profile_column` cached a *failed* org/account probe (`('','')`), pinning the
  not-None guard and disabling deep links until a reload. A failed probe now returns early
  without caching, so the next rerun re-probes.
- **R3-5 (LOW) — bulk-ACK wrote a false audit row for already-ACK events.** The UPDATE only
  transitions OPEN→ACK, but the audit re-selected any event now in ACK. The audit is now
  scoped to events this actor just stamped (`ACK_BY` + a 2-minute recency window).
- **R3-6 (LOW) — closed-loop reverse-hint mislabeled an auto-suspend fix.** A binary ternary
  fell through to "cluster-range" for auto-suspend changes; the hint now keys off the same
  `fix_kind` branching that builds the statement (three-way, incl. `AUTO_SUSPEND`).
- **R3-7 (LOW) — MTD-vs-prior pace delta counted today's partial day.** The equal-length
  "same days" window included today on the current side (still growing) vs full days on the
  prior side, biasing the delta low. It now compares completed days only (`today.day - 1`);
  the displayed full-month MTD is unchanged.

**Deferred — R3-4 (LOW):** the query-failure counters use `EXECUTION_STATUS = 'FAIL'` (drops
the rare INCIDENT status). The clean fix (`<> 'SUCCESS'`) must change the app live builders
*and* their mart-loader twins together (`FACT_QUERY_HOURLY` counts `= 'FAIL'`) or it creates a
mart-vs-live divergence — so it's bundled into V062 with the loader changes.

New `test_bug_round3` locks all six; 1469 pytest green, ruff+mypy clean.

## 4.79.0 — show First/Last names everywhere a USER_NAME login appears (app-only) (2026-07-30)

Owner ask: the cryptic Snowflake login (`USER_NAME`) rarely says *who* the person is —
show their name too, the way the Cortex AI user attribution already does. Generalized
that treatment into a small reusable resolver and applied it to every login-bearing
surface (mapped exhaustively + completeness-critiqued via multi-agent review).

- **Reusable machinery** — `app/data/directory_sql.user_directory()` (one cached,
  account-global `ACCOUNT_USAGE.USERS` read, deduped by login, metadata tier — a single
  shared cache entry for the whole app), `app/logic/directory` (`display_name_map` /
  `resolve_display` / `attach_display_name`: "First Last" with a login fallback for
  service/dropped accounts — never blank, never invented), and
  `components.with_user_names(df, page)` / `user_display_map(page)`.
- **Applied** across ~30 surfaces: Security (MFA gaps, failed logins, privileged/role
  grants incl. `GRANTED_BY`, new-network logins, expiring credentials, dormant users,
  unload activity, DDL-changes chart + table), Operations (heaviest queries, running-
  queries kill-switch, change registry `CHANGED_BY`), the shared **blast-radius** panel
  shown before every suspend/cancel (Alerts + Optimization), Cost (expensive queries,
  cloud-services-by-user, attribution chart, unit-cost query KPI + table, tag-governance
  KPI + table), Control Room (day-replay DDL/grants, incidents `DECLARED_BY`, members
  `LINKED_BY`), Alerts (events `ACK_BY`, routes `CREATED_BY`), Admin (settings
  `UPDATED_BY`), and Chargeback (dept budgets / department map `UPDATED_BY`). Detail
  tables gain a `User`/`…by` name column beside the login (both stay visible); charts
  and KPIs relabel to the name.
- **Deliberately not touched:** the `OWNER` fields (`ACTION_QUEUE`/`ALERT_CONFIG.OWNER`
  default to the team string `'DBA'`, not a login), the incident-timeline event label
  (login is concatenated into free text), the Unmapped-entities table (its raw login is
  copy-paste-load-bearing for the `COMPANY_SCOPE` INSERT), and aggregate distinct-user
  counts. `ACCOUNT_USAGE.USERS` can lag ~2h, so a just-created login briefly shows raw.

New `test_user_directory` locks the resolver, the shared helpers, and a coverage guard
that every page rendering a login column wires the resolver; 1464 pytest green.

## 4.78.0 — V061 migration: AI loader/alert/score/purge correctness (2026-07-29)

Consolidates the deferred loader/mart correctness fixes into `V061` (authored +
lockstepped here; **Joe applies it in Snowsight** — the app never runs it). Scope was
set by the owner to correctness-only (perf loader restructures → V062); the
loader-robustness cluster (B5/B10/B11/B12) and `SP_NOTIFY_WEBHOOK` B9 (ARRAY runtime
semantics that can't be smoke-tested here) are held for a dedicated pass. Built via
`outputs/gen_v061.py` (each proc re-derived from its current definition with
count-asserted needles), byte-compared by `tests/test_v061_ai_loader_alert.py`, and
adversarially verified against the *generated* SQL (4 skeptics, one LOW finding fixed).

- **C5 (data-corruption HIGH)** — the three `SP_LOAD_MARTS_V27` AI arms windowed on
  `CURRENT_TIMESTAMP()` while every other daily arm uses `CURRENT_DATE()`, so the MERGE
  overwrote a complete day with a post-06:45 partial recount once it left the window.
  Day-aligned all three; the migration tail runs `CALL SP_LOAD_MARTS_V27('DAILY', 365)`
  (owner-chosen full-retention heal) to rewrite corrupted `FACT_AI_USAGE_DAILY` rows.
- **C2** — Query-Acceleration credits added to the proc/pipeline attribution sums:
  `SP_LOAD_MARTS_V27` arm-[6] and both `SP_CHANGE_IMPACT_SCAN` arms now
  `SUM(COMPUTE + COALESCE(QAS,0))`, matching the app twin (shipped v4.74.0).
- **C1** — `SP_ALERT_SCAN` `COST_BUDGET_PACE`/`COST_FORECAST_BREACH` price MTD as
  OTHER×compute + AI×AI-rate; `FACT_PLATFORM_SCORE_DAILY` gains `CREDITS_BILLED_AI` and
  `SP_LOAD_PLATFORM_SCORE` populates it. **App follow-up (ships with the app):** both
  score readers emit the column and `scoring.score_history` blends `budget_pct` at the
  AI rate (`CALL SP_LOAD_PLATFORM_SCORE(120)` in the tail backfills history).
- **C6** — seeded `COST_AI_CREEP` + the `[13b]` arm: week-over-week AI-bucket growth at
  the AI rate. A brand-new AI workload (prior week 0) now fires via a finite sentinel
  instead of a NULL ratio silently dropping the row (adversarial-verify hardening).
- **B41** — self-alert block count `17 → 20`; **B33** — `SP_PURGE_FACTS` now purges
  `FACT_OBJECT_COST_DAILY`, `FACT_STORAGE_ACCOUNT_DAILY`, `MART_TASK_NODE_DAILY`.

Lockstep: validate floor → V061, admin `_EXPECTED_MIGRATIONS[61]`, rebuild bundle
regenerated, DEPLOYMENT/README run-lists + heal note, teardown (objects pre-exist).
1458 pytest green, ruff+mypy clean.

## 4.77.0 — perf round 2 T2.1: Control Room live-trio batch + run_mart_first seam (app-only) (2026-07-29)

- **T2.1 — Control Room morning-triage live reads batched.** The three live-tier
  reads on the triage screen (open incidents, incident proposals, triage alerts)
  were issued serially and each re-paid every ~30s in steady state. They now go out
  as one `live` `run_batch` (one round trip, not three), each threaded back via
  prefetch-else-run so its own serial fallback survives if the batch is unavailable
  or a member misses. **Proposals are now fetched only for operators** — the read
  fed an operator-only expander, so non-operators were paying for it every render;
  the batch carries that member only when `_is_op`.
- **`run_mart_first` gains a `preloaded=` seam** (reusable enabler): a section can
  prefetch its independent mart legs in one batch and hand them to `run_mart_first`,
  which still applies `mart_accept` and falls through to the serial mart read on a
  missing/failed prefetch.

Scope note — the remaining T2 first-paint batching (Overview, Contract, and Control
Room's *mart* group) is deferred. After T1.1 those reads sit on the `hourly` tier
(3600s), so batching them helps only the cold first paint (~once/hour/viewer), and
Overview carries an explicit anti-coupling decision (the filter-scoped board must
not share a batch cache with the fixed reads). Those restructures reshape the
hottest pages' whole render flow and warrant a browser first-paint smoke-test, so
they're held for a dedicated pass rather than shipped blind. This release delivers
the one T2 change with clear *steady-state* value (the 30s live trio).

New `test_perf_round2_t1` cases lock the live-trio batch, operator gating, and the
seam; 1443 pytest green, ruff+mypy clean.

## 4.76.0 — perf round 2 T1.4: gate the change-impact drill scans (app-only) (2026-07-29)

- **T1.4 (owner-approved) — Operations change-impact drills stopped auto-scanning.**
  The warehouse drill ran a 28-day WAREHOUSE_METERING + QUERY_HISTORY join on every
  render against the auto-selected first row, and the object drill ran a 28-day
  QUERY/TASK_HISTORY scan against the auto-selected first object — both re-missing
  every ~300s while the section was open, with no user selection. The warehouse
  history now loads only on an explicit row selection; the object history loads on a
  row click or behind a "Load 28-day run history" toggle (the DAG/streams pattern
  already on the page). Both moved to the `historical` tier (sources lag 45min+).
  Owner signed off on the one-click trade for the object drill.

Deferred (owner declined): **T2.4** health_strip retier — kept on the live tier so
new criticals and cross-viewer alert ack/close stay visible within ~30s.

## 4.75.0 — perf round 2 T1 quick wins: hourly tier, CSV keying, alert LIMIT (app-only) (2026-07-29)

First tranche of perf round 2 (docs/reviews/PERF_ROUND_2_SCOPE_2026-07-29.md),
scoped to the two hottest cost/landing surfaces plus two hygiene fixes.

- **T1.1 — hourly cache tier for hourly-loaded facts (Overview + Cost/Spend).**
  The `hourly` tier (3600s TTL) had zero direct adopters, so multi-day FACT_*/MART_*
  reads sat on `recent` (300s) and cold-missed ~12x/hour per viewer. Moved the
  Overview facts (exec board, 45d/150d metering, ML forecast, activity spark, daily
  digest) and the Cost/Spend facts (metering, CS ratio, CS shapes/users, warehouse
  window-vs-prior, warehouse daily, account storage, the 4-member spend batch, and
  unmapped-entities) to `hourly`. Preserved the deliberate exceptions: Overview
  score-inputs stays `recent`, the live WAREHOUSE_METERING fallback stays `recent`.
  (Control Room / Operations facts are deferred to the T2 batching restructure,
  where those reads get regrouped anyway.)
- **T1.6 — CSV prep blob served the wrong table's bytes.** The download-prep key was
  positional (`ow_dlprep_<key>_<seq>`) and `_ow_dl_seq` resets per page, so a page/
  section switch could serve a previous table's bytes under a reused seq — and the
  blob was never evicted. Now keyed by page identity + a content fingerprint of the
  exact frame, kept in one self-evicting session slot, served only on an exact match.
- **T1.9 — `open_alert_events` LIMIT standardized at 500.** Overview (500), Alerts
  (300), and Control Room (100) differed only by the LIMIT baked into the SQL — the
  sole cache-key splitter on the live tier. Unified at 500 (order-safe: the SQL ranks
  severity then RAISED_AT before LIMIT), so the three share one live cache entry, and
  crit/high counts stop truncating; fixed the stale "300 most recent" caption.

Declined after review: **T1.11** (retier the Admin SETTINGS read live→recent) — the
win is micro-credits on a secondary page, and SETTINGS is a deliberately-live
operator-edit surface (audit rule, test_codex_r24) so a concurrent admin sees edits
within 30s; kept live.

New `test_perf_round2_t1` locks the tiers, CSV keying, and LIMIT; 1442 pytest green.

## 4.74.0 — cost review MEDIUMs C2–C12 (app-only) (2026-07-29)

Eleven MEDIUM cost-accuracy and attribution findings from the cost review, all
app-layer. The mart twins (C2 V059/V010 arms, and the C5/C6 AI-loader items) are
queued for V061.

- **C2 — Query Acceleration dropped from proc/CALL/pipeline cost.** The proc,
  CALL, and task-graph attribution surfaces summed `CREDITS_ATTRIBUTED_COMPUTE`
  alone while the query-grain surfaces (the same "measured = compute + QAS"
  contract) added QAS — so proc totals disagreed with the query view and could not
  tie to the Query Acceleration service line. Added the QAS term to all five app
  sites (`procedure_costs_usd`, `call_cost_lookup`, `call_children_costs`,
  `proc_cost_trend`, `graph_daily_costs`); a lock test asserts every attribution
  credit reference carries it.
- **C3 — per-DB storage dropped fail-safe-only databases.** All four per-DB
  storage builders guarded on active bytes only (`HAVING SUM(DB_BYTES) > 0`), so a
  dropped database still in its 7-day fail-safe window (active 0, fail-safe > 0)
  vanished though the panel prices DB + fail-safe. Fail-safe is now in every HAVING.
- **C4 — storage MTD trusted a stalled loader.** `_storage_tab` fell back to live
  only on an empty fact; a partially-loaded fact priced missing days as zero
  storage (the calendar builder divides by days-in-period). Added a coverage guard
  (fall back to live when the fact's latest day is >2 days stale) and a coverage
  caption ("averaged N of M month-to-date days; latest …"), with a warning when short.
- **C7 — contract "consumed" had no coverage guard on the live/Brief path.** The
  live fallback only sees ~365d of history; for a contract older than that, consumed
  was a silent floor while the caption asserted "since contract start". The live
  builder now exposes an *unfiltered* `SOURCE_FIRST_DAY` (retention floor, mirroring
  the mart's r14 #8-safe design — no false gap on a quiet-start contract), and the
  page labels consumed a floor + points at the org balance when coverage is short.
- **C8 — contract burn is credits-only.** Pacing, year projection, and the renewal
  planner extrapolate credit-billed services; storage and transfer draw the same
  dollar commitment. Added the caveat where each claim is made.
- **C9 — chargeback statements are compute-only.** The exported MANIFEST claimed a
  reconciling total with no scope line. Added an explicit "warehouse compute only —
  will not tie out against the full invoice" line to the MANIFEST, a caption by the
  Build button, and corrected the "reconciles by construction" KPI help.
- **C10 — Operations company lens was warehouse-AND-user.** `_query_scope` ANDed
  warehouse and user clauses, so a cross-company user's queries on a warehouse
  vanished from every scope and ALFA+Trexis+UNKNOWN did not partition ALL. Now
  scoped by `COMPANY_FOR_WAREHOUSE` only — the exact FACT_QUERY_HOURLY loader stamp
  the Cost pages use — with a disclosure caption on the Queries tab.
- **C11 — Security → Changes hid account-level and cross-company DDL.** GRANT/REVOKE
  (NULL database) and cross-company DDL failed the actor-AND-object filter. Scoped by
  actor **or** object now (union semantics), with a caption stating the rule.
- **C12 — incident timeline used pre-V044 company labeling.** The 7d/fallback path
  labeled company with `IFF(DATABASE_NAME LIKE 'TRXS%', …)` (mislabeling NULL/non-TRXS
  as ALFA) while the 48h mart path used the evidence-based UDF. Both paths now use
  `COMPANY_FOR_DATABASE(COALESCE(DATABASE_NAME,''))`.

Scope skipped after verification: C5 (AI loader boundary clobber) and C6 (AI alert
coverage) are migration-side → V061. New `test_cost_mediums_c2_c12` locks all app
fixes; 1437 pytest green, ruff+mypy clean.

## 4.73.0 — cost review C1: price AI/Cortex credits at the AI rate everywhere (app-only) (2026-07-29)

Cost review C1 (HIGH): every all-service billed-dollar rollup summed
`CREDITS_BILLED` across all service types and multiplied the mixed total by the
single compute rate ($3.68), so AI/Cortex credits (which bill at
`AI_CREDIT_PRICE_USD`, $2.20 default) were overstated by (compute − AI) × AI
credits. Two surfaces both labeled "billed spend" — the Spend page (already
AI-rate-correct) and the exec KPIs/forecasts/contract pacing — disagreed.

- **Shared readers now carry the split.** `fact_daily_spend`,
  `fact_daily_spend_year`, `health_strip`'s MTD arm, and `compare_billed` emit
  `CREDITS_BILLED_AI` + `CREDITS_BILLED_OTHER` (the proven
  `AI% / %CORTEX% / %INTELLIGENCE%` predicate; NULL service → OTHER/compute).
  The `CREDITS_BILLED` total is unchanged, so credit-space consumers (Brief
  sparkline, contract exhaustion) keep working.
- **New `formulas.blended_billed_usd(other, ai, rate, ai_rate)`** and
  `blended_credit_rate(...)` — the one tested place credits→dollars happens for a
  mixed total, honoring the credits-to-USD-only-in-formulas law.
- **Consumers dollarize with the blend:** Overview (MTD spend, MTD-vs-prior pace,
  month-end projection frame, 3-month backtest), Brief (MTD spend KPI), Cost &
  Contract (year projection legs, renewal planner on one effective blended $/credit
  for burn + remaining + what-if), and Compare (Account-billed KPI). Each falls
  back to the flat rate if a frame predates the split (live fallback / cold cache).
- **Disclosed, not hidden:** the Spend page "why totals differ" expander gains a
  rate-axis bullet; the two seeded budget alerts (pace, forecast) and the ML
  forecast still price the mixed total at the compute rate until their server-side
  rebuild — queued for V061.
- **Verified no missed dollar KPI:** the rate-card model side stays compute-only
  by design (`fact_daily_spend_compute`, triage #6); AI chargeback already prices
  at the AI rate; the platform-score budget input (mart-sourced) and the alert
  legs are mart-side and go to V061.

Scope skipped after verification: `contract_exhaustion` is credit-vs-credit
(credit commitment), never dollarized — no split needed. New
`test_cost_c1_ai_rate` locks the formula, the reader splits, and every consumer;
1426 pytest green, ruff+mypy clean.

## 4.72.0 — bug round 2 batch 2: state/nav/anomaly HIGHs (app-only) (2026-07-29)

Four more HIGH bugs from bug round 2, all app-layer:

- **B4 — morning false anomalies.** All three app anomaly scorers fed today's
  still-growing `FACT_WAREHOUSE_DAILY` row to `flag_anomalies`, so a steady
  warehouse's part-day spend scored as a low outlier and stamped a recurring
  false `SEVERITY=HIGH` in the flagship triage queue every morning. New
  `anomaly.complete_days_only()` drops the current day before scoring (mirroring
  the server twin `SP_ANOMALY_SWEEP`'s `DAY < CURRENT_DATE()`); trend charts keep
  the full frame. Applied in Control Room, Cost→Spend, and Operations→Warehouses.
- **B6 — Jump-to box was a silent no-op.** `consume_pending_navigation` cleared
  `_ow_jump` on *every* rerun, before `_global_jump` could read the pick and fire
  the navigation — so every Jump-to selection was erased on the rerun that
  delivered it. The reset now runs only when a queued jump is actually consumed.
- **B7 — live-inventory DB picks silently reverted.** `init_filters` validated
  `flt_database` against the static company/env tuples, so `DBA_MAINT_DB` and any
  new `ALFA_*`/`TRXS_*` database the SHOW-DATABASES picker offers was un-picked
  next run (and applied saved views lost their DB scope). Now validates with the
  same `classify_databases` rules the picker uses.
- **B8 — cross-profile navigation.** `consume_pending_navigation` wrote
  `_ow_nav_radio` unconditionally, so an EXECUTIVE-profile viewer following an
  Investigate → link to a PERF_/SEC_ page crashed the radio or landed on a dead
  page. The target is now clamped to the viewer's profile pages (fallback Overview).

New `test_bug_round2` locks all four; 1417 pytest green.

## 4.71.0 — bug round 2 batch 1: three dead incident-response write paths (app-only) (2026-07-29)

Bug round 2 (docs/reviews/BUG_ROUND_2_2026-07-29.md) found the top of the queue was
three fully-dead operator write paths — every click failed the r27 executor
allow-list and logged a FAILED audit row. Fixed with owner sign-off on the
allow-list change:

- **B1 — Emergency levers.** The Operations Emergency tab's ALTER PIPE / TASK /
  USER / ACCOUNT-SET levers were refused because the executor allow-list admitted
  only ALTER WAREHOUSE + OVERWATCH DML/CALL. Added the four builder-generated
  prefixes (each lever interpolates only `_ident`-validated identifiers / a
  regex-validated value in `remediation.py`, so the builder is the injection
  defense and the allow-list is defense-in-depth); `ALTER ACCOUNT SET ` — not the
  broader `ALTER ACCOUNT ` — plus the existing interior-`;` guard keep it tight.
- **B2 — running-query kill switch.** `SYSTEM$CANCEL_QUERY` is a `SELECT`, outside
  the write allow-list, so it never ran. Added a dedicated `execute_cancel_query`
  seam: the query id is regex-gated (`^[A-Za-z0-9_-]{1,64}$`) and the exact
  statement is built server-side — no blanket `SELECT` allowance, no operator text
  in the SQL beyond a validated id.
- **B3 — Control Room incident-declare.** The generate-then-run declare opened with
  `SET OW_INC_ID = UUID_STRING();`, refused by the allow-list, so `$OW_INC_ID`
  never existed and both INSERTs failed — the manual declare wrote zero rows.
  The incident id is now generated app-side (`uuid.uuid4()`) and inlined into both
  (already-allow-listed) INSERTs.
- Locks: `test_r27_app` asserts the four prefixes pass while `ALTER ACCOUNT`
  (non-SET) / `ALTER TABLE` / `ALTER SESSION` / `GRANT` stay refused, and the
  cancel seam's id-regex rejects injection; `test_v032_incidents` /
  `test_live_round8` pin the app-side declare id.

## 4.70.1 — verify-round fix-forward: repeat-fingerprints onto the elapsed basis (app-only) (2026-07-29)

The 4.70.0 adversarial verification returned CORRECT on the schema-queued fix, the
alert guard, and the score wiring (regression hunt clean), with one INCOMPLETE:
**`family_repeat_fingerprints` — the *other* reader of `MART_QUERY_FAMILY_DAILY` —
still derived `TOTAL_ELAPSED_HOURS`/`AVG_ELAPSED_SEC` from exec time** while its
live twin uses true elapsed, so the materialization-candidate gate (≥0.5h) and the
"Compute in repeats" KPI silently changed basis depending on which source served.
V060's new column makes it a one-line fix: both metrics now use
`COALESCE(TOTAL_ELAPSED_SEC, TOTAL_EXEC_SEC)` (same degrade as compile-heavy),
docstring + the "exec-time grain" caller label corrected, lock test added.
Cosmetic: the health-strip source label named the wrong freshness object in two
places; admin's V060 note now says "post-V060 rows" for the COMPILE_PCT bound.
Noted for the next migration batch (V061 queue): the efficiency arm's `QUEUED_MIN`
is still overload-only — same class as #11, warehouse grain.

## 4.70.0 — V060 (triage #5/#11 + alert guard) + triage #3 score wiring (2026-07-29)

Closes the metrics triage in full: the two migration-level LOWs, the verify-round
guard, and the deliberately carved-out #3 — the last open findings.

**V060 (migration, one additive column + two proc re-derivations):**
- **#5** `MART_QUERY_FAMILY_DAILY` gains `TOTAL_ELAPSED_SEC` (wall-clock); the qfam
  arm stores it, and `family_compile_heavy` now averages and bounds `COMPILE_PCT`
  on it — exec-only time let COMPILE_PCT exceed 100% for exactly the
  compile-dominated families the view selects. Reader `COALESCE`s to
  `TOTAL_EXEC_SEC` so pre-V060 rows (never re-loaded beyond the trailing 2 days)
  degrade to the old basis instead of dropping out.
- **#11** `FACT_QUERY_SCHEMA_HOURLY`'s `QUEUED_SEC` now includes provisioning-queue
  time (OVERLOAD + PROVISIONING — the `FACT_QUERY_HOURLY` convention), so a schema
  filter no longer silently under-reports the Queued KPI.
- **Guard** `COST_CLOUD_SVC_RATIO`'s metering subquery gains `WAREHOUSE_ID > 0` so
  the `CLOUD_SERVICES_ONLY` pseudo-warehouse (ratio = 100%) can never fire the
  alert — parity with the live CS-ratio builder and every fact writer.
- Derivation law: marts proc from **V059** (V057/V058/V059 fixes byte-proven to
  survive), alert proc from **V056**; byte-restore tests pin both as
  latest + only-the-enumerated-edits.

**Triage #3 (app): the platform score's two dead drivers now fire.**
- The live caller passed 7 of 9 signals, so Stale-telemetry (cap 12) and
  Owner-queue (cap 9) — and their `SCORE_PTS_PER_STALE_SOURCE` /
  `_PER_OPEN_ACTION` SETTINGS — could never take effect: the score read up to
  21 pts high exactly when telemetry was stale or HIGH actions sat open.
- `stale_sources` comes from a new `STALE_SOURCES` arm on the shell-shared
  `health_strip` (same cadence rule as the Control Room freshness board:
  DAILY/METERING sources stale past 30h, hourly past 3h) — rides the existing
  `key="health_strip"` cache entry, zero extra queries on a warm shell.
- `open_high_actions` counts OPEN×HIGH from the ACTION_QUEUE read **hoisted above
  the score**; the Top-actions panel reuses the same result (still exactly one
  `action_queue` read on the page).
- Also: `role_share` (mart) now anchors on `CURRENT_DATE` like its chargeback live
  twin (the verify round's residual same-tile anchor mismatch).

## 4.69.0 — cache-pct scale fix in the repeat-query insights chain (app-only) (2026-07-29)

`PERCENTAGE_SCANNED_FROM_CACHE` is a **0–1 fraction** — now empirically confirmed
on this account's live rows (owner, 2026-07-29: `SELECT PERCENTAGE_SCANNED_FROM_CACHE
… LIMIT 5` returned 0.xx values), settling the scale for three fixes at once (this
one, and retroactively the v4.68.0 #9 CS-drill ×100).

- Two chains treated the raw column as 0–100: the live repeat-query builder
  (`insights_sql.py`, no ×100) and the family-mart reader (`mart27_sql.py:268`
  over V027's unscaled `CACHE_PCT_AVG`). Consumer `flag_repeat_candidates` filters
  `AVG_CACHE_PCT <= 25.0` (percent) — with 0–1 values **every** heavy query passed
  the "cache-poor" filter (a no-op) and the WHY caption printed e.g. "1% cache" for
  a fully-cached query.
- Both read points now scale **×100 at read** — the established pattern from
  triage #9 — leaving the mart loader untouched (no migration). Thresholds and
  `test_insights` fixtures (already 0–100) are unchanged and now correct live.
- Lock test pins both expressions with the empirical-confirmation provenance.

## 4.68.1 — verify-round fix-forward: honest labels for #12/#13 (app-only) (2026-07-29)

The 4.68.0 adversarial verification (7 agents, run against the shipped tree)
returned CORRECT on all SQL: #6/#8/#9/#10 exact (De Morgan complement, measure
parity, ×100-once, single MFA definition) and the anchors verified against every
same-tile feeder. The two findings were **labels lying about verified-correct SQL**:

- **#12 INCOMPLETE → fixed.** Control Room's pulse still said "24h" while its three
  feeding builders are now midnight-anchored (days=1 = yesterday 00:00 → now,
  24–48h). Relabeled: "Queries (since yday)" + help note, source label, empty-state,
  comments, docstring. No SQL change.
- **#13 DEFECTIVE → fixed.** The 4.68.0 rewording claimed Overview "reads at or
  below" the rebate-netted warehouse figure, "never equal" — provably inverted at
  company=ALL, where Overview prices UNADJUSTED usage and reads *above* it by ~the
  rebate. (The triage doc's own prescription encoded the wrong direction — corrected
  there too.) New wording states the different basis and both directions, and stops
  listing "reader" in the remainder (it sits inside the warehouse figure).
- The `%AI%` lock now scans **every** `app/data` SQL-builder module (the 4.68.0
  test covered only the two changed files while the changelog claimed more).

## 4.68.0 — metrics-triage MEDIUM app batch + AI-prefix fix (app-only) (2026-07-29)

Six MEDIUM fixes from the metrics triage (docs/reviews/METRICS_TRIAGE_2026-07-29.md)
plus the AI-service-prefix chip fix, all app-only. **#3 (platform-score wiring) is
deliberately carved out** to its own follow-up — it moves the headline exec score by
up to 21 points and needs a stale-source *count* that no current read exposes.

- **#6 rate-card compute-only.** The model side of the rate-card reconciliation
  (`contract.py`) now uses a new `fact_daily_spend_compute` builder (with canary),
  excluding AI/Cortex so it compares like-for-like against org `COMPUTE_USD` —
  Cortex credits priced at the compute rate were biasing `DELTA_PCT` up and the
  caption invited "fixing" the global rate. The AI exclusion uses the **prefix**
  `'AI%'`: the contains-form would drop `SNOWPARK_CONTAINER_SERVICES` (cont**AI**ner),
  which the org buckets under `RATING_TYPE='COMPUTE'`.
- **AI-prefix everywhere (chip fix).** The same `%AI%` contains-trap existed in
  `fact_cortex_daily_spend` (mart) **and its live twin** in `cost_sql` — both would
  misprice container-services compute as Cortex. Both now use `'AI%'`, changed
  together to preserve mart-vs-live parity; a lock test bans `ILIKE '%AI%'` from the
  SQL builders repo-wide.
- **#8 CS-ratio parity.** `fact_cloud_services_ratio` now excludes the
  `CLOUD_SERVICES_ONLY` pseudo-warehouse and floors near-idle warehouses at 0.5
  credits, matching the live builder — no more phantom 100%-CS ELEVATED row sorted
  first and spuriously triggering the compile-heavy drill.
- **#9 CS "Cache %" ×100.** `cloud_svc_top_shapes` scales the 0–1 cache fraction to
  percent — previously every row rendered 0% or 1%.
- **#10 MFA single definition.** Both Access-panel builders now use `HAS_MFA`
  (matching `governance_counts`); the Duo-specific `EXT_AUTHN_DUO` false-positived
  users with native (non-Duo) MFA.
- **#12 window anchors unified.** `fact_query_window_summary` and
  `schema_window_summary` anchor on `CURRENT_DATE` like their live twin, so the same
  labeled tile covers the same span whichever source serves it.
- **#13 honest label.** The "why totals differ" expander no longer presents the
  account-wide, rebate-netted warehouse total as Overview's company-scoped KPI.
- Chips filed during verification for two adjacent latent bugs (being handled
  next): the 0–1 cache fraction vs 0–100 thresholds in the repeat-query insights
  chain (needs one live row to confirm scale).

## 4.67.0 — V059: triage HIGH #2 — task-graph pipeline credits (2026-07-29)

Metrics-triage HIGH #2. `MART_TASK_GRAPH_DAILY.WH_CREDITS` (pipeline spend /
$-per-run) read **~$0 for every proc-driven task**. `SP_LOAD_MARTS_V27` arm [6]
rolled `QUERY_ATTRIBUTION_HISTORY` up by the **bare QUERY_ID** and joined
`a.QUERY_ID = h.QUERY_ID`, but a task whose body is a stored procedure attributes
its compute to CHILD statements that carry the CALL id only as `ROOT_QUERY_ID` —
so the bare-id join matched only the ~0-credit CALL row.

- The live twin (`graph_sql.graph_daily_costs`) got this fix in v4.60 (audit #10);
  the default-**served** mart never did, so the mart under-reported while the live
  fallback was right. V059 re-derives arm [6] to mirror the live builder: roll up
  by `COALESCE(ROOT_QUERY_ID, QUERY_ID)`, prune on the same coalesced id, and join
  `a.ROOT_ID = h.QUERY_ID`. Four token edits; every other statement byte-identical
  (V057's FAIL fixes and V058's per-node arm [6b] preserved — proven by a
  byte-restore test), and a test asserts the mart rollup matches the live twin.
- Lockstep: `validate.sql` + admin `_EXPECTED_MIGRATIONS` to 59; rebuild bundle
  `V001_V059`; run-lists + runbook `DEPLOY_V059_20260729.md`. Also added a generic
  validate.sql-floor test that auto-tracks the latest migration (no more pinning
  the moving floor per-migration). No schema change, no new object.

## 4.66.0 — triage HIGH #1: month-end projection uses the full-month frame (app-only) (2026-07-29)

Metrics-triage HIGH #1 (docs/reviews/METRICS_TRIAGE_2026-07-29.md). The Overview
**Projected month-end** KPI was fed the exec-board `DAILY_SPEND` frame, which is
windowed to the filter `days` (default 7) — so past ~day-8 of the month it summed
month-to-date over a single week and understated the projection ~60–70% (it could
even read below the account-wide MTD KPI beside it). The board frame is also
company-scoped, while the Projected-month-end / MTD KPIs are account-wide.

- Now projects from a **`proj_daily`** frame built from the account-wide 150d
  `_bt_hist` already loaded for the MTD KPI and the backtest (zero extra reads):
  full-month MTD base **and** account-wide scope, matching the KPI beside it and
  the forecast backtest below it. The `ml_forecast` branch's MTD uses the same
  frame. Falls back to the board `daily` frame only when the 150d mart read fails.
- The exec-board `daily` frame still drives the company-scoped spend sparkline and
  window total — only the forecast/MTD-base moved to the account-wide frame.
- App-only, no migration. New `test_metrics_triage_fixes` lock; 1380 pytest green.

## 4.65.0 — compact triage-filter toolbar (app-only) (2026-07-29)

The global filter strip above every page took ~5 stacked bands. Rebuilt as a
compact toolbar (owner-chosen direction): the "Triage filters" kicker and the
Legend / Views / Reset actions now share one thin header row, then the four scope
controls, then "More filters" — cutting roughly 55–60% of the strip's height.

- **Dropped the redundant telemetry caption** — "Telemetry ≤ Xh old" duplicated
  the Telemetry-age card in the status bar right below it.
- **Dropped the active-scope chip band** — the chips (`ALFA · 30d …`) restated the
  visible controls. The active-filter signal is preserved by the strip's **border
  glow** (kept) plus the existing behavior that a live warehouse/user/schema filter
  **auto-opens "More filters,"** so a hidden filter can never go unseen.
- **Reset** and the per-control clarity (e.g. Environment's "narrows the Database
  picker only" help) are unchanged; `_scope_chips` and its scope-only CSS
  (`.ow-scope-chips`/`.ow-chip-accent`/`.ow-chip-dot`) were removed. The `chip()`
  helper's styling (status-bar pills) is untouched.
- App-only, no migration. `test_design_system` re-pinned to the compact layout;
  1379 pytest green, ruff + mypy clean.

## 4.64.0 — V058: per-node loader-timing observability (perf T3) (2026-07-29)

Opening the **reconcile-scheduling** cluster (T3): a multi-agent design + an
adversarial loader-safety critique both concluded that **none** of the actual
schedule changes — serializing the 5-way daily fan-out, retargeting the nightly
reconcile, de-colliding the 06:30–06:50 root tasks — can be sized safely from code
alone. `MART_TASK_GRAPH_DAILY` keeps only per-*pipeline* wall time and discards
`SCHEDULED_TIME`, so the per-node queue delay and exec duration those changes need
don't exist anywhere. Even a blind cron stagger just relocates a multi-minute
overlap into another task's window. So T3 leads with the **measurement**; the
schedule changes become data-driven fast-follows once a few mornings accumulate.

- **New `MART_TASK_NODE_DAILY`** (grain DAY × DATABASE × SCHEMA × TASK_NAME):
  `RUNS`, `FAILED`, `AVG/P95/MAX_QUEUE_SEC` (the `SCHEDULED_TIME→QUERY_START_TIME`
  dispatch delay that quantifies the XSMALL contention), `AVG/P95/MAX_EXEC_SEC`,
  `FIRST_START`, `LAST_COMPLETED`, `LOAD_TS`.
- **Contained, additive loader change.** `SP_LOAD_MARTS_V27` is re-derived **from
  V057** (preserving its four `EXECUTION_STATUS='FAIL'` fixes) with exactly one new
  standalone guarded arm `[6b]` after the task-graph arm — a single extra
  `TASK_HISTORY` scan at the same `-:d` window, MERGE-keyed on the grain for
  idempotency, in its own `BEGIN…EXCEPTION` so a fault can't reach the other arms.
  A lock test proves the proc equals V057 byte-for-byte **plus only** that arm.
- **No task-graph surgery.** No `SCHEDULE`, no `AFTER` edge, no `SUSPEND`/`RESUME`,
  no `CREATE/ALTER TASK` — `CREATE OR REPLACE PROCEDURE` needs no task suspended,
  so the unattended loader's graph is provably untouched (the runbook has Joe diff
  `SHOW TASKS` before/after to confirm). The table fills on the existing hourly
  cadence, which reaches back over the morning's 06:40/06:45 runs.
- **Deferred, honestly:** per-node **credits** (arm [6] carries pipeline-grain
  credits — though the metrics triage later found *those* were themselves
  under-counted for proc tasks, fixed in **V059**) and the three schedule changes
  (A serialization, B reconcile retargeting, C de-collision) — all now unblocked by
  this data.
- Lockstep: `validate.sql` + admin `_EXPECTED_MIGRATIONS` to 58; rebuild bundle
  `V001_V058`; teardown + run-lists extended; runbook `DEPLOY_V058_20260729.md`.

## 4.63.0 — performance round, #15: Cost first-paint batching (app-only) (2026-07-29)

The default **Spend & Attribution** section fired ~10 serial blocking mart reads
on first paint, top-down, none batched. Now the four independent `tier="recent"`
reads that gate the eager Spend + Attribution panels submit as **one parallel
`run_batch`**, and the below-fold Storage + Unmapped detail is deferred behind a
toggle — so a warm first paint pays one parallel group plus the two window-
dependent allocation reads (~3 round-trips) instead of ~10 serial ones.

- **One recent batch feeds Spend + Attribution.** `cost.py` submits
  `metering` / `csr` / `wh` / `daily` (a new `_spend_attr_recent_jobs` spec-builder
  in `spend.py`) as a single `run_batch(tier="recent")`; the results thread into
  `_spend_tab` / `_attribution_tab` as keyword args. Each panel keeps its exact
  mart-first→live-fallback: a `None`/empty/failed prefetch (batch unavailable or a
  member miss) still triggers that panel's own serial mart and live-fallback read,
  so no honesty label or degrade path is lost.
- **Storage + Unmapped deferred behind one toggle** (`cost_spend_detail`, default
  off) — 3 storage reads + 1 unmapped read leave the default first paint. A toggle,
  not `st.expander` (which still executes its body every rerun), is what actually
  defers the reads. Attribution stays eager (it's half the section name and its two
  reads ride the batch at near-zero marginal cost).
- **Kept separate, deliberately:** the two allocation reads (different `hourly`
  tier + a `window_usd` control-flow dependency) and the cloud-services drill
  (already toggle-gated in Tranche 1). Deferred as their own scope items: splitting
  the batch cache boundary from parallelism (#12) and bounding batch concurrency
  (#13) — a 4-member batch is well under the XS concurrency limit.
- App-only, no migration. Locked by a new `test_perf_pass` assertion; all existing
  cost/perf/design tests unchanged and green.

## 4.62.0 — V057: FAILS token fix (silent-0 failure counts) (2026-07-29)

Opening the loader-SQL cluster for the performance round's Tranche 2 (credit
savings) surfaced a **correctness** bug that outranks every perf item there — so
Tranche 2 leads with the fix. The perf items in that cluster are all confirmed
LOW (WMH is tiny, the ACCOUNT_USAGE views are small, freshness aggregation is
metadata-only), so they are deferred rather than bundled.

- **Four mart failure counts were a constant 0.**
  `SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY.EXECUTION_STATUS` takes the values
  `success` / `fail` / `incident`, and `OW_QH_EXTRACT` copies it verbatim, so a
  failed query is stored as `'FAIL'` — never `'FAILED'`. The primary query facts
  follow that convention (`FACT_QUERY_HOURLY` / `FACT_QUERY_DAILY` / `OPS_DIAG`
  all use `= 'FAIL'`), but four HOURLY arms of `SP_LOAD_MARTS_V27` counted
  failures with `EXECUTION_STATUS = 'FAILED'`, which never matches. So the `FAILS`
  column was stuck at 0 in **MART_WAREHOUSE_EFFICIENCY_DAILY**,
  **MART_QUERY_FAMILY_DAILY**, **FACT_QUERY_ROLE_HOURLY** and
  **FACT_QUERY_SCHEMA_HOURLY**. V057 re-derives `SP_LOAD_MARTS_V27` from V056 with
  exactly the four `'FAILED'` → `'FAIL'` swaps and nothing else (byte-compared).
  The task-graph arm's `STATE = 'FAILED'` predicates are `TASK_HISTORY` states —
  genuinely `'FAILED'` — and are deliberately left untouched.
- **Paired app fix.** `change_impact_sql.warehouse_daily_series` (the per-warehouse
  change-rule chart, live `QUERY_HISTORY`) had the same dead token; it now uses
  `<> 'SUCCESS'`, matching its sibling proc-impact builder in that file (which also
  captures `incident`). A regression test (`test_v057_fail_token.py`) locks
  `app/` against the dead `EXECUTION_STATUS = 'FAILED'` token reappearing.
- **How the data heals.** Forward loads self-correct on the next hourly cycle; the
  nightly reconcile repairs the trailing days. To correct older history in one
  pass, re-run the marts for a wider window — steps in
  `docs/handoff/DEPLOY_V057_20260729.md`, which also carries a one-line Snowsight
  pre-flight to confirm the `{SUCCESS, FAIL, INCIDENT}` domain before applying.
- Lockstep: `validate.sql` floor and admin `_EXPECTED_MIGRATIONS` to 57; rebuild
  bundle `V001_V057`; `DEPLOYMENT.md` / `README.md` run-lists extended. No schema
  change, no new object.

## 4.61.0 — performance round, Tranche 1 (app-only) (2026-07-29)

First tranche of the performance round (scope: docs/reviews/PERF_ROUND_SCOPE_2026-07-29.md).

- **#9 (high) — cache scope keyed every read by the viewer, causing a live-scan
  stampede.** `_cache_scope` baked the viewer's name into every cache key, but
  account-wide reads (marts, ACCOUNT_USAGE) return the same rows for every viewer
  under owner's-rights SiS (role governs visibility, not the person). So on a mart
  outage, N concurrent viewers each cold-missed the live fallback and re-ran the
  same account-wide (~50 GB) scan on the one XS warehouse. Now keys account-wide
  reads by ROLE only, folding in the viewer ONLY for user-specific reads
  (`USER_PREFS` / `CURRENT_USER()`). A focused leak-safety trace confirmed the
  sole per-viewer read (`USER_PREFS`) is isolated by the viewer literal in its own
  SQL text, so role-only scoping serves the fleet from one cache entry without any
  cross-viewer leak.
- **Cloud-services drill-in gated behind a toggle.** The V055 shape/user
  breakdown fired two `MART_CLOUD_SVC_DAILY` reads on every Spend & Attribution
  first paint; it's now an opt-in toggle (the per-warehouse ratio table stays
  live), removing both reads from the default path.
- Re-scoped honestly after verifying against current code: #11 Unit-costs
  leaderboards are already `run_batch`-parallelized (and are the section's primary
  content, so not gated); #16 Overview's board is deliberately kept off the KPI
  batch for cache-coherence (documented); #15's Cost first-paint batching is a
  real win but a moderate `_spend_tab` restructure — carried to a focused
  follow-up rather than rushed here (and note: `st.expander` still executes its
  body every rerun, so "defer" needs a toggle, not just collapsing).

## 4.60.0 — audit #10 + #15 confirmed and fixed (app-only) (2026-07-29)

The two PLAUSIBLE audit items, adjudicated (prosecutor + defender per finding —
all four agents landed on real-bug) and fixed. No migration.

- **#10 task-graph pipeline cost read ~$0 for proc-driven tasks.**
  `graph_daily_costs` aggregated `QUERY_ATTRIBUTION_HISTORY` by `QUERY_ID` and
  joined `a.QUERY_ID = h.QUERY_ID`, where `h.QUERY_ID` is the task's root `CALL`
  statement. Snowflake attributes a stored procedure's compute to its CHILD
  statements (which carry the CALL's id only as `ROOT_QUERY_ID`), so the join
  matched only the ~0-credit CALL row — every `CALL proc()` ETL task (the common
  pattern) showed ~$0 measured credits. Now rolls up via
  `COALESCE(ROOT_QUERY_ID, QUERY_ID)` (the same rollup `insights_sql` already
  uses; the builder's own docstring already described this intended behavior).
- **#15 compare "same N days" pairing produced unequal windows.** `period_pair`'s
  MTD escape hatch clamped only window B to the prior-month boundary, so on a
  month-end after a shorter prior month (Mar 31 vs Feb, May 31 vs Apr) window A
  was longer than B yet labeled "same N days" — inflating the month-over-month
  delta by a day of spend. Now caps BOTH sides to `min(n, prior-month length)`,
  honoring the docstring's "equal-length windows or nothing" contract with a
  truthful label. (Distinct builder from the formulas.py MTD fix in Batch A #8.)
- This closes the 19-finding audit in full (7 high, 10 medium, 2 low — 15
  confirmed + 4 plausible, all now confirmed and fixed).

## 4.59.0 — audit Batch B: loader / reconcile / alert correctness (V056) (2026-07-29)

The migration half of the audit (docs/reviews/BUG_AUDIT_2026-07-28.md) — six
confirmed bugs in stored procs, all derivation-law re-derivations, no schema
change, no new object.

- **#6 FACT_QUERY_DAILY partial-freeze.** `SP_LOAD_QH_EXTRACT`'s day-fact MERGE
  read a rolling 72h window and overwrote the oldest calendar day with a
  shrinking partial, freezing every aging day at ~its last hour (~95% undercount)
  — feeding the exec-board query-volume windows. Day-aligned to `-2 CURRENT_DATE`
  (the same fix shape as the V055 cloud-services mart).
- **#7 nightly-reconcile D-3 damage.** `SP_NIGHTLY_RECONCILE` deleted the
  query/extract-sourced day marts at `-3d` then rebuilt them from the 72h extract
  via `SP_LOAD_MARTS_V27('HOURLY', 3)`, so day D-3 rebuilt ~28% short every night.
  Fix: cap only the four **extract-fed** day arms of `SP_LOAD_MARTS_V27` at
  `LEAST(:d, 2)` (the extract holds at most 2 whole days complete; the normal
  hourly `d=2` call is unchanged), and delete those marts in the reconcile at
  `-2d`. The reconcile keeps `d=3`/`-3d` for the marts it can rebuild D-3
  complete — the full-retention marts (`MART_WAREHOUSE_EFFICIENCY_DAILY`,
  `MART_TASK_GRAPH_DAILY`, which needs it for late-arriving attribution), the
  hour-grain marts, and the primary metering facts. (Adversarial verification
  caught the first cut wrongly dropping the two full-retention marts to `d=2`.)
- **#14 ops-diag hour double-count.** `SP_LOAD_OPS_DIAG` deleted on hour-truncated
  `HOUR_TS` with a non-hour-aligned bound while re-inserting a partial boundary
  bucket → a complete + partial row the panels double-counted. Both bounds now
  hour-aligned.
- **#4** PIPE_TASK_FAILURES dedupe key gains `SCHEMA_NAME`. **#12** SEC_CRED_EXPIRY
  dedupe key gains an EXPIRED/EXPIRING discriminator so the CRITICAL escalation
  isn't deduped against the earlier same-week warning. **#13** COST_CLOUD_SVC_RATIO
  company via `COMPANY_FOR_WAREHOUSE` (UNKNOWN warehouses no longer bill ALFA).
- Process: heavy adversarial verification of a loader migration (the #7 fix
  hinges on the hourly task using `d=2`, verified against V027). Lockstep to 56;
  bundle `V001_V056`; deploy runbook `DEPLOY_V056_20260729.md`. This closes the
  audit except the two PLAUSIBLE items (#10 task-graph proc-internal cost, #15
  compare partial-month) still queued for verify-first.

## 4.58.0 — audit Batch A: 11 app-only correctness fixes (2026-07-28)

A full multi-agent bug audit (find -> adversarially verify, 16 agents) surfaced
19 confirmed bugs across all metrics/sections
(docs/reviews/BUG_AUDIT_2026-07-28.md). This ships Batch A — the app-only fixes,
no migration. Each is now pinned by a regression lock.

- **#2 (high) — the 180/365 window finally reaches the pages.** `exec_board()`
  clamped `days` at the live-scan default (90), so selecting 180/365 silently
  read the 90-day board rows under a long-window label. Adversarial verification
  found the same footgun in **~13 sibling mart readers** fed by the window filter
  (metering-by-service, query/task/warehouse windows, cloud-services ratio, the
  CS drill-down, Cortex spend, alert history/MTTR, app usage, the forecast
  backtest #11). All now `bounded_days(days, MAX_MART_WINDOW_DAYS)`;
  `fact_warehouse_window_vs_prior` (which scans `2*days`) caps at half the mart
  max so it never over-reads the empty prior half as a false swing.
- **#1 (high) — resize savings were dead code.** The estimate branch gated on
  `startswith("DOWN")` but the recommendation is "Size down candidate" ->
  "SIZE DOWN ...", so every downsize booked $0. Gate fixed.
- **#3 (high) — chargeback scoped by warehouse NAME, not the COMPANY column.** A
  COMPANY_SCOPE-mapped warehouse whose name didn't match `WH_ALFA_%` was dropped
  from the total (and never surfaced as UNKNOWN). Now filters the fact's
  evidence-based `COMPANY`.
- **#5 (high) — the query-fail recheck matched the alert.** It computed an
  account-wide, since-midnight rate off raw QUERY_HISTORY while the alert is
  per-company, 24h, off FACT_QUERY_HOURLY — so the drawer could read "clear"
  while the flagged company was still failing. Recheck now takes the event's
  company and matches the alert exactly.
- **#8 — MTD "(same days)" pace delta** now compares equal day counts (the
  displayed MTD stays the true full month-to-date). **#9** — ALFA storage
  includes `DBA_MAINT_DB` (aligned with `classify_database`). **#16** — the
  platform-score trend is labeled account-wide (its fact has no company grain).
  **#17** — an AI budget breach is judged on the user's total across sources
  (was per source, so split spend never flagged Critical). **#18** — cache
  hit-rate denominator is successful queries. **#19** — storage monthly average
  divides by days-in-period, not days-with-a-row.
- Batch B (loader partial-freeze/reconcile #6/#7/#14, alert dedupe/classification
  #4/#12/#13 — one migration) and the two PLAUSIBLE items (#10/#15) remain queued.

## 4.57.0 — cloud-services ratio: drill to the query shapes & users (2026-07-28)

Owner: a `COST_CLOUD_SVC_RATIO` alert sits >21% on two warehouses; "track the
components causing the spike to lower costs." The Spend page already showed the
ratio, the rebate, compile-heavy families, and CS-by-type — what was missing was
a persisted shape/user breakdown (a metadata storm of tiny queries never
surfaces in the compile-heavy view).

- **Per-query cloud-services credits persisted (V055).** `OW_QH_EXTRACT` gains
  `CREDITS_USED_CLOUD_SERVICES` (additive — the extract already scans
  `QUERY_HISTORY` once, so no new scan). `SP_LOAD_QH_EXTRACT` re-derived (from
  V042) to fill it and cascade a **guarded, isolated** `SP_LOAD_CLOUD_SVC_MART`
  (a mart failure logs and never breaks the extract or its watermark).
- **`MART_CLOUD_SVC_DAILY`** — CS credits at day / company / warehouse / user /
  role / query_type / parameterized_hash grain (CS>0 only), MERGE-accumulated
  from the extract like `FACT_QUERY_DAILY`. Retention trimmed by a re-derived
  `SP_PURGE_FACTS` (from V054) on the daily floor.
- **App (mart-backed, no live scan).** Spend → "Cloud-services health by
  warehouse" gains a **warehouse picker** and two tables — top query shapes by
  CS credits (RUNS / CS-per-1k / avg exec / cache% — the triage that names the
  fix) and CS by user/role — for **any** warehouse, not only ELEVATED.
- **Recheck fixed.** The `COST_CLOUD_SVC_RATIO` alert-drawer recheck was
  account-wide (`METERING_HISTORY`); it now matches the per-warehouse alert
  (`WAREHOUSE_METERING_HISTORY`, CS / total credits, filtered by the warehouse),
  and returns no answer rather than a misleading account number when no
  warehouse is in the event.
- Correction on record: the drill-down basics already existed; this round adds
  only the genuine gaps (shape/user grain, warehouse-selectable, warehouse-aware
  recheck), not a rebuild. 5-lens adversarial verification before ship; new
  `outputs/gen_rebuild_bundle.py` already in place. Lockstep to 55; bundle
  `V001_V055`; deploy runbook `DEPLOY_V055_20260728.md`.

## 4.56.0 — exec-board long windows actually read long history (2026-07-27)

Owner: ran a fresh Codex analysis, "review and make recommendations" → after
adjudication, "Build V054." The headline was a CONFIRMED regression I shipped in
V052: the Executive Board advertised 180/365-day windows but read only 90.

- **The bug (Codex r29 #1).** V052 added 180 and 365 to the exec-board loader's
  `windows` CTE, but the three source CTEs it reads from
  (`wh_daily`/`qh_daily`/`tk_daily`) still capped at 90 days
  (`WHERE DAY >= DATEADD('day', -90, CURRENT_DATE())`). The window join reads
  those already-capped frames, so the 180/365 pills computed over only 90 days —
  identical to the 90-day row. A silent lie; I re-derived only the windows CTE in
  V052 and never checked the source horizon the windows read from.
- **The fix (V054).** Re-derive `SP_REFRESH_EXEC_BOARD` from V052 with the three
  source horizons `-90 → -365` (= max `DAY_WINDOW_OPTIONS`). Sources are
  `FACT_*_DAILY` (mart, long retention), so this is history, not a live rescan;
  the aggregate-once/join-small shape is unchanged. The migration re-`CALL`s the
  board so the pills carry real data now.
- **Companion (Codex r29 #20).** Raise the `SP_PURGE_FACTS` daily-fact retention
  floor `180 → 365` so retained history always covers the widest advertised
  window (a runtime no-op under the default 800-day setting; it only tightens the
  guarantee). New STANDING CI invariant
  (`tests/test_v054_window_history.py`): every effective exec-board source
  horizon AND the retention floor must be ≥ `max(DAY_WINDOW_OPTIONS)` — the check
  that would have caught #1 (V052's test compared window *names* only).
- **Codex r29 #7 declined — already fixed.** The QH watermark advance the
  reviewer flagged (V041) was re-derived in V042 (r22 #7) to gate the watermark
  MERGE behind an `ok` flag set only on the extract arm's COMMIT; that reviewer
  cited the superseded V041 body. The rest of r29 is triaged in
  `docs/reviews/CODEX_REVIEW_ADJUDICATION_r29_2026-07-27.md`.
- Process: adversarial multi-lens re-verification before ship (the discipline
  that catches the silent kind). New reusable `outputs/gen_rebuild_bundle.py`
  regenerates the rebuild bundle. Lockstep to 54; bundle `V001_V054`; deploy
  runbook `DEPLOY_V054_20260727.md`.

## 4.55.0 — action layer: procs dropped, P1-A closed safely (2026-07-27)

Owner: "build V053a" -> after three review rounds, "drop the proc; close P1-A
app-side." The action-layer remediation and verify procs were built to the
signed-off V053 design, then abandoned — adversarial re-verification kept
finding that free-text SQL under EXECUTE AS OWNER is not robustly securable by
string checks. What ships is the safe residue that was the actual goal.

- **Both action procs dropped.** SP_EXECUTE_REMEDIATION failed three rounds:
  owner-privileged injection (V051 draft) -> dead happy path + FAILED-as-success
  + audit-rolled-back-on-booking-failure + over-broad allow-list -> the tightened
  allow-list is still a substring check a trailing comment defeats
  (`ALTER TABLE ... DROP COLUMN -- SET DATA_RETENTION_TIME_IN_DAYS`). Not
  reachable through the app (P_STMT only ever comes from clean builders), but an
  owner-privileged gate that can be bypassed does not ship. SP_VERIFY_SAVINGS had
  the parallel problem (operator-supplied proof runs owner-privileged). The
  robust alternative — typed parameters, statement constructed inside the proc —
  is a fresh round, not a patch. Remediation/verify stay on today's already-safe
  path (typed confirmation + operator gating + clean builders).
- **V053 ships the safe, valuable residue (Codex P1-A).** Typed SAVINGS_LEDGER
  columns (FINDING_TYPE/TARGET_OBJECT + proof-evidence columns) + the monthly
  verifier re-derived from V007 (two edits) to select
  FINDING_TYPE='AUTO_SUSPEND' rows, not just the legacy DESCRIPTION LIKE that no
  executed path booked. The app stamps FINDING_TYPE/TARGET_OBJECT on its
  EXISTING (already-guarded) resize/retention/auto-suspend ledger inserts, so
  the verifier finally finds them. No stored procedure, no owner-privileged
  execution, no free-text SQL crossing a trust boundary.
- Process: three adversarial-review rounds gated this — the exact mechanism that
  caught the original injection. The design doc (ACTION_LAYER_V053.md) records
  the attempt and why the procs weren't shippable; test_v053a pins that no
  action proc ships and that the app closes P1-A by typing its own inserts.
  Lockstep to 53; bundle V001_V053; deploy runbook DEPLOY_V053_20260727.md.
  V051's shipped alert-lifecycle proc is untouched.

## 4.54.0 — long-history window filter (180/365) (2026-07-27)

Owner: "expand the triage filters to 180 and 365 days" → "mart-history only" →
"the only live feature where we'd want 180/365 is Cortex Usage user costs".

- **Window filter gains 180 and 365.** DAY_WINDOW_OPTIONS = (7,14,30,60,90,
  180,365). The picker discloses the split: beyond 90 days, only mart-history
  panels and Cortex user costs honor the window; other live scans cap at 90.
- **V052 (forward-generated, byte-locked):** re-derives SP_REFRESH_EXEC_BOARD
  from V045 with one edit — the windows CTE gains 180/365 — so Overview's KPI
  board has rows for the new options. A test-lock requires the board's window
  set to equal DAY_WINDOW_OPTIONS (a window with no board row falls through to
  a 13-month live scan), so the config change and the migration are coupled by
  construction. Proc swap + one board reload; no new objects.
- **The 90-day live guardrail stays.** MAX_LIVE_WINDOW_DAYS = 90 still bounds
  expensive QUERY_HISTORY-scale scans; new MAX_MART_WINDOW_DAYS = 365. Raised
  to honor 365: the mart-backed storage-by-database and department-window
  chargeback builders (facts have 400–800d retention), and — the owner-named
  live exception — the Cortex user-cost readers (cortex_code_user_rollup /
  cortex_code_daily; per-user token telemetry is low-volume, cheap to scan
  long, unlike QUERY_HISTORY). Every other live builder still clamps at 90.
- Locks: test_v052_windows pins the byte-derivation, board-windows==config,
  the live cap unchanged, mart+Cortex honoring 365, and the filter-strip
  disclosure. The V041 board-window lock now asserts V041 froze its era's five
  windows; the config-match check lives with the effective proc. Lockstep:
  validate + _EXPECTED_MIGRATIONS to 52; rebuild bundle V001_V052;
  DEPLOYMENT/README. Deploy runbook: docs/handoff/DEPLOY_V052_20260727.md.

## 4.53.0 — Tranche C: action layer (scoped after review) (2026-07-27)

Owner: "tranche C" → after adversarial review, "ship the correct slice". A
design-phase subagent auto-wrote a full 4-proc action layer; it was NOT
committed. Three independent verifiers reviewed it and found, in procs that no
app path called, an owner-privileged SQL injection (a stored PROOF_SQL built by
string-concatenating a target, later run under EXECUTE AS OWNER) and an
over-broad allow-list (ALTER USER / ACCOUNT / SESSION). The owner scoped V051
down to the one wired, verified path.

- **V051 (hand-authored — re-derives nothing, so no generator):**
  `SP_ALERT_LIFECYCLE` turns alert ack/resolve into ONE set-based transaction
  (audit + update), replacing the app's split UPDATE-then-audit that could
  partially succeed; `OW_ACTION_INTENTS` dedups a re-submitted action by key.
- **Draft bugs fixed in the shipped proc:** audit now runs BEFORE the update on
  the same pre-state filter (the draft audited by post-update status, spuriously
  re-auditing already-ACK/RESOLVED events); `SPLIT_TO_TABLE` values are TRIM-ed;
  an empty/NULL idempotency key returns BLOCKED instead of hitting a PK
  violation; the `AT` reserved-keyword column is `CREATED_AT`.
- **Idempotency stated honestly:** sequential-retry dedup only — Snowflake
  PRIMARY KEY is informational (no lock), and the alert transitions are
  idempotent anyway; full concurrency control was judged over-engineering for
  the ~2-DBA writer population.
- **App wiring (proc-first, legacy fallback):** `execute_action()` in query.py
  CALLs the proc and reads its OK/DUPLICATE/BLOCKED verdict, falling back to the
  v4.52 statements if the proc isn't deployed; `idempotency_key()` in
  identity.py keys on the testable `account_now()`. Alert single + bulk
  lifecycle route through it. Nothing changes behavior until V051 is applied.
- **Deferred (their own hardened round):** the remediation / verify-savings /
  action-queue procs + the typed savings link — they need app wiring, an
  identifier-validated (not concatenated) proof, an allow-list narrowed to
  warehouse/pipe/task/cancel, and per-proc row-affected checks. See the design
  doc. The v4.51 caption fixes already removed the false verifier-settles
  promises, so nothing in the app over-claims in the meantime.
- Lockstep: validate.sql + _EXPECTED_MIGRATIONS to 51; rebuild bundle
  V001_V051; DEPLOYMENT/README run-lists; test_v051_action_layer.py pins the
  slim shape AND that the deferred injection-bearing procs did not ship. Deploy
  runbook: docs/handoff/DEPLOY_V051_20260727.md.

## 4.52.0 — Tranche B: V050 one-pass loader + read/write arms (2026-07-27)

Owner: "tranche B", after running the V048/V049 deploy. One migration for
Joe: docs/handoff/DEPLOY_V050_20260727.md.

- **V050 (forward-generated, byte-locked):** SP_LOAD_OBJECT_COST re-derived
  from V049 with one enumerated edit — the two attribution inserts become a
  staged design. QUERY_ATTRIBUTION_HISTORY is aggregated ONCE and
  ACCESS_HISTORY flattened once per array into session-scoped temp stages
  (V049 re-scanned QAH twice and flattened AH four times per run); both
  inserts read the stages, so the split and the residual partition the same
  staged credits exactly.
- **Read/write arms (Codex #5):** every split share is labeled —
  QUERY_COMPUTE_WRITE is the production share (the cost of building the
  object; the V049 write-target work made it attributable, V050 makes it
  visible), QUERY_COMPUTE_READ the consumption share. Write wins on a
  read+write collapse so each object keeps one share; credits/N additivity
  unchanged. Readers bucket legacy QUERY_COMPUTE rows (pre-reload days) with
  both new arms; registry and captions updated.
- **Riding fix:** credits for queries whose only touched object has a NULL
  name used to VANISH — V049's residual guard lacked the objectName filter
  the split had, so such queries counted "attributed" while the split had no
  row for them. The unified stage routes them to QUERY_COMPUTE_RESIDUAL.
- **Object-ledger reconciliation (Codex #7 app slice):** new
  cost_sql.object_cost_recon — query arms + residual vs QAH, each
  maintenance arm vs its source history, over a lag-safe window ending 2
  days back; surfaced click-gated on Admin > Canary with DELTA columns.
  Canary-registered; Admin's reachable-scan pin updated deliberately.
- Lockstep: validate.sql floor and _EXPECTED_MIGRATIONS to 50; rebuild
  bundle regenerated (02_migrations_V001_V050.sql); DEPLOYMENT/README
  run-lists gain V050 (the v4.51 doc-floor lock enforces this now);
  tests/test_v050_one_pass.py byte-compares the derivation and pins
  one-pass scan counts, arm partition, residual anti-join, and reader
  bucketing.

## 4.51.0 — Tranche A: outcome and audit trust (2026-07-27)

Owner: "tranche A" — the app-only first tranche from the Codex adjudication
(docs/reviews/CODEX_REVIEW_ADJUDICATION_2026-07-27.md). No migration.

- **Audit actors are the viewer.** Both ALERT_AUDIT INSERTs now stamp
  ACTED_BY and all six REMEDIATION_LOG INSERTs stamp EXECUTED_BY with
  identity_sql() — omitting them let the CURRENT_USER() defaults record the
  app owner under owner's-rights SiS. The false r27-era comment ("the table
  has no actor column") is gone. Remediation-history panels light up with
  real names, zero read-side change.
- **Savings captions promise only what settles.** Six screens promised "the
  monthly verifier proves or rejects" — but V007's verifier proposes only,
  and only for a description no executed path books; V038 settles only its
  own rows. Captions now route to the real path (manual proof-backed verify
  on the Savings ledger; the change scan's own measured row for
  warehouse-setting changes). Ledger NOTES stop claiming a verifier will
  test actuals. The selection-gap fix (typed link) stays queued for r28.
- **ETL unit costs are run-grain and honest.** Queries aggregate to
  (pipeline, run_id) before pipeline totals; $/M-rows counts WRITE-statement
  rows only (a 3-stage pipeline no longer counts logical rows 3x); new
  RUN_ID_CREDIT_PCT column — $/run divides only run-tagged spend, so partial
  tagging degrades honestly. Both builders gain database/schema_contains and
  the panel threads the page filters (the ETL panel used to ignore them).
- **Telemetry reports measured time.** Batch members no longer log an
  invented wall/batch_size runtime — they log the measured batch wall with
  BATCH_SIZE labeling the sharing (matching what QueryResult already
  reported). Cache-miss fetches now capture the Snowflake QUERY_ID via the
  async job handle into session telemetry; the persisted column rides the
  next migration.
- **The perf lint sees through page files.** test_v451_trust renders every
  builder each page references and pins the page's TRUE reachable
  ACCOUNT_USAGE table set (unit_costs: 7 tables under a 0 literal budget —
  the gap Codex called). test_perf_budgets reframed as the lint proxy it is;
  house law 3 amended.
- **Canary compile-only mode.** EXPLAIN wraps each of the 181 statements —
  same drift coverage for identifier/object errors, no aggregates executed;
  the classic executed probe stays a toggle away.
- **Docs carry the real floor.** DEPLOYMENT.md and README run-lists extended
  V035..V049 (they stopped at V034 for fifteen migrations); README's retired
  OVERWATCH_MONITOR/OPERATOR annotation and pre-V044 company-scoping prose
  corrected; a lock pins the migration floor into both docs. Admin's
  registry caption stops overclaiming KPI discovery.

## 4.50.0 — five cross-page moves + Cortex real names (2026-07-27)

Owner: "do all 5" (the v4.49 deferred queue) + first/last names on the Cortex
user attribution.

- **Storage → Spend & Attribution.** Per-database calendar-month storage and
  the account tier split leave 'Chargeback & AI' (storage is neither) for a
  labeled Storage section beside Spend; the cortex tab slims to
  _cortex_spend_tab. The v4.46 STORAGE_USAGE live-fallback literal moves with
  it (ai_chargeback budget 5→4, spend 9→10, same probe-gated non-first-paint
  scan).
- **Query-tag governance → Chargeback & AI.** Its own caption always said why
  it exists ("chargeback precision is capped by tag coverage") — now it sits
  with the chargeback it gates.
- **Task graphs ($) → Unit costs.** $/run per pipeline joins the ETL panel on
  the same QUERY_ATTRIBUTION_HISTORY basis; zero ACCOUNT_USAGE literals moved
  (unit_costs budget stays 0). Task MONITORING — the v4.45 owner-correction's
  subject — stays on Operations (Tasks pill, failures, RCA); the v4.45 lock
  updated to say exactly that.
- **Incident lifecycle KPIs → Alerts > History.** The 90d medians (MTTA/MTTR
  incident grain, reopen, alerts/incident, change-correlated) read beside the
  alert-grain MTTA/MTTR; Control Room keeps the one number that changes
  overnight (Open incidents) with a pointer.
- **Emergency console → Operations.** The lever generator, catalogue, and
  running-query kill switch land as an Operations Emergency pill — the
  subjects (warehouses, queries, pipes, tasks) are that page's sections, and
  executions audit to REMEDIATION_LOG under the Operations page name. Admin
  is now purely app-plumbing (7 sections). RUNBOOK §10 heading updated; the
  stale "suspended by resource monitor" runbook entry retired outright
  (OVERWATCH_RM died in v4.45).
- **Cortex attribution shows people (owner ask).** USERS.FIRST_NAME/LAST_NAME
  ride the rollup join (grouped, grain unchanged); the cost-by-user chart
  plots DISPLAY_NAME — "First Last" with a USER_NAME fallback for service
  accounts/dropped users, never blank, never invented — and the User
  attribution detail gains FIRST_NAME/LAST_NAME between USER_NAME and EMAIL.
  The v4.34.2 live-first + exact emails/timestamps decision is intact
  (amended by the owner to add names); locks pin the SQL shape and the
  fallback behavior.
- Locks updated with what they pin: v4.45 restore (pill list), wave2
  adoptions (graphs mart-first followed the move), codex r24 (kill-switch
  profile links), v016 history (the fragment moved), V046 (tiers wiring),
  design-system (nothing-lost list gains _storage_tab/_cortex_spend_tab).

## 4.49.0 — label truth pass + section coherence (2026-07-27)

Owner: "check for other mislabels across the board... make sure each section
has the correct sections within." Ten parallel auditors read every page and
verdicted every cross-page pointer against the real section lists (68 correct,
14 wrong or imprecise). ~55 fixes; the themes:

- **Every pointer now names a real label.** The "Cost > Spend" shorthand family
  (playbooks, alerts, overview, ai_chargeback, operations, brief, companies,
  errors) now says the exact pill — 'Cost & Contract > Spend & Attribution',
  'Contract & Forecast', 'Admin > Errors & telemetry', 'Chargeback & AI > AI
  users' (that one is stamped into ACTION_QUEUE SOURCE rows), 'Auditor export
  pack' (nothing was ever named "quarterly"), Operations' retired "contention
  tab" → Warehouses.
- **Captions stop misdescribing the data.** CS-ratio caption implied the alert
  fires at ~10% — WATCH is >10%, ELEVATED (where it fires) is >20%. Cortex/AI
  spend and role-share panels say account-wide / exec-second like their SQL.
  Failed-logins all-clear discloses the reader's 30d cap. Governance help said
  "unmonitored warehouses" (monitor tracking died in v4.45 — it means no
  auto-suspend) and "fixed weights" (they're SETTINGS-overridable).
  "Break-glass role holders" → "Privileged role holders" (SYSADMINS is the
  routine role by this page's own definition). QAH lag standardized to ~8h
  (registry value); ops-diag "top-20s" → top-50s (v4.36.1); freshness
  fallback label drops the stale "19-aggregate" count; month-end forecast
  formula_version 'v4.x' placeholder → v4.4. Admin's garbled self-cost caption
  ("XSMALL with a ") restored — the v4.45 monitor removal ate its clause; now
  says the real 60s auto-suspend. Admin self-cost source label admits the
  warehouse-name arm of its filter. Stale RUNBOOK pointer for backfill
  (SP_LOAD_MARTS_V27 / "DAILY 3" — neither exists) → backfill_365.sql
  (HOURLY 90, DAILY 365). Deleted the "resource monitor quota" sentence from
  the Emergency help (no such lever since v4.45).
- **Three rendering bugs.** toggle_cost_hint() return value was discarded bare
  on the query-efficiency and storage-waste scans (hint never rendered — now
  st.caption-wrapped); the canary results map dropped GAP rows to NaN
  (PASS/FAIL/GAP now all render); Optimization's divider only appeared when
  the repeat-query toggle was on.
- **Section coherence, applied where low-risk (intra-page):** Security's
  failed-login reasons → Access (it decomposes Failed logins; login telemetry,
  not change evidence) and unused roles → Access (entitlement hygiene, next to
  dormant users) — which also kills the real bug that Changes' empty-DDL
  early-return hid every panel below it on a quiet week. Optimization's
  recurring-patterns + price-a-pattern panels un-nested from inside the
  differently-named expensive-query toggle into their own labeled toggle.
  Dead _SERVICE_CATEGORY copies deleted from cost.py/contract/ai_chargeback/
  optimize (only spend.py ever used it). Docstrings/subtitles on Admin,
  Operations, Control Room, and all cost_parts modules now describe what each
  actually renders (four modules carried the same copied whole-page docstring).
- Locks updated with the changes they pin: playbooks family fallback (exact
  labels), security changes-tab batch (moved reads; early-return regression
  pinned dead), freshness label keeps its pre-V040 phrase.

Deferred (cross-page moves, each its own round): storage panels out of
Chargeback & AI, query-tag governance into it, Task graphs ($) vs Unit costs,
Admin Emergency/fire-drill/restated-days homes, Control Room incident
lifecycle KPIs. Brief/Overview: kept separate deliberately — see the round
notes.

## 4.48.0 — Org spend moves to Cost & Contract (2026-07-15)

Owner: "move them." Admin's Org spend tab was the page's one section about
business cost rather than the app's own operation; its placement dated to a
2026-07-07 credential-tracking commit and was argued nowhere. Three references
already believed the panels were on Cost (formulas.py F3, two ai_chargeback
captions), and the BUDGET alert family deep-links to Contract & Forecast.

- **Rate-card reconciliation** ("Billing truth vs app model") now renders
  directly under the year-projection strip on Contract & Forecast — it audits
  the CREDIT_PRICE_USD that the projection, pacing, steering, and renewal
  planner all price with, so drift shows up before the numbers it distorts.
  Prices from the settings dict cost.py already passes (Admin's copy re-called
  load_settings); the "fix it" pointer now says Admin → Settings.
- **Org accounts spend** (Snowsight's Accounts Spend Summary equivalent) sits
  below the contract-balance billing-truth panel — same ORGANIZATION_USAGE
  source family, same degrade path when org views aren't granted, and
  Contract & Forecast is the one Cost section that is structurally
  company-agnostic, matching org-grain data.
- Admin drops the section (eight app-operation sections remain); run() keys
  are telemetry-only so caches are untouched; no perf-budget movement (both
  files unbudgeted, and ORGANIZATION_USAGE never counted anyway).
- Drift paid: formulas.py F3 now names the real surface (it claimed "the Cost
  rate-card reconciliation panel" since 2026-07-14, before the panel was
  there); test_p4_org_reconciliation cited a nonexistent cost_sql.org_rate_card
  (the language lives in org_account_month_usd). New
  tests/test_org_spend_placement.py pins the placement, the settings-dict
  pricing, the audit-before-consumers ordering, and zero new ACCOUNT_USAGE
  literals on contract.py.

## 4.47.0 — V049: write-target attribution (2026-07-15)

Owner: "let's do both" — deploy corrected V048 and build V049.

- **V049 (forward-generated, byte-locked):** ACCESS_HISTORY.OBJECTS_MODIFIED
  joins the object-cost equal split. V048 split measured query compute across
  BASE_OBJECTS_ACCESSED (reads only), so write-only ETL — COPY INTO,
  INSERT..VALUES, CTAS from constants — read no base table and its credits
  parked in QUERY_COMPUTE_RESIDUAL instead of on the tables it builds. Now
  loads attribute to their targets and the residual shrinks to genuinely
  unattributable compute (no read, no write). DISTINCT over the read/write
  union keeps a read+write of one table to a single share; additivity holds.
  Two enumerated edits (dedup CTE + obj_q CTE — the split and the residual
  must agree on what "attributed" means, or credits double-count or vanish);
  `outputs/gen_v049.py` re-derives from V048's CURRENT proc,
  `tests/test_v049_write_targets.py` byte-compares and pins the invariants
  (UNKNOWN residual kept, QAS kept, equal split kept, proc-swap-only, both
  flatten arms present twice).
- Proc swap + 14-day reload; no new objects (table and task are V048's).
  Registry: object_query_cost source/note now say read **or wrote** (V048/V049);
  Optimize caption matches. validate.sql floor and admin's expected-migrations
  walk to 49 in lockstep; rebuild bundle regenerated
  (`02_migrations_V001_V049.sql`).
- **Deploy runbook** for both pending items:
  `docs/handoff/DEPLOY_V048_V049_20260715.md`. Option A lands the V048
  UNKNOWN-residual correction and V049 in ONE proc swap + reload (V049's body
  derives from corrected V048), plus a surgical UPDATE for the residual rows
  older than the reload window; the verification block captures the residual
  share before/after — the materiality number that originally gated V049.

## 4.46.0 — Phase 4: account-time fixes + derived locks (2026-07-14/15)

(The same Code session's cost-audit round — F1–F4, Codex items 1–8, metric
registry, V046–V048 object-cost ledger, ETL tags — is chronicled in
`docs/design/COSTDB_VS_OVERWATCH_2026-07-14.md` and
`docs/reviews/CODEX_REVIEW_ASSESSMENT_2026-07-14.md`.)

- **Three account-time bugs, one class.** `account_today()` exists because the
  SiS process clock is UTC while the account runs America/Chicago — yet three
  modules still read the server clock for a business date. contract_planner
  dated CURRENT_CONTRACT_EXHAUSTED a day late for ~6h of every day (the old
  lock passed remaining_usd=0, short-circuiting to "n/a": covered, untested);
  actions.rank_actions compared account-time mart stamps against a server-clock
  now(), flagging rows overdue up to 6h early; canary.py anchored probe windows
  on the server date (one via `__import__("datetime").date.today()`), probing a
  day with no data yet on the boundary. New `formulas.account_now()` is the one
  spelling of account time; `account_today()` delegates to it.
- **Locks that DERIVE their targets** (both Phase 4 findings were drift, not
  defect — the codebase knew the answer and nothing enforced it):
  `test_p4_filter_matrix` introspects `app/data/*_sql.py` and drives all 105
  company-taking builders across every COMPANIES value — no builder fails open
  (UNKNOWN included), no scopes collapse, no payload escapes a literal, no day
  window unclamped. `test_p4_dst` pins the DST boundary (the same 05:30 UTC is
  yesterday under CST, today under CDT — no fixed offset passes) plus an AST
  guard: no server-clock reads in app/logic or app/data. `test_p4_org_
  reconciliation` pins the cloud-services rebate at the owner-confirmed
  effective rate (3.68 = DEFAULT_CREDIT_PRICE_USD): band max($1, 1% of billed);
  a dropped 8% rebate breaks it at 8.7x, 30d cent-rounding sits at 0.004%.
- **Why test_injection_fuzz stalled at 28 targets** while claiming "every
  filter-accepting builder": its residue check strips literals but not
  comments, and five builders carry a `--` comment (one an apostrophe, in "the
  CALL's id") — any of them would trip it while perfectly safe. The list was
  silently capped at what the checker could digest. New scanner resolves
  literals and comments in one pass; the fuzz file now states its curated scope
  honestly and defers exhaustiveness to the matrix.
- Gates: mypy.ini treats numpy as Any (numpy 2.x stubs use PEP 695 syntax that
  mypy refuses below the 3.11 target — same footing pandas already has); ruff
  import-order debt in four prior-round test files paid. 1243 passed, 1 skipped.

## 4.45.0 — r29: the owner's correction (2026-07-13)

"i messed up. i meant getting rid of resource monitor, not task
monitoring. we need to add that back. that's my fault."

- **Task monitoring restored end-to-end.** App: everything r26 removed is
  back from git history (Operations Tasks + Task graphs sections, failure
  RCA timeline, Control Room task vitals/triage/replay, Overview task
  metrics + score signal, incident-timeline task arm, change-impact TASK
  drill, graph_sql/graphs modules, all builders, prompts, canaries) —
  with every post-r26 feature kept (SNOW_* captions, viewer identity,
  allow-list, domain salts, V044 clause arms). Loader: **V045**
  (forward-generated, byte-locked) recreates both tables, re-derives all
  seven procs back to their task-inclusive V041/V042 bodies, re-enables
  PIPE_TASK_FAILURES, and refills 120 days from TASK_HISTORY — while
  keeping V043's r25 alert teeth (19 scan arms now) and V044's UNKNOWN
  board scope. Budgets restored 18->22 / 2->3 with the correction noted.
- **Resource monitors removed instead (the actual ask).** V045 detaches
  and drops OVERWATCH_RM — the 30-credit monthly cap that had been
  suspending WH_ALFA_OVERWATCH mid-use (the real source of the error
  storm). App: the governance "No resource monitor" deduction, the
  posture WH_NO_MONITOR read, the emergency monitor levers, and the
  budget<->monitor sync panel are gone; auto-suspend tracking stays.
  Docs and worksheets updated; teardown lines annotated.
- 15 test files wholesale-restored from git, 3 surgically (V044 edits
  kept); the r26 lock keeps its roles half with the correction recorded.

## 4.44.0 — r28a: UNKNOWN classification, adjudication #18 (2026-07-13)

Owner: "ignore 5 do 18." Unknown entities stop silently billing ALFA.

- **V044 (forward-generated, byte-locked):** classification is evidence-
  based on BOTH sides now — Trexis by mapping/prefix/role (unchanged),
  ALFA by WH_ALFA_* names, ALFA%/ADMIN databases, %ALFA% or DBA roles —
  and the residual is **UNKNOWN**. COMPANY_SCOPE mapping rows are the
  explicit lever (DATABASE grain added; DBA_MAINT_DB seeded ALFA — the
  app's own footprint stays attributed). The exec board gains an UNKNOWN
  scope so the new pill is mart-served. SYSTEM lands UNKNOWN on purpose:
  it runs both companies' work.
- **UNKNOWN is a first-class filter** (company pill) and **Cost ->
  Chargeback gains an "Unmapped entities" worklist** — every UNKNOWN
  warehouse/database/user from the facts with the exact COMPANY_SCOPE
  INSERT printed under it. Empty is the goal state.
- **History is honest, not rewritten:** mart rows keep the company stamped
  at load; the nightly reconcile re-stamps the trailing 3 days; older
  rows re-stamp only on a backfill re-run (noted in the migration).
- validate.sql: the "unknown user falls back to ALFA" law is superseded —
  the probe now expects UNKNOWN; KEBARR1's override law unchanged.
  Python mirrors, clause arms (warehouse/database/user/role), and eleven
  scoping locks updated with V044 notes.

## 4.43.0 — r27: V043 + the adjudication's ship list (2026-07-13)

Authority: docs/reviews/CODEX_R27_ADJUDICATION_20260713.md.

- **V043 (forward-generated, byte-locked):** task retirement finished
  loader-side — SP_LOAD_DAILY_FACTS stops scanning TASK_HISTORY; the [6]
  task-graphs arm, the timeline TASK_FAIL union, board/score task inputs
  (zero-filled, shapes kept), purge/reconcile/freshness references all go;
  FACT_TASK_DAILY + MART_TASK_GRAPH_DAILY drop. **PIPE_TASK_FAILURES
  (HIGH) is disabled** — it had been alerting on task failures all along.
  The r25 metrics get teeth: SEC_NEW_ADMIN_NETWORK + COST_EGRESS_SPIKE
  rules and scan arms (18 arms, same dedupe shape).
- **Viewer identity (#4, Snowflake-doc verified):** in owner's-rights SiS,
  CURRENT_USER() is the app owner — prefs, usage rows, audit actors, and
  verification stamps now ride identity_sql() (st.user, CURRENT_USER()
  fallback); the query cache isolates per viewer.
- **Executor allow-list (#10-light):** one statement per call, aimed at
  OVERWATCH tables/procs or a warehouse lever — anything else refuses
  before it reaches Snowflake.
- **Domain cache invalidation (#14):** writes bump only their domain's
  salt (alerts/settings/prefs/budgets/...); unknown targets still bump the
  global salt. An alert ack no longer refetches the whole page.
- **Bulk alert lifecycle (#12-partial):** one UPDATE + one audit INSERT
  for N events (was 2N in a loop); honest failure message when the audit
  half fails. Full atomicity lands with r28's proc layer.
- **Admin:** access self-check (probes every privileged source, prints the
  exact missing grant), grouped error families over raw rows, orphaned
  settings-key warnings.
- **Roles/docs:** audit append-only REVOKEs restored (#6, r26 regression);
  SHOW GRANTS ON STREAMLIT proof block (#7); SNOW_PRI_* overrides removed
  (#8 — suffix heuristics already covered them); DEPLOYMENT/RUNBOOK
  rewritten for the two-role owner's-rights model (#9+#3) and locked.

## 4.42.0 — r26: two roles, no task monitoring (2026-07-13)

Owner: "the only roles that will have access is SNOW_ACCOUNTADMINS and
SNOW_SYSADMINS. remove any traces of other roles. also remove task monitor
references. the app is producing a number of access error messages."

- **roles.sql rewritten.** The OVERWATCH_MONITOR/OPERATOR layer is retired
  (actively dropped, idempotent); every grant now goes directly to
  SNOW_ACCOUNTADMINS and SNOW_SYSADMINS — locked so no other grantee can
  reappear. Teardown keeps its everything-commented safety law; rebuild
  bundle copies regenerated. Re-run roles.sql once to apply.
- **Task monitoring removed end-to-end.** Operations loses the Tasks and
  Task graphs ($) sections; the failure-RCA timeline, release task-deltas,
  Control Room task vitals/triage/replay rows, the Overview task-failure
  score signal, the incident-timeline TASK arm, and the change-impact TASK
  drill are gone with their builders (graph_sql module deleted). Every
  live TASK_HISTORY / TASK_VERSIONS / SERVERLESS_TASK_HISTORY read — the
  access-error sources — is out of the app; the loader's own DAG health
  still surfaces via APP_ERROR_LOG and freshness (loader_chain_check.sql
  unchanged for worksheet diagnosis).
- **Break-glass panels watch the real roles** (SNOW_* pair, not the unused
  built-ins); admin/alert captions and the dept map follow. Platform score
  is task-free in both live and retro paths (weights unchanged otherwise).
- Live-scan budgets DOWN: operations 22 -> 18, control_room 3 -> 2.
  18 task-monitoring tests removed with the feature; needles updated with
  r26 notes; NaT fix in triage RAISED_AT normalization surfaced by the
  slimmer frame.

## 4.41.0 — r25: the two security metrics the owner picked (2026-07-13)

From the seven proposed, the owner chose #6 and #7. Both are click-gated —
Security's first paint pays nothing new.

- **New networks for privileged users (Access tab).** A break-glass user's
  (user, IP) pair appearing for the first time against a 90-day
  LOGIN_HISTORY baseline, with auth factor and success counts per row.
  Rides the tab's existing batch round-trip. Quiet-90d IPs re-flag on
  purpose — a stale re-flag beats a silent novel network.
- **Egress watch (new section).** Outbound bytes by day/destination from
  DATA_TRANSFER_HISTORY (stacked daily + top-destination KPI) and per-user
  UNLOAD activity with GB_OUT and a SAMPLE_TARGET preview of the statement
  — exfiltration and a surprise transfer bill start as the same unwatched
  bytes.
- Canaries for all three builders; section-list lock superseded (4 -> 5
  sections, documented); security.py live-scan budget 18 -> 22 with the
  justification in the dict.

## 4.40.0 — r24: profile links everywhere + the first honesty/tier ships (2026-07-13)

- **Snowsight query-profile links are a pattern now** (owner: "the
  hyperlinks to the query profile are helpful"). A shared helper turns any
  QUERY_ID column into a Profile link (org/account resolved once per
  session; no context = no column, never a dead link). Wired: Operations
  heaviest queries, Optimization costliest queries, Unit-costs CALL
  pricing, Admin running-queries. One click from any row to the plan,
  partitions, and spilling.
- **Review #4 — the dead cache gauge is off the pain board.** CACHE_HIT_PCT
  read 0.0 by construction (persisted rows are slow-biased); the tuning
  targets table drops it, the by-page table keeps it with the
  floor-not-census caption, and the real gauge returns with weighted
  telemetry (review #3).
- **Overview: the Monthly-budget KPI is replaced** (owner: "I don't like
  having useless features" — it read 'Not configured' forever). Its slot
  now paces MTD against the prior month's SAME first-N-days from the 150d
  frame the page already loads (zero new queries); a configured budget
  survives as help-text context. No prior-month data = no fabricated 0%.
- **Review #8, first slice — post-action freshness is systemic.**
  execute_statement bumps the refresh salt on success, so every cached
  read refetches after any operator action; with that guarantee in place,
  the risk-free live-tier downgrades land (SCHEMA_VERSION -> metadata,
  Flyway probe -> recent, alert routes -> recent). The deliberate live
  surfaces (settings table, dept budgets, alert queue, recheck-now)
  stay live. The queue itself is next slice, behind an ack-flow test.

Deploy: app-only — push the build.

## 4.39.0 — triage filters, visible; the 20-item forward review (2026-07-13)

(Entry restored in v4.40.0 — the stamp script died mid-run and the commit
went out titled v4.39.0 without it; the work itself shipped in f5852d4.)

- Triage filter strip visual pass: active-scope chips (contains-filters
  amber), a filtered-strip glow, one-click Reset, and an honest
  "Account-wide · default window" state. Token-layer CSS; user text
  escaped; locked in test_design_system.
- docs/reviews/APP_REVIEW_20260713.md: twenty grounded recommendations in
  four tiers with r24-r26 sequencing.

## 4.38.0 — r23: the post-rebuild fleet board's picks (2026-07-12)

App-only round, by design — the rebuild settles while the telemetry does
the picking (pain = p95 x slow fetches; the board's leaders, not opinions).
All four targets verified in code before shipping:

- **Queue/spill pressure goes fact-first (c_pressure, 17.8s p50).** The
  hourly fact carries exact queued/spill/count sums; a new
  fact_warehouse_pressure reader serves the Contention panel with the live
  scan as labeled fallback (p95 = peak hourly, labeled). Canaried;
  operations' live-scan budget drops 23 -> 22.
- **Task-failure RCA prunes on SCHEDULED_TIME (t_rca, 32.9s).** TASK_HISTORY
  prunes on SCHEDULED_TIME — the V031 builders bound both columns for
  exactly this reason; the RCA read now does too (+1d margin, semantics
  unchanged).
- **Change-impact drill pre-filters before normalizing (chg_hist, 6.1s).**
  The V031 scan-v2 trick applied to object_run_history's PROCEDURE branch:
  a cheap ILIKE rides in front of the POSITION text pass, so only plausible
  CALL rows pay the REPLACE/UPPER normalization.
- **Security's changes tab batches its two live reads** (DDL/DCL + failed-
  login reasons) — the Access-tab parallel-submit pattern, serial fallbacks
  unchanged.

Still open from the board after this round: cs_types 21.4s (routed r22 #16,
extract v2), the loader-v2 re-derivation set (#4/5/6/8/19), and the V1
EXECUTION_STATUS probe (one query in Snowsight decides which side gets the
one-line fix).

Deploy: app-only — push the build; no migration, no grants.

## 4.37.1 — validate: the stale V001-era user-prefix check (2026-07-12)

Live finding on the rebuilt account: 'TRXS_ prefix classifies as Trexis'
FAILed. The check probed a fictional user against V001's prefix rule, which
V019 deliberately replaced with role-membership classification — latently
wrong since then, surfaced by the fresh install. Replaced with two
deterministic checks that test the CURRENT contracts: the database prefix
rule (COMPANY_FOR_DATABASE('TRXS_EDW_PRD') = 'Trexis') and the unknown-user
ALFA fallback. Rebuild-bundle copy regenerated. Also confirmed from the
rebuild's error log: the four mart_load_failed rows are V027/V029 first-fill
replay artifacts (fixed by V030 in-sequence) — expected on every fresh
apply, not a live failure.

## 4.37.0 — Codex r22: eight ships, ten routes, two declines (2026-07-12)

Every claim verified in code first; the adjudication with evidence is
docs/reviews/CODEX_R22_ADJUDICATION_20260712.md. Shipped (V042 +
app):

- **#7 — the extract is atomic and the watermark gates on COMMIT.** The
  v4.36.1 isolation could delete the overlap, fail the insert, and still
  advance the watermark — a hole every consumer MERGEd in until the
  nightly repair. Each arm is one transaction now; a failed cycle
  re-covers its own window.
- **#1 — FACT_QUERY_DAILY** (day grain, year-backfillable): the exec
  board's 14/60/90 windows and the platform score read it, so a fresh
  rebuild starts with real query totals instead of undercounting while
  the hourly fact accrues from day one.
- **#2 — ops diagnostics backfill** (wide explicit loads; recurring stays
  2d) — the 7-day Operations first paint is mart-served on day one.
- **#10 — retention: sixteen V027/V041 tables join SP_PURGE_FACTS** (the
  whole mart family predated the purge, not just the new tables).
- **#15 loader half — the AI fact gains EMAIL + exact FIRST/LAST usage
  stamps.** The users tab STAYS live-first per the owner decision; the
  mart re-swap is queued behind a side-by-side proof.
- **#14 — AI users section is toggled** (the exact Cortex scan no longer
  runs ambiently with the chargeback group), **#17 — drill lookups bound
  to the clicked row's day ±1** (pasted IDs keep the broad scan), **#20
  label half — the fleet board names its exception-weighted sample.**

Routed: #4/#5/#6/#8 (one reviewed loader-v2 re-derivation, together),
#3/#11/#12/#16 (extract v2 / loader v2), #13 (fix-batch), #19 (loader v2
headline: percentile states), #20 stats half (fix-batch), #15 app half
(behind fact proof). Declined with evidence: #9 (COUNT/MAX are
metadata-served — freshness writes are constant-cost already), #18 (the
"second scan" is the share-law denominator; folding it post-filter is the
renormalization bug the law exists to prevent). Open: V1 — the
'FAIL' vs 'FAILED' EXECUTION_STATUS split between V002 facts and V027
marts needs one live probe; the loser gets a one-line fix.

Deploy: V042 after V041 (the rebuild bundle is regenerated to 42 files) ->
roles.sql -> backfill_365.sql (now fills FACT_QUERY_DAILY for the year +
the diagnostics mart for 90d) -> validate expects V001..V042.

## 4.36.2 — the one-shot rebuild bundle (2026-07-12)

Owner: "i want the full rebuild." snowflake/rebuild/ is docs/FULL_REBUILD.md
as six paste-and-run Snowsight files: 00 date-stamped clone backups of all
21 operator tables (verified counts, zero DROPs), 01 teardown (byte-copy),
02 all 41 migrations concatenated in order (Run All halts AT a failure;
every file idempotent), 03 roles, 04 backfill, 05 validate. GENERATED and
equality-locked against the sources (tests/test_rebuild_bundle.py) — edit
the sources, regenerate the bundle, never hand-edit it. Operator data
survives by default; the factory-reset variant deliberately stays manual.

## 4.36.1 — V041 corrections: the owner's regressions, fixed at the root (2026-07-12)

Owner reports after v4.36.0: the cortex user table lost emails/timestamps,
validate.sql errored on TASK_DEPENDENTS, task-graph failures and alerts
went quiet. Root causes + the review live in
docs/reviews/V041_INCIDENT_REVIEW_20260712.md; the live-account recovery
is snowflake/loader_chain_check.sql (step 0), then redeploy, then
docs/FULL_REBUILD.md for the clean slate.

- **Cortex user attribution reverted byte-for-byte to v4.34.2.** The R3
  mart swap served NULL emails and day-grain usage stamps — the exact
  who/when IS that table. Live-first again (probe semantics intact); the
  degraded reader + canary deleted. A correct R3 (EMAIL + FIRST_TS/LAST_TS
  on the fact first) is queued, not shipped.
- **The task tree can no longer strand suspended (the real alerts/task-
  graph killer).** v4.36.0 put every RESUME after seven first-fill CALLs —
  a halted worksheet run left both roots' children suspended (the 07-12
  outage class the design ordered dead). The full resume +
  SYSTEM$TASK_DEPENDENTS_ENABLE block now runs BEFORE the fills and again
  at file end; locked (two enables per root, order asserted).
- **Extract loader isolated + no bare NULLs.** A flaky QUERY_HISTORY scan
  now logs and degrades (consumers read the previous fill) instead of
  failing the task and SKIPping the hourly chain; watermark mode is
  DAYS_BACK <= 0 and the tasks pass 0.
- **Posture SHOW guarded:** a SHOW failure skips only the two monitor
  metrics (HAVING — never a lying zero), never core posture.
- **Ops diagnostics made exact:** top-50/hour (the unfiltered top-50 panel
  is exact by construction, not a sample); USERS_AFFECTED is an honest HLL
  window approx-distinct (V037 precedent), labeled.
- **validate.sql: task monitoring removed** (owner decision — unused, and
  TASK_DEPENDENTS needs a db context bare runs don't have). Task-state
  diagnosis moved to the new snowflake/loader_chain_check.sql.
- New: docs/FULL_REBUILD.md — the safe full drop-and-reinstall (the schema
  is SHARED with the previous app; nothing drops DBA_MAINT_DB).
- Review: v4.34.2 -> v4.36.1 delta audited file-by-file (the three shared
  infra changes are sound — no revert of v4.35.x needed); every remaining
  V041 swap holds its live contract exactly, with coverage-gated fallbacks.

Deploy: redeploy the app; run loader_chain_check.sql step 0 on the live
account now; rebuild per docs/FULL_REBUILD.md when ready (V041 re-applies
with the corrected file).

## 4.36.0 — V041: the loader-efficiency pass (2026-07-12)

One migration, eleven riders — built to the design freeze
(docs/design/V041_LOADER_PASS.md), in a fresh session, doc as contract.

- **R1 — one QUERY_HISTORY scan per hourly cycle.** `OW_QH_EXTRACT`
  (transient, watermark - 45 min, 3-day retention) feeds the design's
  consumer list exactly: FACT_QUERY_HOURLY, _OW_ALLOC_BASE, tag coverage,
  query-family, schema-hourly, role-hourly, the incident-timeline DDL arm,
  and the new R7 diagnostics. The warehouse-efficiency q-CTE and posture
  ADMIN_STMTS_24H are not on the list and deliberately stay live. Build
  note: the FACT_QUERY_HOURLY arm MOVED into SP_LOAD_QH_EXTRACT (verbatim,
  FROM swapped) — the root's proc runs before the extract fills, so an arm
  left behind would trail a cycle.
- **R2 — FACT_COST_ALLOC_XDIM_DAILY** (DAY x WH x DB x USER, no schema
  grain) persists from _OW_ALLOC_BASE before it collapses; Spend's
  database-filtered attribution goes mart-first (was two live scans per
  filter value) and user-within-database is mart-served.
- **R3 — AI users from the fact.** cortex_code_user_rollup's contract now
  reads FACT_AI_USAGE_DAILY (cortex_users p50 17.6s x12 was the worst
  user-facing key); the live view stays as fallback WITH probe semantics —
  the 002139 subscription note still fires where Cortex Code is absent.
- **R4 — exec board v2.** Builds all five config windows (7/14/30/60/90 —
  14/60/90 always fell to the 13-month live scan before), aggregates each
  source once and unpivots, and swaps in atomically via OW_EXEC_BOARD_STAGE
  (the DELETE+INSERT gap stranded Overview on the live fallback hourly).
  PRESSURE_QUEUE / PRESSURE_SPILL / DB_MIX retired: zero readers.
- **R5 — watermarks + nightly reconcile.** OW_LOAD_WATERMARKS; the extract
  reads watermark - 45 min, the daily loader watermark - 1 day (outage
  self-heal, 30d clamp); TASK_NIGHTLY_RECONCILE delete-and-rebuilds the
  trailing 3 days so restated ACCOUNT_USAGE rows and disappeared groups
  cannot survive stale MERGE rows.
- **R6 — loader-owned freshness.** Every SP merges its sources into
  SOURCE_FRESHNESS_STATE (+GENERATION invalidation token, +STATUS) in its
  own commit; TASK_SNAPSHOT_FRESHNESS retired (144 wakes/day);
  SP_SNAPSHOT_FRESHNESS kept for manual refresh.
- **R7 — MART_OPS_DIAG_HOURLY.** Top-20/hour by elapsed + failure families
  from the extract; Operations' UNFILTERED first paint goes mart-first
  (30-37s batch retired); any entity/schema filter keeps the true live
  top-N. Coverage-gated while the mart accrues.
- **R8 — FACT_PLATFORM_SCORE_DAILY.** The retro score's four input
  aggregates load daily; weights stay in Python; Overview's sparkline
  reads the fact with the live aggregation as fallback.
- **R9 — unused-role posture from FACT_QUERY_ROLE_HOURLY**, coverage-gated
  via HAVING (no row — never a lying zero — until the fact spans 90d);
  the 90d QUERY_HISTORY anti-join leaves the daily posture loader.
- **R10 — WAREHOUSE_ID > 0** joins the V27-family loader's two metering
  source reads (the V039 promise); eff-mart reader name-filters stay until
  the next re-derivation.
- **R11 — monitor counts in the posture row** (WH_NO_MONITOR /
  WH_NO_AUTOSUSPEND) via SHOW -> RESULT_SCAN in the daily posture arm
  (V024's scan is the owner's-rights precedent); Security's governance
  panel stops paying a SHOW + parse on render when the posture row
  carries them.

Derivation law: SP_LOAD_MARTS_V27 re-derived VERBATIM from V031's proc +
the enumerated edits; SP_LOAD_HOURLY_FACTS from V039's minus the moved arm;
SP_LOAD_DAILY_FACTS from V002's + watermark bounds. All three (and the
moved arm) are equality-locked in tests/test_v041_loader_pass.py, which
also carries the design's test plan: the numeric-recon pandas harness
(xdim day-sums == single-dim day-sums == never exceed metering), the
extract-consumer projection contract, the board-windows==config cross-lock,
and the task-graph lock (every V041 task has a RESUME; both roots end with
SYSTEM$TASK_DEPENDENTS_ENABLE — the 07-12 outage class stays dead).
Superseded with semantics: test_v039's "loader = V002 + one predicate"
(now the historical chain link), prep_iac's probe-count needle, wave2's
spend filter-split lock. Budgets went DOWN: overview 2->1, ai_chargeback
5->4, operations 24->23. Canaries added for all five new readers;
EXPECTED_GAPS untouched.

Deploy (Joe): push -> Snowsight: V041 after V040 -> re-run roles.sql (new
objects) -> validate.sql expects V001..V041 -> redeploy the app on the
warehouse runtime. Optional but recommended: re-run backfill_365.sql
(extract fills first now) so the xdim fact starts with 90d of history.
Fleet board after 24h tells us what the pass actually bought.

## 4.35.1 — Codex r21: four ships, two corrected claims (2026-07-12)

- **Fragment docstrings are binding now (#4).** _whatif_panel and
  _statement_export claimed "Fragment:" with no @st.fragment — every slider
  move and month pick re-rendered the whole grouped Cost page. Decorators
  added; an AST lock fails any future "Fragment:" docstring without one.
- **Reconciliation on demand (#7).** Opening Admin > Canary paid a 28d
  metering + 7d history comparison; it now waits for a Run toggle.
- **Settings honor manual refresh (#15, real bug).** The outer settings
  frame cache ignored the refresh salt, so edited settings could read stale
  for 5 minutes after an explicit refresh. Salt joins the key.
- **No-op query-param writes suppressed (#19)** for page and section.

Corrected: #5 — SHOW WAREHOUSES inside the what-if panel is metadata-tier
(4h cache), and with the fragment restored it reruns panel-locally; hoisting
buys nothing. #16 — a single WHERE with OR counts each row once; there is
no double-counting in app self-cost (the OR is at worst a pruning nit).
Routed: #1/#2/#3/#6/#9/#17/#18 -> fix-batch; #8/#11/#12 -> query-core v2;
#10/#13/#14/#20 -> polish round.

## 4.35.0 — Codex r20: five verified ships, one decline that mattered (2026-07-12)

- **Remediation reuses the advisor's read (#1).** The Optimization
  remediation block re-ran the live metering x history join the idle
  advisor had already answered mart-first; it now uses the identical
  builder pair, so the advisor's cache serves it.
- **Quarantine is cross-page safe (#2).** Batch-quarantine entries were
  short member keys ('act'), so one page's failure forced same-named
  members on other pages onto the serial path. Identity is now
  page + key + sql-hash.
- **Helper CTEs carry the company predicate (#4).** Idle, sizing, and
  hourly-activity supporting scans (QUERY_HISTORY / FACT_QUERY_HOURLY)
  ran account-wide and were only narrowed by the join; the predicate now
  applies before aggregation ('1 = 1' keeps ALL-scope SQL valid).
- **Sparks match their neighbors (#7).** Overview's activity sparkline is
  company-scoped; Operations' is company+database — same treatment
  Control Room got in v4.34.0.
- **One credentials scan (#17)** via COUNT_IF in the governance fallback.
- **#20 DECLINED — the registry stays full.** Codex claimed eight canary
  readers are unreachable; a whole-tree scan (including app/main.py, which
  subtree greps miss) shows every one has a live caller. Nothing removed.

Routed: #3 retry backoff -> query-core v2; #5/#6 pipeline+topology scoping,
#11-#16, #19 -> the standing fix-batch; #8/#9/#10 -> polish round.

## 4.34.3 — CI hotfix: floor-compat job vs bare sqlglot imports (2026-07-12)

The v4.34.1/v4.34.2 test files imported sqlglot at module level; CI's
floor-compat job installs only the Streamlit/pandas floor pins, so test
collection failed there (the local gates — pytest + ruff — never saw it).
Both files now use pytest.importorskip like the migrations gate. Local
gates gain CI parity permanently: mypy and a floor simulation (pytest with
sqlglot absent) run before every ship. mypy is clean on all 39 files.

## 4.34.2 — Codex r19: six page-level ships, the rest routed (2026-07-12)

Shipped (all verified in code first):

- **Day replay on demand (#2).** Six reads for a bottom-of-page feature ran
  on every Control Room rerun; now behind a Load toggle.
- **Open actions can't age out (#5).** The action reader fetched the newest
  200 of ANY status, then Python dropped closed rows — an old open critical
  could fall outside the window. Status now filters in SQL (literals
  cross-locked to logic.OPEN_STATUSES); ranking stays in one place.
- **Zero-failure scan skip (#6, task side).** When the >=7d task summary in
  the same scope counts zero failures, the 7d TASK_HISTORY root-cause scan
  is skipped. Query side declined: those members run in one parallel batch,
  and serializing them trades latency for bytes.
- **Storage snapshot fetches a snapshot (#7).** Both storage builders now
  QUALIFY to the latest loaded day (the panel discarded the window anyway,
  up to ~90x rows). Also fixed a stale source label that still claimed the
  live view after the fact swap.
- **One-member batch removed (#18)** — contention pressure uses plain run().
- **Exports (#19/#20).** Big-table prep now stores the CSV BYTES (the old
  boolean reserialized on every later rerun); every download_button is
  frontend-only (on_click="ignore", Streamlit >=1.44 — we pin ~=1.45), so
  downloads no longer rerun the app.

Routed: #1 board windows, #3 score snapshot, #8/#9/#10 exec-board rework ->
V041 loader pass (board rider). #4/#11/#12/#13/#14 -> next fix-batch with
r18 #4/#5/#7/#8. #15 canary concurrency, #19-full fingerprint cache ->
query-core v2. #16 declined (the live settings read is the operator
edit-freshness path; the table is tiny). #17 declined (ORDER BY is part of
reader contracts — sparklines and .iloc[0] depend on it; savings negligible
on aggregate outputs).

## 4.34.1 — Correctness audit batch 1 + Codex r18 verified fixes (2026-07-12)

Full-app filter/metric audit (owner ask) merged with r18 verification.
Confirmed and fixed:

- **Broken sizing fallback (r18 #2, REAL — my V039 edit).** The
  warehouse-sizing live builder carried a bare second WHERE and failed on
  every render since v4.30.0; nothing parsed app-side SQL. Fixed, and the
  class is dead: the parse gate now runs sqlglot over EVERY canary-registered
  builder (~165), not just migrations.
- **Admin tuning-target drill (owner report: "flashes and does nothing").**
  selectable_table returns a positional index; the drill subscripted it like
  a row since v4.23.0 and a silent except ate the TypeError on every click.
  Fixed with iloc, and the except now reports instead of passing.
- **Two more attribution-law violations.** Role shares (live + mart) filtered
  roles inside the per-warehouse denominator — excluded roles' slices were
  re-absorbed by this company's roles on shared warehouses. expensive_queries_usd
  filtered the q CTE that also built its warehouse-hour denominator (dead
  param masked it). Both now: whole-scope denominator, filters pick display
  rows after. Optimize's expensive-query scan now honors the sidebar
  database/schema filters.
- **max_rows authoritative (r18 #1).** A trailing LIMIT larger than the cap
  is rewritten down (a 20,000-row reader must honor a 1-row canary cap);
  small trailing limits still win.
- **AI fact-first ordering (r18 #3).** Unit Costs read FACT_AI_USAGE_DAILY
  only after paying the live Cortex scan in its batch, then usually threw
  the live result away. The live member now joins the batch only when the
  fact can't answer. Role-allocation math vectorized (r18 #16).

Audit sweep found no further violations: dept chargeback, CS drills,
fingerprints and pattern movers are measured/exact; deep-scan KPIs label
top-N sums as top-N. r18 #4/#5/#7/#8 (fact-first reader swaps) = approved
next fix-batch; #9-#15/#17-#20 map to V041, registry design, and
query-core v2 (details in the session log).

## 4.34.0 — Control Room follows the database filter (2026-07-11)

Owner ask: "i should be able to filter in Control room using database."
The pulse KPIs and task panels already followed it; now the whole page
answers coherently:

- Activity sparkline: fact_daily_activity gains company + database args
  (FACT_QUERY_HOURLY carries both), so the 14-day spark matches the pulse
  KPIs beside it. Defaults keep every other caller's SQL byte-identical.
- Lock-wait spikes: MART_LOCK_WAIT_DAILY has carried DATABASE_NAME since
  v4.21 — the spike scan now narrows to the selected database.
- Grain honesty where the filter can't reach: the incident timeline
  (company events), spend movers (warehouse grain), and the triage queue's
  alert/anomaly rows say so in one line each, only while a database is
  selected. The header scope chip shows the active database.
- Sidebar help updated: query, task, DDL, attribution, storage, and lock
  panels. Locks in tests/test_cr_db_filter.py.

## 4.33.2 — Codex r17: one new item in a fourth convergent round (2026-07-11)

18 of r17's 20 items restate the adjudicated queue (V041 loader riders,
registry design, query-core v2, standing owner decision on a dedicated UI
warehouse, measure-first clustering, executable perf contracts). Shipped the
one new bug: the optional AI Functions view ran its historical scan on every
AI-tab paint because expander bodies execute even when collapsed — the scan
now waits for an explicit toggle (deep-scan forensics pattern). r17 #17's
mechanism is wrong (st.text_input fires on Enter/blur, not per keystroke),
but the underlying cost of schema-filtered attribution is real and dies with
V041 rider nine; Apply-form/fragments ride the queued fragments round.

## 4.33.0 — Codex r16: the two new items, and calling the convergence (2026-07-11)

r16 is the third consecutive round recommending substantially the same
list. Shipped the two items that were both new AND safe as reader swaps:

- **Cortex service spend from the fact (r16 #7).** Chargeback & AI's
  Cortex panel read live METERING_DAILY_HISTORY although FACT_METERING_DAILY
  carries DAY, SERVICE_TYPE, and billed credits. Fact-first with the live
  scan as fallback; same SERVICE_TYPE predicate, same billed basis. Per the
  r15 lesson, the lock asserts the fact version contains no USAGE_DATE.
- **Overview reads metering once (r16 #17).** The 45d MTD read and the 150d
  backtest read collapse into one 150d frame loaded up front —
  _mtd_spend_usd's preloaded parameter existed for exactly this; the 45d
  read survives only as its internal fallback.

Convergence disposition (#1-#6, #8-#16, #18-#20): every remaining item is
already queued — the loader-efficiency pass (V041: staging extract,
watermarks, task-graph phasing, loader-owned freshness, ops mart, AI Users
fact, role-posture fact, Cortex reader landed here instead), the
coverage/cadence registry (now noting r16 #10's real find: backfill_365
only prepends before MIN(DAY) and cannot repair interior holes), the
query-core v2 design (per-member batch caching, real server metrics,
buffered telemetry, byte budgets), the dedicated UI warehouse (owner
decision), measure-first clustering, the fragments round, and executable
contracts. Recommendation: pause review rounds until the loader pass
lands — the last three rounds' marginal yield was two reader swaps and
two same-day bug catches on freshly written code.

Deploy: redeploy the app; no migration.

## 4.32.1 — r15 #1: my regression, their catch, everyone's class-killer (2026-07-11)

- **Chargeback window read fixed.** The r14 fact swap changed the FROM but
  left the live view's M.START_TIME in the WHERE — FACT_WAREHOUSE_DAILY has
  no such column, so every Chargeback render failed (and failures are never
  cached, so it re-failed each rerun). Now M.DAY, like its month sibling.
- **The class is dead, not just the instance.** New sweep test walks the
  entire canary registry: any builder reading ONLY our facts/marts must
  never reference the live views' time columns. The r14 lock checked the
  table name and missed the column — r15 #20's criticism was fair.
- **Brief stops paying for the health strip twice (r15 #14, the concrete
  half).** The app shell runs it every render under key="health_strip";
  Brief's batch tuple-cache paid the same SQL again. Brief now shares the
  shell's cache entry. (Per-member batch caching remains the query-core v2
  design.)
- r15 disposition: #2/#3/#4/#6/#7/#9/#10 all fold into the loader-efficiency
  pass (now EIGHT riders on the V27 re-derivation — next session, fresh
  context, one migration); #5 dedicated UI warehouse = owner decision,
  standing recommendation; #8 rides it too (Cortex spend from
  FACT_METERING_DAILY is a two-line reader swap once the pass lands);
  #11/#12/#13 = the coverage/cadence registry design; #15/#16/#17 =
  query-core v2 (byte-budgets noted); #18 clustering = measure-first note
  (SYSTEM$CLUSTERING_INFORMATION before any CLUSTER BY — likely unnecessary
  at current table sizes); #19 fragments round; #20 partially delivered
  tonight via the schema sweep.

Deploy: redeploy the app; no migration.

## 4.32.0 — Codex r14: the fact backfill pays off + a same-day bug caught (2026-07-11)

- **Contract coverage predicate fixed (r14 #8 — a bug shipped hours ago).**
  fact_contract_consumed computed MIN(DAY) after WHERE DAY >= start, so a
  quiet contract-start day would read as "no coverage" and fall back to the
  live rescan forever. Coverage (FACT_FIRST_DAY) is now computed over the
  whole fact; the contract-window sum moves into an IFF.
- **Four metering surfaces move to the fact (r14 #5).** The 365-day
  FACT_WAREHOUSE_DAILY backfill already existed — department chargeback
  (window + monthly statements), the Brief's app-cost quarter, and the boss
  chart's long-view fallback now read it instead of live
  WAREHOUSE_METERING_HISTORY. Same exact-metering basis (CREDITS_TOTAL =
  CREDITS_USED), pseudo-warehouse-filtered by construction, no live scans.
- **Freshness boards poll at snapshot cadence (r14 #13).** The V040 state
  table moves every 10 minutes; the boards' 30-second live tier bought
  nothing. Now "recent" (5 min).
- **Cache cardinality bounded (r14 #17).** max_entries on every tier
  fetcher (run + batch): refresh salts and filter permutations mint keys
  forever; process memory now has a ceiling.
- **Security header reads posture once (r14 #18, first half).** The
  governance score (latest day) and the 90-day trend share one read —
  the 3d + 90d double-read is gone. Persisting warehouse-monitor counts
  into the posture snapshot rides the V27-family re-derivation.
- r14 disposition elsewhere: #1+#3+#4+#6 = the loader-efficiency pass
  (one QUERY_HISTORY staging extract, watermark loads, loader-owned
  freshness rows, ops-diagnostics mart) — next session's design+build,
  bundled with the V27 re-derivation and its riders; #7 (AI Users from
  FACT_AI_USAGE_DAILY — the fact HAS user grain) joins it with proper
  contract tests, hourly tier already cut its refire 12x; #2 (dedicated
  UI warehouse) is an owner decision — recommended, needs a quota call;
  #9/#10/#12/#20 = the coverage/cost registry design; #11 waits for a
  second viewer; #14/#15/#16 = query-core v2 note; #19 = fragments
  polish round.

Deploy: no new migration — redeploy the app (V040 still pending if not applied).

## 4.31.0 — Codex r13: the cache stops re-paying hourly data every 5 minutes (2026-07-11)

Driven by the owner's fleet screenshots (1.5-3.4% cache hits on the top
pages with ONE viewer = TTL exhaustion, not user fan-out): the perf batch.

- **V040 (`V040__freshness_state.sql`) — freshness is a lookup (r13 #2).**
  SOURCE_FRESHNESS_STATE: one row per source, snapshotted from the
  19-aggregate view every 10 minutes server-side by SP_SNAPSHOT_FRESHNESS +
  TASK_SNAPSHOT_FRESHNESS (seeded on apply). The health strip and both
  freshness boards read the tiny table (staleness computed from
  LAST_LOAD_TS at render); the view remains the writer and the pre-V040
  fallback.
- **"hourly" cache tier (r13 #3).** TTL 3600 for reads whose sources load
  hourly/daily; `run_mart_first`'s mart side now defaults to it. The
  Refresh button still invalidates everything instantly. Loader-generation
  invalidation stays a design note — this captures most of the win for
  none of the plumbing.
- **Contract & Forecast sheds its live scans (r13 #6/#7).** Steering levers
  build from the efficiency mart + the V037 pattern mart (measured, with a
  shape adapter; live joins remain fallbacks), and contract consumption
  sums FACT_METERING_DAILY gated by a coverage predicate — the fact must
  actually REACH the contract start (FIRST_DAY) or the live rescan serves.
- **Compare pruning finished (r13 #11/#12).** Adjacent A/B windows predicate
  as one contiguous range; the pattern sample-text subquery is bounded on
  both ends.
- **Rendering (r13 #19).** STYLER_MAX_ROWS 1500→400 (Arrow-native formats
  above); table exports are two-step beyond 200 rows so big frames stop
  serializing CSV on every rerun.
- r13 disposition elsewhere: #1 cache policy deferred until a second viewer
  exists (r9 leak guardrail stands); #4+#17 = query-core v2 design note;
  #5 rides the V27 re-derivation (now with freshness-state + pseudo-warehouse
  riders); #8 self-retires as the mart accrues; #9 readiness-by-freshness
  joins the registry design (the state table is its foundation); #10's hot
  case covered by the hourly tier; #13/#14/#15 = R3/R4/usage-first law;
  #16 declined (sampled + capped, no reliable flush hook); #18/#20 queued
  (registry design / fragments polish round).

Deploy: apply V040 after V039, redeploy.

## 4.30.0 — COST_DB adoptions: the phantom warehouse dies (2026-07-11)

The approved batch from docs/design/COSTDB_RECONCILIATION.md — R1 plus the
quick wins. R3 (storage truth pass) and R4 (client-app cost lens) are queued
behind Compare Phase 2.

- **R1 / V039 (`V039__pseudo_warehouse_filter.sql`).** Accounts emit a
  CLOUD_SERVICES_ONLY row in WAREHOUSE_METERING_HISTORY (WAREHOUSE_ID = 0,
  compute = 0) for cloud services consumed outside any warehouse. Our fact
  loader ingested it: a phantom ALFA "warehouse" — 100% idle in the advisor
  with nothing to suspend, a chargeback-unmapped row, a movers/Compare/boss-
  chart slice. SP_LOAD_HOURLY_FACTS is re-derived VERBATIM from V002 plus
  exactly one predicate (WAREHOUSE_ID > 0 — the docs-sanctioned filter
  COST_DB carried and we missed); equality-locked in tests. Same filter in
  every live WMH builder (daily credits, window-vs-prior, CS ratio, idle,
  sizing, hourly activity, 13-month monthly) and backfill_365.sql (the
  loader mirror). Phantom rows deleted from FACT_WAREHOUSE_DAILY and the
  efficiency mart; eff-mart READERS filter by name until the V27-family
  loader's next planned re-derivation (that proc pair is equality-locked —
  not churned for one row a day).
- **R2.** Per-warehouse dollars say the quiet part: chargeback KPI help and
  the attribution note now state that warehouse totals include unadjusted
  cloud-services credits, with the account-level rebate on Spend.
- **R5.** `_categorize` gains OPENFLOW_COMPUTE_SNOWFLAKE (Serverless) and
  HYBRID_TABLE_REQUESTS (Storage) — Spend's "Other" stays honest.
- **R6.** The ELEVATED cloud-services drill gains a by-QUERY_TYPE cut
  (metadata storms — SHOW/DESCRIBE floods — visible beside compile-heavy
  families). Budget: spend.py 8→9, justified in the pin.
- **R7.** Optimization gains a toggled per-table automatic-clustering scan
  (serverless reclustering credits + TB reclustered — the classic silent
  burner). Budget: optimize.py 2→3, justified.
- **R9.** Contract & Forecast opens with the calendar-year strip: YTD billed
  credits + straight-line projected year total at trailing-30d burn, labeled
  as such (new unclamped fact_daily_spend_year builder — bounded_days'
  90-day default would have silently turned YTD into "last 90 days").
- Canaries for all three new builders; 10 new locks including the V039
  derivation-equality law.

Deploy: apply V039 after V038, redeploy. The phantom disappears from every
panel on the next loader run.

## 4.29.0 — V038: the savings ledger books itself (2026-07-11)

Owner, looking at an empty ledger: "how can we automate the savings ledger
— I don't think anyone will use this. I'm not even using it." Root cause:
booking required executing fixes THROUGH the app, and real changes happen
in Snowsight. Detection already existed — the V024 warehouse-change scan
sees every setting change within a day and measures 14 days of before/after
actuals. V038 connects the two:

- **SP_LEDGER_AUTOBOOK + TASK_LEDGER_AUTOBOOK** (chained after the daily
  change scan): a detected cost-lever change (AUTO_SUSPEND down, SIZE down,
  MAX_CLUSTERS down, SCALING_POLICY STANDARD→ECONOMY) books an ESTIMATED
  item at $0 the day it is seen — no invented numbers, the item is
  pipeline visibility. When the registry verdict lands (~14d), the item
  settles ITSELF: VERIFIED with measured credits/day delta x rate x 30
  ($5/mo noise floor, rate from SETTINGS) or REJECTED with the measured
  evidence in the note. Forward-only — settled items never rewrite.
  VERIFIED_BY = 'AUTO:TASK_LEDGER_AUTOBOOK' keeps auto and human verifies
  distinguishable. Dedupe via new SAVINGS_LEDGER.SOURCE_CHANGE_ID.
- **The migration runs a first pass on apply** against the registry's
  existing 90 days — the ledger stops being empty the moment V038 lands.
- Ledger UI reframed: SOURCE column (auto/manual), caption says the ledger
  books itself, manual add stays for one-offs (index rebuilds, contract
  renegotiations — things no scan can see). Monthly TASK_VERIFY_SAVINGS
  continues to own app-booked auto-suspend estimates; autobook items are
  settled by their own 14d verdicts.

Deploy: apply V038 after V037, redeploy. Nothing else to do — that is the
point.

## 4.28.1 — Compare survives an empty side (2026-07-11)

- Live crash (owner screenshots, both trailing pairings): `pct_delta`
  returns None when the B side is zero — its documented contract — and the
  new KPI delta chip formatted it (`NoneType.__format__`), with the same
  landmine in the volume-shape `round()`. Chips now say "no B-side data";
  the volume table carries a blank delta. Regression locks forbid
  formatting or rounding `pct_delta` output directly in compare.py, and a
  behavioral test pins the empty-B path.

## 4.28.0 — V037 + Compare Phase 1: which warehouses did it? (2026-07-11)

The spreadsheet-killer, built to the design doc (docs/design/COMPARE_MODE.md,
owner decisions 2026-07-11: V037 yes, last-full-month default, promotion
channel authoritative).

- **V037 (`V037__pattern_env_grain.sql`)** — MART_PATTERN_COST_DAILY v2:
  DATABASE_NAME joins the grain while the mart is days old (the Compare env
  lens's measured-$ source), and the USERS metric becomes honest (Codex r11
  #9): V036 stored per-warehouse daily distincts summed and readers took the
  max day — neither is window-distinct. The mart now stores a mergeable
  HLL_ACCUMULATE state; readers HLL_COMBINE + HLL_ESTIMATE, so USERS is a
  true approximate distinct over any day range. CREATE OR REPLACE + fresh
  30d backfill (cheaper than in-place while the mart is young). V030 shape
  law throughout; guard = declared-exception pattern.
- **Compare tab on Cost & Contract (Phase 1, period vs period).** Pairing
  picker: last full month vs prior (default), trailing 7d/30d vs prior —
  the current partial day/month is NEVER a side by default; the labeled
  escape hatch pairs MTD against the same day-count of the prior month
  (equal-length windows or nothing, clamped at short months). Paired KPI
  strip with the r11 #12 corrected grains: warehouse spend from
  FACT_WAREHOUSE_DAILY (exact, company-scopable), queries/fail-rate/queued
  from FACT_QUERY_HOURLY (company-scoped), account-billed from
  FACT_METERING_DAILY (account-wide, labeled so). Warehouse movers
  (paired_bars + table, ranked by |Δ$|), pattern movers (measured
  attribution $ per hash from the V037 mart — new-in-A patterns show B=$0),
  volume shape table. One parallel batch (Brief pattern) with serial
  fallbacks; period math in app/logic/compare.py (pure, boundary-tested).
- **charts.paired_bars** — two-side grouped bars, side B dimmed gray.
- Live-scan budget: compare.py pinned at 0 (mart/fact-only forever).
  Compare canaries anchor to recent windows (the fixed-date lock holds).
- **Design doc updated:** Phase-1 KPI grain table corrected (r11 #12);
  Phase 2 env axis recorded as the ORDERED promotion lane
  DEV→UAT→PREPROD→PRD (r11 #17) — the MGM=PREPROD reconcile lands there,
  replacing the binary PROD/NONPROD classifier (V023 seed stays in sync).
- 13 new locks/behavior tests (test_v037_compare.py): pairing edges incl.
  year boundary + Feb clamp, partial exclusion, grain assertions, injection
  + date validation, V037 guard/HLL/MERGE-key locks, budget-0 pin.

Deploy: apply V037 in Snowsight (after V036), redeploy the app. The
backfill runs inside the migration; the Compare tab and pattern panels
light up as soon as it lands.

## 4.27.0 — Codex r11 fix-first: the batch tells the truth about who failed (2026-07-11)

- **Boss chart coverage gate (r11 #2).** `run_mart_first` gains an optional
  `mart_accept` predicate: a usable-but-thin mart can now defer to the live
  view instead of suppressing it. Overview's monthly-spend chart requires
  12 distinct months from the accruing efficiency mart, else the 13-month
  WAREHOUSE_METERING_HISTORY view serves; a broken predicate accepts the
  mart (never breaks a page); if live cannot answer, the partial mart still
  beats an empty panel. Caption states the rate basis: "Dollars at today's
  $X/credit" (r11 #11).
- **Quarantine only confirmed failers (r11 #4).** A submission failure at
  batch member N used to stamp N and every UNSUBMITTED member with the same
  error — innocent keys got quarantined into solo-run purgatory.
  `_BatchPartial` now carries `pending` (unsubmitted ≠ failed): only the
  member whose submit raised is quarantined; pending members take the
  normal run() fallback untainted.
- **Quarantine heals itself (r11 #5).** A quarantined key's next clean solo
  run removes it from quarantine — it re-batches on the following rerun.
  Salt refresh is no longer the only exit. (The suggested reorder of
  healthy-batch-before-singles is a no-op: both paths complete within one
  run_batch call before anything renders — declined as such.)
- **Prefs attempts only post-identity (r11 #3).** `_apply_default_landing`
  returns without spending an attempt until `_ow_current_user` is hydrated
  — disconnected reruns no longer burn the 3-attempt budget, and the
  pre-identity read (the r9 #1 anonymous-scope cache leak class) cannot
  happen at all.
- **Environment filter stops lying (r11 #1, honesty half).** The
  Environment picker only ever narrowed the Database list, but the scope
  chip and the Scope stat claimed it scoped results. Chip and stat dropped;
  the picker's help now says exactly what it does. The MGM=PREPROD lane
  reconcile (companies.py + the V023-seeded volume scope) rides with the
  Compare env lens, where environments get real grain — the binary
  PROD/NONPROD classifier is replaced there anyway.
- **Canary gaps are declarative (r11 #7).** GAP status now requires the
  entry to be listed in `EXPECTED_GAPS` (the five cortex.* canaries — the
  002139 subscription class). An absent CORE object (mart.*, chargeback.*)
  is drift and FAILS loudly; fresh installs read Migrations & freshness for
  the calm view.
- **Behavioral failure-path tests (r11 #14).** tests/test_codex_r11.py:
  fake-session batch submission/gather failures, quarantine membership,
  rehab on clean solo run, mart_accept fall-through/keep/never-break,
  identity-gated prefs attempts, EXPECTED_GAPS hygiene. Two r10 source
  locks updated WITH the semantics they pin.
- **Repair:** v4.26.2's copy-pass commit shipped `operations.py` with its
  last two dispatch lines truncated (mount cp mid-line truncation that
  still compiled — Pipeline SLA / Release compare sections unreachable).
  Restored; ruff (F821) joins the gate so compile-clean truncations get
  caught before commit.
- r11 disposition elsewhere: #9 pattern USERS grain + #12 Compare doc
  grains + #17 promotion-lane framing land with V037/Compare next session;
  #10/#18/#19/#20 already on the roadmap; #6 #8 #13 #15(bridge) #16
  deferred with reasons (see session notes).

## 4.26.2 — the copy polish pass (2026-07-11)

Owner ask: "people understand what they are seeing but not bogged down
by unnecessary wording." Copy only — no SQL, keys, thresholds, or layout.

- Sweep of every page's captions, help text, info/empty states against
  six editorial rules: captions carry the verdict in one sentence where
  possible; detail moves to help= tooltips; plain words over jargon
  ("reads almost the whole table", not "missing pruning"); honesty
  labels (measured vs allocated, partial-not-a-drop, floor-not-census,
  account-level scope) all survive, said in fewer words; changelog
  archaeology dropped from user-facing copy ("(V021)", "retired at
  V034", "r5 #4 decision", "bulk closes used to skip this" and kin —
  version references stay only on Admin setup hints, where admins act
  on them); personality phrases kept only where they are the clearest
  way to say the thing ("the telemetry picks, not opinions" and
  "nothing groups silently" stay, "the fatigue denominator done right"
  and "revoke fodder" go).
- Files touched: overview, spend, unit_costs, contract, operations,
  security, alerts, control_room, admin. Already at standard, untouched:
  brief, cost, optimize, ai_chargeback, components, ai_panel, charts,
  main.
- One caption lock updated WITH its copy (test_live_round8: break-glass
  panel now pinned to "no alert fires on admin-role use").

## 4.26.1 — the pain table read correctly (2026-07-11)

- Today's Cost & Contract slow keys (reclaim 25s, prune/cachehit 15s) are
  the TOGGLE-GATED deep scans — operator-invoked forensics, slow by
  nature and by design (V038 on-demand pattern). Marting rarely-clicked
  scans is poor value; what they lacked was the last-runtime hint the
  other heavy toggles got in v4.20. Both toggles now show it, so the
  next click comes with an expectation instead of a surprise.

## 4.26.0 — V036: the boss chart + measured pattern costs (2026-07-11)

- **Monthly spend by warehouse** (Overview): stacked monthly bars, company-
  scoped, in-flight month dimmed (partial, not a drop), MoM delta on the
  last full month. Mart-first over MART_WAREHOUSE_EFFICIENCY_DAILY as it
  accrues history; 13-month WAREHOUSE_METERING_HISTORY live fallback until
  then (overview budget 1 -> 2, labeled). Better than the POC's version:
  scoped, sourced, and honest about the partial month.
- **Repeated patterns — the silent spend** (Cost -> Unit costs): measured
  QUERY_ATTRIBUTION_HISTORY compute per parameterized hash (V036 mart,
  daily 3d increment + 30d backfill in-migration), $0.01 floor, sample
  text joined from the family mart. The POC estimates; ours bills.
- Telemetry read honestly: SP_LOAD_MARTS_V27 at 1,144s was the ONE-TIME
  90d backfill (RUNS=1), not a regression — watch the hourly cadence
  number instead. Lock-mart MERGE averaged 84 GB because the 45d backfill
  dominated; if tomorrow's 3d increments stay heavy, the increment window
  gets bounded next round. Today's slow keys (reclaim/cachehit/prune on
  Cost & Contract) are the next marting targets per the pain table.

## 4.25.0 — Codex r10 fix-now batch (2026-07-11)

- **Typed error kinds (#4 — fixed a v4.23.2 bug)**: the friendly error
  formatter had erased the marker text my canary GAP classifier searched
  for, so missing objects still read FAIL. Errors are now classified from
  the RAW exception into QueryResult.error_kind (absent / unknown_function
  / timeout / other); the canary, the AI tab, and probe reads all consume
  the kind instead of parsing prettified strings.
- **Prefs bootstrap commits on success (#1)**: a failed first read retries
  (3 tries) instead of silently skipping your saved landing all session.
- **Batch submission harvest (#3)**: if submission dies at member N, the
  already-submitted jobs' results are collected instead of discarded —
  those queries run server-side either way; dropping handles re-paid them.
- **Session quarantine (#2)**: a key that fails inside a batch runs solo
  from then on while the healthy remainder re-batches smaller and CACHES —
  one persistently broken query no longer makes siblings re-execute every
  rerun. Manual refresh clears the quarantine.
- **Tail-aware row caps (#6)**: only a TRAILING limit counts as the outer
  bound; a subquery's LIMIT no longer disables the cap. Executable tests.
- **Query drill always available (#14)**: manual ID entry no longer
  disappears when the candidates table is empty.
- Already true, verified: #11's cheap half (the fact-window p95 is labeled
  as the PEAK hourly p95 in code and UI). Declined: #7 SiS cancel loop
  (warehouse timeout per RUNBOOK Section 20 is the designed backstop), #12
  salt generations (TTL-bounded; clear() would nuke every user), #9
  sampling weights (the floor is deliberate and labeled). Design-first
  next: #17 Compare mode, then #15 Action-Queue workbench, #19 blast
  radius, #18 predictive SLA; #8+#11-full = mart wave 3 note
  (APPROX_PERCENTILE_ACCUMULATE makes true percentiles buildable).

## 4.24.0 — Codex r9: the correctness batch, four real bugs (2026-07-11)

- **Pre-identity pref caching (#1, real)**: the USER_PREFS landing read ran
  before current_role() hydrated the cache scope, so it cached under the
  anonymous scope — and with SQL text identical across users, one user's
  prefs frame could serve another in-process. Identity now hydrates first.
- **Empty mart revived the giant scan (#2, real)**: run_mart_first treated
  a HEALTHY empty mart as a miss, so "no lock waits" re-paid the 46-56 GB
  live scan to confirm an answer we held. New opt-in empty_is_answer=True
  on the lock panel; marts with young-coverage ambiguity keep the default.
- **One failure re-ran the whole batch (#3, real)**: _execute_batch threw
  away already-computed survivor frames. _BatchPartial now carries them out
  of the cached fetcher (a partial batch is STILL never cached) and only
  failed members retry through run(). History lock evolved with the note.
- **Racy cache-hit sentinel (#4, real)**: _FETCH_MISS was a module dict
  shared across concurrent session threads; now a ContextVar. The batch
  wall-time-split telemetry compromise stays (schema change, deferred).
- **Failed settings reads cached defaults for 5 min (#5, real)**: the
  settings frame cache now raises on not-ok (cache_data never caches a
  raise), so a transient failure costs one render, not five minutes.
- Adoption metric counts first-paint page_visit rows only, plus WAU (#9);
  source-badge microtext 9px -> 11px (#18).
- Deferred with reasons: #6 Overview batching (absent from the slow board —
  telemetry picks batching targets); #7 lazy expanders, #10 SECTION+flow,
  #11 central interaction logging, #19 chart semantics, #17 aria pass
  (next polish round, together); #12 Action Queue workbench and #13
  maintenance windows are feature rounds that deserve design docs; #14
  shareable links queued with breadcrumbs; #20 behavioral tests adopted
  narrowly as this round's locks. Declined: #8 per-statement query tags
  (an ALTER SESSION per fetch doubles statements; key-level telemetry
  already attributes cost); #15 per-panel filter muting (account-level
  labels + legend already declare scope); #16 sticky strip (fights SiS
  DOM under Streamlit 1.45 — revisit on upgrade).

## 4.23.2 — the Cortex Code 002139, traced to us after all (2026-07-10)

- Joe ran the QUERY_HISTORY diagnostic and the caller was the dashboard:
  our AI-chargeback reads of ACCOUNT_USAGE.CORTEX_CODE_*_USAGE_HISTORY.
  We never name SYSTEM$GET_CORTEX_CODE_CLI_SUBSCRIPTION, but those views
  call it INTERNALLY; without a Cortex Code subscription the function
  does not exist (002139), so our query throws it for us. The tab now
  shows a truthful "not available in this account/region" note instead
  of a red box, both reads are probe=True (no error-log/failure spam),
  and probe learned the gated-view class ("Unknown function").
- Canary sweep: feature gaps and pre-migration objects now report GAP,
  not FAIL — absence is not drift; the sweep table stays the alarm and
  stops double-logging to APP_ERROR_LOG.

## 4.23.1 — the Flyway probe stops crying wolf (2026-07-10)

- The Admin Flyway panel always degraded honestly ("Flyway not detected"),
  but the probe's failure still landed in APP_ERROR_LOG and the failed-
  fetch telemetry on every visit — a recurring 'flyway_schema_history does
  not exist' error for a table we know does not exist yet. run() gains
  probe=True for optional-object reads: expected absence is neither
  error-logged nor counted as a failure; every other error on the same
  read still records normally. The panel lights up on its own when
  Flyway lands, exactly as before.

## 4.23.0 — Codex r8 adopts: drill-downs, diagnostics, consistency (2026-07-10)

- **Tuning targets drill down** (#1): click a pain-ranked page on Admin ->
  Performance and the slow keys behind it appear (7d persisted telemetry).
- **"Why stale?" diagnostics** (#16): stale freshness rows now map to their
  likeliest cause — never-backfilled (with the RUNBOOK call), the last
  matching loader error, or a suspended-task hint. This week's deploy-gap
  archaeology, turned into a panel.
- **Lock-wait spike watch** (#13): Control Room flags objects locking >=3x
  their prior 6-day baseline; quiet pre-V035 and when calm. The Operations
  panel also names its source now (#14, house result_caption).
- Consistency set: run_batch callers drop the dead `or {}` (#3 — the
  contract guarantees a dict since v4.20); the new lock readers use the
  shared sql_literal helper (#15 — older locked builders keep their
  pinned text); KPI badges moved from inline CSS to .ow-src-badge theme
  classes (#8).
- Declined with reasons: #2 per-key fallback evidence already exists (the
  bfb: key prefix in persisted telemetry names the failing member); #4
  fleet-p95 toggle hints cost a query to decorate a caption; #7 the legend
  already sits in the topbar; #9 the 8 styled_table holdouts are deliberate
  and locked at <=8; #12 blocker/waiter attribution waits for the mart to
  accumulate real data; #18 AppTests cover smoke and snapshot infra costs
  more than it catches; #20 evidence bundles belong to incidents (wave 2),
  not a parallel export. Deferred: #5 workload split (needs builder recon),
  #6 badges-everywhere (opt-in exists; wire per-panel as touched),
  #10/#11/#17 stay queued per r7 reasoning.

## 4.22.1 — V035 guard fix, owner-diagnosed in Snowsight (2026-07-10)

- **V035's guard used invalid scripting** — RAISE only accepts a DECLAREd
  exception name; the inline RAISE EXCEPTION (code, msg) form fails to
  compile ("unexpected (" — the paren after EXCEPTION). Joe hit it live,
  moved the exception into DECLARE, and it worked; the repo file now
  matches that fix (the house pattern every applied migration V001-V034
  already used — V035 alone drifted). If V035 already applied with the
  hand-fix, re-running the fixed file is safe but unnecessary.
- New repo-wide lock: no migration may ever contain "RAISE EXCEPTION (" —
  the sqlglot gate can't see inside $$ bodies, so this class gets its own
  gate.

## 4.22.0 — V035: page views never scan LOCK_WAIT_HISTORY again (2026-07-10)

- **The lock-wait mart** (live finding, owner's Heaviest-queries panel):
  the contention reads were scanning 46-56 GB at 74-259s per page view —
  the single heaviest thing the app did. MART_LOCK_WAIT_DAILY now carries
  day x db x schema x object x lock-type (with COMPANY via the object's
  database), loaded by TASK_LOCK_WAIT_DAILY on a 3-day increment after the
  daily fact load; the migration backfills 45 days once. The panel reads
  the mart first and keeps the live scan only as the pre-V035 fallback.
- Screenshot triage, recorded honestly: everything else on the board is
  the deploy gap, not new bugs — the 08:44 loader error is V029's shape
  (V030 not applied), the compile_fams compilation error is the alias
  shadow fixed in v4.13, the Brief 8.9s p95 predates the batch split, and
  unused_roles/gov_counts burn 12-32s live because their marts never fill
  while the loader fails. One deploy clears the set.
- Admin's 0% cache / 30s p95 deliberately NOT tuned this round: those
  fetches queue behind the lock scans and loader retries on the same
  warehouse — re-measure after V030-V035 land before touching code.

## 4.21.0 — second-tier batch: environments, badges, the queue (2026-07-10)

- **Lock waits name their environment** (owner ask): DATABASE_NAME and
  SCHEMA_NAME join the contention table so a wait on PC_PAMODIFIER in SAN
  reads differently from the same table in PRD. Grain widened to match;
  never-acquired-first ranking unchanged.
- **KPI cards carry source badges** (r7 #12, the deferred lead): a tiny
  mart/live/stale chip inside the card — trust at eye level instead of a
  caption below the fold. Wired on the Brief's money and criticals cards
  first; any kpi_row item can opt in via "badge".
- **Admin ranks the next tuning targets** (r7 #3, honest version): pain =
  p95 x slow-fetch count from the existing per-page telemetry frame, top
  five — the telemetry picks, not opinions; no speculative "likely fix"
  text, because guessing fixes without reading the code is how reviews
  go wrong.
- Still queued with reasons: prior-period deltas on more charts (#6,
  per-chart work), breadcrumbs (#7, needs UX design against 1.45
  constraints), more heatmaps (#9, where data supports them).

## 4.20.0 — polish batch + partial-success batching (2026-07-10)

Codex r7 adopts (owner-approved, including #1 over the evidence gate):

- **run_batch is partial-success aware**: the cached batch unit stays
  all-or-nothing (failures are never cached), but a failed batch now
  retries PER KEY through run() — individual caching, telemetry and error
  isolation per query; one bad member no longer drags its siblings back
  to serial-cold. Always returns every key; callers unchanged; the
  batch_fallback evidence stream survives.
- **Heavy toggles price themselves**: dormant-user, right-sizing and
  repeat-query toggles show the last observed runtime this session
  before you click.
- **The proc trend is discoverable**: click a $/call leaderboard row and
  the trend panel prefills with that procedure.
- **Legend popover** beside Views: severity colors, mart/live/stale
  source labels, measured/allocated/ESTIMATED-vs-VERIFIED money
  semantics — for operators who didn't sit through the design reviews.
- **30 raw st.dataframe calls migrated to styled_table** (shared
  formatting, pinning, status colors); the 8 holdouts carry bespoke
  kwargs and are deliberate.
- **Docs pass**: CHANGELOG date drift fixed (07-11 -> 07-10), FEATURES.md
  gains the since-v4.9 capability table. Deferred with reasons: KPI
  source badges (#12 — needs kpi_row surgery, next batch), attention
  dashboard (#8 — Control Room IS that view), scorecards/campaigns
  (#10/#11 — owner registry first).

## 4.19.0 — the Brief gets fast + the everything-check (2026-07-10)

App-only release:

- **Brief: ten serial reads -> two tier-grouped batches** (fleet board:
  p95 8.9s, 18/19 fetches slow — unacceptable for the exec page). One
  live batch (health strip, incidents, alerts, actions) + one recent
  batch (exhaustion, savings, app cost, sparkline, digest), serial
  per-query fallbacks unchanged, honesty contract and company scoping
  intact, budget still zero live scans.
- **Full repo sweep came back clean**: migrations V001..V034 contiguous
  with sequential guards; all five replaced procs owned by their latest
  migration (derivation chains intact); 135 canaries; every live-scan
  budget exactly at its pin; zero correlated-subquery or alias-shadow
  landmines in any builder (the sweep's four hits were a docstring and
  three cross-scope CTE false positives — verified by eye).

## 4.18.1 — live round 9: the scorecard's CHANGE_SOURCE runs (2026-07-10)

- **Warehouse-setting-changes panel crashed** ('unsupported subquery type'):
  the v4.16 CHANGE_SOURCE derivation used a CORRELATED aggregate subquery
  (outer w.CHANGED_BY inside a MAX over SETTINGS) — Snowflake rejects that
  shape at runtime and the sqlglot gate can't see it (semantic, and app
  builders aren't gated). Fixed uncorrelated-by-construction: the
  DEPLOY_ACTORS setting resolves once via CROSS JOIN, POSITION runs per
  row. Empty actors still reads honestly as MANUAL/UNKNOWN. Lock forbids
  the correlated shape returning. Lesson recorded with the alias-shadow
  rule: correlated scalar subqueries in select lists are a runtime
  landmine — resolve settings once, join them in.
- Noted from the owner's fleet graphics: Brief p95 8.9s (18/19 fetches
  slow) is the next tuning target; MART_SECTION_DECISION_CURRENT_FLAT in
  the volume-drop panel is the PREVIOUS app's orphaned mart (shared
  schema), not an OVERWATCH loader failure — drops with OVERWATCHV2.

## 4.18.0 — trend one procedure, by name (2026-07-10)

App-only release (owner ask: "can I enter it myself" — yes):

- **Cost & Contract -> Unit costs -> "Trend one procedure"**: type a proc
  name (bare or db.schema-qualified; bare matches any qualification via
  the suffix arm) and get its daily measured $ — total, calls, $/call,
  fails, attributed-calls diagnostics — as the standard bars + 7d-average
  chart plus detail table. Same REGEXP extraction and ROOT_QUERY_ID
  rollup as the $/call leaderboard, so the two always agree; honors the
  page filters (company/database/schema/window) per the triage-filter
  law. On-demand live scan (bounded, cached hourly) — occasional-use
  cost profile, no mart needed.
- Existing answers for context: leaderboard = window totals for EVERY
  proc; Price-a-CALL = one run's children; change-impact = before/after
  around an ALTER. This closes the by-name daily-trend gap.

## 4.17.0 — V034 + live round 8: delivery scoping and the triage-filter law (2026-07-10)

Migration V034 (apply after V033):

- **Teams delivery is ALFA-only for now** (owner decision): ALERT_ROUTES
  gains COMPANY_FILTER ('ALL' default; every existing route flips to
  'ALFA'); sender v4 carries a route's company plus account-level events —
  the open_alert_events convention applied to delivery. The expiry
  watchdog learns the same policy, so company-filtered-out events can
  never spam undelivered_expired. In-app visibility untouched. Derived
  VERBATIM from V026's sender with five enumerated edits, revert-locked.
- **Incidents honor the triage filter everywhere** (live round 8: the new
  section showed both companies under ALFA): open_incidents,
  incident_proposals and incident_metrics all take company (company rows +
  account-level, keys scoped); the Brief chip reads the page filter; and
  the declare flow now matches members on the proposal's COMPANY as well
  as its dedupe family — both companies share rule families, so
  family-only linking could have attached Trexis alerts to an ALFA
  incident.
- **The triage-filter law, recorded**: every new metric/panel takes the
  page filters (company at minimum) at birth — locked per-surface and
  written into the SQL discipline notes so review catches it before
  production does.
- **SEC_BREAK_GLASS_USE retired** (owner: admins know what they are
  doing): rule row deleted, lingering open events resolved EXPECTED,
  activity panel stays as evidence. Muted since V025; gone at V034.
- Tests: tests/test_live_round8.py. 623 green.

## 4.16.0 — V033: attribution + Flyway-readiness + incidents SOP (2026-07-10)

The in-the-meantime batch (migration V033, apply after V032):

- **Who made each warehouse change**: WAREHOUSE_CHANGE_REGISTRY gains
  CHANGED_BY via SP_CHANGE_ATTRIBUTION (hourly, chained after
  TASK_LOAD_HOURLY): best-effort join to the successful ALTER in the 65
  minutes before the snapshot saw the change — evidence, not lineage (the
  V010 rule). MANAGED vs MANUAL derives AT READ TIME from the new
  DEPLOY_ACTORS setting (empty today, so everything honestly reads MANUAL
  or UNKNOWN); the scorecard shows both columns with a caption. The
  OPS_UNMANAGED_CHANGE rule deliberately does NOT ship — an alert with no
  populated actors would be decorative config (review #8); it arrives with
  its scan arm the day DEPLOY_ACTORS is populated.
- **Flyway-readiness**: Admin -> Migrations reads flyway_schema_history
  when it exists (quoted-lowercase, deliberately NOT canaried — absence is
  legitimate until adoption) and says plainly which ledger is
  authoritative; docs/FLYWAY_ADOPTION.md is the adoption runbook (service
  user, DEPLOY_ACTORS entry, baseline-at-tip, compatibility replay, what
  changes vs what stays); snowflake/flyway.toml.example ships key-pair
  auth with cleanDisabled.
- **Incidents are documented and visible**: RUNBOOK section 21 (declare /
  auto-declare / close SOP, metric definitions, attribution semantics);
  the Brief gains an Open-incidents chip (guarded — silent pre-V032).
- Tests: tests/test_prep_iac.py (10 locks). 615 green.

## 4.15.0 — V032: the incident object ships (2026-07-10)

Migration V032 (apply after V031, then RE-RUN roles.sql — operator DML
grants on the two new tables):

- **INCIDENTS + INCIDENT_MEMBERS** — permanent, operator-curated, PRESERVED
  in teardown: alerts, task failures, warehouse changes, DDL, deploys and
  remediations under one lifecycle key. Forward-only statuses; reopen is a
  NEW incident carrying REOPENED_FROM (14-day window, owner-set).
- **Three births, none silent**: manual declare (Control Room, DBA-gated,
  generate-then-run — three statements sharing one session-var id, family
  alerts linked without doubling); auto-declare for CRITICALs
  (SP_INCIDENT_AUTODECLARE chained after TASK_LOAD_HOURLY — one incident
  per dedupe family per 24h, never doubles an open family, SETTINGS
  toggle ON per owner decision); INCIDENT_PROPOSALS view (open alert
  families + nearby warehouse changes — suggestions a human confirms).
- **Lineage becomes joins** (Codex r6 #6): REMEDIATION_LOG +EVENT_ID/
  INCIDENT_ID, SAVINGS_LEDGER +REMEDIATION_ID. Additive; history stays
  NULL — no invented lineage.
- **Control Room -> Incidents**: lifecycle KPI strip (open now, incident
  MTTA/MTTR, reopen rate, alerts-per-incident compression,
  change-correlated % — the IaC payoff number), open-incident queue with
  member detail, close flow with root-cause kind + note,
  incident_declare/incident_close usage events.
- The design doc's IS_RERUN scan rider closed as ALREADY-SAFE: rerun rows
  persist RENDER_MS NULL (V027) and OPS_SLOW_RENDER has filtered NULLs
  since V17 — documented in the migration header.
- IaC hooks (deploy members, OPS_MIGRATION_FAILED, OPS_UNMANAGED_CHANGE)
  still SHIP DISABLED until Flyway/Terraform land, per the design doc.
- Tests: tests/test_v032_incidents.py (11 locks). 604 green.

## 4.14.0 — V031: the tuning trio (2026-07-10)

Migration V031 (apply after V030):

- **Change-impact scan v2** (first replacement since V010; median 278s/call
  was the biggest statement family on the shared warehouse). The
  after-window joins now bound to the OLDEST STILL-TRACKING change
  (:trk_lo) instead of a blanket -18d — nothing tracking means near-zero
  scan — and a cheap ILIKE pre-filter runs before the double-REPLACE
  POSITION match, so full-text normalization only touches plausible CALLs.
  Verdict semantics unchanged; derived VERBATIM from V010 with enumerated
  edits, locked.
- **MART_TAG_COVERAGE_DAILY** closes wave 2's last honest non-adoption:
  day x user tagged/untagged exec-time, loaded hourly under the V030 shape
  law (UDF only ever touches a plain column of an aggregated derived
  table). Freshness view re-emitted with the 17th arm; Cost -> Query-tag
  governance goes mart-first with the live scan as labeled fallback
  (tagcov was p95 35.8s live).
- **lock_contention capped at 7 days** (was 14; ~56GB per run on this
  account). Lock triage is a this-week question.
- Tests: tests/test_live_round7.py (derivation, prefilter counts, tag arm
  shape, reader contract, adoption, freshness 17th arm, clamp). 596 green.

## 4.13.0 — V030: the correct loader shape + measured CALL pricing (2026-07-10)

Live round 6. Migration V030 (apply after V029; run the two backfill CALLs
in the file header once for history):

- **Loader fix 2** — V029's MAX() wrap failed differently: COMPANY_FOR_* is
  a SQL UDF that CORRELATES its argument into a subquery, so the aggregate
  landed inside the inlined WHERE ('invalid aggregate function in where
  clause'). V030 uses the bulletproof shape: aggregate first in a derived
  table, UDF applied to a plain column outside — the same reason the other
  seven arms never failed. Derivation lock chain now V027->V028->V029->V030.
- **Posture snapshot completes the governance inputs** (MFA_GAP_USERS,
  BREAKGLASS_GRANTS_30D join the daily 06:30 arm) and the Security first
  paint reads it mart-first — gov_counts topped the fleet slow-fetch board
  (13 hits, p95 12.3s) and now runs only as the labeled live fallback.
- **Unused roles go mart-first** (p95 32s live) via FACT_QUERY_ROLE_HOURLY
  with a coverage guard: a young fact returns zero rows and the live path
  serves — a role used 60 days ago can never be called unused because the
  fact is 3 days old. Activates after the 90d backfill.
- **Price a CALL or session** (owner question: 'three procs in one session,
  no graph id'): Unit costs gains a measured-pricing panel — paste a CALL's
  QUERY_ID or a SESSION_ID; children roll up via
  QUERY_ATTRIBUTION_HISTORY.ROOT_QUERY_ID (no task graph needed), and a
  single CALL also shows the per-child breakdown ('where the money went
  inside this CALL', root's own time labeled). ~6h lag, idle excluded —
  same caveats as the proc leaderboard, stated on the panel.
- **Primary buttons force dark ink across Streamlit markups** ('2 open
  critical(s)' chip and Execute bulk RESOLVE rendered pale-on-pale): the
  accent-pill rule now covers kind= and data-testid variants with
  !important on every descendant.
- Numbering: incident object -> ~V031, owner registry -> ~V032.
- Tests: tests/test_live_round6.py (9 locks incl. the four-link derivation
  chain). 587 green.

## 4.12.1 — V029 hotfix: the loader arms that never loaded (2026-07-10)

Live round 5. Migration V029 (apply after V028; then optionally run the
90-day backfill CALL noted in the file header once):

- **FACT_QUERY_ROLE_HOURLY / FACT_QUERY_SCHEMA_HOURLY never loaded** — the
  V027 loader's COMPANY expressions called COMPANY_FOR_*() on the raw
  column while GROUP BY covered COALESCE(col, 'NONE'), so both arms failed
  hourly since apply (the per-mart EXCEPTION isolation kept the other
  seven loading, and role share / schema summaries silently used their
  live fallbacks — the fallback pattern worked; the facts were empty).
  V029 replaces the proc, derived VERBATIM from V028's with exactly two
  edits: COMPANY_FOR_*(MAX(COALESCE(col, ''))) — the derivation lock chain
  extends V027 -> V028 -> V029.
- **compile-heavy mart reader crashed Cost & Contract** ('aggregate
  functions cannot be nested'): Snowflake resolved the bare RUNS inside
  later aggregates to the SUM(RUNS) AS RUNS alias. Fixed by qualifying
  every column (f.) in family_compile_heavy — and the same latent bug in
  family_repeat_fingerprints, eff_sizing_profile (behind the sizing
  toggle) and ai_costs_by_model before they fired live. New discipline:
  an output alias must never share a name with a column referenced later
  in the same select list; qualified references cannot be shadowed.
- **Multiselect chips are readable** (Alerts bulk picker was a pale wash):
  dark chip, accent hairline, real text color.
- **Heaviest queries gains the date**: START_TIME first column with a
  Started (MMM DD, HH:mm) format, from the same builder.
- Design-doc numbering: incident object -> ~V030, owner registry ->
  ~V031 (V029 became this hotfix).
- Tests: tests/test_live_round5.py (derivation chain, healed arms,
  qualified-aggregate locks, chip CSS, date column). 574 green.

## 4.12.0 — WAVE 2: the marts take over the panels (2026-07-09)

No migration — app release only (needs V027/V028 applied and the loader
tasks running; every adoption degrades to its live builder otherwise).

- **Ten surfaces go mart-first** via the new components.run_mart_first
  helper (mart read under `<key>_fact`, live fallback under the original
  key, source labeled either way): idle advisor, sizing profile and
  repeat-query scan (optimize — the 90s metering x history joins become
  mart reads), compile-heavy families and allocated attribution (spend —
  the mart path dollarizes ALLOC_CREDITS directly instead of share x
  window), role share (chargeback — keeps BOTH the COMPANY column and the
  TRXS role-heuristic guards), task graphs and schema-filtered summaries
  (operations), schema-filtered 24h pulse and the 48h incident timeline
  (control room), AI by function/model (unit costs — FACT_AI_USAGE_DAILY
  unifies Code + Functions, KPI and panel now share one source).
- **Security posture trend** (Codex r6 #15): 90-day direction per metric
  from MART_SECURITY_POSTURE_DAILY under the governance score — unlocks
  at 2+ days of loader history; default metric follows the V028 10-day
  credential bucket.
- **Eight new contract-matched aggregate readers** in mart27_sql (idle,
  sizing, compile, repeat, role share, allocation, schema summary, AI by
  model) + task_graphs gains full filter parity + the timeline reader
  emits the live column contract (AT/EVENT_TYPE/LABEL, account-level rows
  kept). All canaried. Grain caveats ride the source labels (peak-daily
  p95, exec-time hours, day-grain LAST_RUN).
- **Perf budgets**: six more pages pinned in tests/test_perf_budgets.py
  (optimize 2, spend 8, chargeback 5, operations 25, unit_costs 0,
  security 18) — counts only go down from here. Honest non-adoptions:
  tag coverage (needs user grain the family mart lacks) and pruning
  (needs partition stats) stay live by design.
- **Rider panels** (approved r5, refined r6): Alerts -> History gains
  Delivery health (events delivered, p50/p95 raise->send latency,
  undelivered criticals 30m+, route failures with the RUNBOOK 19 pointer)
  and Alert fatigue (events/week per rule, ACTIONED/NOISE/EXPECTED mix,
  UNTAGGED closes, dedupe repeats). Admin -> Performance gains per-page
  fleet telemetry from the V027 rider (p95, cache-hit % — labeled as a
  floor over persisted fetches — batch size, truncation), usage events by
  EVENT_KIND, and the remediation acceptance funnel (executed/copied/
  failed -> estimated/verified/rejected + verified $, audit rows only).
  Overview promotes forecast quality to a page-level readout (per-engine
  ±MAPE, most-reliable engine named) with the monthly evidence kept in
  the expander.
- **Bulk RESOLVE now requires a resolution kind** (r6 #11, verified: the
  single flow forced it since V021, bulk skipped it — untagged closes
  fell out of the precision score). **Reverse guidance** (r6 #18):
  remediation.reverse_hint at the resize and closed-loop exec sites —
  points at WAREHOUSE_CHANGE_REGISTRY for the prior value and
  REMEDIATION_LOG.STATEMENT_SQL for what ran, never invents values.
  **Usage events**: alert_ack / alert_resolve (single + bulk) and
  remediation_exec now log through log_ui_event (r6 #7).
- Tests: tests/test_wave2_adoptions.py (14 locks) +
  tests/test_wave2_riders.py (12 locks). 568 green + floor leg.

## 4.11.0 — V028: live round 4 — replay scope, 10-day creds, readable trends, driver inventory (2026-07-09)

Migration V028 (apply after V027, then validate.sql — expects V001..V028):

- **Credential expiry policy: 30d -> 10d** (owner decision). One UPDATE to
  ALERT_CONFIG.THRESHOLD_NUM (the scan reads it at runtime, since V009) +
  the posture mart bucket follows (metric EXPIRING_CRED_10D). The bucket
  ships as a replacement SP_LOAD_MARTS_V27 derived VERBATIM from V027 with
  exactly two edits — a lock asserts the equality so the copies can't
  drift. App side moves with it: Security panel (10-day horizon), export
  pack sheet, governance counts + deduction message, canary.
- **Day replay now honors the company filter on every metric** (live
  finding: Trexis rows under an ALFA replay). day_spend_movers /
  day_activity / day_task_failures / day_alerts all take company —
  baselines scoped too, alerts keep account-level rows (open_alert_events
  convention); both batch and serial call sites pass the scope and the
  caption says so.
- **Spend trend redesigned** (owner: "not sure what they mean… people will
  ask", twice). Daily bars + 7-day average line instead of the gradient
  wash; the newest day renders dimmed with a caption naming the 24h
  metering lag (partial, not a drop); the forecast band rectangle is gone
  — the Projected month-end KPI already carries the range. Caption states
  window total + week-over-week pace.
- **Security Changes redesigned**: kind-stacked daily bars (create /
  alter / drop / grants) with a statements-by-user bar beside it — the
  chart now answers "what kind" and "who", not just "how many".
- **Client driver inventory** (Security -> Clients, owner ask): driver +
  version parsed from SESSIONS.CLIENT_APPLICATION_ID, PROGRAM from the
  client-reported CLIENT_ENVIRONMENT (VS Code/DBeaver report; many ODBC
  tools like Erwin don't — labeled honestly), users/sessions/first/last
  seen, and BEHIND vs the newest version of the same driver seen in the
  account (padded segment compare, so 3.10 > 3.9). CSV export; canaried.
- README migration list de-duplicated (V021-V026 appeared twice) and both
  install lists gain V027/V028. RUNBOOK: spend-trend + Security sections
  refreshed, new §20 on the SiS 600s statement-timeout restart loop (the
  p95 601s / 33-fails signature) and idle-cost bounds.
- Tests: tests/test_live_round4.py (12 locks incl. the V027/V028 proc
  equality); spend-trend locks in test_ui_round4/test_stress updated to
  the bar design. 542 tests green.

## 4.10.0 — V027: the mart family ships (2026-07-08)

Migration V027 (apply after V026, then re-run roles.sql + validate.sql):

- Nine scheduled marts per the approved design: warehouse efficiency,
  query families (top 2000/day by exec time), role-hour + schema-hour
  query facts, cost allocation (exec-time share of each warehouse-hour,
  four dimensions), task-graph daily, security posture history, 48h
  incident timeline, AI usage (Cortex Code per user + Functions per
  model).
- ONE loader, SP_LOAD_MARTS_V27(scope, days_back): hourly leg chained
  AFTER TASK_LOAD_HOURLY, daily leg AFTER TASK_LOAD_DAILY; per-mart
  EXCEPTION isolation (one mart's source drift never starves the rest);
  MERGE-idempotent on every grain; the migration runs a first fill so
  panels aren't empty until the next task tick. Backfill calls the SAME
  proc with big windows (one loading codepath).
- MART_SOURCE_FRESHNESS gains all nine arms — the freshness board and
  stale labels cover the new marts unchanged.
- Telemetry rider: APP_QUERY_TELEMETRY + CACHE_HIT (real detection via
  the fetcher-body sentinel, not an elapsed-ms guess), SQL_HASH,
  BATCH_SIZE, TRUNCATED; APP_USAGE + EVENT_KIND/IS_RERUN with sampled
  (10%) rerun rows (RENDER_MS NULL so the first-paint p95 sentinel stays
  honest) and interaction events (saved_view_apply, csv_export via
  components.log_ui_event). App inserts degrade gracefully pre-apply.
- Readers for all nine marts (app/data/mart27_sql.py), all canaried.
- WAVE 2 (deliberately separate): panel adoptions go fact-first once the
  marts hold data — adopting before data exists only exercises fallbacks.

## 4.9.1 — visual pass (Codex round 4, Streamlit-reality-checked) (2026-07-08)

Eleven adopted, four declined with rationale, five deferred. Streamlit 1.45
constraints shaped the calls: no sticky positioning, no side drawers.

- FIXED: the spend-trend area gradient had both stops at offset 0.0 — a
  flat wash that never faded (Codex caught a real rendering bug). Now a
  transparent-floor -> accent fade.
- KPI rows cap at four cards and wrap (five-up rows cramped laptops).
- Alerts KPIs are severity-colored (critical=red rail, high=amber) and the
  bulk-execute button is primary — faster reads under pressure.
- The warehouse/user/schema contains-filters collapse into "More filters",
  auto-expanded whenever one is active so a live filter can never hide.
- Compact density toggle (Views popover): tighter cards/tables for triage
  screens; hierarchy and colors unchanged. Session-scoped v1.
- Calm surfaces: hover motion removed (border/shadow response only),
  radii tightened 12/16 -> 8/12, heading letter-spacing zeroed (the
  uppercase kicker tracking stays — that's a label convention).
- Budget line on the spend trend now labels itself without hover
  (screenshots and phones); 💾 emoji retired from the Views control.
- Scope (company · env · days) rides the persistent status bar — the
  1.45-compatible answer to "sticky filter bar".
- Declined: freshness-caption reduction (that trust surface caught a live
  regression; it gets quieter, not fewer), drawer detail views (no side
  drawers in Streamlit; dialogs hide the list being triaged), storm view
  (already exists — Alerts "Group by rule"), broad semantic recolor (cyan
  is the deliberate data brand; status colors already live where status
  does). Deferred: panel-shell component, small multiples, DAG polish,
  Brief redesign (it already has the Now/Fires/Asks bands).

## 4.9.0 — Teams-safe delivery (V026), docs sync, mart-family design (2026-07-08)

- FIXED (V026, sender v3): webhook payloads are JSON-escaped before the
  integration body template splices them into a JSON string — raw newlines
  (the LISTAGG separator + prefix) and quotes in alert titles produced
  invalid JSON: Slack partially tolerated it, Microsoft Teams Workflows
  rejected the card (the "text card" error and the hourly
  route_send_failed rows). CHR()-code escaping only — backslashes don't
  survive multiple string layers (V022/CALLs+ lessons). Everything else in
  the sender is byte-identical to v2.
- webhook_delivery.sql v2: real Microsoft Teams (Workflows) recipe — the
  retired O365 {"text"} shape replaced with the Adaptive-Card
  WEBHOOK_BODY_TEMPLATE, ALERT_ROUTES row, 202-Accepted note; teardown
  covers OVERWATCH_WEBHOOK_TEAMS. RUNBOOK §19: setup + symptom->fix table.
- Docs synced to v4.9: README/DEPLOYMENT migration lists through V026,
  RUNBOOK object table V021-V026, FEATURES "Cost intelligence (v4.7-4.9)"
  section, ARCHITECTURE "Performance model" (fact-first, join-then-group,
  tier batching, telemetry loop).
- docs/design/V027_MART_FAMILY.md: the designed mart batch (9 marts +
  telemetry schema rider, grains, cadences, loader isolation, backfill,
  bookkeeping checklist). Build order finalizes on ~3 days of v4.9
  sampled telemetry.

Deploy: apply V026 in Snowsight (after V025), re-run validate.sql
(V001..V026), recreate the Teams integration per webhook_delivery.sql v2.

## 4.8.4 — Codex round 3: the migration-contract bug + on-demand heavies (2026-07-08)

Round 3 was mostly the already-queued V026 mart family; five items were
actionable now. Best catch of the round was real: Admin's expected-migrations
dict stopped at V020, so the panel could report "all applied" while
V021-V025 were missing.

- FIXED (#1): _EXPECTED_MIGRATIONS covers V021-V025 — and a new CI lock
  scrapes snowflake/migrations/ so the dict AND validate.sql can never
  trail the repo again.
- #5: the right-sizing profile (the ~90s Optimization scan) is on-demand
  behind a toggle; the idle advisor stays default.
- #12: the Security access-review pack fetches all ten sheets in one
  parallel batch (serial cached fallback kept).
- #15: batch_fallback telemetry now records tier, batch size, keys, and
  exception class — the data that decides whether partial-success
  batching (#16) is ever worth building.
- #20 (test half): hot pages carry pinned live-scan budgets — a new
  ACCOUNT_USAGE reference on Brief/Overview/Control Room fails CI with
  instructions to go fact-first instead.
- Deferred to the designed V026 batch: schema/role query facts, warehouse
  efficiency + query-family + cost-allocation + task-graph + incident +
  security + AI marts (#2-4, #6-11, #13-14), telemetry schema additions
  (#17, #19). Declined: #18 (sampled+capped telemetry is <=60 async
  inserts/session; buffering saves little and risks losing the tail).

## 4.8.3 — Codex round 2: caching economics + the healthy baseline (2026-07-08)

Nine of the twenty adopted (several improved on); the mart-family items
(role/schema facts, optimization/security/timeline/graph marts) are deferred
to a designed V026 batch, and partial-success batching is declined until the
batch_fallback telemetry says it matters.

- Health strip fetched+parsed ONCE per rerun in main() and passed to the
  sidebar strip, top bar, and status bar (#1 — third time Codex flagged it,
  now actually fixed rather than argued with).
- run_batch covers all four tiers (live/metadata added) (#2).
- Overview UN-batched (#4, Codex was right): coupling the filter-scoped
  board with the fixed 45d MTD read cold-started the fixed read on every
  filter change. Each read keeps its own cache key now.
- Cost -> Attribution movers and the cloud-services ratio are fact-first
  with live fallback (#5, #6). Improvement on #6: FACT_WAREHOUSE_DAILY
  already stores TOTAL and COMPUTE credits, so cloud services = the
  difference — no migration, contra the recommendation.
- measured_query_costs joins the filtered window FIRST, then aggregates —
  the whole attribution view is never pre-aggregated (#11, same fix the
  graph/proc builders got).
- Unit costs' three reads go out as one parallel batch (#15).
- APP_USAGE.RENDER_MS now spans sidebar/topbar/status chrome (#18).
- Telemetry persists a ~2% sample of ALL fetches, so the fleet view sees
  the healthy baseline, not just the slow tail (#19).

## 4.8.2 — perf pass: fewer scans, parallel first paints (2026-07-08)

Codex-informed review, verified against our own telemetry (renders 63%
sub-50ms; the pain is warehouse scans). No behavior changes — same numbers,
fewer/faster queries.

- Optimization ran the identical idle-warehouse scan twice under different
  cache tiers (advisor vs remediation) — different TTLs could even disagree
  about what "idle" is mid-session. One tier, one cache entry, one scan.
- Control Room 24h pulse is fact-first (FACT_QUERY_HOURLY, live fallback,
  p95 labeled "peak hourly") and spend movers read the new
  fact_warehouse_window_vs_prior instead of scanning metering live.
- Overview first paint and day replay batch their independent reads in
  parallel (tier-grouped; serial cached path on any failure).
- The jump box no longer costs queries on normal paints — SHOW WAREHOUSES
  and alert rules load once per session via an explicit "load all" row.
- The 139s attribution family: graph and procedure cost builders prune
  QUERY_ATTRIBUTION_HISTORY to task/CALL queries BEFORE the big GROUP BY.
- Canary release-compare anchors were pinned to 2026-01-01 (a half-year
  scan, 153s); they now anchor 3 days back.
- Declined from the review: cache-scope sharing (SiS runs one container
  per viewer — no cross-user cache exists to share, and it reintroduces
  the USER_PREFS leak class); use_container_width migration (blocked by
  the streamlit 1.45 SiS floor; becomes a shim when the channel moves).

## 4.8.1 — live round 3: six fixes from the first full day on v4.8 (2026-07-08)

- POLICY (V025): SEC_BREAK_GLASS_USE disabled — ACCOUNTADMIN /
  SNOW_ACCOUNTADMINS are this account's routine operating roles, and the
  rule watches only those two. The Security page panel keeps the
  visibility; bulk-resolve the open events as NOISE.
- FIXED: stored-proc $/call leaderboard was empty — the CALL-name regex
  reached Snowflake as 'CALLs+' (the string literal ate the backslash; the
  V022 lesson, one layer deeper). POSIX [[:space:]] now — zero backslashes
  at any layer. $0-attribution procs stay visible with an ATTRIBUTED_CALLS
  count instead of vanishing.
- FIXED: AI unit costs fall back to the Cortex CODE usage views
  (Snowsight/CLI token credits) — that's where this account's AI spend
  actually bills; the Functions/model view stays primary where populated.
- FIXED: Trexis roles no longer leak into ALFA's role-usage chart and day
  replay — new companies.role_clause (name heuristic) on role-grain
  builders (role share, day DDL, day grants).
- CHARTS: axis labels no longer truncate mid-name (labelLimit 260, value
  headroom on bar charts); every daily chart now labels DAYS ("Jul 05")
  instead of "12 PM" hour ticks that read as intra-day data.
- CLARITY: Overview spend KPI documents its warehouse-exact lens; Cost →
  Spend gains "Why totals differ across pages (and vs Snowsight)" with the
  actual split (billed vs warehouse-exact vs storage/transfer).
- New cost builders registered in the canary (column drift pages us, not
  a user); locks updated.

## 4.8.0 — unit costs: the price tag on one query, one CALL, one AI request (2026-07-08)

- NEW (Cost & Contract → Unit costs): MEASURED per-unit dollars, no
  migration needed. Most expensive individual queries (attribution credits,
  idle excluded — the "what did THIS cost" lens, alongside Optimization's
  allocated "who owns the bill" lens); a $/call leaderboard for EVERY
  stored procedure via ROOT_QUERY_ID roll-up (change-impact keeps watching
  the changed ones); AI spend by function + model with $/1M tokens.
  Queries and procedures honor company/Database/Schema (+ warehouse/user
  contains for queries); the Cortex usage view has no database dimension
  and is labeled account-wide.

## 4.7.0 — task-graph cost trends + warehouse change scorecard (2026-07-08)

- NEW (Operations → Task graphs ($)): pipeline cost over time — one row per
  graph run via GRAPH_RUN_GROUP_ID, MEASURED warehouse credits per run
  (QUERY_ATTRIBUTION_HISTORY roll-up, ~6h lag), $/run (allocated), success
  %, p95 wall time, and a CHEAPER/PRICIER/FLAT trend per pipeline. Honors
  the Company, Database, and Schema filters. Serverless task credits are
  listed separately at task-day grain — never smeared across graphs.
- NEW (Operations → Change impact): warehouse setting changes tracked like
  object changes. V024 snapshots SHOW WAREHOUSES daily (this account has no
  ACCOUNT_USAGE.WAREHOUSES), diffs snapshots into WAREHOUSE_CHANGE_REGISTRY,
  freezes a 14-day pre-change baseline ($/day, p95, queue min/day, spill,
  fail %), refreshes the after-window daily until day 14, and raises
  WH_CHANGE_REGRESSION alerts (CRITICAL at 2x $/day). Verdicts live in the
  proc — the page and the alert can never disagree; the UI adds per-metric
  deltas and the credits/day line with the change marked.
- validate.sql expects V001..V024; teardown drops the new task/proc and
  preserves the registry + snapshot tables (frozen baselines are not
  rebuildable). Locks in tests/test_graph_wh_scorecard.py.

## 4.6.4 — live round 2: filters that actually filter + contract truth (2026-07-08)

- FIXED: alert feeds (Brief fires, Alerts queue, Control Room triage,
  Overview counts) now honor the Company filter — Trexis warehouse fires
  no longer surface under an ALFA scope. Account-level events
  (COMPANY='ALL') always show for everyone, deliberately.
- FIXED: the Database picker honors the Environment filter — ALFA + PROD
  offers exactly ALFA_EDW_PRD/ALFA_EDW_MGM, and a lingering DEV pin resets
  when the environment changes. companies.databases_for() shares
  classify_environment with the SQL clause so list and filter cannot drift.
- NEW: Contract & Forecast shows Snowflake's own contract balance when the
  role can see SNOWFLAKE.ORGANIZATION_USAGE — REMAINING_BALANCE_DAILY
  (the balance that burns down daily) + CONTRACT_ITEMS (commit, term
  dates): remaining $, burn/day (down-days only, so renewal top-ups don't
  poison it), runway, on-demand overrun, burn-down chart. Zero config;
  degrades honestly to the SETTINGS flow when org views aren't visible.
- Locks in tests/test_company_env_scope.py (21 tests).

## 4.6.3 — V022 apply failure: comma-eating comment + a parse gate (2026-07-08)

- FIXED: V022's ALERT_DELIVERIES CREATE TABLE was unparseable — the inline
  ROUTE_ID comment swallowed the column-list comma (caught by the user in
  Snowsight; the guard had run, nothing else applied, so re-running the
  fixed file from the top is clean).
- NEW GATE: tests/test_migrations_parse.py parses every migration/script's
  plain SQL with sqlglot (snowflake dialect) — CREATE TABLE/VIEW, INSERT,
  MERGE, UPDATE, DELETE, SELECT — with a real statement splitter that
  respects strings and comments. Scripting blocks and dialect gaps (tasks,
  alerts, grants, procs) remain Snowsight-only. The gate provably fails on
  the exact V022 bug class. sqlglot added to requirements-dev.

## 4.6.2 — Trexis-PROD lock + teardown integration audit (2026-07-08)

- V023's PROD volume scope verified and LOCKED for both companies:
  tests/test_migration_v023.py scrapes the migration's predicate and proves
  TRXS_EDW_PRD / TRXS_GW_DATA_PRD / TRXS_ABC_METADATA_PRD keep alerting
  while every DEV/SIT/SAN database goes quiet — and that the SQL agrees
  with the app's classify_environment, so PROD has one definition.
- Teardown audit (user catch: "do we drop email integrations?"): NO — the
  webhook integration, its URL secret, the email/recipe integrations, and
  the ML forecast model all survived teardown. Now dropped (integrations
  under an ACCOUNTADMIN-labeled block). The teardown-coverage test now
  parses SECRET / NOTIFICATION INTEGRATION / SNOWFLAKE.ML.FORECAST kinds
  across ALL opt-in scripts, so this class can't slip through again.

## 4.6.1 — first live-fire morning: three fixes from real telemetry (2026-07-08)

Migration V023 (apply in order after V022): sweep v4 + scan v9.

- PIPE_VOLUME_DROP scoped to PROD databases (ALFA_EDW_PRD/MGM, *_PRD). The
  first production sweep raised 700+ HIGHs from DEV/SIT scratch and dated
  backup tables — volume collapse only matters where consumers are.
  Cleanup: bulk-resolve the open storm as NOISE (it seeds the
  threshold-suggestion evidence).
- Scan v9: SEC_CRED_EXPIRY no longer filters CREDENTIALS.DELETED_ON — the
  column doesn't exist on this account (sibling of the V020 EXPIRES_AT
  discovery). Without this, applying V020's v8 would swap the hourly
  EXPIRES_AT failure for an hourly DELETED_ON failure.
- App side: expiring_credentials + governance_counts stripped of the same
  phantom column (live Security-page error 2026-07-08 08:06).

Validated by the instrumentation shipped yesterday: the change-impact
tracker flagged SP_ALERT_SCAN as REGRESSED, and the persisted error log
carried the exact failing identifier per hour.

## 4.6.0 — review-debt closure, delivery v3, structure (2026-07-07)

Migration V022 (ALERT_DELIVERIES per-route ledger + SP_NOTIFY_WEBHOOK v3) —
UNTESTED ON LIVE until applied; prove with the fire drill. Re-run roles.sql.

Review debt closed (consolidated 2026-07-08 review):
- Delivery: per-route fan-out (a Slack success no longer starves PagerDuty),
  failed routes retry inside the window, aging-out events flagged loudly.
- Brief refuses to invent numbers: unreachable telemetry renders n/a + a
  warning, ROI shows "app cost unavailable" instead of $0.00.
- Lock waits: never-acquired locks (the worst cases) are counted and ranked
  first instead of being zeroed by COALESCE.
- Storage movers company label: database-grain CASE (was the warehouse CASE
  applied to a database column — everything read ALFA).
- ONE MFA-gap definition app-wide: password-login evidence (30d), governance
  score included; evidence wording updated.
- THRESHOLDS trimmed to the two knobs code reads; WINDOW_HOURS labeled
  informational in the Rules generator; window-anchoring convention
  documented in data/common.py. Contract-dates guard verified already sound.

Structure:
- cost.py (1,290 lines) split: dispatch-only cost.py + cost_parts/{spend,
  contract,ai_chargeback,optimize}; fixtures stub the parts.
- 13 wave-era lock files moved to tests/history_locks/ + tests/README.md map.
- RUNBOOK §18 syncs 4.1→4.6 (objects, drill, precision workflow, trust
  surfaces, cache identity, layout).

## 4.5.1 — formula fact-check: three corrections (2026-07-07)

Every number-producing function hand-verified (tests/test_formula_audit.py
pins the results). Three discrepancies found and fixed:

- allocate_by_share leaked pennies: naive per-part rounding made chargeback
  parts sum to 99.99 against a 100.00 warehouse total. Largest-remainder
  allocation now sums exactly, preserving proportionality.
- Day-replay activity baseline divided by a fixed 14: loader gaps and quiet
  days deflated the baseline and over-flagged replay days. Divides by days
  actually present.
- Cortex per-user 30d projection used an active-day basis (a user active 2
  of 30 days projected at 15x real burn) while the page's rollup used the
  calendar window — the two surfaces disagreed (review finding #11). Both
  now use the calendar basis; AVG_DAILY_CREDITS stays as the intensity
  metric.

Verified correct as-built (no change): credits/billed/pct math, month_days,
contract_pace, flat-series forecast (+collapsing band), scoring weights and
caps, price-per-run bounds, steering math, MTTA/MTTR NULL handling,
restatement anchor, spend-movers per-warehouse baselines.

## 4.5.0 — differentiators: what Snowsight structurally can't do (2026-07-07)

No migration; one OPT-IN script (snowflake/alert_drill.sql).

- Day replay (Control Room): pick a date → spend movers vs each warehouse's
  own 14d baseline, query activity vs baseline, DDL landed, grant changes,
  task failures, alerts — one cross-domain story with worst-first headlines.
- Contract steering (Cost → Contract): the gap to commit in $/day and how
  far the named levers reach (idle tuning + top recurring patterns), with
  an honest coverage verdict. Estimates route through the verifier.
- Blast radius: every warehouse suspend/resize confirmation (sizing panel,
  alert closed-loop) now shows who ran what there in the last 7 days —
  users, roles, tooling tags — before the typed confirm.
- Object TCO: selecting a storage-reclaim row prices the table end to end —
  storage $/mo + reads/writes/last-touch from ACCESS_HISTORY — and calls
  out "refreshed but never read." Degrades honestly on Standard edition.
- Price-a-pattern: pick any recurring fingerprint → observed $/run and a
  bounded estimate at ±size steps (same assumption pair as the what-if).
- Monthly fire drill (opt-in): synthetic CRITICAL on the 1st must be
  delivered AND acked; Admin → Canary scores the streak and time-to-ack.
- Query-tag governance (Cost → Attribution): exec-time-weighted tag
  coverage with the top untagged workloads named.
- Restated-days detector (Admin → Canary): metering days whose rows changed
  ≥48h after close — the receipt when a reported number moves (v1;
  first-reported snapshots would need a snapshot fact).
- New pure modules: replay, steering, drill; day_literal date gate;
  16 unit locks; 10 canary registrations; teardown covers the drill task.

## 4.4.0 — feature-depth batch: the features earn their claims (2026-07-07)

No migration needed (builds on V021's resolution kinds).

- Threshold suggestions from YOUR resolutions: Rules now computes, per rule,
  the threshold that keeps ≥90% of ACTIONED alerts while cutting NOISE —
  with the statistical basis stated. Advice through the same generate-only
  flow; overlapping distributions honestly say "redesign, don't tune."
- Live re-check in the alert drawer: one button re-runs the rule's condition
  against TODAY's data for the event's target and says "condition clear —
  resolve with this as evidence" or "still over." Covers the warehouse-lever
  rules + cloud-services ratio + fail rate.
- Forecast backtest on Overview: retro-runs both engines at day 7/14/21 of
  the last 3 months vs actuals, shows per-engine mean absolute error, and
  names the engine that's been more reliable vs the one configured.
- Platform score history: 30-day retro score from facts + alert history
  (same weights), as a sparkline on the score card and a trend expander —
  the prerequisite for calibrating the admittedly-uncalibrated weights.
- Recurring cost patterns: the expensive-queries view now also groups the
  hour-share allocation by QUERY_PARAMETERIZED_HASH — $/day per pattern,
  where caching/materialization actually pays.
- New pure modules: logic/tuning.py, data/recheck_sql.py; 17 new unit locks;
  4 new canary registrations.

## 4.3.0 — UI performance + display pass, router fixes (2026-07-07)

Interaction latency:
- Fragments: Views popover, right-size what-if, statement export (alert
  drawer and Admin emergency already were) — widget moves rerun panels,
  not pages.
- pandas Styler capped at 1,500 rows; larger tables fall back to
  Arrow-native printf formats (commas traded for paint time, deliberately).
- run_batch adopted on Operations Queries + Contention (one async round
  trip on cold cache); spinners on the heavy scans (repeat-query, storage,
  expensive queries).
- spend_trend and the incident timeline embed their dataset ONCE per chart
  (was once per layer); hour heatmap capped at top-20 rows.

Display:
- Wide tables auto-pin the first column (8+ cols, runtime-guarded).
- Alert tables triage-sort: worst severity first, newest within.
- Display-timezone conversion is now CENTRAL in the table pipeline (naming
  convention on timestamp columns; explicit conversions kept for charts;
  double-conversion guarded by a frame marker). CSVs stay account time.
- Fresh-deploy setup gaps render as one calm info line, not red errors;
  CSV buttons drop to icon-only and skip tiny frames; ops KPIs get sparks.

Router/classifier audit (user-requested):
- FIXED: alert deep-links routed to the Cost page's PRE-consolidation
  section names (Spend/Optimization/Contract) — every COST_* Investigate→
  and fix jump crashed the section radio since design-system D. Renamed to
  the live labels; COST_DEPT_BUDGET_PACE now lands on Chargeback & AI.
- lazy_sections self-heals: a stale saved-view/deep-link section falls back
  to the first label instead of crashing the page.
- New test suite scrapes lazy_sections labels/keys from page source and
  asserts every navigate.py target, jump-box target, and all 26 seeded rule
  ids resolve — section consolidations can never strand deep links again.

## 4.2.0 — cost intelligence + trust batch (2026-07-07)

Migration V021 (RESOLUTION_KIND on ALERT_EVENTS, APP_QUERY_TELEMETRY + purge
task) — re-run snowflake/roles.sql after applying.

- Most expensive queries in allocated dollars (warehouse-hour credits split
  by execution-second share) — Cost → Optimization, canary-registered.
- Interactive right-size what-if: size step + auto-suspend together, shown as
  a bounded monthly range with stated assumptions — extends the sizing panel.
- Storage reclaim: ACCESS_HISTORY read-evidence joins the waste scan; "stale
  AND never read (90d)" shortlist; degrades honestly on Standard edition.
- Alert precision per rule (ACTIONED / NOISE / EXPECTED resolution kinds,
  new picker on resolve) — Alerts → Rules; pre-V021 deployments retry legacy.
- Mart reconciliation: fact totals vs live ACCOUNT_USAGE with drift bands
  (±2% noise / ±5% act) — Admin → Canary.
- Billing truth vs app model: org rate-card dollars for this account vs
  credits x configured rate, by month — Admin → Org spend.
- Fleet query telemetry: slow (≥2s) and failed fetches persisted from every
  viewer session (sampled, capped, fire-and-forget) — Admin → Performance.
- CI: mypy gate on the pure layers (zero findings) + floor-compat job pinned
  to the requirements minimums; devcontainer, Makefile, secrets.toml.example.
- New test files: test_v22_features (25 locks) + test_operator_gating
  (profile navigation via AppTest + lifecycle SQL state gates).

## 4.1.0 — feature waves V012–V020 + hardening pass (2026-07-07)

Everything shipped after the 4.0.0 rebuild, plus a 20-item review pass.

Feature waves (V012–V020, see FEATURES.md for the full map):
- Alert drawer with playbooks, AI explain, inline closed-loop fixes; webhook
  delivery in-chain with per-family routing; anomaly events pre-explained by
  grounded Cortex; morning AI digest.
- Saved views, default landing, per-user display timezone (USER_PREFS, V013).
- Change-impact regression tracker, fingerprint drift, incident correlation
  timeline, savings verifier (ESTIMATED → VERIFIED/REJECTED).
- Role-based Trexis user scoping via COMPANY_FOR_USER (V019);
  WH_TRXS_LINEAGE; CREDENTIALS expiry rule re-enabled on EXPIRATION_DATE (V020).
- Design system D: SVG nav, status bar, sparklines, section consolidation.

Hardening pass (2026-07-07):
- Row caps can no longer be disabled by a column/comment containing the word
  "limit" (word-boundary LIMIT detection in the query engine).
- Python-side "today" now uses the account timezone (America/Chicago) for MTD
  boundaries, forecasts, contract pace, and statement months — no more
  evening-hours day drift under SiS/UTC.
- Transient role-probe failures no longer pin the session to the ANALYST
  profile; the sidebar Refresh also re-resolves the role.
- Cortex COMPLETE now carries a 90s statement timeout; usage logging and the
  error sink write async (page switches and failure paths stop paying a
  blocking INSERT round trip).
- Exported executive-summary HTML escapes every field; sidebar strip escapes
  interpolated text; expired-session errors get a friendly "press Refresh"
  message; page-boundary captions name Python bug types explicitly.
- Altair theme registered via the altair ≥5.5 API (deprecation warning gone,
  altair-6-proof); ruff rule set widened (C4/SIM/PIE/PERF/RUF); CI gets
  concurrency-cancel, pip caching, and a 15-minute timeout; connection
  failures show the underlying reason on the not-connected screen.

## 4.0.0 — ground-up rebuild (2026-07-07)

Full rewrite in a new repo, driven by the 2026-07-07 hostile panel review of
the original OVERWATCH.

- 7 pages (Overview, Control Room, Alerts, Cost & Contract, Operations,
  Security, Admin) replacing 6 shells + ~30 zombie section modules.
- Pure, tested logic layer (formulas, anomaly, forecast, scoring, actions).
- Single SQL-safety module; blind-except ban enforced by ruff in CI.
- Query engine that never caches errors, shows truncation, keys cache by role.
- Mart-first data architecture with versioned migrations (V001–V005),
  dedicated XSMALL warehouse + resource monitor, chained hourly/daily tasks.
- Billed spend now applies `CREDITS_ADJUSTMENT_CLOUD_SERVICES`.
- Rates ($3.68 compute / $2.20 Cortex) moved to `SETTINGS`; admin-gated.
- No synthetic data anywhere: real series or honest empty states.
- ALFA/Trexis hardcoded scoping isolated to `app/companies.py` with
  `KEBARR1 → ALFA` override; code/seed sync covered by a unit test.
