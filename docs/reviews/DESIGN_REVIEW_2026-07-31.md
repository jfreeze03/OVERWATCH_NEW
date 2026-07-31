# Codex visual/design review — assessment (2026-07-31, v4.102)

Verification of the 20 Codex visual/design/presentation recommendations against the
**current** code, via a find→verify→synthesize workflow (4 group-verifiers + a
gap-finder + synthesis). Each claim was checked at its cited line and cross-checked
against the recent design work (v4.95 rec13 card tokens, v4.96 rec15 a11y, v4.99
rec14 grouped nav, v4.101 review-round-4).

## Verdict

**7 CONFIRM · 7 PARTIAL · 6 DECLINE.** The review is accurate — nearly every factual
claim checks out — but ~half is already (partly) satisfied by v4.65–4.101 work, so
the honest remaining effort is smaller than the raw list implies. **The highest-leverage
payload is what Codex missed:** the color system is centralized in tokens yet not
enforced at the edges (added recs A1–A3).

## Cross-cutting themes

1. **No shared component vocabulary.** The same ACTION_QUEUE / finding / table data is
   rendered 4+ inconsistent ways (bulleted, dead read-only table, click-to-navigate,
   click-to-drawer); headings use 3 competing mechanisms. Every page hand-rolls its own
   presentation (recs 6, 10, 13).
2. **Provenance & chrome noise.** Scope, the ACCOUNT_USAGE lag note, and the OVERWATCH
   kicker are restated across the shell and on every panel caption — scope prints up to
   4×, the lag note verbatim on all ~76 `result_caption` calls (recs 1, 5, 11).
3. **Above-the-fold contract violations.** Pages promise "numbers first, fires second"
   and "the work that needs an owner," then push an expanded AI narrative and full-height
   charts above the actionable list (recs 2, 4).
4. **Color is tokenized but not enforced at the edges.** Charts, the sidebar strip, and
   delta columns diverge from the token palette; the severity ramp isn't colorblind-safe;
   severity/movement are hue-only (added A1/A2/A3/A5). Rec 17 confirms the *core* system
   is fine — so the fix is drift-enforcement, not flattening.
5. **Charts & exports under-state their own conclusion.** The flagship boss chart is
   unreadable at 9 colors; ~13 charts render bare with no takeaway; the downloadable exec
   summary has no trend and no print stylesheet (recs 15, 16, 20).

## Per-rec disposition

| # | Rec | Verdict | P/E | Already done |
|---|-----|---------|-----|--------------|
| 1 | Collapse repeated global chrome | CONFIRMED | P2/S | v4.65 dropped the top-bar chip band; header caption still folds scope |
| 2 | Brief: numbers/fires/asks before AI narrative | **CONFIRMED** | P1/S | nothing — narrative still first |
| 3 | Cap Brief at 5 headline metrics | DECLINE | — | v4.101 kpi_row 7→4+3 fixed the only real downside; extras are conditional signal |
| 4 | Hoist Top actions above Overview charts | **CONFIRMED** | P1/M | v4.101 only restyled headers, didn't move it |
| 5 | Breadcrumbs instead of repeated kicker | PARTIAL | P2/S | untouched |
| 6 | Strict heading hierarchy | PARTIAL | P2/M | v4.101 did the two Overview charts; rest still mixed |
| 7 | Sidebar look like nav, not a form | PARTIAL | P2/S | v4.99 grouped nav + active rail already; radio dot + brand-block remain |
| 8 | Section nav → select/overflow | DECLINE | — | v4.96 flex-wrap keeps every pill visible; a dropdown *hides* options (regression) |
| 9 | Alert drawer → modal/master-detail | DECLINE | — | drawer is a dense fragment-scoped workflow; `st.dialog` would cram it |
| 10 | One shared "finding row" | **CONFIRMED** | P1/M | nothing — each surface hand-rolls |
| 11 | Progressive-disclose provenance | PARTIAL | P2/S | scope line landed; lag note still on all ~76 captions |
| 12 | 3 chips → one prose line | DECLINE | — | directly undoes intentional v4.95/4.101 token split |
| 13 | Central table schemas | PARTIAL | P2/M | `_render_table` auto-formats/colors already; add a display prettifier + config for the few tables that need units |
| 14 | Self-identifying exports/toolbar | **CONFIRMED** | P1/S | untouched — `overwatch_table_{seq}.csv` |
| 15 | Charts lead with conclusion | PARTIAL | P2/M | only `spend_trend` has a takeaway |
| 16 | Simplify the boss chart | **CONFIRMED** | P1/M | r4 gave it stable hues; still 9 colors, no movers table |
| 17 | Flatten palette | DECLINE | — | token-driven + severity-semantic stripes; flattening removes scan signal |
| 18 | Raise typography floor | PARTIAL | P1/S | v4.96 fixed the a11y/contrast half; sizes still 10.6–11.5px |
| 19 | Responsive ladder + snapshot tests | DECLINE | — | over-engineers a desktop-first SiS tool; can't run headless snapshots in CI |
| 20 | Executive presentation/print export | **CONFIRMED** | P1/M | untouched — export is chart-less/print-less |

## Added recs (what Codex missed — highest leverage)

- **A1 — Unify the divergent traffic-light palettes (P1/S).** A DBA crossing Alerts →
  Control Room → sidebar sees CRITICAL as different reds (`#fb7185` / `#ef4444`) and
  healthy as two greens (`#34d399` / `#22c55e`). Promote one severity/health palette to
  the token layer and have charts + status cells + the sidebar strip read it.
- **A2 — Colorblind-safe severity ramp + redundant encoding (P1/M).** The ramp separates
  CRITICAL/HIGH/MEDIUM almost entirely on the red→yellow axis protanopia/deuteranopia
  compress, and severity is color-only on the event timeline dots, KPI stripes, and
  sidebar. Add a shape/icon or text token so severity never rests on hue alone.
- **A3 — Style the movement/delta columns (P1/M).** The primary scan targets of a cost
  command center render as flat monochrome signed numbers — a +$40k jump and a −$40k drop
  differ only by a minus sign (e.g. `control_room.py:546 DELTA_USD`). Color + arrow them
  through the shared status vocabulary.
- **A4 — Green "all-clear" empty states (P2/S).** `guard()` renders neutral blue `st.info`
  on empty, so the flagship "am I on fire" all-clear (no open alerts) reads as a notice to
  stop and read. Give the genuinely-good empty states a green treatment.
- **A5 — Centralize Altair chart conventions (P2/S).** Legend orientation flips top vs
  bottom against the theme default; the dollar y-axis is spelled three ways; day-grain
  tooltips vary. One `_base`/axis/legend convention. Bundle with A1 + rec 15.

## Recommended sequencing — one "scannability wave"

**DO-FIRST** (highest impact-per-effort; almost all S/M):

1. **rec 14 — self-identifying CSV exports** (P1/S). `_render_table`: `overwatch_{page}_{seq}_{YYYYMMDD}.csv` using the existing `_ow_dl_page` key; thread an optional `slug=` through `styled_table`/`selectable_table` for the flagship tables.
2. **rec 2 — reorder the Brief** (P1/S). Move the digest expander below the Asks section; `expanded=False`. Restores the page's own "numbers, fires, asks" docstring contract.
3. **rec 18 — bump micro-label sizes** (P1/S). theme.py only: `stMetricLabel`/`.ow-card__title` 0.72→~0.76rem, `.ow-stat__k` 0.66→~0.72rem. Leave `.ow-help` (a11y already done — drop the "real popover" half as moot).
4. **A1 — unify the traffic-light palette** (P1/S). One token palette read by charts + status + sidebar.
5. **rec 10 — unify the action/finding surface** (P1/M). Overview "Top actions" `styled_table`→`selectable_table` with `request_navigation` (matching Control Room triage, killing the dead read-only wall); Brief Fires → the same bulleted line as Asks; extract `action_list()`/`finding_line()` helpers.
6. **rec 4 — hoist Top actions above the Overview charts** (P1/M). Move the Top-actions/Top-drivers block above the boss chart (keep the boss chart prominent per the owner ask); re-verify the export after the move.
7. **rec 16 — readable boss chart + movers table** (P1/M). `top_n` 8→5 so the stack holds ≤6 colors; add a companion "top movers MoM" table (Δ$ / Δ% per warehouse, sorted by |Δ$|).
8. **rec 20 — exec-summary trend + print CSS** (P1/M). Embed the pure `spark_svg` in the window-spend card; add `@media print{@page{margin:14mm} .card,tr,ul{break-inside:avoid}}`; extend the self-contained-export tests.

**NEXT:** recs 1, 5, 7 (radio-dot CSS), 11 (lag note once per page), 6 (targeted heading pass), 15 (flagship-chart takeaways), 13 (display prettifier); added A2, A3, A5.

**DECLINE/DEFER:** rec 3 (fixed by kpi_row rebalance), rec 8 (dropdown hides options — regression), rec 9 (drawer already a dense fragment workflow; a modal is worse), rec 12 (undoes the intentional token split), rec 17 (flattening removes severity-scan signal), rec 19 (over-engineers a desktop-first SiS tool).

Every DO-FIRST item is app-only, no migration, and self-contained — a single wave lands
the scannability gains without disturbing the deliberate recent design decisions.
