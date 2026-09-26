"""Wiring, perf and copy locks for the v4.595 billed cloud-services family panel (Cost > Spend), its
first-paint batch member (cost.py), the Operations pointer and the sleep-polling advisor finding."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pandas as pd

from app.data import mart_sql
from app.logic import metric_registry as mr
from app.logic import query_advisor, query_opt
from app.logic.playbooks import playbook_for
from app.logic.query_opt import score_opportunities

_ROOT = Path(__file__).resolve().parents[1]
_COST = (_ROOT / "app/ui/pages/cost.py").read_text(encoding="utf-8")
_SPEND = (_ROOT / "app/ui/pages/cost_parts/spend.py").read_text(encoding="utf-8")
_OPS = (_ROOT / "app/ui/pages/operations.py").read_text(encoding="utf-8")


def _helper_body() -> str:
    start = _SPEND.index("def _cs_billed_families_panel(")
    return _SPEND[start:_SPEND.index("\ndef ", start + 10)]


# ------------------------------------------------------------------ wiring --

def test_rides_the_spend_first_paint_batch():
    assert '"key": "csfam", "tier": "hourly"' in _COST
    assert 'mart_sql.cloud_svc_billed_families(f["days"], f["company"], bounds=f["bounds"])' in _COST
    assert 'csfam_res=_pf.get("csfam")' in _COST
    assert _COST.index('"key": "csfam"') < _COST.index("_pf = run_batch_mixed(_spend_specs, page=_PAGE)")
    assert _COST.count("run_batch_mixed(") == 1                     # no extra round trip


def test_panel_renders_for_every_warehouse_before_the_compile_ranking():
    call = _SPEND.index("_cs_billed_families_panel(company, days, rate, _sel_wh, bounds=bounds, "
                        "prefetched=csfam_res)")
    assert _SPEND.index("0 <= int(_wh_sel) < len(csr.df)") < call     # inside the csr guard ...
    assert call < _SPEND.index("if _sel_wh or not elevated.empty:")  # ... not gated on ELEVATED
    assert "napp_res=None, csfam_res=None) -> None:" in _SPEND


def test_panel_is_mart_only_and_hourly():
    body = _helper_body()
    assert 'cloud_svc_billed_families(days, "ALL", sel_wh, bounds=bounds)' in body
    assert "cloud_svc_billed_families(days, company, bounds=bounds)" in body
    assert body.count('tier="hourly"') == 2 and body.count('{_lm}"') == 2
    for banned in ("cost_sql.", "run_mart_first", "ACCOUNT_USAGE"):
        assert banned not in body, banned
    assert "SNOWFLAKE.ACCOUNT_USAGE." not in mart_sql.cloud_svc_billed_families(30, "ALFA")


# -------------------------------------------------------------------- copy --

def test_copy_locks():
    assert "**Which statement families bill the most cloud services**" in _SPEND
    why = re.search(r"_CS_BILLED_WHY = \((.*?)\)\n", _SPEND, re.S)
    assert why is not None
    for phrase in ("not by compile time", "0.5 s", "SYSTEM$WAIT", "sleeps in"):
        assert phrase in why.group(1), phrase
    body = _helper_body()
    for phrase in ("st.caption(md_dollars(_CS_BILLED_WHY))", "methodology_note(md_dollars(detail))",
                   "billed (marginal)", "st.caption(md_dollars(head))"):
        assert phrase in body, phrase
    # the compile-ranked panel says why it cannot see sleep polling, and where to look
    assert "such as sleep polling" in _SPEND
    assert "many tiny/metadata queries or sleep polling" in _SPEND


def _unescaped_system_wait_sinks(src: str) -> list[int]:
    """Line numbers of st.caption / st.markdown calls whose text holds SYSTEM$WAIT without md_dollars."""
    bad = []
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("caption", "markdown") and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "st" and node.args):
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) and arg.func.id == "md_dollars":
            continue
        if any(isinstance(c, ast.Constant) and isinstance(c.value, str) and "SYSTEM$WAIT" in c.value
               for c in ast.walk(arg)):
            bad.append(node.lineno)
    return bad


def test_every_system_wait_markdown_sink_escapes_dollars():
    # "$" pairs into a KaTeX span in st.markdown / st.caption: SYSTEM$WAIT must go through md_dollars
    for src in (_SPEND, _OPS):
        assert _unescaped_system_wait_sinks(src) == []
    # the lock bites: a bare multi-segment caption is caught
    assert _unescaped_system_wait_sinks('st.caption("a " "b SYSTEM$WAIT c")\n') == [1]
    assert 'st.markdown(md_dollars(f"- {_f.detail}"))' in _OPS


def test_operations_chatter_points_at_the_billed_ranking():
    i = _OPS.index("Who is generating the chatter.")
    assert "Which statement families bill the most cloud services" in _OPS[i:i + 900]


def test_column_help_and_playbook():
    keys = ("BILLED_CS_CREDITS", "BILLED_CS_USD", "CS_SHARE_PCT", "SLEEP_SEC", "WAIT_PER_RUN_SEC", "SLEEP_BASIS",
            "CS_CREDITS_PER_SLEEP_HOUR", "USER_TOP_APP", "OWNER_HINT", "NEXT_STEP")
    for k in keys:
        assert k in mr.COLUMN_HELP, k
        assert mr.COLUMN_HELP[k].count("$") <= 1, k
    assert "sleep polling" in mr.COLUMN_HELP["DRIVER_CLASS"]
    pb = playbook_for("COST_CLOUD_SVC_ANOMALY")
    assert "bill the most cloud services" in pb and "sleep polling" in pb


# ----------------------------------------------------------- advisor / OOS --

_SLEEP_FP = {"FINGERPRINT": "ctm", "SAMPLE_TEXT": "select system$wait(10)", "QUERY_TYPE": "SELECT",
             "WAREHOUSE_NAME": "WH_ALFA_TRANSFORM_PRD", "RUNS": 18667, "TOTAL_EXEC_SEC": 184800.0,
             "TOTAL_COMPILE_SEC": 1300.0, "ELAPSED_SEC": 10.0, "COMPILE_SEC": 0.07, "EXECUTION_SEC": 9.9}


def test_sleep_polling_finding_on_the_fingerprint_grain():
    findings, score = query_advisor.advise(_SLEEP_FP)
    assert [f.code for f in findings] == ["sleep_polling"]
    assert score == query_advisor._CAP["sleep_polling"] == 18
    detail = findings[0].detail
    for phrase in ("10s per run", "AFTER", "scheduler", "resize won't help"):
        assert phrase in detail, phrase
    assert detail.count("$") == 1


def test_sleep_polling_finding_on_the_per_query_grain():
    f30, _ = query_advisor.advise({"QUERY_TEXT": "CALL SYSTEM$WAIT(30)", "QUERY_TYPE": "CALL", "ELAPSED_SEC": 30.0})
    assert f30[0].code == "sleep_polling" and "30s per run" in f30[0].detail
    fbind, _ = query_advisor.advise({"QUERY_TEXT": "CALL SYSTEM$WAIT(?)", "QUERY_TYPE": "CALL"})
    assert "requested wait on every run" in fbind[0].detail


def test_shape_identical_statements_stay_clean():
    for text, qtype in (("select a from t", "SELECT"),
                        ("SELECT SYSTEM$WAIT_FOR_SERVICES(60,'s')", "SELECT"),
                        ("CREATE OR REPLACE TASK T AS CALL SYSTEM$WAIT(10)", "CREATE_TASK")):
        findings, score = query_advisor.advise({**_SLEEP_FP, "SAMPLE_TEXT": text, "QUERY_TYPE": qtype})
        assert findings == [] and score == 0, text


def test_oos_board_names_the_pathology():
    assert query_opt._PATHOLOGY["sleep_polling"] == "Sleep polling"
    scored, breakdowns = score_opportunities(pd.DataFrame([_SLEEP_FP]))
    row = scored.iloc[0]
    assert row["PATHOLOGY"] == "Sleep polling" and row["QOP"] == 18
    assert row["FIRST_ACTION"] == breakdowns["ctm"][0][2]
