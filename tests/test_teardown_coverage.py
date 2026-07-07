"""Teardown/migration sync guard.

Every object the migrations (and native templates) create must be handled by
snowflake/teardown.sql — dropped in Section A, or listed in the commented
Section B/C blocks. A migration that adds an object without updating the
teardown fails here, not during a 2 a.m. drop-and-restore.
"""

import re
from pathlib import Path

SNOWFLAKE_DIR = Path(__file__).resolve().parents[1] / "snowflake"

_CREATE_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:TRANSIENT\s+)?"
    r"(TABLE|VIEW|TASK|PROCEDURE|FUNCTION|WAREHOUSE|RESOURCE\s+MONITOR|ALERT)"
    r"\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Z0-9_.]+)",
    re.IGNORECASE,
)

# Created by migrations but intentionally NOT in teardown.sql (none today).
_EXEMPT: set[str] = set()


def _created_objects() -> set[str]:
    created: set[str] = set()
    sources = sorted((SNOWFLAKE_DIR / "migrations").glob("V0*.sql"))
    sources.append(SNOWFLAKE_DIR / "native_alert_templates.sql")
    for path in sources:
        text = path.read_text(encoding="utf-8")
        # strip comment lines so commented examples don't count as created
        live = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))
        for _kind, name in _CREATE_RE.findall(live):
            name = name.upper().rstrip(";")
            if name.startswith("DBA_MAINT_DB.") or "." not in name:
                created.add(name.split("(")[0])
    return created - _EXEMPT


def test_every_created_object_is_covered_by_teardown():
    teardown = (SNOWFLAKE_DIR / "teardown.sql").read_text(encoding="utf-8").upper()
    missing = sorted(
        name for name in _created_objects()
        if name not in teardown
    )
    assert not missing, f"teardown.sql does not mention: {missing}"


def test_teardown_never_drops_the_shared_schema():
    teardown = (SNOWFLAKE_DIR / "teardown.sql").read_text(encoding="utf-8").upper()
    # The schema is shared with the old app: dropping it must be impossible
    # to do by running this file, even with every comment removed.
    assert "DROP SCHEMA" not in teardown
    assert "DROP DATABASE" not in teardown


def test_destructive_sections_are_commented_out():
    text = (SNOWFLAKE_DIR / "teardown.sql").read_text(encoding="utf-8")
    live = [line for line in text.splitlines() if not line.lstrip().startswith("--")]
    live_sql = "\n".join(live).upper()
    # Operator-data tables and shared infra must not have LIVE drop statements.
    for protected in ("SETTINGS", "SAVINGS_LEDGER", "ACTION_QUEUE", "ALERT_AUDIT",
                      "COMPANY_SCOPE", "SCHEMA_VERSION"):
        assert f"DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.{protected}" not in live_sql, protected
    assert "DROP WAREHOUSE" not in live_sql
    assert "DROP ROLE" not in live_sql
    assert "DROP STREAMLIT" not in live_sql
