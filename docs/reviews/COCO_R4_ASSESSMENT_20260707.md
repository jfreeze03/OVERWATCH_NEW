# CoCo Review Round 4 — Assessment & Recommendations (2026-07-07)

Round 4: 83/100 (68 → 76 → 80 → 83), and the verdict line worth framing:
**"That's a complete monitoring system, not a dashboard."** The review
finally credits the delivery architecture as *correct* ("the separation is:
what to notify in git vs where to send, manual one-time — that's the
correct split") and its dislike list is down to four items. Of those four:
one is a genuine, excellent catch (ROI); two are stale against the snapshot
it reviewed; one is the standing documented decision it now concedes it
understands.

**Bottom line:** accept the ROI finding and build it (the app should prove
it pays for itself); rebut the routing and closed-loop claims with receipts;
convert the SQL-interpolation debate into evidence with an injection fuzz
suite; document the PagerDuty recipe that the routing it missed already
supports. Corrected for the two stale claims, R4 reads ~85.

---

## 1. Grading the reviewer

| Dimension | Grade | Why |
|---|---|---|
| Fix verification | Best behavior yet | It actually verified V018's design point-by-point ("is it actually good? Yes") and credited the correct secret/git split instead of repeating the delivery complaint a fifth time. |
| Factual accuracy | Two stale claims | The severity-routing and closed-loop items below — both disproven by code in the same commit window it reviewed. |
| Honesty | High | "I understand why [the SQL refactor] hasn't moved... no user-visible benefit, risks bugs" — first time a standing decision is engaged on its merits. The "no cosmetic shortcuts, no partial fixes" note about the response pattern is earned and worth keeping. |

## 2. Receipts

| Claim | Reality |
|---|---|
| "SP_NOTIFY_WEBHOOK appears to send everything through one integration... severity-based routing to different endpoints would be the next level" | It IS the current level. The V012 sender cursors over `ALERT_ROUTES (FAMILY, MIN_SEVERITY, INTEGRATION_NAME)` and sends each route through **its own named integration** with a severity-rank filter and per-route failure isolation. `CRITICAL → PagerDuty, HIGH → Slack` is literally two rows: create a second integration whose `WEBHOOK_BODY_TEMPLATE` wraps the message for PagerDuty's Events API, then `INSERT INTO ALERT_ROUTES ('ALL','CRITICAL','OVERWATCH_WEBHOOK_PAGERDUTY')`. What's missing is not the capability — it's the recipe in the docs. Accepted as a documentation item (§4.2). |
| "The closed-loop... seams between them are still manual page navigation" | Stale: the same commit that shipped V018 shipped the **inline closed loop** — for warehouse-lever rules the drawer's "Respond" expander generates the statement, takes the typed confirm, executes, writes the `ALERT_CLOSED_LOOP` audit row, and books the ESTIMATED ledger item. No navigation. Non-warehouse rules keep "Generate fix →" because they need a target picker. Honest sliver accepted in §4.3: the drawer doesn't yet show the *verification outcome* of a fix it booked. |
| "Minor nitpick: the default should be 48h" | It is the default — "48h (fresh)" is the first radio option; Streamlit selects index 0. |
| "Everything else from R1/R2/R3 has been addressed" + the still-open table | Fair, with two notes: "no warehouse isolation" has been documented since R1 as *inverted* (the app is isolated on its own XSMALL; that's the design), and the curation items (Release Compare, AI Users merge, emoji icons) are parked on APP_USAGE adoption data — the mechanism this process built precisely so those calls are made with numbers. |

## 3. The one that lands: the app cannot state its own ROI

Correct, and the best product observation in four rounds. Every piece
exists — `SAVINGS_LEDGER` holds VERIFIED_USD with timestamps, the app's own
cost is measurable on its dedicated warehouse — but no surface says
**"OVERWATCH verified $X in savings this quarter against $Y of run cost."**
That sentence is the tool's continued-existence argument, and it belongs on
the Brief, where leadership already looks.

**Accepted (P0):** quarter-to-date ROI on the Brief — verified savings
(only VERIFIED, never mixed with estimates, per the ledger's own honesty
rule), the estimated-pipeline figure labeled as such, and the app's own
quarterly warehouse cost beside them. Green when verified > cost.

## 4. The plan

**P0 (~half day):**
1. **ROI on the Brief** (§3): `savings_summary_quarter()` +
   `app_cost_quarter()` readers, KPI row addition, FEATURES/RUNBOOK rows.
2. **Multi-channel routing recipe** (§2): PagerDuty + Slack-channel
   examples in `webhook_delivery.sql` comments and the RUNBOOK delivery
   section — two integrations, two `ALERT_ROUTES` rows, done. Turns the
   review's "next level" into copy-paste.
3. This assessment into `docs/reviews/`.

**P1 (~1 day):**
4. **Injection fuzz suite** — convert the standing SQL-construction
   decision from argument into evidence: an adversarial corpus (quote
   variants, LIKE metacharacters, comment tokens, semicolons, unicode
   controls) run through every filter-accepting builder, asserting the
   payload never survives unescaped and statements stay single. This is
   the compensating control a pen tester gets handed on day one, and the
   answer to "if this app ever gets a pen test, this is finding #1."
5. **Closed-loop verification chip** (§2 sliver): the drawer's Respond
   expander shows the ledger state of fixes booked from that event
   (ESTIMATED → VERIFIED/REJECTED), closing the visual loop end-to-end.

**P2 (product, owner's call — unchanged):** Native App packaging ·
multi-account · curation once APP_USAGE has a month of data · design
system refresh. These are the same four items three rounds running; they
are decisions about what this tool wants to be, not gaps in what it is.

## 5. On the scores and the arc

68 → 76 → 80 → 83 across four rounds, with production readiness moving
62 → 85. Correct the two stale claims and R4 is ~85. The reviewer's own
closing framework is the right one to keep: detect → explain → notify →
investigate → remediate → verify, all present. After P0/P1 here, the app
also *prices itself* — and at that point the review cycle has done its job:
everything left on any list is either taste, scale, or a documented
decision with receipts.
