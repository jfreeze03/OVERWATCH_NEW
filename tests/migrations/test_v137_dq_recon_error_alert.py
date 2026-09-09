"""Locks for V137 — the DQ_RECON_ERROR daily alert (ETL Phase 3, DB-side twin).

A fresh reconciliation break (source vs target layer mismatch) now pages/emails via the daily
alert scan, the same way PIPE_REF_GAP does. V137 adds ETL_RECON_RESULTS + SP_SCAN_RECON_ERRORS()
(isolated config-driven read of the RECON_MTRC_ERROR table, FQN-allowlisted) + a DQ_RECON_ERROR
rule, and re-derives SP_ALERT_SCAN_DAILY from V129 with an 8th arm that CALLs the scan and raises
one summary alert. The arm does NOT bump the scan-health fails counter (external-dependency add-on).
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MIG = (_ROOT / "snowflake" / "migrations" / "V137__dq_recon_error_alert.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_v137_guarded_and_ordered():
    assert "EXCEPTION (-20137" in _MIG and "IF (v < 136) THEN" in _MIG
    assert "SELECT 137 AS VERSION" in _MIG and "WHERE VERSION = 137)" in _MIG


def test_v137_creates_results_table_and_scan_proc():
    assert "CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.ETL_RECON_RESULTS" in _MIG
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_SCAN_RECON_ERRORS" in _MIG
    # the scan reads the CONFIGURED table and allowlist-validates the FQN (no injection)
    assert "'ETL_RECON_ERROR_FQN'" in _MIG
    assert "RLIKE(TRIM(:recon_fqn)" in _MIG
    # the scan gate: only scan when the rule is enabled; a fixed 2-day recent window
    assert "WHERE RULE_ID = 'DQ_RECON_ERROR' AND ENABLED" in _MIG
    assert "DATEADD(''day'', -2, CURRENT_TIMESTAMP())" in _MIG
    # no backslash escapes in the generated SQL
    assert "\\" not in _MIG


def test_v137_seeds_dq_recon_error_rule_high():
    assert "('DQ_RECON_ERROR', 'PIPELINE'" in _MIG
    assert "'HIGH'" in _MIG  # HIGH so it routes to the email path (pages overnight)
    assert "WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME" in _MIG
    assert "WHEN MATCHED THEN UPDATE" not in _MIG   # never clobber an operator's edited rule


def test_v137_arm_isolation_and_no_fails_bump():
    # re-derived from V129: the new scan proc + the alert-scan proc = 2 procs
    assert _MIG.count("CREATE OR REPLACE PROCEDURE") == 2
    # the new arm CALLs the isolated scan, and the V129 ref-gap arm is preserved
    assert "-- [18] DQ_RECON_ERROR" in _MIG
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_RECON_ERRORS()" in _MIG
    assert "-- [17] PIPE_REF_GAP" in _MIG
    # the recon arm must NOT bump the core scan-health tally (like the ref-gap arm): 6 core arms only
    assert _MIG.count("fails := fails + 1") == 6
    assert "/6 rule blocks ok (daily)" in _MIG
    # it logs failures WITHOUT tripping OPS_SCAN_DEGRADED
    assert "'recon_scan_failed'" in _MIG


def test_validate_teardown_and_docs_track_v137():
    val = _read("snowflake/validate.sql")
    assert "V001..V137 applied" in val and "VERSION BETWEEN 1 AND 137) = 137" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V137__dq_recon_error_alert.sql" in _read(rel)
    teardown = _read("snowflake/teardown.sql")
    assert "SP_SCAN_RECON_ERRORS" in teardown and "ETL_RECON_RESULTS" in teardown


def test_v137_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 137 in _EXPECTED_MIGRATIONS


def test_v137_plain_sql_parses():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_MIG):
        sqlglot.parse(statement, dialect="snowflake")
