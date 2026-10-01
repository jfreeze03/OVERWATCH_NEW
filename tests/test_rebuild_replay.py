"""A full-rebuild replay runs to the end and leaves the account as it found it (round 2, R2-001/002/004).

* R2-001: V006-V008 grant to OVERWATCH_MONITOR / OVERWATCH_OPERATOR, the roles roles.sql retired on
  2026-07-13. No migration creates them, so Run All of rebuild/02 stopped at V006 on every current or
  fresh account, after V002 had attached the 30-credit SUSPEND monitor and V004 had resumed the graph.
  02's generated header now carries a replay shim (CREATE ROLE IF NOT EXISTS for exactly those roles;
  03_roles.sql drops them again), and the migration bodies stay byte-identical between their banners.
* R2-002: V002 sets WH_ALFA_ADMIN's statement timeout back to 300 s and attaches OVERWATCH_RM until
  V045. FULL_REBUILD records the live value before the teardown and puts it back after the replay, and
  says how to detach the monitor when a run stops in between.
* R2-004: the replay re-runs one-time config DML on the operator tables the teardown keeps (route
  company filters, rule flags and thresholds, re-seeded rows). FULL_REBUILD step 3b restores those
  tables from the step-1 clones; the 02 and 00 headers no longer promise the opposite.

The bundle is generated (house law 6): the headers come from outputs/gen_rebuild_bundle.py, and the
first lock below fails on any hand edit of a generated file.
"""

from __future__ import annotations

import importlib.util
import re

from tests._source import ROOT, read

_SF = ROOT / "snowflake"
_RB = _SF / "rebuild"


def _gen():
    spec = importlib.util.spec_from_file_location("gen_rebuild_bundle", ROOT / "outputs" / "gen_rebuild_bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bundle_02() -> str:
    (path,) = sorted(_RB.glob("02_migrations_V001_V*.sql"))
    return path.read_text(encoding="utf-8")


def _preamble() -> str:
    text = _bundle_02()
    return text[:text.index("-- >>> V001__core.sql")]


def _section(text: str, start: str, end: str) -> str:
    return text[text.index(start):text.index(end)]


def _uncommented(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.lstrip().startswith("--")]


def test_every_generated_bundle_file_is_what_the_generator_renders():
    gen = _gen()
    name, text = gen.render_migration_bundle()
    assert (_RB / name).read_text(encoding="utf-8") == text, f"{name}: run python outputs/gen_rebuild_bundle.py"
    for bundle, source, header in gen.COPIES:
        assert (_RB / bundle).read_text(encoding="utf-8") == gen.render_copy(bundle, source, header), bundle
    assert (_RB / "00_backup_operator_data.sql").read_text(encoding="utf-8") == gen.render_backup()
    assert (_RB / "README.md").read_text(encoding="utf-8") == gen.render_readme()


def test_replay_grants_only_to_live_or_shimmed_roles():
    text = _bundle_02()
    roles_sql = read("snowflake/roles.sql")
    live = {m.group(1) for line in _uncommented(roles_sql) for m in re.finditer(r"\bTO ROLE (\w+)", line)}
    retired = set(re.findall(r"^DROP ROLE IF EXISTS (\w+);$", roles_sql, re.M))
    assert live == {"SNOW_ACCOUNTADMINS", "SNOW_SYSADMINS"}             # owner decision 2026-07-13
    preamble = _preamble()
    shim = re.findall(r"^CREATE ROLE IF NOT EXISTS (\w+);$", preamble, re.M)
    targets = {m.group(1) for line in _uncommented(text) for m in re.finditer(r"\bTO ROLE (\w+)", line)}
    # every grant the replay runs names a live role or one the shim created first
    assert not targets - live - set(shim), sorted(targets - live - set(shim))
    # the shim re-creates exactly the retired roles the chain still grants to, and nothing else
    assert set(shim) == targets - live and shim, shim
    assert set(shim) <= retired
    # ...and 03 (byte copy of roles.sql) drops them again, after 02
    r03 = (_RB / "03_roles.sql").read_text(encoding="utf-8")
    for role in shim:
        assert f"DROP ROLE IF EXISTS {role};" in r03, role
    # the shim is the only SQL that runs before the V001 banner
    assert [line for line in _uncommented(preamble) if line.strip()] == [
        f"CREATE ROLE IF NOT EXISTS {role};" for role in shim]


def test_the_shim_lives_in_the_header_only_and_the_migrations_stay_byte_identical():
    text = _bundle_02()
    migrations = sorted((_SF / "migrations").glob("V[0-9]*.sql"))
    for path in migrations:
        assert "CREATE ROLE" not in path.read_text(encoding="utf-8").upper(), path.name
    assert text.count("CREATE ROLE IF NOT EXISTS") == len(re.findall(r"^CREATE ROLE", _preamble(), re.M))
    body = text[text.index("-- ===========================================================================\n-- >>> V001"):]
    joined = "\n".join(
        "-- ===========================================================================\n"
        f"-- >>> {p.name}\n"
        "-- ===========================================================================\n"
        + p.read_text(encoding="utf-8") for p in migrations)
    assert body == joined


def test_bundle_headers_say_what_a_replay_changes():
    preamble = _preamble()
    flat = " ".join(preamble.split())
    assert "re-running a completed file is safe" not in flat
    assert "A REPLAY IS NOT A NO-OP ON KEPT OPERATOR DATA" in flat
    assert "STATEMENT_TIMEOUT_IN_SECONDS back to 300" in flat and "OVERWATCH_RM" in flat
    assert "step 3b" in flat
    bak = (_RB / "00_backup_operator_data.sql").read_text(encoding="utf-8")
    head = " ".join(bak.split("\n\n", 1)[0].split())
    assert "never touches these tables" not in head and "pure insurance" not in head
    assert "step 3b" in head
    readme = (_RB / "README.md").read_text(encoding="utf-8")
    assert "step 3b" in readme and "STATEMENT_TIMEOUT_IN_SECONDS" in readme
    # R2-003: the paste-and-run README also says delivery must be put back after 05
    assert "**After 05** (step 7b)" in readme and "notification integrations" in readme


# Kept operator tables the replay's column-0 DML may touch WITHOUT a step-3b restore, and why.
_REPLAY_DML_NEEDS_NO_RESTORE = {
    # retire idioms (V034, V140, V157, V161) close events of retired rules / self-objects as EXPECTED;
    # re-running them closes only what no scan can raise again
    "ALERT_EVENTS",
    # V140 deletes the scan's own DBA_MAINT_DB rows, which the hardened scan never registers again
    "OBJECT_CHANGE_REGISTRY",
}


def _restored_tables(fr: str) -> set[str]:
    step3b = _section(fr, "## 3b. Put back what the replay rewrote", "## 4. Grants")
    return set(re.findall(r"INSERT OVERWRITE INTO DBA_MAINT_DB\.OVERWATCH\.(\w+)\s+SELECT \* FROM "
                          r"DBA_MAINT_DB\.OVERWATCH\.\1_BAK_<date>;", step3b))


def test_full_rebuild_restores_every_kept_table_the_replay_rewrites():
    teardown = read("snowflake/teardown.sql")
    dropped = set(re.findall(r"^DROP (?:DYNAMIC )?TABLE IF EXISTS DBA_MAINT_DB\.OVERWATCH\.(\w+);", teardown, re.M))
    rewritten: dict[str, set[str]] = {}
    for path in sorted((_SF / "migrations").glob("V[0-9]*.sql")):
        for m in re.finditer(r"^(?:UPDATE|DELETE FROM|MERGE INTO|INSERT INTO|INSERT OVERWRITE INTO) "
                             r"DBA_MAINT_DB\.OVERWATCH\.(\w+)", path.read_text(encoding="utf-8"), re.M):
            table = m.group(1)
            if table not in dropped and table != "SCHEMA_VERSION":
                rewritten.setdefault(table, set()).add(path.name[:4])
    assert {"ALERT_CONFIG", "ALERT_ROUTES", "SETTINGS"} <= set(rewritten), "scan went vacuous"
    fr = read("docs/FULL_REBUILD.md")
    restored = _restored_tables(fr)
    missing = set(rewritten) - restored - _REPLAY_DML_NEEDS_NO_RESTORE
    assert not missing, {t: sorted(rewritten[t]) for t in missing}
    # every restored table has a step-1 clone to restore from
    bak = (_RB / "00_backup_operator_data.sql").read_text(encoding="utf-8")
    for table in restored:
        assert f"CLONE DBA_MAINT_DB.OVERWATCH.{table};" in bak, table
    gen = _gen()
    for table in restored:                       # the generated headers name the same set
        assert table in gen.BACKUP_HEADER and table in gen.README_NOTES, table
    # the old promise is gone, and step 3 sends the keep path to 3b
    assert "That is the designed restore path" not in fr
    step3 = " ".join(_section(fr, "## 3. Migrations", "## 3b.").split())
    assert "V034 sets every 'ALL' route's COMPANY_FILTER to 'ALFA'" in step3 and "Step 3b" in step3


def test_step_3b_closes_resurrected_rules_and_keeps_dead_routes_off():
    fr = read("docs/FULL_REBUILD.md")
    step3b = _section(fr, "## 3b. Put back what the replay rewrote", "## 4. Grants")
    # the replay-only rules are captured BEFORE ALERT_CONFIG is overwritten, their events closed after
    capture = step3b.index("CREATE TEMPORARY TABLE OW_REPLAY_ONLY_RULES")
    overwrite = step3b.index("INSERT OVERWRITE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG")
    close = step3b.index("UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS")
    assert capture < overwrite < close
    assert "MINUS SELECT RULE_ID FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG_BAK_<date>" in step3b
    assert "AND RULE_ID IN (SELECT RULE_ID FROM OW_REPLAY_ONLY_RULES)" in step3b
    # a restored ENABLED route whose integration the teardown dropped goes back off until step 7b
    routes = step3b.index("INSERT OVERWRITE INTO DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES")
    show = step3b.index("SHOW NOTIFICATION INTEGRATIONS;")
    assert routes < show < step3b.index("UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES SET ENABLED = FALSE")
    assert 'NOT IN\n           (SELECT UPPER("name") FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())));' in step3b
    # step 7b still re-enables exactly the routes step 0 recorded
    assert re.search(r"SET ENABLED = TRUE\s+WHERE ROUTE_ID IN \(", _section(fr, "## 7b.", "## 8."))


def test_full_rebuild_records_and_restores_the_warehouse_settings_v002_resets():
    v002 = read("snowflake/migrations/V002__facts.sql")
    assert "ALTER WAREHOUSE WH_ALFA_ADMIN SET STATEMENT_TIMEOUT_IN_SECONDS = 300;" in v002   # still replayed
    fr = read("docs/FULL_REBUILD.md")
    step0 = _section(fr, "## 0. Decide what survives", "## 1. Backups")
    assert "SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN WAREHOUSE WH_ALFA_ADMIN;" in step0
    assert "SHOW WAREHOUSES LIKE 'WH_ALFA_ADMIN';" in step0
    step3 = _section(fr, "## 3. Migrations", "## 3b.")
    assert "ALTER WAREHOUSE WH_ALFA_ADMIN SET RESOURCE_MONITOR = NULL;" in step3
    assert "DROP RESOURCE MONITOR IF EXISTS OVERWATCH_RM;" in step3
    step3b = _section(fr, "## 3b.", "## 4. Grants")
    assert "ALTER WAREHOUSE WH_ALFA_ADMIN SET STATEMENT_TIMEOUT_IN_SECONDS = <step-0 value>;" in step3b
    # the per-file path creates the shim roles itself (rebuild/02 does it in its header)
    shim = re.findall(r"^CREATE ROLE IF NOT EXISTS (\w+);$", _preamble(), re.M)
    for role in shim:
        assert f"CREATE ROLE IF NOT EXISTS {role};" in step3, role


def test_teardown_restore_notes_and_runbook_name_the_replay_steps():
    td = read("snowflake/teardown.sql")
    restore = " ".join(_section(td, "-- RESTORE", "-- Run as the deployment role").replace("--", " ").split())
    assert "replay shim" in restore and "FULL_REBUILD.md step 3b" in restore
    assert "STATEMENT_TIMEOUT_IN_SECONDS" in restore
    dr = " ".join(_section(read("RUNBOOK.md"), "## 16. Disaster recovery", "## 17. Glossary").split())
    assert "CREATE ROLE IF NOT EXISTS OVERWATCH_MONITOR" in dr
