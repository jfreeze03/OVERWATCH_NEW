"""Locks for the Codex r22 shipped set (v4.37.0).

Adjudication: docs/reviews/CODEX_R22_ADJUDICATION_20260712.md. The V042
derivations mirror outputs' generator: every re-derived proc is rebuilt
from V041's copy (SP_PURGE_FACTS from V017's) plus enumerated edits, and
the structural anchors of each edit are asserted here.
"""

from __future__ import annotations

from pathlib import Path

from app.data import insights_sql

_ROOT = Path(__file__).resolve().parents[1]
_MIG = _ROOT / "snowflake" / "migrations"
_V17 = (_MIG / "V017__hardening_v7.sql").read_text(encoding="utf-8")
_V41 = (_MIG / "V041__loader_efficiency.sql").read_text(encoding="utf-8")
_V42 = (_MIG / "V042__codex_r22.sql").read_text(encoding="utf-8")


def _proc(text: str, name: str) -> str:
    start = text.find(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    assert start > 0, name
    open_dd = text.find("$$", start)
    return text[start:text.find("$$;", open_dd + 2) + 3]


def test_v042_guard_version_and_first_fills():
    assert "EXCEPTION (-20042" in _V42 and "RAISE not_ready;" in _V42
    assert "RAISE EXCEPTION (" not in _V42
    assert "IF (v < 41) THEN" in _V42
    assert "SELECT 42 AS VERSION" in _V42
    # the 3-day extract pass fills the new day fact before the board/score read it
    fills = ["CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(3);",
             "CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD();",
             "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_PLATFORM_SCORE(30);"]
    pos = [_V42.index(f) for f in fills]
    assert pos == sorted(pos)
    # no task surgery here — procs swap under the running graph
    assert "CREATE TASK" not in _V42 and "CREATE OR REPLACE TASK" not in _V42


# ---------------------------------------------------------------------------
# r22 #7 — the extract is atomic and the watermark is gated
# ---------------------------------------------------------------------------

def test_extract_arms_are_transactional_and_watermark_gates_on_commit():
    ext = _proc(_V42, "SP_LOAD_QH_EXTRACT")
    assert ext.count("BEGIN TRANSACTION;") == 3          # extract + 2 fact arms
    assert ext.count("COMMIT;") == 3
    assert ext.count("ROLLBACK;") == 3                   # every handler rolls back
    assert "ok BOOLEAN DEFAULT FALSE;" in ext
    assert "ok := TRUE;" in ext
    assert "IF (ok) THEN" in ext                         # watermark + freshness gate
    # the gate sits BEFORE the watermark merge
    assert ext.index("IF (ok) THEN") < ext.index("MERGE INTO DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS")
    # still exactly one live QUERY_HISTORY scan per cycle (R1 holds)
    assert ext.count("FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY") == 1
    # v4.36.1's derivation base is otherwise intact: the V041 fact arm text
    # (its DELETE+INSERT body) still appears verbatim inside the v2 proc
    v41_ext = _proc(_V41, "SP_LOAD_QH_EXTRACT")
    arm_start = v41_ext.index("    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY")
    arm_end = v41_ext.index("GROUP BY 1, 2, 3, 4, 5;", arm_start) + len("GROUP BY 1, 2, 3, 4, 5;")
    assert v41_ext[arm_start:arm_end] in ext


# ---------------------------------------------------------------------------
# r22 #1 — FACT_QUERY_DAILY: loader arm, board + score consumers, backfill
# ---------------------------------------------------------------------------

def test_day_fact_arm_obeys_shape_law_and_matches_the_hourly_conventions():
    ext = _proc(_V42, "SP_LOAD_QH_EXTRACT")
    arm = ext.split("MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY", 1)[1]
    arm = arm.split("END;", 1)[0]
    assert "COMPANY_FOR_WAREHOUSE(g.WAREHOUSE_NAME)" in arm   # UDF outside aggregation
    assert "COMPANY_FOR_WAREHOUSE(MAX(" not in arm            # never the V029 mistake
    assert "IFF(EXECUTION_STATUS = 'FAIL', 1, 0)" in arm      # V002 convention
    assert "FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT" in arm # extract-fed, no new scan
    ddl = _V42.split("CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY (", 1)[1].split(");", 1)[0]
    assert "P95" not in ddl                                   # no day-grain scalar p95, on purpose


def test_board_and_score_read_the_day_fact():
    board = _proc(_V42, "SP_REFRESH_EXEC_BOARD")
    assert "FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY" in board
    assert "FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY" not in board
    # only the qh_daily source swapped — the rest of V041's board is intact
    v41_board = _proc(_V41, "SP_REFRESH_EXEC_BOARD")
    tail = v41_board.split("    tk_daily AS (", 1)[1]
    assert tail in board
    score = _proc(_V42, "SP_LOAD_PLATFORM_SCORE")
    assert "FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY" in score
    assert "FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY" not in score
    rec = _proc(_V42, "SP_NIGHTLY_RECONCILE")
    assert "DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY" in rec
    bf = (_ROOT / "snowflake" / "backfill_365.sql").read_text(encoding="utf-8")
    assert "INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY" in bf
    assert bf.index("FACT_QUERY_DAILY") < bf.index("SP_LOAD_QH_EXTRACT(90)")
    td = (_ROOT / "snowflake" / "teardown.sql").read_text(encoding="utf-8")
    assert "DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY;" in td
    view = _V42.split("MART_SOURCE_FRESHNESS AS", 1)[1].split("-- ------", 1)[0]
    assert view.count("UNION ALL") == 23 and "'FACT_QUERY_DAILY'" in view


# ---------------------------------------------------------------------------
# r22 #2 / #10 / #15 — diag backfill, purge coverage, AI usage stamps
# ---------------------------------------------------------------------------

def test_ops_diag_allows_wide_explicit_backfills():
    diag = _proc(_V42, "SP_LOAD_OPS_DIAG")
    assert "LEAST(COALESCE(DAYS_BACK, 2), 400))::INT" in diag
    bf = (_ROOT / "snowflake" / "backfill_365.sql").read_text(encoding="utf-8")
    assert bf.index("SP_LOAD_QH_EXTRACT(90)") < bf.index("SP_LOAD_OPS_DIAG(90)")
    # the recurring task (V041 file) still passes 2 — unchanged
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_OPS_DIAG(2);" in _V41


def test_purge_covers_the_v027_and_v041_tables():
    purge = _proc(_V42, "SP_PURGE_FACTS")
    # V017's body survives verbatim up to its original last delete
    v17 = _proc(_V17, "SP_PURGE_FACTS")
    head_17 = v17.split("    RETURN ", 1)[0]
    assert purge.startswith(head_17.split("    DELETE FROM")[0])
    for tbl, col in (("FACT_QUERY_ROLE_HOURLY", "HOUR_TS"),
                     ("FACT_QUERY_SCHEMA_HOURLY", "HOUR_TS"),
                     ("MART_OPS_DIAG_HOURLY", "HOUR_TS"),
                     ("MART_INCIDENT_TIMELINE", "EVENT_TS"),
                     ("MART_WAREHOUSE_EFFICIENCY_DAILY", "DAY"),
                     ("MART_QUERY_FAMILY_DAILY", "DAY"),
                     ("MART_COST_ALLOCATION_DAILY", "DAY"),
                     ("FACT_COST_ALLOC_XDIM_DAILY", "DAY"),
                     ("MART_TASK_GRAPH_DAILY", "DAY"),
                     ("MART_SECURITY_POSTURE_DAILY", "DAY"),
                     ("FACT_AI_USAGE_DAILY", "DAY"),
                     ("MART_TAG_COVERAGE_DAILY", "DAY"),
                     ("MART_LOCK_WAIT_DAILY", "DAY"),
                     ("MART_PATTERN_COST_DAILY", "DAY"),
                     ("FACT_QUERY_DAILY", "DAY"),
                     ("FACT_PLATFORM_SCORE_DAILY", "DAY")):
        assert f"DELETE FROM DBA_MAINT_DB.OVERWATCH.{tbl}\n     WHERE {col} <" in purge, tbl
    assert purge.count("DELETE FROM") == 24              # 8 original + 16 new


def test_ai_fact_gains_exact_stamps_and_the_tab_stays_live_first():
    assert "ADD COLUMN IF NOT EXISTS EMAIL VARCHAR(320)" in _V42
    assert "ADD COLUMN IF NOT EXISTS FIRST_TS TIMESTAMP_NTZ" in _V42
    marts = _proc(_V42, "SP_LOAD_MARTS_V27")
    assert "ANY_VALUE(u.EMAIL) AS EMAIL" in marts
    assert "MIN(c.USAGE_TIME) AS FIRST_TS" in marts
    assert marts.count("FIRST_TS = s.FIRST_TS") == 2      # code + functions arms
    # owner decision 2026-07-12 stands: the users tab is live-first until the
    # fact proves it can serve the FULL contract
    cb = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "ai_chargeback.py").read_text(encoding="utf-8")
    assert 'source="ACCOUNT_USAGE.CORTEX_CODE_*_USAGE_HISTORY", probe=True' in cb
    assert "ai_code_user_rollup" not in cb


# ---------------------------------------------------------------------------
# r22 #14 / #17 / #20 — app-side ships
# ---------------------------------------------------------------------------

def test_ai_users_section_is_lazy():
    cost = (_ROOT / "app" / "ui" / "pages" / "cost.py").read_text(encoding="utf-8")
    seg = cost.split('section_header("AI users"', 1)[1][:700]
    assert 'st.toggle("Load AI user attribution' in seg
    assert "toggle_cost_hint" in seg
    assert "_ai_users_tab" in seg.split("st.toggle", 1)[1]  # gated, not ambient


def test_query_detail_is_time_bounded_from_the_table_path():
    sql = insights_sql.query_detail("0123456789abcdef", "2026-07-11")
    assert "AND START_TIME >= DATEADD('day', -1, '2026-07-11'::DATE)" in sql
    assert "AND START_TIME < DATEADD('day', 2, '2026-07-11'::DATE)" in sql
    assert "START_TIME >=" not in insights_sql.query_detail("0123456789abcdef")  # manual stays broad
    import pytest
    with pytest.raises(ValueError):
        insights_sql.query_detail("0123456789abcdef", "not-a-date")
    ops = (_ROOT / "app" / "ui" / "pages" / "operations.py").read_text(encoding="utf-8")
    assert "insights_sql.query_detail(target_id, _hint)" in ops


def test_fleet_board_names_its_sampling_bias():
    adm = (_ROOT / "app" / "ui" / "pages" / "admin.py").read_text(encoding="utf-8")
    assert "EXCEPTION-WEIGHTED sample" in adm
    assert "read HIGHER than true fleet latency" in adm
