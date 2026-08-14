# Owner design review — color scheme + section display (2026-08-14, v4.155)

Owner ask (verbatim): "Review the updated app and recommend improvements to the
app. I do not like the color scheme and some of the sections need to be
displayed better."

## Review of v4.154 (what the owner was looking at)

**Color.** The chrome was dark navy (`#0a0f1c` / `#0f1729` / `#131d33`) with a
cyan-sky accent — and the accent hex (`#38bdf8`) was *the same hue as the INFO
severity*. Brand dot, primary buttons, selected pills, focus rings, the "live"
freshness badge, the filter-contract note, links, and info-severity signals all
shared one blue, on blue-tinted grays (slate hairlines, slate inks), on a navy
background. The app read monochrome-blue "generic dark dashboard" — the exact
look the owner pushed back on. The severity traffic lights themselves
(ok green / warn amber / bad rose / high orange / info sky) are adjudicated
semantics (A1/A2, rec50) and were NOT the problem.

**Sections.** `section_header` rendered a 1.02rem (≈16px) title inside a gray
wash bar with 6px vertical margins — visually the same weight as a KPI-card
title, and the neutral gray wash competed with the severity-tinted washes that
actually carry signal. Long pages (Operations: 20 headers, Security: 16,
Cost: 13) stacked panels with no rhythm, so section boundaries disappeared on
scroll. Two historical `.ow-chip` CSS blocks silently overrode each other.
Micro-labels sat at 11.5px (raised once in rec18, still low).

## Shipped in v4.155

1. **Warm-graphite chrome** (`#131215` / `#1a191d` / `#232228`, neutral inks
   `#eeedf2`/`#b6b3c0`/`#948fa3`, neutral hairlines): the blue cast is gone from
   every non-semantic surface. Muted ink clears WCAG AA on all three surfaces
   (5.97 / 5.59 / 5.05 — computed, and locked by the existing rec15 test).
2. **Iris accent** (`#8f8aff` / `#b0acff`), decoupled from INFO: interactive ≠
   informational. Primary-button ink `#12101f` clears 6.5:1 on the accent
   (computed-contrast lock, not a hoped hex). Multiselect chips, sidebar
   selection, focus rings, scope glow, brand dot all follow.
3. **Severity hues unchanged** — locked by `test_v4155_theme_refresh.py` so a
   re-theme can never silently move the traffic lights.
4. **Section headers are dividers now**: 1.14rem title, 20px top margin
   (rhythm), hairline underline; the neutral gray wash is dropped, severity
   tints stay (color = signal, law 8). Compact density keeps its tighter
   variant.
5. **Type floor raised again** (rec18 second pass): card/metric micro-labels
   0.76→0.80rem, status-bar labels 0.72→0.75rem, chips 0.72→0.74rem.
6. **Consequential cleanups**: one `.ow-chip` definition (the two blocks were
   overriding each other); the chart categorical scale swaps `#c084fc` purple →
   `#e879f9` fuchsia and the near-accent `ACCENT2` slot → retired cyan
   `#22d3ee`, so no two series hues collide with the new accent; the method
   badge follows; the DAG task-graph viewer (its own iframe, so it inherits
   nothing) moves to the new chrome; Altair point strokes read `palette.BG`
   instead of a hardcoded navy.

## Recommendations (not shipped — next rounds, owner to pick)

- **R1 (do first): one live-screenshot round on the new theme.** A local
  browser preview (Streamlit 1.59, static sample data) confirmed the chrome,
  accent states, section hierarchy, and chip/table legibility — but SiS pixels
  should still be eyeballed once. One observed quirk to check there: on newer
  Streamlit, `st.context.theme.type` reported "light" before hydration, so the
  status-tinted table cells served their (legible) light-pastel pairs inside
  the dark app for a render; SiS's runtime takes the `get_option("theme.base")`
  path (dark), so this should not reproduce — verify, and if it does, pin the
  dark pairs since config.toml forces dark.
- **R2: adopt the section jump-strip beyond Security (P2/S).** `section_toc` +
  `section_header(anchor=...)` shipped in rec5 but only Security uses it (7
  anchors). Operations (20 headers) and Cost (13) are the long scrolls that
  need it most.
- **R3: retire the pill-bar `<hr>` (P3/S).** `lazy_sections` draws a divider
  under the section pills; now that headers carry their own underline, the
  double rule may read busy — decide on the R1 screenshots.
- **R4: brand favicon** (`app/assets/favicon.png`) still carries the cyan
  radar dot. Regenerate in iris if the browser-tab mismatch grates.
- **R5: exec-summary HTML export** keeps its own deliberate light/print palette
  (`--accent:#0369a1` sky). Align it with the brand only if exported artifacts
  should match the app.
- **R6 (declined for now): light theme.** `status_colors` already carries
  light-pair equivalents, but charts, the heatmap ramp, and the DAG viewer
  would all need light variants — a full round of its own, only worth it if
  the owner actually wants light mode rather than a better dark one.
