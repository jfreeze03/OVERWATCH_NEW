"""Next-Fifty #24 (v4.588.0): AI text gets a measured numeric grounding check instead of a caption that
merely CLAIMED 'grounded in the on-screen evidence only'. The strict Ask rule (numbers_preserved) moved
verbatim into the pure app/logic/ai_grounding.py; the free-form evaluation panel gets an ADVISORY check
(check_grounding) that lists $/% figures with no match in the evidence rows, within rounding tolerance.
Both digest renders and the Ask phrasing now $-escape (two $ amounts paired into a LaTeX span)."""

from __future__ import annotations

from pathlib import Path

import app.logic.ai_grounding as g
import app.ui.pages.ask as ask

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_numbers_preserved_moved_verbatim():
    assert ask._numbers_preserved is g.numbers_preserved      # alias, not a copy
    np = g.numbers_preserved
    grounded = "Over the last 30d, USER_A is the top spender: 900 credits (60% of named-user spend)."
    assert np(grounded, "USER_A drove 900 credits, 60% of spend.") is True
    assert np(grounded, "USER_A drove 900 credits, about 30% of spend.") is False
    assert np(grounded, "USER_A drove 950 credits.") is False
    grounded = "Over the last 90d, LOADER is the top spender: 120 credits (45% of spend)."
    assert np(grounded, "LOADER led at 120 credits, about 45% of spend.")
    assert not np(grounded, "LOADER spent 130 credits (45%).")
    assert not np(grounded, "LOADER was 99% of spend.")
    assert "_NUM_RE" not in _src("app/ui/pages/ask.py")       # one definition, in the pure module


def test_grounded_currency_and_pct_with_rounding():
    chk = g.check_grounding("Idle cost $1,235 (42%)", "IDLE_USD=1234.5678; IDLE_PCT=42.31")
    assert chk.checked == 2 and chk.ok and chk.ungrounded == ()


def test_suffix_scaling():
    assert g.check_grounding("about $1.2K idle", "IDLE_USD=1234.56").ok
    assert g.check_grounding("about $1.5M idle", "IDLE_USD=1234567").ungrounded == ("$1.5M",)
    assert g.check_grounding("about $1.2 million idle", "IDLE_USD=1234567").ok
    # a word merely STARTING with a scale letter is not a suffix ('$50 before' is $50, not $50B)
    chk = g.check_grounding("$50 before noon", "X=50")
    assert chk.checked == 1 and chk.ok


def test_ratio_licenses_pct():
    assert g.check_grounding("the share is 42%", "SHARE=0.4231").ok


def test_invented_figure_flagged():
    chk = g.check_grounding("This costs $9,999 a month.", "IDLE_USD=12.5")
    assert chk.ungrounded == ("$9,999",) and not chk.ok and chk.checked == 1


def test_trivial_and_dedupe():
    assert g.check_grounding("0% then 100% then $0", "X=7").checked == 0
    assert g.check_grounding("$50 then $50", "X=50").checked == 1
    assert g.check_grounding("42 % and 42%", "X=0.42").checked == 1   # whitespace-normalized token


def test_no_figures_and_years():
    chk = g.check_grounding("In 2026 the loader failed 3 times", "N=3")
    assert chk.checked == 0 and chk.ok


def test_evidence_scientific_notation():
    assert g.check_grounding("roughly $120,000", "X=1.2e+05").ok


def test_empty_inputs_never_raise():
    assert g.check_grounding("", "").checked == 0
    assert g.check_grounding("$10", "").ungrounded == ("$10",)


def test_ai_panel_logs_once_per_generation():
    src = _src("app/ui/ai_panel.py")
    assert "check_grounding(" in src
    btn = src.index('if st.button("Generate AI evaluation"')
    stored = src.index("stored = st.session_state.get(state_key)")
    for ev in ('log_ui_event("ai_eval"', 'log_ui_event("ai_ungrounded"'):
        assert btn < src.index(ev) < stored, ev                  # inside the click branch, not the render
        assert src.count(ev) == 1, ev
    assert "not found in the evidence" in src
    assert "grounded in the on-screen evidence only" not in src   # the caption no longer overclaims


def test_digest_bodies_escape_dollars():
    assert 'st.markdown(md_dollars(str(drow.get("BODY") or "")))' in _src("app/ui/pages/brief.py")
    assert 'st.markdown(md_dollars(str(row.get("BODY") or "")))' in _src("app/ui/pages/overview.py")


def test_ask_phrasing_normalizes_model_and_escapes():
    src = _src("app/ui/pages/ask.py")
    assert "sql_literal(normalize_model(model))" in src
    assert 'st.markdown(md_dollars(f"> {phrased}"))' in src
    assert "cortex_complete(" not in src    # stays on run()'s cached 'ask_narrate' path (no per-rerun re-bill)


# --- adversarial review of #24: what may LICENSE a figure is kept narrow ----------------------------
def test_timestamps_and_instruction_text_license_nothing():
    import pandas as pd

    from app.logic.ai_prompts import task_failure_prompt
    tl = pd.DataFrame([{"QUERY_START_TIME": "2026-09-23 07:45:12.123-05:00", "ROLE_IN_GRAPH": "Root cause",
                        "ERROR_FAMILY": "PERMISSION", "DATABASE_NAME": "DB1", "SCHEMA_NAME": "S1",
                        "TASK_NAME": "LOAD_X1", "RUN_SEC": 30, "ERROR_MESSAGE": "not authorized"}])
    ev = g.evidence_section(task_failure_prompt(tl, "ALFA", 7))
    # the minute (45), second (12), day (23), tz offset (05) and 'max 5' in the rules licensed these before
    assert g.check_grounding("Failures rose 45% and cost $12 extra.", ev).ungrounded == ("$12", "45%")
    assert g.check_grounding("Root cause share 23%; retry wasted $5.", ev).ungrounded == ("$5", "23%")


def test_typed_columns_license_only_their_unit():
    import pandas as pd

    from app.logic.ai_prompts import idle_warehouse_prompt
    adv = pd.DataFrame([{"WAREHOUSE_NAME": "WH_A", "COMPANY": "ALFA", "METERED_HOURS": 120, "IDLE_HOURS": 40,
                         "TOTAL_CREDITS": 88, "IDLE_CREDITS": 30, "IDLE_PCT": 42.3, "IDLE_USD": 55.5,
                         "PROJECTED_MONTHLY_IDLE_USD": 240, "AUTO_SUSPEND": 600, "AUTO_SUSPEND_KNOWN": True,
                         "ACTION_STATUS": "OPEN", "ACTIONABLE": True, "ACTIONABLE_MONTHLY_USD": 180,
                         "SAVINGS_CONFIDENCE": 0.7}])
    ev = g.evidence_section(idle_warehouse_prompt(adv, "ALFA", 30))
    chk = g.check_grounding("Idle $55.50 (42%) — saving $180/month; ~$120/month at 70% confidence.", ev)
    assert chk.ungrounded == ("$120",)          # METERED_HOURS=120 is hours, never a $ figure
    assert chk.checked == 5


def test_evidence_section_drops_the_instruction_head():
    assert g.evidence_section("RULES (1) max 5\n\nEVIDENCE ROWS:\n- X=3") == "\n- X=3"
    assert g.evidence_section("no marker 7") == "no marker 7"
    src = _src("app/ui/ai_panel.py")
    assert "ground_text = evidence_section(prompt) if evidence is None else evidence" in src
    assert src.count("check_grounding(answer, ground_text)") == 2


def test_usd_tokenizer_trailing_comma_and_scale_words():
    chk = g.check_grounding("Idle cost $9,999, which is high; total $9,999.", "X=12")
    assert chk.ungrounded == ("$9,999",) and chk.checked == 1       # one figure, not two
    assert g.check_grounding("Savings of $1.2 Million", "X=1200000").ok
    assert g.check_grounding("about $1.2bn", "X=1200000000").ok
    assert g.check_grounding("about $1.2MM", "X=1200000").ok


def test_playbooks_have_no_bare_dollar_outside_code_spans():
    import re

    from app.logic.playbooks import PLAYBOOKS
    # the alert drawer renders playbooks with plain st.markdown — two bare '$' pair into a KaTeX span
    for rule, text in PLAYBOOKS.items():
        prose = re.sub(r"`[^`]*`", "", text)
        assert "$" not in prose, f"{rule}: bare '$' outside a code span garbles the drawer (write USD)"
