"""OVERWATCH design system — tokens, typography, and component styling.

Replaces the old ~48-line CSS with a proper design language: a token layer
(CSS custom properties), a typographic scale, a card system with severity
stripes, refined native-widget styling (metric, table, tabs, segmented
section nav, buttons, popovers), an inline SVG icon set, a persistent status
bar, and responsive rules for narrow viewports.

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
  --ow-bg:#111827; --ow-surface:#172033; --ow-raised:#1f2937;
  --ow-hairline:rgba(148,163,184,0.16); --ow-hairline2:rgba(148,163,184,0.28);
  /* rec 15 (a11y): muted labels live on one token so small (0.62-0.70rem)
     muted labels clear WCAG AA 4.5:1 on every surface they land on (bg 6.1, surface 5.7,
     raised 5.4). Every muted label references this one token, so one change fixes all. */
  --ow-ink:#f8fafc; --ow-ink-soft:#cbd5e1; --ow-ink-mute:#94a3b8;
  --ow-accent:#60a5fa; --ow-accent2:#2dd4bf;
  --ow-ok:#34d399; --ow-warn:#f59e0b; --ow-bad:#f87171; --ow-info:#60a5fa;
  --ow-ok-dim:rgba(52,211,153,0.13); --ow-warn-dim:rgba(245,158,11,0.13);
  --ow-bad-dim:rgba(248,113,113,0.13); --ow-info-dim:rgba(96,165,250,0.13);
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

div[data-testid="stMetric"] {
  position:relative; background:linear-gradient(180deg,var(--ow-raised),var(--ow-surface));
  border:1px solid var(--ow-hairline); border-radius:var(--ow-r); padding:14px 16px 12px 18px;
  box-shadow:var(--ow-shadow); overflow:hidden; }
/* F13: the RESTING stripe is neutral (matching .ow-card::before) so a colored
   stripe always MEANS severity — the old default accent gradient looked semantic
   on every card and taught the eye that color carries nothing. */
div[data-testid="stMetric"]::before { content:""; position:absolute; left:0; top:0; bottom:0; width:3px;
  background:var(--ow-ink-mute); opacity:0.55; }
[data-testid="stMetricLabel"] p { font-size:0.76rem !important; letter-spacing:0.06em; text-transform:uppercase; color:var(--ow-ink-mute) !important; font-weight:640; }
/* F13: one KPI value size on both KPI surfaces (st.metric + metric_card_html). */
[data-testid="stMetricValue"] { font-size:1.55rem; font-weight:720; color:var(--ow-ink); }
.ow-sev-bad div[data-testid="stMetric"]::before { background:var(--ow-bad); opacity:1; }
.ow-sev-warn div[data-testid="stMetric"]::before { background:var(--ow-warn); opacity:1; }
.ow-sev-ok div[data-testid="stMetric"]::before { background:var(--ow-ok); opacity:1; }
.ow-sev-bad div[data-testid="stMetric"] { border-color:rgba(248,113,113,0.35); }

/* C22: no hover elevation on KPI/status cards — none of them is clickable, and
   hover motion falsely implies it. Interactive surfaces (buttons, rows, pills)
   keep their own hover states; a card that BECOMES a button earns hover back. */
.ow-card { position:relative; background:linear-gradient(180deg,var(--ow-raised),var(--ow-surface));
  border:1px solid var(--ow-hairline); border-radius:var(--ow-r); padding:14px 16px 14px 18px;
  box-shadow:var(--ow-shadow); margin-bottom:var(--ow-3); }
.ow-card::before { content:""; position:absolute; left:0; top:0; bottom:0; width:3px; border-radius:var(--ow-r) 0 0 var(--ow-r); background:var(--ow-ink-mute); }
.ow-card--ok::before { background:var(--ow-ok); } .ow-card--warn::before { background:var(--ow-warn); }
.ow-card--bad::before { background:var(--ow-bad); } .ow-card--info::before { background:var(--ow-info); }
.ow-src-badge { font-size:11px; letter-spacing:0.08em; text-transform:uppercase; border:1px solid; border-radius:8px; padding:1px 6px; white-space:nowrap; }
/* the chips group right-aligns and, on a long two-chip $ card, wraps to its own
   line instead of shrinking the label (the old float:right was dead on a flex child). */
.ow-card__chips { margin-left:auto; display:inline-flex; align-items:center; gap:6px; flex-wrap:wrap; justify-content:flex-end; }
.ow-src-badge--mart { color:var(--ow-ok); border-color:rgba(52,211,153,0.3); }
.ow-src-badge--live { color:var(--ow-accent); border-color:rgba(96,165,250,0.34); }
.ow-src-badge--stale { color:var(--ow-warn); border-color:rgba(245,158,11,0.34); }
.ow-src-badge--other { color:#8b98ad; border-color:rgba(139,152,173,0.3); }
.ow-src-badge--method { color:#c084fc; border-color:rgba(192,132,252,0.35); }  /* rec 13: how derived */
.ow-src-badge--scope { color:#a5b4cf; border-color:rgba(165,180,207,0.4); }    /* rec 13: account-wide / company */
.ow-card__title { font-size:0.76rem; letter-spacing:0.06em; text-transform:uppercase; color:var(--ow-ink-mute); font-weight:640; display:flex; align-items:center; gap:7px; }
.ow-card__value { font-size:1.55rem; font-weight:720; color:var(--ow-ink); margin-top:3px; font-variant-numeric:tabular-nums; }
.ow-card__meta { font-size:0.78rem; color:var(--ow-ink-soft); margin-top:2px; }

/* rec 15 (a11y): keyboard-focusable + touch-tappable KPI help. Replaces the
   hover-only card title= tooltip (invisible on touch, unreachable by keyboard).
   The affordance is a focusable '?' badge; the tooltip fires on hover AND focus,
   so Tab-users and touch-users both get it. CSP-safe (no JS): content:attr(). */
/* C28 (a11y): 24px hit target (was 18px). The negative vertical margin keeps the
   larger badge from growing the title row's line box, so card layout is unchanged. */
.ow-help { display:inline-flex; align-items:center; justify-content:center; width:24px; height:24px;
  margin:-4px 0; flex:0 0 auto; border-radius:var(--ow-r-pill); border:1px solid var(--ow-hairline2);
  color:var(--ow-ink-mute); font-size:0.75rem; font-weight:700; line-height:1; cursor:help;
  position:relative; outline:none; text-transform:none; letter-spacing:0; }
.ow-help:hover, .ow-help:focus-visible { color:var(--ow-ink); border-color:var(--ow-accent); }
.ow-help:focus-visible { box-shadow:0 0 0 2px rgba(96,165,250,0.45); }
/* F21: right-anchor the tooltip (the badge sits at the END of a card title, so a
   left-anchored 300px box overflowed the viewport on right-edge cards — exactly
   where the dense $ cards live) and clamp its width to the viewport. */
.ow-help[data-help]::after {
  content:attr(data-help); position:absolute; right:-4px; left:auto; top:calc(100% + 6px);
  min-width:200px; max-width:min(300px, 74vw); padding:8px 10px; border-radius:var(--ow-r-sm);
  background:var(--ow-raised); border:1px solid var(--ow-hairline2); color:var(--ow-ink-soft);
  font-size:0.72rem; line-height:1.42; font-weight:500; letter-spacing:0; text-transform:none;
  text-align:left; white-space:normal; box-shadow:var(--ow-shadow2); z-index:60;
  opacity:0; visibility:hidden; transition:opacity var(--ow-ease); pointer-events:none; }
.ow-help:hover::after, .ow-help:focus::after, .ow-help:focus-visible::after { opacity:1; visibility:visible; }

.ow-section { display:flex; align-items:center; gap:10px; margin:12px 0 8px; padding:8px 12px; border-radius:var(--ow-r-sm);
  border:1px solid var(--ow-hairline); border-left:3px solid var(--ow-ink-mute);
  background:linear-gradient(90deg,rgba(148,163,184,0.08),rgba(148,163,184,0.02) 64%,transparent); }
.ow-section--ok { border-left-color:var(--ow-ok); background:linear-gradient(90deg,var(--ow-ok-dim),transparent 60%); }
.ow-section--warn { border-left-color:var(--ow-warn); background:linear-gradient(90deg,var(--ow-warn-dim),transparent 60%); }
.ow-section--bad { border-left-color:var(--ow-bad); background:linear-gradient(90deg,var(--ow-bad-dim),transparent 60%); }
.ow-section--info { border-left-color:var(--ow-info); background:linear-gradient(90deg,var(--ow-info-dim),transparent 60%); }
.ow-section__title { font-weight:700; color:var(--ow-ink); font-size:1.02rem; }
.ow-section__icon { display:inline-flex; color:var(--ow-ink-soft); }
.ow-section__badge { margin-left:auto; font-size:0.72rem; font-weight:650; letter-spacing:0.04em; text-transform:uppercase; padding:2px 9px; border-radius:var(--ow-r-pill); border:1px solid var(--ow-hairline2); color:var(--ow-ink-soft); }
/* F19: when a section carries real severity, the glyph and count badge read it too —
   the old stripe-only tint left the header 97% neutral on a red section. */
.ow-section--ok .ow-section__icon { color:var(--ow-ok); }
.ow-section--warn .ow-section__icon { color:var(--ow-warn); }
.ow-section--bad .ow-section__icon { color:var(--ow-bad); }
.ow-section--info .ow-section__icon { color:var(--ow-info); }
.ow-section--ok .ow-section__badge { color:var(--ow-ok); border-color:rgba(52,211,153,0.42); }
.ow-section--warn .ow-section__badge { color:var(--ow-warn); border-color:rgba(245,158,11,0.42); }
.ow-section--bad .ow-section__badge { color:var(--ow-bad); border-color:rgba(248,113,113,0.42); }
.ow-section--info .ow-section__badge { color:var(--ow-info); border-color:rgba(96,165,250,0.42); }

.ow-filter-contract { margin:-2px 0 10px 0; padding:5px 10px; border-left:2px solid var(--ow-info);
  color:var(--ow-ink-mute); background:rgba(96,165,250,0.07); font-size:0.72rem; line-height:1.45; }

.ow-exceptions { margin:4px 0 10px; border-top:1px solid var(--ow-hairline);
  border-bottom:1px solid var(--ow-hairline); }
.ow-exception { display:grid; grid-template-columns:minmax(120px,1fr) auto minmax(180px,2fr);
  gap:12px; align-items:center; padding:7px 10px; border-left:3px solid var(--ow-warn);
  border-bottom:1px solid var(--ow-hairline); background:rgba(245,158,11,0.055); }
.ow-exception:last-child { border-bottom:0; }
.ow-exception--bad { border-left-color:var(--ow-bad); background:rgba(248,113,113,0.06); }
.ow-exception--ok { border-left-color:var(--ow-ok); background:rgba(52,211,153,0.045); }
/* page verdict line — one 'should I worry?' opener above a page (CoCo do-first #1) */
.ow-verdict { display:flex; flex-wrap:wrap; gap:6px 10px; align-items:baseline;
  padding:9px 13px; margin:0 0 12px; border-left:3px solid var(--ow-info);
  border-radius:8px; background:var(--ow-info-dim); font-size:0.94rem; line-height:1.45; }
.ow-verdict__label { font-weight:700; letter-spacing:.01em; white-space:nowrap; color:var(--ow-info); }
.ow-verdict--ok { border-left-color:var(--ow-ok); background:var(--ow-ok-dim); }
.ow-verdict--ok .ow-verdict__label { color:var(--ow-ok); }
.ow-verdict--warn { border-left-color:var(--ow-warn); background:var(--ow-warn-dim); }
.ow-verdict--warn .ow-verdict__label { color:var(--ow-warn); }
.ow-verdict--bad { border-left-color:var(--ow-bad); background:var(--ow-bad-dim); }
.ow-verdict--bad .ow-verdict__label { color:var(--ow-bad); }
/* persistent contract-runway bar (CoCo Overview #20 / Cost #4) */
.ow-runway { margin:0 0 14px; }
.ow-runway__track { height:8px; border-radius:6px; background:var(--ow-info-dim); overflow:hidden; }
.ow-runway__fill { height:100%; border-radius:6px; background:var(--ow-info); min-width:2px; transition:width .3s ease; }
.ow-runway__label { margin-top:5px; font-size:0.8rem; color:var(--ow-ink); opacity:.82; }
.ow-runway--ok .ow-runway__fill { background:var(--ow-ok); }
.ow-runway--warn .ow-runway__fill { background:var(--ow-warn); }
.ow-runway--bad .ow-runway__fill { background:var(--ow-bad); }
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
.ow-stat::before { content:""; position:absolute; left:0; top:0; bottom:0; width:3px; border-radius:var(--ow-r-sm) 0 0 var(--ow-r-sm); background:var(--ow-accent); }
.ow-stat--ok::before { background:var(--ow-ok); } .ow-stat--warn::before { background:var(--ow-warn); }
.ow-stat--bad::before { background:var(--ow-bad); } .ow-stat--info::before { background:var(--ow-info); }
.ow-stat__k { font-size:0.72rem; letter-spacing:0.06em; text-transform:uppercase; color:var(--ow-ink-mute); font-weight:640; }
.ow-stat__v { font-size:1.04rem; font-weight:720; color:var(--ow-ink); font-variant-numeric:tabular-nums; display:flex; align-items:center; gap:6px; }
.ow-stat__spark { margin-top:2px; opacity:0.9; }
.st-key-ow_status_actions { margin-top:-14px; margin-bottom:8px; }
.st-key-ow_status_actions button { min-height:1.7rem; font-size:0.72rem; }

/* Chip pills (chip() helper — scope summary in the status bar + severity pills).
   The scope-chip BAND in the filter strip was retired in v4.65 for the compact
   toolbar; the active-filter border glow below stays. Token layer only. */
.ow-chip{display:inline-flex;align-items:center;gap:6px;padding:2px 10px;
  border-radius:var(--ow-r-pill);font-size:0.72rem;font-weight:600;
  letter-spacing:.02em;line-height:1.55;border:1px solid var(--ow-hairline2);
  color:var(--ow-ink-soft);background:rgba(148,163,184,0.07);margin:0 6px 4px 0;}
.ow-chip b{color:var(--ow-ink);font-weight:700;}
.ow-chip-ok{color:var(--ow-ok);border-color:rgba(52,211,153,0.42);background:var(--ow-ok-dim);}
.ow-chip-bad{color:var(--ow-bad);border-color:rgba(248,113,113,0.42);background:var(--ow-bad-dim);}
.ow-chip-warn{color:var(--ow-warn);border-color:rgba(245,158,11,0.42);background:var(--ow-warn-dim);}
.ow-scope-row{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin:4px 0 8px;}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.ow-scope-active){
  border-color:rgba(96,165,250,0.38);
  box-shadow:0 0 0 1px rgba(96,165,250,0.20),var(--ow-shadow);}
.st-key-ow_triage_toolbar{margin-bottom:8px;}
.st-key-ow_triage_toolbar [data-testid="stVerticalBlock"]{gap:0.22rem;}
.st-key-ow_triage_toolbar [data-testid="stHorizontalBlock"]{gap:0.42rem;}
.st-key-ow_triage_toolbar div[data-baseweb="select"]>div{min-height:2.28rem;}
.st-key-ow_triage_toolbar button{min-height:2.28rem;padding-left:0.65rem;padding-right:0.65rem;}
.ow-triage-title{height:2.28rem;display:flex;flex-direction:column;justify-content:center;
  color:var(--ow-ink);font-size:0.73rem;font-weight:760;letter-spacing:0.06em;text-transform:uppercase;}
.ow-triage-title small{display:block;color:var(--ow-ink-mute)!important;font-size:0.66rem;
  font-weight:600;letter-spacing:0;text-transform:none;white-space:nowrap;}
.ow-kicker { font-size:0.75rem; letter-spacing:0; font-weight:750; color:var(--ow-ink-mute); text-transform:uppercase; margin-bottom:0.1rem; }
.ow-brand { display:flex; align-items:center; gap:9px; }
/* C4: the pulse now MEANS "connected to Snowflake" — _sidebar binds the class to
   the live connection, and the disconnected dot is static grey, so the animation
   no longer implies a liveness the app isn't asserting. */
.ow-brand-dot { width:11px; height:11px; border-radius:999px;
  background:radial-gradient(circle at 30% 30%,var(--ow-accent2),var(--ow-accent));
  box-shadow:0 0 10px rgba(96,165,250,0.72),0 0 2px rgba(45,212,191,0.82); animation:ow-pulse 2.8s ease-in-out infinite; }
.ow-brand-dot--off { background:var(--ow-ink-mute); box-shadow:none; animation:none; opacity:0.6; }
@keyframes ow-pulse { 0%,100% { opacity:1; } 50% { opacity:0.55; } }
/* F22: solid-ink base; the gradient text-clip applies ONLY where the engine
   supports it — otherwise the wordmark rendered as a transparent hole (the
   single brand anchor in the chrome). */
.ow-brand-word { font-weight:850; letter-spacing:0.08em; font-size:1.55rem; line-height:1.02;
  color:var(--ow-ink); }
@supports ((-webkit-background-clip:text) or (background-clip:text)) {
  .ow-brand-word { background:linear-gradient(90deg,var(--ow-ink),var(--ow-accent));
    -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text; }
}
.ow-brand-sub { font-size:0.72rem; font-weight:600; color:var(--ow-ink-mute);
  letter-spacing:0.04em; margin:1px 0 0 20px; text-transform:uppercase; }

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
div[role="radiogroup"][aria-label="Section"] label:hover { background:rgba(148,163,184,0.10); }
div[role="radiogroup"][aria-label="Section"] label:has(input:checked) {
  background:linear-gradient(180deg,var(--ow-accent2),var(--ow-accent)); color:#0f172a; }

.stButton > button { border-radius:var(--ow-r-sm); border:1px solid var(--ow-hairline2); font-weight:620;
  transition:transform var(--ow-ease),box-shadow var(--ow-ease),border-color var(--ow-ease); }
.stButton > button:hover { border-color:var(--ow-accent); box-shadow:0 6px 18px -10px rgba(96,165,250,0.52); }
/* F14 (a11y): ONE keyboard-focus grammar on every interactive control. Before
   this, only the section pill-group and the help badge had a focus rule — a Tab
   user lost the indicator on Execute buttons, nav radios, and every scope
   select/input in a keyboard-heavy DBA tool. */
.stButton > button:focus-visible, .stDownloadButton > button:focus-visible,
.stTextInput input:focus-visible, .stNumberInput input:focus-visible,
.stTextArea textarea:focus-visible {
  outline:2px solid var(--ow-accent) !important; outline-offset:2px; }
div[data-baseweb="select"]:focus-within,
.stMultiSelect [data-baseweb="select"]:focus-within {
  outline:2px solid var(--ow-accent); outline-offset:1px; border-radius:var(--ow-r-sm); }
div[role="radiogroup"] label:has(input:focus-visible),
section[data-testid="stSidebar"] div[role="radiogroup"] label:has(input:focus-visible),
.stCheckbox label:has(input:focus-visible), .stToggle label:has(input:focus-visible) {
  outline:2px solid var(--ow-accent); outline-offset:2px; }
.stButton > button[kind="primary"],
.stButton > button[data-testid="stBaseButton-primary"],
button[data-testid="stBaseButton-primary"],
button[data-testid="baseButton-primary"] {
  background:linear-gradient(180deg,var(--ow-accent2),var(--ow-accent)) !important;
  color:#0f172a !important; border:none !important; }
/* SiS builds vary the button markup; force dark ink on every descendant so
   an accent pill can never render pale-on-pale (live finding 2026-07-10:
   the '2 open critical(s)' chip and Execute bulk RESOLVE were unreadable). */
.stButton > button[kind="primary"] p, .stButton > button[kind="primary"] span,
button[data-testid="stBaseButton-primary"] p, button[data-testid="stBaseButton-primary"] span {
  color:#0f172a !important; }

button[data-baseweb="tab"] { font-weight:640; }

/* Multiselect chips: the default BaseWeb tag rendered as a pale wash —
   selections were unreadable (live finding 2026-07-10, Alerts bulk picker).
   Dark chip, accent hairline, real text. */
.stMultiSelect [data-baseweb="tag"] { background:rgba(96,165,250,0.16) !important;
  border:1px solid rgba(96,165,250,0.52) !important; border-radius:var(--ow-r-sm); }
.stMultiSelect [data-baseweb="tag"] span { color:#dbeafe !important; }
.stMultiSelect [data-baseweb="tag"] svg { fill:#dbeafe !important; }
[data-testid="stDataFrame"] { border:1px solid var(--ow-hairline); border-radius:var(--ow-r-sm); overflow:hidden; box-shadow:var(--ow-shadow); }
[data-testid="stExpander"] { border:1px solid var(--ow-hairline); border-radius:var(--ow-r-sm); background:var(--ow-surface); }
[data-testid="stExpander"] summary:hover { color:var(--ow-accent); }
div[data-testid="stPopover"] > button { border-radius:var(--ow-r-pill); }

section[data-testid="stSidebar"] { background:linear-gradient(180deg,var(--ow-bg),var(--ow-surface)); }
section[data-testid="stSidebar"] div[role="radiogroup"] label { border-radius:var(--ow-r-sm); padding:4px 10px; margin:1px 0; transition:background var(--ow-ease); }
section[data-testid="stSidebar"] div[role="radiogroup"] label:hover { background:rgba(148,163,184,0.10); }
section[data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked) {
  background:linear-gradient(90deg,rgba(96,165,250,0.18),transparent); box-shadow:inset 3px 0 0 var(--ow-accent); }

@media (max-width:640px) {
  .block-container { padding-left:0.6rem; padding-right:0.6rem; }
  [data-testid="stMetricValue"] { font-size:1.32rem; }
  .ow-stat { flex-basis:46%; }
}
/* C42/C47: master-detail (feed/list LEFT, drawer/detail RIGHT via st.columns).
   Streamlit columns are flex children that do NOT auto-stack until they hit a
   tiny built-in min-width — a 2-pane split would shrink to unusable widths
   first — so restack them explicitly on narrow viewports. The generous 1180px
   breakpoint keys off VIEWPORT width while the content area is viewport minus
   the sidebar, so it fires before the panes cramp. `st-key-ow_md_*` is the
   shared container key every master-detail surface wraps its columns in. */
@media (max-width:1180px) {
  [class*="st-key-ow_md_"] [data-testid="stHorizontalBlock"] { flex-wrap:wrap; }
  [class*="st-key-ow_md_"] [data-testid="stColumn"] {
    flex:1 1 100% !important; min-width:100% !important; width:100% !important;
  }
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
.ow-section { padding:4px 10px; margin:4px 0; }
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
