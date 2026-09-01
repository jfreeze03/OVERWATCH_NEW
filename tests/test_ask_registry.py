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


def test_word_boundary_matching_stops_substring_false_positives():
    # review Finding 1: "who" must NOT fire inside "whole", "account" not inside
    # "accounting" — a generic total-cost question must refuse, not hijack the
    # per-user spend answerer.
    assert route("what is the whole cost of storage",
                 default_days=30, company="ALL").answerer is None
    assert route("what does accounting cost",
                 default_days=30, company="ALL").answerer is None


def test_any_cloud_services_phrasing_routes_not_refuses():
    # review Finding 2: cloud-services questions with no "query"/"what" subject
    # word must still reach the CS answerer (single-gate on the CS phrase).
    for q in ("why are cloud services spiking",
              "why is cloud services spend so high",
              "our cloud services are through the roof"):
        assert route(q, default_days=30, company="ALL").answerer.intent == \
            "cloud_services_spike_by_query", q


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
    df = _alloc([100.0, 95.0, 90.0, 88.0, 85.0])   # 5 users, modest top share
    res = _analyze_spend_by_user(AskParams(30, "ALL"), {"alloc": df})
    assert res.confidence == "grounded"
    assert "outlier" not in res.headline.lower()   # nobody stands out
    assert any("spread across the cohort" in b for b in res.bullets)
    assert "CREDIT_SHARE" in res.evidence.columns  # honest column label (not ELAPSED_SHARE)


def test_spend_answer_does_not_claim_spread_when_too_few_to_test():
    # robust_zscores can't test <5 points, so with 3 users (one at 90%) the code
    # must NOT claim "spread across the cohort" off a test that never ran.
    df = _alloc([900.0, 50.0, 50.0], users=["DOMINANT", "A", "B"])
    res = _analyze_spend_by_user(AskParams(30, "ALL"), {"alloc": df})
    assert res.confidence == "grounded"
    assert not any("spread across the cohort" in b for b in res.bullets)
    assert any("too few" in b for b in res.bullets)


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
    # review Finding 3: the % must be honestly labeled as a share of the top
    # shapes (LIMIT 30), never "% of cloud-services credits" (the whole).
    assert "query shapes" in res.headline
    assert "of cloud-services credits" not in res.headline
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


# ============================ round-1 adversarial-test regressions ==========

def test_spend_verb_inflections_all_route():
    # R1 #3 (high): the spend answerer exists to answer these, so none may refuse.
    for q in ("who is the top spender", "top spenders", "biggest spenders",
              "which user spends the most", "who spends the most",
              "which account is the biggest spender"):
        assert route(q, default_days=30, company="ALL").answerer.intent == \
            "spend_spike_by_user", q
    # the word-boundary honesty guard still holds (no user token -> refuse)
    assert route("what is the whole cost of storage",
                 default_days=30, company="ALL").answerer is None


def test_extract_days_clamps_absurd_widths_consistently():
    # R1 #4: 7+ digit counts must clamp to the retention cap, not fall to default.
    assert extract_days("last 999999 days", 30) == 365
    assert extract_days("last 1000000 days", 30) == 365


def test_window_phrases_match_on_word_boundaries():
    # R1 #5: "this week" must not fire inside "this weekend"/"this weekly", etc.
    assert extract_days("this weekend", 30) == 30      # -> default, not 7
    assert extract_days("this weekly", 30) == 30
    assert extract_days("this yearly", 30) == 30       # -> default, not 365
    assert extract_days("todaywalk", 30) == 30         # -> default, not 1
    assert extract_days("this week", 30) == 7          # real phrase still works
    assert extract_days("this year", 30) == 365


def test_spend_missing_dimension_column_is_no_data_not_crash():
    # R1 #1: a non-empty frame lacking DIMENSION must degrade honestly, not KeyError.
    df = pd.DataFrame({"ALLOC_CREDITS": [100.0, 50.0]})
    res = _analyze_spend_by_user(AskParams(30, "ALL"), {"alloc": df})
    assert res.confidence == "no_data"


def test_cloud_services_null_query_type_is_not_rendered_as_None():
    # R1 #2: a SQL-NULL QUERY_TYPE on the top shape must read "query", never "None"/"nan".
    for null in (None, float("nan")):
        s = pd.DataFrame({"QUERY_TYPE": [null, "SHOW"], "SAMPLE_TEXT": ["a", "b"],
                          "RUNS": [999, 50], "CS_CREDITS": [42.0, 5.0]})
        res = _analyze_cs_by_query(AskParams(7, "ALL"), {"shapes": s})
        assert "None pattern" not in res.headline and "nan pattern" not in res.headline
        assert "query pattern" in res.headline


def test_spend_evidence_credit_share_matches_headline_base():
    # R1 #6 (high): CREDIT_SHARE must use the SAME named-user denominator as the
    # headline, not the builder's whole-scope share that includes dropped NONE load.
    c = {"NONE": 5000.0, "ETL_SVC": 100.0, "BI_APP": 50.0}
    tot = sum(c.values())
    df = pd.DataFrame({"DIMENSION": list(c), "ELAPSED_SHARE": [v / tot for v in c.values()],
                       "ALLOC_CREDITS": list(c.values())})
    res = _analyze_spend_by_user(AskParams(30, "ALL"), {"alloc": df})
    # named-user total = 150; ETL_SVC share = 100/150 = 0.667, matching "67%".
    etl = float(res.evidence[res.evidence["DIMENSION"] == "ETL_SVC"]["CREDIT_SHARE"].iloc[0])
    assert round(etl, 3) == 0.667
    assert "67% of named-user spend" in res.headline


def test_cloud_services_single_shape_wording_is_grammatical():
    s = pd.DataFrame({"QUERY_TYPE": ["SELECT"], "SAMPLE_TEXT": ["x"], "RUNS": [10],
                      "CS_CREDITS": [5.0]})
    res = _analyze_cs_by_query(AskParams(30, "ALL"), {"shapes": s})
    assert "top 1 query shapes" not in res.headline
    assert "the only cloud-services query shape" in res.headline


# ============================ round-2 adversarial-test regressions ==========

def test_cloud_services_null_sample_text_shows_placeholder():
    # R2 #1: NULL/NaN SAMPLE_TEXT must not render as "None"/"nan".
    sh = pd.DataFrame({"QUERY_TYPE": ["SELECT", "INSERT"], "SAMPLE_TEXT": [None, float("nan")],
                       "RUNS": [1000, 500], "CS_CREDITS": [12.5, 6.0]})
    res = _analyze_cs_by_query(AskParams(30, "ALL"), {"shapes": sh})
    joined = " ".join(res.bullets) + " " + " ".join(map(str, res.evidence["SAMPLE_TEXT"].tolist()))
    assert "None" not in joined and "nan" not in joined
    assert "(no sample text)" in joined


def test_cloud_services_null_user_name_shows_unknown():
    # R2 #2: NULL USER_NAME on the heaviest-user bullet must read "(unknown)".
    sh = pd.DataFrame({"QUERY_TYPE": ["SELECT"], "SAMPLE_TEXT": ["x"], "RUNS": [10], "CS_CREDITS": [5.0]})
    bu = pd.DataFrame({"USER_NAME": [None], "CS_CREDITS": [9.0], "RUNS": [100]})
    res = _analyze_cs_by_query(AskParams(30, "ALL"), {"shapes": sh, "byuser": bu})
    heaviest = [b for b in res.bullets if "Heaviest user" in b]
    assert heaviest and "(unknown)" in heaviest[0] and "None" not in heaviest[0]


def test_cloud_services_phrase_wins_mixed_domain_questions():
    # R2 #3: a question naming cloud services must route to the CS answerer even
    # with user/spend words present (priority), while pure questions still route.
    for q in ("who is driving cloud services credits",
              "which user is driving cloud services spend",
              "who is the biggest cloud services spender"):
        assert route(q, default_days=30, company="ALL").answerer.intent == \
            "cloud_services_spike_by_query", q
    assert route("who is the top spender", default_days=30, company="ALL").answerer.intent == \
        "spend_spike_by_user"


def test_spend_answer_uses_the_cost_pages_attribution_builder():
    # R2 #4 (high): Ask's per-user spend must use the SAME warehouse-grain,
    # today-excluded builder the Cost page serves, so the two never contradict.
    from app.logic.ask.registry import _needs_spend_by_user
    sql = _needs_spend_by_user(AskParams(30, "ALFA"))[0].sql
    assert "FACT_COST_ALLOC_XDIM_DAILY" in sql          # same mart as Cost & Contract
    assert "DAY < CURRENT_DATE()" in sql                # half-open window (excludes today)
    assert "MART_COST_ALLOCATION_DAILY" not in sql      # not the abandoned owner-scoped mart


# ============================ round-3 adversarial-test regressions ==========

def test_spend_window_label_reflects_the_mart_effective_horizon():
    # R3 #1: alloc_xdim clamps a 365d request to the mart's 182d horizon, so the
    # headline/caption/meta must say 182 (never over-claim 365), with a cap note.
    df = pd.DataFrame({"DIMENSION": ["A", "B", "C", "D", "E"], "ELAPSED_SEC": [9, 5, 4, 3, 2.0],
                       "ELAPSED_SHARE": [.4, .2, .2, .1, .1], "ALLOC_CREDITS": [400., 200, 150, 100, 50]})
    res = _analyze_spend_by_user(AskParams(365, "ALL"), {"alloc": df})
    assert "over the last 182d" in res.headline.lower()
    assert "365d" not in res.headline
    assert res.params["days"] == 182
    assert any("capped to the mart's 182-day" in b for b in res.bullets)
    # a within-horizon window is untouched, no cap note
    res30 = _analyze_spend_by_user(AskParams(30, "ALL"), {"alloc": df})
    assert "over the last 30d" in res30.headline.lower()
    assert not any("capped" in b for b in res30.bullets)


# ============================ answerers 3 & 4: warehouse waste / task fails ==

def test_warehouse_waste_and_task_failures_route():
    for q in ("which warehouse is wasting credits", "which warehouse is idle the most",
              "where am I paying for idle warehouses"):
        assert route(q, default_days=30, company="ALL").answerer.intent == "warehouse_idle_waste", q
    for q in ("what task is failing most", "which task fails the most",
              "which pipeline task keeps failing"):
        assert route(q, default_days=30, company="ALL").answerer.intent == "task_failures", q
    # a per-warehouse spend-spike question (no waste/idle term) has no grounded path
    assert route("which warehouse is causing spend spikes", default_days=30, company="ALL").answerer is None


def test_warehouse_waste_names_top_waster_and_shows_good_news():
    from app.logic.ask.registry import _analyze_warehouse_waste
    idf = pd.DataFrame({"WAREHOUSE_NAME": ["WH_ETL", "WH_BI"], "METERED_HOURS": [720, 720],
                        "IDLE_HOURS": [500, 50], "TOTAL_CREDITS": [1000., 800], "IDLE_CREDITS": [600., 40]})
    res = _analyze_warehouse_waste(AskParams(30, "ALL"), {"idle": idf})
    assert res.confidence == "grounded"
    assert "WH_ETL" in res.headline and "600" in res.headline and "60%" in res.headline
    # zero idle -> honest good news, not a false "no data"
    clean = pd.DataFrame({"WAREHOUSE_NAME": ["WH"], "METERED_HOURS": [100], "IDLE_HOURS": [0],
                          "TOTAL_CREDITS": [50.], "IDLE_CREDITS": [0.]})
    gn = _analyze_warehouse_waste(AskParams(30, "ALL"), {"idle": clean})
    assert gn.confidence == "grounded" and "No idle-credit waste" in gn.headline


def test_warehouse_waste_window_capped_to_the_live_90d_horizon():
    from app.logic.ask.registry import _analyze_warehouse_waste
    idf = pd.DataFrame({"WAREHOUSE_NAME": ["WH"], "METERED_HOURS": [100], "IDLE_HOURS": [50],
                        "TOTAL_CREDITS": [50.], "IDLE_CREDITS": [25.]})
    res = _analyze_warehouse_waste(AskParams(365, "ALL"), {"idle": idf})
    assert res.params["days"] == 90
    assert "over the last 90d" in res.headline.lower() and "365d" not in res.headline
    assert any("capped" in b for b in res.bullets)


def test_task_failures_aggregates_by_task_and_names_worst():
    from app.logic.ask.registry import _analyze_task_failures
    tf = pd.DataFrame({"DAY": ["2026-08-20", "2026-08-21", "2026-08-21"],
                       "DATABASE_NAME": ["D", "D", "D"], "SCHEMA_NAME": ["S", "S", "S"],
                       "TASK_NAME": ["LOAD", "LOAD", "REFRESH"], "RUNS": [10, 10, 5], "FAILED": [3, 4, 1],
                       "LAST_STATE": ["FAILED", "FAILED", "FAILED"],
                       "LAST_ERROR": ["timeout", "oom", "perm denied"]})
    res = _analyze_task_failures(AskParams(30, "ALL"), {"tasks": tf})
    assert res.confidence == "grounded"
    assert "LOAD is failing most" in res.headline and "7 failures" in res.headline  # 3+4 summed
    assert any("oom" in b for b in res.bullets)   # most-recent error surfaces
    # no failures -> honest good news
    gn = _analyze_task_failures(AskParams(30, "ALL"),
                                {"tasks": pd.DataFrame({"TASK_NAME": ["A"], "FAILED": [0], "RUNS": [5]})})
    assert gn.confidence == "grounded" and "No task failures" in gn.headline


# ==================== new-answerer hardening (adversarial round) ============

def test_failover_failsafe_do_not_hijack_task_failures():
    # #1: 'fail' is not a stem; failover/failsafe are unrelated Snowflake terms.
    for q in ("is failover configured for my task", "is failsafe enabled on this job",
              "how much failsafe storage does my task use"):
        assert route(q, default_days=30, company="ALL").answerer is None, q
    assert route("what task is failing most", default_days=30, company="ALL").answerer.intent == "task_failures"


def test_task_failure_verb_inflections_route():
    # coverage: erroring/errored (error stem) route; the task-subject gate still holds.
    for q in ("why do my tasks keep erroring", "which pipeline errored",
              "what task keeps erroring out", "task failure rate"):
        assert route(q, default_days=30, company="ALL").answerer.intent == "task_failures", q
    assert route("what is an error", default_days=30, company="ALL").answerer is None


def test_warehouse_waste_label_never_overclaims_scope():
    from app.logic.ask.registry import _analyze_warehouse_waste
    idf = pd.DataFrame({"WAREHOUSE_NAME": ["WH_A"], "METERED_HOURS": [720], "IDLE_HOURS": [500],
                        "TOTAL_CREDITS": [1000.], "IDLE_CREDITS": [600.]})
    alfa = _analyze_warehouse_waste(AskParams(30, "ALFA"), {"idle": idf})
    assert any("across ALFA warehouses" in b for b in alfa.bullets)
    assert not any("Account-wide" in b for b in alfa.bullets)   # #2: company slice != account
    allc = _analyze_warehouse_waste(AskParams(30, "ALL"), {"idle": idf})
    assert any("Account-wide" in b for b in allc.bullets)


def test_task_failures_null_name_and_schema_qualified():
    from app.logic.ask.registry import _analyze_task_failures
    # #3: null task name -> placeholder, never "nan"/"None"
    tf = pd.DataFrame({"DAY": ["2026-08-20"], "DATABASE_NAME": ["DB"], "SCHEMA_NAME": ["S"],
                       "TASK_NAME": [None], "RUNS": [10], "FAILED": [9], "LAST_ERROR": ["boom"]})
    r = _analyze_task_failures(AskParams(30, "ALL"), {"tasks": tf})
    assert "(unnamed task)" in r.headline and "nan" not in r.headline and "None" not in r.headline
    # #4: same task name in different schemas stays distinguishable; evidence keeps SCHEMA_NAME
    tf2 = pd.DataFrame({"DAY": ["2026-08-24", "2026-08-24"], "DATABASE_NAME": ["DB", "DB"],
                        "SCHEMA_NAME": ["S1", "S2"], "TASK_NAME": ["load", "load"],
                        "RUNS": [10, 10], "FAILED": [8, 2], "LAST_ERROR": ["e1", "e2"]})
    r2 = _analyze_task_failures(AskParams(30, "ALL"), {"tasks": tf2})
    assert "DB.S1.load" in r2.headline
    assert any("DB.S1.load" in b for b in r2.bullets) and any("DB.S2.load" in b for b in r2.bullets)
    assert "SCHEMA_NAME" in r2.evidence.columns


def test_task_failures_spec_is_uncapped_so_rate_is_not_truncated():
    # #5: the per-task-day fact is summed, so a capped read would inflate the rate.
    from app.logic.ask.registry import _needs_task_failures
    assert _needs_task_failures(AskParams(30, "ALL"))[0].max_rows == 0


# ================================================= wiring lock ==============

# ================================================= answerer 5: cortex/AI ====

def test_cortex_question_routes_to_model_answerer():
    for q in ("which model is driving AI spend", "what is driving cortex credits",
              "top cortex models in the last 30 days"):
        assert route(q, default_days=30, company="ALL").answerer.intent == "cortex_spend_by_model", q
    # a plain compute-spend question still goes to the generic spender, not cortex
    assert route("which user is causing spend spikes", default_days=30,
                 company="ALL").answerer.intent == "spend_spike_by_user"
    # and a cloud-services question is not stolen by cortex
    assert route("top cloud services queries in the last 30 days", default_days=30,
                 company="ALL").answerer.intent == "cloud_services_spike_by_query"


def test_cortex_answer_rolls_up_by_model_and_emits_ai_credits_evidence():
    from app.logic.ask.registry import _analyze_cortex_by_model
    df = pd.DataFrame({
        "FUNCTION_NAME": ["COMPLETE", "COMPLETE", "EMBED_TEXT_768"],
        "MODEL_NAME": ["llama3.1-70b", "llama3.1-70b", "e5-base-v2"],
        "TOKENS": [1_000_000, 500_000, 200_000],
        "CREDITS": [80.0, 40.0, 5.0],
        "CREDITS_PER_1M_TOKENS": [0.08, 0.08, 0.025],
    })
    res = _analyze_cortex_by_model(AskParams(30, "ALL"), {"cortex": df})
    assert res.confidence == "grounded"
    assert "llama3.1-70b" in res.headline
    assert "120" in res.headline                      # 80+40 rolled up to the model
    assert res.evidence is not None and "AI_CREDITS" in res.evidence.columns
    assert any("Account-wide" in b for b in res.bullets)


def test_cortex_answer_no_data_on_empty_view():
    from app.logic.ask.registry import _analyze_cortex_by_model
    r = _analyze_cortex_by_model(AskParams(30, "ALL"), {"cortex": pd.DataFrame()})
    assert r.confidence == "no_data"


def test_cortex_evidence_ai_credits_column_triggers_the_coco_rate():
    # the whole point: the AI_CREDITS column name makes the Ask USD helper price at
    # the $2.20 AI/Cortex rate, not the $3.68 compute rate.
    from app.logic.ask.pricing import add_usd_estimates
    ev = pd.DataFrame({"MODEL_NAME": ["m"], "AI_CREDITS": [10.0]})
    out, cols, rates = add_usd_estimates(ev, compute_rate=3.68, ai_rate=2.20)
    assert cols == ["AI_CREDITS_USD"] and rates == {2.20}
    assert out["AI_CREDITS_USD"].iloc[0] == 22.0


def test_ask_page_is_wired_and_isolated():
    # the ONE nav wiring point (main.py) + owner-only visibility (config.py).
    main = (_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert '"Ask": ask.render' in main
    cfg = (_ROOT / "app" / "config.py").read_text(encoding="utf-8")
    assert '"Ask"' in cfg
    # the feature owns its files (delete these + the 2 wiring blocks to revert).
    assert (_ROOT / "app" / "logic" / "ask" / "registry.py").exists()
    ask_py = (_ROOT / "app" / "ui" / "pages" / "ask.py").read_text(encoding="utf-8")
    # review Finding 4: a query FAILURE must branch on res.ok, never fall through
    # to analyze() as a false "no data".
    assert "if not res.ok" in ask_py
