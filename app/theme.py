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
  --ow-bg:#0a0f1c; --ow-surface:#0f1729; --ow-raised:#131d33;
  --ow-hairline:rgba(148,163,184,0.16); --ow-hairline2:rgba(148,163,184,0.28);
  --ow-ink:#e8eef7; --ow-ink-soft:#aab6c8; --ow-ink-mute:#6b7a90;
  --ow-accent:#38bdf8; --ow-accent2:#22d3ee;
  --ow-ok:#34d399; --ow-warn:#fbbf24; --ow-bad:#fb7185; --ow-info:#38bdf8;
  --ow-ok-dim:rgba(52,211,153,0.14); --ow-warn-dim:rgba(251,191,36,0.14);
  --ow-bad-dim:rgba(251,113,133,0.14); --ow-info-dim:rgba(56,189,248,0.14);
  --ow-1:4px; --ow-2:8px; --ow-3:12px; --ow-4:16px; --ow-5:24px; --ow-6:32px;
  --ow-r:12px; --ow-r-sm:8px; --ow-r-lg:16px; --ow-r-pill:999px;
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
h1,h2,h3,h4 { letter-spacing:-0.015em; color:var(--ow-ink); }
h1 { font-weight:750; font-size:1.72rem; } h2 { font-weight:700; }
h3 { font-weight:680; font-size:1.06rem; }
p,li,span,label,.stMarkdown { color:var(--ow-ink-soft); }
[data-testid="stCaptionContainer"],.stCaption,small { color:var(--ow-ink-mute) !important; }
[data-testid="stMetricValue"],.ow-num,td,th { font-variant-numeric:tabular-nums; }

div[data-testid="stMetric"] {
  position:relative; background:linear-gradient(180deg,var(--ow-raised),var(--ow-surface));
  border:1px solid var(--ow-hairline); border-radius:var(--ow-r); padding:14px 16px 12px 18px;
  box-shadow:var(--ow-shadow); transition:transform var(--ow-ease),box-shadow var(--ow-ease),border-color var(--ow-ease); overflow:hidden; }
div[data-testid="stMetric"]::before { content:""; position:absolute; left:0; top:0; bottom:0; width:3px;
  background:linear-gradient(180deg,var(--ow-accent2),var(--ow-accent)); opacity:0.85; }
div[data-testid="stMetric"]:hover { transform:translateY(-2px); box-shadow:var(--ow-shadow2); border-color:var(--ow-hairline2); }
[data-testid="stMetricLabel"] p { font-size:0.70rem !important; letter-spacing:0.06em; text-transform:uppercase; color:var(--ow-ink-mute) !important; font-weight:640; }
[data-testid="stMetricValue"] { font-size:1.62rem; font-weight:720; color:var(--ow-ink); }
.ow-sev-bad div[data-testid="stMetric"]::before { background:var(--ow-bad); opacity:1; }
.ow-sev-warn div[data-testid="stMetric"]::before { background:var(--ow-warn); opacity:1; }
.ow-sev-ok div[data-testid="stMetric"]::before { background:var(--ow-ok); opacity:1; }
.ow-sev-bad div[data-testid="stMetric"] { border-color:rgba(251,113,133,0.35); }

.ow-card { position:relative; background:linear-gradient(180deg,var(--ow-raised),var(--ow-surface));
  border:1px solid var(--ow-hairline); border-radius:var(--ow-r); padding:14px 16px 14px 18px;
  box-shadow:var(--ow-shadow); margin-bottom:var(--ow-3); transition:transform var(--ow-ease),box-shadow var(--ow-ease); }
.ow-card:hover { transform:translateY(-1px); box-shadow:var(--ow-shadow2); }
.ow-card::before { content:""; position:absolute; left:0; top:0; bottom:0; width:3px; border-radius:var(--ow-r) 0 0 var(--ow-r); background:var(--ow-ink-mute); }
.ow-card--ok::before { background:var(--ow-ok); } .ow-card--warn::before { background:var(--ow-warn); }
.ow-card--bad::before { background:var(--ow-bad); } .ow-card--info::before { background:var(--ow-info); }
.ow-card__title { font-size:0.70rem; letter-spacing:0.06em; text-transform:uppercase; color:var(--ow-ink-mute); font-weight:640; display:flex; align-items:center; gap:7px; }
.ow-card__value { font-size:1.55rem; font-weight:720; color:var(--ow-ink); margin-top:3px; font-variant-numeric:tabular-nums; }
.ow-card__meta { font-size:0.78rem; color:var(--ow-ink-soft); margin-top:2px; }

.ow-section { display:flex; align-items:center; gap:10px; margin:6px 0 6px 0; padding:6px 12px; border-radius:var(--ow-r-sm);
  border-left:3px solid var(--ow-ink-mute); background:linear-gradient(90deg,rgba(148,163,184,0.06),transparent 60%); }
.ow-section--ok { border-left-color:var(--ow-ok); background:linear-gradient(90deg,var(--ow-ok-dim),transparent 60%); }
.ow-section--warn { border-left-color:var(--ow-warn); background:linear-gradient(90deg,var(--ow-warn-dim),transparent 60%); }
.ow-section--bad { border-left-color:var(--ow-bad); background:linear-gradient(90deg,var(--ow-bad-dim),transparent 60%); }
.ow-section--info { border-left-color:var(--ow-info); background:linear-gradient(90deg,var(--ow-info-dim),transparent 60%); }
.ow-section__title { font-weight:700; color:var(--ow-ink); font-size:1.02rem; }
.ow-section__icon { display:inline-flex; color:var(--ow-ink-soft); }
.ow-section__badge { margin-left:auto; font-size:0.7rem; font-weight:650; letter-spacing:0.04em; text-transform:uppercase; padding:2px 9px; border-radius:var(--ow-r-pill); border:1px solid var(--ow-hairline2); color:var(--ow-ink-soft); }

.ow-statusbar { display:flex; gap:8px; flex-wrap:wrap; align-items:stretch; margin:0 0 12px 0; }
.ow-stat { flex:1 1 130px; min-width:120px; position:relative; background:linear-gradient(180deg,var(--ow-raised),var(--ow-surface));
  border:1px solid var(--ow-hairline); border-radius:var(--ow-r-sm); padding:8px 12px 8px 14px; box-shadow:var(--ow-shadow); }
.ow-stat::before { content:""; position:absolute; left:0; top:0; bottom:0; width:3px; border-radius:var(--ow-r-sm) 0 0 var(--ow-r-sm); background:var(--ow-accent); }
.ow-stat--ok::before { background:var(--ow-ok); } .ow-stat--warn::before { background:var(--ow-warn); }
.ow-stat--bad::before { background:var(--ow-bad); } .ow-stat--info::before { background:var(--ow-info); }
.ow-stat__k { font-size:0.62rem; letter-spacing:0.06em; text-transform:uppercase; color:var(--ow-ink-mute); font-weight:640; }
.ow-stat__v { font-size:1.04rem; font-weight:720; color:var(--ow-ink); font-variant-numeric:tabular-nums; display:flex; align-items:center; gap:6px; }
.ow-stat__spark { margin-top:2px; opacity:0.9; }

.ow-chip { display:inline-flex; align-items:center; gap:5px; padding:2px 10px; margin:0 6px 4px 0; border-radius:var(--ow-r-pill);
  font-size:0.72rem; font-weight:620; border:1px solid var(--ow-hairline2); color:var(--ow-ink-soft); background:rgba(148,163,184,0.05); }
.ow-chip-ok { color:var(--ow-ok); border-color:rgba(52,211,153,0.45); background:var(--ow-ok-dim); }
.ow-chip-bad { color:var(--ow-bad); border-color:rgba(251,113,133,0.45); background:var(--ow-bad-dim); }
.ow-chip-warn { color:var(--ow-warn); border-color:rgba(251,191,36,0.45); background:var(--ow-warn-dim); }

.ow-kicker { font-size:0.68rem; letter-spacing:0.18em; font-weight:750; color:var(--ow-ink-mute); text-transform:uppercase; margin-bottom:0.1rem; }
.ow-brand { display:flex; align-items:center; gap:9px; }
.ow-brand-dot { width:11px; height:11px; border-radius:999px;
  background:radial-gradient(circle at 30% 30%,var(--ow-accent2),var(--ow-accent));
  box-shadow:0 0 10px rgba(56,189,248,0.9),0 0 2px rgba(56,189,248,1); animation:ow-pulse 2.8s ease-in-out infinite; }
@keyframes ow-pulse { 0%,100% { opacity:1; } 50% { opacity:0.55; } }
.ow-brand-word { font-weight:800; letter-spacing:0.02em;
  background:linear-gradient(90deg,var(--ow-ink),var(--ow-accent)); -webkit-background-clip:text;
  -webkit-text-fill-color:transparent; background-clip:text; }

div[role="radiogroup"][aria-label="Section"], div[role="radiogroup"][aria-label="Window"] {
  gap:4px; padding:4px; background:var(--ow-surface); border:1px solid var(--ow-hairline);
  border-radius:var(--ow-r-pill); overflow-x:auto; scrollbar-width:thin; flex-wrap:nowrap; }
div[role="radiogroup"][aria-label="Section"] label, div[role="radiogroup"][aria-label="Window"] label {
  border-radius:var(--ow-r-pill); padding:3px 12px; margin:0; white-space:nowrap; transition:background var(--ow-ease),color var(--ow-ease); }
div[role="radiogroup"][aria-label="Section"] label:hover { background:rgba(148,163,184,0.10); }
div[role="radiogroup"][aria-label="Section"] label:has(input:checked) {
  background:linear-gradient(180deg,var(--ow-accent2),var(--ow-accent)); color:#06121f; }

.stButton > button { border-radius:var(--ow-r-sm); border:1px solid var(--ow-hairline2); font-weight:620;
  transition:transform var(--ow-ease),box-shadow var(--ow-ease),border-color var(--ow-ease); }
.stButton > button:hover { transform:translateY(-1px); border-color:var(--ow-accent); box-shadow:0 6px 18px -10px rgba(56,189,248,0.6); }
.stButton > button[kind="primary"] { background:linear-gradient(180deg,var(--ow-accent2),var(--ow-accent)); color:#06121f; border:none; }

button[data-baseweb="tab"] { font-weight:640; }
[data-testid="stDataFrame"] { border:1px solid var(--ow-hairline); border-radius:var(--ow-r-sm); overflow:hidden; box-shadow:var(--ow-shadow); }
[data-testid="stExpander"] { border:1px solid var(--ow-hairline); border-radius:var(--ow-r-sm); background:var(--ow-surface); }
[data-testid="stExpander"] summary:hover { color:var(--ow-accent); }
div[data-testid="stPopover"] > button { border-radius:var(--ow-r-pill); }

section[data-testid="stSidebar"] { background:linear-gradient(180deg,var(--ow-bg),var(--ow-surface)); }
section[data-testid="stSidebar"] div[role="radiogroup"] label { border-radius:var(--ow-r-sm); padding:4px 10px; margin:1px 0; transition:background var(--ow-ease); }
section[data-testid="stSidebar"] div[role="radiogroup"] label:hover { background:rgba(148,163,184,0.10); }
section[data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked) {
  background:linear-gradient(90deg,rgba(56,189,248,0.18),transparent); box-shadow:inset 3px 0 0 var(--ow-accent); }

@media (max-width:640px) {
  .block-container { padding-left:0.6rem; padding-right:0.6rem; }
  [data-testid="stMetricValue"] { font-size:1.32rem; }
  .ow-stat { flex-basis:46%; }
}
@media (prefers-reduced-motion:reduce) { *,*::before { transition:none !important; animation:none !important; } }
</style>
"""


def inject_theme() -> None:
    """Inject tokens + component CSS once per render. Cheap; no network."""
    st.markdown(_TOKENS + _CSS, unsafe_allow_html=True)


def chip(text: str, state: str = "") -> str:
    cls = {"ok": "ow-chip ow-chip-ok", "bad": "ow-chip ow-chip-bad",
           "warn": "ow-chip ow-chip-warn"}.get(state, "ow-chip")
    return f'<span class="{cls}">{text}</span>'
