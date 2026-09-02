"""Regression locks for bug-hunt round 17 — defects the adversarial sweep found in the fresh
perf work-stream (v4.441-v4.450).

BUNDLE  queries_health_bundle (v4.444) had NO ORDER BY, so the read layer's DEFAULT_MAX_ROWS
        transport cap kept an ARBITRARY subset and could drop the GRP=3 grand-total (summary)
        row / the heaviest error families before split_health_bundle ran — the replaced
        failures_by_error had ORDER BY FAILURES DESC LIMIT 50 in-SQL. Fix: ORDER BY GRP DESC,
        FAILURES DESC floats the summary + top families first so they survive the cap; the spec
        caps at 200 (>> 1 summary + top-50).
TCO     the Optimize Object-TCO 'Storage $/mo' help claimed 'incl. clone-retained' even on the
        storage_waste fallback frame that carries no clone column (clone silently 0).
CAPTION the storage-drill captions claimed 'Current on-disk snapshot (TABLE_STORAGE_METRICS) ...
        instantaneous' while v4.449 serves them mart-first from the daily MART_TABLE_STORAGE_DAILY.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import sqlglot

from app.data import ops_sql

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- BUNDLE: the summary row + top failures survive the transport row cap ---------------
def test_health_bundle_orders_so_summary_and_top_failures_survive_the_cap():
    sql = ops_sql.queries_health_bundle(30, "ALFA", schema_contains="PUBLIC")
    sqlglot.parse_one(sql, read="snowflake")
    # GRP DESC floats the GRP=3 grand-total (summary) row first, FAILURES DESC the heaviest
    # families next, so the read layer's row cap can't drop the rows split_health_bundle needs.
    assert "ORDER BY GRP DESC, FAILURES DESC" in sql
    # and the Operations bundle spec caps well above 1 summary + top-50 failures.
    seg = _src("app/ui/pages/operations.py").split('"key": "health"', 1)[1].split("], page=_PAGE", 1)[0]
    assert '"max_rows": 200' in seg


def test_split_recovers_summary_and_top_failures_from_an_ordered_then_capped_frame():
    # Behavioral: order the shaped frame the way ORDER BY GRP DESC, FAILURES DESC produces, then
    # head(51) to simulate the transport cap keeping only the first rows — split still finds the
    # GRP=3 summary and the true top-50 families (the bug dropped an arbitrary subset instead).
    rows = [{"GRP": 3, "ERROR_CODE": None, "ERROR_MESSAGE": None, "QUERY_COUNT": 900,
             "FAILED_COUNT": 12, "P95_ELAPSED_SEC": 5, "QUEUED_SEC": 1, "SPILL_REMOTE_GB": 0,
             "FAILURES": 12, "USERS_AFFECTED": 3, "LAST_SEEN": "2026-08-15"}]
    rows.extend(                              # 60 families, biggest first (as ORDER BY yields)
        {"GRP": 0, "ERROR_CODE": f"E{i:04d}", "ERROR_MESSAGE": f"err {i}",
         "QUERY_COUNT": 60 - i, "FAILED_COUNT": 60 - i, "P95_ELAPSED_SEC": 1,
         "QUEUED_SEC": 0, "SPILL_REMOTE_GB": 0, "FAILURES": 60 - i,
         "USERS_AFFECTED": 1, "LAST_SEEN": "2026-08-14"}
        for i in range(60))
    capped = pd.DataFrame(rows).head(51)      # summary(1) + top-50 families survive the cap
    summary, fails = ops_sql.split_health_bundle(capped)
    assert int(summary.iloc[0]["QUERY_COUNT"]) == 900          # grand-total row survived
    assert len(fails) == 50 and int(fails.iloc[0]["FAILURES"]) == 60   # true heaviest family survived


# --- TCO: the clone-retained help is honest on the fallback path ------------------------
def test_object_tco_clone_help_is_honest_on_the_fallback_path():
    opt = _src("app/ui/pages/cost_parts/optimize.py")
    assert "GB total incl. retention + clone-retained." not in opt   # not the unconditional claim
    assert "_has_clone" in opt
    assert "clone-retained not measured on this fallback path" in opt


# --- CAPTION: storage drills don't claim an instantaneous live source ------------------
def test_storage_drill_captions_do_not_claim_instantaneous_live_source():
    for rel in ("app/ui/pages/cost_parts/spend.py", "app/ui/pages/cost_parts/optimize.py"):
        src = _src(rel)
        assert "Current on-disk snapshot (TABLE_STORAGE_METRICS)" not in src
        assert "an instantaneous figure" not in src
        assert "point-in-time" in src        # honest: a snapshot (daily mart or live scan), not "current/instantaneous"
