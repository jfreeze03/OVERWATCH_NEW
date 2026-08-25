"""Ask-OVERWATCH — the page (self-contained; the only file touching Streamlit).

The ONLY file in the feature that touches Streamlit and app.core.query.run. It
routes the question (app.logic.ask.route), runs the chosen answerer's builder
specs, hands the frames to the pure analyze(), and renders a grounded answer —
or an honest refusal. Optional Cortex phrasing only ever *rewords* the already-
grounded result; it is OFF by default and invents nothing.

Revert: see the authoritative REVERT PATH in app/logic/ask/__init__.py (delete
3 new paths — incl. this file — and revert the 3 marked "ASK-OVERWATCH" blocks).
"""

from __future__ import annotations

import streamlit as st

from app.core.errors import safe_page
from app.core.query import run
from app.core.sqlsafe import sql_literal
from app.core.state import filters
from app.logic.ask import REGISTRY, route
from app.logic.ask.types import AnswerResult, AskParams
from app.ui.components import load_settings, page_header

_PAGE = "Ask"


def _capabilities(heading: str) -> None:
    st.markdown(heading)
    for ans in REGISTRY:
        st.markdown(f"- **{ans.title}** — e.g. _{ans.examples[0]}_")


# One-click test gallery — every registered phrasing plus a deliberately
# unmapped probe, so the feature can be exercised end to end without typing.
_REFUSAL_PROBE = "how many failed logins were there today"


def _test_cases() -> list[str]:
    cases: list[str] = []
    for ans in REGISTRY:
        cases.extend(ans.examples)
    cases.append(_REFUSAL_PROBE)  # should return the honest refusal
    return cases


def _ai_phrasing(result: AnswerResult, model: str) -> str | None:
    """Reword the grounded result via Cortex. Given ONLY the deterministic
    finding and told to change no number/name. Degrades silently to None."""
    grounded = result.headline + "\n" + "\n".join(f"- {b}" for b in result.bullets)
    prompt = (
        "Reword the following grounded analytics finding in 1-2 short, plain "
        "sentences for a database administrator. Do NOT add, remove, or change "
        "any number, name, or percentage. Invent nothing beyond what is stated.\n\n"
        + grounded
    )
    try:
        sql = (
            f"SELECT SNOWFLAKE.CORTEX.COMPLETE({sql_literal(model)}, "
            f"{sql_literal(prompt)}) AS TXT"
        )
        res = run(sql, page=_PAGE, key="ask_narrate", tier="live",
                  source="Ask:cortex_narrate")
        if res.df is not None and not res.df.empty:
            txt = str(res.df.iloc[0]["TXT"]).strip()
            return txt or None
    except Exception:  # noqa: BLE001 — narration is a nicety; never break the answer
        return None
    return None


def _render_result(result: AnswerResult, company: str, params: AskParams,
                   use_ai: bool, model: str) -> None:
    if result.confidence == "no_data":
        st.info(result.headline)
    else:
        st.success(result.headline)

    for b in result.bullets:
        st.markdown(f"- {b}")

    if use_ai and result.confidence == "grounded":
        phrased = _ai_phrasing(result, model)
        if phrased:
            st.caption("AI phrasing (grounded numbers unchanged):")
            st.markdown(f"> {phrased}")

    if result.evidence is not None and not result.evidence.empty:
        with st.expander("Evidence — the rows this answer is built from", expanded=True):
            st.dataframe(result.evidence, use_container_width=True, hide_index=True)

    # Use the answer's OWN effective window (an answerer may clamp it, e.g. the
    # spend mart's 182-day horizon), never the raw request, so the caption can't
    # over-claim the window the headline was actually computed over.
    win = result.params.get("days", params.days)
    st.caption(
        f"Source: {result.source} · scope: company={company}, window={win}d "
        "· every number above is live query output."
    )


@safe_page(_PAGE)
def render() -> None:
    page_header(
        "Ask OVERWATCH",
        "Ask in plain English. Answers are grounded in real query output — "
        "or you get an honest \"I can't answer that yet,\" never a guess.",
    )

    f = filters()
    company = str(f.get("company", "ALL"))
    settings = load_settings(_PAGE)
    model = str(settings.get("CORTEX_MODEL", "llama3.1-8b"))

    left, right = st.columns([3, 1])
    with right:
        default_days = int(st.selectbox(
            "Default window", (7, 30, 90), index=1,
            help="Used when your question doesn't name a window. Say "
                 "'last 7 days' to override.",
        ))
        use_ai = st.checkbox(
            "AI phrasing", value=False,
            help="Optionally reword the grounded answer with Cortex. The numbers "
                 "never change; off by default.",
        )

    with left:
        st.caption("Click a question to run it — no typing needed — or type your own below:")
        cases = _test_cases()
        cols = st.columns(3)
        for i, q in enumerate(cases):
            with cols[i % 3]:
                if st.button(q, key=f"ask_chip_{i}", use_container_width=True):
                    st.session_state["ask_q"] = q
        question = st.text_input(
            "Your question", key="ask_q",
            placeholder="which user is causing spend spikes",
        )

    if not question.strip():
        _capabilities("**I can answer these specifically today:**")
        return

    rr = route(question, default_days=default_days, company=company)
    if rr.answerer is None:
        st.warning("I don't have a grounded path for that yet — so I won't guess.")
        _capabilities("**What I *can* answer (each backed by real query output):**")
        return

    ans = rr.answerer
    params = rr.params
    frames = {}
    # run() never raises — it returns ok=False with an empty frame on failure.
    # We MUST branch on ok: feeding that empty frame to analyze() would render a
    # query FAILURE as a confident "no data" answer, the exact false statement
    # this feature promises never to make.
    for spec in ans.needs(params):
        run_kwargs = {"page": _PAGE, "key": f"ask_{ans.intent}_{spec.key}",
                      "tier": spec.tier, "source": f"Ask:{ans.intent}:{spec.key}"}
        if spec.max_rows is not None:   # an answerer that aggregates asks for an uncapped read
            run_kwargs["max_rows"] = spec.max_rows
        res = run(spec.sql, **run_kwargs)
        if not res.ok:
            st.warning(
                f"I couldn't run the grounded query for this ({res.error_kind or 'error'}), "
                "so I won't guess. It's logged for follow-up."
            )
            if res.error:
                st.caption(res.error)
            return
        frames[spec.key] = res.df

    result = ans.analyze(params, frames)
    _render_result(result, company, params, use_ai, model)
