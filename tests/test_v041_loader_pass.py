"""Locks for the V041 loader-efficiency pass (v4.36.0).

Authority: docs/design/V041_LOADER_PASS.md (design freeze 2026-07-12).
The derivation locks REBUILD each re-derived proc from its origin file plus
the enumerated edits and assert byte equality — the V027->V028->V029->V030
(->V031) chain extends to V041. This file also supersedes test_v039's
"loader = V002 + exactly one predicate" claim about the CURRENT loader: that
lock remains true of the V039 FILE; the tip lock lives here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from app.config import DAY_WINDOW_OPTIONS
from app.data import mart27_sql

_ROOT = Path(__file__).resolve().parents[1]
_MIG = _ROOT / "snowflake" / "migrations"
_V02 = (_MIG / "V002__facts.sql").read_text(encoding="utf-8")
_V31 = (_MIG / "V031__scan_tuning_and_tagcov.sql").read_text(encoding="utf-8")
_V39 = (_MIG / "V039__pseudo_warehouse_filter.sql").read_text(encoding="utf-8")
_V41 = (_MIG / "V041__loader_efficiency.sql").read_text(encoding="utf-8")


def _proc(text: str, name: str) -> str:
    start = text.find(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    assert start > 0, name
    open_dd = text.find("$$", start)
    return text[start:text.find("$$;", open_dd + 2) + 3]


def _swap_nth(text: str, needle: str, replacement: str, indices: list[int]) -> str:
    parts = text.split(needle)
    out = parts[0]
    for i, part in enumerate(parts[1:], start=1):
        out += (replacement if i in indices else needle) + part
    return out


# ---------------------------------------------------------------------------
# Guard, version row, first fills, retirement
# ---------------------------------------------------------------------------

def test_v041_guard_version_and_first_fills():
    assert "EXCEPTION (-20041" in _V41
    assert "RAISE not_ready;" in _V41 and "RAISE EXCEPTION (" not in _V41
    assert "IF (v < 40) THEN" in _V41
    assert "SELECT 41 AS VERSION" in _V41
    # extract fills BEFORE the marts read it (3d covers 48h fact + 2d marts)
    fills = [
        "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(3);",
        "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('HOURLY', 2);",
        "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('DAILY', 3);",
        "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_OPS_DIAG(2);",
        "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_PLATFORM_SCORE(30);",
        "CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD();",
    ]
    section = _V41.split("-- First fills", 1)[1].split("-- Resume everything", 1)[0]
    pos = [section.index(f) for f in fills]
    assert pos == sorted(pos)
    # R6: the 10-minute snapshot task retires, suspended THEN dropped; the
    # manual-refresh proc is NOT dropped here.
    s = _V41.index("ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_SNAPSHOT_FRESHNESS SUSPEND;")
    d = _V41.index("DROP TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_SNAPSHOT_FRESHNESS;")
    assert s < d
    assert "DROP PROCEDURE IF EXISTS DBA_MAINT_DB.OVERWATCH.SP_SNAPSHOT_FRESHNESS" not in _V41
    assert "GENERATION NUMBER(18,0)" in _V41  # freshness gains the token column


# ---------------------------------------------------------------------------
# Derivation law: SP_LOAD_MARTS_V27 = V031's proc + the enumerated edits
# ---------------------------------------------------------------------------

_OLD_UNUSED = """                UNION ALL
                SELECT CURRENT_DATE(), 'UNUSED_ROLES_90D', 'ALL', COUNT(*)
                FROM SNOWFLAKE.ACCOUNT_USAGE.ROLES r
                WHERE r.DELETED_ON IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
                      WHERE q.START_TIME >= DATEADD('day', -90, CURRENT_TIMESTAMP())
                        AND q.ROLE_NAME = r.NAME
                  )"""
_NEW_UNUSED = """                UNION ALL
                -- V041 R9: unused-role posture from the role-hour fact, not a
                -- 90d QUERY_HISTORY anti-join. Coverage-gated: HAVING emits NO
                -- row (never a lying zero) until the fact spans the window.
                SELECT CURRENT_DATE(), 'UNUSED_ROLES_90D', 'ALL', COUNT(*)
                FROM SNOWFLAKE.ACCOUNT_USAGE.ROLES r
                WHERE r.DELETED_ON IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_ROLE_HOURLY q
                      WHERE q.HOUR_TS >= DATEADD('day', -90, CURRENT_TIMESTAMP())
                        AND q.ROLE_NAME = r.NAME
                  )
                HAVING (SELECT MIN(HOUR_TS) FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_ROLE_HOURLY)
                       <= DATEADD('day', -89, CURRENT_TIMESTAMP())"""

_ALLOC_END = """            loaded := loaded || 'alloc ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_COST_ALLOCATION_DAILY - other marts unaffected', CURRENT_ROLE();
        END;
"""
_XDIM_ARM = _ALLOC_END + """
        -- [5b] cross-dim allocation fact (V041 R2): persist _OW_ALLOC_BASE at
        -- DAY x WAREHOUSE x DATABASE x USER before it collapses to single-dim.
        -- NO schema grain (cardinality; schema stays live-filtered). Same
        -- expressions as [5], so the day-sums reconcile by construction.
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_COST_ALLOC_XDIM_DAILY t
            USING (
                SELECT DAY, WAREHOUSE_NAME, DATABASE_NAME, USER_NAME,
                       ROUND(SUM(EXEC_MS) / 1000, 1) AS EXEC_SEC,
                       ROUND(SUM(ALLOC_CREDITS), 6) AS ALLOC_CREDITS
                FROM _OW_ALLOC_BASE
                GROUP BY 1, 2, 3, 4
            ) s
            ON t.DAY = s.DAY AND t.WAREHOUSE_NAME = s.WAREHOUSE_NAME
               AND t.DATABASE_NAME = s.DATABASE_NAME AND t.USER_NAME = s.USER_NAME
            WHEN MATCHED THEN UPDATE SET EXEC_SEC = s.EXEC_SEC,
                ALLOC_CREDITS = s.ALLOC_CREDITS, LOAD_TS = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (DAY, WAREHOUSE_NAME, DATABASE_NAME, USER_NAME, EXEC_SEC, ALLOC_CREDITS)
            VALUES (s.DAY, s.WAREHOUSE_NAME, s.DATABASE_NAME, s.USER_NAME, s.EXEC_SEC, s.ALLOC_CREDITS);
            loaded := loaded || 'alloc_xdim ';
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'FACT_COST_ALLOC_XDIM_DAILY - other marts unaffected', CURRENT_ROLE();
        END;
"""

_POSTURE_OPEN = """        -- [7] security posture ------------------------------------------------
        BEGIN
            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_SECURITY_POSTURE_DAILY t
"""
_POSTURE_OPEN_NEW = """        -- [7] security posture ------------------------------------------------
        BEGIN
            -- V041 R11: SHOW -> RESULT_SCAN once daily (V024 precedent), so
            -- Security stops paying a SHOW + parse per render.
            SHOW WAREHOUSES LIMIT 500;
            CREATE OR REPLACE TEMPORARY TABLE _OW_WH_MONITOR AS
            SELECT "name"::VARCHAR AS WAREHOUSE_NAME,
                   COALESCE("resource_monitor"::VARCHAR, 'null') AS RESOURCE_MONITOR,
                   TRY_TO_NUMBER("auto_suspend"::VARCHAR) AS AUTO_SUSPEND
            FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));

            MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_SECURITY_POSTURE_DAILY t
"""

_BREAKGLASS_END = """                  AND ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS')
                  AND CREATED_ON >= DATEADD('day', -30, CURRENT_TIMESTAMP())
"""
_MONITOR_ARMS = _BREAKGLASS_END + """                UNION ALL
                SELECT CURRENT_DATE(), 'WH_NO_MONITOR', 'ALL',
                       COUNT_IF(LOWER(TRIM(RESOURCE_MONITOR)) IN ('null', '', 'none'))
                FROM _OW_WH_MONITOR
                UNION ALL
                SELECT CURRENT_DATE(), 'WH_NO_AUTOSUSPEND', 'ALL',
                       COUNT_IF(COALESCE(AUTO_SUSPEND, 0) <= 0)
                FROM _OW_WH_MONITOR
"""

_HOURLY_CLOSE = """    END IF;

    IF (UPPER(:SCOPE) = 'DAILY') THEN
"""
_HOURLY_FRESH = """
        -- V041 R6: loader-owned freshness — this scope's sources, one commit.
        MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
        USING (
            SELECT SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT
            FROM DBA_MAINT_DB.OVERWATCH.MART_SOURCE_FRESHNESS
            WHERE SOURCE_NAME IN ('MART_WAREHOUSE_EFFICIENCY_DAILY', 'MART_QUERY_FAMILY_DAILY',
                                  'FACT_QUERY_ROLE_HOURLY', 'FACT_QUERY_SCHEMA_HOURLY',
                                  'MART_TAG_COVERAGE_DAILY', 'MART_COST_ALLOCATION_DAILY',
                                  'FACT_COST_ALLOC_XDIM_DAILY', 'MART_TASK_GRAPH_DAILY',
                                  'MART_INCIDENT_TIMELINE')
        ) s
        ON t.SOURCE_NAME = s.SOURCE_NAME
        WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,
            SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,
            STATUS = :loaded
        WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
        VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, :loaded);

    END IF;

    IF (UPPER(:SCOPE) = 'DAILY') THEN
"""

_DAILY_CLOSE = """    END IF;

    RETURN 'V27 marts loaded"""
_DAILY_FRESH = """
        -- V041 R6: loader-owned freshness — this scope's sources, one commit.
        MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
        USING (
            SELECT SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT
            FROM DBA_MAINT_DB.OVERWATCH.MART_SOURCE_FRESHNESS
            WHERE SOURCE_NAME IN ('MART_SECURITY_POSTURE_DAILY', 'FACT_AI_USAGE_DAILY')
        ) s
        ON t.SOURCE_NAME = s.SOURCE_NAME
        WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,
            SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,
            STATUS = :loaded
        WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
        VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, :loaded);

    END IF;

    RETURN 'V27 marts loaded"""


def _expected_marts_proc() -> str:
    marts = _proc(_V31, "SP_LOAD_MARTS_V27")
    marts = marts.replace(_OLD_UNUSED, _NEW_UNUSED)                      # R9
    qh = "FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY"
    marts = _swap_nth(marts, qh, "FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT",
                      [2, 3, 4, 5, 6, 7])                                # R1
    marts = marts.replace(
        """                    FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
                    WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
""",
        """                    FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
                    WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                      AND WAREHOUSE_ID > 0
""")                                                                     # R10 (eff)
    marts = marts.replace(
        """                FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
                WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
""",
        """                FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
                WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())
                  AND WAREHOUSE_ID > 0
""")                                                                     # R10 (alloc)
    marts = marts.replace(_ALLOC_END, _XDIM_ARM)                         # R2
    marts = marts.replace(_POSTURE_OPEN, _POSTURE_OPEN_NEW)              # R11 (show)
    marts = marts.replace(_BREAKGLASS_END, _MONITOR_ARMS)                # R11 (arms)
    marts = marts.replace(_HOURLY_CLOSE, _HOURLY_FRESH)                  # R6 (hourly)
    marts = marts.replace(_DAILY_CLOSE, _DAILY_FRESH)                    # R6 (daily)
    return marts


def test_v041_marts_proc_is_v031_plus_the_enumerated_edits():
    # The anti-drift contract, extended: V027->V028->V029->V030(->V031)->V041.
    assert _proc(_V41, "SP_LOAD_MARTS_V27") == _expected_marts_proc()


def test_v041_marts_proc_consumer_rewiring_counts():
    p = _proc(_V41, "SP_LOAD_MARTS_V27")
    # exactly two live QH scans remain: wh-efficiency q-CTE + posture
    # ADMIN_STMTS_24H (neither is on the design's consumer list)
    assert p.count("FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY") == 2
    assert p.count("FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT") == 6
    assert p.count("AND WAREHOUSE_ID > 0") == 2                          # R10
    # R9 emits NO row (not a lying zero) while the fact is young
    assert "HAVING (SELECT MIN(HOUR_TS) FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_ROLE_HOURLY)" in p
    # V030 shape law still holds everywhere
    assert "COMPANY_FOR_WAREHOUSE(MAX(" not in p
    assert "COMPANY_FOR_DATABASE(MAX(" not in p


# ---------------------------------------------------------------------------
# Derivation law: SP_LOAD_HOURLY_FACTS + the moved FACT_QUERY_HOURLY arm.
# Supersedes test_v039's claim about the CURRENT loader (the V039 file lock
# stays true of that file).
# ---------------------------------------------------------------------------

def _v39_arm_and_rest() -> tuple[str, str]:
    hourly = _proc(_V39, "SP_LOAD_HOURLY_FACTS")
    a = hourly.find("    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY")
    b = hourly.find("GROUP BY 1, 2, 3, 4, 5;\n\n", a)
    arm = hourly[a:b + len("GROUP BY 1, 2, 3, 4, 5;\n")]
    rest = hourly[:a] + hourly[b + len("GROUP BY 1, 2, 3, 4, 5;\n\n"):]
    return arm, rest


def test_v041_hourly_loader_is_v039_minus_the_moved_arm_plus_freshness():
    _arm, rest = _v39_arm_and_rest()
    expected = rest.replace(
        "    RETURN 'hourly facts loaded';",
        """    -- V041 R6: loader-owned freshness (FACT_QUERY_HOURLY moved to the extract).
    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t
    USING (
        SELECT 'FACT_WAREHOUSE_DAILY' AS SOURCE_NAME, MAX(LOAD_TS) AS LAST_LOAD_TS,
               COUNT(*) AS ROW_COUNT
        FROM DBA_MAINT_DB.OVERWATCH.FACT_WAREHOUSE_DAILY
    ) s
    ON t.SOURCE_NAME = s.SOURCE_NAME
    WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,
        SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,
        STATUS = 'loader'
    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)
    VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, 'loader');

    RETURN 'hourly facts loaded';""")
    assert _proc(_V41, "SP_LOAD_HOURLY_FACTS") == expected


def test_v041_extract_carries_the_v002_fact_arm_verbatim_from_swapped():
    arm, _rest = _v39_arm_and_rest()
    expected_arm = arm.replace("FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY",
                               "FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT")
    ext = _proc(_V41, "SP_LOAD_QH_EXTRACT")
    assert expected_arm in ext
    # exactly ONE live QUERY_HISTORY scan per hourly cycle lives here (R1)
    assert ext.count("FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY") == 1
    # watermark mode: 45-min overlap, 48h first run, 3-day clamp/retention
    assert "DATEADD('minute', -45, MAX(WM_TS))" in ext
    assert "DATEADD('hour', -48, CURRENT_TIMESTAMP())" in ext
    assert ext.count("DATEADD('day', -3, CURRENT_TIMESTAMP())") >= 2
    assert "WHERE SOURCE = 'QH_EXTRACT'" in ext


def test_v041_daily_loader_is_v002_plus_watermark_bounds_and_tail():
    daily = _proc(_V02, "SP_LOAD_DAILY_FACTS")
    daily = daily.replace("DATEADD('day', -5, CURRENT_DATE())", ":lo_metering::DATE")
    daily = daily.replace("DATEADD('day', -3, CURRENT_DATE())", ":lo_short::DATE")
    daily = daily.replace("AS\n$$\nBEGIN\n", """AS
$$
DECLARE
    wm TIMESTAMP_NTZ;           -- V041 R5: last successful daily load
    lo_metering TIMESTAMP_NTZ;  -- watermark - 1d overlap (default -5d, clamp -30d)
    lo_short TIMESTAMP_NTZ;     -- watermark - 1d overlap (default -3d, clamp -30d)
BEGIN
    SELECT MAX(WM_TS) INTO :wm
    FROM DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS WHERE SOURCE = 'DAILY_FACTS';
    lo_metering := GREATEST(COALESCE(DATEADD('day', -1, :wm),
                                     DATEADD('day', -5, CURRENT_DATE())::TIMESTAMP_NTZ),
                            DATEADD('day', -30, CURRENT_DATE())::TIMESTAMP_NTZ);
    lo_short := GREATEST(COALESCE(DATEADD('day', -1, :wm),
                                  DATEADD('day', -3, CURRENT_DATE())::TIMESTAMP_NTZ),
                         DATEADD('day', -30, CURRENT_DATE())::TIMESTAMP_NTZ);
""")
    daily = daily.replace("    RETURN 'daily facts loaded';",
                          _V41.split("-- V041 R5+R6: advance the watermark; loader-owned freshness.", 1)[1]
                          .join(["    -- V041 R5+R6: advance the watermark; loader-owned freshness.", ""])
                          .split("    RETURN 'daily facts loaded';", 1)[0]
                          + "    RETURN 'daily facts loaded';")
    assert _proc(_V41, "SP_LOAD_DAILY_FACTS") == daily


# ---------------------------------------------------------------------------
# Extract-consumer contract: every consumer's columns ⊆ the extract projection
# ---------------------------------------------------------------------------

# QUERY_HISTORY columns the extract deliberately does NOT carry — a consumer
# referencing one of these against the extract is the drift this lock kills.
_NOT_PROJECTED = ("SESSION_ID", "ROOT_QUERY_ID", "CLUSTER_NUMBER", "QUERY_LOAD_PERCENT",
                  "TRANSACTION_ID", "OUTBOUND_DATA_TRANSFER_BYTES", "CREDITS_USED_CLOUD_SERVICES",
                  "PARTITIONS_SCANNED", "PARTITIONS_TOTAL", "END_TIME", "RELEASE_VERSION")


def _extract_projection() -> set[str]:
    ddl = _V41.split("CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT (", 1)[1]
    ddl = ddl.split(");", 1)[0]
    return {ln.strip().split()[0] for ln in ddl.splitlines() if ln.strip()}


def _consumer_segments() -> list[str]:
    """Every SELECT-ish segment that reads the extract, across V041."""
    return [_V41[max(0, m.start() - 3000):m.start()]
            for m in re.finditer(r"FROM DBA_MAINT_DB\.OVERWATCH\.OW_QH_EXTRACT", _V41)]


def test_every_extract_consumer_stays_inside_the_projection():
    proj = _extract_projection()
    # the columns consumers actually name must all be projected
    for col in ("START_TIME", "WAREHOUSE_NAME", "DATABASE_NAME", "SCHEMA_NAME", "USER_NAME",
                "ROLE_NAME", "QUERY_TYPE", "EXECUTION_STATUS", "ERROR_CODE", "ERROR_MESSAGE",
                "TOTAL_ELAPSED_TIME", "EXECUTION_TIME", "COMPILATION_TIME",
                "QUEUED_OVERLOAD_TIME", "QUEUED_PROVISIONING_TIME",
                "BYTES_SPILLED_TO_REMOTE_STORAGE", "BYTES_SCANNED",
                "PERCENTAGE_SCANNED_FROM_CACHE", "QUERY_TAG", "QUERY_PARAMETERIZED_HASH",
                "QUERY_TEXT", "QUERY_ID", "WAREHOUSE_SIZE"):
        assert col in proj, col
    segs = _consumer_segments()
    assert len(segs) >= 8  # 6 mart arms + fact arm + R7 (x2 arms)
    for seg in segs:
        # a consumer's SELECT block: the nearest SELECT before the FROM
        body = seg[seg.rindex("SELECT"):] if "SELECT" in seg else seg
        for banned in _NOT_PROJECTED:
            assert banned not in body, f"extract consumer references unprojected {banned}"


def test_extract_consumers_in_the_loader_match_the_design_list_exactly():
    p = _proc(_V41, "SP_LOAD_MARTS_V27")
    arms = {
        "MART_QUERY_FAMILY_DAILY": True, "FACT_QUERY_ROLE_HOURLY": True,
        "FACT_QUERY_SCHEMA_HOURLY": True, "MART_TAG_COVERAGE_DAILY": True,
        "_OW_ALLOC_BASE": True, "MART_INCIDENT_TIMELINE": True,
    }
    for name in arms:
        blk_start = p.index(name)
        blk = p[blk_start:blk_start + 4000]
        assert "OW_QH_EXTRACT" in blk or name == "MART_INCIDENT_TIMELINE", name
    # and the wh-efficiency arm deliberately did NOT move (not on the list)
    eff = p.split("-- [1] warehouse efficiency", 1)[1].split("-- [2]", 1)[0]
    assert "FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY" in eff
    assert "OW_QH_EXTRACT" not in eff


# ---------------------------------------------------------------------------
# R4 — exec board v2: window cross-lock, atomic swap, dead panels retired
# ---------------------------------------------------------------------------

def test_board_windows_match_the_config_tuple():
    board = _proc(_V41, "SP_REFRESH_EXEC_BOARD")
    windows_cte = board.split("windows AS (", 1)[1].split("),", 1)[0]
    built = tuple(int(n) for n in re.findall(r"SELECT (\d+)(?: AS WINDOW_DAYS)?", windows_cte))
    assert built == tuple(DAY_WINDOW_OPTIONS), (
        "exec-board loader windows must equal app.config.DAY_WINDOW_OPTIONS — "
        "the 7/30-only drift class (14/60/90 always falling to the 13-month "
        f"live scan) dies here. built={built} config={tuple(DAY_WINDOW_OPTIONS)}")


def test_board_v2_swaps_atomically_and_drops_the_dead_panels():
    board = _proc(_V41, "SP_REFRESH_EXEC_BOARD")
    ins = board.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.OW_EXEC_BOARD_STAGE")
    swp = board.index("SWAP WITH DBA_MAINT_DB.OVERWATCH.OW_EXEC_BOARD_STAGE")
    assert ins < swp
    assert "DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_EXEC_BOARD" not in board  # the old gap
    for dead in ("PRESSURE_QUEUE", "PRESSURE_SPILL", "DB_MIX"):
        assert dead not in board, f"{dead} has zero readers — stop producing it"
    # single-pass sources: one grouped pass per fact, unpivoted KPI arms
    assert board.count("FROM qh_kpi") == 4
    assert board.count("FROM tk_kpi") == 2
    assert board.count("FROM wh_kpi") == 1


# ---------------------------------------------------------------------------
# Task graph: every task touched here resumes; both roots' trees re-enable
# ---------------------------------------------------------------------------

def test_every_v041_task_has_a_matching_resume_or_dependents_enable():
    created = re.findall(
        r"CREATE (?:OR REPLACE )?TASK (?:IF NOT EXISTS )?DBA_MAINT_DB\.OVERWATCH\.(\w+)", _V41)
    assert set(created) == {"TASK_QH_EXTRACT", "TASK_LOAD_MARTS_V27_HOURLY",
                            "TASK_OPS_DIAG_HOURLY", "TASK_NIGHTLY_RECONCILE",
                            "TASK_PLATFORM_SCORE_DAILY"}
    for name in created:
        assert f"ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.{name} RESUME;" in _V41, name
    # both roots suspend for surgery, resume, and re-enable their WHOLE trees
    # at the very end — the 07-12 alert-outage class stays impossible.
    for root in ("TASK_LOAD_HOURLY", "TASK_LOAD_DAILY"):
        assert f"ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.{root} SUSPEND;" in _V41
        assert f"ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.{root} RESUME;" in _V41
        assert f"SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.{root}');" in _V41
    # phasing: extract is the hourly root's child; the marts task consumes it
    assert "TASK_QH_EXTRACT\n    WAREHOUSE = WH_ALFA_OVERWATCH\n    AFTER DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY" in _V41
    assert "TASK_LOAD_MARTS_V27_HOURLY\n    WAREHOUSE = WH_ALFA_OVERWATCH\n    AFTER DBA_MAINT_DB.OVERWATCH.TASK_QH_EXTRACT" in _V41
    # resumes come AFTER the first fills (a resumed child must not race them)
    assert _V41.index("CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD();") < \
        _V41.index("ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_QH_EXTRACT RESUME;")


def test_teardown_and_backfill_cover_the_new_objects():
    td = (_ROOT / "snowflake" / "teardown.sql").read_text(encoding="utf-8")
    for obj in ("TASK_QH_EXTRACT", "TASK_OPS_DIAG_HOURLY", "TASK_NIGHTLY_RECONCILE",
                "TASK_PLATFORM_SCORE_DAILY", "SP_LOAD_QH_EXTRACT(FLOAT)",
                "SP_LOAD_OPS_DIAG(FLOAT)", "SP_LOAD_PLATFORM_SCORE(FLOAT)",
                "SP_NIGHTLY_RECONCILE()", "OW_QH_EXTRACT", "OW_LOAD_WATERMARKS",
                "FACT_COST_ALLOC_XDIM_DAILY", "MART_OPS_DIAG_HOURLY",
                "FACT_PLATFORM_SCORE_DAILY", "OW_EXEC_BOARD_STAGE"):
        assert obj in td, obj
    bf = (_ROOT / "snowflake" / "backfill_365.sql").read_text(encoding="utf-8")
    # extract fills FIRST — the marts read it now
    assert bf.index("SP_LOAD_QH_EXTRACT(90)") < bf.index("SP_LOAD_MARTS_V27('HOURLY', 90)")


# ---------------------------------------------------------------------------
# Numeric recon (r18 #5's ask): xdim day-sums == single-dim day-sums, and
# allocation never exceeds metered credits. Pandas mirror of the SQL.
# ---------------------------------------------------------------------------

def _synthetic_alloc_base() -> tuple[pd.DataFrame, pd.DataFrame]:
    import itertools
    import random
    rnd = random.Random(41)
    hours = pd.date_range("2026-07-09", periods=30, freq="h")
    whs, users, dbs, schemas, roles = (
        ["WH_A", "WH_B"], ["U1", "U2", "U3"], ["D1", "D2", "NONE"], ["S1", "S2"], ["R1", "R2"])
    rows = []
    for h, w in itertools.product(hours, whs):
        for u, d, s, r in itertools.product(users, dbs, schemas, roles):
            if rnd.random() < 0.55:
                rows.append({"HOUR_TS": h, "WAREHOUSE_NAME": w, "USER_NAME": u,
                             "DATABASE_NAME": d, "SCHEMA_NAME": s, "ROLE_NAME": r,
                             "EXEC_MS": rnd.randint(1, 50_000)})
    q = pd.DataFrame(rows)
    metering = pd.DataFrame(
        [{"HOUR_TS": h, "WAREHOUSE_NAME": w, "HOUR_CREDITS": rnd.uniform(0.0, 2.0)}
         for h, w in itertools.product(hours, whs)])
    # one warehouse-hour with credits but NO queries: its credits must not
    # be allocated anywhere (the <= metering inequality is strict there)
    q = q[~((q["HOUR_TS"] == hours[0]) & (q["WAREHOUSE_NAME"] == "WH_A"))]
    tot = q.groupby(["HOUR_TS", "WAREHOUSE_NAME"], as_index=False)["EXEC_MS"].sum() \
           .rename(columns={"EXEC_MS": "TOTAL_MS"})
    base = q.merge(tot, on=["HOUR_TS", "WAREHOUSE_NAME"]) \
            .merge(metering, on=["HOUR_TS", "WAREHOUSE_NAME"])
    base["ALLOC_CREDITS"] = base["HOUR_CREDITS"] * base["EXEC_MS"] / base["TOTAL_MS"]
    base["DAY"] = base["HOUR_TS"].dt.date
    metering["DAY"] = metering["HOUR_TS"].dt.date
    return base, metering


def test_alloc_xdim_day_sums_reconcile_and_never_exceed_metering():
    base, metering = _synthetic_alloc_base()
    xdim = base.groupby(["DAY", "WAREHOUSE_NAME", "DATABASE_NAME", "USER_NAME"])["ALLOC_CREDITS"].sum()
    single_user = base.groupby(["DAY", "USER_NAME"])["ALLOC_CREDITS"].sum()
    single_db = base.groupby(["DAY", "DATABASE_NAME"])["ALLOC_CREDITS"].sum()
    xd = xdim.groupby("DAY").sum()
    for other in (single_user.groupby("DAY").sum(), single_db.groupby("DAY").sum()):
        pd.testing.assert_series_equal(xd, other, check_names=False, atol=1e-9, rtol=0)
    metered = metering.groupby("DAY")["HOUR_CREDITS"].sum()
    joined = pd.concat([xd.rename("ALLOC"), metered.rename("METERED")], axis=1).fillna(0)
    assert (joined["ALLOC"] <= joined["METERED"] + 1e-9).all()
    # the empty warehouse-hour left credits unallocated somewhere
    assert (joined["ALLOC"] < joined["METERED"] - 1e-9).any()


def test_xdim_arm_reuses_the_single_dim_expressions():
    p = _proc(_V41, "SP_LOAD_MARTS_V27")
    xdim = p.split("-- [5b] cross-dim allocation fact", 1)[1].split("-- [6] task graphs", 1)[0]
    assert "FROM _OW_ALLOC_BASE" in xdim
    assert "ROUND(SUM(ALLOC_CREDITS), 6)" in xdim          # same rounding as [5]
    assert "ROUND(SUM(EXEC_MS) / 1000, 1)" in xdim         # same seconds law as [5]
    assert "SCHEMA_NAME" not in xdim                       # NO schema grain, by design
    assert "GROUP BY 1, 2, 3, 4" in xdim


# ---------------------------------------------------------------------------
# New readers: contracts, coverage gates, and the old time column ABSENT
# ---------------------------------------------------------------------------

def test_alloc_xdim_reader_contract_and_share_law():
    sql = mart27_sql.alloc_xdim_attribution(7, "USER", "ALFA", "CLAIMS_DB")
    for col in ("DIMENSION", "ELAPSED_SEC", "ELAPSED_SHARE", "ALLOC_CREDITS"):
        assert col in sql, col
    assert "START_TIME" not in sql                          # old time column absent
    assert "FIRST_DAY FROM cov" in sql                      # young fact -> live fallback
    # global-share law: the denominator subquery reads the UNFILTERED scope
    assert "NULLIF((SELECT SUM(ALLOC_CREDITS) FROM scoped), 0)" in sql
    assert "CLAIMS_DB" in sql
    import pytest
    with pytest.raises(ValueError):
        mart27_sql.alloc_xdim_attribution(7, "SCHEMA", "ALFA")   # no schema grain
    assert "''" in mart27_sql.alloc_xdim_attribution(7, "USER", "ALFA", "x'y")


def test_ai_rollup_reader_contract_probe_kept_and_time_column_absent():
    sql = mart27_sql.ai_code_user_rollup(7, "ALFA")
    for col in ("USER_NAME", "EMAIL", "SOURCE", "ACTIVE_DAYS", "TOTAL_REQUESTS",
                "TOTAL_CREDITS", "TOTAL_TOKENS", "FIRST_USAGE", "LAST_USAGE",
                "CREDITS_PER_REQUEST", "AVG_DAILY_CREDITS"):
        assert col in sql, col
    assert "USAGE_TIME" not in sql                          # old time column absent
    assert "SOURCE IN ('Snowsight', 'CLI')" in sql          # Functions rows excluded
    cb = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "ai_chargeback.py").read_text(encoding="utf-8")
    body = cb.split("def _ai_users_tab", 1)[1].split("\ndef ", 1)[0]
    assert body.index("mart27_sql.ai_code_user_rollup") < body.index("cortex_sql.cortex_code_user_rollup")
    assert "probe=True" in body                             # 002139 semantics survive
    assert 'error_kind == "unknown_function"' in body


def test_ops_diag_readers_and_first_paint_gate():
    top = mart27_sql.ops_diag_top_queries(7, "ALFA", 50)
    for col in ("QUERY_ID", "START_TIME", "USER_NAME", "WAREHOUSE_NAME", "WAREHOUSE_SIZE",
                "ELAPSED_SEC", "QUEUED_SEC", "SPILL_REMOTE_GB", "QUERY_PREVIEW"):
        assert col in top, col
    assert "TOTAL_ELAPSED_TIME" not in top                  # old time column absent
    assert "FIRST_TS FROM cov" in top                       # accruing-mart gate
    fails = mart27_sql.ops_diag_failures(7, "ALFA")
    for col in ("ERROR_CODE", "ERROR_MESSAGE", "FAILURES", "USERS_AFFECTED", "LAST_SEEN"):
        assert col in fails, col
    assert "MAX(d.USERS_AFFECTED)" in fails                 # peak hourly, labeled
    ops = (_ROOT / "app" / "ui" / "pages" / "operations.py").read_text(encoding="utf-8")
    body = ops.split("def _queries_tab", 1)[1].split("\ndef ", 1)[0]
    assert "_use_diag = not (wh_filter or user_filter or database or schema_contains)" in body
    assert "mart27_sql.ops_diag_top_queries" in body
    assert "mart27_sql.ops_diag_failures" in body
    assert "run_batch" in body                              # filtered path unchanged


def test_platform_score_reader_and_overview_swap():
    sql = mart27_sql.platform_score_inputs(30)
    for col in ("DAY", "CREDITS_BILLED", "QUERY_COUNT", "FAILED_COUNT", "QUEUED_SEC",
                "SPILL_GB", "TASK_RUNS", "TASK_FAILED", "CRIT_RAISED", "HIGH_RAISED"):
        assert col in sql, col
    assert "RAISED_AT" not in sql and "HOUR_TS" not in sql  # old source columns absent
    ov = (_ROOT / "app" / "ui" / "pages" / "overview.py").read_text(encoding="utf-8")
    assert "mart27_sql.platform_score_inputs(30), mart_sql.score_inputs_daily(30)" in ov
    assert "FACT_PLATFORM_SCORE_DAILY (daily snapshot)" in ov


def test_spend_attribution_swap_and_security_monitor_swap():
    sp = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "spend.py").read_text(encoding="utf-8")
    body = sp.split("def _attribution_tab", 1)[1].split("\ndef ", 1)[0]
    assert "elif database:" in body
    assert "mart27_sql.alloc_xdim_attribution" in body
    assert body.index("if schema_contains:") < body.index("elif database:")
    sec = (_ROOT / "app" / "ui" / "pages" / "security.py").read_text(encoding="utf-8")
    gov = sec.split("def _governance_score_panel", 1)[1].split("\ndef ", 1)[0]
    assert '"warehouses_no_monitor" not in inputs' in gov   # SHOW only as fallback
    assert 'snap.get("WH_NO_MONITOR")' in gov
    assert "whs is not None and whs.ok" in gov


def test_canaries_and_expected_gaps():
    canary = (_ROOT / "app" / "data" / "canary.py").read_text(encoding="utf-8")
    for name in ("mart27_sql.alloc_xdim_attribution", "mart27_sql.ai_code_user_rollup",
                 "mart27_sql.ops_diag_top_queries", "mart27_sql.ops_diag_failures",
                 "mart27_sql.platform_score_inputs"):
        assert name in canary, name
    from app.data.canary import EXPECTED_GAPS
    assert not any("v041" in g or "xdim" in g or "ops_diag" in g or "platform_score" in g
                   for g in EXPECTED_GAPS)                  # EXPECTED_GAPS untouched


def test_freshness_view_gains_the_four_new_arms():
    view = _V41.split("CREATE OR REPLACE VIEW DBA_MAINT_DB.OVERWATCH.MART_SOURCE_FRESHNESS AS", 1)[1]
    view = view.split("-- ------", 1)[0]
    assert view.count("UNION ALL") == 22                    # 23 arms
    for src in ("'OW_QH_EXTRACT'", "'FACT_COST_ALLOC_XDIM_DAILY'",
                "'MART_OPS_DIAG_HOURLY'", "'FACT_PLATFORM_SCORE_DAILY'"):
        assert src in view, src


def test_nightly_reconcile_rebuilds_the_merge_loaded_windows():
    rec = _proc(_V41, "SP_NIGHTLY_RECONCILE")
    for tbl in ("FACT_WAREHOUSE_DAILY", "FACT_METERING_DAILY",
                "MART_WAREHOUSE_EFFICIENCY_DAILY", "MART_QUERY_FAMILY_DAILY",
                "FACT_QUERY_ROLE_HOURLY", "FACT_QUERY_SCHEMA_HOURLY",
                "MART_TAG_COVERAGE_DAILY", "MART_COST_ALLOCATION_DAILY",
                "FACT_COST_ALLOC_XDIM_DAILY", "MART_TASK_GRAPH_DAILY"):
        assert f"DELETE FROM DBA_MAINT_DB.OVERWATCH.{tbl}" in rec, tbl
    # FACT_QUERY_HOURLY self-heals (48h DELETE+INSERT) — deliberately absent
    assert "DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY" not in rec
    assert "SET WM_TS = DATEADD('day', -3, CURRENT_TIMESTAMP())" in rec
    calls = ["SP_LOAD_QH_EXTRACT(NULL)", "SP_LOAD_HOURLY_FACTS()",
             "SP_LOAD_DAILY_FACTS()", "SP_LOAD_MARTS_V27('HOURLY', 3)", "SP_LOAD_OPS_DIAG(3)"]
    pos = [rec.index(c) for c in calls]
    assert pos == sorted(pos)
