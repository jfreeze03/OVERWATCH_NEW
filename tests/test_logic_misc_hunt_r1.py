"""Locks for the R1 logic-misc bug hunt (app/logic modules outside the cost/ops/DS clusters).

Each block names the finding it locks; every test here failed on the pre-fix code."""

from __future__ import annotations

import pandas as pd
import sqlglot

from app.data import change_impact_sql
from app.logic import wh_change
from tests._source import read

# ---- R1-065 / R1-121: the warehouse change tiles count the whole window, PENDING only ----------


def test_warehouse_change_registry_carries_untruncated_window_totals():
    sql = change_impact_sql.warehouse_change_registry(90, "ALFA")
    assert "COUNT(*) OVER () AS TOTAL_CHANGES" in sql
    assert "COUNT_IF(w.VERDICT = 'REGRESSED') OVER () AS TOTAL_REGRESSED" in sql
    assert "COUNT_IF(w.VERDICT = 'IMPROVED') OVER () AS TOTAL_IMPROVED" in sql
    assert "COUNT_IF(w.VERDICT = 'PENDING') OVER () AS TOTAL_PENDING" in sql
    assert "LIMIT 200" in sql            # the table is still the newest 200
    sqlglot.parse(sql, dialect="snowflake")


def test_registry_kpis_reads_the_window_totals_not_the_capped_frame():
    # 260 changes in the window, the newest 200 returned: 30 regressed among them, 90 overall
    df = pd.DataFrame({"VERDICT": ["REGRESSED"] * 30 + ["IMPROVED"] * 170,
                       "TOTAL_CHANGES": [260] * 200, "TOTAL_REGRESSED": [90] * 200,
                       "TOTAL_IMPROVED": [165] * 200, "TOTAL_PENDING": [5] * 200})
    assert wh_change.registry_kpis(df) == {
        "changes": 260, "regressed": 90, "improved": 165, "pending": 5}


def test_registry_kpis_never_counts_no_baseline_as_pending():
    df = pd.DataFrame({"VERDICT": ["NO_BASELINE", "NEUTRAL"]})
    assert wh_change.registry_kpis(df)["pending"] == 0


def test_wh_change_block_says_when_the_table_is_the_newest_slice():
    block = read("app/ui/pages/operations.py").split("def _wh_change_block", 1)[1].split("\ndef ", 1)[0]
    assert "if k[\"changes\"] > len(df):" in block
    assert "Table below shows the latest {len(df)} of {k['changes']:,} tracked" in block
    assert "f\"{k['changes']:,}\"" in block
