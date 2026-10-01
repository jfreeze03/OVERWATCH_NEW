"""R1-090: the cloud-services classifier reads "no warehouse" from a NULL / 'NONE' WAREHOUSE_NAME VALUE, never
from an absent column.

classify_row treated a frame with no WAREHOUSE_NAME column like a warehouse-less statement, and neither
Cost ▸ Spend compile-heavy builder (live, its per-warehouse drill, nor the mart twin) selects one -- so every
compile-dominated family there read "Metadata chatter" routed to the BI-tool owner, the 2s metadata cut-off
never applied and "Compile heavy" (owner: the SQL author) could never appear. The Operations chatter-families
builder now selects a warehouse indicator so its genuinely warehouse-less families keep the chatter label.
"""

from __future__ import annotations

import pandas as pd
import pytest
import sqlglot

from app.data import chatter_sql, cost_sql, mart27_sql
from app.logic import cs_driver


def _columns(sql: str) -> list[str]:
    return [s.alias_or_name for s in sqlglot.parse_one(sql, read="snowflake").selects]


_SPEND_BUILDERS = {
    "live": cost_sql.compile_heavy_families(30),
    "drill": cost_sql.compile_heavy_families(30, warehouse="WH_ALFA_QA"),
    "mart": mart27_sql.family_compile_heavy(30),
}


@pytest.mark.parametrize("name", sorted(_SPEND_BUILDERS))
def test_spend_frames_without_a_warehouse_column_classify_compile_heavy(name):
    cols = _columns(_SPEND_BUILDERS[name])
    assert "WAREHOUSE_NAME" not in cols                      # the production shape this lock is about
    vals = {"QUERY_PARAMETERIZED_HASH": "h", "SAMPLE_TEXT": "SELECT a.x, b.y FROM big_fact a JOIN dim b ON 1=1",
            "RUNS": 400, "AVG_COMPILE_S": 5.0, "AVG_TOTAL_S": 8.0, "COMPILE_PCT": 62.5, "TOTAL_COMPILE_HOURS": 0.6}
    chatter = dict(vals, QUERY_PARAMETERIZED_HASH="h2", SAMPLE_TEXT="SELECT something small", AVG_COMPILE_S=0.28,
                   AVG_TOTAL_S=0.3, COMPILE_PCT=95.0)
    frame = pd.DataFrame([{c: v[c] for c in cols} for v in (vals, chatter)])
    out = cs_driver.classify_families(frame)
    assert out["DRIVER_CLASS"].tolist() == [cs_driver.COMPILE_HEAVY, cs_driver.METADATA_CHATTER]
    assert out.iloc[0]["REMEDIATION_OWNER"] == cs_driver.remediation_owner(cs_driver.COMPILE_HEAVY)
    assert out.iloc[0]["REMEDIATION_OWNER"] != out.iloc[1]["REMEDIATION_OWNER"]


@pytest.mark.parametrize("wh", [None, "NONE", "", float("nan")])
def test_a_null_warehouse_value_is_still_metadata_chatter(wh):
    row = {"SAMPLE_TEXT": "SHOW SCHEMAS", "COMPILE_PCT": 80.0, "AVG_TOTAL_S": 8.0, "RUNS": 50, "WAREHOUSE_NAME": wh}
    assert cs_driver.classify_row(row)[0] == cs_driver.METADATA_CHATTER
    assert cs_driver.classify_row(pd.Series(row))[0] == cs_driver.METADATA_CHATTER


def test_chatter_families_select_a_deterministic_warehouse_indicator():
    sql = chatter_sql.chatter_families_for_application("DBeaver")
    assert "WAREHOUSE_NAME" in _columns(sql)
    assert "IFF(COUNT_IF(q.WAREHOUSE_NAME IS NULL) * 2 >= COUNT(*), 'NONE', MAX(q.WAREHOUSE_NAME))" in sql
    # a warehouse-less (majority NULL -> 'NONE') slow family stays chatter; a warehouse-backed one is compile heavy
    base = {"SAMPLE_TEXT": "select 1 from t", "COMPILE_PCT": 70.0, "AVG_TOTAL_S": 6.0, "RUNS": 40}
    out = cs_driver.classify_families(pd.DataFrame([dict(base, WAREHOUSE_NAME="NONE"),
                                                    dict(base, WAREHOUSE_NAME="WH_BI")]))
    assert out["DRIVER_CLASS"].tolist() == [cs_driver.METADATA_CHATTER, cs_driver.COMPILE_HEAVY]
