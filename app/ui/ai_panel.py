"""Reusable, button-gated AI evaluation panel."""

from __future__ import annotations

import streamlit as st

from app.core.ai import cortex_complete
from app.logic.formulas import safe_float
from app.ui.components import download_text_button


def ai_evaluation_panel(*, key: str, prompt: str, settings: dict, page: str,
                        subject: str) -> None:
    """Render an expander that runs a grounded Cortex evaluation on demand.

    Never auto-runs. Shows the model, the credit warning, and the exact
    grounding prompt for audit. Answers are downloadable.
    """
    model = str(settings.get("CORTEX_MODEL") or "llama3.1-8b")
    ai_rate = safe_float(settings.get("AI_CREDIT_PRICE_USD"), 2.20)
    state_key = f"_ai_answer_{key}"
    with st.expander(f"AI evaluation — {subject}"):
        st.caption(
            f"Runs SNOWFLAKE.CORTEX.COMPLETE ('{model}', from SETTINGS) over exactly the evidence "
            f"rows shown above. Each run consumes Cortex credits (billed at ~${ai_rate:.2f}/credit)."
        )
        if st.button("Generate AI evaluation", key=f"ai_btn_{key}"):
            with st.spinner("Asking Cortex..."):
                ok, answer = cortex_complete(prompt, model, page=page)
            st.session_state[state_key] = (ok, answer)
        stored = st.session_state.get(state_key)
        if stored:
            ok, answer = stored
            if ok:
                st.markdown(answer)
                st.caption(f"Model: {model} · grounded in the on-screen evidence only · verify before acting.")
                download_text_button("Download evaluation (.txt)", answer, f"overwatch_ai_{key}.txt")
            else:
                st.error(f"AI evaluation failed: {answer}")
                st.caption("Check that the role has SNOWFLAKE.CORTEX_USER and the model is enabled in this region.")
        with st.popover("Show grounding prompt"):
            st.code(prompt, language="text")
