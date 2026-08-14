# UI review — color scheme + section display (2026-08-13, v4.155)

Owner ask: *"Review the updated app and recommend improvements. I do not like
the color scheme and some of the sections need to be displayed better."*

Review method: full read of the design layer (`theme.py`, `palette.py`,
`status_colors.py`, `charts.py`, `components.py`, `main.py` shell, page
layouts) against the v4.148–4.154 state. Two findings shipped in v4.155; the
rest are recommendations below, each sized so a future round can pick them up
without re-deriving context.

## What was wrong

1. **The scheme was the stock "AI dashboard" look.** Navy-blue blacks
   (`#0a0f1c/#0f1729/#131d33`), one bright sky accent (`#38bdf8`) used for
   *everything* — INFO severity, every neutral card stripe, buttons, nav,
   brand, focus — and pastel-neon severity hues. Because the accent doubled
   as INFO and as the default rail on every neutral card, the page glowed
   blue everywhere and real severity color had no contrast against the
   decoration.
2. **Sections ran together.** `.ow-section` had a 6px vertical rhythm and a
   near-invisible neutral wash; on long pages (Overview stacks 6+ sections;
   Operations/Security more) headers disappeared into the wall of panels.
   The per-section scope-contract line rendered as a *blue info-tinted* bar
   under nearly every header — signal-colored chrome repeated ~everywhere.

## What shipped (v4.155)

### "Graphite & iris" scheme — through the existing token architecture

| Token | Was (navy/sky) | Now (graphite/iris) |
|---|---|---|
| BG / SURFACE / RAISED | `#0a0f1c` / `#0f1729` / `#131d33` | `#0f1016` / `#15161f` / `#1c1d29` |
| INK / SOFT / MUTE | `#e8eef7` / `#aab6c8` / `#8593a8` | `#edeef4` / `#b4b8c6` / `#8f94a6` |
| ACCENT / ACCENT2 | `#38bdf8` / `#22d3ee` (= INFO) | `#8e8ffa` / `#b8b4ff` (iris, interactive only) |
| OK / WARN / BAD | `#34d399` / `#fbbf24` / `#fb7185` | `#3ecf8e` / `#f0b429` / `#f0566d` |
| HIGH / INFO | `#fb923c` / `#38bdf8` | `#f5883d` / `#4cc3f0` |
| on-accent ink | scattered `#06121f` literals | `--ow-on-accent:#14122b` token |

Principles, in order:

- **Accent ≠ severity ≠ information.** Iris appears only on interactive
  chrome (buttons, active nav, brand, focus rings). INFO stays sky and now
  means only "informational". No severity hue is in the blue/violet family,
  so nothing interactive can be misread as a health signal.
- **Calm by default, color = signal.** Neutral metric/KPI/status cards carry
  a hairline rail instead of the accent stripe; only `ok/warn/bad/info`
  classes color a rail. A colored edge now always *means* something.
- **Accessibility floor kept.** `--ow-ink-mute` clears WCAG AA 4.5:1 on every
  surface (worst case 5.5:1, vs 5.4:1 before) — still enforced by the
  contrast test, not by hand. Severity hues sit at ≥5:1 on RAISED.
- **Drift locks strengthened.** `test_palette_drift.py` now bans the retired
  hues in consumers too; the primary-button ink lock pins the token + the
  `!important` force; the metric-card trend lock reads `palette.OK` instead
  of a hex. The name-stable entity palette (`_STABLE_PALETTE`, C15) is
  deliberately untouched — it is an a11y/stability contract, not chrome.

### Section display

- Headers: 22px air above (10px in compact), a full hairline frame with the
  severity rail on the left edge, title 1.02→1.05rem, `flex-wrap` so the
  badge drops below the title on narrow viewports instead of colliding.
- The scope-contract line tucks under its header (same rail inset, pulled up
  via negative margin) and is neutral-tinted — it is metadata, not an
  info-severity signal.

## Recommendations — not shipped this round

Ordered by expected value; none blocks another.

1. **Panel containers on the heaviest pages.** Sections now separate, but the
   *content* of a section (chart + caption + table + result_caption) still
   floats free. Wrapping each section body in `st.container(border=True)`
   on Overview and Cost & Contract would make ownership unambiguous. Touches
   ~9 page files; do it page-by-page, Overview first.
2. **Header chrome diet.** `page_header` renders subtitle caption + lag-note
   caption + scope chips as three stacked rows before content starts. Merge
   the lag note into the subtitle line (one caption) and the first screenful
   gains a row on every page.
3. **`section_toc` adoption.** The jump-strip exists but is barely used;
   Operations → Queries and Security → Posture are long enough to warrant it.
   Zero-risk, additive.
4. **Table density token.** `styled_table` heights come from `sizing.py`, but
   row density is default; a compact-density row-height override
   (`[data-testid="stDataFrame"]` font already shrinks) would fit ~30% more
   rows in triage screens.
5. **Theme debt.** `theme.py` carries two `.ow-chip` rule blocks (v4.65
   leftover). Merge next time the pinned shapes in `test_design_wave` /
   `test_v4134` are touched; left alone this round to keep the diff honest.
6. **If iris misses taste** — the architecture makes a re-hue an 8-value swap
   (tokens + palette + config.toml + the four literal-pinned tests). Two
   pre-checked alternates that keep every severity/contrast rule:
   - *Deep-sea teal:* ACCENT `#2fbfa8` / ACCENT2 `#7fe0cf` — calm, but sits
     closer to OK-green than iris does (rail confusion risk at 3px).
   - *Warm graphite mono:* ACCENT `#d9dae3` (ivory) — severity becomes the
     only color on screen; most radical, zero hue collisions; primary
     buttons read as "paper" pills.
7. **Light theme** is half-plumbed (`status_colors` has light pairs,
   `_theme_is_light()` exists) but the injected CSS is dark-only. A real
   light mode is a second token block + a config toggle — medium effort,
   only worth it if it would actually be used.
