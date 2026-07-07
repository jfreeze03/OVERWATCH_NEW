# CoCo Review — Assessment & Recommendation (2026-07-07)

CoCo scored the app 68/100. Before acting on any of it, this document grades
the review itself, because roughly a quarter of it is factually wrong about
what the app does, another chunk attacks deliberate trade-offs without
engaging the reasons, and the remainder — call it 55-60% — is sharp and worth
money. Act on the last group, rebut the first, and make conscious decisions
on the middle.

**Bottom line up front:** accept ~14 findings (mostly polish + hardening,
listed in §5 with effort), reject ~9 as factually stale or wrong (receipts in
§2), and treat 6 as architecture decisions where our position is defensible
but worth a documented pros/cons call (§4). CoCo's two "40-hour" flagship
recommendations — predictive contract-breach alerting and Cortex-powered
anomaly explanation — are respectively **already ~80% built** and **already
~70% built**; both need a finishing pass, not a build.

---

## 1. Grading the reviewer

| Dimension | Grade | Why |
|---|---|---|
| Snowflake depth | Strong | The CREDITS_ADJUSTMENT nod, ACCOUNT_USAGE latency question, and RLS observation are real expertise. |
| Factual accuracy | Weak | At least 9 "missing" features exist in the repo it reviewed (§2). It praises the savings ledger and V013 saved views, then claims the app "doesn't fire" alerts — same codebase. |
| Consistency | Mixed | Kills Release Compare as "belongs in CI," then praises Change Impact, which is the same idea done better. Demands "query regression fingerprinting" as an innovation — that is V010, verbatim. |
| Usefulness | High | Even the wrong claims tell us what a skeptical outsider will *assume* — which is itself a documentation problem worth fixing. |

The most valuable meta-finding: **if CoCo couldn't find these features, a new
DBA or an auditor won't either.** Several "build it" items below are actually
"surface it" items.

---

## 2. Factually wrong or stale — with receipts

| CoCo's claim | Reality in the repo |
|---|---|
| "Alerting with webhook delivery... you generate SQL, you don't fire" | `SP_NOTIFY_WEBHOOK` + `TASK_ALERT_NOTIFY` fire via `SYSTEM$SEND_SNOWFLAKE_NOTIFICATION` (webhook_delivery.sql), and V012 adds per-family channel routing (`ALERT_ROUTES`) with per-route failure isolation. |
| "No Notification Integrations" | The delivery path is built on named NOTIFICATION INTEGRATIONs; routes map families to them. |
| "Capacity forecasting — project when contract credits run out" (Feature to ADD) | Cost > Contract has pacing vs contract, **and the renewal planner shows the exhaustion date of the current contract under growth scenarios**. V007 fires budget pace + forecast alerts server-side. |
| "Query anomaly detection — runtime regressions per query fingerprint" (Feature to ADD) | V010 change-impact tracking freezes 14-day baselines per procedure/task and alerts on p95/credits/failure regressions, correlated to the exact DDL change and author. Its gap vs CoCo's ask: drift *without* a change event isn't tracked (fair sliver — see §5.9). |
| "Recommendation engine — downsize warehouse X" (Feature to ADD) | Idle advisor + right-sizing simulator + **guarded remediation** that generates the exact ALTER, executes with confirm, writes REMEDIATION_LOG, and books an ESTIMATED savings item the monthly verifier proves or rejects. |
| "Self-healing recommendations with one-click SQL generator" (Innovation) | Same as above — exists. Missing only the queue-time-to-developer-cost framing. |
| "No personalization / customizable dashboards" | Saved views + per-user default landing (V013) — which CoCo itself cites in passing, then forgets. |
| "Emoji-based status indicators... need proper styled status indicators" | Every table has CSS Styler severity tints (status_colors.py, theme-aware light/dark since the UX batch). Emojis remain in exactly one place: the new sidebar health strip (fair — see §5.4). |
| "Cost per business unit — not just warehouse" | Department chargeback exists (V008): warehouse metering = exact billing truth per department + role-share allocated lens. True gap is product-line tags, which the account doesn't have (stated constraint: departments own warehouses; no cost tags). |

Also overstated: **"SQL injection — High risk."** Every free-text filter runs
through `clean_filter_text` (character whitelist, control-char rejection) and
values are embedded via escaping literal builders with injection tests in CI.
The genuinely valid sliver: `%` and `_` survive the whitelist, so a user
typing `%` gets wildcard *matching semantics*, not injection. That's a
five-line fix (§5.2), not a High.

And **"session state collisions under 20+ users"** misunderstands SiS:
each viewer gets their own Streamlit session and runs under their own role;
`st.session_state` is per-session by construction. Memory pressure is a fair
question; "collisions" is not.

---

## 3. Where CoCo is right and it stings

1. **"40+ sections is a content dump, not a product."** The strongest
   paragraph in the review. We've been adding capability faster than
   curating it. The Control Room is the right instinct; it needs to become
   the opinionated front door ("the one thing to know right now"), with
   everything else demoted to reference depth.
2. **"Needs migration VX" strings are developer messages.** Users should
   never see them; the Admin page already has the migration checklist.
3. **The platform score weights are uncalibrated magic numbers** (6 pts per
   critical, 0.5/GB spill — verified in scoring.py). Executives will learn
   to ignore a number nobody can defend.
4. **Fact tables grow forever.** No retention/purge task exists. After a
   year of hourly facts this costs real storage and slows the marts.
5. **.txt executive summary** (verified) does look amateur next to the rest.
6. **The audit trail is not append-only** — an owner-role operator could
   UPDATE their own audit rows.
7. **Security page ambition mismatch.** It's genuinely more than CoCo says
   (credential expiry + weekly re-alert, break-glass tracking, Trust Center
   findings, network-policy failures, dormant users, export pack) — but it
   is hygiene + governance, not threat detection. The honest move is to
   *name it* that, or fund the real thing (§5.11).
8. **No incident correlation timeline.** Alerts, task failures, DDL, spend
   spikes each live on their own page. One time axis would be the single
   most Datadog-like upgrade available.
9. **KPIs without direction.** No sparklines; a number without trend is
   half a number.

---

## 4. Deliberate trade-offs — defend or revisit (the pros/cons you asked for)

**4.1 Custom alert scan vs native `CREATE ALERT`**
*Keep the scan (recommended).* Pros of ours: one proc = one warehouse burst
hourly; central dedupe keys; severity escalation; routing; 12+ rules cost ~0
marginal credits; rules are rows, editable in-app. Cons: not visible in
Snowsight's alert UI; bespoke. Native ALERT pros: platform-native, per-alert
serverless schedule; cons: 12+ separately billed serverless schedules, no
shared dedupe/routing, config drifts out of the app. We already ship
`native_alert_templates.sql` for teams that want them — that's the right
hedge. **Action: add a paragraph to ARCHITECTURE.md making this a reasoned
choice, since two reviewers have now read it as unfamiliarity.**

**4.2 Scheduled MERGE facts vs Dynamic Tables**
*Keep MERGE now; pilot one DT (recommended).* MERGE pros: predictable cost on
our dedicated XSMALL under a resource monitor; explicit load procs the
teardown/canary/tests already cover; works identically across editions. DT
pros: less orchestration code, TARGET_LAG semantics, incremental refresh
managed for us. Cons: serverless refresh billing outside our warehouse
budget; refresh behavior is less inspectable; migration churn for zero user
value. **Action: convert exactly one low-risk mart (MART_EXEC_BOARD refresh)
to a DT as a measured pilot; keep facts as-is. Also silences the "allergic to
native features" line with evidence rather than argument.**

**4.3 Python forecast vs `ML.FORECAST`**
*Add ML.FORECAST as a labeled option (recommended).* Ours: transparent
straight-line, honest label, zero credits. ML.FORECAST: seasonality +
confidence intervals (which the renewal planner would genuinely benefit
from), Cortex credits per call, and another privilege dependency. **Action:
setting-gated `FORECAST_ENGINE = linear | ml_forecast`, show both when
enabled. Small effort, kills a talking point, adds real confidence bands.**

**4.4 Hardcoded company scope vs row access policies**
*Keep hardcoded scope as the filter; add RAP only if a compliance driver
appears.* Ours: one reviewed module + seed table + sync test; zero policy
admin; correct for a 2-company shared account where the boundary is
*convenience*, not security (README says exactly this). RAP pros: real
data-level enforcement, auditor-friendly; cons: policy sprawl across every
ACCOUNT_USAGE-derived object (many of which are views we don't own), admin
overhead, and it still doesn't bind SNOWFLAKE.ACCOUNT_USAGE itself — the
account's own views expose both companies to any IMPORTED PRIVILEGES role.
CoCo's "auditors won't accept it's documented" is fair **only if an auditor
is actually in scope** — insurance compliance may get there. Decision is
business, not technical.

**4.5 Multi-account architecture**
*Reject for now.* You have one account by design (ALFA + Trexis share it).
ORGANIZATION_USAGE spend is already in Admin. Rebuilding for 5-50 accounts
you don't have is résumé-driven engineering. Revisit only if an acquisition
adds accounts.

**4.6 Lazy sections' "hidden tab problem"**
*Accepted trade-off; mitigate cheaply.* The alternative was 20+ queries per
page paint (measured). **Action: count badges on section labels where cheap
(e.g. "Savings ledger (3 unverified)") and the §5.13 curation pass.**

---

## 5. The recommendation — prioritized, with effort

**P0 — before any leadership demo (~1 day total)**
1. Purge "Run migration VX" from user-facing empty states → single Admin
   checklist reference. *(2-3 h)*
2. Escape `%`/`_` in `contains_filter` so filter text matches literally.
   *(30 min, closes the review's only legitimate injection-adjacent nit)*
3. Executive summary → styled HTML download (theme, KPIs, action list); keep
   .txt as secondary. *(2-3 h)*
4. Replace health-strip emojis with the CSS badge language used in tables;
   sweep remaining emoji indicators. *(1-2 h)*
5. Score weights → SETTINGS (SCORE_PTS_PER_CRITICAL etc.) with the defaults
   documented as "uncalibrated starting points — tune against incident
   history." Honesty beats false precision. *(2 h)*

**P1 — hardening + the two "finish, don't build" flagships (~40 h, CoCo's own framing)**
6. **Finish predictive contract breach**: wire the renewal planner's
   exhaustion math into the alert scan (fire at T-30/T-14 projected
   exhaustion), optionally with ML.FORECAST bands (4.3). *(6-8 h)*
7. **Finish Cortex anomaly explanation**: when COST_ANOMALY_SWEEP fires,
   auto-assemble the evidence pack (top query-hash deltas, warehouse events,
   DDL in window — the drill queries all exist) and run the existing
   grounded-AI panel over it; store the hypothesis on the event. *(8-10 h)*
8. Fact retention: `SP_PURGE_FACTS` + task (e.g. 400d hourly / 800d daily,
   settings-driven), teardown + tests. *(4 h)*
9. Fingerprint drift detection (no-change regressions): weekly scan of p95
   per QUERY_PARAMETERIZED_HASH vs trailing 28d — closes the sliver V010
   doesn't cover. *(6 h)*
10. Append-only audit: revoke UPDATE/DELETE on ALERT_AUDIT + REMEDIATION_LOG
    from operator role; document owner-role residual risk. *(2 h)*
11. Incident correlation timeline (first cut): one Control Room panel
    stacking alerts + task failures + DDL + anomaly days on a shared time
    axis with ±30 min click-through. *(10-12 h)*
12. Sparklines on Overview/Cost KPI rows; hourly heatmap for the schedule
    advisor; waterfall for attribution. *(6-8 h)*

**P2 — strategic (pick deliberately)**
13. Curation pass: merge "Cortex & Storage"+"AI Users", fold Contention into
    Warehouses, demote Release Compare into Change impact as a mode — takes
    the 40-section count down ~15% with zero capability loss. *(Your call:
    these sections exist because you asked for them; CoCo isn't wrong that
    fewer is better.)*
14. Security decision: **(a)** rename to "Security & Governance hygiene" and
    add the governance-drift score (tables without masking, new ACCOUNTADMIN
    grants, warehouses without monitors — all cheap queries), or **(b)**
    fund real detection: impossible-travel via LOGIN_HISTORY IP deltas,
    grant-velocity anomalies, DATA_TRANSFER egress spikes. (a) is honest and
    ~8 h; (b) is ~40+ h and only worth it if a CISO is actually a
    stakeholder.
15. Timezone setting (display TZ per user via USER_PREFS). *(3 h)*
16. Async fetch (`collect_nowait`) for the 2-3 slowest live tabs. *(8 h)*
17. DT pilot per 4.2; ML.FORECAST per 4.3.
18. Scheduled operator-table clones + documented DR runbook. *(4 h)*

**Explicitly rejected**
- Multi-account rewrite (4.5). Real-time streaming metrics (SiS + ACCOUNT_USAGE
  latency make this dishonest theater; the 30s cache is not the bottleneck,
  the source lag is). Removing Release Compare outright (it's cheap, it's
  used post-deploy; demote, don't delete). Drag-and-drop ad-hoc exploration
  (that's Snowsight's job; our value is opinionated interpretation — CoCo
  itself says to lean into that).

---

## 6. On the scores

68/100 with "Innovation 45" is defensible only against the feature set CoCo
actually registered. Grading the code that exists — server-side prevention
rules, change-anchored regression tracking, verified-savings loop, guarded
remediation, routing, saved views — innovation is the app's *strength*
relative to off-the-shelf tools, and several "Features to ADD" being already
shipped proves the discoverability problem more than the capability one.
Production readiness 62 is closer to fair: retention, audit immutability,
and the timeline gap are real. The correct response to this review is P0+P1
above, an ARCHITECTURE.md section that pre-answers §4's four questions, and
then inviting the same review again.
