# CoCo Review Round 2 — Assessment & Recommendation (2026-07-07)

Round 2 scored 76/100 (+8) and concedes "8 of my top 12 criticisms addressed
in a single iteration... no shortcuts, no cosmetic fixes." Same treatment as
Round 1: grade the reviewer, receipts for what it got wrong, accept what it
got right, and a prioritized plan.

**Bottom line up front:** the review's single best finding is real and
serious — the monolithic alert-scan INSERT is a ticking bomb (§3.1) and
should be decomposed before anything else. Beyond that: ~9 factual errors
(§2), several of them *repeats from Round 1 that were already rebutted with
receipts* — including three features it lists as "still missing" that exist
in the exact snapshot it reviewed. Its two "biggest improvement"
recommendations are, respectively, ~85% built (closed loop) and 100% built
(Cortex anomaly narratives). Corrected for errors, the real score is ~80.

---

## 1. Grading the reviewer

| Dimension | Grade | Why |
|---|---|---|
| Fairness of deltas | Good | +17 innovation / +12 production-readiness track what shipped; the per-gap quality table is honest. |
| Factual accuracy | Weak, and **repeat-offending** | The webhook/notification claim was disproven with file receipts in Round 1 and is repeated verbatim ("apparently not wired to the scan proc" — it is chained AFTER the scan, with per-family routing). The incident timeline and Cortex explanations are called "still missing" while `control_room.py` and sweep v3 in the same snapshot contain them. |
| Depth | Best yet | The monolithic-scan risk, the migration-order gap, the render-time SLA idea, and the governance-weights inconsistency are all sharp, code-level, and correct. |
| Meta-lesson | Unchanged | If a motivated reviewer misses features **twice**, discoverability is the product's real gap: the README/feature map undersells what exists. §5 adds a FEATURES index for exactly this. |

## 2. Factually wrong — with receipts

| Claim | Reality |
|---|---|
| "Incident correlation timeline — no unified view... you have the data but no temporal correlation view" | Control Room has it: alerts + task failures + DDL on one 7-day axis, severity-colored, click any row for everything ±30 minutes (`mart_sql.incident_timeline`, `charts.event_timeline`). |
| "Cortex-powered anomaly explanation — the narrative isn't there" (top remaining innovation) | Both halves shipped: sweep v3 **pre-explains** fresh anomaly events server-side (grounded prompt, capped 5/run, appended to DETAIL before webhook delivery), and the alert drawer has on-demand *Explain with AI* with operator store-back. |
| "Detection without notification is theater... webhook_delivery.sql apparently not wired to the scan proc" | `TASK_ALERT_NOTIFY` is chained `AFTER TASK_ALERT_SCAN`; `SP_NOTIFY_WEBHOOK` v2 sends via `SYSTEM$SEND_SNOWFLAKE_NOTIFICATION` through per-family `ALERT_ROUTES` with per-route failure isolation. The only "opt-in" part is creating the NOTIFICATION INTEGRATION (the webhook secret) — an ACCOUNTADMIN one-time step Snowflake requires, documented in the file header and RUNBOOK. |
| "Remediation.py and playbooks.py exist but aren't connected to the new detection rules" | The loop runs: alert drawer → playbook + Investigate→ (page/section/filters applied) → guarded remediation executes the fix → append-only REMEDIATION_LOG → ESTIMATED ledger item → monthly verifier flips VERIFIED/REJECTED. The honest sliver: no single "generate the fix" button *inside the drawer itself* — accepted in §3.5. |
| "Sparklines only on Brief; main pages still show numbers without inline trends" | Overview has the 14-day spend/queries/failures sparkline strip under the trend. Not on every KPI row (fair sliver), but "only Brief" is wrong. |
| "Emoji status indicators still present (_PAGE_ICONS)" | `_PAGE_ICONS` are decorative nav icons. **Status** emojis were replaced with CSS badges + aria-labels in the P0 pass — which is what Round 1's kill-list item actually named. |
| "Exec summary .txt → didn't verify" / "migration messages → didn't verify" | Verified here: styled HTML summary shipped (P0), and a guard test bans migration-speak outside Admin permanently. |
| "run_batch is new but core execution is still synchronous" | `run_batch` *is* the async path — `to_pandas(block=False)` server-side parallel jobs with serial fallback. Single queries stay synchronous by design (they're one round trip). |
| "COST_ORG_ACCOUNT_CREEP hints at org awareness but no actual org-level view" | Admin → Org spend has per-account currency totals and stacked daily since V009-era; the rule automates it. |
| "Chargeback... no path to precision (QUERY_ATTRIBUTION_HISTORY mentioned but not actively used)" | The view is actively used (V010 measures credits/call by ROOT_QUERY_ID roll-up). And department chargeback is **exact warehouse billing** — only the role lens is allocated, and it says so. Fair sliver: attribution-history could sharpen the role lens (backlog). |

## 3. Right, and worth acting on

1. **The monolithic scan (its best finding, accepted as #1).** SP_ALERT_SCAN
   v6 is one atomic INSERT across ~14 rule blocks: one runtime error (null
   division, revoked view) kills ALL alerting silently until someone looks.
   The sweep already uses per-block isolation; the scan should too.
   → **V017: scan v7 decomposed into per-family isolated INSERTs**, each in
   its own BEGIN/EXCEPTION with APP_ERROR_LOG capture, plus an
   OPS_SCAN_DEGRADED self-alert when any family fails. This is the "app
   monitors itself" principle applied to its own alerting.
2. **Governance weights should follow the platform-score pattern.** Correct
   consistency argument — same fix, `GOV_PTS_*` settings with documented
   defaults. *(small)*
3. **APP_USAGE needs retention + a disclosure note.** Right — it's not in
   SP_PURGE_FACTS and the RUNBOOK doesn't disclose it. → add to the purge
   proc (V017), document collection/retention in RUNBOOK. *(small)*
4. **Contract exhaustion date belongs on the Brief.** "The single number a
   CIO cares about most" — correct, and it's a cheap read of numbers we
   already compute. *(small)*
5. **Alert → fix in one click.** Fair sliver of the closed loop: for rules
   with a mechanical fix (cloud-svc ratio, storage surge, idle-class), the
   drawer should offer "Generate fix →" landing on the remediation panel
   with the target pre-selected — Investigate already carries the filters;
   this carries the intent. *(moderate)*
6. **Migration-order enforcement.** Admin flags drift after the fact; the
   scripts don't self-check. → V017 opens with a version-guard block
   (raises if V016 absent) as the template for every future migration.
   *(small)*
7. **Render-time SLA.** "The app doesn't know if it's working" is fair for
   render latency: → record per-page render ms into APP_USAGE
   (RENDER_MS column, V017), sentinel gains a p95-render check that raises
   OPS_SLOW_RENDER past 5s. The telemetry infra exists; this persists it.
   *(moderate)*
8. **Review docs out of the repo root.** Fair optics point. Done in this
   commit: both assessments now live under `docs/reviews/` (kept for the
   decision trail — the counter-argument that a visible review loop is a
   strength — but out of a first-time visitor's face).
9. **Fingerprint-drift whitelist.** Fair operational nit once real data
   flows — a small SUPPRESSED_FINGERPRINTS table checked by the Monday
   scan. *(backlog until the false-positive rate is observed)*
10. **YoY/QoQ + capacity utilization.** Legitimate; both unlock after
    `backfill_365.sql` is run — panels are a follow-up once the data exists.

## 4. Wrong-or-deliberate, defended (again where necessary)

- **"No snooze/cooldown beyond dedupe keys."** Dedupe keys *are* the
  cooldown, tuned per rule (daily / weekly / once-per-change — RUNBOOK §12
  documents every cadence). Per-event snooze + ownership was offered
  (round-3 item 1) and **deliberately deferred by the owner**; it stays on
  the backlog, not the bug list.
- **"Brief should be an email, not a page."** Scheduled email delivery was
  offered (round-3 item 11) and skipped; native email needs an
  ACCOUNTADMIN-created integration. Still the right next step when wanted —
  the digest task shows the exact pattern to follow.
- **"String-interpolated SQL" (third repeat).** Same posture as Round 1:
  every user input passes a whitelist/escape layer with injection tests in
  CI, LIKE metacharacters are escaped, identifiers are validated. Snowpark
  binds remain a nice-to-have refactor, not an exposure. What IS owed —
  and now added — is the ARCHITECTURE.md paragraph making the
  native-features and SQL-construction choices explicit, so review #3
  stops re-litigating them.
- **"Company scope should be RAP."** Unchanged decision (Round 1 §4.4):
  convenience scope on a shared account, boundary is Snowflake roles;
  revisit only with a compliance driver.
- **Section curation ("AI Users" merge, Release Compare).** Deliberately
  parked pending APP_USAGE adoption data — which now exists precisely to
  settle this with numbers.
- **"Package as a Native App / multi-account / peer benchmarking."**
  Rejected for the same YAGNI reason as Round 1: one account, two
  companies, internal tool.

## 5. The plan

**P0 (~half day):** Brief exhaustion KPI (§3.4) · GOV_PTS_* settings (§3.2)
· APP_USAGE purge + disclosure (§3.3) · ARCHITECTURE.md decisions paragraph
(§4) · FEATURES.md index — one line per capability with its home page, so
the next reviewer (and the next hire) can't miss what exists.

**P1 (~2-3 days):** V017 scan v7 per-family isolation + OPS_SCAN_DEGRADED
(§3.1, the headline) · migration version-guard pattern (§3.6) · render-ms
capture + p95 sentinel check (§3.7) · drawer "Generate fix →" wiring (§3.5).

**P2 (owner's call):** exec email delivery of the Brief (skipped item 11) ·
fingerprint whitelist once FP rate is known · YoY panels after backfill ·
attribution-sharpened role chargeback.

## 6. On the scores

76 accepts the trajectory (68 → 76). Correct the nine errors — timeline,
narratives, delivery, org view, sparklines, statuses, HTML summary,
migration messages, async path — and the honest number is ~80 before P1
lands. The review's own closer is the right summary: the remaining gap
"is no longer architectural — it's workflow and polish." After V017 closes
the scan-isolation risk and the drawer gets its fix button, round 3 should
be arguing about design systems, not correctness.
