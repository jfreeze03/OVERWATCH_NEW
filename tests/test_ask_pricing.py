"""Locks for the Ask evidence USD-estimate helper (add_usd_estimates)."""
import pandas as pd

from app.logic.ask.pricing import add_usd_estimates


def test_dollarizes_credit_columns_and_leaves_ratios_alone():
    ev = pd.DataFrame({
        "DIMENSION": ["AWS Glue PRD", "SUDEVAX"],
        "ALLOC_CREDITS": [247.7919, 123.8108],
        "CREDIT_SHARE": [0.212, 0.1059],
    })
    out, cols, rates = add_usd_estimates(ev, compute_rate=3.68, ai_rate=2.20)
    assert cols == ["ALLOC_CREDITS_USD"]
    assert rates == {3.68}
    # inserted right after its source column; the share ratio is NOT dollarized
    assert list(out.columns) == [
        "DIMENSION", "ALLOC_CREDITS", "ALLOC_CREDITS_USD", "CREDIT_SHARE"]
    assert round(out["ALLOC_CREDITS_USD"].iloc[0], 2) == round(247.7919 * 3.68, 2)
    assert "CREDIT_SHARE_USD" not in out.columns


def test_rate_and_run_columns_are_not_dollarized():
    ev = pd.DataFrame({"QUERY_TYPE": ["SELECT"], "RUNS": [81287],
                       "CS_CREDITS": [125.1954], "CS_PER_1K_RUNS": [1.5402]})
    out, cols, _ = add_usd_estimates(ev, compute_rate=3.68, ai_rate=2.20)
    assert cols == ["CS_CREDITS_USD"]
    assert "CS_PER_1K_RUNS_USD" not in out.columns and "RUNS_USD" not in out.columns


def test_ai_named_column_uses_ai_rate_but_warehouse_credits_do_not():
    ev = pd.DataFrame({"CORTEX_CREDITS": [10.0], "ALLOC_CREDITS": [10.0], "MAIN_CREDITS": [10.0]})
    out, _, rates = add_usd_estimates(ev, compute_rate=3.68, ai_rate=2.20)
    assert out["CORTEX_CREDITS_USD"].iloc[0] == 22.0    # AI/Cortex rate
    assert out["ALLOC_CREDITS_USD"].iloc[0] == 36.8     # compute rate
    assert out["MAIN_CREDITS_USD"].iloc[0] == 36.8      # "MAIN" must not trip on the "AI" substring
    assert rates == {3.68, 2.20}


def test_intent_is_ai_forces_the_ai_rate():
    ev = pd.DataFrame({"CREDITS": [100.0]})
    out, _, rates = add_usd_estimates(ev, compute_rate=3.68, ai_rate=2.20, intent_is_ai=True)
    assert out["CREDITS_USD"].iloc[0] == 220.0 and rates == {2.20}


def test_no_credit_column_leaves_the_frame_unchanged():
    ev = pd.DataFrame({"TASK_NAME": ["load"], "FAILED": [3], "RUNS": [10]})
    out, cols, rates = add_usd_estimates(ev, compute_rate=3.68, ai_rate=2.20)
    assert cols == [] and rates == set()
    assert list(out.columns) == ["TASK_NAME", "FAILED", "RUNS"]


def test_none_and_empty_are_safe():
    assert add_usd_estimates(None, compute_rate=3.68, ai_rate=2.20)[0] is None
    out, cols, rates = add_usd_estimates(pd.DataFrame(), compute_rate=3.68, ai_rate=2.20)
    assert cols == [] and rates == set() and out.empty


def test_ask_page_wires_the_estimate_and_the_triage_line_height():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    ask = (root / "app" / "ui" / "pages" / "ask.py").read_text(encoding="utf-8")
    assert "add_usd_estimates(" in ask and "AI_CREDIT_PRICE_USD" in ask
    theme = (root / "app" / "theme.py").read_text(encoding="utf-8")
    main = (root / "app" / "main.py").read_text(encoding="utf-8")
    # scope label is a single line (label + sub spans), not a clip-prone two-line stack
    assert ".ow-triage-label" in theme and ".ow-triage-sub" in theme
    assert 'class="ow-triage-label">Scope' in main
    # the bordered toolbar must not clip/scroll its row (what sheared the top off "SCOPE")
    assert 'stVerticalBlockBorderWrapper"]{\n  overflow:visible' in theme
