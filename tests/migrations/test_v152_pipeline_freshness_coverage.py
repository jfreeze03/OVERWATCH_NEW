"""V152 locks: loader-owned freshness coverage for the two unstamped marts (Next-Fifty #10c).

MART_CLOUD_SVC_DAILY (the V150 COST_CLOUD_SVC_ANOMALY baseline) and MART_TASK_NODE_DAILY were the
only FACT_/MART_ load targets with no SOURCE_FRESHNESS_STATE stamp. V152 closes both with three
INSERTION-ONLY re-derivations, each from its CURRENT definer:
  * MART_SOURCE_FRESHNESS view        from V045 -- + a MART_TASK_NODE_DAILY branch;
  * SP_LOAD_QH_EXTRACT(FLOAT)         from V149 -- + a MART_CLOUD_SVC_DAILY arm in the IF (ok) stamp;
  * SP_LOAD_MARTS_V27(VARCHAR, FLOAT) from V146 -- + the ('MART_TASK_NODE_DAILY', 'task_node') row.
The HOURLY stamp JOINs the view to a static srcmap and gates on the arm's :loaded token, so the
task-node row needs BOTH halves (a token alone stamps nothing; a view row alone stamps nothing).
Byte-locked to outputs/gen_v152.py; every object normalizes back to its exact base.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[2]
_MIG = _ROOT / "snowflake" / "migrations"
_NAME = "V152__pipeline_freshness_coverage.sql"
_V152 = (_MIG / _NAME).read_text(encoding="utf-8")
_V045 = (_MIG / "V045__task_monitoring_restored.sql").read_text(encoding="utf-8")
_V149 = (_MIG / "V149__qh_extract_session_client_columns.sql").read_text(encoding="utf-8")
_V146 = (_MIG / "V146__ai_usage_loader_repoint_ai_functions.sql").read_text(encoding="utf-8")

# The three insertions, verbatim (kept in step with outputs/gen_v152.py).
_VIEW_BRANCH = ("FROM DBA_MAINT_DB.OVERWATCH.FACT_PLATFORM_SCORE_DAILY\n"
                "UNION ALL\n"
                "SELECT 'MART_TASK_NODE_DAILY', MAX(LOAD_TS), COUNT(*),\n"
                "       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0\n"
                "FROM DBA_MAINT_DB.OVERWATCH.MART_TASK_NODE_DAILY;\n")
_VIEW_TAIL_V045 = "FROM DBA_MAINT_DB.OVERWATCH.FACT_PLATFORM_SCORE_DAILY;\n"
_QH_INSERT = ("        UNION ALL\n"
              "        SELECT 'MART_CLOUD_SVC_DAILY', MAX(LOAD_TS), COUNT(*)\n"
              "        FROM DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY\n")
_MARTS_INSERT = "                    ('MART_TASK_NODE_DAILY', 'task_node'),\n"

_MARKERS = {
    "MART_SOURCE_FRESHNESS": ("-- >>> derived:MART_SOURCE_FRESHNESS  (from V045;",
                              "CREATE OR REPLACE VIEW DBA_MAINT_DB.OVERWATCH.MART_SOURCE_FRESHNESS AS"),
    "SP_LOAD_QH_EXTRACT": ("-- >>> derived:SP_LOAD_QH_EXTRACT  (from V149;",
                           "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(DAYS_BACK FLOAT)"),
    "SP_LOAD_MARTS_V27": ("-- >>> derived:SP_LOAD_MARTS_V27  (from V146;",
                          "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27"
                          "(SCOPE VARCHAR, DAYS_BACK FLOAT)"),
}


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _one(pattern: str, text: str) -> str:
    found = re.findall(pattern, text, re.S)
    assert len(found) == 1, f"expected exactly one match for {pattern[:60]!r}, got {len(found)}"
    return found[0]


def _view(text: str) -> str:
    return _one(r"CREATE OR REPLACE VIEW DBA_MAINT_DB\.OVERWATCH\.MART_SOURCE_FRESHNESS AS\n.*?;\n", text)


def _proc(text: str, name: str) -> str:
    return _one(rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\$\$;\n", text)


def test_v152_regenerates_byte_identical(tmp_path):
    output = tmp_path / "regen.sql"
    result = subprocess.run(
        [sys.executable, str(_ROOT / "outputs" / "gen_v152.py")],
        env={**os.environ, "V152_OUT": str(output)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == _V152, (
        "V152 drifted from its forward-generation — edit outputs/gen_v152.py, "
        "not the .sql, then regenerate."
    )


def test_v152_guarded_and_versioned():
    assert "EXCEPTION (-20152, 'V152 requires V151 first - apply migrations in order.')" in _V152
    assert "IF (v < 151) THEN" in _V152
    assert _V152.index("EXECUTE IMMEDIATE") < _V152.index("CREATE OR REPLACE")   # guard runs first
    assert "SELECT 152 AS VERSION" in _V152 and "WHERE VERSION = 152)" in _V152
    assert _V152.rstrip().endswith("WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION "
                                   "WHERE VERSION = 152);")


def test_v152_object_inventory_is_two_procs_and_one_view_only():
    assert _V152.count("CREATE OR REPLACE PROCEDURE") == 2
    assert _V152.count("CREATE OR REPLACE VIEW") == 1
    # (the carried timeline arm READS ALERT_EVENTS; nothing in V152 raises into it)
    for banned in ("CREATE TASK", "ALTER TASK", "ALERT_CONFIG", "INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS",
                   "CREATE TABLE", "ALTER TABLE", "CREATE OR REPLACE FUNCTION",
                   "SYSTEM$TASK_DEPENDENTS_ENABLE"):
        assert banned not in _V152, banned
    # no apply-time CALL: the only CALLs live inside the carried proc bodies
    outside = _V152.replace(_view(_V152), "")
    for name in ("SP_LOAD_QH_EXTRACT", "SP_LOAD_MARTS_V27"):
        outside = outside.replace(_proc(_V152, name), "")
    assert not re.search(r"^\s*CALL\b", outside, re.M), "V152 must not CALL anything at apply time"


def test_v152_markers_sit_directly_above_each_create():
    for name, (marker, create) in _MARKERS.items():
        assert _V152.count(marker) == 1, name
        line = next(ln for ln in _V152.splitlines() if ln.startswith(marker))
        assert line.endswith(", V152)"), line
        assert f"{line}\n{create}" in _V152, f"{name}: marker must sit directly above its CREATE"


def test_v152_bases_are_the_immediately_previous_definers():
    """The round-13 class: each base must be the CURRENT definer, not an older copy."""
    from tests.test_proc_lineage import _definers, _migrations
    texts = _migrations()
    defs = _definers(texts)
    assert max(v for v in defs["SP_LOAD_QH_EXTRACT"] if v < 152) == 149
    assert max(v for v in defs["SP_LOAD_MARTS_V27"] if v < 152) == 146
    view_defs = sorted(v for v, t in texts.items()
                       if "CREATE OR REPLACE VIEW DBA_MAINT_DB.OVERWATCH.MART_SOURCE_FRESHNESS AS" in t)
    assert max(v for v in view_defs if v < 152) == 45
    assert 152 in defs["SP_LOAD_QH_EXTRACT"] and 152 in defs["SP_LOAD_MARTS_V27"] and 152 in view_defs


def test_v152_view_is_byte_identical_to_v045_apart_from_the_task_node_branch():
    v152 = _view(_V152)
    assert v152.count(_VIEW_BRANCH) == 1 and v152.endswith(_VIEW_BRANCH)
    assert v152.count("SELECT 'MART_TASK_NODE_DAILY'") == 1
    assert v152.replace(_VIEW_BRANCH, _VIEW_TAIL_V045) == _view(_V045), (
        "V152 changed MART_SOURCE_FRESHNESS beyond the appended MART_TASK_NODE_DAILY branch")
    # the new branch reuses the sibling branches' exact 4-column shape and clock
    sibling = ("SELECT 'MART_TASK_GRAPH_DAILY', MAX(LOAD_TS), COUNT(*),\n"
               "       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0\n")
    assert sibling in v152
    assert sibling.replace("MART_TASK_GRAPH_DAILY", "MART_TASK_NODE_DAILY") in v152


def test_v152_qh_extract_is_byte_identical_to_v149_apart_from_the_cloud_svc_stamp():
    p152 = _proc(_V152, "SP_LOAD_QH_EXTRACT")
    assert p152.count(_QH_INSERT) == 1
    assert p152.replace(_QH_INSERT, "") == _proc(_V149, "SP_LOAD_QH_EXTRACT"), (
        "V152 changed SP_LOAD_QH_EXTRACT beyond the MART_CLOUD_SVC_DAILY freshness arm")
    # the arm sits INSIDE the IF (ok) freshness MERGE, after the mart CALL that fills it, so it stamps
    # only when the extract committed and MAX(LOAD_TS) advances only when the mart MERGE succeeded
    stamp = p152.index("MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t")
    assert (p152.index("CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_CLOUD_SVC_MART();")
            < p152.index("    IF (ok) THEN\n") < stamp < p152.index(_QH_INSERT)
            < p152.index("    ) s\n    ON t.SOURCE_NAME = s.SOURCE_NAME", stamp)
            < p152.index("    END IF;\n\n    RETURN 'qh extract"))
    # a mart failure keeps logging with a CONTEXT that Admin > Diagnose stale sources maps by name
    assert "'cloud_svc_mart_failed', :emsg, 'MART_CLOUD_SVC_DAILY - extract unaffected'" in p152


def test_v152_marts_loader_is_byte_identical_to_v146_apart_from_the_srcmap_row():
    p152 = _proc(_V152, "SP_LOAD_MARTS_V27")
    assert p152.count(_MARTS_INSERT) == 1
    assert p152.replace(_MARTS_INSERT, "") == _proc(_V146, "SP_LOAD_MARTS_V27"), (
        "V152 changed SP_LOAD_MARTS_V27 beyond the MART_TASK_NODE_DAILY srcmap row")


def test_v152_task_node_token_chain_actually_stamps():
    """The row stamps only if (1) the [6b] arm appends 'task_node ' on SUCCESS, (2) in the SAME scope
    block as the srcmap row, and (3) the view the MERGE joins carries a MART_TASK_NODE_DAILY row."""
    p152 = _proc(_V152, "SP_LOAD_MARTS_V27")
    hourly, daily = p152.split("    IF (UPPER(:SCOPE) = 'DAILY') THEN\n", 1)
    assert hourly.count("    IF (UPPER(:SCOPE) = 'HOURLY') THEN\n") == 1
    tok = hourly.index("loaded := loaded || 'task_node ';")
    assert hourly.index("MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_TASK_NODE_DAILY t") < tok
    assert tok < hourly.index("'MART_TASK_NODE_DAILY - other marts unaffected'")   # not in the handler
    assert hourly.rfind("WHEN OTHER THEN", 0, tok) < hourly.rfind("BEGIN", 0, tok)
    fresh = hourly.index("MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t")
    assert tok < fresh < hourly.index(_MARTS_INSERT)
    assert "FROM DBA_MAINT_DB.OVERWATCH.MART_SOURCE_FRESHNESS f" in hourly[fresh:]
    assert "WHERE ARRAY_CONTAINS(m.TOKEN::VARIANT, SPLIT(:loaded, ' '))" in hourly[fresh:]
    assert "task_node" not in daily
    assert "SELECT 'MART_TASK_NODE_DAILY'" in _view(_V152)


def test_v152_new_sources_fall_in_the_lenient_daily_cadence_bucket():
    """Both names contain DAILY, so the shared name rule (app health strip / Admin / Control Room /
    NATIVE_ALERT_STALE_FACTS) judges them at 30h although both load hourly: lenient, never false-stale."""
    for name in ("MART_CLOUD_SVC_DAILY", "MART_TASK_NODE_DAILY"):
        assert "DAILY" in name
    from app.config import THRESHOLDS
    assert THRESHOLDS["stale_daily_fact_hours"] > THRESHOLDS["stale_fact_hours"]


def test_v152_description_is_escaped_and_fits():
    m = re.search(r"SELECT 152 AS VERSION,\s*'((?:[^']|'')*)'\s*AS DESCRIPTION", _V152, re.S)
    assert m, "SCHEMA_VERSION insert must use the SELECT ... AS VERSION, '...' AS DESCRIPTION idiom"
    assert len(m.group(1).replace("''", "'")) <= 4000


# The three lockstep checks below are pinned to the WAVE-2a TIP (V154). They are EXPECTED to fail on the
# w2a-v152 slice branch until the integrator lands the shared validate/docs/admin edits for the wave.
def test_validate_and_docs_track_v152():
    val = _read("snowflake/validate.sql")
    assert "V001..V155 applied" in val and "VERSION BETWEEN 1 AND 155) = 155" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert _NAME in _read(rel), rel


def test_v152_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 152 in _EXPECTED_MIGRATIONS


def test_v152_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    statements = list(_plain_statements(_V152))
    assert any(s.startswith("CREATE OR REPLACE VIEW DBA_MAINT_DB.OVERWATCH.MART_SOURCE_FRESHNESS")
               for s in statements), "the view must be among the sqlglot-parsed statements"
    for statement in statements:
        sqlglot.parse(statement, dialect="snowflake")
