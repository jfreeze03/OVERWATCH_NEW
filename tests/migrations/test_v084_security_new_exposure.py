"""V084 locks: SEC_NEW_EXPOSURE proactive alert (CoCo Sec36).

A privilege granted to PUBLIC is inherited by every role in the account, so a new
grant to PUBLIC is a real widening of the blast radius. V084 seeds a SECURITY rule
and adds arm [20] to SP_ALERT_SCAN (re-derived from V079) that raises it when a new
grant to PUBLIC landed in the last 24h. Derivation law: the ONLY edits vs V079 are
the new arm and the rule-block count 14 -> 15; reversing both reproduces the V079
body byte-for-byte.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[2]
_MIG = _ROOT / "snowflake" / "migrations"
_V084 = (_MIG / "V084__security_new_exposure_alert.sql").read_text(encoding="utf-8")


def _proc(text: str, name: str) -> str:
    return re.search(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n",
        text, re.S,
    ).group(0)


def test_v084_regenerates_byte_identical(tmp_path):
    output = tmp_path / "regen.sql"
    result = subprocess.run(
        [sys.executable, str(_ROOT / "outputs" / "gen_v084.py")],
        env={**os.environ, "V084_OUT": str(output)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == _V084


def test_v084_guard_version_house_rules():
    assert "EXCEPTION (-20084" in _V084 and "RAISE not_ready;" in _V084
    assert "RAISE EXCEPTION (" not in _V084
    assert "IF (v < 83) THEN" in _V084
    assert "SELECT 84 AS VERSION" in _V084 and "WHERE VERSION = 84)" in _V084
    # One re-derived proc; no new objects; never fire the scanner at apply time.
    assert _V084.count("CREATE OR REPLACE PROCEDURE") == 1
    for forbidden in ("CREATE TABLE", "CREATE TASK", "CREATE WAREHOUSE", "RESOURCE MONITOR"):
        assert forbidden not in _V084, forbidden
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN(" not in _V084


def test_v084_seeds_the_security_rule():
    assert "MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG" in _V084
    assert "WHEN NOT MATCHED THEN INSERT" in _V084          # idempotent; never clobbers edits
    assert "'SEC_NEW_EXPOSURE' AS RULE_ID" in _V084
    assert "'SECURITY' AS FAMILY" in _V084
    assert "TRUE AS ENABLED, 'HIGH' AS SEVERITY, 1 AS THRESHOLD_NUM, 24 AS WINDOW_HOURS" in _V084


def test_v084_adds_arm_20_reading_public_grants():
    scan = _proc(_V084, "SP_ALERT_SCAN")
    assert scan.count("-- [20] SEC_NEW_EXPOSURE") == 1
    assert "ON c.RULE_ID = 'SEC_NEW_EXPOSURE'" in scan
    assert "SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES" in scan
    assert "GRANTEE_NAME = 'PUBLIC'" in scan
    assert "DELETED_ON IS NULL" in scan                     # only standing (un-revoked) exposure
    assert "DATEADD('hour', -24, CURRENT_TIMESTAMP())" in scan
    assert "p.N_OBJECTS >= c.THRESHOLD_NUM" in scan         # honors the configured threshold
    # Discrete per-grant signal (like arm [18]), NOT the cumulative daily grain of [19]:
    # deduped on grant identity so distinct same-day grants each page and an
    # already-alerted grant never re-pages as it ages through the rolling window.
    assert "c.RULE_ID || '|' || p.PRIVILEGE || '|' || p.GRANTED_ON || '|' || TO_VARCHAR(p.CREATED_ON)" in scan
    # Scope the "not the daily grain" check to arm [20] itself — other arms (e.g. the
    # cumulative egress arm [19]) legitimately key on CURRENT_DATE.
    arm20 = scan.split("-- [20] SEC_NEW_EXPOSURE", 1)[1].split("    IF (fails > 0) THEN", 1)[0]
    assert "TO_VARCHAR(CURRENT_DATE())" not in arm20         # not the daily-date grain
    # A batch GRANT ON ALL collapses to one event counting its objects (no flood).
    assert "GROUP BY PRIVILEGE, GRANTED_ON, CREATED_ON" in arm20
    assert "COUNT(*) AS N_OBJECTS" in arm20
    assert "WHERE e.DEDUPE_KEY = b.DEDUPE_KEY" in arm20
    # its own isolated failure handling, so a broken arm never kills the whole scan
    assert "'rule SEC_NEW_EXPOSURE - other rules unaffected'" in scan
    # the self-alert bookkeeping count moved 14 -> 15
    assert "' of 15 alert rule block(s) failed this run'" in scan
    assert "(15 - :fails) || '/15 rule blocks ok'" in scan
    assert "of 14 alert" not in scan and "/14 rule blocks" not in scan


def test_v084_only_edits_are_the_arm_and_the_count():
    # Derivation law: reverse the count bump and delete arm [20] -> the SP_ALERT_SCAN
    # body reproduces V079 byte-for-byte. Nothing else changed.
    v079 = (_MIG / "V079__ai_predicate_coco_historical_split.sql").read_text(encoding="utf-8")
    base_scan = _proc(v079, "SP_ALERT_SCAN")
    reverted = (
        _proc(_V084, "SP_ALERT_SCAN")
        .replace("' of 15 alert rule block(s) failed this run'",
                 "' of 14 alert rule block(s) failed this run'")
        .replace("(15 - :fails) || '/15 rule blocks ok'",
                 "(14 - :fails) || '/14 rule blocks ok'")
    )
    reverted = re.sub(
        r"    -- \[20\] SEC_NEW_EXPOSURE.*?(?=    IF \(fails > 0\) THEN)",
        "", reverted, flags=re.S,
    )
    assert reverted == base_scan, "SP_ALERT_SCAN diverged from V079 beyond the arm + count edit"


def test_v084_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V084):
        sqlglot.parse(statement, dialect="snowflake")


def test_v084_in_migration_registry():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 84 in _EXPECTED_MIGRATIONS


def test_v084_validate_floor_and_deploy_docs_track_the_migration():
    validate = (_ROOT / "snowflake" / "validate.sql").read_text(encoding="utf-8")
    assert "V001..V115 applied" in validate
    assert "BETWEEN 1 AND 115) = 115" in validate
    assert "BETWEEN 1 AND 88;" in validate and "n_versions < 88" in validate
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V084__security_new_exposure_alert.sql" in (_ROOT / rel).read_text(encoding="utf-8")
