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


def test_full_rebuild_redeploy_re_runs_the_streamlit_grants():
    """review 4.610.1 #3: step 4 runs roles.sql before step 7 redeploys, and the deploy (CREATE OR REPLACE
    STREAMLIT, no COPY GRANTS) drops the app's USAGE for all four access roles, so step 7 re-runs roles.sql's
    Streamlit block, as DEPLOYMENT.md §2/§3 do after every deploy, and checks its proof block's answer."""
    fr = read("docs/FULL_REBUILD.md")
    assert fr.index("## 4. Grants") < fr.index("## 7. Redeploy the app")
    step7 = " ".join(_section(fr, "## 7. Redeploy the app", "## 7b. Re-install the opt-in objects").split())
    assert "re-run roles.sql's Streamlit block (DEPLOYMENT.md §2)" in step7, step7
    assert "it must return 'Streamlit grants OK'" in step7, step7
    assert "drops the USAGE step 4 granted" in step7, step7
    dep = " ".join(read("DEPLOYMENT.md").split())
    assert "re-run roles.sql's Streamlit block" in dep and "must return 'Streamlit grants OK'" in dep


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
    # v4.609: RUNBOOK §12 names the same current definer (it still said V165 after V171 re-derived the digest)
    rb = read("RUNBOOK.md")
    assert f"(SP_DAILY_DIGEST, V{versions[-1]:03d})" in rb
    assert len(re.findall(r"\(SP_DAILY_DIGEST, V\d+\)", rb)) == 1


# -- v4.609 (V166-V172 wave): RUNBOOK and FULL_REBUILD say what the merged migrations and backfill do -----------

def _rule_row(rb: str, rule: str) -> str:
    return next(line for line in rb.splitlines() if line.startswith(f"| {rule} |"))


def test_runbook_rule_rows_track_the_v168_v169_scans():
    rb = read("RUNBOOK.md")
    assert "once per table per failure day" in _rule_row(rb, "PIPE_COPY_FAILURES")
    assert "stays suppressed until the next Central day" in _rule_row(rb, "PIPE_COPY_FAILURES")
    net = _rule_row(rb, "SEC_NEW_ADMIN_NETWORK")
    assert "first-seen Central day" in net and "V168" in net and "| once per user and IP |" not in net
    assert "previous complete Central day" in _rule_row(rb, "COST_EGRESS_SPIKE")
    recon = _rule_row(rb, "DQ_RECON_ERROR")
    assert "newest error-cycle day key (V169)" in recon and "same-date" in recon
    assert "CONTRACT_END_DATE" in _rule_row(rb, "COST_CONTRACT_BREACH")
    for rule in ("PERF_QUERY_FAIL_PCT", "PERF_QUEUED_MINUTES", "PERF_SPILL_GB"):
        assert "whatever its raise day (V168)" in _rule_row(rb, rule), rule
    # the claims hold in the latest scan bodies
    hourly, daily = _current_proc("SP_ALERT_SCAN"), _current_proc("SP_ALERT_SCAN_DAILY")
    assert "TO_DATE(CONVERT_TIMEZONE('America/Chicago', LAST_LOAD_TIME)) AS FAIL_DAY" in hourly
    assert "TO_DATE(CONVERT_TIMEZONE('America/Chicago', nn.FIRST_SEEN))" in hourly
    assert "(TARGET_REGION IS NOT NULL OR TARGET_CLOUD IS NOT NULL)" in daily
    assert "MAX(LATEST_LOAD) AS NEWEST_LOAD" in daily and "CONTRACT_END_DATE" in daily


def test_runbook_rule_rows_track_the_v171_v172_scans():
    rb = read("RUNBOOK.md")
    assert "ref_gap_check_failed" in _rule_row(rb, "PIPE_REF_GAP")
    assert "'ref_gap_check_failed'" in _current_proc("SP_SCAN_REF_GAPS")
    assert "Hr/Min/Sec (V171)" in _rule_row(rb, "OPS_SLOW_RENDER")
    perf = _rule_row(rb, "PERF_CHANGE_REGRESSION")
    assert "scheduled runs" in perf and "older than 8h" in perf and "`CALL<name>(`" in perf
    assert "stops the scan (V172" in _rule_row(rb, "COST_CLOUD_SVC_ANOMALY")
    assert "only while the rule is enabled (V172)" in _rule_row(rb, "COST_ANOMALY_SWEEP")
    assert "COMPANY_FOR_DATABASE; unmapped -> UNKNOWN, V172" in " ".join(rb.split())
    impact = _current_proc("SP_CHANGE_IMPACT_SCAN")
    assert "COMPANY_FOR_DATABASE" in impact and "'TRXS%'" not in impact


def test_runbook_task_rows_name_the_v166_v167_loader_behaviour():
    from app.data.app_cost_sql import SESSION_PAD_DAYS

    assert "fact_load_failed" in _task_row("TASK_LOAD_STORAGE_TRUTH")
    assert f"sessions resolved {SESSION_PAD_DAYS} days back" in _task_row("TASK_LOAD_APP_COST")
    assert "COVERAGE_FROM" in _task_row("TASK_PATTERN_COST_DAILY")
    assert "swept of rows the reload did not re-stamp" in _task_row("TASK_NIGHTLY_RECONCILE")
    for proc in ("SP_LOAD_APP_COST", "SP_LOAD_STORAGE_TRUTH"):
        body = _current_proc(proc)
        assert "BEGIN TRANSACTION;" in body and "ROLLBACK;" in body and "'fact_load_failed'" in body, proc
    assert f"WHERE CREATED_ON >= DATEADD('day', -{SESSION_PAD_DAYS}, :lo)" in _current_proc("SP_LOAD_APP_COST")
    pattern = _current_proc("SP_LOAD_PATTERN_COST")
    assert "COVERAGE_FROM" in pattern and "BEGIN TRANSACTION;" in pattern


def test_runbook_settings_digest_and_incidents_follow_v170_v171():
    rb = read("RUNBOOK.md")
    flat = " ".join(rb.split())
    assert "seeded FALSE by V171" in flat and "not seeded, and Admin never lists it" not in flat
    assert any(re.search(r"\('CREDIT_PRICE_OVERRIDE',\s*'FALSE'\)", p.read_text(encoding="utf-8"))
               for p in (ROOT / "snowflake" / "migrations").glob("V[0-9]*__*.sql"))
    assert "Nothing declared" in rb and "Declared by and each member's Linked by are the DBA who typed" in rb
    assert "Since V171 the facts cover the 7 complete days ending yesterday" in flat
    assert "'WINDOW_DAYS=7; WAREHOUSE_SPEND_USD='" in _current_proc("SP_DAILY_DIGEST")


def test_runbook_rollbacks_name_each_wave_procs_real_base():
    """Every 'PROC from Vnnn' in the V166-V172 rollback paragraphs is the base the re-deriving migration's lineage
    marker names, and every file the paragraphs cite exists."""
    rb = read("RUNBOOK.md")
    wave = _section(rb, "**Rolling back the V166-V172 wave.**", "| Rule | Family |")
    for v in ("V166", "V167", "V170", "V171", "V172"):
        assert f"**Rolling back {v}.**" in wave, v
    assert "**Rolling back V168 / V169.**" in wave
    migs = {p.name for p in (ROOT / "snowflake" / "migrations").glob("V[0-9]*__*.sql")}
    for name in re.findall(r"V\d{3}__\w+\.sql", wave):
        assert name in migs, name
    markers = "\n".join((ROOT / "snowflake" / "migrations" / m).read_text(encoding="utf-8")
                        for m in migs if 166 <= int(m[1:4]) <= 172)
    claims = re.findall(r"\b(SP_[A-Z0-9_]+) from (V\d{3})(?!\d)", wave)
    assert len(claims) >= 14, claims
    for proc, base in claims:
        assert f"-- >>> derived:{proc}  (from {base};" in markers, (proc, base)
    # the shapes the regex does not read: V170's two bases, V168 / V169's
    assert "V131's CREATE PROCEDURE (the 4-arg) and V072's CREATE VIEW" in wave
    for obj, base in (("SP_INCIDENT_DECLARE", "V131"), ("INCIDENT_PROPOSALS", "V072"),
                      ("SP_ALERT_SCAN", "V162"), ("SP_ALERT_SCAN_DAILY", "V163")):
        assert f"-- >>> derived:{obj}  (from {base};" in markers, obj
        assert f"{base}'s" in wave, base


def test_full_rebuild_names_the_cs_mart_arm_and_the_object_cost_opt_in():
    fr = " ".join(_section(read("docs/FULL_REBUILD.md"), "## 5. History backfill", "## 6. Validate").split())
    assert "MART_CLOUD_SVC_DAILY" in fr and "back to 364 days" in fr and "narrow -364" in fr
    assert "`SP_LOAD_OBJECT_COST(365)`" in fr and "14 days" in fr and "STATEMENT_TIMEOUT_IN_SECONDS" in fr
    assert "SP_LOAD_PATTERN_COST(364)" in fr and "COVERAGE_FROM" in fr
    bf = read("snowflake/backfill_365.sql")
    assert "INSERT INTO DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY" in bf
    assert ">= DATEADD('day', -364, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE)" in bf
    assert "\n--     CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_OBJECT_COST(365);" in bf      # the opt-in stays commented
    assert not re.search(r"^\s*CALL DBA_MAINT_DB\.OVERWATCH\.SP_LOAD_OBJECT_COST", bf, re.M)
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('DAILY', 365);" in bf
