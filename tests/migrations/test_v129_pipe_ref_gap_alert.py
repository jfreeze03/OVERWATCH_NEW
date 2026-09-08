"""Locks for V129 — the PIPE_REF_GAP daily alert (ETL Phase 1b).

The reference-data gap that the v4.499.0 panel surfaces live now pages/emails daily. The
config-driven cross-DB MINUS scan is isolated in SP_SCAN_REF_GAPS() (writes ETL_REF_GAP_RESULTS);
SP_ALERT_SCAN_DAILY gains a 7th arm that CALLs it and raises one alert per code type, inside the
same per-arm EXCEPTION guard as every other rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MIG = (_ROOT / "snowflake" / "migrations" / "V129__pipe_ref_gap_alert.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_v129_guarded_and_ordered():
    assert "EXCEPTION (-20129" in _MIG and "IF (v < 128) THEN" in _MIG
    assert "SELECT 129 AS VERSION" in _MIG and "WHERE VERSION = 129)" in _MIG


def test_v129_seeds_pipe_ref_gap_rule():
    assert "('PIPE_REF_GAP', 'PIPELINE'" in _MIG
    assert "WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME" in _MIG
    assert "WHEN MATCHED THEN UPDATE" not in _MIG   # never clobber an operator's edited rule


def test_v129_creates_isolated_scan_proc_and_results_table():
    assert "CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.ETL_REF_GAP_RESULTS" in _MIG
    assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_SCAN_REF_GAPS" in _MIG
    # the scan reads the same SETTINGS config as the panel
    assert "ETL_REF_GAP_XLAT" in _MIG and "ETL_REF_GAP_CHECKS" in _MIG
    # identifiers AND the family name are allowlist-validated before reaching the built SQL
    assert _MIG.count("RLIKE(") >= 4   # xlat + name + fqn + col
    assert "RLIKE(nm_clean" in _MIG    # the family name is allowlisted too (backslash/quote-safe)
    # no backslash escapes leaked into the generated SQL (CHR(10)/[.]/[*]/positive-allowlist regex)
    assert "\\" not in _MIG


def test_v129_arm_is_exception_isolated_and_calls_the_scan():
    assert "-- [17] PIPE_REF_GAP" in _MIG
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_REF_GAPS()" in _MIG
    # the new arm carries the standard per-arm guard
    assert "rule PIPE_REF_GAP - optional external add-on" in _MIG
    # ref-gap is an external-dependency add-on: it does NOT count toward OPS_SCAN_DEGRADED, so the
    # core scan-health tally stays /6 and only the 6 core arms bump :fails (a grant gap logs to
    # APP_ERROR_LOG without tripping the self-alert).
    assert "/6 rule blocks ok (daily)" in _MIG and "/7 rule blocks" not in _MIG
    assert _MIG.count("fails := fails + 1") == 6
    # exactly one re-derivation of the daily scan proc + the new scan proc = 2 procs
    assert _MIG.count("CREATE OR REPLACE PROCEDURE") == 2
    # nothing dropped from the re-derived proc
    for arm in ("PIPE_TASK_FAILURES", "SEC_FAILED_LOGINS", "COST_BUDGET_PACE",
                "COST_FORECAST_BREACH", "COST_AI_CREEP", "COST_CONTRACT_BREACH"):
        assert arm in _MIG


def test_v129_is_proc_and_seed_only_no_destructive_schema():
    assert "CREATE OR REPLACE VIEW" not in _MIG
    assert "ALTER TABLE " not in _MIG and "CREATE TASK" not in _MIG
    assert "DROP TABLE" not in _MIG and "DROP PROCEDURE" not in _MIG


def test_validate_and_docs_track_v129():
    val = _read("snowflake/validate.sql")
    assert "V001..V133 applied" in val and "VERSION BETWEEN 1 AND 133) = 133" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V129__pipe_ref_gap_alert.sql" in _read(rel)


def test_v129_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 129 in _EXPECTED_MIGRATIONS


def test_v129_plain_sql_parses():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_MIG):
        sqlglot.parse(statement, dialect="snowflake")
