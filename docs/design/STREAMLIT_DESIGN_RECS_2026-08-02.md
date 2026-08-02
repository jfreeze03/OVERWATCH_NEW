# Streamlit design — 50 recommendations (2026-08-02)

Full read of the UI layer at v4.122 (`app/main.py`, `app/theme.py`, `app/ui/*`,
all eight pages + six cost parts, `.streamlit/config.toml`; Streamlit pinned
`~=1.45`). Every item was verified at its cited line before inclusion; two
candidate items were dropped because the code disproved them (notably the
claimed remediation double-fetch in `optimize.py:800` — `run()`'s cache identity
is SQL text + scope, `key=` is telemetry-only, so identical SQL shares one
entry).

**Ratings:** P1 = changes what a viewer decides or how fast the page answers;
P2 = consistency/polish with compounding value. S/M/L = touch surface (S = one
file, M = a page or a shared component + call sites, L = cross-cutting).

## Not re-opened (already adjudicated — see DESIGN_REVIEW_2026-07-31)

Dropdown section nav (hides options), modal alert drawer (dense fragment
workflow stays), palette flattening, Brief KPI cap, chip-to-prose collapse,
and the responsive ladder + snapshot-test program are all owner-adjudicated
DECLINEs. Nothing below relitigates them; where an item touches the same area
it is scoped to stay inside those decisions. The Cortex user-attribution
surface keeps its owner-pinned live-first, exact-emails+timestamps shape —
item 30 is display-only.

---

## A. Navigation & information architecture

**1. Replace the hand-rolled sidebar nav with `st.navigation` + callable
`st.Page`s.** (P1/M) `main.py:95-137` implements grouped nav as N per-group
radios with page-scoped widget keys (`_ow_nav_{group}_{current}`) plus a
seen-marker dance to defuse stale deep links — three documented bug classes
live in the comments. Streamlit 1.45's `st.navigation({"Group": [st.Page(...)]})`
gives grouped sections, exactly-one selection, URL sync, and browser
back/forward natively, and lets the `?page=` reconcile code be deleted. Verify
the SiS runtime accepts it first; the radio fallback stays if not.

**2. Replace the `lazy_sections` pill radio with `st.segmented_control`.**
(P2/S) `components.py:92-125` + the `div[role="radiogroup"][aria-label="Section"]`
CSS in `theme.py:157-168` restyle a radio into pills. `st.segmented_control`
(1.40+) is the native widget for this — keyboard/focus handling for free, less
test-id-coupled CSS to break on Streamlit upgrades. Keep the render-one-section
dispatch and the `?section=` deep-link write; both are right. All options stay
visible (this is not the declined dropdown).

**3. Add "Copy link to this view".** (P1/S) Deep links already encode page +
section (`components.py:117-122`, `main.py:109-112`) but nothing surfaces the
URL; saved views are private per user (`USER_PREFS`). One button in the Views
popover (`main.py:180`) that renders the current URL with `?page=&section=`
makes any triage state shareable in Slack — the cheapest collaboration feature
on the board.

**4. Give cross-page drills a return affordance.** (P1/M)
`request_navigation` lands the viewer on Operations/Alerts with filters
silently overwritten (jump box `main.py:369-383`, triage `control_room.py:496-502`,
Overview actions). Remember the origin (page/section/filters) in the pending-nav
payload and render one dismissible "← Back to Control Room" chip under the
header. Closes the loop the 07-31 review's breadcrumb item (rec 5, PARTIAL)
left open.

**5. Sub-navigate the Optimization section.** (P1/M) `optimize.py:136-896` is
eleven panel blocks in one scroll (idle → sizing → expensive → patterns →
repeat → object → growth → efficiency → waste → clustering → remediation).
Add a second-level `lazy_sections` (Idle · Sizing · Queries · Storage ·
Remediation · Ledger): shorter scroll AND the unvisited groups' queries never
run.

**6. Group Security → Access into worklists.** (P1/M) `security.py:37-199` is
eight stacked table panels with equal visual weight (MFA, failed logins,
admins, reasons, new networks, credentials, dormant, unused roles, grants).
Regroup as Identity hygiene / Privileged access / Entitlements with a small
KPI strip up top and only non-clean groups rendered expanded — triage order
instead of an audit dump.

**7. Rename and split Admin → "Canary".** (P2/S) The tab also holds
reconciliation, fire drills, and restatements (`admin.py:637-782`) — the
additivity proofs nobody finds under a name that promises smoke tests. Rename
to "Health checks" with sub-pills (Canaries · Reconciliation · Drills ·
Restatements); same for Performance's four audiences ("This session / Fleet /
Adoption / Acceptance", `admin.py:443-634`).

**8. Promote density + timezone out of the Views popover.** (P2/S) Compact
density and display timezone (`main.py:228-243`) are environment prefs buried
behind Views, and timezone needs an extra "Save timezone" click before
anything changes. Apply on change (persist best-effort in the background) and
move both to the header row beside Legend/Views.

## B. First paint & perceived performance

**9. Lazy-load Overview below the fold.** (P1/M) The page runs the board,
150d facts, live batch, forecast, score batch, score inputs, and health strip
before the first KPI paints (`overview.py:204-436`), and the digest expander's
query runs whether or not it is opened (`overview.py:738-746` — expander bodies
always execute). First paint should be KPI row + Top actions; the monthly
stacked chart, spend trend, score trend, and digest load behind a
toggle/sub-section. (Leaves the adjudicated batching design untouched —
this defers whole sections, it does not re-batch the board/150d/health legs.)

**10. Batch the Alerts entry reads; defer the SHOW-probes.** (P1/M) Entry
serially runs `open_alert_events` then `open_alert_severity_counts` then
`_delivery_status` (4 metadata/live probes, `alerts.py:646-686` + `256-267`)
before the first tile. Brief already shows the pattern (`brief.py:52-68`):
one `run_batch` for the tier-mates, and the delivery SHOW-probes move inside
the Native delivery section where their answer is consumed.

**11. Batch the Admin access self-check.** (P2/S) Seven grant probes run
one-by-one on button click (`admin.py:386-392`). `run_batch` them — same
diagnosis, one round trip.

**12. Batch the Contract steering reads.** (P2/S) `steer_idle` and
`steer_pats` are independent mart-first reads issued serially
(`contract.py:455-466`). Prefetch both mart legs in one `run_batch` and feed
`run_mart_first(preloaded=...)` — the seam already exists (`components.py:536-539`).

**13. Spinners on heavy section loads.** (P2/S) The unit-costs attribution
batch (`unit_costs.py:93`), the spend CS drill (`spend.py:206-226`), and the
Queries failure timeline (~15s, acknowledged at `operations.py:259-266`) render
nothing while they run. `st.spinner("Pricing queries & procedures…")` etc. —
the toggles already do this (`optimize.py:339-342`); the eager paths should too.

**14. Put the Control Room freshness board behind a toggle.** (P2/S) It loads
on every visit (`control_room.py:612` → `200-242`) though the page's job is the
triage queue; day replay next to it already shows the gated pattern
(`control_room.py:616-619`).

**15. Gate Optimization's two eager historical reads.** (P1/S) Object-cost and
storage-growth run on every Optimization visit (`optimize.py:498-598`) while
their five sibling scans are all behind toggles. One "Load cost ledger &
storage movers" toggle brings the section's default paint down to the idle
advisor. (Subsumed by 5 if sub-nav lands first.)

**16. Gate the unit-costs task-graph block.** (P2/S) Graph attribution +
serverless reads run eagerly (`unit_costs.py:359-419`) below an already-heavy
batch; ETL right above it is toggle-gated (`unit_costs.py:296-348`). Parity.

**17. Stop the Admin "Why stale?" expander paying its query every render.**
(P2/S) `run(app_error_log(100), tier="live")` sits directly in the expander
body (`admin.py:300-328`), which executes whether or not it's open — the exact
trap the house already documented at `cost.py:85-86`. Put the read behind a
toggle inside the expander.

**18. Progress feedback on long button-triggered operations.** (P2/S) The
Security auditor export builds a 10-sheet zip with no progress indication
(`security.py:410-421`) — the Canary page already shows the right pattern
(`st.progress`, `admin.py:670-686`). Same for the alert drawer's Cortex explain
and recheck buttons (`alerts.py:393-394`, `533-535`): wrap in
`st.spinner("Assembling evidence…")` so a click never looks dead.

## C. Interaction: fragments, dialogs, selection

**19. Fragment every drill-drawer.** (P1/M) Alerts' open-events fragment
(`alerts.py:326`) proves the pattern; nothing else adopted it. Selecting a
Control Room incident re-runs the whole page including pulse + triage + timeline
queries (`control_room.py:367-390`); same for the timeline ±30m drill
(`552-569`), Overview score-deduction buttons (`overview.py:700-709`), the
Contract planner sliders (`contract.py:523-591`), and Optimize's pattern-price
sliders (`optimize.py:413-427`). `@st.fragment` each: interaction cost drops
from "page" to "panel".

**20. Fragment the Cost storage/unmapped toggle.** (P1/S) Flipping the toggle
(`cost.py:87-108`) re-runs the whole Spend & Attribution section including the
4-read prefetch batch (`cost.py:73-74`). Wrap the toggle + its two panels in a
fragment so detail-on-demand stops re-paying the section's first paint.

**21. Move type-to-confirm operator gates into `st.dialog`.** (P2/M) Incident
close (`control_room.py:377-415`), warehouse resize (`optimize.py:293-332`),
retention change (`optimize.py:720-769`), and the Emergency levers
(`operations.py:958-1013`) inline SQL preview + blast radius + typed confirm
into the main scroll, where any stray rerun scrolls the operator away
mid-confirmation. A modal holds focus until Confirm/Cancel. Scope: short
confirm gates only — the Alerts drawer stays a fragment per the 07-31
adjudication.

**22. Standardize the sticky-selection guard.** (P1/S) Overview guards against
a stale sticky row re-firing navigation (`overview.py:536-538`); Control Room's
triage queue navigates immediately on selection with no guard
(`control_room.py:496-502`). Fold the last-seen-selection guard into
`selectable_table` itself so every navigate-on-select surface gets it for free.

**23. Bulk-ack ergonomics for storms.** (P1/M) The bulk picker is one
multiselect over up to 500 long event labels (`alerts.py:602-606`), and rollup
mode — the storm view — returns early with no bulk actions at all
(`alerts.py:353-355`). Add "select all shown / by severity / by rule"
shortcuts, and in rollup offer "ack all OPEN for this rule". Storms are
exactly when per-event picking fails.

**24. Refresh the evidence after an operator action.** (P1/S) Executing a
resize/suspend/retention fix ends at `notify()` — the advisor row that
motivated it stays on screen until a manual refresh (`optimize.py:293-332`,
`720-769`). Domain-scoped cache salts already exist; bump the relevant salt and
`st.rerun()` so the queue visibly shrinks — the same post-action contract the
Alerts ack flow honors.

**25. Batch the AI-exception queue inserts.** (P2/S) "Execute inserts" loops
`execute_statement` per exception with no busy state (`ai_chargeback.py:236-241`).
Disable the button while running and execute as one statement batch; report
per-row outcomes once.

## D. Tables & data formatting

**26. Convention-based `DatetimeColumn` formatting.** (P1/S)
`_render_table` already recognizes timestampish columns for timezone conversion
(`components.py:833-844`) but leaves display as raw `2026-08-02 14:03:22.123`.
In the same pass, give those columns
`st.column_config.DatetimeColumn(format="MMM D, HH:mm")` (caller config still
wins). One edit; every RAISED_AT/LAST_SUCCESS_LOGIN/GRANT time in the app gets
readable. Finishes the 07-31 review's rec 13 (central table schemas, PARTIAL).

**27. `ProgressColumn` for share/percent columns.** (P2/S) Tagged %
(`cost.py:159-160`), attribution coverage, cache hit %, budget pace all render
as bare numbers. Extend the `_auto_formats` convention (`components.py:801-819`)
so `*_PCT`/`*_SHARE` in 0–100 render as progress bars — scan magnitude without
reading digits.

**28. Extend `_auto_formats` to the columns it misses.** (P2/S) The convention
covers `_USD`/`CREDITS`/`_GB`/counts, but plain `GB`, `TIB`, `USD_MO`-shaped
storage-tier columns (`spend.py:474`), CSR ratio columns (`spend.py:149`), and
the savings ledger's `ESTIMATED/VERIFIED` money columns (`optimize.py:922-924`)
fall through unformatted. Grow the suffix map instead of adding per-site
config.

**29. Route the remaining raw `st.dataframe` sites through `styled_table`.**
(P2/S) Control Room movers (`control_room.py:598-607`), Admin telemetry
(`admin.py:472-478`), storage growth (`optimize.py:580-588`), and the AI user
grid (`ai_chargeback.py:172-183`) bypass the house table — losing status
colors, timezone conversion, auto-formats, and CSV export. Keep their explicit
`column_config`; `styled_table` accepts it.

**30. Make the AI user grid scannable (display-only).** (P2/S)
`ai_chargeback.py:172-183` shows every column of the owner-pinned live scan at
equal weight. Keep exact emails + timestamps present (owner decision), but
default-sort by SPEND_USD, order columns user → spend → tokens → first/last
use, and date-type the timestamps. No source change.

**31. "Showing N of M" above capped tables.** (P1/S) Alerts' true >500 totals
live only in KPI help text (`alerts.py:676-678`) while the table caps at 500;
the CR triage queue has no count/filter affordance at height 260
(`control_room.py:489`). Print the cap line above the table and add
severity/kind filter chips to the queue.

**32. Recommendation-first column order on the idle advisor.** (P2/S) The
table leads with credit anatomy; the decision columns (recommendation,
recoverable $) sit right of the fold (`optimize.py:184-194`). Order decision →
evidence.

**33. Column config for the Rules table.** (P2/S) `styled_table(rules.df)`
bare (`alerts.py:696`): thresholds, enabled flags, and timestamps all
unformatted on the page where an operator decides to mute/tune a rule.

**34. Search box on the metric registry.** (P2/S) A 460px table with no filter
(`admin.py:800`) is the app's semantic contract; finding one metric means
scrolling. `st.text_input` filter + method selectbox above it.

**35. Stop dumping whole SQL template files.** (P2/S) Native delivery renders
two full repo files via `st.code(read_text())` (`alerts.py:896-905`) — hundreds
of lines of scroll. Show the first ~40 lines + a `download_text_button`; the
runbook caption stays.

## E. Charts

**36. Give Security → Access one chart.** (P2/S) The section is 8 tables /
0 charts (`security.py:37-199`; the page's charts all live in Changes/Egress).
A small `bar_count` of failed-login reasons and MFA-gap counts turns the two
worklists people actually scan into shapes.

**37. Contract pace chart.** (P1/M) Pacing is KPI-only (`contract.py:420-437`).
One cumulative-burn line vs a linear commit line (or consumed-share vs
time-share bullet) shows over/under-pace at a glance — this is the page's
single question, and it currently has no picture.

**38. Budget vs MTD bar per department.** (P2/S) Budgets render as a bare
table divorced from spend (`ai_chargeback.py:407-442`) while dept MTD spend is
already loaded on the same panel. One `paired_bars(dept, MTD, budget)` makes
overage visible without cross-referencing.

**39. Complete the Compare grammar.** (P2/S) Warehouse movers get Δ% + chart;
pattern movers get neither (`compare.py:233-240`) and the volume-shape metrics
are a tiny table under a chart (`245-257`). Add DELTA_PCT to patterns and
render volume shape with the existing `paired_bars`.

**40. Spend anomalies: one table, not a warning stack.** (P2/S) Up to five
stacked `st.warning` boxes (`spend.py:408-416`) is the least scannable format
for per-warehouse z/$/direction. One severity-colored table (warehouse, day,
$, z, spike/collapse), warning banner only for the count.

**41. Auto-aggregate long windows.** (P2/M) At 180/365d, `spend_trend` and the
daily stacked charts draw 180–365 skinny bars (`charts.py:104` already shrinks
bars to 4px). Above ~90 points, aggregate to week grain and say so in the
caption — trend legibility over false daily precision.

**42. One sparkline system.** (P2/S) KPI cards embed the SVG `spark_svg`
(`components.py:161-200`) while pages also use the Altair `sparkline_row`
(`charts.py:274-296`) — two visual languages for the same job. Standardize on
`spark_svg` (cheaper, themable) with an optional last-value label, and keep
Altair for full charts.

**43. Chart-level honest empties + one heatmap palette.** (P2/S)
`spend_trend` silently `return`s when coercion empties the frame
(`charts.py:97-98`) — a blank hole where house law (#8) demands "checked,
clean". Render a one-line caption instead. Same pass: `hour_heatmap` hardcodes
`scheme="orangered"` (`charts.py:318`) against the theme's registered cyan
heatmap ramp (`charts.py:48`) — pick one (finishes the 07-31 A5 conventions
item).

## F. Design-system hygiene

**44. Finish the heading grammar.** (P2/M) 92 `st.markdown("**…**")`
pseudo-headers remain across the pages (operations 17, security 15, alerts 11,
optimize 14, …), plus one stray `st.subheader` (`control_room.py:576`), while
the section system exists (`components.py:325-334`). Add a lightweight
`subsection_header()` and sweep — this is the 07-31 review's rec 6 (PARTIAL)
carried to done. Mechanical, safe, and the single biggest consistency win.

**45. Align `.streamlit/config.toml` with the token layer.** (P2/S) Native
theme says `backgroundColor #0b1220 / secondaryBackground #111a2c`
(`config.toml:4-5`); the token layer paints `--ow-bg #0a0f1c / --ow-surface
#0f1729` (`theme.py:22`). Native surfaces (select menus, popovers, toasts)
render a subtly different dark than the cards. Copy the token values into
config.toml, and decide dark-only explicitly (status_colors carries a full
light-mode map, `status_colors.py:23-30`, that nothing can trigger with
`base="dark"` pinned — either wire it or note it as dormant).

**46. De-drift the CSS: dead classes, duplicate blocks, and a lock test.**
(P2/S) `theme.py` defines `.ow-chip` twice with conflicting font-weight/
line-height (`theme.py:129-133` vs `138-143`) and ships a dead `.ow-brand-word`
(`153-155`); `page_header` emits `class="ow-scope-row"` (`components.py:151`)
that no stylesheet defines. Fix the three, then add a small test asserting
every `ow-*` class emitted by components.py exists in theme.py — the same
drift-lock pattern the palette already has.

**47. Scope + clock honesty on Brief and Alerts headers.** (P1/S) Both pages
filter by company but render no scope note (`brief.py:39`, `alerts.py:641` —
Overview/CR do), and Brief's footer stamps browser-local
`pd.Timestamp.now()` (`brief.py:233`) in an app whose law is account time.
`scope_note=f"{company} · MTD is account-wide"` + `account_now()`.

**48. Split the empty-state grammar: clean vs not-installed vs failed.**
(P1/S) Brief renders "Alerting not installed yet." as a green `st.success`
(`brief.py:200`, same at `221`) — absent monitoring displayed as health, the
exact misread house law 8 exists to prevent. Give `guard()`/callers three
states: clean → success, not-installed → info, failed → error. Also the green
half of the 07-31 A4 item.

**49. Fix icon semantics and grow the set.** (P2/S) "AI users" on the Cost
page wears the `operations` icon (`cost.py:167`); `icons.py` holds ~20 glyphs
serving ~40 surfaces, so sections borrow whatever's close. Add ai/user/table
/storage/egress glyphs and re-point the misassignments.

**50. Visible labels + help coverage on dense panels.** (P2/S) The Control
Room timeline window radio hides its label
(`label_visibility="collapsed"`, `control_room.py:532-535`) — hover-only
context. Prefer a visible micro-label, and extend `panel_help` (the "what is
this / when red do X" popover) to the triage queue, incidents, and Security
Access panels; today the timeline has it and its denser neighbors don't.

---

## If only ten ship

1, 9, 19, 20 (interaction + first-paint cost), 22, 24 (drill safety and
closure), 26 (every timestamp readable), 44 (heading sweep), 47+48 (Brief
honesty), 31 (capped-table truth). Items 44/47/48 are pure-app S efforts and
gate-safe; 1 needs a SiS capability check first.

All 50 are app-layer only: no migrations, no loader changes, no new marts.
