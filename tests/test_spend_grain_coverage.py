"""v4.597 (Decision Studio Option C, slice S3): the Cost Truth ratio line moved from Decision Studio ▸
Cost Truth to Cost ▸ Spend & Attribution, inside the on-demand "Load company attribution" toggle.

Locks:
  * metered_grain_coverage presence gating (NULL basis = no evidence, never a measured 0; METERED > 0);
  * the wiring — a "grain" member in the Spend/Attribution recent-jobs spec, filtered ONLY into the
    attribution batch (first paint unchanged), a prefetch-else-run serial fallback, and ONE caption
    right after the warehouse table's result_caption;
  * the spend.py ACCOUNT_USAGE literal count stays at its budget of 12 (the new source label is
    fact/mart only) and the grain builder reaches no ACCOUNT_USAGE view;
  * the coverage ladder's stale section pointers are fixed, with the locked phrases kept.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.data import workbench_sql
from app.logic.cost_coverage import metered_grain_coverage

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _frame(metered=100.0, measured=40.0, allocated=90.0, billed=150.0) -> pd.DataFrame:
    # workbench_sql.cost_truth always returns the four bases; an empty basis is NULL CREDITS.
    return pd.DataFrame({
        "BASIS": ["BILLED", "METERED", "MEASURED", "ALLOCATED"],
        "CREDITS": [billed, metered, measured, allocated],
    })


# ---------------------------------------------------------------------------
# Pure helper: presence gating
# ---------------------------------------------------------------------------
def test_grain_coverage_is_shares_of_metered():
    cov = metered_grain_coverage(_frame())
    assert cov is not None
    assert round(cov["measured_pct"], 6) == 40.0 and round(cov["allocated_pct"], 6) == 90.0
    assert (cov["metered_credits"], cov["measured_credits"], cov["allocated_credits"]) == (100.0, 40.0, 90.0)
    # lenses, not addends: owner-scoped allocation can exceed 100% of a company's metered credits
    over = metered_grain_coverage(_frame(metered=50.0, allocated=80.0))
    assert over is not None and over["allocated_pct"] > 100.0


def test_grain_coverage_needs_all_three_lenses_present():
    for missing in ("METERED", "MEASURED", "ALLOCATED"):
        df = _frame()
        df.loc[df["BASIS"] == missing, "CREDITS"] = None      # NULL basis = no evidence
        assert metered_grain_coverage(df) is None, missing
    # a genuinely measured 0 is present (not absent): 0% measured is a real statement
    zero = metered_grain_coverage(_frame(measured=0.0))
    assert zero is not None and zero["measured_pct"] == 0.0
    # BILLED is not a lens of this ratio: its absence changes nothing
    assert metered_grain_coverage(_frame(billed=None)) == metered_grain_coverage(_frame())


def test_grain_coverage_degrades_to_none_never_raises():
    assert metered_grain_coverage(None) is None
    assert metered_grain_coverage(pd.DataFrame()) is None
    assert metered_grain_coverage(pd.DataFrame({"BASIS": ["METERED"], "X": [1.0]})) is None
    assert metered_grain_coverage(_frame(metered=0.0)) is None          # no divide-by-zero
    assert metered_grain_coverage(_frame(metered=-5.0)) is None
    # decimal-ish / string credits from the connector still coerce
    df = _frame()
    df["CREDITS"] = ["150", "100", "25", "50"]
    cov = metered_grain_coverage(df)
    assert cov is not None and round(cov["measured_pct"]) == 25 and round(cov["allocated_pct"]) == 50
    # shaped-harness frames (non-string BASIS) simply don't match a basis
    assert metered_grain_coverage(pd.DataFrame({"BASIS": [1.0, 2.0], "CREDITS": [1.0, 2.0]})) is None


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------
def test_grain_member_rides_only_the_attribution_batch():
    spend = _src("app/ui/pages/cost_parts/spend.py")
    cost = _src("app/ui/pages/cost.py")
    jobs = spend.split("def _spend_attr_recent_jobs(", 1)[1].split("\ndef ", 1)[0]
    assert '{"key": "grain", "sql": workbench_sql.cost_truth(days, company, bounds=bounds),' in jobs
    assert ('"source": "FACT_WAREHOUSE_DAILY + FACT_OBJECT_COST_DAILY + MART_COST_ALLOCATION_DAILY '
            '(grain coverage)"') in jobs
    assert "from app.data import app_cost_sql, cost_sql, insights_sql, mart27_sql, mart_sql, workbench_sql" in spend
    # first paint unchanged: the Spend batch still filters to metering/csr/coco only
    assert 'if j["key"] in ("metering", "csr", "coco")]' in cost
    # the attribution toggle batches wh/daily/grain in ONE round trip and hands grain through
    assert 'run_batch([j for j in _all_jobs if j["key"] in ("wh", "daily", "grain")],' in cost
    toggle = cost.split('key="cost_attribution_load"', 1)[1].split("st.divider()", 1)[0]
    assert 'grain_res=_pf.get("grain")' in toggle and "_attribution_tab(" in toggle
    # the perf #15 lock still holds: all four original members threaded
    for k in ("metering", "csr", "wh", "daily"):
        assert f'_pf.get("{k}")' in cost, k


def test_attribution_tab_reads_grain_prefetch_else_serial_and_captions_once():
    spend = _src("app/ui/pages/cost_parts/spend.py")
    body = spend.split("def _attribution_tab(", 1)[1].split("\ndef ", 1)[0]
    assert "grain_res=None" in body.split("->", 1)[0]          # the new keyword in the signature
    assert body.count("grain_res if grain_res is not None else run(") == 1
    assert 'key=f"spend_grain_cov_{company}_{days}{_lm}", tier="hourly",' in body
    assert body.count("workbench_sql.cost_truth(days, company, bounds=bounds)") == 1
    assert "metered_grain_coverage(grain.df if grain.usable() else None)" in body
    # exactly one grain caption, gated on presence, right after the warehouse table's result_caption
    assert body.count("Grain coverage (") == 1
    rc = body.index("result_caption(wh, note=")
    cap = body.index("Grain coverage (")
    assert rc < body.index("grain_res if grain_res is not None else run(") < cap
    assert body.index("if _grain_cov is not None:") < cap
    caption = body[cap:cap + 900]
    assert "window_phrase(bounds, int(days))" in caption
    assert "measured object-query compute is" in caption and "user-allocated" in caption
    assert "of metered warehouse credits — separate lenses, not addends." in caption
    # the help discloses the owner-scoped allocation basis and the >100% / ALL caveat
    assert "owner-scoped" in caption and "MART_COST_ALLOCATION_DAILY" in caption
    assert "exceed 100%" in caption and "Company = ALL" in caption
    # no dollar sign in the caption text (percentages only), so no md_dollars pairing risk
    assert "$" not in caption.split("help=", 1)[0]
    # the ratio is computed from the ONE cost_truth frame, never the today-excluded vs-prior pool
    assert "CREDITS_CURRENT" not in body[body.index("grain_res if grain_res"):cap]


def test_spend_budget_and_reach_unchanged():
    spend = _src("app/ui/pages/cost_parts/spend.py")
    assert spend.count("ACCOUNT_USAGE") == 12                 # tests/test_perf_budgets.py ceiling
    # the grain builder is fact/mart only (no reachable-table pin change)
    for bounds in (None, (pd.Timestamp("2026-08-01").date(), pd.Timestamp("2026-09-01").date())):
        sql = workbench_sql.cost_truth(30, "ALFA", bounds=bounds)
        assert "ACCOUNT_USAGE" not in sql
        assert "ACTION_QUEUE" not in sql and "ALERT_EVENTS" not in sql   # no write-domain re-colds


def test_coverage_ladder_points_at_real_sections():
    spend = _src("app/ui/pages/cost_parts/spend.py")
    ladder = spend.split("Cost coverage ladder", 1)[1].split("st.caption(", 1)[0]
    # locked phrases (tests/history_locks/test_codex_r2_wave.py) kept
    assert "Object cost ledger" in ladder and "rate-card reconciliation" in ladder
    # stale pointers fixed: the ledger lives on Cost ▸ Optimization & Savings, the reconciliation
    # on Cost ▸ Contract & Forecast (Operations ▸ Optimize is now the query fix queue)
    assert "**Object cost ledger** (Cost ▸ \"\n                    \"Optimization & Savings)" in ladder
    assert "**rate-card reconciliation** (Cost ▸ \"\n                    \"Contract & Forecast)" in ladder
    assert "Operations → " not in ladder and "Cost & Contract" not in spend
    # still audit-gated (tests/test_uiux_wave2_modes.py)
    idx = spend.index("Cost coverage ladder")
    assert "if audit_mode():" in spend[idx - 200:idx]



# ---------------------------------------------------------------------------
# Render: the caption actually paints from a realistic cost_truth frame (and only then)
# ---------------------------------------------------------------------------
def _render_attribution(monkeypatch, grain_frame, *, prefetch: bool):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    from app.core.result import QueryResult
    from app.ui.pages.cost_parts import spend

    calls: list[dict] = []
    grain = QueryResult(df=grain_frame, ok=True, source="grain stub")

    def _run(sql, *args, **kwargs):
        calls.append({"sql": str(sql), **kwargs})
        if "MART_COST_ALLOCATION_DAILY" in str(sql) and "FACT_OBJECT_COST_DAILY" in str(sql):
            return grain
        return QueryResult(df=pd.DataFrame(), ok=True, source="stub")

    monkeypatch.setattr(spend, "run", _run)
    monkeypatch.setattr(spend, "run_mart_first",
                        lambda *a, **k: QueryResult(df=pd.DataFrame(), ok=True, source="stub"))
    wh = QueryResult(df=pd.DataFrame({"WAREHOUSE_NAME": ["WH_A"], "COMPANY": ["ALFA"],
                                      "CREDITS_CURRENT": [10.0], "CREDITS_PRIOR": [8.0]}),
                     ok=True, source="wh stub")
    empty = QueryResult(df=pd.DataFrame(), ok=True, source="stub")
    # AppTest runs the function's SOURCE as a script (no closures), so hand the prefetched
    # results over through a module attribute the script imports.
    monkeypatch.setattr(spend, "_GRAIN_TEST_ARGS",
                        {"wh_res": wh, "daily_res": empty, "grain_res": grain if prefetch else None},
                        raising=False)

    def _app():
        from app.ui.pages.cost_parts import spend as _spend
        _spend._attribution_tab("ALL", 30, 3.0, **_spend._GRAIN_TEST_ARGS)

    at = AppTest.from_function(_app, default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    return at, calls


def _captions(at) -> str:
    return " ".join(str(c.value) for c in at.caption)


def test_grain_caption_renders_from_the_prefetched_frame(monkeypatch):
    at, calls = _render_attribution(monkeypatch, _frame(metered=200.0, measured=50.0, allocated=150.0),
                                    prefetch=True)
    text = _captions(at)
    assert "Grain coverage (the last 30 days): measured object-query compute is 25% and "            "user-allocated 75% of metered warehouse credits" in text
    # prefetched -> no serial grain read
    assert not any("spend_grain_cov_" in str(c.get("key", "")) for c in calls)


def test_grain_caption_serial_fallback_reads_hourly_mart(monkeypatch):
    at, calls = _render_attribution(monkeypatch, _frame(), prefetch=False)
    grain_calls = [c for c in calls if str(c.get("key", "")).startswith("spend_grain_cov_")]
    assert len(grain_calls) == 1
    assert grain_calls[0]["key"] == "spend_grain_cov_ALL_30" and grain_calls[0]["tier"] == "hourly"
    assert "ACCOUNT_USAGE" not in grain_calls[0]["source"]
    assert "measured object-query compute is 40%" in _captions(at)


def test_grain_caption_absent_without_every_lens(monkeypatch):
    df = _frame()
    df.loc[df["BASIS"] == "ALLOCATED", "CREDITS"] = None
    at, _ = _render_attribution(monkeypatch, df, prefetch=True)
    assert "Grain coverage" not in _captions(at)
