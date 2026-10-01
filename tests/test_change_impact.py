"""Change-impact tracking (V010): builders + migration contract."""

from pathlib import Path

import pytest

from app.data import change_impact_sql

_V010 = (Path(__file__).resolve().parents[1] / "snowflake" / "migrations"
         / "V010__change_impact.sql").read_text(encoding="utf-8")


def test_registry_reader_bounded_and_scoped():
    sql = change_impact_sql.change_registry(9999, "Trexis", "TRXS_DW", "STAGING")
    assert "DATEADD('day', -120" in sql          # clamped
    assert "COMPANY = 'Trexis'" in sql
    assert "UPPER(DATABASE_NAME) IN ('TRXS_DW')" in sql  # exact database filter
    assert "SCHEMA_NAME ILIKE '%STAGING%' ESCAPE '~'" in sql
    assert "OBJECT_CHANGE_REGISTRY" in sql and "LIMIT 200" in sql


def test_registry_reader_all_companies_has_no_company_filter():
    sql = change_impact_sql.change_registry(30, "ALL")
    assert "COMPANY =" not in sql
    # schema is always visible as its own column
    assert "SCHEMA_NAME" in sql and "DATABASE_NAME" in sql


def test_run_history_procedure_matches_call_text():
    sql = change_impact_sql.object_run_history("PROCEDURE", "DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN", 28)
    assert "QUERY_TYPE = 'CALL'" in sql
    assert "'.SP_ALERT_SCAN('" in sql        # schema-qualified CALL target
    assert "'CALLSP_ALERT_SCAN('" in sql     # unqualified CALL target
    assert "ACCOUNT_USAGE.QUERY_HISTORY" in sql


def test_run_history_task_uses_task_history_equality():
    sql = change_impact_sql.object_run_history("task", "DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY")
    assert "ACCOUNT_USAGE.TASK_HISTORY" in sql
    assert "'DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY'" in sql
    assert "STATE IN ('SUCCEEDED', 'FAILED')" in sql


def test_run_history_rejects_bad_input():
    with pytest.raises(ValueError):
        change_impact_sql.object_run_history("VIEW", "A.B.C")
    with pytest.raises(ValueError):
        change_impact_sql.object_run_history("PROCEDURE", "X'; DROP TABLE Y;--")


def test_v010_registry_distinguishes_schema():
    """Owner requirement: every change must show which schema it came from."""
    assert "DATABASE_NAME     VARCHAR(200)  NOT NULL" in _V010
    assert "SCHEMA_NAME       VARCHAR(200)  NOT NULL" in _V010
    assert "PROCEDURE_SCHEMA AS SCHEMA_NAME" in _V010


def test_v010_seeds_rule_and_scan():
    assert "'PERF_CHANGE_REGRESSION'" in _V010
    assert "SP_CHANGE_IMPACT_SCAN" in _V010
    assert "TASK_CHANGE_IMPACT_SCAN" in _V010
    assert "QUERY_ATTRIBUTION_HISTORY" in _V010
    assert "COALESCE(ROOT_QUERY_ID, QUERY_ID)" in _V010   # children roll up to the CALL
    assert "attribution_unavailable" in _V010             # graceful fallback path
    assert _V010.count("EXCEPTION") >= 2                  # task + attribution guards


def test_v010_baselines_frozen_and_alert_deduped():
    assert "BASELINE_FROM IS NULL" in _V010               # freeze-once marker
    assert "TO_VARCHAR(r.CHANGE_SEEN_AT::DATE)" in _V010  # dedupe on object + change day
    assert "'INSUFFICIENT_AFTER'" in _V010
    assert "SELECT 10 AS VERSION" in _V010


# ---------------------------------------------------------------------------------------------------------------
# Scan <-> drill parity (V172): the LATEST SP_CHANGE_IMPACT_SCAN and the Operations "Run history around one change"
# drill (object_run_history) count the same calls and the same runs, so the verdict the scan books reconciles with
# the drill chart beside it.
# ---------------------------------------------------------------------------------------------------------------
def _scan() -> str:
    from tests.test_alert_rule_consistency import _latest_proc_bodies
    return _latest_proc_bodies()["SP_CHANGE_IMPACT_SCAN"]


def test_scan_and_drill_share_the_anchored_call_rule():
    """R2-021: both strip ALL whitespace (the POSIX class, so a tab or CRLF is stripped too) and require
    'CALL<name>(' or '.<name>(' -- never the bare '<name>(' suffix that let RUN_SP_LOAD count as SP_LOAD."""
    drill = change_impact_sql.object_run_history("PROCEDURE", "DB.SCH.SP_LOAD", 28)
    scan = _scan()
    assert "REGEXP_REPLACE(UPPER(QUERY_TEXT), '[[:space:]]', '')" in drill
    assert "'CALLSP_LOAD('" in drill and "'.SP_LOAD('" in drill
    assert scan.count("REGEXP_REPLACE(UPPER(q.QUERY_TEXT), '[[:space:]]', '')") == 8          # 4 sites x 2 anchors
    assert scan.count("POSITION('CALL' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '('") == 4
    assert scan.count("POSITION('.' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '('") == 4
    assert "REPLACE(REPLACE(UPPER(q.QUERY_TEXT)" not in scan and "CHR(10)" not in scan
    # the cheap ILIKE pre-filter rides in front of the normalisation on both sides (r23 #2)
    assert "QUERY_TEXT ILIKE '%SP_LOAD%'" in drill
    assert scan.count("AND q.QUERY_TEXT ILIKE '%' || SPLIT_PART(r.OBJECT_NAME, '.', 3) || '%'") == 4


@pytest.mark.parametrize(("text", "anchored", "v140_suffix"), [
    ("CALL DB.SCH.SP_LOAD()", True, True), ("call sp_load ()", True, True), ("CALL\tSP_LOAD(1)", True, True),
    ("CALL\r\nDB.SCH.SP_LOAD()", True, True),
    ("CALL DB.SCH.RUN_SP_LOAD()", False, True), ("CALL RUN_SP_LOAD('x')", False, True),   # the R2-021 blend
    ("CALL\tDB.SCH.RUN_SP_LOAD()", False, True), ("CALL DB.SCH.SP_LOAD_V2()", False, False),
])
def test_anchored_match_semantics(text, anchored, v140_suffix):
    """The two anchors over a whitespace-class strip, emulated (the rule both sides now share), beside V140's bare
    'NAME(' suffix match over a space/LF strip, which counted every RUN_SP_LOAD call as SP_LOAD."""
    import re
    norm = re.sub(r"\s", "", text.upper())
    assert ("CALLSP_LOAD(" in norm or ".SP_LOAD(" in norm) is anchored
    assert ("SP_LOAD(" in text.upper().replace(" ", "").replace("\n", "")) is v140_suffix


def test_scan_and_drill_share_the_terminal_attempt_rule():
    """R2-025: a TASK run is a SCHEDULED run on both sides -- STATE filtered first, then the terminal attempt per
    SCHEDULED_TIME (COMPLETED_TIME DESC NULLS LAST) -- so a retried-then-succeeded run is no failure."""
    drill = change_impact_sql.object_run_history("TASK", "DB.SCH.TASK_A", 28)
    assert drill.index("STATE IN ('SUCCEEDED', 'FAILED')") < drill.index("QUALIFY ROW_NUMBER()")
    assert "ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1" in drill
    counts = _scan().split("-- 5) Measured credits/call", 1)[0]
    legs = counts.split("FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY")[1:]
    assert len(legs) == 2
    for leg in legs:
        leg = leg[:leg.index(") h\n")]
        assert leg.index("AND STATE IN ('SUCCEEDED', 'FAILED')") < leg.index("QUALIFY ROW_NUMBER()")
        assert "PARTITION BY DATABASE_NAME, SCHEMA_NAME, NAME, SCHEDULED_TIME" in leg
        assert "ORDER BY COMPLETED_TIME DESC NULLS LAST) = 1" in leg


def test_scan_after_credits_per_call_is_per_settled_scheduled_run():
    """R2-022 + R2-025: the AFTER credits/call divides by DISTINCT runs (a task run's retry attempts are ONE run, the
    same unit as BASELINE_CALLS), over runs older than the ~8h attribution lag only, and writes nothing until one is
    attributed."""
    after = _scan().split("SET AFTER_CREDITS_PER_CALL = s.CR_PER_CALL", 1)[1].split("    EXCEPTION", 1)[0]
    assert "SUM(COALESCE(a.CR, 0)) / NULLIF(COUNT(DISTINCT x.RUN_KEY), 0) AS CR_PER_CALL" in after
    assert "q.QUERY_ID AS RUN_KEY" in after
    assert "r.OBJECT_NAME || '|' || TO_VARCHAR(h.SCHEDULED_TIME) AS RUN_KEY" in after
    assert after.count("< DATEADD('hour', -8, CURRENT_TIMESTAMP())") == 2
    assert "LEFT JOIN (" in after and "HAVING COUNT(a.RID) > 0" in after


def test_change_impact_help_names_v172_measurement_only_after_the_apply():
    """Law 12: the panel help claims V172's measurement rules only once V172 is applied."""
    from app.ui.pages import operations
    before, after = operations._change_impact_help(False), operations._change_impact_help(True)
    assert after.startswith(before) and after != before
    for phrase in ("more than 8h ago", "one scheduled run", "UNKNOWN", "Unmapped entities"):
        assert phrase not in before and phrase in after, phrase
    src = (Path(__file__).resolve().parents[1] / "app" / "ui" / "pages" / "operations.py").read_text(encoding="utf-8")
    assert "panel_help(_change_impact_help(has_migration(172, _PAGE)))" in src
