"""V087 locks: security finding -> monitored rule (CoCo Sec35).

ALTER ALERT_CONFIG ADD METRIC_NAME + a generic SP_ALERT_SCAN arm [21] that raises any
enabled rule carrying a METRIC_NAME when its newest MART_SECURITY_POSTURE_DAILY reading
is at/over THRESHOLD_NUM. Operators generate such rules from a Security finding.
Derivation law: the ONLY SP_ALERT_SCAN edit vs V086 is the arm + the count 15->16.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app.logic.security import POSTURE_METRICS, posture_alert_rule_sql

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[2]
_MIG = _ROOT / "snowflake" / "migrations"
_V087 = (_MIG / "V087__security_posture_rule.sql").read_text(encoding="utf-8")


def _proc(text: str, name: str) -> str:
    return re.search(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n",
        text, re.S,
    ).group(0)


def test_v087_regenerates_byte_identical(tmp_path):
    output = tmp_path / "regen.sql"
    result = subprocess.run(
        [sys.executable, str(_ROOT / "outputs" / "gen_v087.py")],
        env={**os.environ, "V087_OUT": str(output)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == _V087


def test_v087_guard_version_and_alter():
    assert "EXCEPTION (-20087" in _V087 and "RAISE not_ready;" in _V087
    assert "IF (v < 86) THEN" in _V087
    assert "SELECT 87 AS VERSION" in _V087 and "WHERE VERSION = 87)" in _V087
    assert "ALTER TABLE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG ADD COLUMN IF NOT EXISTS METRIC_NAME VARCHAR(60)" in _V087
    assert _V087.count("CREATE OR REPLACE PROCEDURE") == 1
    for forbidden in ("CREATE TABLE", "CREATE TASK", "CREATE WAREHOUSE", "RESOURCE MONITOR"):
        assert forbidden not in _V087, forbidden
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN(" not in _V087


def test_v087_generic_posture_arm():
    scan = _proc(_V087, "SP_ALERT_SCAN")
    assert scan.count("-- [21] SEC_POSTURE_METRIC") == 1
    # Data-driven: matches ANY enabled rule with a METRIC_NAME (no hardcoded rule id).
    assert "WHERE ENABLED AND COALESCE(METRIC_NAME, '') <> ''" in scan
    assert "DBA_MAINT_DB.OVERWATCH.MART_SECURITY_POSTURE_DAILY" in scan
    assert "UPPER(m.METRIC) = UPPER(c.METRIC_NAME)" in scan
    assert "m.VALUE >= c.THRESHOLD_NUM" in scan                 # higher = worse
    # newest reading per (metric, company); stale posture excluded.
    assert "QUALIFY ROW_NUMBER() OVER (PARTITION BY METRIC, COMPANY ORDER BY DAY DESC) = 1" in scan
    assert "m.DAY >= DATEADD('day', -2, CURRENT_DATE())" in scan
    # daily dedup per rule per company (posture is a persistent condition).
    assert "c.RULE_ID || '|' || m.COMPANY || '|' || TO_VARCHAR(m.DAY)" in scan
    # count bumped 15 -> 16.
    assert "of 16 alert rule block(s)" in scan and "(16 - :fails) || '/16 rule blocks ok'" in scan
    assert "of 15 alert" not in scan and "/15 rule blocks" not in scan


def test_v087_only_edits_are_the_arm_and_the_count():
    # Reverse the count bump + delete arm [21] -> reproduces V086's SP_ALERT_SCAN.
    v086 = (_MIG / "V086__alert_snooze.sql").read_text(encoding="utf-8")
    base_scan = _proc(v086, "SP_ALERT_SCAN")
    reverted = (
        _proc(_V087, "SP_ALERT_SCAN")
        .replace("' of 16 alert rule block(s) failed this run'",
                 "' of 15 alert rule block(s) failed this run'")
        .replace("(16 - :fails) || '/16 rule blocks ok'",
                 "(15 - :fails) || '/15 rule blocks ok'")
    )
    reverted = re.sub(
        r"    -- \[21\] SEC_POSTURE_METRIC.*?(?=    IF \(fails > 0\) THEN)",
        "", reverted, flags=re.S,
    )
    assert reverted == base_scan, "SP_ALERT_SCAN diverged from V086 beyond the arm + count edit"


def test_v087_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V087):
        sqlglot.parse(statement, dialect="snowflake")


def test_v087_in_migration_registry():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 87 in _EXPECTED_MIGRATIONS


def test_posture_alert_rule_sql_generates_a_valid_upsert_rule():
    sql = posture_alert_rule_sql("MFA_GAP_USERS", 3, severity="HIGH")
    sqlglot.parse(sql, dialect="snowflake")
    assert "MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG" in sql
    # Upsert: creates OR updates the threshold/severity — re-running is never a silent
    # no-op (a WHEN-NOT-MATCHED-only MERGE would report success while changing nothing).
    assert "WHEN NOT MATCHED THEN INSERT" in sql
    assert "WHEN MATCHED THEN UPDATE SET THRESHOLD_NUM = s.THRESHOLD_NUM" in sql
    assert "'SEC_POSTURE_MFA_GAP_USERS' AS RULE_ID" in sql
    assert "'SECURITY' AS FAMILY" in sql
    assert "'MFA_GAP_USERS' AS METRIC_NAME" in sql
    assert "3.0 AS THRESHOLD_NUM" in sql
    # Rejects an unknown metric / non-positive threshold rather than a dead rule.
    with pytest.raises(ValueError):
        posture_alert_rule_sql("NOT_A_METRIC", 1)
    with pytest.raises(ValueError):
        posture_alert_rule_sql("MFA_GAP_USERS", 0)
    # Every advertised metric produces parseable SQL.
    for metric in POSTURE_METRICS:
        sqlglot.parse(posture_alert_rule_sql(metric, 1), dialect="snowflake")


def test_v087_app_wires_the_generate_ui():
    center = (_ROOT / "app" / "ui" / "security_center.py").read_text(encoding="utf-8")
    assert "posture_alert_rule_sql(" in center and "POSTURE_METRICS" in center
    assert "Monitor as a posture rule" in center


def test_v087_validate_and_docs_track_the_migration():
    validate = (_ROOT / "snowflake" / "validate.sql").read_text(encoding="utf-8")
    assert "V001..V116 applied" in validate
    assert "BETWEEN 1 AND 116) = 116" in validate
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V087__security_posture_rule.sql" in (_ROOT / rel).read_text(encoding="utf-8")
