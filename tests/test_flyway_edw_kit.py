"""Locks for the EDW Flyway CI/CD kit (flyway-edw/): the guard rules, the bundle, the promotion gate, drift policy,
scaffolding, and the consistency between the kit's config, Jenkinsfiles and docs.

The kit is a copy-into-your-EDW-repo deliverable; these tests run it in OVERWATCH's suite so it ships green.
Every guard rule has at least one fixture that must trip it; the shipped sample migrations must pass all of them.
"""

from __future__ import annotations

import importlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parents[1] / "flyway-edw"
sys.path.insert(0, str(KIT / "scripts"))

sqlscan = importlib.import_module("fwkit.sqlscan")
guard_mod = importlib.import_module("fwkit.guard")
manifest_mod = importlib.import_module("fwkit.manifest")
bundle_mod = importlib.import_module("fwkit.bundle")
promote_mod = importlib.import_module("fwkit.promote")
drift_mod = importlib.import_module("fwkit.drift")
scaffold_mod = importlib.import_module("fwkit.scaffold")
fw = importlib.import_module("fw")

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- helpers

def _mini_kit(tmp_path: Path) -> Path:
    """A kit with the real config files and no SQL, so a fixture's findings are its own."""
    root = tmp_path / "kit"
    root.mkdir(parents=True)
    for name in ("flyway.toml", "runways.toml"):
        shutil.copy(KIT / name, root / name)
    for d in ("migrations", "repeatable", "callbacks"):
        (root / d).mkdir()
    return root


def _write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _run(root: Path, base: str | None = None) -> list:
    return guard_mod.Guard(manifest_mod.load_kit(root), base=base, now=NOW).run()


def _rules(findings: list, severity: str = "error") -> set[str]:
    return {f.rule for f in findings if f.severity == severity}


V = "migrations/SALES/V2026.10.13.0900__sales_x.sql"


# --------------------------------------------------------------------------- the shipped kit

def test_runway_ids_are_the_airport_codes_in_order():
    kit = manifest_mod.load_kit(KIT)
    assert list(kit.runways) == ["DEV", "SAN", "SEA", "PHX", "MGM", "PRD"]
    assert [kit.runways[r].database for r in kit.runways] == [f"ALFA_EDW_{r}" for r in kit.runways]
    assert kit.runways["PRD"].promote_from == ("MGM",)
    assert set(kit.runways["MGM"].promote_from) == {"SAN", "SEA", "PHX"}
    for r in ("SAN", "SEA", "PHX"):
        assert kit.runways[r].promote_from == ("DEV",)


def test_flyway_toml_safety_settings_are_locked():
    kit = manifest_mod.load_kit(KIT)
    fw_ = kit.flyway["flyway"]
    assert fw_["cleanDisabled"] is True and fw_["baselineOnMigrate"] is False
    assert fw_["createSchemas"] is False and fw_["defaultSchema"] == "FLYWAY"
    for rid, env in kit.flyway["environments"].items():
        assert "JDBC_QUERY_RESULT_FORMAT=JSON" in env["url"], rid
        assert "password" not in {k.lower() for k in env}, rid
    assert kit.flyway["environments"]["RH_PRD"]["flyway"]["placeholders"]["tasks_enabled"] == "false"


def test_flyway_image_is_pinned_and_matches_version():
    kit = manifest_mod.load_kit(KIT).kit
    assert re.fullmatch(r"redgate/flyway:\d+\.\d+\.\d+", kit["flyway_image"])
    assert kit["flyway_image"].endswith(kit["flyway_version"])
    major, minor, _ = (int(x) for x in kit["flyway_version"].split("."))
    assert (major, minor) >= (13, 8)  # Snowflake snapshot-history support (drift preview) arrived in 13.8.0


# --------------------------------------------------------------------------- config rules FG018 / FG019

def test_fg018_trips_on_clean_enabled_and_password(tmp_path):
    root = _mini_kit(tmp_path)
    t = (root / "flyway.toml").read_text()
    t = t.replace("cleanDisabled = true\nvalidateOnMigrate", "cleanDisabled = false\nvalidateOnMigrate", 1)
    t = t.replace('user = "${env.FLYWAY_SF_USER}"', 'user = "${env.FLYWAY_SF_USER}"\npassword = "hunter2"', 1)
    (root / "flyway.toml").write_text(t)
    msgs = [f.message for f in _run(root) if f.rule == "FG018"]
    assert any("cleanDisabled" in m for m in msgs)
    assert any("password" in m.lower() for m in msgs)


def test_fg018_trips_on_wrong_db_and_missing_json_format(tmp_path):
    root = _mini_kit(tmp_path)
    t = (root / "flyway.toml").read_text().replace("db=ALFA_EDW_SAN&", "db=ALFA_EDW_PRD&")
    t = t.replace("&JDBC_QUERY_RESULT_FORMAT=JSON", "", 1)
    (root / "flyway.toml").write_text(t)
    msgs = [f.message for f in _run(root) if f.rule == "FG018"]
    assert any("db=ALFA_EDW_SAN" in m for m in msgs)
    assert any("JDBC_QUERY_RESULT_FORMAT" in m for m in msgs)


def test_fg019_trips_on_prod_runway_without_approval(tmp_path):
    root = _mini_kit(tmp_path)
    t = (root / "runways.toml").read_text().replace('approval_group = "edw-prod-approvers"', 'approval_group = ""')
    t = t.replace('lanes = ["main"]\npromote_from = ["MGM"]', 'lanes = ["main", "branch"]\npromote_from = ["MGM"]')
    (root / "runways.toml").write_text(t)
    msgs = [f.message for f in _run(root) if f.rule == "FG019"]
    assert any("approval_group" in m for m in msgs)
    assert any("main bundles only" in m for m in msgs)


# --------------------------------------------------------------------------- naming / versions

@pytest.mark.parametrize("rel", [
    "migrations/SALES/V1__sales_x.sql",
    "migrations/SALES/V2026.13.40.0900__sales_x.sql",
    "migrations/sales/V2026.10.13.0900__sales_x.sql",
    "migrations/SALES/V2026.10.13.0900__finance_x.sql",
    "repeatable/SALES/R__30_FINANCE__v_x.sql",
    "repeatable/SALES/R__v_x.sql",
    "callbacks/beforeDeploy.sql",
    "migrations/V2026.10.13.0900__sales_x.sql",
])
def test_fg001_naming(tmp_path, rel):
    root = _mini_kit(tmp_path)
    _write(root, rel, "SELECT 1;\n")
    assert "FG001" in _rules(_run(root))


def test_fg002_duplicate_version_across_schemas_and_orphan_undo(tmp_path):
    root = _mini_kit(tmp_path)
    _write(root, "migrations/SALES/V2026.10.13.0900__sales_a.sql", "SELECT 1;\n")
    _write(root, "migrations/FINANCE/V2026.10.13.0900.0__finance_b.sql", "SELECT 1;\n")
    assert "FG002" in _rules(_run(root))  # 2026.10.13.0900.0 == 2026.10.13.0900: numeric parts, trailing zeros
    root2 = _mini_kit(tmp_path / "b")
    _write(root2, "migrations/SALES/U2026.10.13.0900__sales_a.sql", "SELECT 1;\n")
    assert "FG002" in _rules(_run(root2))


def test_fg004_future_version(tmp_path):
    root = _mini_kit(tmp_path)
    _write(root, "migrations/SALES/V2027.01.01.0000__sales_x.sql", "SELECT 1;\n")
    assert "FG004" in _rules(_run(root))


def test_version_key_matches_flyway():
    assert guard_mod.version_key("2026.10.13.0900") == guard_mod.version_key("2026.10.13.900")
    assert guard_mod.version_key("1.0") == guard_mod.version_key("1")
    assert guard_mod.version_key("2026.10.13.0900.1") > guard_mod.version_key("2026.10.13.0900")


# --------------------------------------------------------------------------- FG003 immutability (git)

def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_fg003_applied_migration_edit_and_delete(tmp_path):
    root = _mini_kit(tmp_path)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    a = _write(root, "migrations/SALES/V2026.10.13.0900__sales_a.sql", "CREATE TABLE IF NOT EXISTS SALES.A (X INT);\n")
    b = _write(root, "migrations/SALES/V2026.10.13.0901__sales_b.sql", "CREATE TABLE IF NOT EXISTS SALES.B (X INT);\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    assert "FG003" not in _rules(_run(root, base="HEAD"))
    a.write_text("CREATE TABLE IF NOT EXISTS SALES.A (X INT, Y INT);\n")
    b.unlink()
    msgs = [f for f in _run(root, base="HEAD") if f.rule == "FG003"]
    assert {Path(f.path).name for f in msgs} == {a.name, b.name}


def test_fg004_warns_new_migration_older_than_base(tmp_path):
    root = _mini_kit(tmp_path)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _write(root, "migrations/SALES/V2026.10.13.0900__sales_a.sql", "CREATE TABLE IF NOT EXISTS SALES.A (X INT);\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _write(root, "migrations/SALES/V2026.10.12.0900__sales_older.sql", "CREATE TABLE IF NOT EXISTS SALES.O (X INT);\n")
    assert "FG004" in _rules(_run(root, base="HEAD"), "warning")


def test_guard_unknown_base_is_usage_error(tmp_path):
    root = _mini_kit(tmp_path)
    _git(root, "init", "-q")
    with pytest.raises(guard_mod.GuardUsageError):
        _run(root, base="no-such-ref")


# --------------------------------------------------------------------------- statement rules

@pytest.mark.parametrize(("rule", "sql"), [
    ("FG005", "SELECT '${not_declared}';"),
    ("FG006", "DROP TABLE SALES.ORDERS;"),
    ("FG006", "TRUNCATE TABLE SALES.ORDERS;"),
    ("FG006", "CREATE OR REPLACE TABLE SALES.ORDERS (ID INT);"),
    ("FG006", "ALTER TABLE SALES.ORDERS DROP COLUMN STATUS;"),
    ("FG006", "DELETE FROM SALES.ORDERS;"),
    ("FG006", "DROP DATABASE ALFA_DB;"),
    ("FG007", "CREATE WAREHOUSE WH_X;"),
    ("FG007", "GRANT ROLE R TO USER U;"),
    ("FG007", "GRANT USAGE ON DATABASE X TO ROLE R;"),
    ("FG007", "ALTER ACCOUNT SET TIMEZONE = 'UTC';"),
    ("FG008", "USE DATABASE X;"),
    ("FG008", "USE SCHEMA SALES;"),
    ("FG008", "ALTER SESSION SET TIMEZONE = 'UTC';"),
    ("FG009", "CREATE TABLE IF NOT EXISTS SALES.T AS SELECT * FROM ALFA_EDW_PRD.SALES.ORDERS;"),
    ("FG009", "CREATE OR REPLACE VIEW SALES.V COPY GRANTS AS SELECT ID FROM ${edw_db}.SALES.ORDERS;"),
    ("FG009", "CREATE OR REPLACE VIEW SALES.V COPY GRANTS AS SELECT ID FROM ${flyway:database}.SALES.ORDERS;"),
    ("FG010", "CREATE TABLE IF NOT EXISTS ORDERS (ID INT);"),
    ("FG010", "ALTER TABLE ORDERS ADD COLUMN X INT;"),
    ("FG011", "CREATE OR REPLACE VIEW SALES.V AS SELECT 1 AS X;"),
    ("FG011", "CREATE OR REPLACE PROCEDURE SALES.P() RETURNS INT LANGUAGE SQL AS $$ BEGIN RETURN 1; END; $$;"),
    ("FG014", "CREATE OR ALTER TASK SALES.T WAREHOUSE = W SCHEDULE = '5 MINUTE' AS SELECT 1;"),
    ("FG015", "DECLARE x INT; BEGIN x := 1; END;"),
    ("FG015", "BEGIN\n  SELECT 1;\nEND;"),
    ("FG015", "CREATE OR REPLACE PROCEDURE SALES.P() COPY GRANTS RETURNS INT LANGUAGE SQL AS $$ BEGIN RETURN 1;"),
    ("FG016", "CREATE PIPE IF NOT EXISTS SALES.P AS COPY INTO SALES.RAW FROM @SALES.S;"),
])
def test_statement_rules_trip(tmp_path, rule, sql):
    root = _mini_kit(tmp_path)
    _write(root, V, sql + "\n")
    assert rule in _rules(_run(root)), [f.render() for f in _run(root)]


@pytest.mark.parametrize("sql", [
    "CREATE TABLE IF NOT EXISTS SALES.ORDERS (ID INT);",
    "ALTER TABLE SALES.ORDERS ADD COLUMN IF NOT EXISTS STATUS VARCHAR;",
    "ALTER TABLE SALES.ORDERS DROP CONSTRAINT PK_ORDERS;",
    "DELETE FROM SALES.ORDERS WHERE ID < 0;",
    "CREATE OR REPLACE VIEW SALES.V COPY GRANTS AS SELECT ID FROM SALES.ORDERS;",
    "CREATE OR ALTER VIEW SALES.V AS SELECT ID FROM SALES.ORDERS;",
    "CREATE SCHEMA IF NOT EXISTS SALES;",
    "-- USE DATABASE X; DROP TABLE SALES.ORDERS;  (comment only)\nSELECT 1;",
    "SELECT 'DROP TABLE SALES.ORDERS; USE DATABASE ALFA_PRD' AS NOT_CODE;",
    "BEGIN TRANSACTION;\nINSERT INTO SALES.ORDERS (ID) VALUES (1);\nCOMMIT;",
    "EXECUTE IMMEDIATE $$\nDECLARE x INT DEFAULT 1;\nBEGIN\n  RETURN x;\nEND;\n$$;",
    "CREATE OR REPLACE PROCEDURE SALES.P() COPY GRANTS RETURNS INT LANGUAGE SQL AS\n"
    "$$\nBEGIN\n  -- USE DATABASE in a comment; a ';' inside the body\n  RETURN 1;\nEND;\n$$;",
    "CREATE PIPE IF NOT EXISTS SALES.P AS COPY INTO RAW_ORDERS FROM @SALES.S;",
    "CREATE TABLE IF NOT EXISTS ${edw_db}.SALES.T (ID INT);",
])
def test_statement_rules_do_not_trip_on_good_sql(tmp_path, sql):
    root = _mini_kit(tmp_path)
    _write(root, V, sql + "\n")
    assert _rules(_run(root)) == set(), [f.render() for f in _run(root)]


def test_waiver_needs_ticket_and_reason_and_some_rules_are_unwaivable(tmp_path):
    root = _mini_kit(tmp_path)
    _write(root, V, "-- FLYWAY-WAIVER: FG006 EDW-123 retire the legacy table after the 2026 cutover\n"
                    "DROP TABLE IF EXISTS SALES.LEGACY;\n")
    assert _rules(_run(root)) == set()
    _write(root, V, "-- FLYWAY-WAIVER: FG006\nDROP TABLE IF EXISTS SALES.LEGACY;\n")
    assert {"FG001", "FG006"} <= _rules(_run(root))
    _write(root, V, "-- FLYWAY-WAIVER: FG006 EDW-1 nope\n-- FLYWAY-WAIVER: FG008 EDW-1 nope\n"
                    "DROP DATABASE X;\nUSE DATABASE X;\n")
    assert {"FG006", "FG008"} <= _rules(_run(root))


def test_fg012_repeatable_must_be_idempotent(tmp_path):
    root = _mini_kit(tmp_path)
    _write(root, "repeatable/SALES/R__30_SALES__v_x.sql",
           "CREATE VIEW SALES.V_X AS SELECT 1 AS A;\nINSERT INTO SALES.T VALUES (1);\n")
    msgs = [f for f in _run(root) if f.rule == "FG012"]
    assert len(msgs) == 2


def test_fg013_repeatable_dependency_order(tmp_path):
    root = _mini_kit(tmp_path)
    _write(root, "repeatable/SALES/R__30_SALES__v_a.sql",
           "CREATE OR REPLACE VIEW SALES.V_A COPY GRANTS AS SELECT * FROM SALES.V_B;\n")
    _write(root, "repeatable/SALES/R__30_SALES__v_b.sql",
           "CREATE OR REPLACE VIEW SALES.V_B COPY GRANTS AS SELECT 1 AS X;\n")
    assert "FG013" in _rules(_run(root))  # '30 SALES  v a' < '30 SALES  v b': V_A runs first but needs V_B
    (root / "repeatable/SALES/R__30_SALES__v_a.sql").rename(root / "repeatable/SALES/R__31_SALES__v_a.sql")
    assert "FG013" not in _rules(_run(root))


def test_flyway_description_and_sort_key_match_flyway():
    # ResourceNameParser: text after the separator with every '_' replaced by a space; repeatables sort on it
    assert guard_mod.flyway_description("R__30_SALES__v_open_orders.sql") == "30 SALES  v open orders"
    keys = sorted(guard_mod.flyway_description(n) for n in
                  ("R__80_SALES__task_x.sql", "R__30_SALES__v_x.sql", "R__30_FINANCE__v_y.sql", "R__20_SALES__fn.sql"))
    assert keys == ["20 SALES  fn", "30 FINANCE  v y", "30 SALES  v x", "80 SALES  task x"]


def test_fg014_task_file_gated_and_resumed_passes(tmp_path):
    root = _mini_kit(tmp_path)
    _write(root, "repeatable/SALES/R__80_SALES__task_x.sql",
           "ALTER TASK IF EXISTS SALES.TASK_X SUSPEND;\n"
           "CREATE OR ALTER TASK SALES.TASK_X WAREHOUSE = ${etl_wh} SCHEDULE = '5 MINUTE' AS SELECT 1;\n"
           "EXECUTE IMMEDIATE $$\nBEGIN\n  IF ('${tasks_enabled}' = 'true') THEN\n"
           "    LET r VARCHAR := (SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('SALES.TASK_X'));\n  END IF;\nEND;\n$$;\n")
    assert _rules(_run(root)) == set(), [f.render() for f in _run(root)]


def test_fg005_skipped_when_placeholder_replacement_is_off(tmp_path):
    root = _mini_kit(tmp_path)
    _write(root, "repeatable/SALES/R__20_SALES__fn_js.sql",
           "CREATE OR REPLACE FUNCTION SALES.FN_JS(S VARCHAR) COPY GRANTS RETURNS VARCHAR LANGUAGE JAVASCRIPT AS\n"
           "$$ return `hello ${S}`; $$;\n")
    assert "FG005" in _rules(_run(root))
    _write(root, "repeatable/SALES/R__20_SALES__fn_js.sql.conf", "placeholderReplacement=false\n")
    assert "FG005" not in _rules(_run(root))


def test_fg005_per_deploy_placeholder_in_a_repeatable(tmp_path):
    root = _mini_kit(tmp_path)
    _write(root, "repeatable/SALES/R__30_SALES__v_x.sql",
           "CREATE OR REPLACE VIEW SALES.V_X COPY GRANTS COMMENT = 'build ${git_sha}' AS SELECT 1 AS A;\n")
    assert "FG005" in _rules(_run(root))
    _write(root, V, "INSERT INTO SALES.AUDIT (NOTE) VALUES ('${git_sha}');\n")  # fine in a versioned migration
    assert [f for f in _run(root) if f.path == V] == []


def test_fg020_conf_keys(tmp_path):
    root = _mini_kit(tmp_path)
    _write(root, V, "SELECT 1;\n")
    _write(root, V + ".conf", "shouldexecute=${env}==PRD\nexecuteInTransaction=maybe\nnotAKey=1\n")
    msgs = [f.message for f in _run(root) if f.rule == "FG020"]
    assert any("unknown key 'notAKey'" in m for m in msgs)
    assert any("case-sensitive: shouldExecute" in m for m in msgs)  # SqlScriptMetadata keys are case-sensitive
    assert any("true or false" in m for m in msgs)
    _write(root, V + ".conf", "shouldExecute=${flyway:environment}==PRD\n")
    assert "FG020" in _rules(_run(root), "warning")


def test_fg017_secrets_anywhere_in_the_kit(tmp_path):
    root = _mini_kit(tmp_path)
    _write(root, "jenkins/Jenkinsfile", "def k = '-----BEGIN ENCRYPTED PRIVATE KEY-----'\n")
    _write(root, "bootstrap/x.sql", "CREATE USER U PASSWORD = 'Secret123';\n")
    assert {f.path for f in _run(root) if f.rule == "FG017"} == {"jenkins/Jenkinsfile", "bootstrap/x.sql"}


def test_guard_cli_exit_codes_and_junit(tmp_path):
    root = _mini_kit(tmp_path)
    _write(root, V, "DROP TABLE SALES.ORDERS;\n")
    junit = tmp_path / "out" / "guard.xml"
    assert fw.main(["--root", str(root), "guard", "--junit", str(junit)]) == 1
    xml = junit.read_text()
    assert 'name="FG006 destructive DDL (data loss)"' in xml and "<failure" in xml
    (root / V).write_text("SELECT 1;\n")
    assert fw.main(["--root", str(root), "guard"]) == 0
    assert fw.main(["--root", str(tmp_path / "nope"), "guard"]) == 2


# --------------------------------------------------------------------------- scanner

def test_scanner_splits_like_flyway():
    text = ("-- c; c\nCREATE OR REPLACE PROCEDURE S.P() AS $$ BEGIN RETURN ';'; END; $$;\n"
            "SELECT 'a;b' AS \"x;y\";  /* ; */\nSELECT 2")
    s = sqlscan.scan(text)
    assert [st.line for st in s.statements] == [2, 3, 4]
    assert s.statements[0].bodies and "RETURN" in s.statements[0].bodies[0].text
    assert "RETURN" not in s.statements[0].masked
    assert len(s.code) == len(s.masked) == len(text)
    assert sqlscan.scan("SELECT $$ open").unterminated == "dollar"


def test_normalize_name():
    assert sqlscan.normalize_name('${edw_db}.sales."Mixed Case"') == "SALES.Mixed Case"
    assert sqlscan.normalize_name("Sales.Orders") == "SALES.ORDERS"


# --------------------------------------------------------------------------- bundle

def test_bundle_verify_detects_tamper_extra_and_branch(tmp_path):
    res = bundle_mod.build(KIT, tmp_path / "d", build_id="8", git_sha="abc", branch="feature/x")
    bdir = bundle_mod.extract(res.archive, tmp_path / "x")
    with pytest.raises(bundle_mod.BundleError, match="accepts only"):
        bundle_mod.verify(bdir, require_branch="main")
    (bdir / "flyway.toml").write_text("tampered")
    (bdir / "evil.sql").write_text("DROP DATABASE X;")
    with pytest.raises(bundle_mod.BundleError, match=r"modified flyway\.toml.*|unexpected"):
        bundle_mod.verify(bdir)


def test_bundle_extract_refuses_unsafe_members(tmp_path):
    evil = tmp_path / "evil.tar.gz"
    payload = tmp_path / "p.txt"
    payload.write_text("x")
    with tarfile.open(evil, "w:gz") as tar:
        tar.add(payload, arcname="../escape.txt")
    with pytest.raises(bundle_mod.BundleError):
        bundle_mod.extract(evil, tmp_path / "out")


# --------------------------------------------------------------------------- promotion gate

def _manifest() -> dict:
    return {"versioned": [{"version": "2026.10.13.0900", "description": "sales create orders"},
                          {"version": "2026.10.13.0915", "description": "sales add status"}],
            "repeatable": [{"description": "30 SALES  v open orders"}]}


def _info(rows: list[dict]) -> dict:
    return {"schemaVersion": "2026.10.13.0915", "migrations": rows}


def test_promote_check_pass_and_any_of(tmp_path):
    good = _info([
        {"category": "Versioned", "version": "2026.10.13.0900", "state": "Success"},
        {"category": "Versioned", "version": "2026.10.13.915", "state": "Out of Order"},
        {"category": "Repeatable", "version": "", "description": "30 SALES  v open orders", "state": "Superseded"},
        {"category": "Repeatable", "version": "", "description": "30 SALES  v open orders", "state": "Success"},
    ])
    bad = _info([{"category": "Versioned", "version": "2026.10.13.0900", "state": "Pending"}])
    (tmp_path / "san.json").write_text(json.dumps(bad))
    (tmp_path / "sea.json").write_text(json.dumps(good))
    ok, verdicts = promote_mod.check("MGM", ("SAN", "SEA", "PHX"),
                                     {"SAN": tmp_path / "san.json", "SEA": tmp_path / "sea.json"}, _manifest())
    assert ok
    assert [v.ok for v in verdicts] == [False, True, False]


@pytest.mark.parametrize(("rows", "needle"), [
    ([{"category": "Versioned", "version": "2026.10.13.0900", "state": "Failed"}], "Failed"),
    ([{"category": "Versioned", "version": "2026.10.13.0900", "state": "Success"},
      {"category": "Versioned", "version": "2026.10.13.0915", "state": "Success"},
      {"category": "Repeatable", "description": "30 SALES  v open orders", "state": "Outdated"}], "Outdated"),
    ([], "not known"),
])
def test_promote_check_fails(rows, needle):
    v = promote_mod.judge("MGM", _info(rows), _manifest())
    assert not v.ok and any(needle in p for p in v.problems)


def test_promote_check_accepts_a_declared_should_execute_skip():
    m = _manifest()
    rows = [{"category": "Versioned", "version": "2026.10.13.0900", "state": "Ignored"},
            {"category": "Versioned", "version": "2026.10.13.0915", "state": "Success"},
            {"category": "Repeatable", "description": "30 SALES  v open orders", "state": "Success"}]
    assert not promote_mod.judge("MGM", _info(rows), m).ok  # Ignored without a declared shouldExecute: never ran
    m["versioned"][0]["should_execute"] = "${env}==PRD"
    assert promote_mod.judge("MGM", _info(rows), m).ok


def test_bundle_records_should_execute(tmp_path):
    root = _mini_kit(tmp_path)
    _write(root, V, "SELECT 1;\n")
    _write(root, V + ".conf", "shouldExecute=${env}==PRD\n")
    _write(root, "scripts/fw.py", "")
    res = bundle_mod.build(root, tmp_path / "d", build_id="1", git_sha="s", branch="main")
    assert res.manifest["versioned"][0]["should_execute"] == "${env}==PRD"


def test_promote_check_below_baseline_counts_as_delivered():
    rows = [{"category": "", "version": "2026.10.14.0000", "type": "BASELINE", "state": "Baseline"},
            {"category": "Repeatable", "description": "30 SALES  v open orders", "state": "Success"}]
    assert promote_mod.judge("PRD", _info(rows), _manifest()).ok


def test_promote_check_cli(tmp_path):
    (tmp_path / "m.json").write_text(json.dumps(_manifest()))
    (tmp_path / "i.json").write_text(json.dumps({"error": {"message": "boom"}}))
    assert fw.main(["--root", str(KIT), "promote-check", "--runway", "DEV", "--bundle-manifest",
                    str(tmp_path / "m.json")]) == 0
    assert fw.main(["--root", str(KIT), "promote-check", "--runway", "PRD", "--bundle-manifest",
                    str(tmp_path / "m.json")]) == 1
    assert fw.main(["--root", str(KIT), "promote-check", "--runway", "PRD", "--info", f"MGM={tmp_path / 'i.json'}",
                    "--bundle-manifest", str(tmp_path / "m.json")]) == 1


# --------------------------------------------------------------------------- drift + rehearsal

def test_drift_sql_targets_the_runway_and_history_table():
    kit = manifest_mod.load_kit(KIT)
    sql = drift_mod.drift_sql(kit, kit.runway("MGM"))
    assert 'ALFA_EDW_MGM.FLYWAY."flyway_schema_history"' in sql
    assert "ALFA_EDW_MGM.INFORMATION_SCHEMA.TABLES" in sql and "LAST_DDL_BY" in sql


def test_drift_eval_policy(tmp_path):
    rows = [
        {"OBJECT_KIND": "TABLE", "SCHEMA_NAME": "SALES", "OBJECT_NAME": "ORDERS", "CHANGED_AT": "t",
         "CHANGED_BY": "JSMITH"},
        {"OBJECT_KIND": "TABLE", "SCHEMA_NAME": "SALES", "OBJECT_NAME": "TMP_LOAD", "CHANGED_BY": "JSMITH"},
        {"OBJECT_KIND": "TABLE", "SCHEMA_NAME": "FLYWAY", "OBJECT_NAME": "DEPLOY_LOG", "CHANGED_BY": "JSMITH"},
        {"OBJECT_KIND": "TABLE", "SCHEMA_NAME": "SALES", "OBJECT_NAME": "X", "CHANGED_BY": "SVC_EDW_FLYWAY_PRD"},
    ]
    p = tmp_path / "rows.json"
    p.write_text(json.dumps(rows))
    kit = manifest_mod.load_kit(KIT)
    out = drift_mod.evaluate(kit, kit.runway("PRD"), drift_mod.load_rows(p))
    assert [r.name for r in out] == ["ORDERS"]
    root = _mini_kit(tmp_path)
    (root / "runways.toml").write_text((root / "runways.toml").read_text().replace(
        'four_eyes = true\nticket_required = true\nrehearsal = true\ndrift_gate = "warn"',
        'four_eyes = true\nticket_required = true\nrehearsal = true\ndrift_gate = "fail"'))
    assert fw.main(["--root", str(root), "drift-eval", "--runway", "PRD", "--rows", str(p)]) == 1
    assert fw.main(["--root", str(root), "drift-eval", "--runway", "PRD", "--rows", str(p), "--ack"]) == 0
    assert fw.main(["--root", str(root), "drift-eval", "--runway", "SAN", "--rows", str(p)]) == 0
    (tmp_path / "empty.json").write_text("[]")
    assert fw.main(["--root", str(root), "drift-eval", "--runway", "PRD", "--rows", str(tmp_path / "empty.json")]) == 0


def test_rehearsal_sql_never_targets_a_real_runway():
    kit = manifest_mod.load_kit(KIT)
    create = drift_mod.rehearsal_sql(kit, kit.runway("PRD"), "create")
    assert "CREATE OR REPLACE DATABASE ALFA_EDW_RH_PRD CLONE ALFA_EDW_PRD" in create
    assert drift_mod.rehearsal_sql(kit, kit.runway("MGM"), "drop") == "DROP DATABASE IF EXISTS ALFA_EDW_RH_MGM;\n"
    with pytest.raises(drift_mod.DriftError):
        drift_mod.rehearsal_sql(kit, kit.runway("SAN"), "drop")


# --------------------------------------------------------------------------- scaffold + env

def test_scaffold_files_pass_the_guard(tmp_path):
    root = _mini_kit(tmp_path)
    at = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
    v = scaffold_mod.new_versioned(root, "sales", "create_orders", at=at, undo=True)
    assert [p.name for p in v] == ["V2026.10.13.0900__sales_create_orders.sql", "U2026.10.13.0900__sales_create_orders.sql"]
    v2 = scaffold_mod.new_versioned(root, "SALES", "sales_more", at=at)
    assert v2[0].name == "V2026.10.13.0900.1__sales_more.sql"
    for layer, obj in ((20, "fn_x"), (30, "v_x"), (50, "sp_x"), (70, "s_x"), (80, "task_x"), (85, "alert_x"),
                       (90, "grants")):
        scaffold_mod.new_repeatable(root, "SALES", layer, obj)
    for p in root.rglob("*.sql"):  # templates carry <angle> slots for the author; make them parseable SQL
        p.write_text(re.sub(r"<[A-Za-z_ -]+>", "X", p.read_text()).replace("SALES.X", "SALES.T"))
    errors = [f.render() for f in _run(root) if f.severity == "error"]
    assert errors == []
    with pytest.raises(scaffold_mod.ScaffoldError):
        scaffold_mod.new_repeatable(root, "SALES", 30, "v_x")


def test_env_command_formats(capsys):
    assert fw.main(["--root", str(KIT), "env", "--runway", "prd", "--format", "json"]) == 0
    s = json.loads(capsys.readouterr().out)
    assert s["EDW_LOCK"] == "ALFA_EDW_PRD" and s["FLYWAY_RH_DB"] == "ALFA_EDW_RH_PRD"
    assert s["FLYWAY_SF_ROLE"] == "EDW_PRD_DEPLOYER" and s["EDW_FOUR_EYES"] == "true"
    # Flyway reads FLYWAY_<SETTING> env vars as configuration (ConfigUtils.convertKey: FLYWAY_TARGET -> target,
    # FLYWAY_ENVIRONMENT, FLYWAY_BASELINE_VERSION ...): pipeline keys are EDW_*, and the only FLYWAY_* names are the
    # connection variables flyway.toml resolves, none of which is a Flyway setting.
    assert {k for k in s if not k.startswith("EDW_")} == {"FLYWAY_SF_USER", "FLYWAY_SF_ROLE", "FLYWAY_SF_WAREHOUSE",
                                                          "FLYWAY_RH_DB"}
    assert fw.main(["--root", str(KIT), "env", "--runway", "SAN", "--auditor"]) == 0
    out = capsys.readouterr().out
    assert "export FLYWAY_SF_ROLE=EDW_FLYWAY_AUDITOR" in out
    assert fw.main(["--root", str(KIT), "env", "--runway", "XYZ"]) == 2


# --------------------------------------------------------------------------- Jenkins + docs consistency

def _jenkinsfiles() -> list[Path]:
    return sorted((KIT / "jenkins").glob("Jenkinsfile*"))

