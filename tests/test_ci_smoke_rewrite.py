"""The opt-in snowflake-smoke can only touch its throwaway clone (round 2, R2-005).

Its old rewrite only swapped DBA_MAINT_DB for the clone's name, and its fail-closed guard only grepped
for DBA_MAINT_DB. Account-level objects are never cloned, so the replay still ran V002's CREATE / ALTER
WAREHOUSE WH_ALFA_ADMIN (timeout back to 300 s) and the 30-credit SUSPEND resource monitor against
production, the V006-V008 grants to the retired roles (a guaranteed stop), task definitions on
WH_ALFA_ADMIN, RESUMEs of cloned task roots whose bodies still CALL the production procedures, and
V158's EXECUTE TASK. .github/scripts/snowflake_smoke_rewrite.py now neutralizes those statements, keeps
every clone task suspended and fails closed on anything account-level that survives; ci.yml runs it,
enforces the CI role (secondary roles off, no inherited admin role) and switches delivery off in the
clone before the replay.

v4.608 holistic #11/#12: the guard was a case-sensitive denylist that read notifier CALLs at top level
only. It is now an ALLOWLIST of the top-level statement kinds the real chain uses (proven by replaying
every migration through it), case-insensitive, with GRANT/REVOKE, SYSTEM$ functions, account-object and
database DDL, and DDL / writes / CALLs / USE naming another database refused at any depth, and the
notifier-CALL ban reaching into $$ bodies and dynamic-SQL strings. Step 0 also turns V164's escalation
email off in the clone, and ci.yml names the real send guarantee (suspended tasks + the CALL ban).
"""

from __future__ import annotations

import importlib.util
import re

import pytest

from tests._source import ROOT, read

_CLONE, _WH = "OVERWATCH_CI_SMOKE_42", "WH_CI_SMOKE"


def _mod():
    spec = importlib.util.spec_from_file_location(
        "snowflake_smoke_rewrite", ROOT / ".github" / "scripts" / "snowflake_smoke_rewrite.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rewritten() -> dict[str, tuple[str, str, list[str]]]:
    mod = _mod()
    out = {}
    for src, rel in mod.sources():
        text = src.read_text(encoding="utf-8")
        new, skipped = mod.rewrite(text, _CLONE, _WH)
        out[rel] = (text, new, skipped)
    return out


def test_the_rewritten_replay_carries_nothing_account_level():
    mod = _mod()
    copies = _rewritten()
    assert len(copies) >= 167 and "validate.sql" in copies and "task_audit.sql" in copies
    for rel, (_src, new, _skipped) in copies.items():
        assert mod.violations(new, _CLONE) == [], rel


def test_the_old_identifier_only_rewrite_fails_the_guard():
    mod = _mod()
    found = {}
    for name in ("V002__facts.sql", "V006__pipeline_sla.sql", "V045__task_monitoring_restored.sql",
                 "V158__operator_backup_generations.sql", "V004__alerts.sql"):
        old = re.sub(r"\bDBA_MAINT_DB\b", _CLONE, read(f"snowflake/migrations/{name}"))
        found[name] = {what for _line, what, _snippet in mod.violations(old, _CLONE)}
    assert {"warehouse DDL", "resource monitor", "production warehouse"} <= found["V002__facts.sql"]
    assert "grant to a retired role" in found["V006__pipeline_sla.sql"]
    assert "resource monitor" in found["V045__task_monitoring_restored.sql"]
    assert "task or alert start" in found["V158__operator_backup_generations.sql"]
    assert "task or alert start" in found["V004__alerts.sql"]


def test_only_the_account_level_statements_are_skipped():
    mod = _mod()
    copies = _rewritten()
    skipped = {rel: why for rel, (_s, _n, why) in copies.items() if why}
    assert sorted(skipped) == ["migrations/V002__facts.sql", "migrations/V006__pipeline_sla.sql",
                               "migrations/V007__automation.sql", "migrations/V008__chargeback.sql",
                               "migrations/V045__task_monitoring_restored.sql",
                               "migrations/V158__operator_backup_generations.sql"]
    assert len(skipped["migrations/V002__facts.sql"]) == 4      # CREATE + 2 ALTER WAREHOUSE + monitor
    for rel, (src, new, _why) in copies.items():
        # same line count; a line either matches the source after the substitutions or is a skipped one
        expected = mod._PROD_DB.sub(_CLONE, src)
        expected = mod._TASK_WAREHOUSE.sub(lambda m: f"WAREHOUSE{m.group(1)}={m.group(2)}{_WH}", expected)
        expected = mod._DEPENDENTS_ENABLE.sub("TO_VARCHAR(", mod._RESUME_TASK.sub(r"\1SUSPEND", expected))
        got, want = new.split("\n"), expected.split("\n")
        assert len(got) == len(want), rel
        for g, w in zip(got, want, strict=True):
            assert g == w or g == mod.SKIP_MARK + w, (rel, g)
    v002 = copies["migrations/V002__facts.sql"][1]
    code = [ln for ln in v002.splitlines() if not ln.lstrip().startswith("--")]
    assert not [ln for ln in code if re.search(r"WAREHOUSE WH_ALFA_ADMIN|RESOURCE MONITOR|RESOURCE_MONITOR =", ln)]
    v158 = copies["migrations/V158__operator_backup_generations.sql"][1]
    assert f"{mod.SKIP_MARK}EXECUTE TASK {_CLONE}.OVERWATCH.TASK_BACKUP_OPERATOR;" in v158


def test_every_clone_task_stays_suspended_on_the_ci_warehouse():
    mod = _mod()
    copies = _rewritten()
    resume = re.compile(r"\bALTER\s+TASK\b[^;]*\bRESUME\b", re.I)
    # read in code: proc message strings such as '... ALTER TASK ' || '... RESUME' are text, not statements
    assert len([rel for rel, (src, _n, _w) in copies.items() if resume.search(mod.code_view(src))]) >= 30
    resumed = {rel for rel, (_s, new, _w) in copies.items() if resume.search(mod.code_view(new))}
    assert not resumed, sorted(resumed)
    assert not [rel for rel, (_s, new, _w) in copies.items()
                if "SYSTEM$TASK_DEPENDENTS_ENABLE" in mod.code_view(new)]
    task_wh = {m.group(1) for _s, new, _w in copies.values()
               for m in re.finditer(r"^\s*WAREHOUSE\s*=\s*(\w+)", new, re.M)}
    assert task_wh == {_WH}
    # the RESUME inside V070's scripting block became a SUSPEND, so the block still compiles
    v070 = copies["migrations/V070__delivery_routing_teams_only.sql"][1]
    assert f"ALTER TASK IF EXISTS {_CLONE}.OVERWATCH.TASK_ALERT_NOTIFY SUSPEND;" in v070


def test_the_guard_reads_code_not_comments_or_strings():
    mod = _mod()
    assert mod.violations("-- ALTER WAREHOUSE X SET RESOURCE_MONITOR = Y;\nSELECT 1;\n", _CLONE) == []
    assert mod.violations("SELECT 'EXECUTE TASK X; ALTER ACCOUNT SET A = 1';\n", _CLONE) == []
    nested = "EXECUTE IMMEDIATE $$\nBEGIN\n    ALTER WAREHOUSE W SET WAREHOUSE_SIZE = XLARGE;\nEND;\n$$;\n"
    assert [what for _l, what, _s in mod.violations(nested, _CLONE)] == ["warehouse DDL"]
    assert "notification send" in _whats(mod, f"CALL {_CLONE}.OVERWATCH.SP_NOTIFY_WEBHOOK();\n")
    assert "session role or warehouse switch" in _whats(mod, "USE ROLE ACCOUNTADMIN;\n")
    assert "integration DDL" in _whats(mod, "CREATE OR REPLACE NOTIFICATION INTEGRATION N TYPE = EMAIL;\n")
    assert "account or user change" in _whats(mod, "ALTER ACCOUNT SET TIMEZONE = 'UTC';\n")
    assert "production database" in _whats(mod, "SELECT * FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;\n")


def _whats(mod, sql: str) -> set[str]:
    return {what for _line, what, _snippet in mod.violations(sql, _CLONE)}


def _nested(sql: str) -> str:
    return f"EXECUTE IMMEDIATE $$\nBEGIN\n    {sql}\nEND;\n$$;\n"


# Holistic #11: the notifier-CALL ban read top-level statements only, where $$ bodies are blanked, so a
# CALL nested in an anonymous block, a procedure body or a dynamic-SQL string passed it.
@pytest.mark.parametrize("sql", [
    _nested(f"CALL {_CLONE}.OVERWATCH.SP_NOTIFY_WEBHOOK();"),
    _nested(f"CALL {_CLONE}.OVERWATCH.SP_DAILY_DIGEST();"),
    _nested(f"EXECUTE IMMEDIATE 'CALL {_CLONE}.OVERWATCH.SP_NOTIFY_WEBHOOK()';"),
    f"CREATE OR REPLACE PROCEDURE {_CLONE}.OVERWATCH.SP_ALERT_CYCLE()\nRETURNS VARCHAR\nLANGUAGE SQL\nAS\n$$\n"
    f"BEGIN\n    CALL {_CLONE}.OVERWATCH.SP_NOTIFY_WEBHOOK();\n    RETURN 'ok';\nEND;\n$$;\n",
    f"call {_CLONE.lower()}.overwatch.sp_daily_digest();\n",
    "SELECT SYSTEM$SEND_SNOWFLAKE_NOTIFICATION('x', 'y');\n",
    _nested("CALL SYSTEM$SEND_EMAIL('OVERWATCH_EMAIL', 'ops@example.com', 's', 'b');"),
    # review of cfa3cd9e: a comment between two tokens is whitespace to Snowflake, and these passed the
    # ban (the first three regressed from the base, which matched the comment-blanked statement)
    f"CALL/**/{_CLONE}.OVERWATCH.SP_NOTIFY_WEBHOOK();\n",
    f"CALL -- c\n {_CLONE}.OVERWATCH.SP_DAILY_DIGEST();\n",
    _nested(f"CALL /* c */ {_CLONE}.OVERWATCH.SP_DAILY_DIGEST();"),
    f"CALL {_CLONE}.OVERWATCH/**/.SP_NOTIFY_WEBHOOK();\n",
    "SELECT SYSTEM$SEND_SNOWFLAKE_NOTIFICATION/**/('x','y');\n",
    # ... and inside a dynamic-SQL string, read as the SQL it runs
    _nested(f"EXECUTE IMMEDIATE 'CALL/**/{_CLONE}.OVERWATCH.SP_NOTIFY_WEBHOOK()';"),
    _nested(f"LET s VARCHAR := 'CALL /* c */ {_CLONE}.OVERWATCH.SP_DAILY_DIGEST()'; EXECUTE IMMEDIATE :s;"),
])
def test_a_notifier_call_or_send_anywhere_outside_a_task_body_fails_closed(sql):
    assert "notification send" in _whats(_mod(), sql)


def test_the_notifier_task_bodies_and_definitions_still_replay():
    mod = _mod()
    # the two real task bodies (V007, V018) are top-level `AS CALL` text: the tasks stay suspended
    task = (f"CREATE TASK IF NOT EXISTS {_CLONE}.OVERWATCH.TASK_ALERT_NOTIFY\n    WAREHOUSE = {_WH}\n"
            f"AS\n    CALL {_CLONE}.OVERWATCH.SP_NOTIFY_WEBHOOK();\n")
    assert mod.violations(task, _CLONE) == []
    # the notifier's own definition may hold the send primitive; nothing can CALL it
    proc = (f"CREATE OR REPLACE PROCEDURE {_CLONE}.OVERWATCH.SP_NOTIFY_WEBHOOK()\nRETURNS VARCHAR\nLANGUAGE SQL\n"
            "AS\n$$\nBEGIN\n    CALL SYSTEM$SEND_SNOWFLAKE_NOTIFICATION('m', 'i');\n    RETURN 'ok';\nEND;\n$$;\n")
    assert mod.violations(proc, _CLONE) == []
    other = proc.replace("SP_NOTIFY_WEBHOOK()", "SP_SOMETHING_ELSE()")
    assert "notification send" in _whats(mod, other)
    # prose that names the primitive (no call) is data; a task body inside a $$ block is not exempt
    assert mod.violations("SELECT 'the notifier uses SYSTEM$SEND_SNOWFLAKE_NOTIFICATION';\n", _CLONE) == []
    assert "notification send" in _whats(mod, task.replace(
        f"CALL {_CLONE}.OVERWATCH.SP_NOTIFY_WEBHOOK();\n",
        f"EXECUTE IMMEDIATE $$ CALL {_CLONE}.OVERWATCH.SP_NOTIFY_WEBHOOK() $$;\n"))


# Holistic #12: the account-level guard was a case-sensitive denylist, so these all passed it.
_ACCOUNT_LEVEL = [
    ("GRANT CREATE DATABASE ON ACCOUNT TO ROLE OVERWATCH_CI_SMOKE;", "grant or revoke"),
    ("GRANT USAGE ON INTEGRATION OVERWATCH_EMAIL TO ROLE SNOW_SYSADMINS;", "grant or revoke"),
    ("GRANT ROLE SOME_ADMIN TO ROLE SNOW_SYSADMINS;", "grant or revoke"),
    ("GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE TO ROLE SNOW_SYSADMINS;", "grant or revoke"),
    ("revoke usage on warehouse wh_x from role snow_sysadmins;", "grant or revoke"),
    ("CALL SYSTEM$SEND_EMAIL('OVERWATCH_EMAIL', 'ops@example.com', 's', 'b');", "system function"),
    ("SELECT SYSTEM$ABORT_SESSION(1);", "system function"),
    ("ALTER NETWORK POLICY P SET ALLOWED_IP_LIST = ('0.0.0.0/0');", "account-level object DDL"),
    ("CREATE SHARE S;", "account-level object DDL"),
    ("DROP DATABASE ALFA_EDW_PRD;", "database DDL or schema move"),
    ("ALTER SCHEMA OVERWATCH SWAP WITH OVERWATCH_BAK;", "database DDL or schema move"),
    ("CREATE DATABASE IF NOT EXISTS ALFA_SCRATCH;", "name outside the clone"),
    ("CREATE SCHEMA IF NOT EXISTS ALFA_EDW_PRD.SCRATCH;", "name outside the clone"),
    ("DROP TABLE ALFA_EDW_PRD.PUBLIC.CONTROL_STATUS;", "name outside the clone"),
    ("insert into alfa_edw_prd.public.control_status values (1);", "name outside the clone"),
    ('DELETE FROM "ALFA_EDW_PRD".PUBLIC.T;', "name outside the clone"),
    ("CALL ALFA_EDW_PRD.PUBLIC.SP_RELOAD();", "name outside the clone"),
    ("USE DATABASE ALFA_EDW_PRD;", "name outside the clone"),
    (f"ALTER TABLE {_CLONE}.OVERWATCH.T RENAME TO ALFA_EDW_PRD.PUBLIC.T;", "name outside the clone"),
    ("DELETE FROM IDENTIFIER('ALFA_EDW_PRD.PUBLIC.T');", "IDENTIFIER() of a literal name"),
    ("CREATE STAGE S URL = 's3://bucket/x';", "external stage or data unload"),
    ("CREATE OR REPLACE PROCEDURE P() RETURNS VARCHAR LANGUAGE PYTHON EXTERNAL_ACCESS_INTEGRATIONS = (X) "
     "AS 'x';", "integration reference"),
    # review of cfa3cd9e: only ALTER ACCOUNT / ALTER USER were matched, so these passed in a $$ body
    ("CREATE USER CI_BACKDOOR PASSWORD = 'x';", "account or user change"),
    ("CREATE OR REPLACE USER X;", "account or user change"),
    ("DROP USER SOMEONE;", "account or user change"),
    ("DROP ACCOUNT A;", "account or user change"),
]


@pytest.mark.parametrize(("sql", "what"), _ACCOUNT_LEVEL)
def test_account_level_sql_fails_closed_at_top_level(sql, what):
    assert what in _whats(_mod(), sql + "\n")


@pytest.mark.parametrize(("sql", "what"), _ACCOUNT_LEVEL)
def test_account_level_sql_fails_closed_nested_in_a_dollar_body(sql, what):
    assert what in _whats(_mod(), _nested(sql))


def _proc_executing(literal: str) -> str:
    """A procedure whose body hands ``literal`` (already quoted) to EXECUTE IMMEDIATE, then a CALL of it."""
    return (f"CREATE OR REPLACE PROCEDURE {_CLONE}.OVERWATCH.SP_X()\nRETURNS VARCHAR\nLANGUAGE SQL\nAS\n$$\n"
            f"BEGIN\n    EXECUTE IMMEDIATE {literal};\n    RETURN 'ok';\nEND;\n$$;\n"
            f"CALL {_CLONE}.OVERWATCH.SP_X();\n")


def _quoted(sql: str) -> str:
    return "'" + sql.rstrip(";").replace("'", "''") + "'"


# Review of cfa3cd9e: the account-level checks read code with '...' strings blanked, so a literal handed
# straight to EXECUTE IMMEDIATE in a procedure body (the shape the chain uses in 12 places) passed them.
@pytest.mark.parametrize(("sql", "what"), _ACCOUNT_LEVEL)
def test_account_level_sql_fails_closed_as_a_literal_handed_to_execute_immediate(sql, what):
    assert what in _whats(_mod(), _proc_executing(_quoted(sql)))


@pytest.mark.parametrize(("literal", "what"), [
    ("'GRANT USAGE ON INTEGRATION OVERWATCH_EMAIL TO ROLE PUBLIC'", "grant or revoke"),
    ("'DROP TABLE ALFA_EDW_PRD.PUBLIC.CONTROL_STATUS'", "name outside the clone"),
    ("'SELECT SYSTEM$ABORT_SESSION(1)'", "system function"),
    ("'ALTER WAREHOUSE WH_X SET WAREHOUSE_SIZE = XLARGE'", "warehouse DDL"),
    ("/* c */ 'GRANT ROLE SOME_ADMIN TO ROLE PUBLIC'", "grant or revoke"),
    ("'EXECUTE IMMEDIATE ''GRANT ROLE SOME_ADMIN TO ROLE PUBLIC'''", "grant or revoke"),
    ("'DROP/**/USER SOMEONE'", "account or user change"),
    ("'GRANT USAGE ON INTEGRATION ' || :name || ' TO ROLE PUBLIC'", "grant or revoke"),
])
def test_a_literal_handed_to_execute_immediate_is_read_as_the_sql_it_runs(literal, what):
    mod = _mod()
    found = [(line, w) for line, w, _s in mod.violations(_proc_executing(literal), _CLONE)]
    assert (7, what) in found, found        # reported on the EXECUTE IMMEDIATE line of the body


def test_prose_strings_and_the_chains_dynamic_sql_shapes_stay_clean():
    mod = _mod()
    for sql in (
        "INSERT INTO T (MSG) VALUES ('GRANT USAGE ON INTEGRATION X TO ROLE Y, then DROP USER Z');\n",
        _nested("LET msg VARCHAR := 'ALTER WAREHOUSE W SET WAREHOUSE_SIZE = XLARGE is the fix';"),
        _proc_executing("'SELECT ''GRANT ROLE X TO ROLE Y'' AS NOTE'"),
        _proc_executing(f"'CREATE OR REPLACE TRANSIENT TABLE {_CLONE}.OVERWATCH.' || :tname || '_BAK CLONE "
                        f"{_CLONE}.OVERWATCH.' || :tname"),
        _proc_executing("'SELECT 1 FROM ' || :cname || ' LIMIT 1'"),
        _proc_executing(f"'DROP TABLE IF EXISTS {_CLONE}.OVERWATCH.T -- it''s a comment'"),
    ):
        assert mod.violations(sql, _CLONE) == [], sql


@pytest.mark.parametrize("sql", [
    "TRUNCATE TABLE T;",
    "CREATE OR REPLACE ALERT A WAREHOUSE = W SCHEDULE = '1 MINUTE' IF (EXISTS (SELECT 1)) THEN SELECT 1;",
    "CREATE PIPE P AS SELECT 1;",
    "ALTER SESSION SET QUERY_TAG = 'x';",
    "BEGIN\n    SELECT 1;\nEND;",
    "UNDROP TABLE T;",
    "COMMENT ON TABLE T IS 'x';",
    "CREATE DATABASE ALFA_SCRATCH;",
    f"CREATE OR REPLACE DATABASE {_CLONE};",
    "DROP SCHEMA OVERWATCH;",
])
def test_a_top_level_statement_kind_the_chain_never_uses_fails_closed(sql):
    assert "statement kind not on the allowlist" in _whats(_mod(), sql + "\n")


def test_lowercase_production_identifiers_are_rewritten_and_never_survive():
    mod = _mod()
    for sql in ("insert into dba_maint_db.overwatch.settings (key, value) values ('k', 'v');\n",
                "USE DATABASE Dba_Maint_Db;\n",
                _nested("DELETE FROM dba_maint_db.overwatch.alert_events;")):
        assert "production database" in _whats(mod, sql), sql
        new, _skipped = mod.rewrite(sql, _CLONE, _WH)
        assert "dba_maint_db" not in new.lower() and _CLONE in new, new
        assert mod.violations(new, _CLONE) == [], new
    job = _smoke_job(read(".github/workflows/ci.yml"))
    assert job.count("grep -rIil 'DBA_MAINT_DB' \"$REWRITE_DIR\"") == 2
    assert "grep -rIl 'DBA_MAINT_DB'" not in job


def test_every_real_migration_replays_in_order_with_nothing_legitimate_refused():
    """The allowlist is the chain's own statement kinds: applied to every real migration (in the order
    the smoke applies them), validate.sql and task_audit.sql, nothing is refused, and every allowed kind
    is one the chain really uses (so the list cannot quietly grow past what the chain needs)."""
    mod = _mod()
    srcs = mod.sources()
    migrations = [rel for _p, rel in srcs if rel.startswith("migrations/")]
    assert len(migrations) >= 165
    assert migrations == sorted(migrations, key=lambda r: int(re.match(r"migrations/V(\d+)__", r).group(1)))
    used: set[str] = set()
    for src, rel in srcs:
        new, _skipped = mod.rewrite(src.read_text(encoding="utf-8"), _CLONE, _WH)
        assert mod.violations(new, _CLONE) == [], rel
        for _start, _end, code in mod.statements(new):
            kind = mod.top_level_kind(code)
            assert kind is not None, (rel, code[:100])
            used.add(kind)
    assert used == {why for why, _rx in mod._TOP_LEVEL_KINDS}


def test_a_statement_it_cannot_skip_cleanly_fails_closed():
    mod = _mod()
    with pytest.raises(ValueError):
        mod.neutralize("SELECT 1; ALTER WAREHOUSE W SET AUTO_SUSPEND = 60;\n")
    with pytest.raises(ValueError):
        mod.neutralize("ALTER WAREHOUSE W SET AUTO_SUSPEND = 60; SELECT 1;\n")


def test_main_writes_nothing_when_a_migration_is_unsafe(tmp_path):
    mod = _mod()
    root = tmp_path / "repo"
    (root / "snowflake" / "migrations").mkdir(parents=True)
    (root / "snowflake" / "migrations" / "V001__x.sql").write_text(
        "CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.T (A INT);\nALTER ACCOUNT SET TIMEZONE = 'UTC';\n",
        encoding="utf-8")
    out = tmp_path / "out"
    assert mod.main(["--out", str(out), "--clone-db", _CLONE, "--ci-warehouse", _WH, "--root", str(root)]) == 1
    assert not out.exists()
    (root / "snowflake" / "migrations" / "V001__x.sql").write_text(
        "CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.T (A INT);\n"
        "grant usage on integration overwatch_email to role snow_sysadmins;\n", encoding="utf-8")
    assert mod.main(["--out", str(out), "--clone-db", _CLONE, "--ci-warehouse", _WH, "--root", str(root)]) == 1
    assert not out.exists()
    (root / "snowflake" / "migrations" / "V001__x.sql").write_text(
        "CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.T (A INT);\n", encoding="utf-8")
    assert mod.main(["--out", str(out), "--clone-db", _CLONE, "--ci-warehouse", _WH, "--root", str(root)]) == 0
    written = (out / "migrations" / "V001__x.sql").read_text(encoding="utf-8")
    assert written.startswith(mod.header(_CLONE)) and "USE SECONDARY ROLES NONE;" in written
    assert f"{_CLONE}.OVERWATCH.T" in written and "DBA_MAINT_DB" not in written


@pytest.mark.parametrize(("clone", "wh"), [("DBA_MAINT_DB", _WH), (_CLONE, "WH_ALFA_ADMIN"),
                                           ("X; DROP DATABASE DBA_MAINT_DB", _WH), (_CLONE, "WH CI")])
def test_main_refuses_production_or_unsafe_names(tmp_path, clone, wh):
    with pytest.raises(SystemExit):
        _mod().main(["--out", str(tmp_path / "o"), "--clone-db", clone, "--ci-warehouse", wh])


def _smoke_job(ci: str) -> str:
    return ci[ci.index("  snowflake-smoke:\n"):]


def test_ci_runs_the_rewrite_and_enforces_isolation_before_the_replay():
    ci = read(".github/workflows/ci.yml")
    job = _smoke_job(ci)
    assert "continue-on-error: true" in job                              # still non-gating
    assert ('python .github/scripts/snowflake_smoke_rewrite.py --out "$REWRITE_DIR" \\\n'
            '            --clone-db "$CLONE_DB" --ci-warehouse "$CI_WAREHOUSE"') in job
    assert "sed -i" not in job and "cp snowflake/migrations" not in job   # the identifier-only rewrite is gone
    rewrite = job.index("snowflake_smoke_rewrite.py")
    role_check = job.index("IS_ROLE_IN_SESSION('ACCOUNTADMIN')")
    clone = job.index('-q "CREATE DATABASE $CLONE_DB CLONE $SNOWFLAKE_DATABASE;"')
    routes_off = job.index("UPDATE $CLONE_DB.OVERWATCH.ALERT_ROUTES SET ENABLED = FALSE;")
    replay = job.index('for f in $(ls "$REWRITE_DIR"/migrations/V*.sql | sort); do')
    assert rewrite < role_check < clone < routes_off < replay
    check = job[role_check - 200:role_check + 400]
    assert "USE SECONDARY ROLES NONE;" in check
    for role in ("ACCOUNTADMIN", "SYSADMIN", "SECURITYADMIN", "SNOW_ACCOUNTADMINS", "SNOW_SYSADMINS"):
        assert f"IS_ROLE_IN_SESSION('{role}')" in check, role
    # the pass token is built by concatenation, so an echoed query text can never satisfy the grep
    (query,) = re.findall(r"-q \"(USE SECONDARY ROLES NONE; SELECT 'ROLE_CHECK_'[^\"]*)\"", job)
    assert "ROLE_CHECK_PASSED" not in query and "grep -q 'ROLE_CHECK_PASSED'" in job
    # every -q statement against the clone runs with secondary roles off
    assert all(q.startswith('"USE SECONDARY ROLES NONE; ') for q in re.findall(r'\bsql -q ("[^\n]*)', job))
    # no new secret and no permissions block: the fix narrows, it never widens
    assert sorted(set(re.findall(r"secrets\.(\w+)", ci))) == [
        "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_CI_ROLE", "SNOWFLAKE_CI_WAREHOUSE", "SNOWFLAKE_PASSWORD",
        "SNOWFLAKE_ROLE", "SNOWFLAKE_USER", "SNOWFLAKE_WAREHOUSE"]
    assert not re.search(r"^\s*permissions:", ci, re.M)
    assert "See ONE green workflow_dispatch run under that role" in job


def test_the_contract_names_the_real_send_guarantee_and_step0_turns_escalation_off():
    """Holistic #11: disabling the clone's routes stops the Teams legs only. V164's CRITICAL escalation
    emails SETTINGS.ESCALATE_EMAIL_INTEGRATION whatever the routes say, so the guarantee is the suspended
    tasks plus layer 1's notifier-CALL ban, and step 0 also switches escalation off in the clone."""
    ci = read(".github/workflows/ci.yml")
    job = _smoke_job(ci)
    contract = ci[ci.index("ISOLATION CONTRACT"):ci.index("  snowflake-smoke:\n")]
    flat = " ".join(line.strip().lstrip("#").strip() for line in contract.splitlines())
    assert "so nothing it runs can post through" not in flat
    assert "every clone task stays SUSPENDED" in flat and "EXECUTE TASK is refused" in flat
    assert "ban on any CALL of SP_NOTIFY_WEBHOOK or SP_DAILY_DIGEST" in flat
    assert "it stops only the Teams legs" in flat
    # review of cfa3cd9e: the contract says what the text check reads and what it leaves to layer 2
    assert "A comment between two tokens reads as whitespace" in flat
    assert "a '...' literal handed straight to EXECUTE IMMEDIATE" in flat
    assert "layer 1 reads only the literal EXECUTE IMMEDIATE is handed" in flat
    step0 = job[job.index("# 0. Delivery off IN THE CLONE"):job.index("# 1. Migrations in order")]
    (query,) = re.findall(r'sql -q ("[^\n]*")', step0)
    assert "UPDATE $CLONE_DB.OVERWATCH.ALERT_ROUTES SET ENABLED = FALSE;" in query
    assert ("MERGE INTO $CLONE_DB.OVERWATCH.SETTINGS t USING (SELECT * FROM VALUES "
            "('ESCALATE_AFTER_MIN', '0'), ('ESCALATE_EMAIL_INTEGRATION', '') AS s(KEY, VALUE)) s "
            "ON t.KEY = s.KEY WHEN MATCHED THEN UPDATE SET VALUE = s.VALUE "
            "WHEN NOT MATCHED THEN INSERT (KEY, VALUE) VALUES (s.KEY, s.VALUE);") in query
    # the replay keeps those clone values: V164 seeds them WHEN NOT MATCHED only, and nothing else writes them
    v164 = read("snowflake/migrations/V164__notify_actionable_lines_escalation.sql")
    seed = v164[v164.index("MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS"):]
    seed = seed[:seed.index(";")]
    assert "ESCALATE_AFTER_MIN" in seed and "WHEN MATCHED" not in seed
    writers = sorted(p.name for p in (ROOT / "snowflake" / "migrations").glob("V*.sql")
                     if "ESCALATE_AFTER_MIN" in p.read_text(encoding="utf-8"))
    assert writers == ["V164__notify_actionable_lines_escalation.sql"]
