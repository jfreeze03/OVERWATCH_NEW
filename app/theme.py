"""Minimal styling. ~60 lines, not the old app's 1,192-line CSS fork.

Native Streamlit components (st.metric, st.tabs, st.dataframe) carry the UI;
CSS only tightens spacing and styles the two custom chips we actually need.
"""

from __future__ import annotations

import streamlit as st

_CSS = """
<style>
  .block-container { padding-top: 1.2rem; padding-bottom: 2rem; }
  div[data-testid="stMetric"] {
      background: rgba(148, 163, 184, 0.07);
      border: 1px solid rgba(148, 163, 184, 0.18);
      border-radius: 10px;
      padding: 10px 14px;
  }
  .ow-chip {
      display: inline-block; padding: 2px 10px; margin-right: 6px;
      border-radius: 999px; font-size: 0.72rem; font-weight: 600;
      border: 1px solid rgba(148,163,184,0.35); color: #94a3b8;
  }
  .ow-chip-ok { color: #34d399; border-color: rgba(52,211,153,0.5); }
  .ow-chip-bad { color: #f87171; border-color: rgba(248,113,113,0.5); }
  .ow-kicker {
      font-size: 0.72rem; letter-spacing: 0.14em; font-weight: 700;
      color: #64748b; text-transform: uppercase; margin-bottom: 0.1rem;
  }
  .ow-brand { display: flex; align-items: center; gap: 8px; }
  .ow-brand-dot {
      width: 10px; height: 10px; border-radius: 999px; background: #38bdf8;
      box-shadow: 0 0 8px rgba(56, 189, 248, 0.8);
  }
  .ow-scope-row { margin: 2px 0 6px 0; }
  button[data-baseweb="tab"] { font-weight: 600; }
</style>
"""


def inject_theme() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)


def chip(text: str, state: str = "") -> str:
    cls = {"ok": "ow-chip ow-chip-ok", "bad": "ow-chip ow-chip-bad"}.get(state, "ow-chip")
    return f'<span class="{cls}">{text}</span>'
