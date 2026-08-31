# OVERWATCH — 50 visual recommendations (2026-08-31)

Grounded against **v4.365.0** (`APP_VERSION` in `app/config.py`). This list is
**additive** to `docs/reviews/UIUX_MASTER_LIST_2026-08-25.md`. Most of that
Wave 1–2 work already shipped (palette, verdict line, empty-state vocabulary,
active-state grammar, compact clean rows, operator/audit mode). These 50 are
what still reads as unfinished, noisy, or two-languages-at-once **on screen**.

Scope: look, layout, visuals, wording. No data-correctness items. Each rec
names a current file:line so it can be verified, not asserted.

Priority: **P1** = high visible payoff, **P2** = coherence/craft, **P3** = polish.
Effort: **S** / **M** / **L**.

---

## A. Above-the-fold chrome (recs 1–10)

The first screenful on a drill page is mostly chrome. A typical Cost / Operations
landing paints, in order: scope toolbar → breadcrumb → H1 + icon → subtitle →
scope chips → ACCOUNT_USAGE lag caption → verdict → since-last-visit → filter
contract → section pills → hairline. Content starts around the fold.

### 1. Collapse the page-header stack into one band — P1 / M
`page_header` (`app/ui/components.py:226`) emits breadcrumb, title, subtitle,
scope chips, and the lag note as five separate Streamlit elements. Each one
adds a `gap` from `.main .block-container > div { gap:0.55rem }`
(`app/theme.py:51`). Merge into a single HTML block: kicker + title on one
row, subtitle + chips on the next, lag note only as a trailing muted clause.

### 2. Stop restating scope three times — P1 / S
Still-open **F3**. The toolbar already shows Company / Window / Database
(`app/main.py:611`). `_scope_chip_html` reprints them 20px below
(`app/ui/components.py:24`). The filter-contract line then names them a third
time. Drop the header chip row, or make it a sticky mini-scope that appears
only after the toolbar scrolls away.

### 3. Give the scope strip visible field labels — P1 / S
Company, Date range, and Database use `label_visibility="collapsed"`
(`app/main.py:637-668`). The strip is three unlabeled dropdowns plus a "Scope
/ Account view" kicker. On a fresh session it is not obvious which control is
which. Show the Streamlit labels (or a 10px uppercase micro-label above each
select) and drop the decorative "Scope" column.

### 4. Don't leave a hole where Reset used to be — P2 / S
Reset renders only when a filter is active (`app/main.py:687`). The six-column
row then has an empty last cell and the More button sits off-center. Always
render Reset, disabled + dashed (the F24 locked treatment already exists) when
there is nothing to clear.

### 5. Put compact density back on a control the viewer can see — P1 / S
`_COMPACT_CSS` still injects when `_ow_density == "compact"` (`app/theme.py:451`),
and USER_PREFS still hydrates it (`app/main.py:257`). The in-strip editor was
removed in v4.157 ("nobody uses them"). A pref with no affordance is a ghost
feature. Restore a single sidebar toggle next to Audit detail, or delete the
dead CSS/pref path so the density language is honest.

### 6. Show that Audit detail is on — P2 / S
The sidebar toggle (`app/main.py:197`) is the only cue. In audit mode the page
gains fetched-at stamps and methodology captions with no page-level marker, so
two viewers comparing screenshots cannot tell why one is noisier. Add a small
"Audit" chip on the header band when `_ow_present_mode == "audit"`.

### 7. Fold the lag note into the `as_of` stamp — P2 / S
`ACCOUNT_USAGE_LAG_NOTE` is a full caption on every metering page
(`app/config.py:119`, printed at `app/ui/components.py:269`). It is hedging
jargon the eye learns to skip. KPI cards already have an `as_of` line
(`metric_card_html`, `app/ui/components.py:434`). Put "as of 10:14 · metering
can lag 24h" on the card that needs it; drop the page-level sentence.

### 8. Make the Overview status strip itself the jump target — P2 / M
`_persistent_status_bar` paints four `.ow-stat` cards, then a second row of
"Open …" tertiary buttons (`app/ui/components.py:746`). That is eight chrome
objects for four signals, on the one page that already has a verdict and KPIs.
Make each stat a single clickable card (or drop the button row and keep
click-on-card only).

### 9. Sidebar group captions need weight — P2 / S
Nav groups are bare `st.caption("Watch")` (`app/main.py:142`). They read as
disabled helper text, not section headers. A 0.68rem uppercase kicker with a
hairline — the same grammar as `.ow-breadcrumb` — would make Watch / Analyze /
Govern scan as groups.

### 10. The Jump box needs a heading and shorter recents — P2 / S
The select is `label_visibility="collapsed"` with placeholder "Jump to…"
(`app/main.py:374`). Recent destinations render as full-width tertiary buttons
with labels like `Section · Cost & Contract → Spend & Attribution`, which wrap
into two ugly lines. Caption the box "Jump to", and truncate recents to the
leaf (`Spend & Attribution`) with the kind in a muted prefix.

---

## B. Type, color, and icon language (recs 11–20)

The design tokens are real and drift-tested. The leftovers are mixed alphabets
and a typeface the app does not actually load.

### 11. Inter is declared but never loaded — P1 / S
`--ow-font` leads with `'Inter var','Inter'` (`app/theme.py:42`). SiS CSP
forbids a Google Fonts `@import`, and there is no self-hosted woff under
`app/assets/`. Every machine falls through to SF Pro / system UI. Either
bundle a woff (CSP-safe) or drop Inter from the stack so the declared
typeface is the one on screen.

### 12. Finish replacing emoji — P1 / S
`app/ui/icons.py` exists because "emoji render inconsistently across
platforms." Residuals:

| Site | Glyph | File |
|---|---|---|
| `notify` toast | ✅ / ⚠️ | `app/ui/components.py:2638` |
| CSV / export | ⬇ | `app/ui/components.py:667, 2015` |
| `panel_help` | ⓘ | `app/ui/components.py:97` |
| Undelivered criticals | ⚠ | `app/ui/pages/control_room.py:431` |
| Watch | ★ / ☆ | `app/ui/components.py:2188` |
| Table totals | Σ | `app/ui/components.py:1967` |
| Alerts receipt | ✅ | `app/ui/pages/alerts.py:435` |

Route all of these through `icon()` or Material glyphs. One alphabet.

### 13. Stop mixing Material icons with the Feather set — P2 / S
The status-bar jump uses `icon=":material/arrow_forward:"`
(`app/ui/components.py:763`). Every other glyph is a 24×24 stroke SVG from
`icons.py`. Pick one. If Material is the SiS-native path, adopt it for page
headers too; if Feather stays, drop the Material one-off.

### 14. Purple method chips and lime chart series are off-palette — P2 / S
`.ow-src-badge--method` is `#c084fc` (`app/theme.py:111`). The Altair category
range includes `#c084fc` and `#a3e635` (`app/ui/charts.py:112`). Neither is an
`--ow-*` token; `tests/test_palette_drift.py` does not cover them. Add tokens
(`--ow-method`, `--ow-chart-4`) or recolor to accent2 / warn so a method chip
does not introduce a fifth brand hue.

### 15. Ship a real light theme, or delete the light branches — P1 / M
Still-open **F15**. `theme.py` tokens are dark-only. `status_colors.py:35`
already swaps table cells to pastel-on-white when `st.context.theme` is light.
Under a light host, cells go light and every `.ow-card` / wordmark stays navy
— the wordmark nearly vanishes. Either a full `:root` light block (bg / surface
/ ink / hairline / dim tints) or neutralize `_theme_is_light()` so a light host
keeps the designed dark app.

### 16. Compact density only shrinks a few paddings — P2 / S
`_COMPACT_CSS` (`app/theme.py:436`) tightens container gap, metric padding, and
card padding. It does not touch `.ow-section`, `.ow-verdict`, `.ow-statusbar`,
chart heights (`sizing.py`), or pill-group padding. A "compact" viewer still
gets the same chrome stack with slightly squashed cards. Either extend the
overrides to the whole token scale, or drop the mode (see rec 5).

### 17. All-caps micro-labels at 0.76rem are the floor, not a style — P2 / S
`.ow-card__title`, `.ow-stat__k`, and `stMetricLabel` are 0.76rem / 0.72rem
uppercase (`app/theme.py:76, 113, 207`). The a11y comment on `--ow-ink-mute`
already flags 0.62–0.70rem. Uppercase plus tracking plus mute is the hardest
combination to read. Sentence-case the labels ("Open criticals", "Mtd credit
spend") and keep the size; the hierarchy still holds.

### 18. Breadcrumb at 0.68rem uppercase is decoration, not orientation — P2 / S
`.ow-breadcrumb` (`app/theme.py:61`) is smaller than the mute-label floor the
theme itself calls out. "ANALYZE ▸ OPERATIONS ▸ WAREHOUSES" is the one thing a
deep-link landing needs to be readable. 0.78rem, weight 650, no tracking — or
fold it into the H1 as a quieter prefix.

### 19. The brand mark is a pulsing dot, not the favicon — P3 / S
The browser tab uses `app/assets/favicon.png` (`app/main.py:13`). The sidebar
wordmark is a CSS pulse-dot + gradient text (`app/theme.py:242`). They do not
look like the same product. Put the radar-mark PNG (or an inline SVG of it)
next to the wordmark and retire the decorative dot, whose liveness meaning is
already subtle.

### 20. Primary buttons and selected pills share one gradient — P2 / S
Selected section pills (`app/theme.py:275`) and primary buttons
(`app/theme.py:348`) both fill `linear-gradient(180deg, var(--ow-accent2),
var(--ow-accent))` with `#0f172a` ink. A "2 open critical(s)" primary and the
active "Open events" pill are the same object. Keep the fill for pills; give
primary a solid accent (no gradient) or a bad/warn fill when the action is
consequential.

---

## C. Page layout and information architecture (recs 21–32)

### 21. Shorten section-pill labels so the bar stays one row — P1 / S
Pills wrap (`flex-wrap:wrap`, `app/theme.py:264`). Current long names:

- Cost: "Spend & Attribution", "Contract & Forecast", "Optimization & Savings"
- Control Room: "Incidents & triage", "Timeline & movers", "Freshness & replay"
- Admin: "Migrations & freshness", "Errors & telemetry"
- Security: nine pills including "Least privilege" and "Trust Center"

A two-row pill bar plus the header stack pushes the first KPI below the fold
on a 14" laptop. Prefer one word where it is unambiguous ("Spend", "Contract",
"Optimize", "Incidents", "Freshness").

### 22. Nested pill rows need a quieter inner grammar — P1 / S
Cost ▸ Optimize opens a second `lazy_sections` ("Idle & sizing" / "Queries &
patterns" / …) at `app/ui/pages/cost_parts/optimize.py:271` with
`deep_link=False`. Two identical filled-pill tracks stacked is "which bar am I
in?". Style the nested row as underline-tabs (the F5 tab grammar already
exists) or a smaller muted segmented control.

### 23. Decision Studio still has eight peer pills — P2 / S
`["Scorecard", "ROI", "Portfolio", "SLOs", "Products", "Cost Truth",
"Scenarios", "Experiments"]` (`app/ui/pages/decision_studio.py:47`). The Aug 25
parking lot declined a 4-bucket regroup; the visual problem remains. Group into
two rows with a kicker ("Prove" / "Plan") or nest Scorecard+ROI+SLOs under
Prove and the rest under Plan.

### 24. Security is nine pills and a disclaimer paragraph — P2 / M
Nine sections (`app/ui/pages/security.py:1421`) plus a three-sentence RBAC
disclaimer (`:1414`) plus the filter contract. The disclaimer is load-bearing
but it is the third caveat after the subtitle ("not a threat-detection SOC")
and the contract. Move the RBAC sentence into `panel_help` on Decision queue;
cut the page-level paragraph.

### 25. Operations subtitle is a comma-separated inventory — P2 / S
"Queries, tasks, warehouses, contention, change impact, releases, pipeline
SLAs, and emergency levers." (`app/ui/pages/operations.py:2478`). The pills
already name those things. Subtitle should be the job: "What is running, what
is waiting, what to change." Same treatment for Admin (`app/ui/pages/admin.py:1333`).

### 26. Replace raw `st.markdown("**…**")` panel titles with `section_header` — P1 / M
~80 in-page headings are bold markdown, not `.ow-section`. Worst clusters:
`cost_parts/optimize.py` (Idle warehouse advisor, QAS, Storage waste, …),
`admin.py` (Source freshness, Access self-check, Fire-drill scoreboard, …),
`alerts.py` (Rule precision, Alert fatigue, …), `workbench.py` (Evidence graph,
Blast radius, …). Two heading languages on one scroll. A thin
`section_header(title, health="", icon_name="")` — or a new `panel_title()`
without the stripe — would make every block scan the same.

### 27. `st.divider()` is the leftover rhythm — P2 / S
~40 bare `st.divider()` calls (Cost, Optimize, Admin, Security, Operations).
The designed section header already has a border. Dividers between two
`section_header`s double-rule the page. Delete the divider when the next
widget is a section header; keep it only between two untitled tables.

### 28. Widen the canvas on large monitors — P2 / S
`.block-container { max-width:1360px }` (`app/theme.py:50`) with
`layout="wide"`. A 12-column movers table and the Alerts master-detail both
want more than 1360 once the sidebar is subtracted. Raise to ~1600, or drop
the cap and let Streamlit's wide layout use the display.

### 29. Brief should look like a different product surface — P1 / M
Brief is specified as "one phone-friendly scroll" (`app/ui/pages/brief.py:1`)
but it reuses `kpi_row`, `page_verdict_line`, `contract_runway_bar`, and
`panel_help` at desktop density. On a phone the chip-heavy KPI cards wrap into
a tall stack and the help popover is a third of the screen. A Brief-only
scale: 1.9rem values, no method/scope chips, no "ⓘ about this panel", runway
bar first.

### 30. Ask still says "Ask OVERWATCH" in the H1 — P1 / S
F1 reconciled Brief and Security with their nav labels. Ask was missed:
sidebar/nav is `"Ask"` (`app/config.py:155`); `page_header("Ask OVERWATCH", …)`
at `app/ui/pages/ask.py:108`, and no `icon_name`. Match the nav label; put
"Ask OVERWATCH" in the subtitle. Add `icon_name="search"`.

### 31. The Ask test-gallery looks like a QA harness — P1 / M
Every registered example plus a refusal probe renders as a 3-column wall of
stretch `st.button`s (`app/ui/pages/ask.py:132`). That is the right tool for
the author and the wrong first paint for an operator. Hide the gallery behind
"Try an example" (closed), lead with the text input, and show three chips max.

### 32. Connection-error is an unstyled dead end — P2 / S
`app/main.py:732` is `st.title` + `st.error` + a markdown bullet list. It does
not use the brand block, verdict, or empty-state vocabulary. Render the
sidebar brand, then `empty_state("unavailable", …)` with the Retry button as
`action_label`. Same product, even when Snowflake is down.

---

## D. Cards, tables, charts, empties (recs 33–42)

### 33. KPI cards are a chip pile — P1 / M
A flagship $ card can carry: uppercase label, `?` help, freshness badge,
method badge, scope badge, 1.55rem value, credits sub-line, `as of` line,
delta + arrow, sparkline (`app/ui/components.py:383`). On a 4-across row the
chips wrap onto the value. Keep label + value + delta on the card; move
method/scope/as-of into the `?` tooltip (or show them only in audit mode).

### 34. One null glyph everywhere — P2 / S
Tables use `—` (`app/ui/components.py:1876`). KPIs use `—` as the default
value (`:389`). Ask/alerts formatters return `"n/a"`
(`app/ui/pages/alerts.py:65`). Scenario KPIs now say `"Unpriced"` (v4.365).
Three languages for "no number". Reserve `—` for missing, `n/a` for
not-applicable, and keep "Unpriced" only for the priced-vs-unpriced distinction
— and document that in the table caption once, not per cell.

### 35. Table headers still Title-Case the SQL — P2 / S
`_prettify_header` (`app/ui/components.py:1595`) turns `P95_ELAPSED_SEC` into
"P95 Elapsed" and `EST_USD` into "Est Usd" unless the caller passed a
`column_config`. Pages that care (Overview movers, Security grants) hand-label;
pages that don't show warehouse-SQL English. Add a small override map for the
common leftovers (`EST_USD` → "Est. $", `TOKEN_Z` → "Token z") so the default
is never "Est Usd".

### 36. CSV download under every table is visual noise — P2 / S
Still-open **C31**. `_render_table` emits a tertiary "⬇ CSV" after every frame
of ≥4 rows (`app/ui/components.py:1991`). A Cost ▸ Optimize section with six
tables is six download buttons. One page-level "Export visible tables" in the
header, or a single toolbar row (CSV + row count + sort provenance) per table.

### 37. Status fills turn a triage table into a candy stripe — P2 / M
`status_css` paints a full-cell background + 600-weight text
(`app/ui/status_colors.py:111`). A 20-row Alerts feed is a stack of red / amber
/ blue blocks; the title column is the only rest. Prefer a leading 8px stripe
(the card grammar) or a pill inside the cell, and leave the row background
alone. Color-blind / print-friendly bonus: pair the stripe with the existing
text.

### 38. TRUE is amber — P3 / S
`STATUS_COLOR_MAP["TRUE"] = _WARN` (`app/ui/status_colors.py:60`). A boolean
column that means "flagged" is correctly amber; a boolean that means "enabled"
is inverted via `_TRUE_IS_GOOD`. Any other TRUE (watched, candidate) reads as
a warning. Default TRUE to `_INFO` (in-motion), not `_WARN`, and keep the
explicit invert list.

### 39. Chart empties bypass the empty-state vocabulary — P2 / S
`_empty_note` is `st.caption("No plottable rows for this window.")`
(`app/ui/charts.py:141`). House rule 8 says absence goes through
`empty_state`. A coercively-empty chart should be `empty_state("no_data_yet",
…)` so it matches a coercively-empty table.

### 40. `needs_setup` still looks like a raw Streamlit info box — P2 / S
`empty_state("needs_setup")` calls `st.info(message)`
(`app/ui/components.py:1297`). `clean` got a designed `.ow-exception--ok` row;
`needs_setup` and `unavailable` still use native banners. Give needs_setup the
`.ow-verdict--info` treatment and unavailable the `.ow-verdict--bad`
treatment so all four kinds share one silhouette.

### 41. Charts are a fixed 264px under a tall chrome stack — P2 / S
`CHART_H_MD = 264` (`app/ui/sizing.py:22`) was chosen when the header was
shorter. On a 900px-tall laptop the Overview spend chart is a postage stamp
below the fold. Offer a `CHART_H_PAGE` (~360) for the one "boss" chart per
section (daily spend, burndown, heatmap) and keep 264 for secondaries.

### 42. The hour heatmap's orange ramp fights the brand — P3 / S
`_HEATMAP_RANGE` is brown→amber→gold (`app/ui/charts.py:91`) so "hot" reads
intuitively. Next to teal/blue spend charts on the same Operations page it
looks like a second product. A single-hue ramp on `--ow-accent` (deep navy →
sky) keeps "more = brighter" without introducing a heat language the rest of
the app does not speak. Keep orange only if the page is literally about
contention/hotspots and say so in the caption.

---

## E. Wording, voice, and operator English (recs 43–50)

### 43. Filter-contract copy is engineer English — P1 / M
`filter_contract_text` (`app/ui/components.py:470`) emits:

> Applies: Company · Window | Panel-dependent: Database · Schema | Active but
> ignored: Warehouse

An operator needs: **"Warehouse filter is on, but this section ignores it."**
Keep the full contract in audit mode / a popover; make the banner one
sentence in operator English. The C13 "only show when it prevents a misread"
gate is correct; the sentence it shows is not.

### 44. Verdict healthy-copy is uneven — P2 / S
- Alerts: "the alert queue is empty" (`alerts.py:1258`) — good.
- Control Room: "no open criticals, delivery clear, telemetry fresh" — good.
- Cost: "contract on track at the current burn — open a section for detail"
  (`cost.py:137`) — the second clause is a UI hint, not a verdict.
- Brief: "no open criticals or incidents, and contract runway is comfortable"
  (`brief.py:270`) — "comfortable" is vague next to a runway bar that has a
  number.

Drop UI hints from verdicts. Prefer a measured healthy line ("runway 11 months
at current burn") when the number is already in hand.

### 45. Page subtitles should say the job, not the inventory or the caveat — P2 / S
Current mix:

| Page | Subtitle | Problem |
|---|---|---|
| Overview | "Spend, risk, and the work that needs an owner." | Good. |
| Security | "Hygiene and governance posture — not a threat-detection SOC." | Opens with a negation. |
| Alerts | "Open events, lifecycle with audit, and the rules that raise them." | Inventory. |
| Ask | long honesty paragraph | Belongs under the input, not in the H1 caption. |
| Brief | "Your morning one-scroll: numbers first, fires second, asks third." | Good, a bit cute. |

Rewrite Security as "Who has access, what left the account, what to revoke."
Put the SOC caveat in `panel_help`. Cut Ask's subtitle to "Grounded answers
from live query output — never a guess."

### 46. Result captions still say warehouse object names — P2 / S
Operator mode already trims fetched-at (`result_caption`,
`app/ui/components.py:795`) but still prints
`Source: MART_TAG_COVERAGE_DAILY (mart, loaded hourly)`. That is a table name,
not a source kind. Operator: "Mart · hourly". Audit: the object name. The
source *kind* (mart / live / stale) is already on the KPI chip; the caption
should match that vocabulary.

### 47. Type-to-confirm looks like a normal text box — P1 / S
`confirm_gate` (`app/ui/components.py:1255`) is a default `st.text_input` plus
a locked button. The F24 dashed-disabled treatment helps the button; the input
does not say "this writes the account." Wrap the pair in a `.ow-danger` card
(bad-dim fill, "Type RESOLVE to execute") so a classifying write is visually
a different class of control from "Save work item".

### 48. SQL previews are a raw `st.code` dump — P2 / S
Write sites show the statement as unstyled code. F58 already added a
plain-English effect line on some workbench writes. Give every preview a
shared `sql_preview(effect, sql)` : effect sentence, then a collapsed
"Show SQL" expander. The default view is the sentence; the statement is one
click behind, not a 20-line wall above Execute.

### 49. Admin headings are questions to the author, not the operator — P2 / S
"Mart reconciliation — do the numbers MATCH the source?"
"Fire-drill scoreboard — does the page reach a human?"
(`app/ui/pages/admin.py:1144, 1174`). The ALL-CAPS MATCH and the rhetorical
question are review-doc voice leaking onto the page. Prefer "Mart vs source"
and "Delivery fire-drill (did the page arrive?)".

### 50. Write a one-line voice rule and apply it to the next subtitle you touch — P2 / S
The app currently mixes three voices: operator-direct ("what broke, what's
burning"), hedge-caveat ("not a threat-detection SOC"), and review-doc
("do the numbers MATCH"). Add a three-bullet voice note at the top of
`app/ui/components.py` (or a 10-line `docs/design/VOICE.md`):

1. Lead with the job, not the caveat.
2. Prefer a measured number over an adjective ("11 months left", not
   "comfortable").
3. Caveats live in `panel_help` / audit mode, not in the H1 caption.

Then apply it the next time any subtitle, verdict, or filter-contract string
is edited. No mass rewrite.

---

## Already on the Aug 25 list (not re-counted)

These are still open and still worth doing; they are **not** part of the 50
above so the lists stay disjoint:

| ID | Item |
|---|---|
| C31 | Compact table toolbar (CSV + count + view toggle) |
| C32 | Summary / Diagnostics / All column sets |
| F29 | Magnitude-aware $ precision |
| F30 | Inline sparkline columns on movers grids |
| C40 | DAG minimap + fit-to-selection |
| C45 | Case File persistent tray |
| C46 | Watch monitoring tray in the shell |
| C10 | Copy scoped-link / view token |
| C2 | Icon-and-text sidebar nav |
| C11 | Click-through "View on «owner» →" under mirrored KPIs |
| C20 | One section-render order helper |
| C36 | Auto entity-drills on identity columns |

---

## Suggested first cut (if any of this ships)

Highest payoff per line-of-CSS/copy, no migration, no new queries:

1. Recs **1, 2, 3** — the header/scope stack (one band, one scope statement, labeled controls).
2. Recs **12, 30, 31** — emoji leftovers + Ask as a product surface.
3. Recs **21, 22, 26** — shorter pills, quieter nested nav, one heading component.
4. Recs **33, 43, 45** — calmer KPI cards + operator English on contracts and subtitles.

Everything else can land as drive-by edits next to whatever page is already open.

---

*Generated 2026-08-31 against `origin/main` v4.365.0. Trust the cited lines over
this snapshot if they have moved.*
