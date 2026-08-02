# OVERWATCH Streamlit design — 50 impactful recommendations (2026-08-02, v4.122)

Fresh design pass over the app shell (`app/main.py`), the design system
(`app/theme.py`, `app/ui/components.py`, `app/ui/charts.py`, `app/ui/icons.py`,
`app/ui/status_colors.py`), the native theme config (`.streamlit/config.toml`),
and representative pages (`overview.py`, `brief.py`). Every item is verified at
its cited line and cross-checked against the two most recent design rounds
(`DESIGN_REVIEW_2026-07-31.md`, `RECS_REVIEW_2026-07-31.md`) so this list does
not relitigate shipped work — where an item **extends** a landed rec it says so.

This is a **recommendations document only** — no app behavior is changed by this
file. Sequencing at the end. Sev = correctness/UX weight; Effort = S/M/L.

## The one paragraph

The design system is genuinely good: tokenized colors, a card/section/status-bar
vocabulary, an inline SVG icon set, honest empty/error states, tabular figures,
a11y groundwork, and colorblind-redundant severity on the timeline. The
remaining leverage is almost entirely **drift at the edges** (the token layer is
authoritative for CSS but three other surfaces — `config.toml`, the Altair
palette, and dead CSS selectors — hold their own copies of the same colors) and
**a handful of Streamlit-native affordances the custom layer reimplements or
skips** (native theme config, `st.navigation`, `st.fragment` scoping, skeleton
loaders, accessible icon buttons). None require a migration.

## Highest-leverage five (start here)

- **R1** — `.streamlit/config.toml` chrome colors don't match the token layer.
- **R2** — dead CSS selectors (`aria-label="Window"`, theme `heatmap` range) style nothing.
- **R7** — one Python color source feeding BOTH the CSS tokens and the Altair theme.
- **R21** — `st.fragment`-scope the interactive panels so a click doesn't rerun the whole page.
- **R31** — skeleton/progress affordance for cold live scans (pages paint blank for seconds).

---

## Theme A — Color-system drift (the token layer isn't the only source of truth)

The single biggest theme after v4.95–4.101: A1 unified the *severity* palette into
tokens, but the *base chrome* palette and the *chart* palette still live in three
independent places. Reconciling them is small and kills a whole class of
"why is this a slightly different blue" bugs.

**R1 (Sev: high · Effort: S) — Reconcile `.streamlit/config.toml` with the token layer.**
`config.toml` sets `backgroundColor="#0b1220"`, `secondaryBackgroundColor="#111a2c"`,
`textColor="#e2e8f0"` (`.streamlit/config.toml:3-5`) while the token layer sets
`--ow-bg:#0a0f1c`, `--ow-surface:#0f1729`, `--ow-ink:#e8eef7`
(`app/theme.py:22-27`). The native config paints the very first frame and every
widget the CSS doesn't reach (native tables, date pickers, the running-man
spinner overlay), so the app opens on one near-black and settles into another —
a visible flash + permanent mismatch on un-styled widgets. Make `config.toml`
the byte-exact base-chrome values the tokens use. Extends A1 (which stopped at
severity hues and never touched the base chrome).

**R2 (Sev: medium · Effort: S) — Delete dead CSS selectors so the intent isn't silently unfulfilled.**
Two rules match nothing today: (a) `div[role="radiogroup"][aria-label="Window"]`
(`app/theme.py:157,164`) — the day-window control became a `st.select_slider`
("Window (days)", `app/main.py:580`), which is not a radiogroup, so the intended
pill styling never applies; (b) the theme's `"heatmap": ["#0f1729"…"#a5f3fc"]`
range (`app/ui/charts.py:48`) is overridden everywhere by an explicit
`scheme="orangered"` (`app/ui/charts.py:317`), so the token heatmap ramp is
never used. Either wire the control/scheme to the token, or remove the dead code
so the next reader doesn't trust a style that isn't live.

**R3 (Sev: medium · Effort: S) — Unify the two categorical palettes.**
Single-series bars/lines use the Altair theme `range.category`
(`_ACCENT`, green, purple… `app/ui/charts.py:46-47`), but multi-series stacked
charts use `_STABLE_PALETTE`, a Tableau-10 set
(`app/ui/charts.py:195-196`). A user reading the monthly boss chart (Tableau
colors) then a single-series driver bar (accent gradient) sees two color
languages for "category." Pick one categorical ramp (the crc32-stable one is the
better choice — keep its stability contract) and have the theme `range.category`
reference the same list.

**R4 (Sev: low · Effort: S) — Chart label/grid hexes should read the tokens, not copies.**
`charts.py` hardcodes `_LABEL="#8b98ad"`, `_TITLE="#c3cddb"`, `_GRID=…`
(`app/ui/charts.py:17-20`). `_LABEL` is the *pre-a11y* muted value; the token
layer already lifted its equivalent to `--ow-ink-mute:#8593a8` for WCAG AA
(`app/theme.py:24-27`). Axis labels therefore sit a shade below the contrast
floor the rest of the app cleared. Source these from the same Python constants R7
introduces.

**R5 (Sev: low · Effort: S) — Sidebar health-strip hues are a fourth copy.**
`_STRIP_COLORS` in `app/main.py:389-390` re-spells OK/WARN/BAD/INFO as literal
hexes with a comment admitting they were "a shade off from every other surface."
They now match, but as *copies* — one edit to a token and this drifts again.
Read `--ow-ok/--ow-warn/--ow-bad/--ow-info` (or the R7 constants) instead of
re-declaring them.

**R6 (Sev: low · Effort: S) — `_SEV_HEX` in components is a fifth copy.**
`app/ui/components.py:157-158` hardcodes the same severity hues for sparkline
tint. Same fix: one source.

**R7 (Sev: high · Effort: M) — Introduce ONE Python palette module that generates both surfaces.**
R1–R6 are all symptoms of the same root: colors live in CSS-string form in
`theme.py` and can't be imported by Python (charts, the strip, `_SEV_HEX`). Add
`app/ui/palette.py` with the canonical hexes as Python constants; have `theme.py`
f-string them into the `:root` block, have `charts._overwatch_theme()` build its
palette from them, and have `main._STRIP_COLORS`/`components._SEV_HEX` import
them. Add a lock-test asserting the `config.toml` chrome values equal the palette
constants (closes R1 permanently). This is the drift-enforcement the last review
called "the highest-leverage payload" — generalized past severity to the whole
palette.

---

## Theme B — Streamlit-native affordances the custom layer skips

**R8 (Sev: medium · Effort: S) — Add `menu_items` to `set_page_config`.**
`app/main.py:9-14` sets title/icon/layout but no `menu_items`, so the top-right
"⋮" menu still shows Streamlit's generic Report-a-bug/About. Point About at a
one-line OVERWATCH description + version, and Get-help at the RUNBOOK — free
orientation for a viewer who lands cold.

**R9 (Sev: medium · Effort: L) — Evaluate `st.navigation`/`st.Page` for the shell.**
The nav is hand-rolled: per-group `st.radio` widgets with page-scoped keys, a
manual `?page=` deep-link reconcile, and a `_RENDERERS` dispatch dict
(`app/main.py:62-151`). Native `st.navigation` (1.36+) gives grouped sections,
real per-page URLs, and working browser back/forward for free, and would delete
the subtle multi-radio desync plumbing documented at `app/main.py:99-137`.
Tradeoff worth weighing openly (prior round *declined* a nav dropdown because it
hid options — `st.navigation` does not hide, it groups). Prototype behind a flag;
keep the profile-based page filtering.

**R10 (Sev: low · Effort: S) — Inject theme via `st.html`, once, not `st.markdown` every rerun.**
`inject_theme()` re-emits the full token+CSS blob through `st.markdown(...,
unsafe_allow_html=True)` on every rerun (`app/theme.py:238-242`). `st.html`
(1.33+) is the purpose-built, de-duped sink for a style block and signals intent
better than a markdown element. Minor DOM/allocation win, clearer code.

**R11 (Sev: low · Effort: S) — Replace the emoji page icon with the app's own mark.**
The shell removed emoji from nav on purpose (`app/main.py:58-60`) yet the browser
tab is `page_icon="🛰️"` (`app/main.py:11`). Use an inline SVG/data-URI of the
`ow-brand-dot` so the tab matches the in-app brand and the anti-emoji principle
holds end to end.

---

## Theme C — Rerun scope & perceived performance

**R21 (Sev: high · Effort: M) — `st.fragment`-scope independent panels.**
Only `_views_popover` is a fragment (`app/main.py:179`). Every other
interaction — selecting a row in Overview's Top-actions table
(`app/ui/pages/overview.py:528`), toggling a section, opening an expander —
reruns the *entire* page function, re-evaluating every `run()` (cached, so cheap
in queries) but re-executing all Python and re-painting all chrome. Wrap
self-contained sections (each `section_header` block, the alert drawer, the
attribution tab) in `@st.fragment` so a local interaction reruns only that
block. Biggest perceived-latency win available without touching SQL.

**R22 (Sev: medium · Effort: S) — Cache the sparkline SVG-gradient id off content, not `hash()`.**
`spark_svg` builds its `<linearGradient>` id as `abs(hash(line)) % 100000`
(`app/ui/components.py:188`). Two sparklines with identical data on one page get
the *same* DOM id → the second polygon can reference the first's gradient (SVG
ids are document-global); and `hash()` is per-process-salted, so ids also jump
run to run. Use a monotonic per-render counter (or `zlib.crc32` like the palette
already does at `charts.py:202`) to guarantee uniqueness and determinism.

**R23 (Sev: low · Effort: S) — Give KPI cards equal height within a row.**
`metric_card_html` sets `min-height:96px` (`app/ui/components.py:262`) but a card
with a sparkline/delta is taller, so a `kpi_row` mixing spark and non-spark cards
(the Overview flagship row) has ragged bottoms. Add `height:100%` to `.ow-card`
inside `kpi_row` columns (or `align-items:stretch`) so a row reads as one shelf.

---

## Theme D — Accessibility (extends rec15/A2, still-open items)

**R31 (Sev: high · Effort: M) — Skeleton/progress for cold live scans.**
Cached reads use `show_spinner=False` throughout (e.g. `app/main.py:419`,
`components.py:605,737`), so on a cold live ACCOUNT_USAGE scan (seconds) a panel
paints *nothing* — no spinner, no skeleton — then pops in. `toggle_cost_hint`
warns *before* a toggle (`components.py:663`) but the general first-paint has no
progress cue. Add lightweight `st.skeleton`-style placeholders (or a scoped
`st.spinner`) around the heavy sections so "slow" reads as "loading," not
"broken/empty."

**R32 (Sev: medium · Effort: S) — Label the icon-only buttons for screen readers.**
The CSV download renders as a bare "⬇" glyph (`app/ui/components.py:989,1009`)
and `panel_help` as "ⓘ about this panel". Icon-only controls announce as
"button" with no name. Add a text label or `help=`/aria via a wrapping element so
the download/help affordances are reachable non-visually. (a11y groundwork from
rec15 covered KPI help; these buttons were out of that scope.)

**R33 (Sev: medium · Effort: S) — Global `:focus-visible` ring on the accent.**
Only `.ow-help` has a custom focus ring (`app/theme.py:98-99`). Native buttons,
radios, selects keep the browser default outline (often near-invisible on the
dark surface). Add one global `:focus-visible { outline / box-shadow }` in the
accent so keyboard traversal is visible on every control, matching `.ow-help`.

**R34 (Sev: low · Effort: S) — Sparklines and status dots need text alternatives.**
`spark_svg` (`components.py:196`) and the KPI value SVGs carry no `role`/`aria-label`;
a screen-reader user gets the number but not the "trend up/down" the sparkline
encodes. The health-strip dot already does this right (`role="img"
aria-label=…` at `app/main.py:400`). Add an `aria-label` summarizing
direction/last value to `spark_svg`'s `<svg>`.

**R35 (Sev: low · Effort: S) — Add a mid (tablet) responsive breakpoint.**
Only `@media (max-width:640px)` exists (`app/theme.py:206-210`); 641–1024px (a
docked laptop half-screen, an iPad) gets the full desktop layout with 4-across
KPI cards that cramp. Add an intermediate breakpoint dropping to 2-across cards
and a tighter `block-container` pad.

**R36 (Sev: low · Effort: S) — Verify the `.ow-help` tooltip can't clip.**
`.ow-help[data-help]::after` is absolutely positioned `left:0` with a 300px max
(`app/theme.py:100-106`); on a right-edge KPI card the tooltip can overflow the
viewport, and inside any ancestor with `overflow:hidden` (`st.metric` cards set
it at `theme.py:58`) it would clip. Add an edge-aware variant (or flip to
`right:0` on the last column) and confirm the `.ow-card` path never inherits
`overflow:hidden`.

---

## Theme E — Data-display consistency

**R41 (Sev: medium · Effort: S) — Percent columns should carry the `%` glyph.**
`_auto_formats` formats `_PCT` columns as `"{:,.1f}"` with no unit
(`app/ui/components.py:815`), so a `FAIL_PCT` cell reads `12.3`, not `12.3%`,
unless a caller hand-overrides with a `NumberColumn`. Append `%` for the clearly-
percent suffixes (`_PCT`, `HIT_PCT`) while leaving `_SHARE` (0–1 fractions)
alone. One central fix removes a recurring per-site override.

**R42 (Sev: medium · Effort: S) — Give the "all-clear" empty states a green treatment.**
`guard()` renders every empty result as neutral blue `st.info`
(`app/ui/components.py:698`), so the flagship "am I on fire?" all-clear (zero
open alerts) reads as a notice, not good news — while `brief.py` hand-rolls
`st.success("No open critical or high alerts.")` (`brief.py:192`) for the same
condition. Add a `good_empty=True` (or `empty_kind="ok"`) parameter to `guard()`
so genuinely-healthy empties render green and consistently. This is the still-
open A4 from the last round, generalized into the shared gate.

**R43 (Sev: low · Effort: S) — One provenance vocabulary for cards and tables.**
KPI cards show freshness/method/scope as chips (`components.py:255-261`); tables
show the same facts as `result_caption` prose (`components.py:358-373`). A viewer
learns two provenance dialects. Emit a small freshness chip above/beside table
panels (reuse the `.ow-src-badge` tokens) so provenance looks the same wherever
it appears. Pairs with the shipped rec13 token split.

**R44 (Sev: low · Effort: S) — `styled_table` should expose a caption/title slot.**
Tables get a header only when a caller remembers a preceding `section_header` or
`st.caption`; there's no first-class "table title" on `styled_table`
(`components.py:1039`). Add an optional `title=`/`caption=` so every table can
self-identify (it already self-identifies its *export* via `slug` — R-shipped
rec14). Improves scannability of stacked tables.

**R45 (Sev: low · Effort: M) — Audit `section_header` icon names against the icon set.**
`icon()` falls back to a neutral dot for unknown names (`app/ui/icons.py:46`), so
a typo'd `icon_name` degrades silently to a dot with no error. Add a
dev/test-time assertion that every `icon_name`/`page_icon` string passed in the
app exists in `_PATHS`, so a missing glyph is caught in CI, not shipped as a dot.

---

## Theme F — Charts lead with their conclusion (extends rec15)

Only `spend_trend` and the boss chart carry a takeaway caption; ~a dozen charts
render bare. rec15 was PARTIAL last round. These are the remaining high-traffic ones.

**R51 (Sev: medium · Effort: S) — `bar_usd`/`bar_count` takeaway line.**
`bar_usd` (`charts.py:148`) renders the ranked bars but the caller must add the
"top driver is X, Y% of spend" conclusion by hand (Overview does; most callers
don't). Add an optional one-line takeaway (top label + share) emitted under the
chart, so every ranked bar states its own headline.

**R52 (Sev: medium · Effort: S) — `hour_heatmap` should name the hot cell.**
`hour_heatmap` (`charts.py:299`) shows the grid but not "peak burn: WH_X at
14:00." Emit the argmax cell as a caption. A heatmap without a stated peak makes
the reader hunt.

**R53 (Sev: low · Effort: S) — `waterfall_usd` vs `bar_usd` redundancy remains a pattern.**
The last round confirmed the double-render on the attribution tab (rec12). Fold
the guidance into `charts`: a waterfall and a ranked bar of the *same* top-N are
redundant — pick the waterfall (it shows cumulative build-up) and give it an
explicit "Other / not shown" terminal bar so it accounts for 100%, rather than
pairing it with a bar. Prevents the pattern recurring on new pages.

**R54 (Sev: low · Effort: S) — Standardize the "partial latest period" dimming into one helper.**
`spend_trend` and `monthly_stacked_usd` each independently compute a PROVISIONAL/
partial flag and dim the last bar (`charts.py:100,443`). `daily_count_bars`,
`daily_stacked_usd`, `events_by_day` do not, so a partial latest day reads as a
real drop there. Extract one `mark_partial(df, period_col)` used by all day/month
bar charts so the honesty rule is uniform.

**R55 (Sev: low · Effort: S) — Chart height should follow density.**
Charts are a fixed `_HEIGHT=264` (`charts.py:13`); the compact-density mode
(`_COMPACT_CSS`, `theme.py:223`) shrinks cards/tables but not charts, so on a
triage screen the charts still eat the same vertical budget. Have `_base` read a
compact height when `_ow_density=="compact"`.

---

## Theme G — Navigation, orientation, discoverability

**R61 (Sev: medium · Effort: S) — Breadcrumb the current location.**
rec5 (breadcrumbs) was PARTIAL/untouched last round. The page header shows title
+ scope chips but not "Analyze › Cost & Contract › Attribution." A one-line
breadcrumb (nav group › page › section) at the top of `page_header`
(`components.py:132`) orients a deep-linked viewer who didn't click their way in.

**R62 (Sev: low · Effort: S) — Show keyboard hints on the Jump-to box.**
`_global_jump` is a strong command-palette-in-waiting (`app/main.py:333`) but is
an unlabeled selectbox placeholdered "Jump to…". Add a visible affordance (a `/`
or `⌘K` hint chip) so viewers discover it; it already routes to pages, DBs,
warehouses, and rules.

**R63 (Sev: low · Effort: S) — Persist the last section per page across sessions.**
Sections deep-link via `?section=` and survive a rerun (`components.py:100-125`),
but there's no per-page "return to where I was" across sessions. The Views/prefs
machinery (`prefs_sql`) already persists a default landing view; add an opt-in
"remember last section per page" so a DBA who lives on Operations → Locks lands
there.

**R64 (Sev: low · Effort: S) — Surface active non-default filters in the browser tab / header more loudly.**
`_scope_is_active` drives a border glow (`app/main.py:525`, `theme.py:144-146`),
which is subtle. When a warehouse/user/schema `contains` filter is live, also
reflect it in the page `<title>` (via `set_page_config` can't change post-load,
but a small persistent "filtered" pill in `page_header` can) so a scoped number
is never mistaken for account-wide after scrolling.

---

## Theme H — Motion, feedback, polish

**R71 (Sev: low · Effort: S) — Toast + inline `notify` is double feedback.**
`notify()` fires a `st.toast` *and* an inline `st.success/st.error`
(`components.py:1088-1094`) for the same action, so an operator sees the message
twice. Keep the toast (survives layout shift) and downgrade the inline echo to a
quieter, dismissible state, or make the inline echo opt-in for errors only.

**R72 (Sev: low · Effort: S) — The brand-dot pulse is decorative motion on every screen.**
`ow-brand-dot` animates a 2.8s opacity pulse forever (`theme.py:151-152`).
`prefers-reduced-motion` disables it (good, `theme.py:218`), but for everyone
else it's constant low-level motion in the peripheral vision of a monitoring
tool. Consider pulsing it only when there's an open critical (tie it to the
health strip) so motion *means* something.

**R73 (Sev: low · Effort: S) — Consistent button width policy.**
Buttons mix `use_container_width=True` (sidebar, `main.py:142`) and default width
(inline actions), and the primary-button ink override is defended against four
different SiS markup shapes (`theme.py:173-184`) — a sign the styling fights the
runtime. Document a single button convention (full-width for sidebar/section
actions, intrinsic for inline) and lean on `st.button(type=…)` rather than
markup-shape CSS where possible.

**R74 (Sev: low · Effort: S) — Popover/expander open-state should be scannable.**
Expanders are the app's main progressive-disclosure device (forecast accuracy,
score deductions, AI digest). There's no visual cue which expanders *have new/
notable* content vs routine. Add a small count/severity badge to expander
summaries that gate something actionable (e.g. "Platform score deductions (3)").

---

## Theme I — Content, copy, honesty (the app's strongest asset — keep it consistent)

**R81 (Sev: medium · Effort: S) — Centralize the recurring disclosure captions.**
The "storage & transfer bill separately" caption is authored verbatim on both
Overview (`overview.py:499`) and Brief (`brief.py:169`); the ACCOUNT_USAGE lag
note is already centralized (rec11) — do the same for the storage/transfer
disclosure and the "credits × rate" basis line so wording can't drift between
pages. Put them next to `ACCOUNT_USAGE_LAG_NOTE` in `config.py`.

**R82 (Sev: low · Effort: S) — `md_dollars` escaping is applied by hand at every sink.**
Every caption that might carry two `$` amounts calls `md_dollars` to dodge
LaTeX-math rendering (`overview.py:618`, `components.py:373`, `brief.py:213`,
`ai_panel.py:38`, …). Wrap the common sinks (`result_caption`, a `dollar_caption`
helper) so callers can't forget the escape — the current pattern is a footgun
that has bitten repeatedly (per the inline comments).

**R83 (Sev: low · Effort: S) — Consistent number humanization for large counts.**
KPI dollars use `format_usd`; counts use `{:,.0f}`. Very large counts (e.g.
"1,240,000 queries") would read better abbreviated ("1.24M"). Add a
`format_count` humanizer and use it in status-bar/KPI count values (not in
tables/exports, which keep full precision).

**R84 (Sev: low · Effort: S) — Empty-state copy should always name the next action.**
Most empty states are excellent ("checked, clean" / "install the mart on Admin →
Migrations"), but a few are terminal (`guard`'s generic `empty_message`). Ensure
every empty/`st.info` names *where to act* (a page link or the Admin setup path),
matching the strong examples already in `guard()` at `components.py:689-691`.

**R85 (Sev: low · Effort: S) — Legend/Views/Reset live in the top bar; add an in-context "?" per section.**
`panel_help` exists (`components.py:85`) but isn't universally attached. Adopt a
convention: every `section_header` optionally takes a `help=` that renders the
`panel_help` popover inline, so the "what is this / when red do X" is one click
away on every section, not only the ones that remembered to add it.

---

## Theme J — Structural / architectural (bigger bets)

**R91 (Sev: medium · Effort: M) — Extract a shared `finding_row`/`action_list` component.**
The last round CONFIRMED rec10 (the same ACTION_QUEUE/finding data is rendered
4+ ways) and it remains largely open: Overview uses a selectable table
(`overview.py:528`), Brief uses bullets (`brief.py:210-215`), the Control Room
uses its own. Extract one `action_list(df, mode="table"|"bullets", on_select=…)`
so severity color, ranking caption, and click-to-navigate are defined once. This
is the single largest consistency win still on the board.

**R92 (Sev: low · Effort: M) — A component gallery / visual regression harness.**
There's no place to see all cards/sections/chips/charts at once, so drift (the
five palette copies above) is only found by reading files. Add a hidden
`?dev=components` route rendering every component with sample data. Pairs with R7:
the gallery is where a human confirms the token unification actually looks right
(headless snapshot tests were rightly declined last round; a manual gallery is
the pragmatic middle).

**R93 (Sev: low · Effort: M) — Promote `lazy_sections` to the standard page scaffold.**
`lazy_sections` (render only the selected section, `components.py:92`) is the
right pattern and is used, but each page still hand-assembles header + status bar
+ sections. A `page_scaffold(title, sections=…)` helper would guarantee every
page gets the header, the account-usage note, the section nav, and per-section
fragments (R21) uniformly — and make a new page correct by construction.

**R94 (Sev: low · Effort: S) — Density toggle should also relax chart/altair sizing and table row height.**
`_COMPACT_CSS` (`theme.py:223-234`) tightens padding and dataframe font but not
Altair charts (R55) or the `st.dataframe` row height. Complete the density
contract so "compact" is uniformly denser, not partially.

**R95 (Sev: low · Effort: S) — Consolidate the three "partial deployment" info paths.**
`guard()` special-cases the "run the migrations" message (`components.py:688`),
KPIs render "Needs daily facts"/"Setup" (`overview.py:457,470`), and pages render
their own `st.info`. A single `setup_needed(what, where)` helper would make
"this needs installing, here's where" look identical everywhere, reinforcing the
honest-degrade story.

---

## Sequencing — one "palette & scaffold" wave, then polish

**DO-FIRST (highest impact-per-effort, all app-only, no migration):**

1. **R7 + R1–R6** — one palette module; reconcile `config.toml`; delete dead
   selectors (R2). Kills the whole color-drift class and the first-frame flash.
2. **R21** — fragment-scope the interactive panels. Biggest perceived-latency win.
3. **R31** — skeleton/progress on cold scans. Turns "blank = broken" into "loading."
4. **R42** — green all-clear empties in `guard()` (the still-open A4).
5. **R41** — `%` on percent columns; **R23** equal-height KPI cards. Two tiny scannability fixes.

**NEXT:** R8 (menu_items), R10 (`st.html`), R32/R33 (icon-button labels + focus
ring), R51/R52/R54 (chart takeaways + uniform partial-dimming), R91 (shared
finding row — the open rec10), R81/R82 (centralize disclosures + `$`-escape).

**BIGGER BETS (weigh openly):** R9 (`st.navigation`), R92 (component gallery),
R93 (page scaffold). Each is a modernization, not a bug — prototype behind a flag.

**Notes on prior rounds:** items that were DECLINED last round for good reasons
are not re-proposed here (palette flattening, section-nav dropdown, alert-drawer
modal, headless snapshot CI). Where an item extends a shipped/partial rec it is
labeled inline (A1→R1/R7, A2→R34, A4→R42, rec10→R91, rec11→R81, rec12→R53,
rec13→R43, rec14→R44, rec15→R32/R33/R35).
