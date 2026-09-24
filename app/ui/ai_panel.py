"""Reusable, button-gated AI evaluation panel."""

from __future__ import annotations

from collections.abc import Callable

import streamlit as st

from app.core.ai import cortex_complete, normalize_model
from app.logic.ai_grounding import check_grounding, evidence_section
from app.logic.formulas import safe_float
from app.ui.components import download_text_button, log_ui_event, notify, status_chips


def ai_evaluation_panel(*, key: str, prompt: str, settings: dict, page: str,
                        subject: str,
                        on_save: Callable[[str], tuple[bool, str]] | None = None,
                        save_label: str = "Save evaluation",
                        evidence: str | None = None) -> None:
    """Render an expander that runs a grounded Cortex evaluation on demand.

    Never auto-runs. Shows the model, the credit warning, and the exact
    grounding prompt for audit. Answers are downloadable.

    ``on_save`` (optional): a persist callback that receives the AI answer text
    and returns ``(ok, message)``. When supplied, a ``save_label`` button appears
    under a successful answer; clicking it invokes the callback and reports the
    result via ``notify``. Callers use this to write the answer somewhere durable
    (e.g. append it to an alert event). Backward-compatible: omit it and no save
    button renders.

    ``evidence`` (optional): the text the answer's $/% figures are checked against
    (Next-Fifty #24). Defaults to the prompt's evidence section (ai_grounding.evidence_section). The check is
    ADVISORY — figures with no match get a warn chip; the answer always renders unchanged.
    """
    # AIP-3: normalize for DISPLAY so the credit caption, the audit caption, and the
    # downloaded .txt name the model that cortex_complete will ACTUALLY run — a stored
    # CORTEX_MODEL with a space/underscore (edited via the unvalidated free-text input)
    # silently falls back to the default inside cortex_complete, and attributing the answer
    # to a model that never executed is an audit drift in an audit tool.
    model = normalize_model(settings.get("CORTEX_MODEL") or "llama3.1-8b")
    ai_rate = safe_float(settings.get("AI_CREDIT_PRICE_USD"), 2.20)
    state_key = f"_ai_answer_{key}"
    ground_key = f"_ai_ground_{key}"
    # the figures are checked against the EVIDENCE part only — the instruction text ('max 5',
    # 'last 7 days') must never license a figure (adversarial review of #24)
    ground_text = evidence_section(prompt) if evidence is None else evidence
    with st.expander(f"AI evaluation — {subject}"):
        st.caption(
            f"Runs SNOWFLAKE.CORTEX.COMPLETE ('{model}', from SETTINGS) over exactly the evidence "
            f"rows shown above. Each run consumes Cortex credits (billed at ~${ai_rate:.2f}/credit)."
        )
        if st.button("Generate AI evaluation", key=f"ai_btn_{key}"):
            with st.spinner("Asking Cortex..."):
                ok, answer = cortex_complete(prompt, model, page=page)
            st.session_state[state_key] = (ok, answer)
            _check = check_grounding(answer, ground_text) if ok else None
            st.session_state[ground_key] = _check
            if ok:
                # Logged ONCE per generation (never per rerun): Admin's usage-by-event panel then shows
                # ai_eval vs ai_ungrounded — the measured ungrounded-answer rate for judging CORTEX_MODEL.
                log_ui_event("ai_eval", page=page)
                if _check is not None and _check.ungrounded:
                    log_ui_event("ai_ungrounded", page=page)
        stored = st.session_state.get(state_key)
        if stored:
            ok, answer = stored
            if ok:
                from app.logic.formulas import md_dollars
                # $-escape: LLM cost answers routinely contain multiple $ amounts,
                # which pair into LaTeX math spans and garble the narrative.
                st.markdown(md_dollars(answer))
                # An answer stored before this check shipped has no ground_key entry: compute it at
                # render time (no usage event, so the ungrounded rate is never double-counted).
                _check = st.session_state.get(ground_key) or check_grounding(answer, ground_text)
                if _check.ungrounded:
                    _n = len(_check.ungrounded)
                    status_chips([(f"{_n} figure{'s' if _n != 1 else ''} not found in the evidence "
                                   "— may be derived; verify", "warn")])
                    _shown = ", ".join(_check.ungrounded[:6]) + (f" +{_n - 6} more" if _n > 6 else "")
                    st.caption(md_dollars(f"Not in the evidence rows: {_shown}. Sums, annualized or re-rounded "
                                          "figures are legitimate — check them against the table above before acting."))
                _note = ("no $ or % figures to check" if _check.checked == 0 else
                         f"all {_check.checked} $/% figure(s) found in the evidence" if _check.ok else
                         f"{len(_check.ungrounded)} of {_check.checked} $/% figure(s) not found in the evidence")
                st.caption(md_dollars(f"Model: {model} · prompted with the on-screen evidence only · {_note} "
                                      "· verify before acting."))
                download_text_button("Download evaluation (.txt)", answer, f"overwatch_ai_{key}.txt")
                if on_save is not None and st.button(save_label, key=f"ai_save_{key}"):
                    ok_s, msg_s = on_save(answer)
                    if msg_s:
                        notify(ok_s, msg_s)
            else:
                st.error(f"AI evaluation failed: {answer}")
                st.caption("Check that the role has SNOWFLAKE.CORTEX_USER and the model is enabled in this region.")
        with st.popover("Show grounding prompt"):
            st.code(prompt, language="text")
