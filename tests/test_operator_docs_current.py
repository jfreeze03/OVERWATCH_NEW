"""The operator runbooks stay derived from the code they describe (v4.607 cleanup, review R1-274..R1-352).

RUNBOOK §4 had drifted to 12 of the 32 live tasks and §12 had dropped ten seeded alert rules; FULL_REBUILD
stopped at V124 and never said that the teardown drops the opt-in email alerts, drill, ML forecast and
notification integrations; the manual deploy path skipped four app subpackages (the app would not start);
the email pre-flight blocked a resume on rows the alerts never send. Each lock below reads its truth from the
code or the migrations, so it holds across a new migration or release instead of pinning a tip.
"""

from __future__ import annotations

import re

from app.data.ops_sql import OVERWATCH_TASKS
from tests._source import ROOT, read


def _section(text: str, start: str, end: str) -> str:
    return text[text.index(start):text.index(end)]


def _first_cells(table_text: str) -> set[str]:
    return {line.split("|")[1].strip().strip("~").strip("`")
            for line in table_text.splitlines() if line.startswith("| ")}


def _current_proc(name: str) -> str:
    """A procedure's CURRENT body: the last migration that CREATE OR REPLACEs it."""
    pat = re.compile(r"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\." + name + r"\(")
    for mig in sorted((ROOT / "snowflake" / "migrations").glob("V[0-9]*__*.sql"), reverse=True):
        text = mig.read_text(encoding="utf-8")
        hits = list(pat.finditer(text))
        if hits:
            start = hits[-1].start()
            return text[start:text.index("\n$$;", start)]
    raise AssertionError(f"no migration defines {name}")


def _task_row(task: str) -> str:
    table = _section(read("RUNBOOK.md"), "## 4. Scheduled automation", "**Notes on the automation:**")
    return next(line for line in table.splitlines() if line.startswith(f"| {task} |"))


def test_runbook_task_table_is_the_live_task_set():
    """R1-346: one row per task the migrations leave live, plus the struck-through retired one."""
    rb = read("RUNBOOK.md")
    table = _section(rb, "## 4. Scheduled automation", "**Notes on the automation:**")
    rows = {cell for cell in _first_cells(table) if cell.startswith("TASK_")}
    assert rows - {"TASK_BACKUP_OPERATOR"} == set(OVERWATCH_TASKS)
    assert "| ~~TASK_BACKUP_OPERATOR~~ | retired V161 |" in table
    # V071 re-pointed these two behind the extract; the current sweep returns v3
    for task in ("TASK_REFRESH_EXEC_BOARD", "TASK_ALERT_SCAN"):
        row = next(line for line in table.splitlines() if line.startswith(f"| {task} |"))
        assert "after TASK_QH_EXTRACT (V071)" in row, row
    assert "SP_ANOMALY_SWEEP (v3)" in table and "(v2)" not in table


def test_runbook_task_rows_name_what_each_proc_writes():
    """d4 review: each Writes cell matches the proc's current definer -- no invented column, no
    missing mart, no missing rule."""
    # SP_CHANGE_ATTRIBUTION only SETs CHANGED_BY; CHANGE_SOURCE exists in no migration (it is the
    # change-impact read's derived column, RUNBOOK §21).
    attribution = _task_row("TASK_CHANGE_ATTRIBUTION")
    set_cols = set(re.findall(r"\bSET\s+(\w+)\s*=", _current_proc("SP_CHANGE_ATTRIBUTION")))
    assert set_cols == {"CHANGED_BY"}, set_cols
    assert "WAREHOUSE_CHANGE_REGISTRY.CHANGED_BY" in attribution
    assert not any("CHANGE_SOURCE" in p.read_text(encoding="utf-8")
                   for p in (ROOT / "snowflake" / "migrations").glob("V[0-9]*__*.sql"))
    assert "CHANGE_SOURCE is derived from it on read" in attribution
    # every proc the extract CALLs (SP_LOAD_CLOUD_SVC_MART fills MART_CLOUD_SVC_DAILY) is named
    extract = _task_row("TASK_QH_EXTRACT")
    called = re.findall(r"CALL DBA_MAINT_DB\.OVERWATCH\.(\w+)\(", _current_proc("SP_LOAD_QH_EXTRACT"))
    assert "SP_LOAD_CLOUD_SVC_MART" in called
    assert "MART_CLOUD_SVC_DAILY" in extract
    for proc in called:
        assert proc in extract, proc
    # every rule the sweep raises itself, and every scanner it CALLs plus that scanner's rule
    sweep_row = _task_row("TASK_ANOMALY_SWEEP")
    sweep = _current_proc("SP_ANOMALY_SWEEP")
    rules = set(re.findall(r"\bRULE_ID = '([A-Z][A-Z0-9_]+)'", sweep))
    scanners = re.findall(r"CALL DBA_MAINT_DB\.OVERWATCH\.(SP_SCAN_\w+)\(", sweep)
    assert {"PIPE_DT_FAILURES", "COST_ORG_ACCOUNT_CREEP", "PIPE_VOLUME_DROP"} <= rules
    assert scanners
    for scanner in scanners:
        assert scanner in sweep_row, scanner
        rules |= set(re.findall(r"\bRULE_ID = '([A-Z][A-Z0-9_]+)'", _current_proc(scanner)))
    missing = sorted(rule for rule in rules if rule not in sweep_row)
    assert not missing, missing


def _seeded_rule_ids() -> set[str]:
    """Rule ids the migrations seed through the two house MERGE shapes."""
    ids: set[str] = set()
    for path in sorted((ROOT / "snowflake" / "migrations").glob("V[0-9]*__*.sql")):
        text = path.read_text(encoding="utf-8")
        ids.update(re.findall(r"SELECT\s+'([A-Z][A-Z0-9_]+)'\s+AS\s+RULE_ID", text))
        ids.update(m.group(1) for m in re.finditer(
            r"\(\s*'([A-Z][A-Z0-9_]+)'\s*,\s*'(?:COST|PERFORMANCE|PIPELINE|SECURITY|PLATFORM|WAREHOUSE)'", text))
    return ids


def test_runbook_rule_table_covers_every_seeded_rule():
    """R1-345: §15 sends a missed alert to 'recurrence in §12', so every seeded rule needs a row there."""
    rb = read("RUNBOOK.md")
    rows = _first_cells(_section(rb, "## 12. Alert engine reference", "## 13. Object inventory"))
    seeded = _seeded_rule_ids()
    assert {"COST_AI_CREEP", "SEC_NEW_ADMIN_NETWORK", "DQ_RECON_ERROR", "WH_CHANGE_REGRESSION"} <= seeded
    assert not seeded - rows, sorted(seeded - rows)
    assert "SEC_POSTURE_<METRIC>" in rows          # operator-created posture monitors, arm [21]


def test_runbook_scores_match_the_score_modules():
    """R1-341 / R1-339: no resource-monitor deduction or hard cap (owner 2026-07-13); real platform states."""
    from app.logic.governance import DEFAULT_GOV_WEIGHTS

    rb = read("RUNBOOK.md")
    scores = " ".join(_section(rb, "## 6. Calculated scores", "## 7. Forecast engines").split())
    assert "without monitor" not in scores and "Weights fixed" not in scores
    assert "≥50 Degraded, else At risk" in scores and "Critical veto" in scores
    assert "Incomplete" in scores and "GOV_PTS_*" in scores
    assert len(DEFAULT_GOV_WEIGHTS) == 5                       # the five drivers the section lists
    assert "resource monitor with" not in rb                   # §20 no longer recommends one


def test_full_rebuild_is_tip_agnostic_and_restores_the_opt_ins():
    """R1-274 / R1-333 / R1-334."""
    fr = read("docs/FULL_REBUILD.md")
    assert re.search(r"V001\s*(?:\.\.|→)\s*V1\d\d|1\.\.1\d\d|all 1\d\d migrations|expects exactly", fr) is None
    assert "## 7b. Re-install the opt-in objects" in fr
    for needle in ("NATIVE_ALERT_*", "TASK_ALERT_DRILL", "FORECAST_ML_DAILY", "OVERWATCH_EMAIL",
                   "OVERWATCH_WEBHOOK_TEAMS", "SET ENABLED = TRUE"):
        assert needle in fr, needle
    # the generated bundle's README is not hand-edited (house law 6); it points at this runbook
    assert "The runbook is docs/FULL_REBUILD.md" in read("snowflake/rebuild/README.md")


def test_full_rebuild_restores_delivery_without_duplicates_or_lost_grants():
    """d4 review: re-enable only the routes that were live (a duplicate disabled on purpose stays off),
    re-grant the re-created integration, and say which Section B drops run live."""
    fr = read("docs/FULL_REBUILD.md")
    step0 = _section(fr, "## 0. Decide what survives", "## 1. Backups")
    assert re.search(r"SELECT ROUTE_ID, INTEGRATION_NAME FROM DBA_MAINT_DB\.OVERWATCH\.ALERT_ROUTES\s+WHERE ENABLED;",
                     step0)
    assert "SHOW GRANTS ON INTEGRATION OVERWATCH_WEBHOOK_TEAMS;" in step0
    step7b = _section(fr, "## 7b. Re-install the opt-in objects", "## 8. Prove the chain ticks")
    assert re.search(r"SET ENABLED = TRUE\s+WHERE ROUTE_ID IN \(", step7b)
    assert re.search(r"SET ENABLED = TRUE\s+WHERE INTEGRATION_NAME", fr) is None
    assert "GRANT USAGE ON INTEGRATION OVERWATCH_WEBHOOK_TEAMS TO ROLE SNOW_ACCOUNTADMINS;" in step7b
    assert "ALERT_DELIVERIES" in step7b and "route_send_failed" in step7b
    # every table drop that runs live in teardown Section B is named in step 2
    td = read("snowflake/teardown.sql")
    section_b = _section(td, "-- B. OPERATOR DATA", "-- C. SHARED INFRASTRUCTURE")
    live = re.findall(r"^DROP TABLE IF EXISTS DBA_MAINT_DB\.OVERWATCH\.(\w+);", section_b, re.M)
    assert "ALERT_DELIVERIES" in live
    step2 = _section(fr, "## 2. Teardown", "## 3. Migrations")
    missing = [table for table in live if table not in step2]
    assert not missing, missing


def test_manual_deploy_path_uploads_every_app_folder():
    """R1-322 / R1-340: PUT does not recurse, so every folder holding app files is listed."""
    dep = read("DEPLOYMENT.md")
    manual = _section(dep, "Manual path (no CLI", "`LIST @DBA_MAINT_DB.OVERWATCH.OVERWATCH_STAGE`")
    folders = sorted({p.parent.relative_to(ROOT).as_posix() for p in (ROOT / "app").rglob("*")
                      if p.is_file() and p.parent != ROOT / "app"
                      and not any(part == "__pycache__" or part.startswith(".") for part in p.parts)})
    assert "app/ui/pages/cost_parts" in folders
    missing = [f for f in folders if not re.search(rf"{re.escape(f)}(?![/\w])", manual)]
    assert not missing, missing
    assert "re-run snowflake/roles.sql" in manual


def test_email_preflight_matches_the_delivery_alert():
    """R1-352: pre-flight (2) checks exactly the NotifyWebhook types NATIVE_ALERT_DELIVERY_FAILING emails."""
    tpl = read("snowflake/native_alert_templates.sql")
    alert = tpl[tpl.index("CREATE OR REPLACE ALERT DBA_MAINT_DB.OVERWATCH.NATIVE_ALERT_DELIVERY_FAILING"):]
    emailed = re.findall(r"'(\w+)'", re.search(r"ERROR_TYPE IN \(([^)]*)\)", alert).group(1))
    assert emailed == ["route_send_failed", "undelivered_expired", "webhook_run_failed"]
    doc = read("docs/EMAIL_RECIPIENT_RUNBOOK.md")
    preflight = doc[doc.index("-- (2)"):doc.index("-- (3)")]
    assert "OR PAGE = 'NotifyWebhook')" not in preflight
    checked = re.search(r"PAGE = 'NotifyWebhook'\s+AND ERROR_TYPE IN \(([^)]*)\)", preflight)
    assert checked and re.findall(r"'(\w+)'", checked.group(1)) == emailed


def _definer_versions(name: str) -> list[int]:
    """Every migration version that CREATE OR REPLACEs the procedure, ascending (the last one is its current definer)."""
    pat = re.compile(r"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\." + name + r"\(")
    return sorted(int(re.match(r"V(\d+)__", mig.name).group(1))
                  for mig in (ROOT / "snowflake" / "migrations").glob("V[0-9]*__*.sql")
                  if pat.search(mig.read_text(encoding="utf-8")))


def test_webhook_setup_names_the_digest_current_definer():
    """V171 review: webhook_delivery.sql names SP_DAILY_DIGEST's current definer, and its never-re-run-V018 warning
    lists every later re-definition that a V018 re-run would undo (it went stale when V171 re-derived the digest)."""
    wd = read("snowflake/webhook_delivery.sql")
    versions = _definer_versions("SP_DAILY_DIGEST")
    assert versions[:2] == [7, 18] and len(versions) > 2
    assert f"(SP_DAILY_DIGEST, V{versions[-1]:03d})" in wd
    assert len(re.findall(r"\(SP_DAILY_DIGEST, V\d+\)", wd)) == 1
    undone = "/".join(f"V{v:03d}" for v in versions if v > 18)
    assert f" and undoes {undone}." in wd.replace("\n-- ", " ")
