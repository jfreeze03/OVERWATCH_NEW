# CoCo Review Round 3 — Assessment & Recommendations (2026-07-07)

Round 3: 80/100 (68 → 76 → 80), "the engineering is now solid... the
remaining gap is product decisions, not engineering deficiencies." The
review is the fairest of the three and its sharpest finding — the incident
timeline's cache tier — is correct and accepted. Two claims are factually
stale (one for the third consecutive round), and its "biggest improvement"
list contains one item that deserves to finally be settled structurally
rather than re-rebutted: delivery.

**Bottom line:** accept five findings (timeline cost is the headline), take
the delivery criticism seriously *as a packaging problem* even though the
wiring claim is wrong, and build the closed-loop drawer it correctly calls
"the product." Corrected for the two stale claims, R3 ≈ 82.

---

## 1. Grading the reviewer

| Dimension | Grade | Why |
|---|---|---|
| Technical judgment | Best of the three | The tier="recent" timeline-cost catch (including the OPS_SLOW_RENDER irony), the EXECUTE IMMEDIATE consistency note, and the alert-storm question are all real operator thinking. Crediting APPROX_PERCENTILE as the *correct* choice shows it reads intent, not just code. |
| Factual accuracy | Two misses | "Governance drift weights still hardcoded" is false — `GOV_PTS_*` settings + `resolve_gov_weights` shipped in the same batch as V017 it otherwise reviewed (config.py, governance.py, tests). And the delivery claim repeats for the third round (§3). |
| Fairness | High | Explicitly credits the methodical criticism-addressing; resolves its own prior items honestly; "didn't verify the sweep body, but the claim is there" is at least transparent this time. |

## 2. Receipts (short list this round)

| Claim | Reality |
|---|---|
| "Governance drift weights are still hardcoded... apply the same pattern" | Applied already: six `GOV_PTS_*` SETTINGS keys, `resolve_gov_weights()` with fallback, Security page passes them, tested (`test_p0_polish.py::test_gov_weights_configurable`). |
| "Cortex anomaly explanation — claimed, didn't verify" | Real, twice: sweep v3 (V016) appends a grounded hypothesis to fresh anomaly events server-side (capped 5/run, `Using ONLY this evidence` prompt), and the drawer has on-demand explain with operator store-back. |
| "webhook_delivery.sql isn't wired into the scan" (3rd round) | `TASK_ALERT_NOTIFY` is created chained `AFTER TASK_ALERT_SCAN`; the sender walks `ALERT_ROUTES` per family with per-route failure isolation. The *task ships suspended* because the NOTIFICATION INTEGRATION holds a webhook secret that cannot be committed to a repo — resuming it is the one-line install step. |
| "No dark mode" | Status tints are theme-aware (paired light/dark palettes with runtime detection); the rest is Streamlit's own theming, by design. |
| "Closed-loop still not wired together" | Partially stale: the drawer now has playbook + Investigate→ + **Generate fix→** landing on the remediation surface with the event's scope applied, and execution is audited + ledger-booked + monthly-verified. What's honestly missing is CoCo's *single-surface* version — accepted in §4.3. |

## 3. The delivery question — settle it structurally

Three rounds of "detection without delivery is theater" against wiring that
exists but ships suspended means the *packaging* is the problem: an
evaluator (and therefore a new DBA) reads "opt-in file outside the numbered
chain" as "disconnected." Accept that lesson without accepting the claim:

**V018 makes delivery first-class.** Move `ALERT_ROUTES` + `SP_NOTIFY_WEBHOOK`
+ `TASK_ALERT_NOTIFY` into the numbered migration chain; the migration
auto-resumes the task **if** the integration already exists (guarded check,
no-op otherwise); the Alerts page gets a **delivery-status chip** — integration
present? notify task started? last successful send? — so "who gets paged at
2am?" is answered by the UI, green or red, instead of by archaeology. Add an
optional guarded send of the morning digest through the same route (the
"AI-narrated Brief in Slack" CoCo asks for — the narrative already exists as
DAILY_DIGEST; delivery is one guarded CALL).

## 4. Accepted findings

1. **Incident timeline cache tier (its best catch).** Correct, including
   the irony that OPS_SLOW_RENDER would fire on Control Room first. But
   `tier="historical"` is the *wrong* fix for an incident view — freshness
   matters mid-incident. Right fix: **default the window to 48h at
   `recent`** (small scans, fresh) with a **7d option served at
   `historical`** (1h cache for the retrospective view). Also make the
   drill's exception caption name the offending column instead of the
   generic apology. *(small)*
2. **Alert-storm rollup.** Fair: 5 warehouses over budget = 5 rows. Add a
   **group-by-rule toggle** on Open events (rule, count, worst severity,
   newest) with expand-to-events — display-level rollup, dedupe semantics
   untouched. *(small-moderate)*
3. **Closed-loop single surface ("that's the product").** Correct. For
   warehouse-scoped rules the drawer should show the *generated statement
   inline* — playbook, SQL, typed confirm, execute, audit row, ESTIMATED
   ledger item — no navigation hop. Warehouse-lever rules get inline
   execution; table-scoped rules keep Generate fix→ (they need a picker).
   *(the P1 centerpiece)*
4. **What-if load on the renewal planner.** Cheap and real: one "add
   hypothetical daily credits" input reprojects the exhaustion date and
   scenario table. *(small)*
5. **Brief should carry the AI narrative.** The digest already *is* the
   3-sentence morning summary — surface it on Brief (it renders on
   Overview only today), and V018's guarded send delivers it to Slack.
   *(small)*
6. **Housekeeping notes, accepted as documentation:** sentinel's
   EXECUTE IMMEDIATE gets a comment stating why it's exempt from the
   sqlsafe rule (hardcoded array, no user input); RUNBOOK gains the scan
   testing strategy (structural CI assertions + live sentinel +
   OPS_SCAN_DEGRADED as the runtime failure detector) and the note that
   the Monday 05:30 sentinel piggybacks the morning batch's warehouse
   resume; FEATURES.md gets a "guarded by tests, update with features"
   header line. DT pilot cost measurement documented (refreshes run on
   WH_ALFA_OVERWATCH, visible in Admin self-cost).

## 5. Declined / parked

- **Native App + Marketplace packaging, multi-account** — same YAGNI ruling
  as R1/R2: one account, two companies, internal tool. Revisit if another
  account appears.
- **Cost section merges / Release Compare** — parked on APP_USAGE adoption
  data, which is now accumulating for exactly this decision.
- **Emoji nav icons** — decorative taste, not status semantics (those are
  CSS badges since P0). Owner's call, zero engineering.
- **Parameterized-SQL / RAP** — position unchanged and now documented in
  ARCHITECTURE.md "Deliberate choices"; R3 notably softened here.
- **In-app onboarding flow** — FEATURES.md + per-panel help popovers +
  RUNBOOK cover it for a two-company internal team; a guided tour is
  polish, not posture. Parked.

## 6. The plan

**P0 (~half day):** timeline 48h/7d tiering + drill caption (§4.1) ·
digest on Brief (§4.5) · planner what-if input (§4.4) · doc notes (§4.6).

**P1 (~2 days):** **V018 delivery-first-class** — routes/notify/task in the
chain, guarded auto-resume, Alerts delivery-status chip, guarded digest
send (§3) · **closed-loop drawer** — inline generated fix + confirm +
execute + audit + ledger for warehouse-scoped rules (§4.3) · alert-storm
group-by-rule toggle (§4.2).

**P2 (product decisions, owner's call):** Native App packaging · section
curation once APP_USAGE has a month of data · onboarding tour.

## 7. On the trajectory

68 → 76 → 80, with the reviewer conceding the gap is now "product
decisions, not engineering deficiencies." Correct the two stale claims and
R3 reads ~82. After P1, the three items every round has circled — delivery
visibility, the closed loop, and timeline cost — are closed, and what
remains on any future review is taste: packaging, theming, curation. That
is the definition of done for this phase.
