"""Ask-OVERWATCH — router + answerer contracts (ISOLATED feature).

Pure tests: synthetic frames in, grounded AnswerResult out. No Streamlit, no DB.
Part of the revertible feature — delete this file when reverting (see the header
of app/logic/ask/__init__.py for the full revert path).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.logic.ask import REGISTRY, route
from app.logic.ask.registry import _analyze_cs_by_query, _analyze_spend_by_user
from app.logic.ask.router import extract_days
from app.logic.ask.types import AskParams

_ROOT = Path(__file__).resolve().parents[1]


# ================================================= router: intent match =====

def test_spend_question_routes_to_user_answerer():
    rr = route("which user is causing spend spikes", default_days=30, company="ALL")
    assert rr.answerer is not None
    assert rr.answerer.intent == "spend_spike_by_user"


def test_cloud_services_question_routes_to_query_answerer():
    rr = route("which query is causing cloud services to spike",
               default_days=30, company="ALL")
    assert rr.answerer is not None
    assert rr.answerer.intent == "cloud_services_spike_by_query"


def test_unmapped_question_refuses_honestly_not_guesses():
    # the whole point: an unroutable question returns None, never a plausible lie.
    for q in ("what is the weather today", "tell me a joke", "", "   "):
        assert route(q, default_days=30, company="ALL").answerer is None


def test_spiking_stem_and_generic_cloud_services_phrasing_route():
    # "spiking" must match (stem), and a cloud-services question with no literal
    # "query" word still reaches the CS answerer.
    assert route("what is spiking cloud services",
                 default_days=30, company="ALL").answerer.intent == "cloud_services_spike_by_query"
    assert route("who is spiking my spend",
                 default_days=30, company="ALL").answerer.intent == "spend_spike_by_user"


def test_spend_and_cloud_are_not_confused():
    # a pure-spend question must NOT trip the cloud-services answerer and vice versa.
    assert route("who is my most expensive user this month",
                 default_days=30, company="ALL").answerer.intent == "spend_spike_by_user"
    assert route("top cloud services queries in the last 30 days",
                 default_days=30, company="ALL").answerer.intent == "cloud_services_spike_by_query"


# ================================================= router: window lift ======

def test_extract_days_from_text_overrides_default():
    assert extract_days("last 7 days", 30) == 7
    assert extract_days("past 90 days please", 30) == 90
    assert extract_days("this month", 30) == 30
    assert extract_days("this week", 30) == 7
    assert extract_days("no window mentioned", 45) == 45     # default falls through
    assert extract_days("9999 days", 30) == 365              # clamped to retention


def test_route_carries_company_scope_from_caller_not_text():
    # company is a security boundary — always the page filter, never parsed.
    rr = route("which user is causing spend spikes for TREXIS",
               default_days=30, company="ALFA")
    assert rr.params.company == "ALFA"


# ================================================= needs(): builds SQL =======

def test_every_answerer_needs_builds_runnable_sql():
    p = AskParams(days=30, company="ALL")
    for ans in REGISTRY:
        specs = ans.needs(p)
        assert specs, f"{ans.intent} produced no query specs"
        for spec in specs:
            assert isinstance(spec.sql, str) and "SELECT" in spec.sql.upper()
            assert spec.key and spec.tier


# ================================================= analyze(): spend =========

def _alloc(credits: list[float], users: list[str] | None = None) -> pd.DataFrame:
    users = users or [f"USER_{i}" for i in range(len(credits))]
    tot = sum(credits)
    return pd.DataFrame({
        "DIMENSION": users,
        "ELAPSED_SEC": [c * 2 for c in credits],
        "ELAPSED_SHARE": [(c / tot if tot else 0.0) for c in credits],
        "ALLOC_CREDITS": credits,
    })


def test_spend_answer_names_top_user_and_flags_outlier():
    df = _alloc([1000.0, 10.0, 9.0, 8.0, 7.0, 6.0], users=["ETL_SVC", "A", "B", "C", "D", "E"])
    res = _analyze_spend_by_user(AskParams(30, "ALL"), {"alloc": df})
    assert res.confidence == "grounded"
    assert "ETL_SVC" in res.headline
    assert "1,000" in res.headline                 # the real credit number, grounded
    assert "outlier" in res.headline.lower()       # 1000 vs ~8 peers -> clear outlier
    assert res.evidence is not None and len(res.evidence) == 6


def test_spend_answer_is_honest_when_no_single_outlier():
    df = _alloc([100.0, 95.0, 90.0, 88.0, 85.0])
    res = _analyze_spend_by_user(AskParams(30, "ALL"), {"alloc": df})
    assert res.confidence == "grounded"
    assert "outlier" not in res.headline.lower()   # nobody stands out
    assert any("spread across the cohort" in b for b in res.bullets)


def test_spend_answer_never_names_unattributed_load_as_the_culprit():
    # UNKNOWN has the most credits, but it is not a user — a real user must head
    # the answer, and NONE/UNKNOWN must not appear in the evidence.
    df = _alloc([5000.0, 100.0, 50.0], users=["UNKNOWN", "ETL_SVC", "BI_APP"])
    res = _analyze_spend_by_user(AskParams(30, "ALL"), {"alloc": df})
    assert res.confidence == "grounded"
    assert "ETL_SVC" in res.headline and "UNKNOWN" not in res.headline
    assert res.evidence is not None
    assert not res.evidence["DIMENSION"].astype(str).str.upper().isin(("NONE", "UNKNOWN")).any()


def test_spend_answer_no_data_on_empty():
    res = _analyze_spend_by_user(AskParams(30, "ALL"), {"alloc": pd.DataFrame()})
    assert res.confidence == "no_data"
    res2 = _analyze_spend_by_user(AskParams(30, "ALL"),
                                  {"alloc": _alloc([0.0, 0.0, 0.0])})
    assert res2.confidence == "no_data"            # zero total = nothing to attribute


# ================================================= analyze(): cloud svc =====

def _cs_frames():
    shapes = pd.DataFrame({
        "QUERY_TYPE": ["SELECT", "MERGE"],
        "SAMPLE_TEXT": ["SELECT * FROM BIG_TABLE WHERE x = ?", "MERGE INTO T ..."],
        "RUNS": [500, 100],
        "CS_CREDITS": [120.0, 30.0],
        "CS_PER_1K_RUNS": [240.0, 300.0],
    })
    byuser = pd.DataFrame({"USER_NAME": ["ETL_SVC", "BI_APP"], "CS_CREDITS": [90.0, 60.0]})
    ratio = pd.DataFrame({
        "WAREHOUSE_NAME": ["WH_A", "WH_B"],
        "CLOUD_SVC_PCT": [35.0, 5.0],
        "STATUS": ["ELEVATED", "OK"],
    })
    return {"shapes": shapes, "byuser": byuser, "ratio": ratio}


def test_cloud_services_answer_names_shape_user_and_warehouse():
    res = _analyze_cs_by_query(AskParams(30, "ALL"), _cs_frames())
    assert res.confidence == "grounded"
    assert "SELECT" in res.headline and "120" in res.headline   # top shape + its credits
    assert "500" in res.headline                                # runs, grounded
    assert any("ETL_SVC" in b for b in res.bullets)             # heaviest user
    assert any("WH_A" in b for b in res.bullets)                # elevated warehouse
    assert res.evidence is not None and not res.evidence.empty


def test_cloud_services_answer_no_data_on_empty():
    res = _analyze_cs_by_query(AskParams(30, "ALL"), {"shapes": pd.DataFrame()})
    assert res.confidence == "no_data"


def test_cloud_services_sample_text_is_clipped_in_evidence():
    frames = _cs_frames()
    frames["shapes"].loc[0, "SAMPLE_TEXT"] = "SELECT " + "col, " * 80 + "FROM T"
    res = _analyze_cs_by_query(AskParams(30, "ALL"), frames)
    assert res.evidence is not None
    assert res.evidence.iloc[0]["SAMPLE_TEXT"].endswith("…")    # long text truncated


# ================================================= wiring lock ==============

def test_ask_page_is_wired_and_isolated():
    # the ONE nav wiring point (main.py) + owner-only visibility (config.py).
    main = (_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert '"Ask": ask.render' in main
    cfg = (_ROOT / "app" / "config.py").read_text(encoding="utf-8")
    assert '"Ask"' in cfg
    # the feature owns its files (delete these + the 2 wiring blocks to revert).
    assert (_ROOT / "app" / "logic" / "ask" / "registry.py").exists()
    assert (_ROOT / "app" / "ui" / "pages" / "ask.py").exists()
