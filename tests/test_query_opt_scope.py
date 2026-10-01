"""R1-092: Operations ▸ Optimization opportunities says when its KPIs cover only the top-500 fingerprints.

ops_sql.query_opportunity_fingerprints serves the 500 largest-footprint fingerprints (ORDER BY exec / compile
time, LIMIT 500), and the five KPIs (Opportunities, High QOP, Concurrency-starved, Cold-start wait, Memory spill)
were counted from those rows under "this window" help text. run()'s 5,000-row cap never fires on a 500-row
LIMIT, so nothing said the counts were capped -- and the footprint order ignores queue time, so the
queue-bound families the KPIs exist to surface are exactly the ones the cap drops. The builder now carries the
uncapped FINGERPRINTS_TOTAL (counted before the LIMIT) and a capped feed reads "≥ N" with a caption.
"""

from __future__ import annotations

import re

import pandas as pd
import sqlglot

from app.data import ops_sql
from app.logic import query_opt
from tests._source import read


def test_builder_counts_every_fingerprint_before_the_limit():
    sql = ops_sql.query_opportunity_fingerprints(30)
    sqlglot.parse_one(sql, read="snowflake")
    assert "COUNT(*) OVER () AS FINGERPRINTS_TOTAL" in sql
    assert sql.index("FINGERPRINTS_TOTAL") < sql.index("GROUP BY QUERY_PARAMETERIZED_HASH")
    assert re.search(rf"LIMIT {query_opt.FINGERPRINT_CAP}\s*$", sql)


def _feed(n: int, total: int | None) -> pd.DataFrame:
    df = pd.DataFrame({"FINGERPRINT": [f"f{i}" for i in range(n)]})
    if total is not None:
        df["FINGERPRINTS_TOTAL"] = total
    return df


def test_scope_and_lower_bound_labels():
    capped = query_opt.fingerprint_scope(_feed(500, 1400))
    assert capped == {"served": 500, "total": 1400, "capped": True}
    assert query_opt.scoped_count(0, capped) == "≥ 0" and query_opt.scoped_count(1234, capped) == "≥ 1,234"
    whole = query_opt.fingerprint_scope(_feed(120, 120))
    assert whole["capped"] is False and query_opt.scoped_count(7, whole) == "7"
    # an older-shaped frame (no total column) and an empty one are never called capped
    assert query_opt.fingerprint_scope(_feed(500, None))["capped"] is False
    assert query_opt.fingerprint_scope(pd.DataFrame())["capped"] is False
    assert query_opt.fingerprint_scope(None) == {"served": 0, "total": 0, "capped": False}


def test_score_opportunities_output_is_unchanged_by_the_total_column():
    row = {"FINGERPRINT": "a", "SAMPLE_TEXT": "SELECT 1", "QUERY_TYPE": "SELECT", "WAREHOUSE_NAME": "WH",
           "RUNS": 10, "TOTAL_EXEC_SEC": 5.0, "ELAPSED_SEC": 1.0, "FINGERPRINTS_TOTAL": 900}
    scored, _ = query_opt.score_opportunities(pd.DataFrame([row]))
    assert "FINGERPRINTS_TOTAL" not in scored.columns


def test_operations_kpis_read_the_scope_and_disclose_the_cap():
    src = read("app/ui/pages/operations.py")
    block = src.split('section_header("Optimization opportunities"', 1)[1].split("_disp = _scored.head(50)", 1)[0]
    assert "_qscope = query_opt.fingerprint_scope(_qopp.df)" in block
    for var in ("_actionable", "_crit", "_conc", "_cold", "_spill"):
        assert f"query_opt.scoped_count({var}, _qscope)" in block, var
    assert 'if _qscope["capped"]:' in block and "each is a lower bound" in block
