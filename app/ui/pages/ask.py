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

import re

import streamlit as st

from app.core.errors import safe_page
from app.core.query import run, run_batch_mixed
from app.core.sqlsafe import sql_literal
from app.core.state import filters
from app.logic.ask import REGISTRY, route
from app.logic.ask.pricing import add_usd_estimates, is_ai_credit_column
from app.logic.ask.types import AnswerResult, AskParams
from app.logic.formulas import safe_float
from app.ui.components import load_settings, page_header

_PAGE = "Ask"

# Intents whose credits are AI/Cortex-metered (priced at AI_CREDIT_PRICE_USD, not the
# compute rate). The Cortex answerer already names its column AI_CREDITS, which the pricing
# helper prices at the AI rate on its own; declaring the intent here is belt-and-suspenders
# and the extension point for any future AI answerer with a generically-named credit column.
_AI_INTENTS: frozenset[str] = frozenset({"cortex_spend_by_model"})


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


_NUM_RE = re.compile(r"\d[\d,]*\.?\d*")
_PCT_RE = re.compile(r"(\d[\d,]*\.?\d*)\s*%")


def _numbers_preserved(grounded: str, phrased: str) -> bool:
    """Enforce the 'grounded numbers unchanged' promise: every numeric token in the AI
    phrasing must already appear in the grounded finding. The prompt TELLS the model not to
    change numbers, but nothing made it true — a drifted figure would render under a caption
    claiming the numbers are unchanged. Thousands-commas are normalized so '1,234' == '1234'.

    ASK-G1: also bind PERCENTAGES to their role. The flat set alone let a bare number in
    one role license the same digits as a percentage in another — e.g. the window '30d'
    licensed a wrong '30%' when the grounded share was 60%. So every phrased ``N%`` must
    also appear as a percentage in the grounded text, not merely as some bare digit."""
    def toks(s: str) -> set[str]:
        return {m.group().replace(",", "").rstrip(".") for m in _NUM_RE.finditer(s)}

    def pct_toks(s: str) -> set[str]:
        return {m.group(1).replace(",", "").rstrip(".") for m in _PCT_RE.finditer(s)}

    return toks(phrased) <= toks(grounded) and pct_toks(phrased) <= pct_toks(grounded)


def _ai_phrasing(result: AnswerResult, model: str) -> str | None:
    """Reword the grounded result via Cortex. Given ONLY the deterministic
    finding and told to change no number/name. Degrades silently to None —
    including when the phrasing introduces a number not in the grounded finding."""
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
            # AIP-1: a SQL NULL result (content-filtered / model returned nothing) is a
            # NON-empty one-row frame whose TXT is None/NaN — str() would yield the literal
            # "None"/"nan", which has no digits so _numbers_preserved trivially passes and a
            # non-answer renders under the "grounded numbers unchanged" caption. Coalesce and
            # reject the placeholder strings, mirroring core.ai.cortex_complete's guard.
            _val = res.df.iloc[0]["TXT"]
            txt = "" if _val is None else str(_val).strip()
            # str(NaN) == "nan"; also catch the SQL-NULL/model placeholders.
            if txt.lower() in ("", "none", "nan", "null"):
                return None
            # Discard a rephrase that invented or changed a number — the caption promises
            # the grounded numbers are unchanged, so honor it rather than trust the model.
            return txt if _numbers_preserved(grounded, txt) else None
    except Exception:  # noqa: BLE001 — narration is a nicety; never break the answer
        return None
    return None


def _render_result(result: AnswerResult, company: str, params: AskParams,
                   use_ai: bool, model: str, settings: dict) -> None:
    if result.confidence == "no_data":
        # review fix: Ask's no_data headline IS the answer to a question the
        # user just typed — conditional feedback, not a panel absence, so the
        # empty-state vocabulary (quiet caption) does not apply here.
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
        # Dollarize any credit-quantity column at the current compute rate ($3.68) or the
        # AI/Cortex "CoCo" rate ($2.20) — per column, so a warehouse-credit column is never
        # priced at the AI rate. No credit column -> the frame renders unchanged.
        compute_rate = safe_float(settings.get("CREDIT_PRICE_USD"), 3.68)
        ai_rate = safe_float(settings.get("AI_CREDIT_PRICE_USD"), 2.20)
        ev, usd_cols, _rates = add_usd_estimates(
            result.evidence, compute_rate=compute_rate, ai_rate=ai_rate,
            intent_is_ai=result.intent in _AI_INTENTS,
        )
        with st.expander("Evidence — the rows this answer is built from", expanded=True):
            colcfg = {
                c: st.column_config.NumberColumn(
                    c[:-4].replace("_", " ").title() + " ($)", format="$%.2f")
                for c in usd_cols
            }
            st.dataframe(ev, width="stretch", hide_index=True,
                         column_config=colcfg or None)
            if usd_cols:
                # Label by each column's ACTUAL kind (via the pricing helper), not by
                # comparing the two configured rates — an admin may set them equal.
                _intent_ai = result.intent in _AI_INTENTS
                _has_ai = _intent_ai or any(is_ai_credit_column(c[:-4]) for c in usd_cols)
                _has_compute = (not _intent_ai) and any(
                    not is_ai_credit_column(c[:-4]) for c in usd_cols)
                if _has_ai and _has_compute:
                    _note = (f"AI/Cortex credits at ${ai_rate:.2f}, "
                             f"compute credits at ${compute_rate:.2f}")
                elif _has_ai:
                    _note = f"at ${ai_rate:.2f}/credit (AI/Cortex rate)"
                else:
                    _note = f"at ${compute_rate:.2f}/credit (compute rate)"
                st.caption(f"$ columns are estimates = credits × rate, {_note}. "
                           "Edit the rates on Admin → Settings.")

    # Use the answer's OWN effective window (an answerer may clamp it, e.g. the
    # spend mart's 182-day horizon), never the raw request, so the caption can't
    # over-claim the window the headline was actually computed over.
    win = result.params.get("days", params.days)
    # Some answerers are account-wide by construction (e.g. Cortex usage has no company
    # grain); label the scope honestly rather than pinning it to the page company filter.
    _scope = "account-wide" if result.params.get("account_wide") else f"company={company}"
    st.caption(
        f"Source: {result.source} · scope: {_scope}, window={win}d "
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
                if st.button(q, key=f"ask_chip_{i}", width="stretch"):
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
    # An answerer's needs() returns ALL its specs upfront (no spec depends on another's
    # result), so a multi-query answerer submits them as ONE parallel round-trip via
    # run_batch_mixed (each spec keeps its own tier) instead of N serial run() calls.
    specs = list(ans.needs(params))
    batch = []
    for spec in specs:
        bspec = {"key": f"ask_{ans.intent}_{spec.key}", "sql": spec.sql,
                 "tier": spec.tier, "source": f"Ask:{ans.intent}:{spec.key}"}
        if spec.max_rows is not None:   # an answerer that aggregates asks for an uncapped read
            bspec["max_rows"] = spec.max_rows
        batch.append(bspec)
    results = run_batch_mixed(batch, page=_PAGE) if batch else {}
    # run() / run_batch_mixed never raise — a failed member is ok=False with an empty frame.
    # We MUST branch on ok: feeding that empty frame to analyze() would render a query FAILURE
    # as a confident "no data" answer, the exact false statement this feature promises never to
    # make. Warn on the FIRST failing spec (in needs() order) and refuse to guess.
    for spec in specs:
        res = results.get(f"ask_{ans.intent}_{spec.key}")
        if res is None or not res.ok:
            st.warning(
                f"I couldn't run the grounded query for this ({(res.error_kind if res else '') or 'error'}), "
                "so I won't guess. It's logged for follow-up."
            )
            if res is not None and res.error:
                st.caption(res.error)
            return
        frames[spec.key] = res.df

    result = ans.analyze(params, frames)
    _render_result(result, company, params, use_ai, model, settings)
