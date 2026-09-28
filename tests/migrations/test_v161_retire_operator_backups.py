"""V161: scheduled operator-data backups retired (owner decision 2026-09-28).

V158's daily TASK_BACKUP_OPERATOR generations in DBA_MAINT_DB.OVERWATCH_BAK, the weekly <T>_BAK_LAST copies, the
OPERATOR_BACKUP_LOG ledger, the BACKUP_KEEP_* settings and the OPERATOR_BACKUP_DAILY dead-man row all go; recovery is
Time Travel plus the manual <T>_BAK_<yyyymmdd> clones. These tests lock the forward generation, the guards and their
order, the exact (never pattern-matched) drop set, the existence-gated drops, the view re-derivation back to V151's
text, and the app / script / doc state that says the backups are gone.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_migration_proc_syntax import _strip_noise

_ROOT = Path(__file__).resolve().parents[2]
_MIGDIR = _ROOT / "snowflake" / "migrations"
_NAME = "V161__retire_operator_backups.sql"
_MIG = (_MIGDIR / _NAME).read_text(encoding="utf-8")
_V151 = (_MIGDIR / "V151__security_change_risk_identity_policy_drops.sql").read_text(encoding="utf-8")
_V158 = (_MIGDIR / "V158__operator_backup_generations.sql").read_text(encoding="utf-8")
_V160 = (_MIGDIR / "V160__sleep_polling_alert.sql").read_text(encoding="utf-8")

_VIEW_RE = re.compile(r"CREATE OR REPLACE VIEW DBA_MAINT_DB\.OVERWATCH\.V_SECURITY_EXCEPTION_QUEUE AS.*?;\n", re.S)
_TABLES = tuple(re.findall(r"'(\w+)'", re.search(r"tables ARRAY DEFAULT \[(.*?)\];", _V158, re.S).group(1)))
_MOVE = ("OPERATOR_BACKUP_LOG", *(f"{t}_BAK_LAST" for t in _TABLES))


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _norm(text: str) -> str:
    return " ".join(text.split())


def _blocks(text: str) -> list[str]:
    """Every EXECUTE IMMEDIATE $$ ... $$ block body, in file order."""
    return re.findall(r"EXECUTE IMMEDIATE\n\$\$\n(.*?)\n\$\$;\n", text, re.S)


def _gen_env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("V161_OUT", "PREFLIGHT_OUT", "PART_B_OUT")}
    env.update(extra)
    return env


def _run_gen(tmp_path: Path, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v161.py")], env=_gen_env(**extra),
                          cwd=tmp_path, capture_output=True, text=True)


def _extras(tmp_path: Path) -> tuple[str, str]:
    pre, part_b = tmp_path / "PREFLIGHT_V161.sql", tmp_path / "PART_B_V161.sql"
    result = _run_gen(tmp_path, V161_OUT=str(tmp_path / "m.sql"), PREFLIGHT_OUT=str(pre), PART_B_OUT=str(part_b))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "m.sql").read_text(encoding="utf-8") == _MIG     # the extras never change the migration
    return pre.read_text(encoding="utf-8"), part_b.read_text(encoding="utf-8")


# -- generation ----------------------------------------------------------------------------------------------

def test_v161_regenerates_byte_identical_and_writes_nothing_else(tmp_path):
    out = tmp_path / "regen.sql"
    result = _run_gen(tmp_path, V161_OUT=str(out))
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == (_MIGDIR / _NAME).read_bytes(), (
        "V161 drifted from its forward-generation -- edit outputs/gen_v161.py, not the .sql.")
    assert b"\r\n" not in out.read_bytes()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["regen.sql"]     # PREFLIGHT / PART B only on request
    assert "PREFLIGHT" not in result.stdout and "PART B" not in result.stdout


def test_v161_generator_never_imports_the_v158_generator():
    # outputs/gen_v158.py rewrites V158 at import time (no __main__ guard)
    gen = _read("outputs/gen_v161.py")
    assert "import gen_v158" not in gen and "from gen_v158" not in gen and "outputs.gen_v158" not in gen


@pytest.mark.parametrize("which", ["preflight", "part_b"])
def test_v161_preflight_and_part_b_are_read_only(tmp_path, which):
    pre, part_b = _extras(tmp_path)
    sql = pre if which == "preflight" else part_b
    code = _strip_noise(sql)                       # comments + string literals out
    for banned in ("INSERT", "UPDATE", "DELETE", "MERGE", "CALL", "CREATE", "ALTER", "DROP", "TRUNCATE",
                   "GRANT", "REVOKE", "EXECUTE", "UNDROP", "RENAME"):
        assert not re.search(rf"\b{banned}\b", code, re.I), (which, banned)
    assert re.search(r"\bSELECT\b", code)


def test_v161_preflight_lists_what_v161_would_stop_on_and_what_it_keeps(tmp_path):
    pre, _ = _extras(tmp_path)
    assert pre.startswith("-- PREFLIGHT_V161.sql -- READ-ONLY")
    assert "'FOREIGN' END AS KIND" in pre
    # review r1: SHOW OBJECTS lists only tables and views -- every non-table kind V161 checks is listed per kind
    assert not re.search(r"^SHOW OBJECTS", pre, re.M)
    for view, col in (("STAGES", "STAGE_SCHEMA"), ("SEQUENCES", "SEQUENCE_SCHEMA"),
                      ("FILE_FORMATS", "FILE_FORMAT_SCHEMA"), ("FUNCTIONS", "FUNCTION_SCHEMA"),
                      ("PROCEDURES", "PROCEDURE_SCHEMA"), ("PIPES", "PIPE_SCHEMA")):
        assert f"FROM DBA_MAINT_DB.INFORMATION_SCHEMA.{view} WHERE {col} = 'OVERWATCH_BAK'" in pre, view
    # review r2: database-wide SHOW + a schema filter reads 0 (never errors) once the schema is gone or unreadable,
    # so Run All reaches every grid below; only the V161 migration's own SHOW ... IN SCHEMA is gated
    assert not re.search(r"^SHOW \w+ IN SCHEMA DBA_MAINT_DB\.OVERWATCH_BAK", pre, re.M)
    for kind in ("TASKS", "STREAMS", "ALERTS"):
        assert (f"SHOW {kind} IN DATABASE DBA_MAINT_DB;\nSELECT COUNT(*) AS OVERWATCH_BAK_{kind} FROM "
                "TABLE(RESULT_SCAN(LAST_QUERY_ID())) WHERE \"schema_name\" = 'OVERWATCH_BAK';") in pre, kind
    assert "'MANUAL_CLONE_KEPT'" in pre and "REGEXP_LIKE(TABLE_NAME, '.+_BAK_[0-9]{8}')" in pre
    # review r3: P3 groups by KIND, so its comment promises the grid it returns (not "26 rows")
    p3 = pre[pre.index("-- P3."):pre.index("-- P4.")]
    assert "expect 26 rows" not in p3 and "TABLES 26" in p3 and "TRANSIENT_TABLES 25" in p3 and "GROUP BY 1" in p3
    assert "TASK_HISTORY(" in pre
    # review r1: the ledger read (the one statement a partial V161 breaks) runs LAST, alone
    last = pre.rstrip().rsplit("\n\n", 1)[1]
    assert "FROM DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG" in last and "UNION" not in last
    assert pre.count("OVERWATCH.OPERATOR_BACKUP_LOG") == 1


def test_v161_part_b_checks_every_retired_object(tmp_path):
    _, part_b = _extras(tmp_path)
    for n in range(1, 14):
        assert f"V161.{n} " in part_b, n
    for frag in ("SCHEMA_NAME = 'OVERWATCH_BAK') = 0", "PROCEDURE_NAME = 'SP_BACKUP_OPERATOR_TABLES') = 0",
                 "SOURCE_NAME = 'OPERATOR_BACKUP_DAILY') = 0", "KEY IN ('BACKUP_KEEP_DAILY', 'BACKUP_KEEP_WEEKLY')) = 0",
                 "SHOW TASKS LIKE 'TASK_BACKUP_OPERATOR' IN SCHEMA DBA_MAINT_DB.OVERWATCH;",
                 "NOT CONTAINS(GET_DDL('VIEW', 'DBA_MAINT_DB.OVERWATCH.V_SECURITY_EXCEPTION_QUEUE'), 'OVERWATCH_BAK')"):
        assert frag in part_b, frag
    # the GET_DDL fragments are literally in the view V161 installs
    view = _VIEW_RE.findall(_MIG)[0]
    assert "DROP BACKUP POLICY %" in view and "TF~_%" in view and "OVERWATCH_BAK" not in view
    # the 26 names PART B checks are the 26 V161 moves out
    in_list = re.search(r"TABLE_NAME IN \((.*?)\)\) = 0", part_b, re.S).group(1)
    assert tuple(re.findall(r"'(\w+)'", in_list)) == _MOVE
    # review r1: the footprint grid sees a fallback DROP of ANY moved table (the permanent ledger included)
    grid = part_b[part_b.index("-- V161.13"):]
    tokens = re.findall(r"CONTAINS\(UPPER\(QUERY_PREVIEW\), '(\w+)'\)", grid)
    for name in _MOVE:
        assert any(tok in name for tok in tokens), name
    assert "OPERATOR_BACKUP_LOG" in tokens


# -- guard, order, shape -------------------------------------------------------------------------------------

def test_v161_first_line_guard_and_version():
    assert _MIG.startswith(f"-- {_NAME}\n")
    guard = _blocks(_MIG)[0] + "\n"
    guard160 = _blocks(_V160)[0] + "\n"
    want = (guard160.replace("-20160", "-20161").replace("'V160 requires V159 first", "'V161 requires V160 first")
            .replace("IF (v < 159)", "IF (v < 160)"))
    assert guard == want
    assert "SELECT 161 AS VERSION" in _MIG
    assert _MIG.rstrip().endswith("WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION "
                                  "WHERE VERSION = 161);")


def test_v161_file_order():
    # review r1: the session is pinned first, so the security loader records DBA_MAINT_DB.OVERWATCH
    assert "\nUSE SCHEMA DBA_MAINT_DB.OVERWATCH;\n" in _MIG and "USE DATABASE" not in _MIG
    marks = ["\nUSE SCHEMA DBA_MAINT_DB.OVERWATCH;\n", "EXCEPTION (-20161", "EXCEPTION (-20611",
             "ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR SUSPEND;",
             "EXCEPTION (-20612", "DROP TASK DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR;",
             "DROP PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_BACKUP_OPERATOR_TABLES();",
             " RENAME TO DBA_MAINT_DB.OVERWATCH_BAK.", "DROP SCHEMA DBA_MAINT_DB.OVERWATCH_BAK CASCADE;",
             "DELETE FROM DBA_MAINT_DB.OVERWATCH.SETTINGS",
             "DELETE FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE",
             "UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS", "CREATE OR REPLACE VIEW", "SELECT 161 AS VERSION"]
    pos = [_MIG.index(m) for m in marks]
    assert pos == sorted(pos), [m for m, p in zip(marks, pos, strict=True) if p != sorted(pos)[marks.index(m)]]
    for m in marks:
        assert _MIG.count(m) == 1, m
    assert len(_blocks(_MIG)) == 4          # guard, preflight, in-flight, retire


def test_v161_creates_nothing_but_the_view_and_runs_nothing():
    code = _strip_noise(_MIG)
    assert len(re.findall(r"\bCREATE\b", code)) == 1 and "CREATE OR REPLACE VIEW" in _MIG
    assert "CREATE OR REPLACE PROCEDURE" not in _MIG and "EXECUTE TASK" not in _MIG
    assert not re.search(r"^\s*CALL\b", _MIG, re.M) and not re.search(r"\bCALL\b", code)
    assert "RAISE EXCEPTION (" not in _MIG and "DETAIL =" not in _MIG and "$_" not in _MIG
    # the collision scans of V158 (any CREATE of an _OWBAK_ name) never see one here
    assert not re.search(r"CREATE[^;]*_OWBAK_[DW]\d{8}", _MIG)


def test_v161_names_are_exactly_v158s_25_never_a_pattern():
    assert len(_TABLES) == 25 and _TABLES[0] == "SETTINGS" and _TABLES[-1] == "SLO_OBJECTIVES"
    retire = _blocks(_MIG)[3]
    targets = re.search(r"targets ARRAY DEFAULT \[(.*?)\];", retire, re.S).group(1)
    assert tuple(re.findall(r"'(\w+)'", targets)) == _MOVE
    # the preflight accepts exactly V158's generation names plus the same 26
    pre = _blocks(_MIG)[1]
    assert f"REGEXP_LIKE(TABLE_NAME, '({'|'.join(_TABLES)})_OWBAK_[DW][0-9]{{8}}')" in pre
    assert tuple(re.findall(r"'(\w+)'", pre.split("OR TABLE_NAME IN (", 1)[1].split(")))", 1)[0])) == _MOVE
    # nothing is ever matched by LIKE (the view below keeps V151's own LIKE predicates): the only LIKE before the
    # view is the SHOW TASKS name probe, whose result is then filtered on the exact name
    head = "\n".join(line for line in _MIG[:_MIG.index("CREATE OR REPLACE VIEW")].splitlines()
                     if not line.lstrip().startswith("--"))      # executable lines only (the header cites SHOW ... LIKE)
    assert re.findall(r"\bLIKE\b\s*'[^']*'", head) == ["LIKE 'TASK_BACKUP_OPERATOR'"]
    assert "_BAK_2" not in _MIG          # the manual clones' date token never appears


def _prune_rx() -> re.Pattern:
    return re.compile("(" + "|".join(_TABLES) + ")_OWBAK_[DW][0-9]{8}")


def test_v161_preflight_accepts_only_v158s_objects():
    rx = _prune_rx()
    ok = ["SETTINGS_OWBAK_D20260926", "SLO_OBJECTIVES_OWBAK_W20260927", "OPERATOR_BACKUP_LOG",
          "ALERT_EVENTS_BAK_LAST"]
    foreign = ["SETTINGS", "SETTINGS_BAK_20260712", "FOO_OWBAK_D20260926", "SETTINGS_OWBAK_D2026092",
               "XSETTINGS_OWBAK_D20260926", "SETTINGS_OWBAK_D20260926_COPY", "MY_NOTES"]
    for name in ok:
        assert rx.fullmatch(name) or name in _MOVE, name
    for name in foreign:
        assert not rx.fullmatch(name) and name not in _MOVE, name


def test_v161_every_drop_is_existence_gated():
    retire = _blocks(_MIG)[3]
    code = _strip_noise(retire)
    assert "IF EXISTS" not in code, "a no-op DROP IF EXISTS still succeeds and is scored as a DROP"
    task = retire.index("DROP TASK DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR;")
    assert retire.rindex("SHOW TASKS LIKE 'TASK_BACKUP_OPERATOR' IN SCHEMA DBA_MAINT_DB.OVERWATCH;", 0, task) >= 0
    assert "WHERE \"name\" = 'TASK_BACKUP_OPERATOR';\n    IF (n > 0) THEN\n        DROP TASK" in retire
    assert ("AND ARGUMENT_SIGNATURE = '()';\n    IF (n > 0) THEN\n        DROP PROCEDURE "
            "DBA_MAINT_DB.OVERWATCH.SP_BACKUP_OPERATOR_TABLES();") in retire
    assert "IF (bak_schema) THEN\n        DROP SCHEMA DBA_MAINT_DB.OVERWATCH_BAK CASCADE;" in retire
    # the table drops/moves run only over the names the one probe found
    loop = retire[retire.index("IF (ARRAY_SIZE(:found_names) > 0) THEN"):retire.index("IF (bak_schema) THEN\n        DROP SCHEMA")]
    assert "tname := GET(:found_names, i)::VARCHAR;" in loop
    assert loop.count("'DROP TABLE DBA_MAINT_DB.OVERWATCH.' || :tname") == 2        # the fallback + no-schema path
    assert "WHEN OTHER THEN\n                        EXECUTE IMMEDIATE 'DROP TABLE" in loop
    assert "AND ARRAY_CONTAINS(TABLE_NAME::VARIANT, :targets);" in retire
    assert "TABLE_SCHEMA = 'OVERWATCH' AND TABLE_TYPE = 'BASE TABLE'" in retire


def test_v161_preflight_stops_before_any_change():
    pre = _blocks(_MIG)[1]
    assert "RAISE foreign_objects;" in pre
    code = _strip_noise(pre)
    for banned in ("DROP", "ALTER", "DELETE", "UPDATE", "INSERT", "RENAME"):
        assert not re.search(rf"\b{banned}\b", code), banned
    assert _MIG.index("RAISE foreign_objects;") < _MIG.index("SUSPEND;")
    # review r1: every kind DROP SCHEMA ... CASCADE would take, not just tables and views, counts as foreign
    for view, col in (("STAGES", "STAGE_SCHEMA"), ("SEQUENCES", "SEQUENCE_SCHEMA"),
                      ("FILE_FORMATS", "FILE_FORMAT_SCHEMA"), ("FUNCTIONS", "FUNCTION_SCHEMA"),
                      ("PROCEDURES", "PROCEDURE_SCHEMA"), ("PIPES", "PIPE_SCHEMA")):
        assert f"+ (SELECT COUNT(*) FROM DBA_MAINT_DB.INFORMATION_SCHEMA.{view} WHERE {col} = 'OVERWATCH_BAK')" in pre
    for kind in ("TASKS", "STREAMS", "ALERTS"):
        show = f"        SHOW {kind} IN SCHEMA DBA_MAINT_DB.OVERWATCH_BAK;\n"
        assert (show + "        SELECT COUNT(*) INTO :n FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));\n"
                "        foreign_n := foreign_n + n;\n") in pre, kind
    # SHOW ... IN SCHEMA errors on a missing schema: every probe sits inside the existence gate (re-run safe)
    gate = pre.index("    IF (bak_schema) THEN\n")
    assert gate < pre.index("INFORMATION_SCHEMA.TABLES") and gate < pre.index("SHOW TASKS")
    assert pre.index("SHOW ALERTS") < pre.index("    END IF;\n    IF (foreign_n > 0) THEN")


def test_v161_in_flight_guard_follows_the_suspend_and_ignores_stale_rows():
    guard = _blocks(_MIG)[2]
    assert _MIG.index("TASK_BACKUP_OPERATOR SUSPEND;") < _MIG.index("backup_in_flight EXCEPTION (-20612")
    assert "TASK_NAME => 'TASK_BACKUP_OPERATOR'" in guard
    assert "WHERE DATABASE_NAME = 'DBA_MAINT_DB' AND SCHEMA_NAME = 'OVERWATCH'" in guard
    assert "STATE = 'EXECUTING'" in guard
    assert ("OR (STATE = 'SCHEDULED'\n                    AND SCHEDULED_TIME <= CURRENT_TIMESTAMP()\n"
            "                    AND SCHEDULED_TIME >= DATEADD('minute', -30, CURRENT_TIMESTAMP()))") in guard
    # review r1: a replay's own V158-tail run normally ends within a bounded wait, so V161 completes instead of
    # halting; it raises only if the run is still going after the loop. Review r2: 16 x 15 s (~4 min) stays inside
    # the 300 s STATEMENT_TIMEOUT_IN_SECONDS V002 sets on WH_ALFA_ADMIN, so -20612 (not a timeout) is what fires
    v002 = (_MIGDIR / "V002__facts.sql").read_text(encoding="utf-8")
    assert "ALTER WAREHOUSE WH_ALFA_ADMIN SET STATEMENT_TIMEOUT_IN_SECONDS = 300;" in v002
    attempts = int(re.search(r"FOR attempt IN 1 TO (\d+) DO", guard).group(1))
    assert attempts * 15 + attempts * 2 < 300, attempts
    loop = guard[guard.index(f"FOR attempt IN 1 TO {attempts} DO"):guard.index("END FOR;")]
    assert loop.index("SELECT COUNT(*) INTO :running") < loop.index("IF (running = 0) THEN\n            BREAK;")
    assert loop.index("BREAK;") < loop.index("SELECT SYSTEM$WAIT(15);")
    assert guard.index("END FOR;") < guard.index("IF (running > 0) THEN\n        RAISE backup_in_flight;")
    assert _MIG.count("SYSTEM$WAIT") == 2          # the header's disclosure + the one wait


def test_v161_data_steps_close_the_dead_man_last():
    assert ("DELETE FROM DBA_MAINT_DB.OVERWATCH.SETTINGS\n WHERE KEY IN ('BACKUP_KEEP_DAILY', 'BACKUP_KEEP_WEEKLY');"
            in _MIG)
    assert ("DELETE FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE\n"
            " WHERE SOURCE_NAME = 'OPERATOR_BACKUP_DAILY';") in _MIG
    upd = _MIG[_MIG.index("UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS"):]
    upd = upd[:upd.index(";") + 1]
    assert "SET STATUS = 'RESOLVED', RESOLUTION_KIND = 'EXPECTED'" in upd
    assert "RULE_ID = 'OPS_PIPELINE_DEGRADED'" in upd
    assert "STARTSWITH(DEDUPE_KEY, 'OPS_PIPELINE_DEGRADED|STALE|OPERATOR_BACKUP_DAILY|')" in upd
    assert "STATUS IN ('OPEN', 'ACK', 'SNOOZED')" in upd
    # the key shape the scans write (V157 / V160 arm [22]): RULE|STALE|SOURCE|day
    assert "c.RULE_ID || '|STALE|' || f.SOURCE_NAME || '|'" in _V160


def test_v161_plain_sql_parses():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    stmts = list(_plain_statements(_MIG))
    assert any(s.startswith("CREATE OR REPLACE VIEW") for s in stmts)
    assert sum(s.startswith("DELETE ") for s in stmts) == 2 and any(s.startswith("UPDATE ") for s in stmts)
    for statement in stmts:
        sqlglot.parse(statement, dialect="snowflake")


def test_v161_description_fits_and_doubles_apostrophes():
    desc = re.search(r"SELECT 161 AS VERSION,\n       '(.*)' AS DESCRIPTION\n", _MIG, re.S).group(1)
    assert "\n" not in desc and "'" not in desc.replace("''", "")
    assert len(desc.replace("''", "'")) <= 4000
    assert desc.startswith("Scheduled operator-data backups retired (owner decision 2026-09-28)")


# -- the view -------------------------------------------------------------------------------------------------

def test_v161_view_is_v151s_text_and_v158s_minus_the_carve_out():
    new, v158, v151 = _VIEW_RE.findall(_MIG), _VIEW_RE.findall(_V158), _VIEW_RE.findall(_V151)
    assert len(new) == len(v158) == len(v151) == 1
    assert new[0] == v151[0]
    carve = v158[0][v158[0].index("      -- V158 (Next-Fifty #32)"):v158[0].index("\n), open_actions AS (")]
    assert v158[0].replace("\n" + carve, "", 1) == new[0]
    assert "OVERWATCH_BAK" not in new[0] and "'SYSTEM'" not in new[0]


def test_v161_view_marker_names_the_current_definer():
    from tests.test_proc_lineage import _HISTORICAL_WAIVERS, _definers, _lineage, _migrations, _violations
    texts = _migrations()
    rows = {(r["v"], r["proc"]): r for r in _lineage(texts)}
    r = rows[(161, "V_SECURITY_EXCEPTION_QUEUE")]
    assert r["src"] == "marker" and r["claims"] == [158] and r["prev"] == 158 and not r["waived"], r
    assert [v for v in _definers(texts)["V_SECURITY_EXCEPTION_QUEUE"] if v > 151] == [158, 161]
    assert not [v for v in _violations(texts, _HISTORICAL_WAIVERS) if v.startswith("V161 ")]
    assert _MIG.count("-- >>> derived:") == 1 and "LINEAGE-WAIVER" not in _MIG


# -- registration -------------------------------------------------------------------------------------------

def test_v161_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    text = str(_EXPECTED_MIGRATIONS[161])
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_" not in text and "$" not in text
    assert "OVERWATCH_BAK" in text and "retired" in text


def test_v161_validate_and_docs():
    val = _read("snowflake/validate.sql")
    assert "'V001..V161 applied'" in val and "VERSION BETWEEN 1 AND 161) = 161" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert f"snowflake/migrations/{_NAME}" in _read(rel), rel
    assert (_ROOT / "snowflake" / "rebuild" / "02_migrations_V001_V161.sql").exists()
    assert not (_ROOT / "snowflake" / "rebuild" / "02_migrations_V001_V160.sql").exists()


def test_app_version_and_changelog():
    assert 'APP_VERSION = "4.598.0"' in _read("app/config.py")
    head = _read("CHANGELOG.md").split("\n## ", 2)[1]
    assert head.startswith("4.598.0 - Scheduled operator backups retired (V161)")
    assert "apply before then" in head and "CHANGE RISK" in head and "Time Travel" in head


# -- the retired state stays retired ------------------------------------------------------------------------

def test_no_setting_or_editor_for_the_retired_backups():
    from app.config import DEFAULT_SETTINGS
    from app.ui.pages import admin
    for key in ("BACKUP_KEEP_DAILY", "BACKUP_KEEP_WEEKLY"):
        assert key not in DEFAULT_SETTINGS and key not in admin._SETTING_EDITORS, key


def test_scripts_no_longer_expect_the_backup():
    val = _read("snowflake/validate.sql")
    assert "'Operator backups fresh" not in val
    # review r1: validate FAILs while a retired object exists, read from the objects (a replay that stopped at
    # V161 still reads 'V001..V161 applied'), outside the EXECUTE IMMEDIATE teeth
    head = val.split("EXECUTE IMMEDIATE $$", 1)[0]
    row = head[head.index("SELECT 'Scheduled operator backups retired (V161"):]
    row = row[:row.index("UNION ALL")]
    for leg in ("INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = 'OVERWATCH_BAK'",
                "PROCEDURE_SCHEMA = 'OVERWATCH' AND PROCEDURE_NAME = 'SP_BACKUP_OPERATOR_TABLES'",
                "KEY IN ('BACKUP_KEEP_DAILY', 'BACKUP_KEEP_WEEKLY')",
                "SOURCE_FRESHNESS_STATE WHERE SOURCE_NAME = 'OPERATOR_BACKUP_DAILY'"):
        assert leg in row, leg
    assert ") = 0,\n               'OK', 'FAIL: a retired backup object is back" in row and "SCHEMA_VERSION" not in row
    assert "OPERATOR_BACKUP_DAILY" not in val.split("EXECUTE IMMEDIATE $$", 1)[1]
    assert "TASK_BACKUP_OPERATOR" not in _read("snowflake/task_audit.sql")
    lcc = _read("snowflake/loader_chain_check.sql")
    assert "BackupOperatorTables" not in lcc and "OPERATOR_BACKUP_DAILY" not in lcc


def test_tag_builders_carry_no_backup_schema():
    from app.data import security_sql
    for sql in (security_sql.object_tag_coverage("ALL"), security_sql.untagged_objects("ALL")):
        assert "OVERWATCH_BAK" not in sql
    assert "_NOT_BACKUP_SCHEMA" not in _read("app/data/security_sql.py")


def test_task_cadence_leaves_the_retired_task_out():
    from app.data import ops_sql
    assert ops_sql._RETIRED_OVERWATCH_TASKS == ("TASK_BACKUP_OPERATOR",)
    sql = ops_sql.task_freshness_sla(14, "ALL")
    clause = "NOT (DATABASE_NAME = 'DBA_MAINT_DB' AND SCHEMA_NAME = 'OVERWATCH' AND NAME IN ('TASK_BACKUP_OPERATOR'))"
    assert sql.count(clause) == 2                       # both TASK_HISTORY reads (runs + last_run) share `where`
    assert sql.count("SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY") == 2
    # scoped: a same-named task elsewhere is still judged
    assert "NAME IN ('TASK_BACKUP_OPERATOR')" not in sql.replace(clause, "")


def test_teardown_and_clone_scripts_after_retirement():
    td = _read("snowflake/teardown.sql")
    assert "RETIRED by V161" in td and "DROP SCHEMA" not in td.upper()
    assert "DROP TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_BACKUP_OPERATOR;" in td       # a replay re-creates them
    assert "DROP PROCEDURE IF EXISTS DBA_MAINT_DB.OVERWATCH.SP_BACKUP_OPERATOR_TABLES();" in td
    assert "the B0 clones are the only copy outside Time Travel" in _norm(td)
    assert "-- CREATE TABLE DBA_MAINT_DB.OVERWATCH." not in td and "-- CREATE TRANSIENT TABLE DBA_MAINT_DB.OVERWATCH." in td
    assert "V001..V157 only" not in td and "OVERWATCH_BAK.<T>_OWBAK_D<yyyymmdd>;" not in td
    bak = _read("snowflake/rebuild/00_backup_operator_data.sql")
    # review r1: no IF NOT EXISTS -- an unedited suffix that already exists fails loudly, never keeps an old clone
    assert not re.search(r"^CREATE [^\n]*IF NOT EXISTS", bak, re.M) and not re.search(r"^CREATE TABLE ", bak, re.M)
    assert bak.count("CREATE TRANSIENT TABLE DBA_MAINT_DB.OVERWATCH.") == 29
    assert "FIRST change every _20260712 suffix" in bak and "re-run from the failing CREATE" in bak
    # review r2: rebuild/00 covers at least everything teardown B0 clones (DEPLOYMENT offers either one)
    b0 = td[td.index("-- B0. Backups"):td.index("-- B1. Drops")]
    b0_names = set(re.findall(r"^-- CREATE TRANSIENT TABLE DBA_MAINT_DB\.OVERWATCH\.(\w+?)_BAK_\d{8}", b0, re.M))
    r00_names = set(re.findall(r"^CREATE TRANSIENT TABLE DBA_MAINT_DB\.OVERWATCH\.(\w+?)_BAK_\d{8}", bak, re.M))
    assert len(b0_names) == 27 and not b0_names - r00_names, sorted(b0_names - r00_names)
    for name in r00_names:            # every clone has its verify row
        assert f"SELECT '{name}' AS TABLE_NAME," in bak, name
    fr = _read("docs/FULL_REBUILD.md")
    assert "`IF NOT EXISTS` keeps" not in fr and "it has no `IF NOT EXISTS`" in fr


_B1_REBUILT = {"ETL_REF_GAP_RESULTS", "ETL_RECON_RESULTS", "ETL_CYCLE_TASKS", "SLEEP_POLLING_WEEKLY",
               "OPERATOR_BACKUP_LOG"}   # scan caches rebuilt by their scans; the ledger V161 drops


def test_teardown_b0_clones_every_operator_table_b1_drops():
    # review r1: with no scheduled backup, B0 is the only copy outside Time Travel for a factory reset
    td = _read("snowflake/teardown.sql")
    b0 = td[td.index("-- B0. Backups"):td.index("-- B1. Drops")]
    b1 = td[td.index("-- B1. Drops"):td.index("-- To restore operator data after a factory reset")]
    dropped = set(re.findall(r"^-- DROP TABLE IF EXISTS DBA_MAINT_DB\.OVERWATCH\.(\w+);", b1, re.M))
    cloned = set(re.findall(r"^-- CREATE TRANSIENT TABLE DBA_MAINT_DB\.OVERWATCH\.(\w+)_BAK_\d{8}\s+CLONE "
                            r"DBA_MAINT_DB\.OVERWATCH\.(\w+);", b0, re.M))
    assert all(a == b for a, b in cloned)
    cloned_names = {a for a, _ in cloned}
    assert len(dropped) >= 25 and {"SETTINGS", "ALERT_ROUTES", "REMEDIATION_LOG"} <= dropped
    assert not (dropped - _B1_REBUILT) - cloned_names, sorted((dropped - _B1_REBUILT) - cloned_names)
    # and every one of V158's 25 is covered by B0 or dropped in Section A as rebuildable (DAILY_DIGEST)
    assert not set(_TABLES) - cloned_names - {"DAILY_DIGEST", "INCIDENTS", "INCIDENT_MEMBERS"}
    assert "DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST;" in td


def test_dr_docs_are_time_travel_and_manual_clones():
    rb = _read("RUNBOOK.md")
    dr = rb[rb.index("## 16. Disaster recovery"):rb.index("## 17. Glossary")]
    n = _norm(dr)
    assert "no scheduled operator-data backups" in n
    assert "INSERT OVERWRITE INTO <T> SELECT * FROM <T> AT(OFFSET => -3600);" in dr
    assert "INSERT OVERWRITE INTO <T> SELECT * FROM <T> BEFORE(STATEMENT => '<query_id>');" in dr
    assert "INSERT OVERWRITE INTO <T> SELECT * FROM <T>_BAK_<yyyymmdd>;" in dr
    assert "SHOW PARAMETERS LIKE 'DATA_RETENTION_TIME_IN_DAYS'" in dr and "at most 1 day" in n
    assert "CREATE TRANSIENT TABLE ... CLONE" in dr
    for gone in ("OVERWATCH_BAK", "_OWBAK_", "OPERATOR_BACKUP_LOG", "BACKUP_KEEP", "V001..V157",
                 "CLONE <T> AT(OFFSET"):
        assert gone not in dr, gone
    assert "| ~~TASK_BACKUP_OPERATOR~~ | retired V161 |" in rb
    assert "**Rolling back V161.**" in rb and "BACKUP_KEEP_DAILY 14" not in rb
    # review r1: the rollback recovers the dropped schema FIRST (UNDROP within retention), and redeploys 4.597.0
    rollback = _norm(rb[rb.index("**Rolling back V161.**"):].split("\n\n", 1)[0])
    # review r2: UNDROP SCHEMA brings the ledger + weekly copies back INSIDE OVERWATCH_BAK; they move back to
    # OVERWATCH (or the ledger is UNDROPped) before V158 creates empty ones
    move_back = "`ALTER TABLE DBA_MAINT_DB.OVERWATCH_BAK.<name> RENAME TO DBA_MAINT_DB.OVERWATCH.<name>;`"
    assert (rollback.index("UNDROP SCHEMA DBA_MAINT_DB.OVERWATCH_BAK;") < rollback.index(move_back)
            < rollback.index("UNDROP TABLE DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG;") < rollback.index("V158 in full"))
    assert "Redeploy app 4.597.0" in rollback and "0c8afb7" in rollback and "gone for good" in rollback
    assert "v4.597.0 tag" not in rb                              # no such tag exists
    hdr = _MIG[:_MIG.index("USE SCHEMA")]
    assert "UNDROP SCHEMA DBA_MAINT_DB.OVERWATCH_BAK" in hdr and "redeploy app 4.597.0" in hdr
    assert "RENAME TO DBA_MAINT_DB.OVERWATCH.<name>" in hdr and "UNDROP TABLE DBA_MAINT_DB.OVERWATCH.OPERATOR_BACKUP_LOG" in hdr
    # review r2: no doc quotes the pre-r1 -20612 text or promises the old 10-minute wait
    for rel in ("RUNBOOK.md", "DEPLOYMENT.md", "docs/FULL_REBUILD.md", "snowflake/teardown.sql", "CHANGELOG.md"):
        text = _read(rel)
        assert "run is in flight\"" not in text and "about 10 minutes" not in text, rel
    assert "still in flight after about 4 minutes" in _MIG
    dep = _read("DEPLOYMENT.md")
    six = dep[dep.index("## 6. Disaster recovery"):dep.index("- **App broken after deploy:**")]
    assert "No scheduled backups (V161" in six and "OVERWATCH_BAK" not in six and "V001..V157" not in six
    assert "BEFORE(STATEMENT => '<query_id>')" in six
    fr = _read("docs/FULL_REBUILD.md")
    assert "stop after V157" not in fr and "_OWBAK_" not in fr and "only copy" in fr
    feats = _read("FEATURES.md")
    assert "were retired by V161" in feats and "Daily (14) + Sunday-weekly (8)" not in feats
