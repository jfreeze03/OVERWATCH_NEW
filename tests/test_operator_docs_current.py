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
    assert "docs/FULL_REBUILD.md steps 0 and 7b" in read("snowflake/rebuild/README.md")


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
