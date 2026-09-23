# Codex 50-Rec Visual Review — Ground-Truth Adjudication

**Date:** 2026-09-23 · **Reviewed against:** OVERWATCH_NEW `main` @ `ad72426` (v4.582.0) · **No app files changed.**
**Method:** 7-agent adversarial workflow — 5 adjudicators (one per 10-rec group, each reading the cited code) → 2 refute-by-default verifiers (one re-opened the code behind every ALREADY_DONE / WRONG_PREMISE claim; one audited Streamlit-in-Snowflake fit + the owner's preservation list). Every verdict cites real code.

> **Bottom line:** Codex's read of the *architecture* ("stronger hierarchy, less framing") is fair, and ~half the recs are real, cheap wins. But **10 of 50 are already shipped or fight Streamlit's grain**, and of Codex's own seven "build first" picks, **only two (4 and 20) survive clean** — the rest are already-done, SiS dead-ends, over-built, or large projects mislabeled as quick wins.

## Verdict tally

| Verdict | Count | Meaning |
|---|---:|---|
| **ALREADY_DONE** | 5 | Codex misread the current state — the app already does it |
| **PARTIAL** | 22 | Half shipped; a real but bounded incremental gap (several with a SiS-dead-end half) |
| **VALID_GAP** | 23 | A real, code-confirmed improvement not yet done |
| *(→ OVERBUILT on verify)* | 1 | rec18's named fix fights the SiS grain |

All 5 ALREADY_DONE and 4 spot-checked VALID_GAPs were **code-confirmed, none overturned**. The one verify overturn was **rec18** (see below).

## On Codex's own "build first" set (1, 2, 4, 5, 18, 20, 24)

| Rec | Codex says | Grounded verdict |
|---|---|---|
| **4** — remove heading boxes | build first | **✅ SURVIVES** — one global CSS edit (`theme.py:204`), reserves the box for severity states. Cleanest win. |
| **20** — retire completed loading panels | build first | **✅ SURVIVES** — a one-line `st.status(...).update(state="complete")` on 3 shared sites. Clean. |
| **5** — Warehouses attention-ranked opener | build first | **Worthwhile but NOT quick** — effort **L**: needs a merged builder over `warehouse_health`/`fact_warehouse_pressure` (both exist, scattered across sub-lenses) and must preserve the blue-selection drill + default spend chart. |
| **1** — compress the page header | build first | **~ALREADY_DONE** — the substantive scope/freshness de-dup is shipped (`components.py:262-272`, attributed to rec1/rec11 in-code). Only a cosmetic "two physical rows" remains, which fights SiS block layout. Nothing to build. |
| **2** — Overview cards → rail | build first | **Half done, half dead-end** — abnormal-first is already there (severity tokens + `page_verdict_line`); the unshipped half (value+nav **inside** a card) is impossible in SiS (`metric_card_html` is a static markdown string — no live button). |
| **18** — scope toolbar reflow | build first | **OVERBUILT (overturned)** — `st.columns` don't wrap and every interaction reruns; the app *deliberately* chose single-line truncation (`theme.py:294-308`). A width-aware reflow fights the grain; only a cosmetic ratio-rebalance is SiS-fit, and that's LOW value. |
| **24** — unified investigation panel | build first | **Real but weak** — L effort for LOW value; incident-close vs grant-revoke are genuinely different domains. Adopt `master_detail` opportunistically, not as a priority. |

**Net:** the two real build-first wins are **4 and 20**. **5** is a good larger project. **1/2/18/24** do not justify build-first status.

## Recommended first wave — the clean, SiS-safe small wins

All **effort S**, no preservation erosion, each a one-place edit:

1. **Rec 4** — reserve the section-heading box for severity states (global CSS; sharpens the existing severity hierarchy). *MED value.*
2. **Rec 20** — mark the 3 auto-completing `st.status` panels `state="complete"` so a finished page stops reading as still-loading.
3. **Rec 26** — fix the over-broad responsive selector (`theme.py:487` uses a descendant selector that flattens *every* nested column under 1180px, not just the two master/detail panes — a real layout bug). Use a child combinator.
4. **Rec 39** — heatmap rows are *selected* by impact rank but *displayed* in default order (`charts.py:1274` has no sort) — add `EncodingSortField('Value', 'sum', 'descending')` so what you see matches what was ranked. *(A correctness bug, not just polish.)*
5. **Rec 25** — dress the empty master/detail pane with the existing `empty_state` primitive instead of a bare caption (fixes several sites at once).
6. **Rec 8** — move the open-incident worklist *above* the 14-day lifecycle Gantt in Control Room (pure reorder; count-summary stays first).
7. **Rec 27** — give the queue/spill + lock-wait contention tables an asymmetric ratio instead of two cramped 50% columns.
8. **Rec 30** — wrap Decision Studio's ROI breakdown table in an `st.expander` so the lever chart leads (export stays reachable inside).

**Chart quick wins** (also S, batch into one `charts.py` pass): **34** endpoint labels on count bars · **37** drop per-day point markers on long lines · **38** real sparkline units/tooltips (not "Day"/"Value") · **41** move computed takeaways above their charts · **35** row-count height scaling for `bar_count`.

**Optional taste calls** (S, but touch every label — get an owner eyeball): **12** bump small metadata to ~12px · **13** sentence-case metric labels.

## Worthwhile larger projects (M–L)

- **Rec 9 / Rec 10** (M) — chapter **Pipeline SLA** (Tonight / Recurring / Performance / Data checks) and **Security ▸ Access** (auth / privileged access / lifecycle) using the **existing `nested_sections`** primitive already used on sibling tabs. Container-only, each panel unchanged. Good structure-per-effort.
- **Rec 5** (L) — the Warehouses attention-ranked opener (above).
- **Rec 3 / Rec 6** (M) — wire Overview's headline through the existing `hero_metric` primitive; pin Brief's spend/criticals/nightly-cycle to fixed slots with conditional cards in a second band.
- **Rec 33 / Rec 36** (M) — chart color-collision handling and horizontal bars/dumbbells for long entity names.

## Already shipped — Codex misread (no action)

- **Rec 14** — provenance badges (Metered/Allocated/Estimated) already render neutral purple/slate, *off* the warn/critical palette (`theme.py:148-149`).
- **Rec 15** — the interactive-vs-static grammar (accent pills/tabs/sidebar rail, focus-visible outlines) is a built, documented system.
- **Rec 17** — every KPI card is one template in fixed order with a min-height floor and shared value size — the exact alignment asked for.
- **Rec 23** — triage inbox column proportions already flow from `_width_for_column` by name (small for severity/kind/age, large for title/detail).
- **Rec 1** — scope/freshness de-duplication already shipped.

## Don't-build / SiS dead-ends

- **Rec 2** (per-card nav), **Rec 22** ("owner+deadline beneath the title" in a cell), **Rec 23** (per-row "inbox card" HTML), **Rec 29** (inline the export button), **Rec 28** (per-cell color on >400-row grids): all need per-widget HTML inside `st.dataframe`/`st.markdown` that SiS can't render while keeping row selection — the app already made these trade-offs deliberately.
- **Rec 18** — the toolbar reflow (over-built, above).

## Preservation watch-list (owner's protected items)

Any implementation of these must **not** erode the named guarantees:

| Preserve | At-risk recs | Guard |
|---|---|---|
| Billing-basis distinctions | 2, 3, 6, 14, 21, 22 | Keep the `method`/`Basis` chip + credit-billed-vs-storage/transfer captions on any card/table redesign. |
| Numeric exports | 6, 21, 22, 29, 30 | The CSV *is* the displayed df — never trim columns off the export; keep the download button intact. |
| Default-visible Cost coverage tables | 21, 22 | Column trims must move fields to the drill/export, not remove them from the default view. |
| Blue selection language | 5, 8, 10, 24 | Pure reorders/regroups keep `entity_nav_table`/`selectable_table` drills. |
| Task-graph capabilities | 43, 44, 45 | Keep Find/zoom/Fit/Failed/Critical/Highlight/Full/SVG (recs 43-45 are inside the `components.v1.html` iframe, so they *don't* fight SiS — full CSS/JS control). |
| Chart detail (rec32) | 32 | A daily Top-N+"Other" rollup is safe **only** if the full category set stays default-visible in the accompanying table/legend. |
| Evidence-qualified security score (rec47) | 47 | Leading the strip with findings must retain the score+coverage semantics (the "—" when coverage is incomplete) as the supporting value. |
| No false conversion funnel (rec48) | 48 | Adoption stages as aligned stat tiles, never funnel geometry (the stages aren't a strict subset). |

## Full adjudication (all 50)

`AD`=already done · `PT`=partial · `VG`=valid gap · `OB`=overbuilt · effort/value in parentheses. Bold = recommended first wave.

| # | V | E/Val | What & grounded note |
|---|---|---|---|
| 1 | AD | S/LOW | Header scope/freshness de-dup already shipped; "two rows" is cosmetic, fights SiS blocks. |
| 2 | PT | L/LOW | Abnormal-first done; per-card nav is a SiS dead-end (static markdown card). |
| 3 | PT | M/MED | `hero_metric` exists & used elsewhere; wire Overview's headline through it. Keep billing captions. |
| **4** | **VG** | **S/MED** | **Every neutral heading is boxed (`theme.py:204`); reserve the box for severity. One CSS edit.** |
| 5 | PT | L/MED | Real "lead with what's wrong" opener; needs a merged builder over existing health/pressure. |
| 6 | PT | M/MED | First 3 KPIs pinned but nightly-cycle floats; add a fixed slot + secondary band. Feeds exports. |
| 7 | PT | M/MED | `page_verdict_line` already lists worst-first; the 3 actionable jumps could consolidate. |
| **8** | **VG** | **S/MED** | **Open-incident worklist sits below the Gantt; reorder it above. Pure reorder.** |
| 9 | VG | M/MED | Chapter Pipeline SLA via existing `nested_sections`. Container-only. |
| 10 | VG | M/MED | Chapter Security ▸ Access via `nested_sections`. Keep drills/exports. |
| 11 | PT | M/LOW | Type scale real but only ~2 of ~8 sizes are tokens; rest are scattered rem literals. |
| 12 | PT | S/MED | Small metadata renders ~10.5-11px; bump toward 12px. Taste call. |
| 13 | VG | S/LOW | Labels are uppercase+tracked; sentence-case = drop 4 CSS rules. Taste call. |
| 14 | AD | S/LOW | Provenance chips already neutral purple/slate, off the warn palette. |
| 15 | AD | S/LOW | Interactive-vs-static grammar already a built system. |
| 16 | PT | M/MED | No shared prose max-width; add ~70-80ch cap to verdict/explanatory blocks. |
| 17 | AD | S/LOW | KPI cards already single-template, fixed order, min-height floor. |
| 18 | PT→**OB** | M/MED | Reflow fights SiS (`st.columns` don't wrap; truncation was deliberate). Don't build. |
| 19 | VG | S/LOW | Tighten sidebar spacing + pull Jump up (a divider pushes it away). Small CSS. |
| **20** | **PT** | **S/LOW** | **3 `st.status` panels never mark complete; one-line `.update(state="complete")`.** |
| 21 | PT | L/MED | No essentials-vs-export split; blunt trim risks Cost tables + CSV. Keep full cols in export. |
| 22 | PT | M/MED | Column trim feasible; "beneath the title" not (dataframe grain). Keep Basis/Est.$ in drill. |
| 23 | AD | S/LOW | Inbox column widths already applied; per-row card HTML fights the grain. |
| 24 | VG | L/LOW | `master_detail` exists; incidents/security hand-roll. L for LOW — opportunistic only. |
| **25** | **VG** | **S/LOW** | **Empty detail pane is a bare caption; dress with `empty_state`. Fixes several sites.** |
| **26** | **VG** | **S/LOW** | **Responsive selector flattens all nested columns <1180px (`theme.py:487`); child-combinator fix. Real bug.** |
| **27** | **VG** | **S/MED** | **Contention tables cramped at 50/50; asymmetric ratio or stack.** |
| 28 | PT | L/LOW | >400-row path drops per-cell color (Styler skipped by design); no SiS equivalent. |
| 29 | PT | M/LOW | Footer already consolidated; can't inline the download button into a caption. |
| **30** | **VG** | **S/MED** | **DS ROI table duplicates the chart at equal weight; wrap in `st.expander` (export stays).** |
| 31 | VG | S/LOW | Chart axis/legend at 11px floor; bump to 12-13px where space allows. |
| 32 | VG | M/MED | Daily stacked has no Top-N+Other (monthly does). **Keep full set in table/export (preserve).** |
| 33 | VG | M/MED | Stable color map can collide two visible entities; add labels/outlines/panels. |
| **34** | **VG** | **S/MED** | **Count/rate bars lack endpoint labels (dollar bars have them); add `mark_text`.** |
| 35 | PT | S/MED | `bar_count` alone lacks per-row height scaling; a 10-row chart squeezes. |
| 36 | VG | M/MED | Angled labels for long names; switch to horizontal grouped/dumbbell bars. |
| **37** | **VG** | **S/MED** | **Long daily lines carpet-dotted; drop routine point markers (keep peak/rule).** |
| **38** | **VG** | **S/MED** | **Sparklines show generic "Day"/"Value"; thread the real measure + units.** |
| **39** | **VG** | **S/MED** | **Heatmap rows selected by rank but displayed unsorted; add the sort. Correctness bug.** |
| 40 | PT | S/MED | Partial-month labels dim to 0.45 (hard to read) with no "Partial" word; keep legible + annotate. |
| **41** | **VG** | **S/MED** | **Computed takeaways render below their charts; move the caption above (data already computed).** |
| 42 | PT | M/LOW | Horizon-in-title already done; only "distinct local views" remains (sections already separate). |
| 43 | PT | M/LOW | Task-graph toolbar ungrouped; group + wrap (inside iframe, SiS-safe). Keep all controls. |
| 44 | VG | L/MED | Nodes ordered alphabetically, not by adjacency; add barycenter ordering to cut crossings. |
| 45 | VG | M/MED | Node labels truncate at 31 chars; add an outside-the-graph selected-node inspector. |
| 46 | PT | S/LOW | Brief Asks are inline markdown; structure into title / owner-deadline / right-aligned estimate. |
| 47 | PT | S/MED | Posture strip leads with score; lead with open findings (keep score+coverage as support). |
| 48 | PT | S/LOW | Adoption stages are a text arrow-chain; render aligned stat tiles (no funnel geometry). |
| 49 | VG | S/LOW | Scenarios full-width; recompose `st.columns([1,2])` (assumptions narrow, results wide). |
| 50 | PT | S/LOW | Deferred-load sections ad-hoc; standardize name + one-line purpose + toggle state. |
