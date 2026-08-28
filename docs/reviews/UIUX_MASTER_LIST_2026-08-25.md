# OVERWATCH — UI/UX Polish Master List (2026-08-25)

The attack plan for making OVERWATCH read as one coherent, modern command center
instead of a collection of rich panels.

**Sources.** (1) Codex's 50-item UI/UX review, each rec **ground-truthed against the
actual code** by a review agent; (2) **60 fresh recommendations** from a 5-agent
inspection pass (shell/nav, visual system, tables, charts, workflows). IDs: `C##` =
Codex rec, `F##` = fresh rec — both trace to `file:line`.

**Codex adjudication headline:** of 50, **0 fully-shipped, 26 valid-new, 23
partially-done** (the app already does part — scope shrinks to "what's left"), **1
infeasible** (C41, SiS sandbox). So ~half of Codex's list is *smaller than it reads* —
worth doing, but not from scratch.

**Priority** re-rated by payoff-to-effort (not Codex's): `P0` correctness · `P1` high
visible payoff · `P2` coherence/craft · `P3` nit. **Effort** `S`/`M`/`L`.

---

## ⚠ Fix first — interaction correctness (not cosmetic)

- [x] **F51 · P0/M · Alerts drawer rebinds to the wrong event after a write.**
  The open-events drawer selects by a **positional** `st.dataframe` index; after
  ACK/RESOLVE/SNOOZE the 500-row feed re-reads smaller, every row below shifts up, and
  the same index silently binds the drawer to a *different* event — while the per-event
  note/action widgets still carry the prior text. Track the acted `EVENT_ID`, clear the
  selection after a write, drop stale per-event widget state. `app/ui/pages/alerts.py:443`

---

## Wave 1 — highest payoff-to-effort (do first)

Cheap, high-visibility coherence + a11y wins. Mostly `S`, a few small `M`. No
cross-cutting risk.

### Severity & color legibility
- [ ] **C23 + F19 · P1/M · Data-derive section-header severity (stripe + icon + badge).**
  Hard-coded amber headers ("failures", "MFA gaps", "unused roles") look alarming even
  when clean. Thread each section's already-computed row/finding count into
  `section_header(health=…)`; when amber/red, also tint the icon and count badge (today
  they stay neutral, so severity rests on one thin left border). `app/theme.py:113-122`
- [x] **F13 · P1/S · Neutralize the default KPI stripe; reserve hue for real severity.**
  `st.metric` cards get a teal→blue gradient stripe *by default* that looks semantic but
  isn't; `metric_card_html` gets a neutral slate stripe. A viewer can never learn
  "colored stripe = severity". Make the resting stripe neutral on **both**, tint only on
  real severity, align size/padding. `app/theme.py:59-91`; `app/ui/components.py:280-337`
- [x] **C22 · P1/S · Remove hover elevation from non-interactive cards.** Hover
  box-shadow/border on `.ow-card`/`stMetric` implies clickability no card has. Delete the
  `:hover` rules (and the dead transition); keep hover only if a card becomes a button.
  `app/theme.py:230-244`
- [x] **F16 · P2/S · Reconcile severity hues between tables and charts.** `LOW`/`INFO`
  are slate in `STATUS_COLOR_MAP` but blue/slate in `SEVERITY_HUES` — an INFO event is
  grey in a table and blue in the chart beside it. One hue per severity name, one shared
  map. `app/ui/status_colors.py:48` vs `app/ui/palette.py:49`

### Accessibility
- [x] **F14 · P1/S · Focus-visible rings on every interactive control.** Keyboard focus
  is styled only on the section pill-group and help badge; primary/Execute buttons,
  sidebar nav radios, and scope selects have **no** focus rule. Add one shared
  `:focus-visible` (2px accent, offset). This is a keyboard-heavy DBA tool. `app/theme.py:230-232`
- [x] **C28 · P1/S · KPI help target 18→24px.** Raise `.ow-help` to 24×24, keep the
  focus ring + tooltip anchoring, don't let it push the card title. `app/theme.py:98`
- [x] **F21 · P2/S · Stop the KPI help tooltip clipping off-screen.** 300px tooltip
  anchored `left:0` overflows the right-edge/last-wrapped card (exactly where dense $
  cards sit). Right-anchor or clamp into the viewport. `app/theme.py:104-111`
- [x] **F29-a11y (C29) · P2/S · Mark decorative sparklines `aria-hidden`.** The
  number+delta already convey the trend; add `aria-hidden="true" focusable="false"` to
  `spark_svg`. `app/ui/components.py:241`

### Orientation & chrome
- [x] **F1 · P1/S · Reconcile nav labels with the page titles they open.** Sidebar says
  "Brief" → page H1 says "Morning brief"; "Security" → "Security & Governance"; "Ask" →
  "Ask OVERWATCH". The sidebar label *is* the "you are here" anchor — make click target
  and landing H1 read identically. `app/main.py:75`; `brief.py:73`; `security.py:1379`
- [ ] **C13 · P1/S · Show the filter-contract banner only when it prevents a misread.**
  `filter_contract_text` already computes applies/partial/ignored — render the blue
  banner only when partial/ignored is non-empty; tuck the full "Applies:" contract behind
  a small Evidence popover. `app/ui/components.py:393`
- [x] **C4 + F22 · P2/S · Make the brand dot mean something + give the wordmark a solid
  fallback.** The dot pulses unconditionally (implies liveness it doesn't represent) —
  bind a connected/degraded state class or drop the animation. The gradient text-clip
  wordmark can render transparent on a host that skips `background-clip:text` — set
  `color:var(--ow-ink)` base. `app/theme.py:201-207`; `app/main.py:95-99`
- [ ] **C24 + F31 · P2/S · Compact verified-clean rows + demote the truncation banner.**
  Replace large green success banners with an `.ow-exception--ok` one-liner (collapses the
  Security "green wall"); fold the yellow "showing first N rows" warning into the size
  caption for routine `max_rows` caps. `app/ui/components.py:1020-1024`

### Chart clarity (all `S`, high payoff)
- [x] **F38 · P1/S · Add a color/dash legend to the task DAG.** Nodes are red/blue/
  green/dashed with no key — meaning lives only in per-node hover. Add a static
  bottom-left legend reusing the same palette hexes. `app/ui/charts.py:378`
- [x] **F37 · P1/S · Highlight critical-path EDGES, not just nodes.** The DAG marks
  critical *nodes* but draws every edge grey, so the "longest path" reads as scattered
  boxes with no traceable route. Give edges whose endpoints are both critical an accent
  stroke. `app/ui/charts.py:235`
- [x] **F40 · P2/S · Day-grain tooltips stop showing "12:00:00 AM".** Day charts pass
  `Day:T` with no format → hover reads "August 14, 2026, 12:00:00 AM". Set `%b %d, %Y`.
  `app/ui/charts.py:645`
- [x] **F39 · P2/S · Compact SI dollar axes.** `$,.0f` prints "$1,240,000" per tick;
  switch large-magnitude axes to `$,.3~s` ("$1.24M"), keep tooltip precision to the cent.
  `app/ui/charts.py:738`
- [x] **F47 · P2/S · Force a full 0–23 domain on the hour heatmap.** Zero-activity hours
  are omitted, sliding columns left and shifting the daily shape — defeating an
  hour-of-day view. Pin `scale=domain(range(24))`. `app/ui/charts.py:975`

### Tables (cheap correctness)
- [x] **F28 · P2/S · Byte-column headers contradict their cells.** Cells auto-scale
  (512 MB, 1.2 GB, 3 TB) but the header still appends a fixed "(GB)". Suppress the
  parenthesized unit on auto-humanized byte columns. `app/ui/components.py:1360-1389`
- [x] **F25 · P1/S · Apply `COLUMN_HELP` to caller-configured columns too.** The
  billed/measured/allocated "which dollar is this" help is skipped exactly on the columns
  a page hands an explicit `NumberColumn` — i.e. the ones that need it. Merge help into
  the caller's column instead of `continue`. `app/ui/components.py:1529-1544`

---

## Wave 2 — coherence & craft (mostly `M`)

### Tables — scannability & one grammar
- [ ] **F26 · P1/M · In-cell magnitude bar on the ranked $/credits column.** A cost
  board's #1 scan question is "where's the weight" — today it's a wall of flat numbers.
  Wrap the primary ranked column in a native `ProgressColumn(max=col max)` so the top few
  dominate visibly. `app/ui/components.py:1277-1322`
- [ ] **C35 + F27 · P1/M · Centralize Snowsight links; extend to warehouse/db/table.**
  Auto-attach the query-profile link whenever a non-blank `QUERY_ID` column exists (remove
  per-site wiring); add a sibling "Open in Snowsight ↗" for `WAREHOUSE_NAME`/`DATABASE_NAME`/
  table objects. `app/ui/components.py:651-698`
- [ ] **C34 + F34 · P1/M · Standardize selectable-table affordances.** Add a visible
  row-action cue, a shared "select a row to see detail here" empty-detail placeholder, and
  a `#` rank ordinal on ranked tables (owner's "I don't know the query id to select" pain).
  `app/ui/components.py:1634-1641`
- [ ] **C31 · P2/M · Compact table toolbar.** Lay CSV button + size caption + a
  Summary/Diagnostics/All view toggle (**C32**) into one flex row. `app/ui/components.py:1660`
- [ ] **C37 · P1/M · Reconciliation footers on additive cost tables.** Extend the
  `totals` mechanism into a footer: visible total, expected parent, variance, coverage %;
  a `nonadditive=True` flag suppresses it on lens tables. `app/ui/components.py`
- [ ] **F29 · P2/M · Magnitude-aware $ precision.** Cents only below ~$100, whole dollars
  to ~$10k, `k`/`M` above; CSV keeps raw, credits stay exact. `app/ui/components.py:1296`
- [ ] **F32 · P2/S · Mute true-zero cells** so non-zero values carry the eye on sparse
  operational tables (the table equivalent of the `—` NULL treatment). `app/ui/components.py:1473-1484`
- [ ] **F33 · P2/M · Column-width intent by name convention** (id/status→small,
  preview→large, numerics→small) so wide tables stop laying out raggedly. `app/ui/components.py:1520-1547`
- [ ] **F35 · P3/S · Keep the order/window provenance on small ranked tables** (the
  "by $ desc · last 30d" line is gated behind >10 rows; a 6-row movers table drops it).
  `app/ui/components.py:1634-1640`
- [ ] **F36 · P3/S · State the CSV-is-raw contract** by the download button. `app/ui/components.py:1600-1628`

### Charts — one visual grammar
- [ ] **C40 · P1/M · DAG minimap + fit-to-selection.** Corner viewport overview + a
  button that fits to the failed/critical bounding box. `app/ui/charts.py`
- [ ] **C38 · P2/M · Document a measured/modeled/projected mark grammar** (solid vs
  dashed vs band). Provisional-dimming already ships; make the convention reusable.
- [ ] **F44 · P2/M · Label the stack total above each stacked bar** (incl. the monthly
  "boss chart") so the primary per-period number is legible without hovering segments.
  `app/ui/charts.py:895`
- [ ] **F46 · P2/M · Quadrant guides + region labels on the workload scatter** so a dot's
  *position* states its ACT NOW / PLAN / VALIDATE lane, color merely confirms. `app/ui/charts.py:1200`
- [ ] **F45 · P2/M · Waterfall connector lines + a muted "Other" bar** so it reads as a
  running total, not scattered bars. `app/ui/charts.py:999`
- [ ] **F41 · P2/S · Unit-aware `daily_metric_line` tooltips/peak** ($ line hover shows
  "742389.5" with no `$`; % line shows "93.1" with no `%`). `app/ui/charts.py:1240`
- [ ] **F48 · P3/S · Label the reference rule on `daily_metric_line`** (bare dashed
  vertical with no text). `app/ui/charts.py:1255`
- [ ] **F30 · P2/M · Inline sparkline columns** (`LineChartColumn`) for per-entity daily
  series on movers/top-spend/warehouse grids. `app/ui/components.py:1660-1671`
- [ ] **F42 + F43 · P2/M · Fix sparkline magnitude scaling + polarity color.**
  Auto-scaling makes 99.1→99.4% look as dramatic as a doubling; and the spark is tinted by
  card severity, not trend direction — a cost card trending *up* can draw a calm blue line
  over a red delta. Anchor the domain; color the spark by `delta_color` polarity.
  `app/ui/components.py:241,290`

### Orientation & metric ownership
- [ ] **C17 · P1/M · One data-derived verdict line per analytical page.** The mechanism
  exists (`page_verdict_line`) and Alerts already has the counts; compose a verdict from
  signals Security/Operations/Decision-Studio already compute, above the section bar.
- [ ] **C16 · P2/S · Section count badges from already-loaded values** (overdue on
  Operations, open findings on Security, running experiments on Decision Studio) via the
  existing `counts=` badge.
- [ ] **F9 · P2/M · Section kicker/breadcrumb in the header** ("Analyze ▸ Operations ▸
  Warehouses") so a deep-link landing says where you are above the fold. `app/ui/components.py:177-201`
- [ ] **F3 · P2/M · Stop double-stating scope** — the header chip row repeats the toolbar
  ~20px above; make it a sticky mini-scope that appears only once the toolbar scrolls away.
  `app/ui/components.py:23-38`
- [ ] **C9 · P1/M · One-hop contextual return.** Push an origin tuple (page, section,
  selection) into nav context on each jump; render a single "Return to …" in `page_header`,
  restoring section + drill. `app/core/state.py:89-163`
- [ ] **C15 · P2/S · Show resolved calendar dates** ("Current month → Aug 1–25"). The
  boundary computation already exists and is passed to headers; the render discards it.
- [ ] **C18 · P2/M · "Since your last visit" on Alerts / Action Center / Security /
  Decision Studio**, with direct links to changed items (`since_last_visit` opener already
  ships).
- [ ] **F6 · P2/S · Data-aware ACCOUNT_USAGE lag caption** — it prints on every page
  including ones that read no lagging data, so it stops registering where it matters. Gate
  to metering surfaces or fold into the card `as_of` stamp. `app/ui/components.py:199-201`

### Alerts & workflow interactions
- [ ] **F49 · P1/M · Hoist a "decide bar" to the top of the alert drawer.** The
  ACK/RESOLVE/SNOOZE controls render *last*, below six evidence panels — an operator
  scrolls a long drawer to act on every event. Put the decision controls directly under
  the title; demote playbook/history/AI to a supporting group. `app/ui/pages/alerts.py:459`
- [ ] **C43 · P1/M · Bulk-select alerts in the event table** (`selection_mode='multi-row'`
  → existing `_bulk_lifecycle_sql`/confirm gate), deleting the duplicate multiselect.
- [ ] **C44 · P2/S · Advance to the next open item after ACK/resolve/snooze** + a one-line
  durable receipt ("Resolved X — next: Y"). `app/ui/pages/alerts.py`
- [ ] **F50 · P1/M · Persist the alert re-check verdict + one-click "Resolve as ACTIONED
  with this evidence."** Today the "condition clear" result vanishes on the next rerun.
  `app/ui/pages/alerts.py:503`
- [ ] **F57 · P2/M · Batch the drawer's 4 serial reads into one `run_batch` + named
  progress** on first open. `app/ui/pages/alerts.py:477`
- [ ] **F53 · P2/M · Selection summary + severity quick-select before bulk confirm**
  ("6 selected · 2 CRITICAL, 3 HIGH"). `app/ui/pages/alerts.py:769`
- [ ] **F52 · P2/S · Show the resolved wake time when picking a snooze + a relative
  countdown in the snoozed tray.** `app/ui/pages/alerts.py:715`
- [ ] **C48 · P1/M · App-wide in-flight action state** (disable initiator, "Executing…",
  block duplicate clicks) — extend beyond lifecycle to unguarded remediation writes.
- [ ] **F54 · P2/S · Dirty-check the Action Center "Save work item"** so no-op saves stop
  writing empty audit rows. `app/ui/pages/workbench.py:180`
- [ ] **F58 · P2/S · Plain-English effect line above every write's SQL preview**
  ("Set RUNNING → VERIFIED and book $X to the ledger"). `app/ui/pages/workbench.py:178`

### Empty states & consistency
- [ ] **C25 · P1/M · Finish the shared empty-state conversion** — route `guard()`'s
  empty/error branches + the ~150 remaining raw `st.success`/`st.info` through
  `empty_state`; add an "unavailable" kind.
- [ ] **F56 · P2/M · Give workflow empty states a next-best-action** (Watchlist empty →
  "Browse the catalog"; Experiments empty → create one). `app/ui/pages/workbench.py:287`
- [ ] **F5 · P2/M · Unify the selected/active visual grammar** across nav / section pills /
  status cards (three different "active" languages today). `app/theme.py:166-264`
- [ ] **F17 · P2/S · Equalize KPI card heights in a row** (full-height flex so a row with
  one sparkline doesn't bottom-align raggedly). `app/ui/components.py:335`
- [ ] **F59 · P2/S · One optimistic star affordance for Watch everywhere** (button in
  Entity 360, raw boolean column on boards, Brief badge — three languages today).
  `app/ui/pages/workbench.py:610`
- [ ] **F60 · P3/S · Unify the three confidence encodings** across the decision
  workbenches (chips+caption vs ProgressColumn vs absent). `app/ui/pages/workbench.py:114`
- [ ] **F24 · P2/S · Explicit disabled treatment for gated actions** (a type-to-confirm
  Execute doesn't visibly read as locked). `app/theme.py:230-244`
- [ ] **F19-icon (F20/F23) · P3/S · Icon-weight-by-size + one chip shape language**
  (rectangular trust-chips vs pill everything-else). `app/ui/icons.py:47`; `app/theme.py:80`

---

## Wave 3 — bigger bets (`L` / cross-cutting)

- [ ] **C42 · P1/L · Desktop master-detail layout for Alerts** (feed left, drawer right)
  — wrap the existing `@st.fragment` in `st.columns`, stacked fallback on narrow viewports.
- [ ] **C47 · P1/L · Shared master-detail for Action Center + Decision Studio** (ranked
  work left, ownership/evidence/lifecycle right). Accepts Streamlit's whole-page rerun on
  right-pane edits.
- [ ] **C45 · P1/M · Operator Case File → persistent tray.** Show the item count in the
  shell on every page; extend `add_to_case_button` to Action Center rows, Entity 360,
  recommendations, cost exceptions, Security findings. **F55** adds per-item notes +
  reorder before export.
- [ ] **C46 · P1/M · Watch → visible monitoring tray.** Hoist the already-computed
  `watch_summary` (watched/attention counts + newest movement) into the sidebar/status
  strip on every page.
- [ ] **C19 · P1/L · Operator vs Audit presentation modes** stored as a profile pref (the
  density-toggle precedent). Cost is the *breadth* of gating every `result_caption`/
  methodology/formula block, not the plumbing.
- [ ] **C3 · P1/M · Finish the command palette** — fold the separate Investigate ID-lookup
  into the unified "Jump to" box as a mode, drop the extra Open button (enter-to-go), add a
  recents list. (The cross-page destination search already ships.) `app/main.py:272-372`
- [ ] **C10 · P1/M · "Copy scoped link"** — reuse the existing page/section/filters
  serializer, but ship it as a copyable **view token** (SiS URL round-tripping is
  unreliable) that rehydrates through `_apply_default_landing`. `app/main.py:224-233`
- [ ] **F15 · P2/L · Real light-theme token block, or drop light support.** `theme.py`
  defines only dark tokens, but `status_colors.py` already branches on `_theme_is_light()`
  — under a light host the table cells go pastel-on-white while every card stays dark navy
  and the wordmark nearly vanishes. Add a genuine light palette or neutralize the light
  branches. `app/theme.py:19-41`
- [ ] **C36 · P1/L · Auto entity-drills for identity columns** (warehouse/db/task/object/
  user/role). Buildable as auto-detecting a *primary* identity column → row drill; the
  maximal "every cell is a drill" form is **not** possible in `st.dataframe` (LinkColumn is
  external-URL only). Pair with **F27** (external Snowsight links).
- [ ] **C20 · P2/M · One section order everywhere** (verdict → exceptions → actions → KPIs
  → visuals → table → methodology) via a thin section-render helper adopted by the dense
  Operations/Security/Decision-Studio sub-sections.
- [ ] **C2 · P2/M · Icon-and-text sidebar nav** (swap radios for icon buttons reusing the
  existing page-icon map). Aesthetic; grouping/active-state already work.
- [ ] **C11 · P2/M · Click-through metric ownership** — a small "View on «owner» →"
  affordance under each mirrored KPI (ownership IA already exists in caption prose).

---

## Parking lot — low value, subjective, or infeasible

- **C41 · ✗ INFEASIBLE · DAG ↔ detail-pane bidirectional sync** — graph-click→pane sync
  can't work in SiS's one-way component sandbox. The detail content already ships in the
  adjacent ranked table + node tooltips; the only approximation is a Python selectbox that
  pre-highlights the SVG (not a true click-sync). Consider that instead.
- **C8 · Operations regroup** — lowest value of the regroup set; a 7-pill bar scans fine.
  Skip unless the owner wants the workflow framing. (Same call as the dropped density toggle.)
- **C5 / C6 / C7 · Security / Decision Studio / Admin regroups** — buildable via existing
  `nested_sections`, but subjective IA bets that add a click of depth. Do only if the four
  buckets map cleanly; treat as opt-in.
- **C21 · Flatten card gradients** — the near-flat card gradient is a taste call, not a
  defect; static tables are already solid.
- **C27-uppercase · Remove uppercase micro-labels** — fights the established micro-label
  language. (The *size-floor* half — bump smallest tokens to ~13px — is real; kept as F/nit.)
- **C26 · Shorten copy** — a subjective 3–4 subtitle edit; the structural caveat-relocation
  already happened.
- **C30 · Vertical rhythm** — mostly addressed by C11/C13; residual is a convention pass.
- **P3 chrome nits** (do opportunistically): F7 (group-header weight), F8 (reset-column
  gap), F10 (account/role stamp), F12 (dead `scope_note` path), F18 (button pressed state).

---

## Appendix A — Codex 50, adjudicated

Verdict key: **★** valid-new · **◐** partially-done (some ships) · **✗** infeasible.

| # | V | Rec | Note |
|---|---|-----|------|
| 1 | ★ | Status-card → command rail | Real but only on 2 surfaces, not "every page" → P2 |
| 2 | ★ | Icon+text sidebar nav | Radios have no icons; aesthetic refactor |
| 3 | ◐ | Unify Jump + Investigate | Cross-page search ships; fold Investigate in, add recents |
| 4 | ★ | Meaningful brand dot | Pulses unconditionally; bind state or drop |
| 5 | ★ | Regroup Security → 4 | Subjective IA; buildable via nested_sections |
| 6 | ★ | Regroup Decision Studio → 4 | Subjective IA |
| 7 | ★ | Regroup Admin → 4 | Clean mapping, low value |
| 8 | ★ | Simplify Operations → 5 | Lowest value; skip |
| 9 | ★ | One-hop return | Genuine gap; push origin into nav context |
| 10 | ★ | Copy scoped link | Real; ship as view token (SiS URL unreliable) |
| 11 | ◐ | Explicit metric ownership | IA exists; add click-through on mirrored KPIs |
| 12 | ★ | Section-aware scope toolbar | Real but marginal; lags one rerun |
| 13 | ★ | Filter contract only on misread | Text already computed; banner-gate + popover |
| 14 | ★ | Individually clearable chips | Only the 3 "contains" filters are buried |
| 15 | ◐ | Resolved calendar dates | Boundary computed; render discards it |
| 16 | ★ | Section count badges | Cheap counts on triage sections |
| 17 | ◐ | Data-derived verdict line | Mechanism + Alerts counts exist; compose the rest |
| 18 | ◐ | "Since last visit" everywhere | Opener ships; Security/DS need change-inputs |
| 19 | ★ | Operator / Audit modes | Pref plumbing easy; gating breadth is the cost |
| 20 | ◐ | One section order | De-facto pattern; make it a helper |
| 21 | ◐ | Reduce gradient/shadow | Tables already solid; card gradient is taste |
| 22 | ★ | No hover on non-interactive cards | Correct + cheap |
| 23 | ★ | Data-derived header severity | Per-call `health=` wiring, no new queries |
| 24 | ★ | Verified-clean rows vs banners | Primitive exists (`.ow-exception--ok`) |
| 25 | ◐ | Finish empty-state conversion | Component shipped, adoption stalled |
| 26 | ◐ | Shorten copy | Mostly done; subjective residue |
| 27 | ★ | Microtext floor / less uppercase | Size floor real; uppercase-removal subjective |
| 28 | ★ | KPI help 18→24px | Trivial |
| 29 | ★ | Card/spark semantics | Small a11y gap; aria-hidden the spark |
| 30 | ◐ | Vertical rhythm | Partly overstated; convention pass |
| 31 | ◐ | Compact table toolbar | Minor consolidation into `_render_table` |
| 32 | ★ | Summary/Diagnostics/All views | Genuinely unbuilt; `column_sets` param |
| 33 | ◐ | Pin identity+status columns | First-col pin ships; co-pin status |
| 34 | ◐ | Selectable-table affordances | Selection ships; empty-detail + cue are the gap |
| 35 | ◐ | Auto Snowsight links | Helper ships; centralize into table layer |
| 36 | ◐ | Auto entity drills | Primary-col drill OK; "every cell" infeasible in st.dataframe |
| 37 | ◐ | Reconciliation footers | Math present; package as a footer |
| 38 | ◐ | Measured/modeled grammar | Provisional/dashed ship; document convention |
| 39 | ◐ | Standardize chart reading | Provisional+takeaways ship; add nearest-x + labels |
| 40 | ★ | DAG minimap + fit-to-selection | Both absent, feasible client-side |
| 41 | ✗ | DAG ↔ pane sync | Infeasible (one-way sandbox); use selectbox approximation |
| 42 | ★ | Alerts master-detail | Drawer is stacked; wrap in columns |
| 43 | ★ | Bulk-select in event table | multi-row selection → existing bulk path |
| 44 | ★ | Advance to next item | Toast is partial; add queue-advance + receipt |
| 45 | ★ | Case File persistent tray | Both halves unbuilt + buildable |
| 46 | ◐ | Watch monitoring tray | Logic exists; needs shell placement |
| 47 | ◐ | Shared master-detail | Pattern shared; only horizontal layout is new |
| 48 | ◐ | In-flight action state | Double-write blocked; add the affordance |
| 49 | ◐ | Consequential-action shell | `confirm_gate` ships; receipt is net-new |
| 50 | ◐ | Cold-paint loading | Named progress on 2 sections; stale-while-revalidate infeasible |

## Appendix B — Fresh 60 (F1–F60)

Grouped by domain; see the item bodies in Waves 1–3 and the parking lot. Full rationale
with `file:line` for each is preserved in the review workflow output
(`tasks/wx8pu34ds.output`). Domains: **shell/chrome** F1–F12 · **visual system**
F13–F24 · **tables** F25–F36 · **charts** F37–F48 · **workflows** F49–F60.

---

*Generated 2026-08-25 from Codex's 50-rec review (ground-truthed) + a 10-agent
inspection pass (5 adjudicators + 5 generators), at `origin/main` v4.302.0.*
