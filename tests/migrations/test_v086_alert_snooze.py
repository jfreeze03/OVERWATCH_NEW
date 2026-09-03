"""V086 locks: per-event alert snooze (CoCo Alerts29).

Silence one alert until a wake time without acking/resolving. A snoozed event moves
to STATUS='SNOOZED' (off the OPEN/ACK feed with NO read-path change) and the hourly
SP_ALERT_SCAN (re-derived from V084) returns expired snoozes to OPEN. Derivation law:
the ONLY SP_ALERT_SCAN edit vs V084 is the wake block; removing it reproduces V084.
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
_V086 = (_MIG / "V086__alert_snooze.sql").read_text(encoding="utf-8")


def _proc(text: str, name: str) -> str:
    return re.search(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n",
        text, re.S,
    ).group(0)


def test_v086_regenerates_byte_identical(tmp_path):
    output = tmp_path / "regen.sql"
    result = subprocess.run(
        [sys.executable, str(_ROOT / "outputs" / "gen_v086.py")],
        env={**os.environ, "V086_OUT": str(output)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == _V086


def test_v086_guard_version_house_rules():
    assert "EXCEPTION (-20086" in _V086 and "RAISE not_ready;" in _V086
    assert "RAISE EXCEPTION (" not in _V086
    assert "IF (v < 85) THEN" in _V086
    assert "SELECT 86 AS VERSION" in _V086 and "WHERE VERSION = 86)" in _V086
    # Two procs (SP_ALERT_SNOOZE + re-derived SP_ALERT_SCAN); no tables/tasks; never
    # fire the scanner at apply.
    assert _V086.count("CREATE OR REPLACE PROCEDURE") == 2
    for forbidden in ("CREATE TABLE", "CREATE TASK", "CREATE WAREHOUSE", "RESOURCE MONITOR"):
        assert forbidden not in _V086, forbidden
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN(" not in _V086


def test_v086_adds_the_snooze_columns_and_proc():
    for col in ("SNOOZED_UNTIL TIMESTAMP_NTZ", "SNOOZE_BY VARCHAR(200)",
                "SNOOZE_REASON VARCHAR(1000)"):
        assert f"ADD COLUMN IF NOT EXISTS {col}" in _V086, col
    snooze = _proc(_V086, "SP_ALERT_SNOOZE")
    # Server-computed wake from a duration (no app clock); guarded range, 0 = un-snooze.
    assert "P_SNOOZE_HOURS FLOAT" in snooze
    assert "DATEADD('minute', ROUND(:v_hours * 60), CURRENT_TIMESTAMP())" in snooze
    assert "BLOCKED: snooze hours must be in [0, 8760] (0 = un-snooze now)" in snooze
    # Idempotent + audited + atomic, like SP_ALERT_LIFECYCLE.
    assert "OW_ACTION_INTENTS WHERE IDEM_KEY = :P_IDEM_KEY" in snooze
    assert "INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT" in snooze and "'SNOOZE'" in snooze
    assert "BEGIN TRANSACTION;" in snooze and "COMMIT;" in snooze and "ROLLBACK;" in snooze
    # Only OPEN/ACK events can be snoozed; sets STATUS='SNOOZED' + the wake time.
    assert "SET STATUS = 'SNOOZED', SNOOZED_UNTIL = :v_until" in snooze
    assert "STATUS IN ('OPEN', 'ACK')" in snooze
    # hours=0 branch = early un-snooze: restore the true prior status (same as the wake).
    assert "IF (:v_hours = 0) THEN" in snooze
    assert "'UNSNOOZE'" in snooze and "'ALERT_UNSNOOZE'" in snooze
    assert snooze.count("SET STATUS = IFF(ACK_AT IS NOT NULL, 'ACK', 'OPEN')") == 1


def test_v086_scan_wakes_expired_snoozes_and_is_otherwise_v084():
    scan = _proc(_V086, "SP_ALERT_SCAN")
    # The wake step runs before the rule arms and returns expired snoozes to their
    # true prior status (ACK if acked, else OPEN) — never stranding a stale ACK_AT.
    assert "-- [wake] V086" in scan
    assert "SET STATUS = IFF(ACK_AT IS NOT NULL, 'ACK', 'OPEN')," in scan
    assert "SNOOZED_UNTIL = NULL, SNOOZE_BY = NULL, SNOOZE_REASON = NULL" in scan
    assert "WHERE STATUS = 'SNOOZED'" in scan
    assert "SNOOZED_UNTIL <= CURRENT_TIMESTAMP()" in scan
    assert scan.index("-- [wake] V086") < scan.index("-- [01] COST_DAILY_CREDITS")
    # It does NOT touch the rule-block count (it is not a rule).
    assert "/15 rule blocks ok" in scan and "of 15 alert rule block(s)" in scan
    # Derivation law: remove the wake block -> reproduces V084's SP_ALERT_SCAN.
    v084 = (_MIG / "V084__security_new_exposure_alert.sql").read_text(encoding="utf-8")
    base_scan = _proc(v084, "SP_ALERT_SCAN")
    reverted = re.sub(
        r"    -- \[wake\] V086.*?(?=    -- \[01\] COST_DAILY_CREDITS)",
        "", scan, flags=re.S,
    )
    assert reverted == base_scan, "SP_ALERT_SCAN diverged from V084 beyond the wake block"


def test_v086_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_V086):
        sqlglot.parse(statement, dialect="snowflake")


def test_v086_in_migration_registry():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 86 in _EXPECTED_MIGRATIONS


def test_v086_teardown_drops_the_new_proc():
    teardown = (_ROOT / "snowflake" / "teardown.sql").read_text(encoding="utf-8")
    assert "DROP PROCEDURE IF EXISTS DBA_MAINT_DB.OVERWATCH.SP_ALERT_SNOOZE(" in teardown


def test_v086_app_wires_the_snooze_action():
    alerts = (_ROOT / "app" / "ui" / "pages" / "alerts.py").read_text(encoding="utf-8")
    assert 'st.radio("Action", ["ACK", "RESOLVE", "SNOOZE"]' in alerts
    assert "SNOOZE_PRESETS" in alerts and "def _snooze_stmts(" in alerts
    assert "CALL {core_object('SP_ALERT_SNOOZE')}" in alerts
    assert "alert_snooze" in alerts   # UI telemetry event
    # Snoozed view + early un-snooze (hours=0), so a snooze is never a black hole.
    assert "mart_sql.snoozed_alert_events(" in alerts
    assert "def _unsnooze_stmts(" in alerts
    assert "alert_unsnooze" in alerts


def test_v086_snoozed_reader_is_scoped_and_status_filtered():
    from app.data import mart_sql
    sql = mart_sql.snoozed_alert_events(100, "ALFA")
    sqlglot.parse(sql, dialect="snowflake")
    assert "STATUS = 'SNOOZED'" in sql
    assert "SNOOZED_UNTIL" in sql and "SNOOZE_BY" in sql
    assert "(COMPANY = 'ALFA'" in sql                      # company-scoped incl. account-level
    assert "(COMPANY" not in mart_sql.snoozed_alert_events(1, "ALL")   # account-wide: no filter
    assert "'x''y'" in mart_sql.snoozed_alert_events(1, "x'y")         # injection-safe


def test_v086_validate_and_docs_track_the_migration():
    validate = (_ROOT / "snowflake" / "validate.sql").read_text(encoding="utf-8")
    assert "V001..V125 applied" in validate
    assert "BETWEEN 1 AND 125) = 125" in validate
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V086__alert_snooze.sql" in (_ROOT / rel).read_text(encoding="utf-8")
