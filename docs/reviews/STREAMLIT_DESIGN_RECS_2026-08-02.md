# 50 Streamlit design recommendations (2026-08-02, v4.122)

Fifty impactful, evidence-grounded recommendations for the app's Streamlit design,
from a full pass over `app/main.py`, `app/theme.py`, `app/ui/*` and all page
modules. Numbered **SD01–SD50** (the `rec1–rec20` / `A1–A5` namespaces from
`docs/reviews/DESIGN_REVIEW_2026-07-31.md` and `RECS_REVIEW_*` are taken; many of
those shipped — markers are in code).

**Respects prior owner adjudications.** Nothing here re-litigates the 2026-07-31
declines: no dropdown section nav (rec 8), no drawer→modal (rec 9), no chip→prose
collapse (rec 12), no palette flattening (rec 17), no full responsive ladder +
snapshot tests (rec 19), no Brief metric cap (rec 3). Two accepted-but-unshipped
items from that round are carried forward and marked: A4 (SD26) and A5 (SD34).

Tags: **P1/P2** priority · **S/M/L** effort. Items marked **[ver]** depend on a
Streamlit feature newer than the SiS conda channel may serve — verify
`st.__version__` in SiS first; local dev pins `streamlit~=1.45` which has all of
them. Everything degrades per the house pattern (try/except, feature-detect).

---

## A. Navigation & app shell

1. **SD01 — Replace the hand-rolled sidebar nav with `st.navigation`/`st.Page`.**
   (P1/M) **[ver]** `main.py:95–137` documents three bug classes the grouped-radio
   hack needed: multi-select highlight, stale per-group keys (fixed by re-scoping
   keys to the current page), and the `?page=` deep-link echo (`_ow_req_seen`
   reconcile). `st.navigation` (1.36+) provides grouped sections
   (`{"Watch": [...], "Analyze": [...]}` — maps 1:1 onto `NAV_GROUPS`), URL
   routing, and per-page icons natively, deleting ~60 lines of fragile state
   machinery and the whole desync class.

2. **SD02 — `st.page_link` for payload-less cross-page jumps.** (P2/S) **[ver]**
   Buttons that call `request_navigation` with no filter payload (e.g. Brief's
   "Open the alert queue →", `brief.py:197`) cost a full rerun plus pending-nav
   session state. A real link gives anchor semantics (middle-click, open-in-new-tab)
   for free. Keep `request_navigation` for jumps that carry filters.

3. **SD03 — `st.segmented_control` for `lazy_sections`.** (P2/S) **[ver]** Not the
   declined dropdown — a segmented control keeps every option visible (the decline
   reason) while shedding the CSS reskin of
   `div[role="radiogroup"][aria-label="Section"]` (`theme.py:157–168`) that breaks
   whenever Streamlit's radio DOM shifts. Keep the lazy single-section dispatch,
   the slug deep-link, and the wrap behavior.

4. **SD04 — Make the section nav sticky.** (P1/S) On Alerts → History or Security
   → Access (10+ stacked panels) switching sections means scrolling all the way
   back up. `position:sticky; top:0` (plus a surface background + z-index) on the
   section-nav container keeps the pills reachable; works with the current radio
   or SD03.

5. **SD05 — Deep links that carry scope.** (P1/S) `?page=` and `?section=` sync
   today (`components.lazy_sections`, `core/state.py`), but company/days/database
   don't — a pasted URL reproduces the *place* but not the *view*. Serialize the
   non-default filters into query params and add a "Copy link to this view" row
   in the Views popover (the `_current_view_payload()` JSON already exists —
   reuse it).

6. **SD06 — Grow "Jump to" into a command palette.** (P2/S) `main.py:333–383`
   offers pages, DBs, warehouses, rules — but not sections. Add "Page · Section"
   entries from `PAGE_SECTION_KEYS` and the user's recent pages (APP_USAGE
   already logs visits). Most navigation questions are "where do I see X" —
   sections are the real destinations.

7. **SD07 — Promote "Optimization & Savings" to its own page.** (P1/M)
   `cost_parts/optimize.py` is 986 lines and ~12 toggle-gated panels — the app's
   densest working surface, buried as one of six sections inside Cost & Contract.
   A top-level "Optimize" entry in the Analyze nav group shortens the daily path
   and lets its sections get their own `lazy_sections` split (Advisors / Ledger /
   Remediation / Savings).

## B. Chrome & vertical rhythm

8. **SD08 — Collapse the chrome bands.** (P1/M) Every page opens with up to five
   stacked chrome rows: sidebar health strip, the bordered "Triage filters" box,
   the `ow-statusbar` cards, the page header, and captions. The status bar's
   Scope cell duplicates the filter controls directly above it; open-criticals
   shows in the sidebar strip AND the status bar AND (on Overview) a KPI card.
   Fold `_persistent_status_bar` into the triage-filters container's header row
   and reclaim a full band above the fold.

9. **SD09 — Retire the always-on lag caption.** (P2/S) rec11 removed the
   ACCOUNT_USAGE lag note from ~76 panel captions, but `page_header`
   (`components.py:154`) still prints it on every page, every render. Once
   learned it is noise. Move it into the Legend popover and the telemetry-age
   stat's tooltip; the health strip already carries *actual* staleness.

10. **SD10 — Sidebar "About" popover.** (P2/S) Version string, the
    "telemetry lags…" caption (`main.py:150`), and the last-refreshed note crowd
    the nav rail permanently. One small "v4.122 · About" popover holds
    version/lag/refresh detail; the rail keeps brand + nav + health only.

11. **SD11 — Promote and persist the density toggle.** (P1/S) Compact density is
    the ops user's mode, yet it hides inside the Views popover and lives only in
    session state (`main.py:228–232`) — every new session resets it. Surface it
    as a topbar icon toggle and persist to USER_PREFS like DISPLAY_TZ already is.

12. **SD12 — One clock for chrome timestamps.** (P2/S) `last_refreshed_note()`
    uses `datetime.now()` (server tz) and Brief's footer uses
    `pd.Timestamp.now()` (`brief.py:233`), while every data surface uses account
    time / display tz. Route chrome timestamps through `account_now()` + the
    display-tz preference — trust tools shouldn't have two clocks.

13. **SD13 — One narrow-laptop tune-up (not the declined ladder).** (P2/S) The
    only media query is 640px (`theme.py:206`). At ~1100–1280px the 4-column
    filter row + 3-column "More filters" and 4-up KPI rows visibly cramp. One
    intermediate rule (filters 2×2, KPI min-width) covers the 13" laptop case
    without the rejected snapshot-test apparatus.

## C. Visual hierarchy

14. **SD14 — Give Overview a hero number.** (P1/S) Five equal-weight KPI cards
    yield no focal point on the flagship page. Add a `hero` flag to `kpi_row`
    that renders the first card at double width/value-size (window spend or
    platform score). Executive glance pages need one number first, four numbers
    second.

15. **SD15 — Finish the `section_header` adoption.** (P1/S) The prior round's
    heading pass (rec 6, PARTIAL) stopped at Overview. Alerts → History
    sub-panels, Security → Access (~10 panels), Optimize's toggle panels, and
    Brief's "Fires"/"Asks" still use `st.markdown("**…**")`, and Control Room
    mixes in one `st.subheader` (`control_room.py:576`). The themed header with
    severity stripe + count badge is the scan rhythm on exactly these densest
    pages.

16. **SD16 — Flatten the alert drawer's inner stack.** (P2/M) The drawer itself
    stays (rec 9 decline respected). Inside it, Playbook → Respond → AI explain →
    SQL preview are vertically stacked expanders (`alerts.py:384–636`), so the
    respond form can sit 2–3 folds deep. A small in-drawer segmented control
    (Playbook / Respond / AI / SQL) flattens depth without hiding anything —
    content is already loaded, this is layout only.

17. **SD17 — Equal-height KPI cards.** (P2/S) Cards carry `min-height:96px`
    inline (`components.py:262`), but delta/spark presence still varies heights
    within a row. Stretch `.ow-card` to 100% of its column and move the
    min-height into the theme so compact density can tune it too.

18. **SD18 — TOC chips for 6+-panel walls.** (P2/M) Security → Access, Operations
    → Pipeline SLA, and Alerts → History are long single scrolls. A row of anchor
    chips at section top ("MFA · Grants · Keys · Break-glass · …"), or
    promote-top-3-collapse-the-rest, gives the wall a map. Pairs with SD04.

## D. Component consistency

19. **SD19 — Close the 8 raw `st.dataframe` bypasses + add a drift lock.** (P1/S)
    `operations.py:587`, `control_room.py:598`, `spend.py:246`, `optimize.py:580`,
    `contract.py:323` + `:564`, `ai_chargeback.py:172`, `admin.py:472` bypass
    `styled_table`, silently losing status colors, number formats, CSV export,
    header prettifying, and timezone conversion (admin.py even documents the
    convention it breaks). Fix the eight, then pin with a test that greps
    `app/ui/pages` for `st.dataframe(` — same enforcement style as
    `test_perf_budgets`.

20. **SD20 — `status_chips` has zero callers: adopt or delete.** (P2/S)
    Defined at `components.py:731`, referenced nowhere. Either use it (Alerts
    severity summary, Security posture chips) or remove it — dead design-system
    API teaches contributors the wrong vocabulary.

21. **SD21 — One `confirm_action()` component for typed gates.** (P1/M) Nine
    hand-rolled variants: `ACK`, `RESOLVE`, `BULK ACK`, `BULK RESOLVE`,
    `EMERGENCY`, `CANCEL`, `DECLARE`, type-the-setting-key, type-the-warehouse/
    table-name. Same skeleton every time (text input, disabled-until-match
    button, help copy), reimplemented with drifting copy. One component with a
    `blast_radius` slot standardizes muscle memory for the app's most dangerous
    clicks.

22. **SD22 — `st.dialog` for the final destructive click.** (P2/M) **[ver]**
    Complement to SD21 (not the declined drawer-modal): the *last* irreversible
    confirmation (emergency levers, bulk RESOLVE) moves into a modal holding the
    typed gate + blast radius, so a mid-confirmation scroll or rerun can't leave
    a half-armed form sitting in the page flow.

23. **SD23 — Replace emoji glyphs with the icon system.** (P2/S) `⚠` in primary
    buttons (`brief.py:180`, `control_room.py` ~263), `⬇` on every table export,
    `✅/⚠️` in `notify()` toasts, `🛰️` as page icon — while `icons.py`'s own
    docstring says emoji render inconsistently across platforms.
    `st.button`/`st.download_button` accept `icon=":material/warning:"` — use it,
    or the SVG set where HTML is possible.

24. **SD24 — One download-affordance wrapper.** (P2/S) Table exports are tertiary
    icon buttons with `on_click="ignore"`; document exports are labeled
    `use_container_width` buttons; `download_text_button` is a third shape. Wrap
    once (`table_export()` / `document_export()`) so type/help/on_click semantics
    can't drift per call site.

25. **SD25 — Stop `notify()`'s double feedback.** (P2/S) Every action fires a
    toast AND an inline `st.success`/`st.error` (`components.py:1088–1094`).
    After several actions on a long page the inline banners stack into noise.
    Keep the toast for success; reserve inline placement for errors (which must
    persist).

26. **SD26 — Ship A4: green all-clear empty states.** (P1/S) Accepted in the
    2026-07-31 round, still unshipped: `guard()` renders neutral blue `st.info`
    for every empty (`components.py:698`), so "no open alerts" — the flagship
    good news — reads as a notice. Add an `empty_state(kind="clean"|"pending")`
    helper: green + check icon for genuinely-good empties, calm info for
    not-installed-yet, and adopt it at the ~40 `guard()` call sites.

## E. Tables & data display

27. **SD27 — Native multi-row selection for Alerts bulk ops.** (P1/M) **[ver]**
    The bulk ACK/RESOLVE flow re-lists potentially hundreds of event titles in a
    separate `st.multiselect` below the table. `st.dataframe`
    `selection_mode="multi-row"` selects in place — one surface, no duplicate
    list, and the selection survives the existing fragment reruns
    (`selectable_table` already wraps single-row; extend it).

28. **SD28 — "Showing X of Y · Load more" for capped feeds.** (P2/M) The alerts
    feed caps at 500 with a caption; detail tables truncate at
    `DEFAULT_MAX_ROWS=5000` with a warning to narrow filters. An incremental
    "load next 500" (bump a LIMIT in session state) is a kinder pattern than
    telling users to go change filters, and keeps first paint cheap.

29. **SD29 — Signal row-clickability consistently.** (P1/S) `selectable_table`
    rows look identical to static rows. Overview captions "Click a row to open…"
    (`overview.py:543`); Control Room triage and Operations queries don't.
    Standardize: `selectable_table` renders the hint caption itself (param to
    suppress), so no call site can forget the affordance.

30. **SD30 — Relative fetch age in `result_caption`.** (P2/S) "fetched 14:03:22"
    forces clock math; the sidebar already renders "Updated 4m ago"
    (`last_refreshed_note`). Show "fetched 4m ago (14:03:22)" — relative first,
    absolute in parentheses.

31. **SD31 — Truncate long errors in `guard()`.** (P2/S) A failed panel prints
    the full Snowflake error inline (`components.py:693`) — compilation errors
    run hundreds of characters and blow out panel layout. One-line summary
    inline + full text in an expander ("Error detail"), matching the
    connection-error pattern `main.py` already uses.

32. **SD32 — Admin migration warnings as a status table.** (P2/S) Migrations &
    freshness renders pending/drift warnings as long `md_dollars` strings. A
    `styled_table` with a STATE column rides the existing `STATUS_COLOR_MAP`
    (OK/PENDING/STALE already have colors) — scannable at a glance, CSV export
    for the runbook free of charge.

## F. Charts

33. **SD33 — Dark Vega tooltips.** (P1/S) Chart tooltips render in Vega's default
    white theme — the one bright-white surface in an otherwise tokenized dark UI.
    Pass the dark tooltip theme through the chart embed options (or usermeta) in
    one place — `_base()` — so every chart inherits it.

34. **SD34 — Adaptive x-axis ticks (ships part of A5).** (P1/S) Day-grain axes
    hardcode `tickCount="day"` + `labelOverlap="greedy"`
    (`charts.py:106,181,227,260,389`) — at 90/180/365d the axis is label soup
    saved only by overlap culling. One shared axis helper picking day/week/month
    units by span, used by all five day-grain charts, also centralizes the A5
    axis/legend conventions (legend orient still flips top vs bottom per chart).

35. **SD35 — Click-to-drill on charts.** (P2/M) **[ver]** Tables got row-select →
    navigate; charts are hover-only. `st.altair_chart(on_select="rerun")` with a
    point selection lets "Top cost drivers" bars jump to Operations scoped to
    that warehouse, and the monthly boss chart drill into a month × warehouse.
    Start with `bar_usd` — one shared param, biggest reuse.

36. **SD36 — Annotate sparkline_row with last value + Δ.** (P2/S) The Brief/
    Overview spark rows (`charts.py:274–296`) are unlabeled trend shapes — "a
    number without direction is half a number" cuts both ways: a direction
    without magnitude is half a trend. Print the latest value and window delta
    beside each label (the KPI-card spark already pairs value+trend; the row
    should too).

37. **SD37 — Pick one heatmap ramp.** (P2/S) `hour_heatmap` hardcodes
    `scheme="orangered"` (`charts.py:318`) while the registered theme defines a
    brand cyan heatmap range (`charts.py:48`) that consequently never applies.
    Use the theme ramp (or delete it and standardize on orangered) — one ramp,
    stated in the Legend.

38. **SD38 — Fixed-height honest chart empties.** (P2/S) Empty data renders a
    text `st.info` where a 264px chart would be, so panels jump height between
    reruns/filters. An `empty_chart(note)` placeholder at `_HEIGHT` with a
    centered "no data in this window" keeps layout stable and reads as a chart
    that is honestly empty, not a missing one.

39. **SD39 — Theme the task-DAG Graphviz.** (P1/S) `operations.py:546` fills
    nodes with light pastels (`#fecaca`/`#e2e8f0`/`#bbf7d0`) on a default white
    canvas — the single biggest theme break in the app on a dark screen. Set
    `bgcolor="transparent"`, node fills from the dark `status_colors` pairs,
    font/edge colors from tokens.

## G. Theme & CSS hygiene

40. **SD40 — Deduplicate `.ow-chip`.** (P1/S) Defined twice with conflicting
    values (`theme.py:129` gap 5px/`rgba` bg vs `theme.py:138` gap 6px/raised
    bg); the later rule silently wins. Merge into one definition; behavior today
    is an accident of source order.

41. **SD41 — Fix dead/missing CSS.** (P2/S) `.ow-brand-word` (`theme.py:153`) has
    zero users; `.ow-scope-row` is emitted by `page_header`
    (`components.py:151`) but never defined, so the scope-chip row has no
    spacing rule. Delete the former, define the latter, and add a small
    class-drift test (classes emitted by components ⊆ classes defined in theme).

42. **SD42 — Replace test-id/:has selectors with `st.container(key=)` hooks.**
    (P2/M) **[ver]** The active-scope glow rides
    `div[data-testid="stVerticalBlockBorderWrapper"]:has(.ow-scope-active)`
    (`theme.py:144`) — the exact fragility theme.py's docstring apologizes for.
    Streamlit ≥1.39 stamps `st-key-<key>` classes on keyed containers: target
    `.st-key-ow_scope_box` instead and the selector survives DOM churn.

43. **SD43 — Decide dark-only and delete the light path.** (P2/S)
    `.streamlit/config.toml` pins dark; every token in `theme.py` is dark-only;
    yet `status_colors.py:23–41` maintains a light-equivalent map + runtime
    theme detection that can never fire in SiS. Either finish light tokens
    (weeks of surface work) or delete the dead branch. Recommend delete — one
    theme, honestly.

44. **SD44 — Move inline styles into theme classes.** (P2/S) `metric_card_html`'s
    `min-height:96px`, `main.py:_strip_line`'s inline flex/dot styles, the
    sidebar refresh caption's inline font style (`main.py:91`), and
    `lazy_sections`' injected `<hr>` (`components.py:123`) all bypass the token
    layer — compact density and future theme changes can't reach them. One
    class each in `_CSS`.

45. **SD45 — One numeric-precision policy, shared by tables and charts.** (P2/S)
    Tables format USD as `${:,.2f}` (`_auto_formats`) while charts use `$,.0f`;
    large-frame tables silently drop thousands separators (printf path,
    `components.py:828`). Adopt magnitude-aware precision (cents only under
    $1k) in one helper both `_auto_formats` and chart encodings read.

## H. Feedback & perceived performance

46. **SD46 — Cover the unspinnered long operations.** (P1/S) `st.spinner` exists
    only in `ai_panel` and three Optimize scans. Alerts "AI explain", the
    Security auditor zip (10 heavy sheets), chargeback queue inserts, and Admin
    reconciliation run silent — a click with no feedback for many seconds. Add
    spinners; use `st.status` with step lines for the multi-step ones (canary's
    `st.progress` is the house precedent).

47. **SD47 — Skeleton first paint for KPI rows.** (P2/M) Overview/Brief render
    nothing, then everything, when the first-paint `run_batch` resolves — the
    p95s in APP_USAGE say this is seconds. Render placeholder cards (gray
    shimmer via a small CSS class on `ow-card`) into `st.empty()` slots before
    the batch, then overwrite. Perceived speed without touching a query.

48. **SD48 — More `@st.fragment` islands around actions.** (P1/M) Alerts'
    open-events section proves the pattern: clicks repaint the fragment, not the
    page. Optimize's remediation execute, Admin's canary, and chargeback's
    budget MERGE still rerun the entire page (and its chrome queries) per
    button click. Fragment-scope each action panel — the cheapest large
    interaction-latency win available.

## I. Accessibility

49. **SD49 — Direction glyphs in delta cells.** (P1/S) `delta_css` colors
    movement columns red/green text only (`status_colors.py:119–134`) — the A2
    principle (severity never rests on hue alone) stops at the timeline dots.
    Prefix ▲/▼ via the same Styler format pass so a +$40k and −$40k differ by
    more than hue and a minus sign.

50. **SD50 — Focus rings + a tooltip length budget.** (P2/S) Only `.ow-help` has
    a `:focus-visible` treatment; buttons, nav radios, and section pills rely on
    browser defaults muted by the dark theme. Add one token-based focus ring for
    interactive chrome. And cap `data-help` CSS tooltips at ~160 chars — several
    KPI helps run 3+ sentences and render as giant hover paragraphs
    (`overview.py:449–453,484–492`); long prose belongs in `panel_help`
    popovers.

---

## Suggested sequencing

**Wave 1 — polish sprint (all P1/S, app-only, no migration):**
SD40 (chip dedupe) · SD41 (dead/missing CSS) · SD39 (DAG theming) · SD26 (A4
empty states) · SD19 (dataframe bypasses + lock) · SD15 (section_header pass) ·
SD29 (row-click affordance) · SD33 (dark tooltips) · SD34 (adaptive ticks) ·
SD49 (delta glyphs) · SD46 (spinners) · SD04 (sticky section nav) · SD05 (scope
deep links) · SD11 (density persist) · SD14 (hero KPI).

**Wave 2 — structural (P1/M):**
SD21 (confirm component) · SD48 (fragment islands) · SD08 (chrome collapse) ·
SD07 (Optimize page) · SD27 (multi-row bulk select **[ver]**) · SD01
(st.navigation **[ver]**).

**Wave 3 — refinements (P2):** everything else, cheapest first; batch the
**[ver]** items behind one SiS version probe.

Every item is app-only (no migrations). The **[ver]** items need one fact
checked first: the Streamlit version the SiS conda channel actually serves.
