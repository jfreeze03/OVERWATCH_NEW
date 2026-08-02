# OVERWATCH Streamlit design — 50 impactful recommendations (2026-08-02, v4.122)

A fresh design pass over the **presentation layer** (`app/main.py`, `app/theme.py`,
`app/ui/**`, `.streamlit/config.toml`), grounded in the current code and cross-checked
against the two prior visual passes (`DESIGN_REVIEW_2026-07-31.md`,
`RECS_REVIEW_2026-07-31.md`) so nothing already shipped is re-listed as new. Where a
recommendation *finishes* a prior partial, it says so.

The design system is genuinely strong: a real token layer, severity-semantic cards,
an SVG icon set, honest empty/error states, tabular figures, a11y contrast lifts, and
grouped nav all already exist. So these 50 are mostly **hygiene, consistency, and
scannability at the edges** — the token system is centralized but not enforced, and the
same widget class is still hand-rolled several ways across pages.

## Verdict & themes

Five cross-cutting themes drive most of the list:

1. **Tokens exist but aren't enforced at the edges.** Color hexes are re-declared in
   ≥4 files; `.streamlit/config.toml` uses *different* hexes than the CSS tokens; inline
   styles bypass the `--ow-*` spacing scale. The fix is drift-enforcement, not redesign.
2. **Emoji vs the SVG icon set.** `icons.py` exists *because* "emoji render
   inconsistently," yet the CSV button (`⬇`), `notify()` (`✅/⚠️`), the Brief (`⚠`), and
   the page icon (`🛰️`) still use emoji.
3. **Heading & "finding" vocabulary is still multiplied.** `page_header` / `section_header`
   coexist with ad-hoc `st.markdown("**Fires**")` pseudo-headings; actions render 3+ ways.
4. **Per-rerun chrome cost.** The full ~230-line CSS blob is re-injected on every rerun
   (incl. the 30s auto-refresh); there are no cold-paint skeletons, so the KPI row/charts
   pop in with layout shift.
5. **Provenance & motion carry noise, not always signal.** Four provenance vocabularies;
   an always-animating brand pulse; long wall-of-text captions.

Legend: **Sev** = user impact (high/med/low), **Effort** = S/M/L. All items are
**app-only, no migration** unless noted.

---

## I. Information architecture & navigation

**1. Give each page a one-line "job," and de-overlap Brief / Overview / Control Room.**
(med / S) All three surface open alerts + actions + spend, so their distinct purpose is
implicit. Brief = phone glance, Overview = executive analysis, Control Room = triage & act.
Put a single purpose sentence in each `page_header` subtitle and make the overlap
deliberate (Brief links *into* the others, which it already does) — see `brief.py:39`,
`overview.py:197`, `control_room.py`.

**2. Encode active scope in the URL query params for shareable deep links.** (med / M)
Today only `?page=` and `?section=` round-trip (`components.lazy_sections`, `main._sidebar`);
company/days/database/contains live only in session + saved views. A DBA can't paste "the
thing I'm looking at" to a colleague. Serialize the filter dict into query params
(reuse `_current_view_payload` in `main.py:154`) so a link reproduces the scope.

**3. Make "Jump to" a real command palette.** (low / M) `_global_jump` (`main.py:333`)
builds one flat `st.selectbox` of `Page · / DB · / WH · / Rule ·` that can grow to
hundreds of rows in the sidebar's scroll. Add a keyboard shortcut, category separators,
and keep it above the fold; the "load all warehouses & alert rules…" sentinel row is a
clever lazy-load but reads as a stray option — label it as an action.

**4. Move to `st.navigation`/`st.Page` grouped nav now that `NAV_GROUPS` exists.** (med / M)
`_sidebar` renders one `st.radio` *per group* with a caption (`main.py:123`), so a keyboard
user tabs through N independent radio groups and the "one source of truth" (`_ow_page`) is
reconciled by hand across reruns (the long desync comment at `main.py:99-137` is the
smell). Native grouped pages give real section headers, single focus order, and URL routing
for free — collapsing a lot of bespoke state machinery.

**5. Add a breadcrumb in the top bar.** (low / S) Prior rec 5 is still open. `page_header`
dropped the per-page kicker (good), but there's now no persistent "Analyze › Cost & Contract ›
Spend" orientation once you scroll past the header. A thin breadcrumb in `_topbar_scope`
(`main.py:539`) reusing `nav_groups_for` + the active section restores it cheaply.

**6. Put counts on the section pills.** (low / S) `lazy_sections` (`components.py:92`)
renders label-only pills; "Open events", "Rules", "Native delivery" give no sense of what's
inside. Where a cheap count is already loaded (open events, open actions), append it
(`Open events (3)`) so the nav previews the work.

## II. Global shell & chrome

**7. Inject the base CSS once per session, not every rerun.** (med / S) `inject_theme()`
(`theme.py:238`, called from `main()` every run) re-emits `_TOKENS + _CSS` (~230 lines)
via `st.markdown` on *every* rerun, including the 30s auto-refresh. Gate the static
token+base blob behind a `st.session_state` flag; only the compact-density toggle needs to
switch per-run. Cuts DOM churn and payload on the hottest path.

**8. Reconcile `.streamlit/config.toml` with the CSS token palette.** (med / S) Native
widgets (inputs, sidebar base, menu) are painted from `config.toml`
(`backgroundColor=#0b1220`, `textColor=#e2e8f0`, `secondaryBackgroundColor=#111a2c`), but
custom cards use the tokens (`--ow-bg:#0a0f1c`, `--ow-ink:#e8eef7`, `--ow-surface:#0f1729`).
The hexes are *close but not equal*, so there are faint seams between native chrome and
custom surfaces. Align the two so the app reads as one surface.

**9. Centralize every color hex in one Python palette module.** (high / S) The same
values are re-declared in `charts.py` (`_ACCENT`, `_LABEL`, `SEV_COLORS`),
`status_colors.py` (`_BAD/_HIGH/…`), `main.py` (`_STRIP_COLORS`), `components.py`
(`_SEV_HEX`), and the theme tokens. Altair can't read CSS vars, so *some* Python literals
are unavoidable — but they should all come from ONE `app/ui/palette.py`, with a test
asserting they match the `--ow-*` tokens. This is the enforcement half of last pass's A1
(which only unified the sidebar strip).

**10. Route emoji through the SVG icon set.** (med / S) `icons.py` exists precisely because
"emoji render inconsistently across platforms," yet: the CSV download button uses `⬇`
(`components.py` `_render_table`), `notify()` uses `✅/⚠️`, `brief.py:180` uses `⚠`, and
`st.set_page_config(page_icon="🛰️")`. Replace the in-body glyphs with `icon(...)`; keep the
browser-tab emoji only if a favicon SVG isn't practical.

**11. Make the brand pulse carry signal.** (low / S) `.ow-brand-dot` animates
`ow-pulse` infinitely (`theme.py:151`) as pure decoration that competes with real alerts.
Pulse it *only* when `health_vals` shows an open critical; otherwise render it static. Motion
should mean something.

**12. Kill or use the dead brand CSS.** (low / S) `.ow-brand-word` (a gradient wordmark,
`theme.py:153`) is defined but never rendered — the sidebar shows plain
`**Snowflake Command Center**` markdown (`main.py:83`). Either build the branded lockup as
one component or drop the dead rule.

**13. Pin a compact sidebar footer for Refresh + last-updated.** (low / S) The sidebar packs
brand → nav (N radios) → Jump-to → health strip → Refresh → a two-line caption
(`main.py:77-151`); on short viewports Refresh and the telemetry note fall below the fold.
Pin "Updated Nm ago · ⟳ Refresh" as a sticky footer so the freshest control is always reachable.

## III. Design-token & CSS hygiene

**14. Stop bypassing the spacing scale with raw px.** (low / M) `--ow-1..6` exist, but inline
styles hardcode `margin:-2px 0 2px 0`, `gap:11px`, `padding-top:1.1rem`
(`components.page_header`, `theme.block-container`, `_strip_line`). Route recurring spacing
through the tokens so density changes are one edit.

**15. Extract the repeated "muted note" inline style into a class.** (low / S) The string
`font-size:0.72rem;color:var(--ow-ink-mute);margin-top:…` recurs (e.g. `main.py:91`,
`_strip_line`). Add one `.ow-note` class and reuse it.

**16. Don't stripe neutral KPI cards.** (med / S) `div[data-testid="stMetric"]::before`
paints an accent gradient on *every* metric (`theme.py:59`), and `.ow-card::before` defaults
to a muted stripe — so a colored *severity* stripe no longer pops because there's always a
stripe. Give neutral cards no (or a hairline) stripe so red/amber actually draw the eye.

**17. Scope the stale-element blanking rule to the page body.** (med / S)
`[data-stale="true"]{opacity:0 !important}` is global (`theme.py:215`); on some SiS builds
this can briefly blank legitimately-updating chrome during a rerun. Scope it to the main
block container so structural chrome never flashes.

**18. Centralize the `md_dollars` escape at the sink.** (med / S) `md_dollars` is called at
~30 sites (`overview`, `brief`, `components`, `ai_panel`, …) because a bare `$` in
`st.caption`/`st.markdown` becomes a LaTeX math span. Add `data_caption()` / `data_markdown()`
wrappers that always escape, so no author has to remember. Fewer silent garbled captions.

**19. Extract guarded HTML builders.** (low / M) `metric_card_html`, `status_bar`,
`_strip_line`, `page_header` all hand-concatenate `unsafe_allow_html` strings. Factor a
tiny builder module that enforces `html.escape` on every interpolated value (most already
do, but by hand) — less duplication and a smaller injection surface.

**20. Standardize dividers.** (low / S) `st.divider()` (sidebar), a custom
`<hr style="opacity:0.25">` (`lazy_sections`), and the `.ow-section` gradient bar are three
separators for the same job. Pick one structural-divider rule and use `section_header` for
content breaks.

## IV. Typography & density

**21. Raise the micro-label floor in comfortable mode.** (med / S) `.ow-stat__k` 0.72rem and
`.ow-card__title`/`stMetricLabel` 0.76rem are still small for a wall-mounted command center.
Bump comfortable-mode labels to ~0.8rem; keep today's sizes in compact mode. Finishes prior
rec 18 (which fixed contrast but not size).

**22. Cap caption length; push the rest into the panel popover.** (med / S) Several captions
are 6-8 sentences — the Overview score-trend expander caption (`overview.py:724`), the
spend-trend caption (`charts.py:146`), some KPI `help`. `panel_help` already exists but is
under-used. Lead with one line; move the nuance into "ⓘ about this panel."

**23. Enforce one heading hierarchy; ban pseudo-headings.** (med / M) Prior rec 6 is partial.
`brief.py` uses `st.markdown("**Fires**")` / `**Asks**` while peers use `section_header`;
counts across `app/ui` show `st.title/header/subheader/**bold**` all in play. Make
`page_header` (h1) → `section_header` (h2) → a new `subsection()` the only sanctioned
headings and convert the stragglers.

**24. Right-align numeric KPI values.** (low / S) Tables right-align numbers (tabular-nums),
but `metric_card_html` value + delta are left-aligned (`components.py:262`). In a 4-across
row of dollar KPIs, right-aligned values scan like a ledger; left-aligned they jitter.

## V. Color & severity

**25. Extend redundant severity encoding to stripes and chips.** (med / M) Prior A2 added a
shape axis to the event-timeline dots, but severity is still hue-only on KPI stripes
(`.ow-sev-*`), sidebar strip dots (`_strip_line`), and status chips. Add a glyph or the
severity word so protan/deutan users don't depend on red-vs-amber.

**26. Lighten status-cell fills to a border/dot.** (med / M) `status_colors` paints deep
saturated backgrounds per cell (`_BAD=#7f1d1d`, etc.); a dense table becomes a wall of color
blocks. The delta columns already show the restrained pattern — text-color only (`delta_css`).
Mirror that for status columns (colored dot or left border) so tables stay scannable.

**27. Give INFO its own color, distinct from LOW.** (low / S) `STATUS_COLOR_MAP` maps both
`INFO` and `LOW` to `_MUTED` (`status_colors.py:46`), collapsing "informational" and
"low severity" into one slate. INFO is sky elsewhere (chips, sidebar) — make the table cell
match so the semantics don't fork.

**28. Green "all-clear" empty states.** (med / S) Prior A4 is still open: `guard()` renders
neutral blue `st.info` on empty (`components.py:697`), so "no open criticals" reads as a
notice, not relief. Add a `success_empty()` and use it for genuinely-good empties; Brief
already does this ad-hoc with `st.success` — make it the shared path.

**29. Relate the chart categorical palette to the app palette.** (low / M) `_STABLE_PALETTE`
(`charts.py:195`) is a Tableau-ish set (`#4c78a8`, `#f58518`, …) with no relationship to the
OVERWATCH accent/severity family, so a warehouse series looks foreign to the UI. Derive it
from the token accent family (or document the intentional split) and keep the crc32 stability.

## VI. KPI cards & components

**30. Auto-fit long KPI values.** (med / S) `metric_card_html` fixes `min-height:96px` and
`font-size:1.55rem` (`components.py:262`); a value like `$1,234,567 / $2,000,000` (the
budget KPI) wraps or overflows. Add a value clamp (shrink font when long) or ellipsis +
title so cards stay uniform.

**31. Add a threshold/target bar to reference-framed KPIs.** (med / M) Score/100, MTD/budget,
and contract runway are numbers begging for a reference bar. A thin progress track under the
value (green→amber→red at thresholds) turns "74" and "$41k/$60k" into an instant read. One
optional `progress` key on the card dict.

**32. Define wrap/priority for the card title row.** (low / S) The `?` help affordance plus up
to three trust chips (freshness/method/scope) can crowd a narrow card's title
(`components.py:261`). Test at 4-across on a laptop and set an explicit wrap/hide order so the
label never gets squeezed.

**33. Unify the "vital signs" component.** (med / M) The persistent status bar (flex stat
cards, `status_bar`) and the sidebar health strip (`_strip_line` dots) present the SAME
numbers (open criticals, telemetry age, MTD) in two visual languages. Build one vitals
component and render it in both places.

**34. Put the latest value + arrow next to sparklines.** (low / S) `spark_svg`/`sparkline_row`
draw a trend with no axis and no last-value label, so the magnitude is hover-only. Append the
latest value and a direction arrow (the `_delta_html` arrow set already exists) so the spark
reads at a glance.

## VII. Tables & exports

**35. Fold units into prettified headers.** (med / S) `_prettify_header` Title-cases
`UPPER_SNAKE` (good) but drops the unit, and the large-frame path loses thousands commas
(`_PRINTF_EQUIV`). Put the unit in the header (`Spend ($)`, `Size (GB)`, `Hit (%)`) for the
convention-formatted columns so a bare number is never ambiguous.

**36. Standardize a labeled Export control.** (low / S) The CSV control is a tiny tertiary
`⬇` that differs by frame size (inline vs 2-step prepare) and is easy to miss
(`components._render_table`). Make it a consistently-placed, SVG-iconed "Export CSV" affordance
at the table's top-right.

**37. Show a row-count/scope line above every table.** (low / S) Truncation is surfaced only
when the *server* capped rows (`guard()`), and the height cap hides the total. Add
"N rows • account-wide • last 7d" above tables so the reader knows the extent before scrolling.

**38. One shared `column_config` builder keyed on the metric registry.** (med / M)
`st.column_config.NumberColumn(format=…)` is hand-written per call site (`overview.py`,
`spend.py`, movers table, …). A `columns_for(metric_keys)` helper reading
`logic/metric_registry.py` would give consistent units/labels and is the concrete, shippable
subset of "executable registry" (prior rec 16) that pays off immediately.

**39. Make click-to-navigate rows discoverable everywhere.** (low / S) `selectable_table`
drives navigation on Overview (with a "click a row" caption) but silently elsewhere. Add a
consistent hover-row highlight + one caption wherever a table is selectable, so the
interaction isn't hidden.

## VIII. Charts

**40. Every chart leads with its conclusion.** (med / M) Prior rec 15 is partial — only
`spend_trend` and the cost-driver bar carry a takeaway. Add a one-line auto takeaway to
`daily_stacked_*`, `hour_heatmap` (peak hour), `waterfall_usd` (dominant contributor),
`events_by_day` (worst day). The data to compute it is already in-frame.

**41. Responsive + density-aware chart heights.** (low / M) Heights are fixed
(`_HEIGHT=264`, timeline 186, metric line 220) and don't shrink on mobile or in compact mode,
so a chart can eat the whole fold on a phone. Scale height by viewport/density.

**42. Migrate off deprecated `use_container_width`.** (med / M) ≥40 call sites
(`st.altair_chart`, `st.dataframe`, `st.button`, `st.download_button`, …) pass
`use_container_width=True`, which Streamlit has deprecated in favor of `width="stretch"`.
In SiS this will start emitting deprecation warnings; migrate centrally (the wrapped
`styled_table`/`kpi_row`/chart helpers make this a small, contained change).

**43. Centralize Altair tooltip/axis conventions.** (low / S) Finishes prior A5: day-grain
tooltips, the `$,.0f` dollar axis, and legend orientation are re-specified per chart with
small variations. A `tooltip_day($|count)` + axis helper in `charts.py` makes hover and axes
identical across charts.

**44. Use a neutral scheme for the hour heatmap.** (low / S) `hour_heatmap` uses
`scheme="orangered"` (`charts.py:317`), so a benign "credits by hour" reads as severity-red.
Switch to a sequential blues/teals scheme from the token family and reserve red for actual
severity heat.

## IX. States, feedback & motion

**45. Add cold-paint skeletons for the KPI row and primary chart.** (med / M) Cached reads use
`show_spinner=False`, so on a cold paint the KPI row and charts appear abruptly and shift
layout. Reserve fixed-height `st.empty` skeleton blocks (a shimmer via CSS) for the KPI row
and the first chart so there's no layout jump — the biggest perceived-quality win here.

**46. Pick one feedback channel for operator actions.** (low / S) `notify()` fires BOTH
`st.toast` and an inline `st.success/error` (`components.py:1088`). That's redundant and
competes. Use toast for transient confirmation and reserve the inline banner for durable state
(e.g. an error that needs reading).

**47. Show progress on Refresh and guard double-clicks.** (low / S) "Refresh data"
(`main.py:142`) bumps the salt and `st.rerun()` with no visible progress; an impatient
double-click stacks reruns. Wrap in `st.status`/spinner and disable while refreshing.

**48. Audit the motion budget.** (low / S) Card hover transform+shadow, the infinite brand
pulse, and stale-opacity transitions accumulate. The reduced-motion rule already blankets
`*` with `animation:none` (`theme.py:218`), so accessibility is covered — but when the pulse
becomes signal-driven (#11) keep it under that rule, and keep all transitions ≤150ms so the
30s auto-rerun never feels busy.

## X. Micro-copy & consistency

**49. One provenance vocabulary.** (med / M) Provenance is spoken four ways: `result_caption`
("Source: X · fetched HH:MM:SS"), card trust chips (mart/live/stale/method/scope), section
scope notes, and the legend popover. Define one provenance component + copy rule so "where did
this number come from" always looks and reads the same.

**50. Write the UI style guide and lint it.** (med / M) Capture the rules this doc leans on —
tokens over raw hex, `page_header/section_header` over pseudo-headings, `icon()` over emoji,
`data_caption` over bare `st.caption`, one palette module — in `docs/design/UI_STYLEGUIDE.md`,
and add a grep-proxy test (mirroring `tests/test_perf_budgets.py`) that fails CI on new emoji
in UI bodies, raw hex in page files, or `st.markdown("**…**")` pseudo-headings. Turns this
review into a standing guardrail instead of a one-time cleanup.

---

## Recommended sequencing — one "consistency wave"

**DO-FIRST (highest impact-per-effort, all app-only S/M):**

1. **#10** route emoji through `icon()` — visible, trivial, fixes a stated house rule.
2. **#9** centralize color hex in `app/ui/palette.py` + a token-parity test — the
   enforcement half of A1; unblocks #29/#44.
3. **#7** inject base CSS once per session — cheap perf/quality win on the hot path.
4. **#8** align `config.toml` with the tokens — removes the native-vs-custom seam.
5. **#16** stop striping neutral cards — restores severity-stripe signal.
6. **#28** green all-clear empties (`success_empty()` in `guard()`).
7. **#18** `data_caption()`/`data_markdown()` — centralize the `md_dollars` escape.
8. **#45** cold-paint skeletons for the KPI row + first chart.

**NEXT:** #23 heading hierarchy + #49 provenance component (shared vocabulary); #42
`width="stretch"` migration; #35/#36/#37 table units/export/row-count; #40 chart takeaways;
#31 threshold bars; #33 unified vitals; #2 scope-in-URL.

**BACKLOG / larger:** #4 native grouped nav; #38 registry-keyed `column_config`;
#50 style guide + lint (do this alongside #23/#10 so the guardrail lands with the cleanup);
#25/#26 severity-encoding + status-cell restraint.

Every DO-FIRST item is self-contained, migration-free, and preserves the deliberate recent
design decisions (token split, grouped nav, honesty states). No `APP_VERSION` bump or
migration is implied by this document — it is a recommendations pass, not a shipped change.
