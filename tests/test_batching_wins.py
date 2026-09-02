"""Perf wins #4/#5 (batching): the two independent ETL unit-cost reads submit as ONE
parallel run_batch instead of two serial round-trips (and now share the same window, incl.
Last-month bounds), and a multi-query Ask answerer submits its specs as ONE run_batch_mixed
round-trip instead of a serial per-spec loop. (perf audit 2026-09-02)
"""

from __future__ import annotations

import datetime
from pathlib import Path

import sqlglot

from app.data import etl_sql

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- ETL builder: coverage KPI now shares the pipeline board's window ------------------
def test_etl_tag_coverage_honors_bounds_like_the_pipeline_board():
    b = (datetime.date(2026, 8, 1), datetime.date(2026, 9, 1))
    for comp in ("ALFA", "Trexis", "UNKNOWN", "ALL"):
        sqlglot.parse_one(etl_sql.etl_tag_coverage(30, comp, "ALFA_EDW_PRD", "PUBLIC"), read="snowflake")
        sqlglot.parse_one(etl_sql.etl_tag_coverage(30, comp, bounds=b), read="snowflake")
    cov = etl_sql.etl_tag_coverage(30, "ALFA", bounds=b)
    pipe = etl_sql.etl_cost_by_pipeline(30, "ALFA", bounds=b)
    win = "q.START_TIME >= '2026-08-01' AND q.START_TIME < '2026-09-01'"
    assert win in cov and win in pipe                       # same QUERY_HISTORY window under bounds
    cred = "START_TIME >= '2026-08-01' AND START_TIME < '2026-09-01'"
    assert cred in cov and cred in pipe                     # same QUERY_ATTRIBUTION window too
    # trailing (no bounds) still clamps to -days
    assert "DATEADD('day', -30" in etl_sql.etl_tag_coverage(30, "ALFA")


# --- ETL reads batch instead of two serial run() calls ---------------------------------
def test_etl_section_submits_both_reads_in_one_batch():
    uc = _src("app/ui/pages/cost_parts/unit_costs.py")
    seg = uc.split("Run ETL unit-cost scan", 1)[1]
    assert 'run_batch([' in seg
    assert '"key": "cov"' in seg and '"key": "pipe"' in seg   # both members present
    assert 'tier="historical"' in seg
    # the coverage read is served from the batch, not a mandatory serial run() before it
    assert '_etl_pf.get("cov")' in seg and '_etl_pf.get("pipe")' in seg


# --- Ask answerer batches its specs (one parallel round-trip) ---------------------------
def test_ask_answerer_uses_run_batch_mixed_not_a_serial_loop():
    ask_py = _src("app/ui/pages/ask.py")
    assert "run_batch_mixed(batch, page=_PAGE)" in ask_py
    # the old serial per-spec read is gone
    assert "res = run(spec.sql" not in ask_py
    # a query FAILURE still short-circuits (never fed to analyze as a false "no data")
    assert "if res is None or not res.ok:" in ask_py
    assert "return" in ask_py.split("run_batch_mixed", 1)[1].split("ans.analyze", 1)[0]
