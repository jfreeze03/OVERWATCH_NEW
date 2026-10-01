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
        assert mod.violations(new) == [], rel


def test_the_old_identifier_only_rewrite_fails_the_guard():
    mod = _mod()
    found = {}
    for name in ("V002__facts.sql", "V006__pipeline_sla.sql", "V045__task_monitoring_restored.sql",
                 "V158__operator_backup_generations.sql", "V004__alerts.sql"):
        old = re.sub(r"\bDBA_MAINT_DB\b", _CLONE, read(f"snowflake/migrations/{name}"))
        found[name] = {what for _line, what, _snippet in mod.violations(old)}
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
    assert mod.violations("-- ALTER WAREHOUSE X SET RESOURCE_MONITOR = Y;\nSELECT 1;\n") == []
    assert mod.violations("SELECT 'EXECUTE TASK X; ALTER ACCOUNT SET A = 1';\n") == []
    nested = "EXECUTE IMMEDIATE $$\nBEGIN\n    ALTER WAREHOUSE W SET WAREHOUSE_SIZE = XLARGE;\nEND;\n$$;\n"
    assert [what for _l, what, _s in mod.violations(nested)] == ["warehouse DDL"]
    assert mod.violations("CALL C.OVERWATCH.SP_NOTIFY_WEBHOOK();\n")[0][1] == "notification send"
    assert mod.violations("USE ROLE ACCOUNTADMIN;\n")[0][1] == "session role or warehouse switch"
    assert mod.violations("CREATE OR REPLACE NOTIFICATION INTEGRATION N TYPE = EMAIL;\n")[0][1] == "integration DDL"
    assert mod.violations("ALTER ACCOUNT SET TIMEZONE = 'UTC';\n")[0][1] == "account or user change"
    assert mod.violations("SELECT * FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;\n")[0][1] == "production database"


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
