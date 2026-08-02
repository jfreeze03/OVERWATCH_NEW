# OVERWATCH Streamlit design — 50 impactful recommendations (2026-08-02, v4.122)

A fresh, whole-app design pass over the Streamlit surface (`app/ui/**`, `app/theme.py`,
`app/main.py`). Every item is grounded at a cited line against the current code and is
written to *not* relitigate settled owner decisions (flat-palette, dropdown-nav, and the
drawer-vs-modal calls in `DESIGN_REVIEW_2026-07-31.md` stay declined — see §9).

The recurring finding is the same one the last two reviews named and only partly closed:
**the design system is excellent in the token layer and almost never enforced at the call
sites.** `theme.py` + `components.py` + `charts.py` + `status_colors.py` are a genuine design
language, but the eight page modules each hand-roll headings, tables, and captions that drift
from it. Most of these 50 are "route the call site through the system you already built,"
which is why the majority are S/M effort and app-only.

Effort: **S** ≤ one function, **M** a page or helper, **L** cross-cutting.
Impact: **P1** users feel it immediately · **P2** polish/consistency · **P3** nice-to-have.

---

## Group A — One component vocabulary (headings, panels, tables)

**1. Kill the three competing heading mechanisms (P1/M).** The app renders section titles
three ways: `section_header()` with a severity stripe (`components.py:325`), bare
`st.markdown("**Title**")` pseudo-headings (30+ sites — `operations.py:138,165,243,532`,
`security.py:68,82,101,111,128,148`, `alerts.py:601,702,809`), and one lone
`st.subheader` (`control_room.py:576`). The bold-markdown form is not a real heading — no
visual weight, no scan rhythm, and screen readers get a `<strong>`, not an `<h3>`. Pick
`section_header()` as the single sub-section primitive and a lighter `subsection()` helper for
the `**bold**` tier; migrate the bold-markdown sites. This is the biggest single scannability
win on the detail pages.

**2. Give every panel a real container (P1/M).** `.ow-card` exists and is beautiful
(`theme.py:69`) but is used only for KPI cards. Sections today are a header followed by naked
tables/charts with no bounding surface, so on long pages (Operations, Security History) it is
hard to see where one panel ends and the next begins. Add a `panel()` context manager
(`st.container(border=True)` + `section_header`) and wrap each logical block; the border the
top-bar already uses (`main.py:550`) is the same affordance.

**3. Extract one `finding_row()` / `action_list()` helper (P1/M).** The same
ACTION_QUEUE/finding data is rendered ≥4 ways: bulleted markdown (`brief.py:210-215`),
`selectable_table` → navigate (`overview.py:528`, `control_room.py:489`), read-only
`styled_table` (`alerts.py`), and ad-hoc `st.markdown` loops (`overview.py:704-709`). Rec 10
from the prior review is still open. Ship `finding_line(row)` and `action_table(df, on_select)`
so Brief/Overview/Control Room speak one grammar for "here is a thing that needs an owner."

**4. Add `subsection_header()` and delete inline `st.markdown("**...**")` (P2/S).** Bundle with
#1: a tiny helper (uppercase micro-label + optional count badge, same tokens as
`.ow-card__title`) replaces the ~40 bold-markdown labels. One function, one look, and it can
carry the row-count/severity badge #6 asks for.

**5. Standardize the "generate-then-run" block into one component (P1/M).** The
review→confirm→execute pattern is copy-pasted across Alerts (`alerts.py:576-597`), Control
Room (`control_room.py:377-390,405-415`), Operations Emergency (`operations.py:952-970`),
Pipeline SLA (`operations.py:404`), and Security. Each hand-rolls `st.code(sql)` + a
type-to-confirm `text_input` + a gated `st.button` + `notify()`. Extract
`operator_action(sql, *, confirm_word, audit_kind, page)` that renders the SQL, the confirm
box, the blast-radius (where relevant), and the execute+audit call. Removes ~150 lines and
guarantees every destructive action looks and behaves identically.

**6. Put live counts/severity on the `lazy_sections` pills (P1/M).** The section radio
(`components.py:92`) is the primary in-page nav on Alerts/Operations/Security but the pills are
static text. "Open events" should read **Open events · 12** and glow red when criticals are
open; "Change impact" should badge the regressed count. The counts are already computed on the
page — thread them into the labels so the reader knows where the fires are before clicking.

**7. Route the two raw `st.dataframe` movers/anomaly tables through the system (P1/S).** Control
Room spend movers (`control_room.py:598-607`) and the Operations warehouse-anomaly table
(`operations.py:587-594`) call `st.dataframe` directly with hand-written `column_config`,
bypassing `styled_table`'s semantic colors, header prettifier, tz conversion, CSV export, and —
critically — the A3 delta styling. The `DELTA_USD`/`DELTA_PCT` columns here are *exactly* the
signed-movement columns `delta_css` was built to color (`status_colors.py:119`), yet they
render flat because the call skips `_render_table`. Switch both to `styled_table`. (Several
`cost_parts/` panels do the same — `spend.py:246`, `optimize.py:580`, `contract.py:323,564`,
`ai_chargeback.py:172` — and `admin.py:494` even leaves a comment that the house convention is
`styled_table`, not a raw call. Sweep them together.)

**8. Centralize the repeated `column_config` dicts (P2/M).** `DELTA_USD → "$%+.0f"`,
`ESTIMATED_USD → "$%.0f"`, `PRECISION_PCT → "%.1f%%"` are re-declared inline at
`overview.py:531,641`, `control_room.py:602-605`, `operations.py:791-794`, `alerts.py:713`.
Rec 13 (partial) asked for this. Add `app/ui/columns.py` with named specs (`USD_DELTA`,
`PCT`, `EST_USD`, `PROFILE_LINK`) so a formatting change lands once and every table agrees.

**9. Give `section_header` an icon at every call, or none (P2/S).** `section_header` accepts
`icon_name` (`components.py:325`) but almost no caller passes it — Operations passes
`"warehouse"` (`operations.py:663,1054`), everyone else omits it, so iconography is random.
Either assign a section→icon map (like `icons._PAGE_ICON`) and pass it everywhere, or drop the
param. Consistency beats sporadic decoration.

**10. Make `panel_help` placement consistent (P2/S).** `panel_help()` (the "ⓘ about this
panel" popover, `components.py:85`) sits *above* the data on some panels
(`operations.py:422,440,454`) and is absent on peers that are just as dense (the DDL stacked
chart, the egress table). Adopt a rule — every chart/table panel gets exactly one help affordance
in the header row — and lint for it, so "what is this / when red do X" is always one predictable
click away.

---

## Group B — Navigation & information architecture

**11. Show the current location as a breadcrumb in the top bar (P1/S).** Rec 5 (prior review,
still open). The sidebar brand block repeats "OVERWATCH" (`main.py:79`) and every page header
carries its own title+icon, but there is no persistent "Analyze › Cost & Contract › Attribution"
line. Put a compact breadcrumb (group › page › section) in the thin top-bar header row
(`main.py:552`) — it orients better than the repeated brand and reads the `_ow_page` +
`PAGE_SECTION_KEYS` the router already tracks.

**12. Persist the section selection per page across visits (P2/S).** `lazy_sections` keys off a
single `key` in session_state (`components.py:100`) so leaving Operations→Alerts→Operations
resets to the first section. Namespacing the remembered choice by page (it partly is) and
restoring it makes multi-page triage far less clicky. Verify the deep-link `?section=` path
still wins.

**13. Add a "back to top / jump to section" affordance on long pages (P2/M).** Operations
(7 sections, some 600+px) and Security History are long scrolls. A small floating section
menu (or an in-page anchor row under the header) lets a DBA jump from Queries to Emergency
without hunting. The section list already exists in each `render()` — reuse it.

**14. Surface the active filter scope as removable chips, not just a glow (P2/S).** v4.65
retired the scope-chip band for a border glow (`main.py:144`, `_scope_is_active`). The glow says
*that* a filter is active but not *which*; `_scope_chip_html()` still builds the chips
(`components.py:20`) and is only used in the page header. Render those chips in the top bar with
an "×" to clear each dimension — one click to drop a stray `warehouse~` filter beats reopening
"More filters."

**15. Elevate global search from a `selectbox` to a command palette (P2/M).** "Jump to"
(`main.py:333`) is a solid one-box router over pages/DBs/warehouses/rules but it is buried under
the sidebar nav and only routes to *destinations*. Promote it to a top-bar search (or `/`
shortcut) and extend it to jump to a rule, an incident, or a saved view — the plumbing
(`request_navigation`) already exists.

**16. Move "Refresh data" out of the nav stack into the top bar (P2/S).** The refresh button
sits below the jump box and health strip in the sidebar (`main.py:142`), visually mixed into
navigation. It is a global data action, not a page — pair it with the breadcrumb/last-refreshed
note in the top bar so "how old is this" and "refresh it" live together.

**17. Let the sidebar collapse to icons on narrow viewports (P3/M).** The grouped radio nav
(`main.py:123-128`) is good but the sidebar has no compact mode; on a laptop the brand block +
version + caption + jump + health strip + refresh eat vertical space before the nav. A
`page_icon`-only rail (icons already exist, `icons.py:38`) under `max-width` would recover it.

**18. Order sections by triage priority, not authoring order (P2/S).** Alerts opens on "Open
events" (good), but Security opens with the governance score panel *above* the section radio
(`security.py:574`) so the section nav is pushed down; Operations leads with Queries when a DBA's
morning question is usually Tasks/Warehouses. Reconfirm each page's default section against "what
does the operator look at first," which the Control Room already nails.

---

## Group C — Data presentation & tables

**19. Right-align and monospace all numeric columns (P1/S).** `_render_table` applies
`font-variant-numeric:tabular-nums` globally (`theme.py:53`) but Streamlit's dataframe left-aligns
numbers by default; a column of `$1,234 / $984 / $12,004` is much harder to compare left-aligned.
Set `st.column_config.NumberColumn` alignment (or CSS `text-align:right` on numeric cells) in
`_auto_formats` so every money/count column reads as a clean right-aligned stack.

**20. Cap and standardize table heights so pages don't jump (P2/S).** Heights are passed
ad hoc: `140`, `170`, `180`, `190`, `200`, `220`, `240`, `260`, `280`, `300`, `320`, `380`
across the pages, and `_render_table` defaults to `380` when `len>10` (`components.py:948`).
Define `TABLE_H = {"compact":200,"default":320,"tall":480}` tokens and use them; a stable
rhythm reads calmer and scrolls predictably.

**21. Make the CSV/export affordance discoverable (P2/S).** The per-table download is a single
"⬇" tertiary glyph (`components.py:989,1009`) that most users won't notice, and it only appears
for `len(df) >= 4`. Give it a label ("Export CSV") on hover-reveal or a consistent corner slot in
the panel header, and expose it on the KPI-ish small frames too where it's currently suppressed.

**22. Add column tooltips from a shared glossary (P2/M).** Headers like `P95_ELAPSED_SEC`,
`QUEUED_SEC`, `SPILL_REMOTE_GB`, `CREDITS_BILLED_OTHER` are prettified for display
(`components.py:851`) but carry no definition. A `{column: help}` glossary threaded into
`st.column_config.*(help=...)` would put "p95 = 95th-percentile elapsed seconds" one hover away,
reusing the `metric_registry` prose where it exists.

**23. Freeze the identity column and cap wide tables to horizontal-scroll gracefully (P2/S).**
`_render_table` already pins the first column when `>=8` cols (`components.py:931`) — good — but
the pin silently drops on older Streamlit (`except TypeError`) and wide QUERY_HISTORY tables
(`operations.py:148`) still overflow. Confirm the pin is landing in SiS's Streamlit build and add
a "scroll for more →" caption on tables wider than the viewport.

**24. Truncate long free-text cells with a hover-to-expand, not a hard clip (P2/S).**
`QUERY_PREVIEW`, `ERROR_MESSAGE`, `DETAIL`, `VERDICT_DETAIL`, `SAMPLE_TARGET` are shown raw in
tables and get visually clipped by column width. A consistent `TextColumn(width="medium")` +
tooltip (or a drawer, which several pages already do) keeps rows scannable without losing the
text.

**25. Sort every table by its most-actionable column by default (P1/S).** `severity_sort`
exists and is used on the alert feed (`components.py:713`, `alerts.py:331`) but most tables land
in query order. Failures-by-error, task tables, movers, and change-impact should default-sort by
failures/Δ$/regression so the worst row is at the top without the user clicking a header —
`severity_sort`'s pattern generalizes.

**26. Replace boolean `True/False` cells with glyphs (P2/S).** Booleans render as the strings
`True`/`False` and get colored via `STATUS_COLOR_MAP["TRUE"/"FALSE"]` (`status_colors.py:57`),
which is a wash of amber/slate. `st.column_config.CheckboxColumn` or a check/dash glyph reads instantly
for `SLA_MET`, `ENABLED`, `GOT_WORSE`, `STALE`, `IS_ANOMALY`.

---

## Group D — Charts & visualization

**27. Give every chart a one-line conclusion caption (P1/M).** Rec 15 (partial). Only
`spend_trend` and the Overview `bar_usd` driver chart lead with a takeaway. `daily_stacked_count`,
`daily_stacked_usd`, `hour_heatmap`, `bar_count`, `events_by_day`, `event_timeline`,
`daily_metric_line`, `paired_bars`, `waterfall_usd`, and `sparkline_row` render bare (`charts.py`).
Add an optional `takeaway=` param and pass the computed headline ("Failures concentrate in
PIPELINE, 62% of the total") — the caller already has the numbers.

**28. Standardize chart heights on tokens (P2/S).** Heights are `_HEIGHT=264` but then
overridden ad hoc: `186` (`event_timeline`), `220`/`260`/`280` (metric line, waterfall, monthly),
`56` (sparkline_row), `max(120, 24*rows)` (heatmap). Define `CHART_H = {"spark":64,"panel":264,
"tall":320}` and use them so charts on one page align to a grid.

**29. Add value labels to bars where the axis alone under-reads (P2/S).** `bar_usd` labels its
bars (`charts.py:162`) but `bar_count`, `daily_count_bars`, and the stacked charts rely on the
axis. For the small top-N bars (failures by family, statements by user) a direct data label is
faster to read than tracing to the axis — reuse `bar_usd`'s `mark_text` pattern.

**30. Fix the task-DAG colors to the token palette (P1/S).** `graphviz_chart` hardcodes
`#fecaca / #e2e8f0 / #bbf7d0` (`operations.py:546`) — a red/gray/green a shade off from
`SEV_COLORS` and the `--ow-*` tokens (the exact A1 drift the last review flagged for other
surfaces). Pull these from the shared palette so "failed/suspended/healthy" matches every other
red/green in the app.

**31. Carry the colorblind-safe redundant encoding into more charts (P2/M).** `event_timeline`
correctly adds a shape channel alongside severity color (`charts.py:366`, A2). `events_by_day`,
`daily_stacked_*`, and the DAG still separate categories on hue alone. Where a chart encodes
severity, add the shape/pattern; where it encodes entity, the crc32-stable palette
(`_stable_color`) already helps — extend it to `events_by_day`.

**32. Make chart empty-states explicit, not silent (P2/S).** Several chart builders `return`
on empty data with no message (`spend_trend` `if data.empty: return` at `charts.py:97`,
`sparkline_row` prints a bare "–"). A caller that forgot the `guard()` gate then shows a blank
gap. Have chart builders render a small "no data for this window" placeholder so a missing chart
never reads as a layout bug.

**33. Add axis-free micro-sparklines to more KPI cards (P2/S).** `metric_card_html` supports an
inline `spark` (`components.py:231`) and Overview/Operations use it, but most KPI rows
(Alerts tiles, Security governance, Cost pages) pass none, so "a number without direction is half
a number" (the module's own words) applies to most cards. Where a 14-day series is already loaded
for the page, thread its tail into `spark`.

**34. Unify legend orientation and the dollar-axis spelling (P2/S).** A5 (still open). Legends
flip between `orient="top"` (`bar` charts, `paired_bars`) and `orient="bottom"` (stacked charts,
`events_by_day`); the dollar axis is spelled `"$,.0f"` in three places. The Altair theme
(`charts.py:28`) sets `legend.orient:"top"` — let it win everywhere and drop the per-chart
overrides, and add a `_usd_axis()` helper.

**35. Bound the heatmap and monthly-stack legends before they clip (P2/S).** `hour_heatmap`
caps to 20 rows (good, `charts.py:14`) and `monthly_stacked_usd` caps to top-5 + Other (good),
but `daily_stacked_count`/`daily_stacked_usd` are uncapped — a DDL day with 12 change kinds or a
20-region egress chart produces an unreadable legend. Apply the same top-N + "Other" rollup
`monthly_stacked_usd` uses (`charts.py:439`).

---

## Group E — Color, theming & accessibility

**36. Promote one severity/health palette to tokens and have everything read it (P1/M).** A1
(still the highest-leverage miss). `SEV_COLORS` in `charts.py:24`, `_STRIP_COLORS` in
`main.py:389`, `_SEV_HEX` in `components.py:157`, the `_BAD/_HIGH/...` pairs in
`status_colors.py:13`, and the `--ow-*` CSS tokens in `theme.py:29` are four+ near-duplicate
definitions of the same traffic light. Define them once (ideally generate the CSS vars from the
Python palette) and import — a DBA crossing sidebar→Alerts→charts should never see two greens.

**37. Raise the small-label floor to ≥12px (P2/S).** After v4.96, card titles/metric labels are
`0.76rem` (good) but `.ow-stat__k` is `0.72rem` (`theme.py:125`), `.ow-kicker` `0.68rem`
(`theme.py:147`), `.ow-src-badge` `11px` (`theme.py:76`), and chart labels are `11px`
(`charts.py:38`). On a dark theme these small muted labels are the readability floor — lift the
status-bar key and kicker to ~0.75rem and the badges/chart labels to 12px.

**38. Verify `--ow-ink-mute` clears AA on `--ow-raised`, not just `--ow-bg` (P2/S).** The token
was lifted to `#8593a8` with the contrast ratios documented against bg/surface/raised
(`theme.py:24`) — good. But it's used inside `.ow-card` gradients and chart labels on
transparent backgrounds where the effective background varies. Add a contrast lock-test
(the palette is now centralized per #36) so a future token tweak can't silently drop a label
under 4.5:1.

**39. Make genuinely-good empty states green (P2/S).** A4 (still open). `guard()` renders
neutral `st.info` on every empty result (`components.py:698`), so the flagship "no open
criticals" all-clear reads as a notice to stop and read rather than reassurance. Brief and
Control Room already special-case success (`brief.py:192`, `control_room.py:478`). Add an
`all_clear=True` flag to `guard()` that renders `st.success` for the empties that are actually
good news.

**40. Respect and expose a light-theme path end to end (P2/M).** `status_colors.py` has a full
`_LIGHT_EQUIV` map and detection (`status_colors.py:23-42`) but `theme.py` hardcodes a dark
palette in `:root` with no light branch, and charts hardcode dark grid/label colors
(`charts.py:16-20`). Either commit to dark-only and delete the half-built light path, or finish
it — the current split means a viewer on light mode gets dark chrome with light table cells.

**41. Add visible focus states and skip-to-content for keyboard users (P2/M).** The `.ow-help`
badge has a nice focus ring (`theme.py:99`) but the custom HTML KPI cards, section headers, and
the status bar are `st.markdown` divs with no focus affordance, and there's no skip link past the
sidebar. For an internal tool this is lower stakes, but the `?`-tooltip work already set the bar —
extend focus-visible rings to the interactive custom components.

**42. Honor `prefers-reduced-motion` on the brand pulse and hover transitions (P2/S).** The
reduced-motion media query exists (`theme.py:218`) and disables transitions/animations — verify
it also kills the `ow-pulse` brand-dot animation (`theme.py:151`) and the card hover
`box-shadow` transitions, which are the two most motion-heavy elements. It should via the `*`
selector; add a test asserting the rule covers `::before`.

**43. Tighten the dark-mode chart contrast on the partial/provisional dimming (P2/S).** Both
`spend_trend` and `monthly_stacked_usd` dim the in-flight period to `opacity:0.45`
(`charts.py:113,458`) to signal "partial, not a drop." On the dark background 0.45 opacity on a
small final bar can vanish. Add a subtle hatch or a dashed outline in addition to the opacity so
the provisional bar is clearly *present but partial*, per the honesty contract.

---

## Group F — Feedback, loading & empty states

**44. Show skeleton/placeholder blocks while queries run (P1/M).** Pages fire many
`run()`/`run_batch()` calls and Streamlit shows the default top-right "Running"; a cold Overview
or Operations paints its structure only after the data returns, so the layout pops in. Reserve
space with `st.empty()` skeletons (grey KPI-card and table placeholders) sized to the final
layout, filled on arrival — the perceived-latency win is large on the mart-cold paths this app
explicitly optimizes for.

**45. Standardize operator-action feedback beyond the toast (P2/S).** `notify()` fires a toast +
inline `st.success/st.error` (`components.py:1088`), but the inline message lands wherever the
button was, which on long pages is off-screen after the rerun. Pair the toast with a pinned
status line in the panel and, for destructive actions, an explicit "N rows affected" from the
execute result so the operator sees the outcome without scrolling.

**46. Make truncation and "capped" states louder and consistent (P2/S).** Truncation shows a
`st.warning` (`components.py:702`), the heatmap shows a caption (`charts.py:324`), the
alert-tile total shows help text (`alerts.py:676`), and `bar_usd`/`waterfall`'s silent `top_n`
cut shows nothing. Unify on one "showing X of Y — narrow to see the rest" treatment so a reader
never mistakes a capped view for the whole.

**47. Distinguish "not installed" from "error" from "empty" everywhere (P1/S).** `guard()` already
special-cases the migration-absent message (`components.py:689`) — a good pattern. But many pages
re-implement this inconsistently (`operations.py:365` info vs `alerts.py:706` info vs raw
`st.error`). Route all three states through `guard()`/a shared `absent_hint()` so a fresh
deployment shows calm "install this" lines, a real failure shows red, and a true empty shows the
all-clear — never one masquerading as another.

**48. Add a per-page "data freshness at a glance" strip (P2/S).** `result_caption` prints
source+fetched-time per panel (`components.py:358`) and the sidebar/status bar show the stalest
source globally, but there's no per-page "these 6 panels are mart-fresh, 1 is on live fallback"
summary. A compact freshness ribbon under the page header (reusing the freshness board data
Control Room already loads) tells the reader how much to trust the page before they read it.

**49. Replace ad-hoc `st.spinner` / toggle-cost captions with a consistent "this is a heavy scan"
badge (P2/S).** Heavy on-demand scans gate behind toggles with a cost hint
(`toggle_cost_hint`, `components.py:663`; used at `security.py:150`) — an excellent pattern that
only a few toggles use. The day-replay toggle (`control_room.py:616`), the DAG
(`operations.py:533`), streams (`operations.py:459`), and dormant-scan all vary their wording.
Standardize a "⚡ live scan · ~Ns last run" badge on every heavy toggle.

**50. Give long generate-then-run SQL blocks a copy button and syntax affordance (P2/S).** Every
operator flow shows `st.code(sql, language="sql")` (Streamlit's block has a built-in copy button —
good) but the giant `_EMERGENCY_CATALOG` markdown table (`operations.py:845`) and the pasted
`native_alert_templates.sql`/`webhook_delivery.sql` file dumps (`alerts.py:903`) are wall-of-text.
Render the emergency catalog as a compact two-column reference (lever → when) and put the template
files behind expanders with a download button, so the reference is scannable and the copy target
is obvious.

---

## §9 — Explicitly NOT recommending (settled decisions)

To avoid relitigating the owner/reviewer calls already on record:

- **Flattening the severity-stripe palette** (rec 17, DECLINED 2026-07-31) — the token-driven
  stripes are scan signal, not decoration. All color recs here *unify* the palette, never remove it.
- **Section nav → dropdown** (rec 8, DECLINED) — a dropdown hides options; #6 keeps the visible
  wrap-flow pills and only adds counts.
- **Alert drawer → `st.dialog` modal** (rec 9, DECLINED) — the drawer is a dense fragment
  workflow; a modal would cram it.
- **Collapsing the 3 card trust-chips into one prose line** (rec 12, DECLINED) — the
  method/scope/freshness split is intentional (`components.py:243-261`).
- **Headless responsive snapshot tests in CI** (rec 19, DECLINED) — over-engineers a desktop-first
  SiS tool; #17/#37 are the pragmatic responsive/typography subset instead.

## Recommended sequencing — one "consistency wave"

**DO-FIRST (P1, mostly S/M, app-only, no migration):**
1. **#36** unify the traffic-light palette to tokens — unblocks #30, #38, #31.
2. **#7** route the two raw movers/anomaly tables through `styled_table` (A3 delta color lands free).
3. **#1 + #4** collapse the heading mechanisms into `section_header`/`subsection_header`.
4. **#6** live counts on the section pills.
5. **#27** chart takeaway captions.
6. **#25 + #19** default-sort by the actionable column + right-align numerics.
7. **#5** the shared `operator_action()` component (removes ~150 lines, one look for every write).
8. **#44** skeleton loaders on the mart-cold pages.

**NEXT (P2):** #2 panel containers, #3 finding-row helper, #8 shared column specs, #11 breadcrumb,
#39 green all-clears, #47 absent/error/empty unification, #48 freshness ribbon, #37 label floor.

**POLISH (P2/P3):** the rest — each is self-contained and app-only.

Every DO-FIRST item is app-only, needs no migration, and is a net simplification (routing call
sites through helpers that already exist), so a single wave lands the scannability gains without
disturbing the deliberate recent design work.
