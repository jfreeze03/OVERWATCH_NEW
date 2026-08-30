"""V094 locks: FACT_QUERY_HOURLY boundary-hour duplicate fix.

SP_LOAD_QH_EXTRACT's FACT_QUERY_HOURLY refresh DELETEd on hour-truncated HOUR_TS but
against a NON-truncated -48h instant, while the INSERT truncated START_TIME to the
hour — so the boundary hour was never deleted yet re-inserted partial, leaving a
permanent duplicate row per grain that ~2x'd multi-day query facts. V094 re-derives the
proc so both bounds are hour-truncated (like the sibling SP_LOAD_OPS_DIAG) and dedups the
rows the bug left. Byte-locked to outputs/gen_v094.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[2]
_MIG = _ROOT / "snowflake" / "migrations"
_V094 = (_MIG / "V094__fact_query_hourly_boundary_dedupe.sql").read_text(encoding="utf-8")
_V062 = (_MIG / "V062__loader_robustness_alert_split_webhook.sql").read_text(encoding="utf-8")


def test_v094_regenerates_byte_identical(tmp_path):
    output = tmp_path / "regen.sql"
    result = subprocess.run(
        [sys.executable, str(_ROOT / "outputs" / "gen_v094.py")],
        env={**os.environ, "V094_OUT": str(output)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == _V094, (
        "V094 drifted from its forward-generation — edit outputs/gen_v094.py, "
        "not the .sql, then regenerate."
    )


def test_v094_is_one_guarded_proc_redefinition_plus_dedup():
    assert "EXCEPTION (-20094" in _V094 and "IF (v < 93) THEN" in _V094
    assert "SELECT 94 AS VERSION" in _V094 and "WHERE VERSION = 94)" in _V094
    assert _V094.count("CREATE OR REPLACE PROCEDURE") == 1
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT" in _V094
    # procedure-only + a one-time data cleanup — no schema/view/task change
    assert "CREATE TABLE" not in _V094 and "ALTER TABLE" not in _V094
    assert "CREATE OR REPLACE VIEW" not in _V094 and "CREATE TASK" not in _V094


def test_v094_both_boundary_bounds_are_hour_truncated():
    # the DELETE and the INSERT bound are BOTH hour-truncated (the fix), matching the
    # sibling SP_LOAD_OPS_DIAG; the watermark first-run fallback stays a raw -48h instant.
    assert _V094.count("DATE_TRUNC('hour', DATEADD('hour', -48, CURRENT_TIMESTAMP()))") == 2
    assert "DATEADD('hour', -48, CURRENT_TIMESTAMP())::TIMESTAMP_NTZ" in _V094   # fallback untouched
    # the old raw-bounded DELETE (the bug) is gone
    assert "WHERE HOUR_TS >= DATEADD('hour', -48, CURRENT_TIMESTAMP());" not in _V094
    # V062 (the base) still carries the bug, confirming V094 supersedes it
    assert "WHERE HOUR_TS >= DATEADD('hour', -48, CURRENT_TIMESTAMP());" in _V062


def test_v094_dedups_existing_duplicates_keeping_the_complete_hour():
    assert "INSERT OVERWRITE INTO DBA_MAINT_DB.OVERWATCH.FACT_QUERY_HOURLY" in _V094
    assert ("PARTITION BY HOUR_TS, WAREHOUSE_NAME, DATABASE_NAME, USER_NAME, COMPANY"
            in _V094)
    assert "ORDER BY QUERY_COUNT DESC, LOAD_TS DESC) = 1" in _V094   # keep the complete hour


def test_v094_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V094):
        sqlglot.parse(statement, dialect="snowflake")
