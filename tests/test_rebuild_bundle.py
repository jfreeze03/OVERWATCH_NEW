"""The rebuild bundle can never drift from its sources (v4.36.2).

snowflake/rebuild/ is GENERATED: byte-identical copies (teardown, roles,
backfill, validate), the ordered concatenation of all 45 migrations, and a
clone-backup script covering every operator table teardown's safety model
names. Editing a source without regenerating the bundle fails here.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SF = _ROOT / "snowflake"
_RB = _SF / "rebuild"


def _body(name: str) -> str:
    """A bundle file minus its generated header comment block."""
    text = (_RB / name).read_text(encoding="utf-8")
    return text.split("\n\n", 1)[1]


def test_bundle_copies_are_byte_identical_to_their_sources():
    for bundle, src in (("01_teardown_rebuildables.sql", "teardown.sql"),
                        ("03_roles.sql", "roles.sql"),
                        ("04_backfill_365.sql", "backfill_365.sql"),
                        ("05_validate.sql", "validate.sql")):
        assert _body(bundle) == (_SF / src).read_text(encoding="utf-8"), (
            f"{bundle} drifted from snowflake/{src} — regenerate the bundle "
            "(see snowflake/rebuild/README.md), never hand-edit it.")


def test_bundle_migrations_are_the_ordered_byte_concatenation():
    migs = sorted((_SF / "migrations").glob("V0*.sql"))
    assert len(migs) == 48
    text = (_RB / "02_migrations_V001_V048.sql").read_text(encoding="utf-8")
    # every file present, in order, byte-identical between its banners
    positions = []
    for m in migs:
        banner = f"-- >>> {m.name}\n"
        assert text.count(banner) == 1, m.name
        start = text.index(banner) + len(banner)
        start = text.index("\n", start) + 1          # skip the banner underline
        body = m.read_text(encoding="utf-8").rstrip("\n")
        assert text[start:start + len(body)] == body, (
            f"{m.name} drifted inside the bundle — regenerate it.")
        positions.append(text.index(banner))
    assert positions == sorted(positions)


def test_backup_script_covers_every_operator_table_and_verifies_counts():
    bak = (_RB / "00_backup_operator_data.sql").read_text(encoding="utf-8")
    # teardown's safety model: everything Section B could destroy, plus the
    # preserved incident/registry history — all cloned before anything runs.
    for t in ("SETTINGS", "COMPANY_SCOPE", "ALERT_CONFIG", "ALERT_EVENTS",
              "ALERT_AUDIT", "ALERT_ROUTES", "ACTION_QUEUE", "SAVINGS_LEDGER",
              "APP_ERROR_LOG", "SCHEMA_VERSION", "PIPELINE_SLA_CONFIG",
              "DEPARTMENT_MAP", "OBJECT_CHANGE_REGISTRY",
              "WAREHOUSE_CHANGE_REGISTRY", "WAREHOUSE_CONFIG_SNAPSHOT",
              "USER_PREFS", "DEPT_BUDGETS", "REMEDIATION_LOG", "INCIDENTS",
              "INCIDENT_MEMBERS", "APP_USAGE"):
        assert f"CLONE DBA_MAINT_DB.OVERWATCH.{t};" in bak, t
        assert f"{t}_BAK_" in bak, t
    assert "SOURCE_ROWS" in bak and "CLONE_ROWS" in bak
    # clones are CREATEs only — the backup script must never drop anything
    assert re.search(r"^\s*DROP ", bak, re.MULTILINE | re.IGNORECASE) is None


def test_bundle_readme_matches_the_runbook_order():
    readme = (_RB / "README.md").read_text(encoding="utf-8")
    order = re.findall(r"\| (0\d) \|", readme)
    assert order == ["00", "01", "02", "03", "04", "05"]
    assert "docs/FULL_REBUILD.md" in readme
    assert "loader_chain_check.sql" in readme
