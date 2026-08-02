# 50 Streamlit design recommendations — OVERWATCH (2026-08-02)

Grounded in the live UI (`app/theme.py`, `app/ui/components.py`, `app/ui/charts.py`,
`app/main.py`, page modules) after the v4.103/v4.104 scannability waves. These are
**net-new** items — not re-litigation of shipped recs 1–20 / A1–A5 from
`DESIGN_REVIEW_2026-07-31.md`.

**How to read this list.** Each item: impact → effort → primary file(s) → why.
Priority bands: **P0** (operator risk / daily friction), **P1** (scan & trust),
**P2** (polish & consistency), **P3** (ambitious / SiS-constrained). Effort:
**S** = theme/CSS or one helper, **M** = shared component + a few call sites,
**L** = cross-page IA or new interaction model.

Constraints that bound every rec: Streamlit-in-Snowflake CSP (no external fonts/
scripts), owner's-rights SiS, honesty rules (no fabricated zeros), and
`lazy_sections` (never swap back to `st.tabs` for section nav).

---

## A. Information architecture & above-the-fold (1–10)

### 1. One "work now" rail on every page — P0/M
Promote the Action Queue / open-critical strip into a shared `work_rail()` that
opens every page (Overview already hoists Top actions; Alerts/Control Room/
Operations still bury ownership under charts or expanders). Same three columns:
severity · entity · next step. File: `app/ui/components.py` + page tops.

### 2. Role-profile first paint contracts — P0/M
`PAGES_BY_PROFILE` already trims nav; extend it to a **first-paint contract**
per profile (EXECUTIVE = Brief+Overview KPIs only; OPERATOR = Alerts triage +
blast-radius actions; ADMIN = freshness + migrations). Today every profile gets
the same chrome density. File: `app/config.py`, `app/main.py`.

### 3. Collapse Cost's six-section mega-page — P1/L
Cost still packs Spend, Contract, Chargeback, Unit costs, Compare, Optimization
into one lazy radio (`cost.py:64-66`). Split **Unit costs + Compare** into a
sibling page or a second-level "Tools" group so the money narrative stays
three sections. Operators lose the thread after the fourth pill wrap.

### 4. Operations IA: Queries / Warehouses / Tasks as hubs — P1/L
`operations.py` (~1064 lines) is a vertical encyclopedia. Group into three hubs
with `lazy_sections`, and move Emergency levers + Change impact behind a
Govern sub-rail. Same content, shorter first scroll.

### 5. Brief as the default landing for EXECUTIVE — P1/S
Brief is already the phone-first contract; make it the profile default when no
`DEFAULT_VIEW` is saved (today Overview wins). One-line change in
`_apply_default_landing` + profile map.

### 6. Persist "last useful section" per page — P1/S
`lazy_sections` deep-links via `?section=` but a mid-session nav away and back
re-seeds from query params / first label inconsistently. Persist
`_ow_last_section_{page}` in `USER_PREFS` so Operators return to Open events,
not Rules.

### 7. Breadcrumb trail under the page header — P1/S
Deferred half of old rec 5. Render `Watch › Alerts › Open events` (group ·
page · section) as one mute caption under `page_header`. Orientation without
re-introducing the OVERWATCH kicker.

### 8. Sticky scope strip that survives scroll — P1/M
Scope chips live in the header (`page_header`) and vanish after one viewport
of tables. A CSS `position:sticky` bar (company · days · active filters ·
Reset) under the Streamlit header keeps the honesty contract visible while
scanning long Cost/Ops tables.

### 9. Demote nested expanders in the Alerts drawer — P0/M
The event drawer nests Playbook / History / Respond / Explain expanders
(`alerts.py`). Flatten to a segmented control inside the fragment (Detail |
Fix | Explain) so triage is one click deep, not four accordion opens.

### 10. Overview chart stack: one flagship, rest progressive — P1/S
Monthly stack + spend trend both claim the fold after Top actions. Keep the
boss chart; tuck the daily trend behind a "Daily detail" toggle (pattern
already used for Storage on Cost).

---

## B. Component system & visual language (11–22)

### 11. Export tokens as a Python palette module — P1/S
`--ow-*` CSS tokens still re-hardcoded as `_SEV_HEX`, `SEV_COLORS`,
`_STRIP_COLORS`, spark defaults. One `app/ui/palette.py` feeding theme CSS
*and* Python callers ends the A1 drift class permanently. Lock with a test that
hex sets are identical.

### 12. Green all-clear empty states (ship A4) — P1/S
`guard()` always uses `st.info` (blue). Distinguish `empty_kind="clear"|"setup"|"none"`
so "No open criticals / checked, clean" renders `ow-empty--ok` (green stripe),
while setup gaps stay info and errors stay error. House law #8 already wants
this wording; the color must match.

### 13. Shared `finding_row()` / `action_list()` — P1/M
Overview selectable table, Brief bullets, Control Room triage, and AI
chargeback exceptions still hand-roll presentation. One helper: severity chip ·
title · entity · CTA button. Completes the unfinished half of rec 10.

### 14. Chart takeaway helper — P1/S
Only Overview cost-drivers lead with a conclusion caption. Add
`charts.takeaway(df, template=...)` and require it on every chart call site
(spend trend, movers, heatmap, task DAG). "Top mover: WH_X · +$12k (38%)".

### 15. Replace page_config emoji with SVG favicon data-URI — P2/S
`st.set_page_config(page_icon="🛰️")` is the last emoji brand mark; nav already
banned emoji. Use a tiny inline SVG / PNG data URI so the browser tab matches
the sidebar brand-dot.

### 16. Kill the brand-dot glow or gate it — P2/S
`.ow-brand-dot` uses a 10px cyan glow + pulse (`theme.py`). On SiS projectors
and OLED it blooms. Prefer a solid accent disc; keep pulse only when
`prefers-reduced-motion: no-preference` *and* a critical is open (status as
motion, not decoration).

### 17. Raise interactive hit targets to 44px — P1/S
Section pills, tertiary download "⬇", and sidebar radios sit under 32px tall.
Bump padding for touch/Brief phone use; keep compact density as the opt-in
shrink path.

### 18. Discoverable density + timezone — P2/S
Compact density and display TZ hide inside Views. Surface a one-line "Display"
popover next to Refresh (or a gear icon in the status bar) so Ops can toggle
without hunting.

### 19. Download affordance: label + icon — P2/S
`⬇` alone is cryptic and fails WCAG name. Use `⬇ CSV` / `Prepare CSV` always
(large-frame path already does the latter). File: `_render_table`.

### 20. Skeleton placeholders for mart-first panels — P1/M
Heavy Cost/Ops panels flash empty then populate. Emit a CSS skeleton card
(same min-height as `ow-card`) while `run_mart_first` / `run_batch` are in
flight — Streamlit spinners alone don't preserve layout, so the page jumps.

### 21. Unify heading hierarchy across Admin / Security / Ops — P2/M
Control Room/Overview/Cost use `section_header`; Admin and parts of Ops still
mix `st.subheader` and raw markdown. Lint: no `st.subheader` outside an
allow-list; everything goes through `section_header`.

### 22. Method/scope badge contrast pass — P2/S
`--ow-src-badge--method` purple (`#c084fc`) and mute scope grey sit near the
edge of AA on raised surfaces. Re-tune against `--ow-raised` / `--ow-surface`
like the v4.96 mute-ink lift, or swap method to a non-purple token (teal hatch)
so severity hues stay unique.

---

## C. Tables, charts & data density (23–32)

### 23. Row density modes for dataframes — P1/S
Comfortable vs compact already exists for cards; extend `_COMPACT_CSS` to
`stDataFrame` row padding and default font (0.82rem is a start). Add a third
"dense triage" (0.75rem, 28px rows) for Alerts Open events.

### 24. Pin severity + entity columns, not only col[0] — P1/S
Wide-table pin only freezes `df.columns[0]`. For alert/action frames, pin
`SEVERITY` + entity name explicitly via a `pin=("SEVERITY","ENTITY")` kwarg on
`styled_table`.

### 25. Conditional formatting for threshold breaches — P1/M
Status colors cover enums; numeric breaches (cloud-services %, queue ms,
budget %) stay monochrome. Extend styler with
`threshold_css(col, warn_at, bad_at)` driven by SETTINGS keys.

### 26. Heatmap scroll trap → pagination — P2/S
`HEATMAP_MAX_ROWS = 20` already caps; above that, offer "Next 20 warehouses"
instead of silent truncation. Caption must say what was cut (honesty rule).

### 27. Boss-chart companion: always show movers table — P1/S
Rec 16 added movers for the monthly stack; enforce the same pattern for any
stacked/multi-series chart (Attribution waterfall, Chargeback dept bars).
Chart for shape, table for action.

### 28. Dual-axis / small-multiples for company compare — P2/M
ALFA vs Trexis compare currently fights for one legend. Prefer two small
multiples with a shared y-domain over a 9-series stack. File: `compare.py` +
`charts.py`.

### 29. Sparkline hover value — P2/S
KPI sparklines are decorative; add a native `title=` on the SVG last point
("latest: $x · Δ7d: y%") so hover/focus reveals the number without a full
chart.

### 30. Print / PDF stylesheet for Brief + exec summary — P1/M
Rec 20 landed print CSS on the HTML export; Brief itself has none. Add
`@media print` rules that hide sidebar, filters, and download chrome so
"Print Brief" from the browser is board-ready.

### 31. Column show/hide presets per table — P2/M
Ops Queries and Alerts Open events ship 12–20 columns. A Views-saved
`TABLE_COLS:{page}:{slug}` pref lets operators hide noise without code
changes.

### 32. Consistent empty-chart contract — P2/S
Some charts no-op silently (`spend_trend` returns early); callers sometimes
forget `guard()`. Make every chart function call `st.info` itself on empty
input with a required `empty=` kwarg — fail loud in tests if omitted.

---

## D. Navigation, filters & shell (33–40)

### 33. Sidebar page icons beside labels — P1/S
Nav is plain text because `st.radio` can't render HTML; inject a CSS
`::before` map keyed on label text (or switch to `st.pills` / custom HTML nav
if the runtime allows) so icons match page headers.

### 34. Hide radio dots (deferred rec 7) behind a feature flag — P2/S
DOM-fragile, but ship behind `_ow_nav_css_v2` session flag with a visual
lock test in AppTest. Cleaner nav without betting the farm.

### 35. Jump box: recent targets — P1/S
Jump already lazy-loads warehouses/rules. Persist last 8 jumps in session
(and optionally USER_PREFS) at the top of the selectbox — Ops muscle memory.

### 36. Filter "More" auto-open is good; add clear-per-chip — P1/S
Active warehouse/user/schema chips in the header aren't dismissible. Make each
chip a button that clears that one filter (pattern: Streamlit chip close).

### 37. Deep-link copy button — P2/S
`?page=&section=` already works. Add "Copy link to this view" next to Views
that serializes page + section + filters into the clipboard-friendly URL /
downloadable text (SiS may block clipboard APIs — fall back to a read-only
text input).

### 38. Health strip vs status bar de-duplication — P2/S
Sidebar strip and `_persistent_status_bar` repeat criticals / staleness / MTD.
Keep the status bar; shrink the sidebar to **only** the actionable critical /
undelivered buttons. Less chrome, same signal.

### 39. Keyboard shortcuts cheat-sheet — P3/M
`R` refresh, `1–8` pages, `/` jump focus — via `st.components` or query-param
hooks where CSP allows. Document in Legend popover. SiS may block; prototype
locally first.

### 40. Connection-loss banner — P0/S
When `connection_available()` flips false mid-session, paint a full-width
`ow-banner--bad` above the status bar ("Snowflake session lost — Refresh")
instead of letting every panel error individually.

---

## E. Accessibility, motion & responsive (41–46)

### 41. Focus ring system for custom HTML — P1/S
`.ow-help` has `focus-visible`; `.ow-card`, chips, section headers, and
status-bar cells do not. Global `:focus-visible { outline: 2px solid var(--ow-accent) }`
on interactive custom elements.

### 42. Screen-reader labels on severity stripes — P1/S
Severity is still mostly color + left stripe. Add visually-hidden text
(`<span class="ow-sr-only">CRITICAL</span>`) inside metric cards and status
cells so AT doesn't depend on hue (pairs with shipped A2 shapes on charts).

### 43. Light-theme first-class path — P2/M
`status_colors` already has light pairs; `.streamlit/config.toml` forces dark.
Offer a Views toggle that flips `theme.base` / injects a light token sheet for
projector / printed-meeting use without washing out charts.

### 44. Brief mobile: single-column KPI force — P1/S
At `max-width:640px`, KPI rows still try 3–4 columns. Force `kpi_row` to
`columns=1` (or 2) under a mobile CSS + columns override so Brief stays
thumb-scrollable.

### 45. Reduce-motion already present — extend to status pulse — P2/S
`prefers-reduced-motion` kills transitions; ensure the brand-dot animation and
any future skeleton shimmer respect it (see #16).

### 46. Contrast audit on chart label gray — P2/S
Altair `_LABEL = "#8b98ad"` on transparent/dark backgrounds is ~3.8:1. Lift to
match `--ow-ink-mute` (#8593a8) or brighter so axis ticks clear AA.

---

## F. Trust, honesty & operator feedback (47–50)

### 47. Panel-level "why is this number?" drawer — P1/M
`panel_help` + metric `?` exist, but dollars still confuse measured vs
allocated vs estimated. A standard footer on Cost panels: registry grain ·
method · window served (`served_days`) · source path — one line, always.

### 48. Optimistic UI for ack/resolve with undo window — P1/M
Bulk ack already exists; per-event ack still feels laggy. Toast + local status
flip immediately, server confirm async, 5s "Undo" via fragment. Cuts triage
time without weakening audit.

### 49. Error taxonomy styling — P1/S
`guard()` treats setup-missing calmly; other failures are generic red.
Map `error_kind` (absent / permission / timeout / SQL) to distinct banners so
operators know whether to wait, escalate, or open Admin → Migrations.

### 50. Design-system Storybook page under Admin — P2/M
An Admin → "UI kit" section that renders every `ow-card` severity, chip,
status cell, chart theme, empty state, and button kind. Locks visual
regressions the way `test_design_system.py` locks strings — reviewers see the
system, not just CSS.

---

## Suggested sequencing

| Wave | Items | Why |
|------|-------|-----|
| **Wave D1 — trust & triage** | 9, 12, 13, 19, 40, 47, 49 | Operator-daily; mostly shared helpers |
| **Wave D2 — scan & chrome** | 1, 7, 8, 11, 14, 21, 24, 27, 38, 41, 42 | Extends v4.104; low migration risk |
| **Wave D3 — IA** | 2, 3, 4, 5, 6, 10 | Bigger nav/page splits; do after D1/D2 stabilize |
| **Wave D4 — density & a11y** | 17, 18, 20, 23, 25, 30, 43, 44, 46 | Comfort for Brief/phone + meetings |
| **Later / optional** | 15, 16, 22, 26, 28, 29, 31, 32, 33, 34, 35, 36, 37, 39, 45, 48, 50 | Polish, SiS experiments, Storybook |

## Explicit non-goals (still decline)

- Replacing `lazy_sections` with `st.tabs` (defeats one-section paint).
- Flattening the severity palette (removes scan signal; see 2026-07-31 decline).
- Headless visual snapshot CI in SiS (no reliable headless Streamlit in this env).
- External webfonts (CSP). Prefer a tuned system stack; Inter remains the
  *declared* preference only because SiS often already has it — do not load it.

## Evidence anchors

| Area | Path |
|------|------|
| Tokens / CSS | `app/theme.py` |
| Shared components | `app/ui/components.py` |
| Charts | `app/ui/charts.py` |
| Status palette | `app/ui/status_colors.py` |
| Shell / filters / health | `app/main.py` |
| Prior design adjudication | `docs/reviews/DESIGN_REVIEW_2026-07-31.md` |
| Shipped waves | CHANGELOG `4.103.0`, `4.104.0` |
