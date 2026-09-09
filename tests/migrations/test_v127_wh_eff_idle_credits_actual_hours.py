"""V127: idle credit waste reads ACTUAL zero-query-hour credits (not a pro-rate).

MART_WAREHOUSE_EFFICIENCY_DAILY stored only an hour-count IDLE_PCT, so the reader
mart27_sql.eff_idle_analysis derived idle spend by PRO-RATING the day's total credits by
that fraction — while the live fallback insights_sql.idle_warehouse_analysis SUMs the ACTUAL
credits burned in zero-query hours. Both feed the same Optimize idle-$ KPI via run_mart_first,
so the headline flipped with mart warmth and the pro-rate over-stated idle for scale-out
warehouses (round-28b twin-divergence [2]).

V127 ADDs an IDLE_CREDITS column and re-derives SP_LOAD_MARTS_V27 from V126 so the wh_eff arm
stores it by joining hourly metering to the span-expanded active hours (mirroring the live
twin). Unlike V125/V126, V127 is NOT proc-only — it carries one ALTER TABLE ADD COLUMN. The
readers COALESCE(IDLE_CREDITS, legacy pro-rate) so pre-re-stamp rows degrade gracefully.
These locks pin the schema change, the loader formula, the reader fallback, that BOTH prior
loader fixes (V125 MFA-gap, V126 task-graph) survived, and byte-fidelity vs V126.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_MIG_DIR = _ROOT / "snowflake" / "migrations"
_V127 = (_MIG_DIR / "V127__wh_eff_idle_credits_actual_hours.sql").read_text(encoding="utf-8")
_V126 = (_MIG_DIR / "V126__task_graph_wh_credits_all_attempts.sql").read_text(encoding="utf-8")


def _proc_block(sql: str) -> str:
    start = sql.index("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27")
    end = sql.index("\n$$;", start) + len("\n$$;")
    return sql[start:end]


def _without_wh_eff_arm(proc: str) -> str:
    """proc body with the [1] warehouse-efficiency MERGE arm sliced out, so the rest can be
    byte-compared across V126/V127 (V127 changes ONLY that arm inside the proc)."""
    start = proc.index("-- [1] warehouse efficiency")
    end = proc.index("loaded := loaded || 'wh_eff ';") + len("loaded := loaded || 'wh_eff ';")
    return proc[:start] + proc[end:]


def test_v127_guarded_and_versioned() -> None:
    assert "EXCEPTION (-20127" in _V127 and "RAISE not_ready;" in _V127
    assert "IF (v < 126) THEN" in _V127
    assert "SELECT 127 AS VERSION" in _V127
    assert "WHERE VERSION = 127" in _V127
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27" in _V127
    # forward-healing: no in-migration CALL (the nightly task / RUN_NEXT re-stamp populates)
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27" not in _V127


def test_v127_adds_the_idle_credits_column_and_no_other_schema_change() -> None:
    # the ONE schema change is the additive, idempotent column (V127 is not proc-only)
    assert "ALTER TABLE DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY" in _V127
    assert "ADD COLUMN IF NOT EXISTS IDLE_CREDITS NUMBER(18,4)" in _V127
    # nothing destructive / no table (re)creation
    assert "CREATE TABLE" not in _V127
    assert "DROP TABLE" not in _V127 and "DROP COLUMN" not in _V127
    assert _V127.count("ALTER TABLE") == 1


def test_v127_loader_stores_actual_zero_query_hour_credits() -> None:
    body = _proc_block(_V127)
    arm = body[body.index("-- [1] warehouse efficiency"):body.index("loaded := loaded || 'wh_eff ';")]
    # a dedicated CTE sums credits in warehouse-hours with NO active (span-expanded) query,
    # mirroring the live twin insights_sql.idle_warehouse_analysis
    assert "m_idle AS (" in arm
    assert "SUM(IFF(a.HOUR_TS IS NULL, COALESCE(mh.CREDITS_USED, 0), 0)) AS IDLE_CREDITS" in arm
    # joined to DISTINCT active hours so a metering slice is never fanned out
    assert "(SELECT DISTINCT WAREHOUSE_NAME, HOUR_TS FROM qh) a" in arm
    assert "LEFT JOIN m_idle mi" in arm
    # and the new column is written through the MERGE (update + insert)
    assert "IDLE_CREDITS = s.IDLE_CREDITS" in arm
    assert "s.CREDITS_PER_QUERY, s.IDLE_CREDITS)" in arm


def test_v127_preserves_the_v125_and_v126_loader_fixes() -> None:
    body = _proc_block(_V127)
    # V125 MFA-gap COALESCE canonical still present, bare form gone
    assert "WHERE U.DELETED_ON IS NULL AND COALESCE(U.DISABLED, FALSE) = FALSE" in body
    assert "AND U.DISABLED = FALSE" not in _V127
    # V126 task-graph all-attempts still present: TERMINAL_RN counts, no terminal-collapse
    # QUALIFY in the task-graph credit arm, and the "accepted" ledger note stays gone
    tg = body[body.index("MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY"):]
    tg_arm = tg[:tg.index("\n            ) s")]
    assert "WITH attempts AS (" in tg_arm and "AS TERMINAL_RN" in tg_arm
    assert "QUALIFY ROW_NUMBER() OVER (" not in tg_arm
    assert "contextual pipeline cost, not the authoritative ledger" not in _V127


def test_v127_differs_from_v126_only_in_the_wh_eff_arm() -> None:
    """Byte-fidelity: inside the proc, ONLY the warehouse-efficiency arm changed vs V126.
    (The ALTER TABLE, header, guard and version stamp live OUTSIDE the proc body.)"""
    assert _without_wh_eff_arm(_proc_block(_V126)) == _without_wh_eff_arm(_proc_block(_V127)), \
        "V127 changed the proc body OUTSIDE the warehouse-efficiency arm"


def test_v127_readers_use_stored_idle_credits_with_prorate_fallback() -> None:
    src = (_ROOT / "app" / "data" / "mart27_sql.py").read_text(encoding="utf-8")
    idle = src.split("def eff_idle_analysis", 1)[1].split("\ndef ", 1)[0]
    sizing = src.split("def eff_sizing_profile", 1)[1].split("\ndef ", 1)[0]
    # both read the stored column, falling back to the legacy pro-rate for un-restamped rows
    assert "SUM(COALESCE(IDLE_CREDITS, CREDITS_TOTAL * COALESCE(IDLE_PCT, 0) / 100))" in idle
    assert "SUM(COALESCE(e.IDLE_CREDITS, e.CREDITS_TOTAL * COALESCE(e.IDLE_PCT, 0) / 100))" in sizing
    # the bare pro-rate (no COALESCE to the stored column) is gone from both
    assert "SUM(CREDITS_TOTAL * COALESCE(IDLE_PCT, 0) / 100)" not in idle
    assert "SUM(e.CREDITS_TOTAL * COALESCE(e.IDLE_PCT, 0) / 100)" not in sizing


def test_v127_is_the_latest_full_loader_definition() -> None:
    defs = sorted(p for p in _MIG_DIR.glob("V[0-9]*.sql")
                  if "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27"
                  in p.read_text(encoding="utf-8"))
    assert defs[-1].name == "V127__wh_eff_idle_credits_actual_hours.sql"


def test_v127_floor_tracks_the_tip() -> None:
    v = (_ROOT / "snowflake" / "validate.sql").read_text(encoding="utf-8")
    assert "V001..V137 applied" in v
    assert "BETWEEN 1 AND 137) = 137" in v


def test_v127_is_tracked_in_deploy_and_admin_surfaces() -> None:
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V127__wh_eff_idle_credits_actual_hours.sql" in (_ROOT / rel).read_text(encoding="utf-8")
    assert "127:" in (_ROOT / "app" / "ui" / "pages" / "admin.py").read_text(encoding="utf-8")
