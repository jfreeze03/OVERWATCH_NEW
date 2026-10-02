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


# R1-090 review: with Compile heavy reachable, the "resize not indicated" captions name each class's own fix.
_PLAN = {"QUERY_PARAMETERIZED_HASH": "h1", "SAMPLE_TEXT": "SELECT a.x FROM big_fact a JOIN dim b ON a.k = b.k",
         "RUNS": 400, "COMPILE_PCT": 62.5, "AVG_TOTAL_S": 8.0}
_CHATTER = {"QUERY_PARAMETERIZED_HASH": "h2", "SAMPLE_TEXT": "SELECT something small", "RUNS": 400,
            "COMPILE_PCT": 95.0, "AVG_TOTAL_S": 0.3}


def test_compile_heavy_only_families_never_get_the_chatter_remedy():
    two_plans = pd.DataFrame([_PLAN, dict(_PLAN, QUERY_PARAMETERIZED_HASH="h3")])
    summ = cs_driver.driver_summary(cs_driver.classify_families(two_plans))
    assert summ["not_indicated"] == 2 and summ["not_indicated_by_class"] == {cs_driver.COMPILE_HEAVY: 2}
    text = cs_driver.not_indicated_remedies(summ)
    assert text == "2 compile heavy — simplify the plan or parameterize huge IN-lists (the SQL author)"
    assert "cache metadata" not in text and "polling" not in text


def test_a_mixed_table_names_each_class_count_with_its_own_fix():
    sleep = {"QUERY_PARAMETERIZED_HASH": "h4", "SAMPLE_TEXT": "select system$wait(10)", "RUNS": 900,
             "COMPILE_PCT": 0.7, "AVG_TOTAL_S": 10.0}
    normal = {"QUERY_PARAMETERIZED_HASH": "h5", "SAMPLE_TEXT": "SELECT * FROM t", "RUNS": 50,
              "COMPILE_PCT": 10.0, "AVG_TOTAL_S": 4.0}
    df = cs_driver.classify_families(pd.DataFrame([_PLAN, _CHATTER, sleep, normal]))
    summ = cs_driver.driver_summary(df)
    assert (summ["total"], summ["not_indicated"]) == (4, 3)              # the NORMAL family is not counted
    assert cs_driver.not_indicated_remedies(summ) == (
        "1 compile heavy — simplify the plan or parameterize huge IN-lists (the SQL author); "
        "1 metadata / discovery chatter — behavioural: cache metadata, batch the calls, cut polling / reconnects; "
        "1 sleep polling — move the wait out of Snowflake (the scheduler / task owner)")
    assert "$" not in cs_driver.not_indicated_remedies(summ)             # captions render markdown
    empty = cs_driver.driver_summary(pd.DataFrame())
    assert empty["not_indicated_by_class"] == {} and cs_driver.not_indicated_remedies(empty) == ""


def test_an_unmapped_not_indicated_class_points_at_the_columns_not_a_guessed_fix():
    summ = {"not_indicated_by_class": {cs_driver.COMPILE_HEAVY: 1, "Some future class": 2}}
    assert cs_driver.not_indicated_remedies(summ).endswith(
        "; 2 other — see the Remediation owner column")


@pytest.mark.parametrize("path", ["app/ui/pages/cost_parts/spend.py", "app/ui/pages/operations.py"])
def test_both_family_captions_word_the_fix_per_class(path):
    from tests._source import read
    src = read(path)
    assert "{cs_driver.not_indicated_remedies(_summ)}" in src
    assert "cut polling / reconnects" not in src                         # the one-size remedy is gone
