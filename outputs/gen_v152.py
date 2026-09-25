#!/usr/bin/env python3
"""Forward-generate V152: loader-owned freshness coverage for the two unstamped marts (Next-Fifty #10c).

A scripted scan of the latest proc bodies shows every FACT_/MART_ load target carries a
loader-owned SOURCE_FRESHNESS_STATE stamp EXCEPT two:
  * MART_CLOUD_SVC_DAILY -- loaded by SP_LOAD_CLOUD_SVC_MART (V055), which SP_LOAD_QH_EXTRACT
    CALLs hourly; the V150 COST_CLOUD_SVC_ANOMALY baseline reads it, so a silent stall starves
    that alert with nothing on any freshness board.
  * MART_TASK_NODE_DAILY -- loaded by the [6b] arm of SP_LOAD_MARTS_V27('HOURLY'), which already
    appends the 'task_node' token to :loaded on success, but the token is mapped nowhere.

Why three objects: the HOURLY stamp in SP_LOAD_MARTS_V27 JOINs the MART_SOURCE_FRESHNESS view to a
static srcmap VALUES list and gates on ARRAY_CONTAINS(token, SPLIT(:loaded)). A token alone stamps
nothing, and a view row alone stamps nothing -- MART_TASK_NODE_DAILY needs BOTH (view row + srcmap
row). MART_CLOUD_SVC_DAILY rides SP_LOAD_QH_EXTRACT's existing IF (ok) UNION ALL freshness MERGE
(its own proc's RETURN ... :SQLROWCOUNT would need rework to stamp there).

Three INSERTION-ONLY re-derivations, each from its CURRENT definer (tests/test_proc_lineage.py):
  1. MART_SOURCE_FRESHNESS view    from V045 -- + a MART_TASK_NODE_DAILY branch (sibling idiom);
  2. SP_LOAD_QH_EXTRACT(FLOAT)     from V149 -- + a MART_CLOUD_SVC_DAILY arm in the IF (ok) MERGE;
  3. SP_LOAD_MARTS_V27(VARCHAR, FLOAT) from V146 -- + ('MART_TASK_NODE_DAILY', 'task_node') srcmap row.
Everything else is byte-identical (tests/migrations/test_v152_* normalizes each back to its base).
No task / rule change and no tail CALL: the next hourly graph run stamps both rows.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE_VIEW = MIG / "V045__task_monitoring_restored.sql"
BASE_QH = MIG / "V149__qh_extract_session_client_columns.sql"
BASE_MARTS = MIG / "V146__ai_usage_loader_repoint_ai_functions.sql"


def _one(pattern: str, text: str, what: str) -> str:
    matches = re.findall(pattern, text, re.S)
    assert len(matches) == 1, f"{what}: expected exactly 1 definition, got {len(matches)}"
    return matches[0]


def _replace_once(text: str, old: str, new: str, what: str) -> str:
    assert text.count(old) == 1, f"{what}: anchor must occur exactly once, got {text.count(old)}"
    out = text.replace(old, new, 1)
    assert out.count(new) == 1, f"{what}: insertion must land exactly once"
    return out


# --- 1. MART_SOURCE_FRESHNESS view (base V045:2074-2169): append one branch -------------------
view_base = _one(
    r"CREATE OR REPLACE VIEW DBA_MAINT_DB\.OVERWATCH\.MART_SOURCE_FRESHNESS AS\n.*?;\n",
    BASE_VIEW.read_text(encoding="utf-8"), "MART_SOURCE_FRESHNESS (V045)")
VIEW_OLD = "FROM DBA_MAINT_DB.OVERWATCH.FACT_PLATFORM_SCORE_DAILY;\n"
VIEW_BRANCH = (
    "UNION ALL\n"
    "SELECT 'MART_TASK_NODE_DAILY', MAX(LOAD_TS), COUNT(*),\n"
    "       DATEDIFF('minute', MAX(LOAD_TS), CURRENT_TIMESTAMP()) / 60.0\n"
    "FROM DBA_MAINT_DB.OVERWATCH.MART_TASK_NODE_DAILY;\n"
)
VIEW_NEW = "FROM DBA_MAINT_DB.OVERWATCH.FACT_PLATFORM_SCORE_DAILY\n" + VIEW_BRANCH
view = _replace_once(view_base, VIEW_OLD, VIEW_NEW, "view tail")
assert view.endswith(VIEW_BRANCH)                              # the new branch is the LAST one
assert view.count("UNION ALL\n") == view_base.count("UNION ALL\n") + 1
assert view.count("SELECT 'MART_TASK_NODE_DAILY'") == 1

# --- 2. SP_LOAD_QH_EXTRACT(FLOAT) (base V149): + MART_CLOUD_SVC_DAILY in the IF (ok) stamp ------
qh_base = _one(r"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.SP_LOAD_QH_EXTRACT\(.*?\$\$;\n",
               BASE_QH.read_text(encoding="utf-8"), "SP_LOAD_QH_EXTRACT (V149)")
assert qh_base.startswith("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(DAYS_BACK FLOAT)")
QH_ANCHOR = ("        SELECT 'FACT_QUERY_DAILY', MAX(LOAD_TS), COUNT(*)\n"
             "        FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY\n")
QH_INSERT = ("        UNION ALL\n"
             "        SELECT 'MART_CLOUD_SVC_DAILY', MAX(LOAD_TS), COUNT(*)\n"
             "        FROM DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY\n")
qh = _replace_once(qh_base, QH_ANCHOR, QH_ANCHOR + QH_INSERT, "SP_LOAD_QH_EXTRACT freshness MERGE")
# the new arm sits INSIDE the IF (ok) freshness MERGE, after the mart CALL that fills it
_stamp = qh.index("MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE")
assert qh.index("    IF (ok) THEN\n") < _stamp < qh.index(QH_INSERT) < qh.index("    END IF;\n\n    RETURN 'qh extract")
assert qh.index("CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_CLOUD_SVC_MART();") < _stamp
for anchor in (   # every V149 arm carries over verbatim
    "SESSION_ID, IS_CLIENT_GENERATED_STATEMENT",
    "DATE_TRUNC('hour', DATEADD('hour', -48, CURRENT_TIMESTAMP()))",
    "'cloud_svc_mart_failed'",
    "STATUS = 'loader'",
):
    assert anchor in qh, anchor

# --- 3. SP_LOAD_MARTS_V27(VARCHAR, FLOAT) (base V146): + the task_node srcmap row --------------
marts_base = _one(r"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.SP_LOAD_MARTS_V27\(.*?\$\$;\n",
                  BASE_MARTS.read_text(encoding="utf-8"), "SP_LOAD_MARTS_V27 (V146)")
assert marts_base.startswith(
    "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27(SCOPE VARCHAR, DAYS_BACK FLOAT)")
MARTS_ANCHOR = "                    ('MART_TASK_GRAPH_DAILY', 'graphs'),\n"
MARTS_INSERT = "                    ('MART_TASK_NODE_DAILY', 'task_node'),\n"
marts = _replace_once(marts_base, MARTS_ANCHOR, MARTS_ANCHOR + MARTS_INSERT, "HOURLY srcmap")
# token chain: the [6b] arm appends 'task_node ' on its SUCCESS path, in the SAME (HOURLY) scope block
# as the srcmap row -- otherwise the row would never stamp.
_hourly, _daily = marts.split("    IF (UPPER(:SCOPE) = 'DAILY') THEN\n", 1)
_tok = _hourly.index("loaded := loaded || 'task_node ';")
assert _hourly.index("MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_TASK_NODE_DAILY t") < _tok
assert _tok < _hourly.index("MART_TASK_NODE_DAILY - other marts unaffected")    # before its handler
assert _tok < _hourly.index(MARTS_INSERT) and MARTS_INSERT not in _daily
assert marts.count("AS srcmap(SOURCE_NAME, TOKEN)") == 2                        # HOURLY + DAILY maps

MARKER_VIEW = "-- >>> derived:MART_SOURCE_FRESHNESS  (from V045; + MART_TASK_NODE_DAILY row, V152)"
MARKER_QH = "-- >>> derived:SP_LOAD_QH_EXTRACT  (from V149; + MART_CLOUD_SVC_DAILY freshness stamp, V152)"
MARKER_MARTS = ("-- >>> derived:SP_LOAD_MARTS_V27  (from V146; + MART_TASK_NODE_DAILY in the HOURLY "
                "freshness srcmap, V152)")

DESCRIPTION = (
    "Pipeline freshness coverage (Next-Fifty #10c): the two FACT_/MART_ load targets that carried no "
    "loader-owned SOURCE_FRESHNESS_STATE stamp now get one. MART_CLOUD_SVC_DAILY (the V150 "
    "COST_CLOUD_SVC_ANOMALY baseline) is stamped by SP_LOAD_QH_EXTRACT (re-derived from V149) as one more "
    "arm of its IF (ok) UNION ALL freshness MERGE -- MAX(LOAD_TS) advances only when the SP_LOAD_CLOUD_SVC_MART "
    "MERGE succeeded. MART_TASK_NODE_DAILY needs both halves of the token-gated HOURLY stamp: a view row "
    "(MART_SOURCE_FRESHNESS re-created from V045 + one branch) and a srcmap row ((''MART_TASK_NODE_DAILY'', "
    "''task_node'') in SP_LOAD_MARTS_V27, re-derived from V146) for the token its [6b] arm already appends "
    "on success. Insertions only; each object is otherwise byte-identical to its base. Both names contain "
    "DAILY, so the shared name rule judges them at 30h although both load hourly (lenient, never a false "
    "stale). No task, rule or alert change; no tail CALL -- the next hourly run stamps both rows."
)
assert "'" not in DESCRIPTION.replace("''", "")                  # every apostrophe doubled
assert len(DESCRIPTION.replace("''", "'")) <= 4000

out = f"""-- V152__pipeline_freshness_coverage.sql
--
-- Pipeline freshness coverage (Next-Fifty #10c). Every FACT_/MART_ table a loader MERGEs or
-- INSERTs into carries a loader-owned SOURCE_FRESHNESS_STATE stamp EXCEPT two, so a stalled load
-- of either was invisible to the health strip, the Control Room / Admin freshness boards and the
-- native NATIVE_ALERT_STALE_FACTS watcher:
--   * MART_CLOUD_SVC_DAILY -- filled by SP_LOAD_CLOUD_SVC_MART, which SP_LOAD_QH_EXTRACT CALLs
--     hourly; the V150 COST_CLOUD_SVC_ANOMALY baseline reads it.
--   * MART_TASK_NODE_DAILY -- filled by the [6b] arm of SP_LOAD_MARTS_V27('HOURLY'), which already
--     appends 'task_node' to :loaded on success, but that token was mapped nowhere.
--
-- The HOURLY stamp JOINs the MART_SOURCE_FRESHNESS view to a static srcmap VALUES list and gates on
-- ARRAY_CONTAINS(token, SPLIT(:loaded)): a token alone stamps nothing, and a view row alone stamps
-- nothing, so MART_TASK_NODE_DAILY gets BOTH. MART_CLOUD_SVC_DAILY rides SP_LOAD_QH_EXTRACT's existing
-- IF (ok) UNION ALL freshness MERGE (STATUS 'loader'): its MAX(LOAD_TS) advances only when the mart
-- MERGE succeeded (a failure logs cloud_svc_mart_failed with CONTEXT 'MART_CLOUD_SVC_DAILY - ...', which
-- Admin > Diagnose stale sources maps), so the stamp never reads a failed mart as fresh.
--
-- Three insertion-only re-derivations, each from its CURRENT definer; everything else byte-identical:
--   1. MART_SOURCE_FRESHNESS           from V045 -- + one MART_TASK_NODE_DAILY branch (the sibling
--                                         branches' exact clock idiom; no COPY GRANTS, as V041-V045);
--   2. SP_LOAD_QH_EXTRACT(FLOAT)       from V149 -- + one MART_CLOUD_SVC_DAILY arm in the stamp MERGE;
--   3. SP_LOAD_MARTS_V27(VARCHAR, FLOAT) from V146 -- + one HOURLY srcmap row.
-- Both new SOURCE_NAMEs contain DAILY, so the shared cadence name rule judges them at 30h although
-- both load hourly: lenient (a stall shows after 30h), never a false stale.
--
-- No task, rule or alert change and no tail CALL: the next hourly TASK_LOAD_HOURLY run stamps both
-- rows (STATUS 'loader' / 'task_node'). Idempotent; safe to re-run. Owner applies in Snowsight after
-- V151. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20152, 'V152 requires V151 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 151) THEN
        RAISE not_ready;
    END IF;
END;
$$;

{MARKER_VIEW}
{view}
{MARKER_QH}
{qh}
{MARKER_MARTS}
{marts}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 152 AS VERSION,
       '{DESCRIPTION}' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 152);
"""

# --- self-assertions -------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 2
assert out.count("CREATE OR REPLACE VIEW") == 1
for marker, create in (
    (MARKER_VIEW, "CREATE OR REPLACE VIEW DBA_MAINT_DB.OVERWATCH.MART_SOURCE_FRESHNESS AS"),
    (MARKER_QH, "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT("),
    (MARKER_MARTS, "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27("),
):
    assert out.count(marker) == 1 and out.count(create) == 1
    assert marker + "\n" + create in out                        # marker sits directly above its CREATE
assert "CREATE TASK" not in out and "ALTER TASK" not in out and "ALERT_CONFIG" not in out
assert "CREATE TABLE" not in out and "ALTER TABLE" not in out and "COPY GRANTS" not in view
_outside = out.replace(view, "").replace(qh, "").replace(marts, "")
assert not re.search(r"^\s*CALL\b", _outside, re.M)            # no apply-time CALL
assert "EXCEPTION (-20152" in out and "IF (v < 151) THEN" in out
assert "SELECT 152 AS VERSION" in out and "WHERE VERSION = 152)" in out

target = Path(os.environ.get("V152_OUT") or (MIG / "V152__pipeline_freshness_coverage.sql"))
with target.open("w", encoding="utf-8", newline="\n") as fh:
    fh.write(out)
print(f"wrote {target} ({len(out)} chars)")
