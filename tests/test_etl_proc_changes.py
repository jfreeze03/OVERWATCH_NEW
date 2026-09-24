"""Next-Fifty #21 (v4.589.0): the ETL failure-recurrence, runtime-drift and runtime-creep rows name the
stored-procedure redeploy behind them. The Informatica tasks are SP_* CALLs, so 'SP_X got 1.9x slower'
now carries 'redeployed 2026-09-20 · JDOE · REGRESSED' from OBJECT_CHANGE_REGISTRY, matched by proc name
(a pointer, not a verdict). One small mart read rides the chapter's run_batch prefetch."""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import pandas as pd

from app.logic.insights import annotate_proc_changes, latest_proc_changes, proc_key

_ROOT = Path(__file__).resolve().parents[1]
_NOW = pd.Timestamp("2026-09-24 09:00")


def test_proc_key_normalizes():
    assert proc_key("DB.SCH.SP_LOAD") == "SP_LOAD"
    assert proc_key("sp_load(VARCHAR)") == "SP_LOAD"
    assert proc_key('"SP_X"') == "SP_X"
    assert proc_key(None) == "" and proc_key("nan") == ""


def test_latest_proc_changes_filters_type_window_and_dedupes():
    reg = pd.DataFrame([
        {"OBJECT_TYPE": "PROCEDURE", "OBJECT_NAME": "DB.S.SP_A", "CHANGE_SEEN_AT": _NOW - timedelta(days=2),
         "CHANGED_BY": "JDOE", "VERDICT": "REGRESSED"},
        {"OBJECT_TYPE": "PROCEDURE", "OBJECT_NAME": "DB.S.SP_A", "CHANGE_SEEN_AT": _NOW - timedelta(days=20),
         "CHANGED_BY": "OLD", "VERDICT": "IMPROVED"},
        {"OBJECT_TYPE": "TASK", "OBJECT_NAME": "DB.S.SP_A", "CHANGE_SEEN_AT": _NOW - timedelta(days=1),
         "CHANGED_BY": "X", "VERDICT": None},
        {"OBJECT_TYPE": "PROCEDURE", "OBJECT_NAME": "DB.S.SP_OLD", "CHANGE_SEEN_AT": _NOW - timedelta(days=40),
         "CHANGED_BY": "Y", "VERDICT": None},
    ])
    out = latest_proc_changes(reg, now=_NOW)
    assert list(out["PROC_KEY"]) == ["SP_A"] and out.iloc[0]["CHANGED_BY"] == "JDOE"
    tz = reg.assign(CHANGE_SEEN_AT=pd.to_datetime(reg["CHANGE_SEEN_AT"]).dt.tz_localize("America/Chicago"))
    assert list(latest_proc_changes(tz, now=_NOW)["PROC_KEY"]) == ["SP_A"]
    assert latest_proc_changes(pd.DataFrame({"X": [1]})).empty and latest_proc_changes(None).empty


def test_annotate_inserts_after_task_and_counts_recent():
    changes = pd.DataFrame([
        {"PROC_KEY": "SP_A", "CHANGE_SEEN_AT": _NOW - timedelta(days=2), "CHANGED_BY": "JDOE",
         "VERDICT": "REGRESSED", "AGE_DAYS": 2.0},
        {"PROC_KEY": "SP_B", "CHANGE_SEEN_AT": _NOW - timedelta(days=10), "CHANGED_BY": None,
         "VERDICT": None, "AGE_DAYS": 10.0},
    ])
    frame = pd.DataFrame({"WORKFLOW_NAME": ["W"] * 3, "TASK_NAME": ["SP_A", "M_MAP", "SP_B"], "X": [1, 2, 3]},
                         index=[7, 8, 9])
    out, n_recent = annotate_proc_changes(frame, changes)
    cols = list(out.columns)
    assert cols[cols.index("TASK_NAME") + 1] == "CHANGED_RECENTLY"
    assert out.loc[8, "CHANGED_RECENTLY"] is None
    assert out.loc[7, "CHANGED_RECENTLY"].startswith("2026-09-22") and "REGRESSED" in out.loc[7, "CHANGED_RECENTLY"]
    assert out.loc[9, "CHANGED_RECENTLY"].endswith("— · PENDING")
    assert n_recent == 1                                   # SP_B at 10 days is outside the 7-day count
    assert list(out.index) == [7, 8, 9] and list(out["X"]) == [1, 2, 3]


def test_annotate_noop_when_nothing_matches():
    frame = pd.DataFrame({"TASK_NAME": ["M_MAP"]})
    changes = pd.DataFrame([{"PROC_KEY": "SP_Z", "CHANGE_SEEN_AT": _NOW, "CHANGED_BY": "a", "VERDICT": "b",
                             "AGE_DAYS": 1.0}])
    for c in (changes, pd.DataFrame(), None):
        out, n = annotate_proc_changes(frame, c)
        assert list(out.columns) == ["TASK_NAME"] and n == 0


def test_pipeline_panels_annotate_from_one_registry_read():
    src = (_ROOT / "app/ui/pages/operations.py").read_text(encoding="utf-8")
    # a dedicated PROCEDURE-only, per-object feed (the generic change_registry is LIMIT 200 over every type)
    assert '_add("chg_registry", change_impact_sql.proc_redeploys(ETL_CHANGE_LOOKBACK_DAYS)' in src
    assert 'change_impact_sql.proc_redeploys(ETL_CHANGE_LOOKBACK_DAYS), page=_PAGE, key="etl_chg_registry"' in src
    wants = re.findall(r"_pipeline_prefetch\(days, want=\{([^}]*)\}\)", src)
    assert sum('"chg_registry"' in w for w in wants) == 2
    for fn in ("_failure_recurrence_panel", "_workflow_drift_panel", "_runtime_creep_panel"):
        body = src.split(f"def {fn}(", 1)[1].split("\ndef ", 1)[0]
        assert "annotate_proc_changes(" in body, fn
