"""Cloud-services driver classifier — pure logic (app/logic/cs_driver.py)."""

from __future__ import annotations

import pandas as pd
import pytest

from app.logic import cs_driver as cs
from app.logic.cs_driver import (
    ACTION_COLS,
    BILLED_VIEW_COLS,
    COMPILE_HEAVY,
    DRIVER_COLS,
    GOVERNANCE_DISCOVERY,
    INFORMATION_SCHEMA,
    JDBC_ODBC_DISCOVERY,
    METADATA_CHATTER,
    NORMAL,
    RESIZE_INSUFFICIENT,
    RESIZE_NOT_INDICATED,
    SLEEP_POLLING,
    STAGE_FILE,
    SYSTEM_GENERATED,
    UNKNOWN,
    billed_family_view,
    billing_basis_note,
    classify_families,
    classify_row,
    driver_summary,
    owner_hint,
    remediation_action,
    sleep_estimate,
)


def _fam(text: str = "SELECT 1", *, runs: float = 100, compile_pct: float = 5.0,
         total_s: float = 10.0, **over) -> dict:
    row = {
        "SAMPLE_TEXT": text, "RUNS": runs, "AVG_COMPILE_S": round(total_s * compile_pct / 100, 3),
        "AVG_TOTAL_S": total_s, "COMPILE_PCT": compile_pct, "TOTAL_COMPILE_HOURS": 0.01,
    }
    row.update(over)
    return row


# ============================================ text-signature classification ==

def test_fbe_is_system_generated_and_resize_irrelevant():
    cls, conf = classify_row(_fam("CALL SYSTEM$FBE_CAPTURE_FILE_REVISION_HISTORY('workspace', (?), (?))",
                                   runs=410, compile_pct=99.7, total_s=0.55))
    assert cls == SYSTEM_GENERATED and conf == "HIGH"
    assert cs.resize_verdict(cls, 99.7, 0.55) == RESIZE_NOT_INDICATED
    assert cs.remediation_owner(cls).startswith("Platform")


def test_governance_and_cortex_probes():
    assert classify_row(_fam("SELECT SYSTEM$GET_CLASSIFICATION_STATUS_WITH_ELIGIBILITY(?, true)",
                             runs=13, compile_pct=99.6, total_s=2.0))[0] == GOVERNANCE_DISCOVERY
    assert classify_row(_fam("SELECT SYSTEM$CORTEX_MODEL_ACCESSIBLE(?)",
                             runs=8, compile_pct=99.8, total_s=1.1))[0] == GOVERNANCE_DISCOVERY


def test_jdbc_driver_metadata():
    # a non-thin JDBC family: the driver-comment signature is HIGH confidence
    cls, conf = classify_row(_fam("show /* JDBC:DatabaseMetaData.getPrimaryKeys() */ primary keys in table X",
                                  runs=40, compile_pct=79.7, total_s=0.64))
    assert cls == JDBC_ODBC_DISCOVERY and conf == "HIGH"
    assert cs.remediation_owner(cls) == "BI / IDE tool owner"
    # the observed family is thin (6-12 runs) -> honestly downgraded to MEDIUM
    assert classify_row(_fam("show /* JDBC:DatabaseMetaData.getPrimaryKeys() */ pk", runs=12,
                             compile_pct=79.7, total_s=0.64)) == (JDBC_ODBC_DISCOVERY, "MEDIUM")


def test_information_schema_discovery():
    cls, _ = classify_row(_fam("SELECT DISTINCT UPPER(COLUMN_NAME), COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS",
                               runs=9, compile_pct=3.8, total_s=19.0))
    assert cls == INFORMATION_SCHEMA  # text signature beats the low-compile shape


def test_stage_file_read():
    assert classify_row(_fam("SELECT $1 FROM @ALFA_EDW_SAN.PUBLIC.EDW_STAGE/ETLNAS/Parameter/ALFA_LIFT",
                             runs=5, compile_pct=48.5, total_s=1.2))[0] == STAGE_FILE


def test_unknown_system_function_still_platform():
    assert classify_row(_fam("CALL SYSTEM$SOME_NEW_INTERNAL(?)", compile_pct=99.0, total_s=0.4))[0] == SYSTEM_GENERATED


# ================================================= shape-based classification =

def test_compile_heavy_warehouse_backed():
    cls, _ = classify_row(_fam("SELECT a,b,c FROM big_join WHERE ...", compile_pct=62.0, total_s=8.0,
                               WAREHOUSE_NAME="WH_ALFA_QA"))
    assert cls == COMPILE_HEAVY
    assert cs.resize_verdict(cls, 62.0, 8.0) == RESIZE_NOT_INDICATED  # compile-bound => resize won't help


def test_metadata_chatter_shape_when_subsecond():
    cls, _ = classify_row(_fam("SELECT something small", compile_pct=95.0, total_s=0.3))
    assert cls == METADATA_CHATTER


def test_normal_exec_dominated_is_insufficient_evidence():
    cls, _ = classify_row(_fam("SELECT sum(x) FROM fact GROUP BY 1", compile_pct=5.0, total_s=25.0))
    assert cls == NORMAL
    # exec-dominated but no spill/queue columns here -> Phase 0 never claims RESIZE MAY HELP
    assert cs.resize_verdict(cls, 5.0, 25.0) == RESIZE_INSUFFICIENT


def test_thin_sample_decays_confidence():
    _, conf = classify_row(_fam("CALL SYSTEM$FBE_CAPTURE_FILE_REVISION_HISTORY(?)", runs=3,
                                compile_pct=99.0, total_s=0.5))
    assert conf == "MEDIUM"  # a HIGH text signature on <20 runs is only MEDIUM


# =================================================== frame decoration + safety =

def test_classify_families_stamps_all_columns_and_never_mutates():
    df = pd.DataFrame([
        _fam("CALL SYSTEM$FBE_CAPTURE_FILE_REVISION_HISTORY(?)", runs=410, compile_pct=99.7, total_s=0.55),
        _fam("SELECT sum(x) FROM fact", compile_pct=5.0, total_s=25.0),
    ])
    out = classify_families(df)
    for col in cs.DRIVER_COLS:
        assert col in out.columns
    assert list(out["DRIVER_CLASS"]) == [SYSTEM_GENERATED, NORMAL]
    assert "DRIVER_CLASS" not in df.columns  # caller frame untouched


def test_resize_verdict_is_always_two_state_in_phase0():
    df = pd.DataFrame([
        _fam("CALL SYSTEM$FBE_(?)", compile_pct=99.0, total_s=0.5),
        _fam("SELECT ... FROM x", compile_pct=3.0, total_s=30.0),
        _fam("show /* JDBC:DatabaseMetaData.getTables() */", compile_pct=70.0, total_s=0.6),
    ])
    verdicts = set(classify_families(df)["RESIZE_VERDICT"])
    assert verdicts <= {RESIZE_NOT_INDICATED, RESIZE_INSUFFICIENT}  # RESIZE MAY HELP never asserted


def test_empty_frame_keeps_schema():
    out = classify_families(pd.DataFrame())
    assert out.empty
    for col in cs.DRIVER_COLS:
        assert col in out.columns


def test_missing_or_nan_columns_never_crash():
    for row in ({}, {"SAMPLE_TEXT": None}, {"COMPILE_PCT": float("nan")}, {"RUNS": "x"}):
        cls, conf = classify_row(row)
        assert cls in cs.DRIVER_CLASSES if hasattr(cs, "DRIVER_CLASSES") else isinstance(cls, str)
        assert conf in ("HIGH", "MEDIUM", "LOW")
    # a text-less thin row is UNKNOWN/LOW
    assert classify_row({})[0] == UNKNOWN


def test_driver_summary_rollup():
    df = classify_families(pd.DataFrame([
        _fam("CALL SYSTEM$FBE_(?)", compile_pct=99.0, total_s=0.5),
        _fam("show /* JDBC:DatabaseMetaData.getColumns() */", compile_pct=70.0, total_s=0.6),
        _fam("SELECT ... FROM INFORMATION_SCHEMA.COLUMNS", compile_pct=4.0, total_s=19.0),
    ]))
    summ = driver_summary(df)
    assert summ["total"] == 3
    assert summ["not_indicated"] == 3  # all three are compile-layer classes
    assert summ["by_class"][SYSTEM_GENERATED] == 1
    assert summ["top_owner"]  # a non-empty dominant owner


# ============================================ v4.595: sleep polling (SYSTEM$WAIT) =


def test_control_m_poll_is_sleep_polling_not_benign_platform():
    # the owner's #1 CS line: Control-M's select system$wait(10), 18,667 runs/week, 0.7% compile
    row = _fam("select system$wait(10)", runs=18667, compile_pct=0.7, total_s=10.0,
               WAREHOUSE_NAME="WH_ALFA_TRANSFORM_PRD")
    cls, conf = classify_row(row)
    assert (cls, conf) == (SLEEP_POLLING, "HIGH")
    assert cs.resize_verdict(cls, 0.7, 10.0) == RESIZE_NOT_INDICATED
    assert cs.remediation_owner(cls) == "Job scheduler / task owner"


def test_task_call_sleeps_and_thin_decay():
    for n, runs in ((30, 3505), (60, 1108)):
        assert classify_row(_fam(f"CALL SYSTEM$WAIT({n})", runs=runs, compile_pct=0.2, total_s=n)) == (
            SLEEP_POLLING, "HIGH")
    # the (1200) family runs 12x a week: a HIGH signature on a thin sample is MEDIUM
    assert classify_row(_fam("CALL SYSTEM$WAIT(1200)", runs=12, compile_pct=0.0, total_s=1200)) == (
        SLEEP_POLLING, "MEDIUM")


def test_other_system_functions_still_fall_through():
    assert classify_row(_fam("SELECT SYSTEM$WAIT_FOR_SERVICES(60,'s')"))[0] == SYSTEM_GENERATED
    assert classify_row(_fam("CALL SYSTEM$SOME_NEW_INTERNAL(?)", compile_pct=99.0, total_s=0.4))[0] == (
        SYSTEM_GENERATED)
    # defining a task that sleeps is not sleeping
    ddl = _fam("CREATE OR REPLACE TASK T AS CALL SYSTEM$WAIT(10)", QUERY_TYPE="CREATE_TASK")
    assert classify_row(ddl)[0] != SLEEP_POLLING


def test_unhashed_bucket_is_never_confident():
    na_sig = _fam("select system$wait(10)", QUERY_PARAMETERIZED_HASH="n/a")
    na_shape = _fam("SELECT a FROM big", compile_pct=62.0, total_s=8.0, WAREHOUSE_NAME="WH",
                    QUERY_PARAMETERIZED_HASH="n/a")
    assert classify_row(na_sig)[1] == "LOW" and classify_row(na_shape)[1] == "LOW"
    assert remediation_action(SLEEP_POLLING, na_sig).startswith("Mixed statements (no family hash): ")
    assert sleep_estimate(na_sig) == (None, None, "")


def test_default_classify_families_output_is_byte_stable():
    # the Spend compile-heavy + Ops chatter panels rely on EXACTLY DRIVER_COLS being added
    df = pd.DataFrame([_fam("select system$wait(10)"), _fam("SELECT 1")])
    assert list(classify_families(df).columns) == list(df.columns) + list(DRIVER_COLS)
    wide = classify_families(df, with_action=True)
    assert list(wide.columns) == list(df.columns) + list(DRIVER_COLS) + list(ACTION_COLS)
    empty = classify_families(pd.DataFrame(), with_action=True)
    assert empty.empty and all(c in empty.columns for c in DRIVER_COLS + ACTION_COLS)


def test_owner_hint_and_next_step():
    assert owner_hint({"USER_NAME": "SYSTEM", "ROLE_NAME": "TRXS_TASK_OWNER"}) == "Task owner (role TRXS_TASK_OWNER)"
    hint = owner_hint({"USER_NAME": "CTM_SVC", "USER_TOP_APP": "Control-M"})
    assert "CTM_SVC" in hint and "Control-M" in hint
    assert owner_hint({"USER_NAME": "CTM_SVC"}) == "User CTM_SVC"
    assert owner_hint({}) == ""
    task_step = remediation_action(SLEEP_POLLING, {"USER_NAME": "SYSTEM"})
    assert "AFTER" in task_step and "stream" in task_step
    assert "scheduler" in remediation_action(SLEEP_POLLING, {"USER_NAME": "CTM_SVC"})
    for cls in cs.DRIVER_CLASSES:
        for row in ({"USER_NAME": "SYSTEM"}, {"USER_NAME": "X"}, None):
            assert remediation_action(cls, row).count("$") <= 1, cls   # two "$" pair into LaTeX


def test_sleep_estimate_requested_vs_measured():
    ctm = {"SAMPLE_TEXT": "select system$wait(10)", "QUERY_TYPE": "SELECT", "RUNS": 18667,
           "QUERY_PARAMETERIZED_HASH": "h1"}
    assert sleep_estimate(ctm) == (10.0, 186670.0, "requested")
    assert sleep_estimate({**ctm, "SAMPLE_TEXT_ALT": "select system$wait(10)"})[2] == "requested"
    # MIN / MAX samples disagree -> the parsed wait is not trusted; fall back to measured elapsed
    mixed = {**ctm, "SAMPLE_TEXT": "CALL SYSTEM$WAIT(30)", "SAMPLE_TEXT_ALT": "CALL SYSTEM$WAIT(60)",
             "QUERY_TYPE": "CALL", "RUNS": 100, "AVG_ELAPSED_S": 37.0}
    assert sleep_estimate(mixed) == (None, 3700.0, "measured")
    assert sleep_estimate({**mixed, "AVG_ELAPSED_S": None}) == (None, None, "")
    assert sleep_estimate({**ctm, "SAMPLE_TEXT": "select 1"}) == (None, None, "")


def _billed_frame(**window):
    """The owner's four sleep families (DIAG 2026-09-26, 7 days) as the builder returns them."""
    fams = [("select system$wait(10)", "SELECT", "WH_ALFA_TRANSFORM_PRD", "CTM_SVC", 18667, 28.8),
            ("CALL SYSTEM$WAIT(30)", "CALL", "WH_TRXS_TRANSFORM", "SYSTEM", 3505, 16.1),
            ("CALL SYSTEM$WAIT(60)", "CALL", "WH_TRXS_TRANSFORM", "SYSTEM", 1108, 10.2),
            ("CALL SYSTEM$WAIT(1200)", "CALL", "WH_TRXS_TRANSFORM", "SYSTEM", 12, 2.2)]
    rows = []
    for i, (text, qtype, wh, user, runs, cs_cr) in enumerate(fams):
        rows.append({
            "CS_RANK": i + 1, "QUERY_PARAMETERIZED_HASH": f"h{i}", "QUERY_TYPE": qtype, "WAREHOUSE_NAME": wh,
            "USER_NAME": user, "ROLE_NAME": "TRXS_TASK_OWNER" if user == "SYSTEM" else "CTM_ROLE",
            "USER_TOP_APP": "Control-M" if user == "CTM_SVC" else None, "SAMPLE_TEXT": text,
            "SAMPLE_TEXT_ALT": text, "RUNS": runs, "ACTIVE_DAYS": 7, "AVG_COMPILE_S": 0.07,
            "AVG_EXEC_S": None, "AVG_ELAPSED_S": None, "CS_CREDITS": cs_cr, "BILLED_CS_CREDITS": cs_cr,
            "CS_SHARE_PCT": cs_cr / 218.55 * 100, "SLEEP_FLAG": 1})
    df = pd.DataFrame(rows)
    base = {"SCOPE_CS_CREDITS": 218.55, "SCOPE_UNMETERED_CS_CREDITS": 0.0, "LOW_COMPILE_CS_CREDITS_ALL": 150.0,
            "SLEEP_FAMILIES_ALL": 4, "SLEEP_CS_CREDITS_ALL": 57.3, "SLEEP_BILLED_CS_CREDITS_ALL": 57.3,
            "METERED_CS_CREDITS": 218.55, "METERED_DAYS": 7, "UNDER_ALLOWANCE_DAYS": 0,
            "LAST_METERED_DAY": "2026-09-25"}
    base.update(window)
    for k, v in base.items():
        df[k] = v
    return df


def test_billed_view_reproduces_the_owner_diag():
    view, s = billed_family_view(_billed_frame(), 3.68)
    assert list(view.columns) == list(BILLED_VIEW_COLS)
    assert set(view["DRIVER_CLASS"]) == {SLEEP_POLLING}
    assert abs(view["CS_CREDITS"].sum() - 57.3) < 1e-9
    assert abs(view["SLEEP_SEC"].sum() - 372_700) < 1e-6                     # 103.5 hours slept
    ctm = view.iloc[0]
    assert abs(ctm["CS_CREDITS_PER_SLEEP_HOUR"] - 28.8 / (186_670 / 3600)) < 1e-9   # ~0.555
    assert abs(ctm["BILLED_CS_USD"] - 105.984) < 1e-9                        # unrounded
    assert abs(view["BILLED_CS_USD"].sum() - 210.864) < 1e-9
    assert list(view["WAIT_PER_RUN_SEC"]) == [10.0, 30.0, 60.0, 1200.0]
    assert set(view["SLEEP_BASIS"]) == {"requested"}
    assert ctm["OWNER_HINT"] == "Control-M · CTM_SVC" and "scheduler" in ctm["NEXT_STEP"]
    assert view.iloc[1]["OWNER_HINT"] == "Task owner (role TRXS_TASK_OWNER)"
    assert "AFTER" in view.iloc[1]["NEXT_STEP"]
    assert (view["RESIZE_VERDICT"] == RESIZE_NOT_INDICATED).all()
    assert s["sleep_families"] == 4 and s["sleep_hours_complete"] is True
    assert abs(s["sleep_usd"] - 57.3 * 3.68) < 1e-9
    assert abs(s["sleep_share_pct"] - 57.3 / 218.55 * 100) < 1e-9
    assert abs(s["low_compile_pct"] - 150.0 / 218.55 * 100) < 1e-9
    # no helper columns leak into the display / CSV
    for helper in ("SAMPLE_TEXT_ALT", "SLEEP_FLAG", "SCOPE_CS_CREDITS", "SLEEP_FAMILIES_ALL", "METERED_DAYS",
                   "CS_RANK", "AVG_TOTAL_S"):
        assert helper not in view.columns


def test_unmetered_family_is_blank_not_zero_dollars():
    df = _billed_frame()
    df.loc[0, "BILLED_CS_CREDITS"] = None
    view, _ = billed_family_view(df, 3.68)
    assert pd.isna(view.iloc[0]["BILLED_CS_USD"])
    _, s = billed_family_view(_billed_frame(METERED_DAYS=0, SLEEP_BILLED_CS_CREDITS_ALL=0.0), 3.68)
    assert pd.isna(s["sleep_usd"])                                           # unpriced, never $0


def test_summary_reads_the_uncapped_window_totals():
    # the window has MORE sleep families than the rows shown: the KPI must use the SQL window total
    df = _billed_frame(SLEEP_FAMILIES_ALL=6, SLEEP_CS_CREDITS_ALL=70.0, SLEEP_BILLED_CS_CREDITS_ALL=69.5)
    _, s = billed_family_view(df, 3.68)
    assert s["sleep_usd"] == pytest.approx(69.5 * 3.68)
    assert s["sleep_cs"] == 70.0 and s["sleep_families"] == 6
    assert s["sleep_hours_complete"] is False                                # shown 4 < 6 -> "≥"


def test_billed_view_empty_and_missing_columns():
    for empty in (None, pd.DataFrame()):
        view, s = billed_family_view(empty, 3.68)
        assert view.empty and list(view.columns) == list(BILLED_VIEW_COLS)
        assert s["rows"] == 0 and s["sleep_families"] == 0 and pd.isna(s["sleep_usd"])
    thin = _billed_frame().drop(columns=["AVG_ELAPSED_S", "USER_TOP_APP", "SAMPLE_TEXT_ALT"])
    view, s = billed_family_view(thin, 3.68)
    assert len(view) == 4 and s["sleep_families"] == 4


def test_billed_view_duration_columns_humanize():
    from app.ui import components
    for col in ("AVG_COMPILE_S", "AVG_EXEC_S", "AVG_ELAPSED_S", "SLEEP_SEC", "WAIT_PER_RUN_SEC"):
        assert components._duration_unit_for_column(col) == "s", col
    assert components._duration_unit_for_column("CS_CREDITS_PER_SLEEP_HOUR") is None


def test_billing_basis_note_copy():
    base = {"metered_days": 7, "under_allowance_days": 0, "unmetered_cs": 0.0}
    head, detail = billing_basis_note(base, 3.68)
    assert "on all 7 metered days" in head and "$3.68" in head and "billed" in head
    head, _ = billing_basis_note({**base, "under_allowance_days": 2}, 3.68)
    assert "under the free allowance" in head and "only credits above it" in head
    head, _ = billing_basis_note({**base, "metered_days": 0}, 3.68)
    assert "not priced" in head
    head, _ = billing_basis_note({**base, "unmetered_cs": 3.2}, 3.68)
    assert "not yet in daily metering" in head
    assert "UTC" in detail and "alone stopped" in detail and "$" not in detail
