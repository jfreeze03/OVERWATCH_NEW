# Streamlit design recommendations — 50 items — 2026-08-02 (main @ 02335b7, v4.122.0)

Fifty code-verified recommendations for the app's Streamlit design: navigation,
shell chrome, layout, component consistency, tables, charts, write actions, and
feedback. Every item was checked against the current source at the cited lines.
Priority P1 = do next round, P2 = queue, P3 = polish. Effort S/M/L.

**Honored prior decisions (not relitigated):** no `st.tabs` (executes every tab
body — `components.py:95`), no dropdown section nav (hides options — design
review 2026-07-31 rec 8 DECLINE), the alert drawer stays a drawer (rec 9
DECLINE), the three-audience Brief/Overview/Control Room split stays (C12
REFUTED), the token palette is not flattened (rec 17 DECLINE), and shipped
rec1–rec20/A1–A5 work is treated as settled.

**Do-first shortlist (P1×S/M):** 1, 2, 7, 9, 11, 17, 21, 23, 29, 31, 36, 42,
45, 49.

---

## A. Navigation & information architecture

**1. Give Control Room section pills + a deep-link key (P1/M).**
Control Room is the only workflow page rendered as one ~370-line scroll
(pulse → incidents → triage → timeline → lock spikes → movers → freshness →
replay, `control_room.py:245` on), and it has no entry in `PAGE_SECTION_KEYS`
(`navigate.py:13-19`), so alerts, saved views, and `?section=` links can't
target anything inside it. Adopt `lazy_sections` (e.g. Pulse · Incidents &
triage · Timeline & movers · Freshness & replay) — it also stops paying for
below-the-fold reads on first paint.

**2. Break up the Optimization & Savings divider wall (P1/M).**
`cost_parts/optimize.py` is 986 lines of ~10 sequential panels separated by
`st.divider` (idle → sizing → expensive queries → patterns → repeats → object
ledger → storage → efficiency → waste → clustering → remediation → savings
ledger). Add a second-level pill row inside the section (Idle & sizing ·
Queries & patterns · Storage & waste · Remediation & ledger) using the same
`lazy_sections` mechanics with its own key, so each subgroup renders and
queries independently.

**3. Evaluate `st.navigation`/`st.Page` for the top-level nav (P2/L).**
The grouped sidebar radio needs a page-scoped-key workaround to avoid
double-highlight/stale-state bugs (the long comment at `main.py:99-105` and
the `_ow_req_seen` reconciliation at 134-137 document two generations of
fixes). `st.navigation` (Streamlit ≥1.36; local floor is 1.45) provides real
URLs, browser back/forward, and grouped sections natively while keeping the
single-entrypoint dispatch. Gate on SiS runtime support and keep the radio as
the degrade path — consistent with the theme.py "everything degrades safely"
contract.

**4. Add sections to the Jump-to box (P2/S).**
`_global_jump` offers pages, databases, warehouses, and rules
(`main.py:337-359`) but not sections, even though `request_navigation` already
accepts one and `PAGE_SECTION_KEYS` enumerates them. "Section · Cost &
Contract → Unit costs" entries make the box the actual command palette it
wants to be.

**5. Mini-TOC for mega-sections (P2/M).**
Alerts → History renders six KPI/table blocks in sequence (events chart, MTTA/
MTTR, incident lifecycle, delivery SLO, backlog, fatigue — `alerts.py:757-869`);
Security → Access stacks 8+ panels before the auditor export. A one-line chip
row at the top of such sections ("On this section: MTTA · SLO · Fatigue …")
that jumps/anchors would give long sections the orientation the page-level
pills give the page.

**6. Show the active section in the page header (P2/S).**
`page_header` prints page title + subtitle (`components.py:132-154`) but not
which of the 4–7 sections is active; on a scrolled page the pills are
off-screen and nothing says "you are in Unit costs". Append the section to the
scope line (the caption already assembles scope fragments) or render it as a
chip beside the title.

**7. Count badges on section pills (P1/S).**
The Alerts header already loads uncapped severity counts before the pills
render (`alerts.py:653-680`), yet the pills read "Open events" with no number
(`alerts.py:687`). Extend `lazy_sections` to accept optional
`labels_with_counts` ("Open events (12)") wherever the number is already in
hand — zero extra queries, and triage urgency becomes visible from any
section.

**8. Persist the last-visited section per page (P3/S).**
Section state lives in session keys (`ops_section`, `cost_section`, …) and
dies with the session; a returning user always lands on the first pill unless
they saved a view. Persist a small `LAST_SECTION:{page}` pref through the
existing `prefs_sql.upsert_pref_sql` machinery (same pattern as `DISPLAY_TZ`,
`main.py:240-243`).

## B. Shell chrome & global filters

**9. Replace the Window select-slider with a segmented control (P1/S).**
`st.select_slider("Window (days)")` (`main.py:580`) makes a 7-option discrete
choice into a drag interaction — fiddly with a mouse, worse on touch, and the
selected value is the only visible option. `st.segmented_control` (≥1.40)
shows all seven windows and switches in one click; keep the slider as the
fallback when the SiS runtime lacks it.

**10. Drop the status bar's "Scope" stat (P2/S).**
The persistent status bar's first cell prints `{company} · {days}d`
(`main.py:501-504`) directly beneath the filter toolbar whose controls display
exactly that. One of the four always-visible slots is spent restating what's
on screen; give it to something not already visible (e.g. open incidents), or
drop it and let the bar breathe.

**11. Remove the sidebar's duplicate lag note (P1/S).**
rec11 moved the ACCOUNT_USAGE lag note to once per page in `page_header`
(`components.py:152-154`), but the sidebar still carries the same sentence
verbatim (`main.py:150`). Two copies of the same disclaimer are chrome noise —
keep the per-page one, cut the sidebar one.

**12. Split display preferences out of the Views popover, and persist density (P2/S).**
The "Views" popover holds saved views AND the compact-density toggle AND the
timezone picker (`main.py:184-246`); density is also the only preference that
is session-only (`_ow_density` never reaches USER_PREFS, so it resets on every
reload while the timezone survives). Rename to "Views & display" or split into
two popovers, and persist density via the same `upsert_pref_sql` used for
`DISPLAY_TZ`.

**13. Show the sidebar MTD strip in dollars (P2/S).**
The health strip prints `MTD: 12,345 credits` (`main.py:481-483`) while Brief,
Overview, and the status bar all lead with USD for the same figure. The rate is
one `load_settings` call away; print `$45k MTD (12,345 cr)` so the app's most
persistent surface speaks the same unit as every other one.

**14. `initial_sidebar_state="auto"` (P3/S).**
`main.py:13` pins the sidebar expanded; on narrow/mobile viewports it covers
the content on load, and Brief is explicitly the phone surface. `"auto"`
collapses on narrow viewports and stays expanded on desktop.

**15. Replace the 🛰️ favicon (P3/S).**
`page_icon="🛰️"` (`main.py:11`) is the last emoji in the chrome after the SVG
icon pass ("replaces emoji, which render inconsistently across platforms" —
`icons.py:1-4`). Render the brand dot/radar mark to a small PNG and pass that
path so the browser tab matches the app.

**16. Make the Jump-to "load more" an explicit button (P3/S).**
Loading all warehouses + alert rules is triggered by *selecting a fake option*
("More · load all warehouses & alert rules…", `main.py:359-368`) — an option
that mutates state instead of navigating is surprising and invisible to anyone
who doesn't open the list. A small tertiary button under the box ("Load all
targets") says what it does.

## C. Layout & density

**17. Standardize table/chart heights behind tokens (P1/S).**
Pages hardcode at least 15 distinct heights (140–460px): the Brief fires table
is 220 (`brief.py:194`), Overview movers 240 (`overview.py:638`), blast radius
200 (`components.py:1085`), Admin metrics 460, charts get 220/260/280
(`charts.py:376,401,488` vs the 264 default at `charts.py:13`). Define
`TABLE_H_SM/MD/LG` and `CHART_H_SM/MD` in one module so the same content class
gets the same height everywhere and density changes are one edit.

**18. Restructure the alert drawer's action row (P2/S).**
`st.columns([1.1, 1.1, 0.9, 1.9])` (`alerts.py:554`) crams Investigate,
Generate fix, the ACK/RESOLVE radio, and a 500-char note input into one row —
the note gets ~30% width for the drawer's only free-text field. Two rows: nav
buttons + action radio on one, the note full-width beneath, then the
SQL/confirm block in a bordered container.

**19. Collapse the Native-delivery SQL walls (P2/S).**
The Native delivery section `st.code`s two entire repo SQL files inline
(`alerts.py:896-905`) — hundreds of lines of scroll before the section ends.
Wrap each template in a collapsed expander with a `download_text_button`
(component already exists, `components.py:1116`), keeping the two-line blurbs
visible. This is also a trust boundary, not just scroll: when a live webhook
credential was committed into `webhook_delivery.sql` (found 2026-08-02 during
this review; scrubbed on its own branch), this panel rendered the secret on
screen to every viewer. Whatever renders here should be treated as public.

**20. Header-level Export popover (P3/M).**
Overview ends with two side-by-side download buttons (`overview.py:790-796`);
Security and Chargeback have their own zip-export idioms elsewhere on the
page. A compact "Export" popover in the header area (HTML · TXT · CSV pack)
gives every page's exports one predictable home and frees end-of-page real
estate.

## D. Component consistency

**21. Convert the eight raw `st.dataframe` sites to `styled_table` (P1/S).**
`operations.py:587`, `control_room.py:598`, `ai_chargeback.py:172`,
`contract.py:323` and `:564`, `admin.py:472`, `spend.py:246`,
`optimize.py:580` bypass `_render_table` and silently lose status tinting,
convention number formats, timezone conversion, header prettifying, and the
CSV button — `admin.py`'s own comment admits the convention. Mechanical
migration; anything that genuinely needs raw rendering should say why inline.

**22. One heading system (P2/S).**
Brief titles panels with `st.markdown("**Fires**")`/`"**Asks**"`
(`brief.py:184,202`), Operations with `st.markdown("**Task graph (DAG)**")`
(`operations.py:532`), Security with bold-markdown panel titles, while Cost
and Control Room use `section_header` (stripe + icon + badge). Migrate the
markdown-bold titles to `section_header` — it exists precisely so visual
weight is consistent, and it takes severity + icon the bold string can't.

**23. Codify empty-state vocabulary in one helper (P1/S).**
Three idioms coexist: `st.success("No open critical…")` for checked-clean
(`brief.py:192`), `st.info` for not-installed (`guard`,
`components.py:695-701`), and bare captions for absent data ("Fatigue metrics
appear once events exist…", `alerts.py:869`). Add `empty_state(kind=
"clean"|"needs_setup"|"no_data_yet", …)` to components and sweep callers, so
green always means "verified clean" and never "nothing loaded" — this is house
rule 8 as a component.

**24. Upgrade `lazy_sections` pills to `st.segmented_control` (P2/S).**
The pill look is CSS on a radio group keyed off `aria-label` selectors
(`theme.py:157-168`) that the theme's own docstring admits can shift between
Streamlit versions. `st.segmented_control` (≥1.40) is the native widget with
the same single-select semantics — keep the dispatch-one-section contract and
the radio+CSS as the degrade path. (This is *not* the declined dropdown/tabs
change: all options stay visible, one section still renders.)

**25. Unify the AI evaluation surfaces (P2/M).**
The alert drawer's "AI explain" (`alerts.py:512-553`) hand-rolls the same
expander → button → spinner → markdown → save flow that `ai_panel.
ai_evaluation_panel` centralizes (caption with model+rate, grounding-prompt
popover). Extend `ai_panel` with the optional "save hypothesis to event" hook
and delete the one-off, so every AI answer carries the same cost disclosure
and grounding transparency.

**26. Theme the task-graph DAG (P2/S).**
The Graphviz DAG hardcodes pastel fills `#fecaca/#e2e8f0/#bbf7d0`
(`operations.py:546`) — light-theme tints on the dark canvas, with default
black-on-white node text, and no `bgcolor`. Set `bgcolor="transparent"`, node
font color to the ink token, and fills from the status palette so failed/
suspended/healthy match every other surface's red/gray/green.

**27. One placeholder-glyph convention (P3/S).**
Missing values render as `—` on metric cards (`components.py:227`), `–`
(en-dash) in Styler `na_rep` (`components.py:905`), `"n/a"` and `"?"` on Brief
KPIs (`brief.py:89,97`), and `"–"` in sparkline rows (`charts.py:282`). Pick
one glyph for "no value" (em-dash) and reserve `n/a` for "not applicable, and
the help says why".

**28. Give `panel_help` full coverage (P2/M).**
The "ⓘ about this panel" popover exists on 13 panels concentrated in
Operations (5) and Security (3); Overview, Brief, Admin, Spend, Unit costs,
and Compare have zero. The KPI `?` badges cover cards, but tables and charts
on those pages have no "what is this / when red do X". Sweep each section's
lead panel — the component is one call.

## E. Tables

**29. Extract the guarded row-selection idiom into components (P1/S).**
Overview guards sticky `st.dataframe` selections with a `_ov_actions_last`
last-seen key before navigating (`overview.py:536-538`, with a comment
explaining the re-fire loop); Control Room's triage table navigates on raw
selection. One `selectable_nav_table(df, key, on_select)` in components with
the guard built in prevents every future page from rediscovering the
sticky-selection rerun bug.

**30. Make the CSV affordance legible (P2/S).**
The export control is a bare `"⬇"` tertiary button (`components.py:989,1013`)
— a tiny tap target that reads as decoration, and it lives wherever the table
ends. Label it `⬇ CSV`, right-align it in a consistent table-footer row, and
keep the two-step prepare/ready flow for big frames.

**31. Every table declares its size and window (P1/S).**
Row counts surface only on truncation (`components.py:703-707`). Add a
standing `styled_table` caption — "142 rows · last 30d · account time" — built
from `len(df)` and the caller's `days`/`served_days`. It costs one line and
extends the source-labeling honesty rule to table volume.

**32. Registry-driven column help (P2/M).**
`_render_table` prettifies `UPPER_SNAKE` headers (`components.py:851-861`) but
carries no semantics: BILLED vs MEASURED vs ALLOCATED columns explain
themselves only in per-page captions. `metric_registry` is already the
semantic contract — map known column names to `st.column_config.*Column(help=…)`
in `_render_table` so hovering a header answers "which basis is this dollar?"

**33. Delta columns need a non-color direction cue (P2/S).**
`delta_css` is text-color only, red/green (`status_colors.py:119-134`), and
the generic auto-format path doesn't force a leading sign, so under red-green
color-blindness an unsigned delta's direction can vanish. Make the delta
formatter always emit a signed number (`+`/`−`) — the same reasoning that
added shapes to the severity timeline (A2).

**34. Print the timezone note once per page (P3/S).**
`_render_table` prints "Times shown in X (stored in account time)" above
*every* converted table (`components.py:885-890`); a six-table Operations
section says it six times. Move it next to the page-header lag note via a
per-render session flag — the identical dedupe already done for the lag note
(rec11).

## F. Charts

**35. Build takeaway lines into the chart helpers (P1/M).**
`spend_trend` computes its own conclusion caption (total, pace —
`charts.py:134-146`) and Overview hand-writes one for top drivers
(`overview.py:567-569`), but `daily_stacked_usd`, `daily_stacked_count`,
`events_by_day`, `hour_heatmap`, and `waterfall_usd` render bare. Add an
opt-out `takeaway=True` that emits one computed line (top category and share,
worst day, hottest cell) — the "lead with the conclusion" pattern applied
where the data already is, for all ~13 remaining bare charts at once.

**36. Chart helpers render honest empty states (P1/S).**
`spend_trend` silently returns when coercion empties the frame
(`charts.py:97-98`); callers guarded the *pre*-coercion frame, so a
bad-typed column yields blank space — exactly the "empty panels say 'checked,
clean', not blank" violation house rule 8 exists for. Every helper's early
return should `st.caption("No plottable rows for this window.")` instead of
rendering nothing.

**37. Adaptive x-axis ticks by window (P2/S).**
Five helpers pin `tickCount="day"` (`charts.py:106,181,227,260,389`); at 90d+
(the picker offers 180/365) `labelOverlap="greedy"` drops labels
unpredictably. Choose day ticks ≤31d, week ≤120d, month beyond — a small
shared `_time_axis(days)` helper.

**38. One heatmap ramp (P2/S).**
The registered theme defines the house cyan heatmap range (`charts.py:48`) but
`hour_heatmap` overrides it with `scheme="orangered"` (`charts.py:318`).
Either the hot-spot semantics justify orange — then put the orange ramp *in*
the theme with a comment — or use the theme ramp; today it's a one-off.

**39. Standardize legend placement (P3/S).**
The theme default is `orient: "top"` (`charts.py:44`) but the stacked helpers
override to bottom (`charts.py:230,263,422,455`) while `paired_bars` and the
timeline sit at top. Pick one position (top, adjacent to the title the eye
just read) so multi-chart pages don't bounce the reader's gaze.

**40. Chart click-through (P2/M).**
Tables became clickable surfaces (rec10) but charts are dead pixels: clicking
WH_X's segment in the boss chart or a bar in top drivers does nothing.
`st.altair_chart(..., on_select="rerun")` with a point selection can set
`flt_warehouse_contains` or `request_navigation("Operations", "Warehouses",…)`
— same guarded-selection idiom as rec 29.

**41. Dollar-format parity in `paired_bars` and `waterfall_usd` (P3/S).**
`paired_bars` takes `unit="$"` but encodes no axis/tooltip dollar format
(`charts.py:481-487` — tooltip is `,.2f`), while every sibling uses `$,.0f`
axes. Thread the unit into `alt.Axis(format=…)`/tooltip so compare-mode
dollars read like every other dollar.

## G. Forms & write actions

**42. One `confirm_gate()` component for the nine type-to-confirm sites (P1/M).**
Nine hand-rolled gates with drifting phrasing and case-sensitivity:
`alerts.py:580,617`, `alerts.py:474`, `operations.py:958,1011`,
`control_room.py:384,405`, `admin.py:248`, `optimize.py:311,751,865`. Typing
`wh_alfa_admin` fails a `WH_ALFA_ADMIN` gate today. Centralize: consistent
prompt copy, case-insensitive match for object names (exact case only for
action verbs), and the disabled-button binding in one place.

**43. `st.form` for compose-then-submit flows (P2/S).**
Bulk ack/resolve is multiselect + radio + kind + note + confirm
(`alerts.py:602-621`) where every widget interaction reruns the fragment
mid-composition; the SLA register and settings editor are the same shape. The
app uses zero `st.form` today — wrapping these gives batched submit and
Enter-to-submit for free, with no rerun churn while typing.

**44. `st.dialog` for the highest-stakes confirmations (P2/M).**
Emergency levers (`operations.py:958`), kill-query (`operations.py:1011`), and
incident RESOLVE/DECLARE (`control_room.py:384,405`) confirm inline where
ambient page content competes for attention. A modal (≥1.34) isolates "you are
about to change prod" with the SQL preview + gate inside. (Prior DECLINE
covered the alert *drawer*, which stays a drawer — this is single-shot
confirmations only.)

**45. Typed editors in Admin → Settings (P1/M).**
Every setting edits through a generic text input regardless of type
(`admin.py` Settings section), yet the schema is known: `FORECAST_ENGINE` is
an enum (`linear|seasonal|ml_forecast`), `CONTRACT_START/END_DATE` are dates,
budgets/rates are floats with sane bounds (`config.py:37-80`). Dispatch on
`DEFAULT_SETTINGS` value type: `st.selectbox` for enums, `st.date_input` for
dates, `st.number_input` with min/step for numbers — typos become impossible
instead of caught later.

**46. Write-risk parity for the unguarded writes (P2/S).**
Dept budget save (`ai_chargeback.py:428-442`) and the Pipeline-SLA register
(`operations.py:~404`) execute with neither the SQL-preview expander nor any
confirm, while every peer write shows its SQL first. They're low-risk upserts
— type-to-confirm would be overkill — but the "SQL always shown first" rule
should hold everywhere a statement executes.

## H. Feedback, loading & trust

**47. Section-level loading status for multi-read first paints (P2/S).**
Spinners exist on three Optimize scans and the AI panel; Control Room and
Cost → Spend first paints run several mart reads with only Streamlit's
default skeleton. Wrap section prefetch batches in `st.status("Loading Spend &
Attribution — 4 mart reads…")` (collapsing on success) so slow cold paints
read as progress, not a hang — the same transparency `toggle_cost_hint`
already gives on-demand scans.

**48. Refresh feedback, and honest freshness wording (P2/S).**
"Refresh data" (`main.py:142-149`) bumps the salt and reruns with no
acknowledgment, and the sidebar note "Updated 5m ago"
(`components.py:1103-1113`) actually tracks session load/manual refresh — not
data freshness (per-tier TTLs mean data may be fresher). Toast "Caches cleared
— fetching fresh data" on click, and relabel to "Session refreshed 5m ago" or
derive from the newest `result.fetched_at`.

**49. Truncate error walls with detail-on-demand (P1/S).**
`guard()` prints the raw error into `st.error` (`components.py:693`);
Snowflake compilation errors run hundreds of characters with embedded SQL
fragments, dominating panels. Show the first line ("Query failed: …") and put
the full text in a collapsed "Error detail" expander — the connection-error
screen already does exactly this (`main.py:670-672`). Honesty kept, wall
gone.

**50. One palette source with drift enforcement (P1/M).**
The severity hues live in four Python copies (`main.py:389` `_STRIP_COLORS`,
`components.py:157` `_SEV_HEX`, `charts.py:24` `SEV_COLORS`,
`status_colors.py` pairs) that A1 aligned *by hand*, and
`.streamlit/config.toml` carries a fifth, *unaligned* set (`#0b1220/#111a2c/
#e2e8f0` vs tokens `#0a0f1c/#0f1729/#e8eef7`) — native widget menus render on
a slightly different dark than the cards beside them. Create `app/ui/
palette.py` as the single source imported by all four, align config.toml to
the tokens, and add a drift test so the next divergence fails CI instead of
shipping.

---

## Priority × effort summary

| P | S | M | L |
|---|---|---|---|
| **P1** | 7, 9, 11, 17, 21, 23, 29, 31, 36, 49 | 1, 2, 35, 42, 45, 50 | — |
| **P2** | 4, 6, 10, 12, 13, 18, 19, 22, 24, 26, 30, 33, 37, 38, 43, 46, 47, 48 | 5, 25, 28, 32, 40, 44 | 3 |
| **P3** | 8, 14, 15, 16, 27, 34, 39, 41 | 20 | — |

Sequencing note: 21+23+31 (table/empty-state consistency) share files and land
well as one sweep; 42–46 (write-action ergonomics) are one thematic round; 50
should precede any further chart work so new colors have a single home.
