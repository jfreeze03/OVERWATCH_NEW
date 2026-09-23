"""Cloud-services driver classifier — pure logic (app/logic/cs_driver.py)."""

from __future__ import annotations

import pandas as pd

from app.logic import cs_driver as cs
from app.logic.cs_driver import (
    COMPILE_HEAVY,
    GOVERNANCE_DISCOVERY,
    INFORMATION_SCHEMA,
    JDBC_ODBC_DISCOVERY,
    METADATA_CHATTER,
    NORMAL,
    RESIZE_INSUFFICIENT,
    RESIZE_NOT_INDICATED,
    STAGE_FILE,
    SYSTEM_GENERATED,
    UNKNOWN,
    classify_families,
    classify_row,
    driver_summary,
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
