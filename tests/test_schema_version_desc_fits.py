"""Every migration's SCHEMA_VERSION description must fit the declared column.

Live finding (owner, 2026-08-01): the DESCRIPTION column was VARCHAR(200), but the migration
notes from V041 on ran 200-1100 chars, so the INSERT INTO SCHEMA_VERSION at the end of each of
those migrations FAILED at apply time with a string-too-long error — a defect only Snowflake
surfaces (sqlglot parses it fine). V001 was widened to VARCHAR(4000); this test parses that
declared width from V001 and asserts no migration's description exceeds it, so a future
verbose note can't silently reintroduce the failure.
"""

from __future__ import annotations

import re
from pathlib import Path

_MIG = Path(__file__).resolve().parents[1] / "snowflake" / "migrations"


def _declared_width() -> int:
    """The DESCRIPTION column width declared in V001's SCHEMA_VERSION CREATE TABLE."""
    v001 = (_MIG / "V001__core.sql").read_text(encoding="utf-8")
    block = v001.split("CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION", 1)[1]
    m = re.search(r"DESCRIPTION\s+VARCHAR\((\d+)\)", block)
    assert m, "could not find DESCRIPTION VARCHAR(N) in V001 SCHEMA_VERSION"
    return int(m.group(1))


def _description(sql: str) -> str | None:
    """The SCHEMA_VERSION insert's DESCRIPTION literal (SELECT ... AS DESCRIPTION or VALUES),
    with doubled '' un-escaped to real length."""
    m = re.search(r"AS VERSION,\s*'((?:[^']|'')*)'\s*AS DESCRIPTION", sql, re.S)
    if not m:
        m = re.search(r"SCHEMA_VERSION[^)]*\)\s*VALUES\s*\(\s*\d+\s*,\s*'((?:[^']|'')*)'", sql, re.S)
    return m.group(1).replace("''", "'") if m else None


def test_v001_declares_a_wide_description_column():
    # regression anchor for the 2026-08-01 fix — the column must not shrink back.
    assert _declared_width() >= 1200, "DESCRIPTION column too small for existing migration notes"


def test_every_migration_description_fits_the_column():
    width = _declared_width()
    offenders = []
    for path in sorted(_MIG.glob("V*.sql")):
        desc = _description(path.read_text(encoding="utf-8"))
        if desc is not None and len(desc) > width:
            offenders.append(f"{path.name}: {len(desc)} chars > VARCHAR({width})")
    assert not offenders, (
        "SCHEMA_VERSION description(s) exceed the declared column width — the INSERT will "
        "fail at apply with a string-too-long error:\n  " + "\n  ".join(offenders)
        + "\n\nShorten the description, or widen DESCRIPTION in V001 (VARCHAR is grow-in-place)."
    )
