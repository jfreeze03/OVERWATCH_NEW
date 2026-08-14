"""OVERWATCH design system — tokens, typography, and component styling.

Replaces the old ~48-line CSS with a proper design language: a token layer
(CSS custom properties), a typographic scale, a card system with severity
stripes, refined native-widget styling (metric, table, tabs, segmented
section nav, buttons, popovers), an inline SVG icon set, a persistent status
bar, and responsive rules for narrow viewports.

v4.155 scheme ("graphite & iris", owner ask 2026-08-13: "I do not like the
color scheme"): the cold navy chrome and the everywhere-sky accent are
retired. Neutrals are de-blued graphite; the accent is iris and appears ONLY
on interactive elements (buttons, active nav, links, brand); severity hues
are richer and less neon; neutral cards carry a quiet hairline rail so a
colored rail always MEANS something (calm by default, color = signal).

Everything degrades safely: if a Streamlit test-id selector shifts between
versions, the app still renders — it just loses that flourish. No external
fonts or scripts (Streamlit-in-Snowflake CSP friendly); the type system uses
a tuned platform stack with tabular figures for data.
"""

from __future__ import annotations

import streamlit as st

_TOKENS = """
<style>
:root {
  --ow-bg:#0f1016; --ow-surface:#15161f; --ow-raised:#1c1d29;
  --ow-hairline:rgba(154,158,178,0.16); --ow-hairline2:rgba(154,158,178,0.30);
  /* rec 15 (a11y): every muted label references --ow-ink-mute, which clears WCAG AA
     4.5:1 on every surface it lands on (bg 6.3, surface 6.0, raised 5.5) — one token,
     one place to keep the floor. The v4.155 re-hue kept the same guarantee (tested). */
  --ow-ink:#edeef4; --ow-ink-soft:#b4b8c6; --ow-ink-mute:#8f94a6;
  --ow-accent:#8e8ffa; --ow-accent2:#b8b4ff;
  /* dark ink for text ON an accent-filled control (primary buttons, active pill) */
  --ow-on-accent:#14122b;
  --ow-ok:#3ecf8e; --ow-warn:#f0b429; --ow-bad:#f0566d; --ow-info:#4cc3f0;
  --ow-ok-dim:rgba(62,207,142,0.14); --ow-warn-dim:rgba(240,180,41,0.14);
  --ow-bad-dim:rgba(240,86,109,0.14); --ow-info-dim:rgba(76,195,240,0.14);
  --ow-1:4px; --ow-2:8px; --ow-3:12px; --ow-4:16px; --ow-5:24px; --ow-6:32px;
  --ow-r:8px; --ow-r-sm:6px; --ow-r-lg:12px; --ow-r-pill:999px;
  --ow-shadow:0 1px 2px rgba(0,0,0,0.30),0 6px 20px -12px rgba(0,0,0,0.55);
  --ow-shadow2:0 2px 6px rgba(0,0,0,0.35),0 18px 40px -18px rgba(0,0,0,0.65);
  --ow-ease:150ms cubic-bezier(0.22,1,0.36,1);
  --ow-font:'Inter var','Inter','SF Pro Display',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;
  --ow-mono:'SF Mono','JetBrains Mono','Roboto Mono',ui-monospace,Menlo,Consolas,monospace;
}
</style>
"""

_CSS = """
<style>
.block-container { padding-top:1.1rem; padding-bottom:2.4rem; max-width:1360px; }
.main .block-container > div { gap:0.55rem; }
html, body, [class*="css"] { font-family:var(--ow-font); }
h1,h2,h3,h4 { letter-spacing:0; color:var(--ow-ink); }
h1 { font-weight:750; font-size:1.72rem; } h2 { font-weight:700; }
h3 { font-weight:680; font-size:1.06rem; }
p,li,span,label,.stMarkdown { color:var(--ow-ink-soft); }
[data-testid="stCaptionContainer"],.stCaption,small { color:var(--ow-ink-mute) !important; }
[data-testid="stMetricValue"],.ow-num,td,th { font-variant-numeric:tabular-nums; }

.ow-page-heading { display:flex; align-items:center; gap:11px; margin:-2px 0 2px 0; }
.ow-page-heading h1 { margin:0; padding:0; font-size:1.72rem; font-weight:750; letter-spacing:0; }
.ow-page-heading__icon { color:var(--ow-accent); display:inline-flex; flex:0 0 auto; }

/* v4.155 calm-by-default: neutral metric/KPI cards carry a quiet hairline rail;
   only a severity class colors it — so a colored rail always MEANS something.
   (The old always-on accent stripe made every page glow blue and buried real
   severity in decoration.) */
div[data-testid="stMetric"] {
  position:relative; background:linear-gradient(180deg,var(--ow-raised),var(--ow-surface));
  border:1px solid var(--ow-hairline); border-radius:var(--ow-r); padding:14px 16px 12px 18px;
  box-shadow:var(--ow-shadow); transition:box-shadow var(--ow-ease),border-color var(--ow-ease); overflow:hidden; }
div[data-testid="stMetric"]::before { content:""; position:absolute; left:0; top:0; bottom:0; width:3px;
  background:var(--ow-hairline2); }
div[data-testid="stMetric"]:hover { box-shadow:var(--ow-shadow2); border-color:var(--ow-hairline2); }
[data-testid="stMetricLabel"] p { font-size:0.76rem !important; letter-spacing:0.06em; text-transform:uppercase; color:var(--ow-ink-mute) !important; font-weight:640; }
[data-testid="stMetricValue"] { font-size:1.62rem; font-weight:720; color:var(--ow-ink); }
.ow-sev-bad div[data-testid="stMetric"]::before { background:var(--ow-bad); opacity:1; }
.ow-sev-warn div[data-testid="stMetric"]::before { background:var(--ow-warn); opacity:1; }
.ow-sev-ok div[data-testid="stMetric"]::before { background:var(--ow-ok); opacity:1; }
.ow-sev-bad div[data-testid="stMetric"] { border-color:rgba(240,86,109,0.35); }

.ow-card { position:relative; background:linear-gradient(180deg,var(--ow-raised),var(--ow-surface));
  border:1px solid var(--ow-hairline); border-radius:var(--ow-r); padding:14px 16px 14px 18px;
  box-shadow:var(--ow-shadow); margin-bottom:var(--ow-3); transition:box-shadow var(--ow-ease),border-color var(--ow-ease); }
.ow-card:hover { box-shadow:var(--ow-shadow2); border-color:var(--ow-hairline2); }
.ow-card::before { content:""; position:absolute; left:0; top:0; bottom:0; width:3px; border-radius:var(--ow-r) 0 0 var(--ow-r); background:var(--ow-hairline2); }
.ow-card--ok::before { background:var(--ow-ok); } .ow-card--warn::before { background:var(--ow-warn); }
.ow-card--bad::before { background:var(--ow-bad); } .ow-card--info::before { background:var(--ow-info); }
.ow-src-badge { font-size:11px; letter-spacing:0.08em; text-transform:uppercase; border:1px solid; border-radius:8px; padding:1px 6px; white-space:nowrap; }
/* the chips group right-aligns and, on a long two-chip $ card, wraps to its own
   line instead of shrinking the label (the old float:right was dead on a flex child). */
.ow-card__chips { margin-left:auto; display:inline-flex; align-items:center; gap:6px; flex-wrap:wrap; justify-content:flex-end; }
.ow-src-badge--mart { color:#3ecf8e; border-color:rgba(62,207,142,0.3); }
.ow-src-badge--live { color:#4cc3f0; border-color:rgba(76,195,240,0.3); }
.ow-src-badge--stale { color:#f0b429; border-color:rgba(240,180,41,0.3); }
.ow-src-badge--other { color:#8d93a4; border-color:rgba(141,147,164,0.3); }
.ow-src-badge--method { color:#5bc8bf; border-color:rgba(91,200,191,0.35); }  /* rec 13: how derived (teal — iris now belongs to the accent) */
.ow-src-badge--scope { color:#a9b0c4; border-color:rgba(169,176,196,0.4); }    /* rec 13: account-wide / company */
.ow-card__title { font-size:0.76rem; letter-spacing:0.06em; text-transform:uppercase; color:var(--ow-ink-mute); font-weight:640; display:flex; align-items:center; gap:7px; }
.ow-card__value { font-size:1.55rem; font-weight:720; color:var(--ow-ink); margin-top:3px; font-variant-numeric:tabular-nums; }
.ow-card__meta { font-size:0.78rem; color:var(--ow-ink-soft); margin-top:2px; }

/* rec 15 (a11y): keyboard-focusable + touch-tappable KPI help. Replaces the
   hover-only card title= tooltip (invisible on touch, unreachable by keyboard).
   The affordance is a focusable '?' badge; the tooltip fires on hover AND focus,
   so Tab-users and touch-users both get it. CSP-safe (no JS): content:attr(). */
.ow-help { display:inline-flex; align-items:center; justify-content:center; width:18px; height:18px;
  flex:0 0 auto; border-radius:var(--ow-r-pill); border:1px solid var(--ow-hairline2);
  color:var(--ow-ink-mute); font-size:0.75rem; font-weight:700; line-height:1; cursor:help;
  position:relative; outline:none; text-transform:none; letter-spacing:0; }
.ow-help:hover, .ow-help:focus-visible { color:var(--ow-ink); border-color:var(--ow-accent); }
.ow-help:focus-visible { box-shadow:0 0 0 2px rgba(142,143,250,0.45); }
.ow-help[data-help]::after {
  content:attr(data-help); position:absolute; left:0; top:calc(100% + 6px);
  min-width:200px; max-width:300px; padding:8px 10px; border-radius:var(--ow-r-sm);
  background:var(--ow-raised); border:1px solid var(--ow-hairline2); color:var(--ow-ink-soft);
  font-size:0.72rem; line-height:1.42; font-weight:500; letter-spacing:0; text-transform:none;
  text-align:left; white-space:normal; box-shadow:var(--ow-shadow2); z-index:60;
  opacity:0; visibility:hidden; transition:opacity var(--ow-ease); pointer-events:none; }
.ow-help:hover::after, .ow-help:focus::after, .ow-help:focus-visible::after { opacity:1; visibility:visible; }

/* v4.155 section display: sections were cramped (6px rhythm) and the neutral
   wash was too faint to separate a header from the panel above it — long pages
   read as one wall. Headers get real air above, a full hairline frame with the
   severity rail on the left edge, and wrap cleanly on narrow viewports. */
.ow-section { display:flex; align-items:center; flex-wrap:wrap; gap:10px; margin:22px 0 10px; padding:8px 14px;
  border-radius:var(--ow-r-sm); border:1px solid var(--ow-hairline);
  border-left:3px solid var(--ow-hairline2); background:linear-gradient(90deg,rgba(154,158,178,0.07),transparent 62%); }
.ow-section--ok { border-left-color:var(--ow-ok); background:linear-gradient(90deg,var(--ow-ok-dim),transparent 62%); }
.ow-section--warn { border-left-color:var(--ow-warn); background:linear-gradient(90deg,var(--ow-warn-dim),transparent 62%); }
.ow-section--bad { border-left-color:var(--ow-bad); background:linear-gradient(90deg,var(--ow-bad-dim),transparent 62%); }
.ow-section--info { border-left-color:var(--ow-info); background:linear-gradient(90deg,var(--ow-info-dim),transparent 62%); }
.ow-section__title { font-weight:700; color:var(--ow-ink); font-size:1.05rem; }
.ow-section__icon { display:inline-flex; color:var(--ow-ink-soft); }
.ow-section__badge { margin-left:auto; font-size:0.72rem; font-weight:650; letter-spacing:0.04em; text-transform:uppercase; padding:2px 9px; border-radius:var(--ow-r-pill); border:1px solid var(--ow-hairline2); color:var(--ow-ink-soft); }

/* v4.155: the scope-contract line tucks under its section header (same rail
   inset, pulled up) and is neutral — it is metadata, not an info-severity
   signal; nearly every section renders one, so the blue tint was chrome noise. */
.ow-filter-contract { margin:-6px 0 10px 0; padding:5px 12px 5px 14px; border-left:3px solid var(--ow-hairline2);
  color:var(--ow-ink-mute); background:rgba(154,158,178,0.05); font-size:0.72rem; line-height:1.45; }

.ow-exceptions { margin:4px 0 10px; border-top:1px solid var(--ow-hairline);
  border-bottom:1px solid var(--ow-hairline); }
.ow-exception { display:grid; grid-template-columns:minmax(120px,1fr) auto minmax(180px,2fr);
  gap:12px; align-items:center; padding:7px 10px; border-left:3px solid var(--ow-warn);
  border-bottom:1px solid var(--ow-hairline); background:rgba(240,180,41,0.045); }
.ow-exception:last-child { border-bottom:0; }
.ow-exception--bad { border-left-color:var(--ow-bad); background:rgba(240,86,109,0.055); }
.ow-exception--ok { border-left-color:var(--ow-ok); background:rgba(62,207,142,0.045); }
.ow-exception__label { color:var(--ow-ink); font-size:0.78rem; font-weight:700; }
.ow-exception__value { color:var(--ow-ink); font-size:0.84rem; font-weight:750;
  font-variant-numeric:tabular-nums; }
.ow-exception__detail { color:var(--ow-ink-mute); font-size:0.72rem; line-height:1.35; }
@media (max-width:700px) {
  .ow-exception { grid-template-columns:1fr auto; }
  .ow-exception__detail { grid-column:1 / -1; }
}

.ow-statusbar { display:flex; gap:8px; flex-wrap:wrap; align-items:stretch; margin:0 0 12px 0; }
.ow-stat { flex:1 1 130px; min-width:120px; position:relative; background:linear-gradient(180deg,var(--ow-raised),var(--ow-surface));
  border:1px solid var(--ow-hairline); border-radius:var(--ow-r-sm); padding:8px 12px 8px 14px; box-shadow:var(--ow-shadow); }
.ow-stat::before { content:""; position:absolute; left:0; top:0; bottom:0; width:3px; border-radius:var(--ow-r-sm) 0 0 var(--ow-r-sm); background:var(--ow-hairline2); }
.ow-stat--ok::before { background:var(--ow-ok); } .ow-stat--warn::before { background:var(--ow-warn); }
.ow-stat--bad::before { background:var(--ow-bad); } .ow-stat--info::before { background:var(--ow-info); }
.ow-stat__k { font-size:0.72rem; letter-spacing:0.06em; text-transform:uppercase; color:var(--ow-ink-mute); font-weight:640; }
.ow-stat__v { font-size:1.04rem; font-weight:720; color:var(--ow-ink); font-variant-numeric:tabular-nums; display:flex; align-items:center; gap:6px; }
.ow-stat__spark { margin-top:2px; opacity:0.9; }
.st-key-ow_status_actions { margin-top:-14px; margin-bottom:8px; }
.st-key-ow_status_actions button { min-height:1.7rem; font-size:0.72rem; }

.ow-chip { display:inline-flex; align-items:center; gap:5px; padding:2px 10px; margin:0 6px 4px 0; border-radius:var(--ow-r-pill);
  font-size:0.72rem; font-weight:620; border:1px solid var(--ow-hairline2); color:var(--ow-ink-soft); background:rgba(154,158,178,0.05); }
.ow-chip-ok { color:var(--ow-ok); border-color:rgba(62,207,142,0.45); background:var(--ow-ok-dim); }
.ow-chip-bad { color:var(--ow-bad); border-color:rgba(240,86,109,0.45); background:var(--ow-bad-dim); }
.ow-chip-warn { color:var(--ow-warn); border-color:rgba(240,180,41,0.45); background:var(--ow-warn-dim); }

/* Chip pills (chip() helper — scope summary in the status bar + severity pills).
   The scope-chip BAND in the filter strip was retired in v4.65 for the compact
   toolbar; the active-filter border glow below stays. Token layer only. */
.ow-chip{display:inline-flex;align-items:center;gap:6px;padding:2px 10px;
  border-radius:var(--ow-r-pill);font-size:0.72rem;font-weight:600;
  letter-spacing:.02em;line-height:1.55;border:1px solid var(--ow-hairline2);
  color:var(--ow-ink-soft);background:var(--ow-raised);}
.ow-chip b{color:var(--ow-ink);font-weight:700;}
.ow-chip-warn{border-color:rgba(240,180,41,0.45);background:var(--ow-warn-dim);color:var(--ow-ink);}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.ow-scope-active){
  border-color:rgba(142,143,250,0.40);
  box-shadow:0 0 0 1px rgba(142,143,250,0.22),var(--ow-shadow);}
.ow-kicker { font-size:0.75rem; letter-spacing:0; font-weight:750; color:var(--ow-ink-mute); text-transform:uppercase; margin-bottom:0.1rem; }
.ow-brand { display:flex; align-items:center; gap:9px; }
.ow-brand-dot { width:11px; height:11px; border-radius:999px;
  background:radial-gradient(circle at 30% 30%,var(--ow-accent2),var(--ow-accent));
  box-shadow:0 0 10px rgba(142,143,250,0.9),0 0 2px rgba(142,143,250,1); animation:ow-pulse 2.8s ease-in-out infinite; }
@keyframes ow-pulse { 0%,100% { opacity:1; } 50% { opacity:0.55; } }
.ow-brand-word { font-weight:800; letter-spacing:0.02em;
  background:linear-gradient(90deg,var(--ow-ink),var(--ow-accent)); -webkit-background-clip:text;
  -webkit-text-fill-color:transparent; background-clip:text; }

/* Native segmented controls (stButtonGroup) wrap every option and retain a
   visible keyboard focus ring. The role/label rules below are the old-radio
   compatibility path for Streamlit-in-Snowflake runtimes without the widget. */
div[data-testid="stButtonGroup"] div[data-baseweb="button-group"] {
  gap:4px; padding:4px; background:var(--ow-surface); border:1px solid var(--ow-hairline);
  border-radius:var(--ow-r-lg); flex-wrap:wrap !important; width:100%; }
div[data-testid="stButtonGroup"] button { flex:0 1 auto; min-width:44px; white-space:normal;
  border-radius:var(--ow-r-pill); }
div[data-testid="stButtonGroup"] button:focus-visible { outline:2px solid var(--ow-accent);
  outline-offset:2px; z-index:2; }
div[role="radiogroup"][aria-label="Section"], div[role="radiogroup"][aria-label^="Window"] {
  gap:4px; padding:4px; background:var(--ow-surface); border:1px solid var(--ow-hairline);
  border-radius:var(--ow-r-lg); flex-wrap:wrap; }
div[role="radiogroup"][aria-label="Section"] label, div[role="radiogroup"][aria-label^="Window"] label {
  border-radius:var(--ow-r-pill); padding:3px 12px; margin:0; white-space:nowrap; transition:background var(--ow-ease),color var(--ow-ease); }
div[role="radiogroup"][aria-label="Section"] label:hover { background:rgba(154,158,178,0.10); }
div[role="radiogroup"][aria-label="Section"] label:has(input:checked) {
  background:linear-gradient(180deg,var(--ow-accent2),var(--ow-accent)); color:var(--ow-on-accent); }

.stButton > button { border-radius:var(--ow-r-sm); border:1px solid var(--ow-hairline2); font-weight:620;
  transition:transform var(--ow-ease),box-shadow var(--ow-ease),border-color var(--ow-ease); }
.stButton > button:hover { border-color:var(--ow-accent); box-shadow:0 6px 18px -10px rgba(142,143,250,0.6); }
.stButton > button[kind="primary"],
.stButton > button[data-testid="stBaseButton-primary"],
button[data-testid="stBaseButton-primary"],
button[data-testid="baseButton-primary"] {
  background:linear-gradient(180deg,var(--ow-accent2),var(--ow-accent)) !important;
  color:var(--ow-on-accent) !important; border:none !important; }
/* SiS builds vary the button markup; force dark ink on every descendant so
   an accent pill can never render pale-on-pale (live finding 2026-07-10:
   the '2 open critical(s)' chip and Execute bulk RESOLVE were unreadable). */
.stButton > button[kind="primary"] p, .stButton > button[kind="primary"] span,
button[data-testid="stBaseButton-primary"] p, button[data-testid="stBaseButton-primary"] span {
  color:var(--ow-on-accent) !important; }

button[data-baseweb="tab"] { font-weight:640; }

/* Multiselect chips: the default BaseWeb tag rendered as a pale wash —
   selections were unreadable (live finding 2026-07-10, Alerts bulk picker).
   Dark chip, accent hairline, real text. */
.stMultiSelect [data-baseweb="tag"] { background:rgba(142,143,250,0.18) !important;
  border:1px solid rgba(142,143,250,0.55) !important; border-radius:var(--ow-r-sm); }
.stMultiSelect [data-baseweb="tag"] span { color:#e4e3ff !important; }
.stMultiSelect [data-baseweb="tag"] svg { fill:#e4e3ff !important; }
[data-testid="stDataFrame"] { border:1px solid var(--ow-hairline); border-radius:var(--ow-r-sm); overflow:hidden; box-shadow:var(--ow-shadow); }
[data-testid="stExpander"] { border:1px solid var(--ow-hairline); border-radius:var(--ow-r-sm); background:var(--ow-surface); }
[data-testid="stExpander"] summary:hover { color:var(--ow-accent); }
div[data-testid="stPopover"] > button { border-radius:var(--ow-r-pill); }

section[data-testid="stSidebar"] { background:linear-gradient(180deg,var(--ow-bg),var(--ow-surface)); }
section[data-testid="stSidebar"] div[role="radiogroup"] label { border-radius:var(--ow-r-sm); padding:4px 10px; margin:1px 0; transition:background var(--ow-ease); }
section[data-testid="stSidebar"] div[role="radiogroup"] label:hover { background:rgba(154,158,178,0.10); }
section[data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked) {
  background:linear-gradient(90deg,rgba(142,143,250,0.16),transparent); box-shadow:inset 3px 0 0 var(--ow-accent); }

@media (max-width:640px) {
  .block-container { padding-left:0.6rem; padding-right:0.6rem; }
  [data-testid="stMetricValue"] { font-size:1.32rem; }
  .ow-stat { flex-basis:46%; }
}
/* Crisp section switches: when the lazy-section radio changes, Streamlit marks
   the outgoing section's elements stale while the new ones render. Hide stale
   elements (opacity, so layout height doesn't thrash) so the previous section
   can't visually bleed/linger under the new one. */
[data-stale="true"], .element-container[data-stale="true"], .stStale {
  opacity: 0 !important; transition: opacity 40ms linear !important; pointer-events: none !important;
}
@media (prefers-reduced-motion:reduce) { *,*::before { transition:none !important; animation:none !important; } }
</style>
"""


_COMPACT_CSS = """
<style>
/* Compact density (Views popover toggle): more rows per screen, same order.
   Ops/DBA scanning mode — spacing shrinks, hierarchy and colors do not. */
.block-container { padding-top:0.6rem; padding-bottom:1.4rem; }
.main .block-container > div { gap:0.35rem; }
div[data-testid="stMetric"] { padding:8px 10px 7px 12px; }
[data-testid="stMetricValue"] { font-size:1.3rem !important; }
.ow-card { padding:8px 10px 8px 12px; margin-bottom:var(--ow-2); }
.ow-section { padding:5px 10px; margin:10px 0 6px; }
.ow-filter-contract { margin:-4px 0 6px 0; }
div[data-testid="stDataFrame"] { font-size:0.82rem; }
</style>
"""


def inject_theme() -> None:
    """Inject tokens + component CSS once per render. Cheap; no network."""
    st.markdown(_TOKENS + _CSS, unsafe_allow_html=True)
    if st.session_state.get("_ow_density") == "compact":
        st.markdown(_COMPACT_CSS, unsafe_allow_html=True)


def chip(text: str, state: str = "") -> str:
    cls = {"ok": "ow-chip ow-chip-ok", "bad": "ow-chip ow-chip-bad",
           "warn": "ow-chip ow-chip-warn"}.get(state, "ow-chip")
    return f'<span class="{cls}">{text}</span>'
