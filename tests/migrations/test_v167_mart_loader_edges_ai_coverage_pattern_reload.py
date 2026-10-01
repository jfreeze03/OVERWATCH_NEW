"""Locks for V167 -- mart-loader window edges, the AI coverage watermark and the atomic pattern reload.

STRUCTURE + GENERATION + LOCKS; tests/migrations/test_v167_harness.py EXECUTES the sweeps, the stamps and the
pattern reload in sqlite. What this file proves, from the shipped text:
  * generation -- outputs/gen_v167.py regenerates the migration byte-for-byte, reads only its three bases and
    writes nothing else; PREFLIGHT / PART B are read-only and parse; OWNER_REPAIRS pins Central first and its
    arm-[6] rebuild is V167's own arm text with d = 364, inside an anonymous ROLLBACK + RAISE block;
  * shape -- first line, guard (-20167, v < 166), the ALTER before every CREATE, three markers + procs, the
    twin DELETE after the last CREATE, the version row; nothing is CALLed at apply time;
  * lineage + round 13 -- each proc is re-derived from its immediately previous definer (V159 / V064 / V120),
    and reversing the declared deltas with THIS file's own copies of the old text gives each base back
    byte-for-byte (with a teeth mutation for each);
  * the deltas -- arm-scoped window locks ([1] / [6] / [9]), the DAILY-only COVERAGE_FROM inside the existing
    MERGE (no UPDATE of SOURCE_FRESHNESS_STATE), the sweep tokens == the loader's own tokens, the LOAD_TS
    invariant the sweep and the twin DELETE stand on, the pattern transaction order;
  * models -- R2-015 (zoneinfo America/Chicago, a year of left edges) and R2-014 (the real write schedule:
    hourly d=2 at the gated hours + the nightly reconcile) replayed on the arm predicates read from the text.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests._source import ROOT, migration_tip, read
from tests.test_migration_proc_syntax import _strip_noise

_NAME = "V167__mart_loader_edges_ai_coverage_pattern_reload.sql"
_MIG = read(f"snowflake/migrations/{_NAME}")
_V159 = read("snowflake/migrations/V159__loader_compile_diet.sql")
_V064 = read("snowflake/migrations/V064__webhook_drain_watermarks_alert_burn_telemetry.sql")
_V120 = read("snowflake/migrations/V120__pattern_cost_runs_fanout_fix.sql")
_V165 = read("snowflake/migrations/V165__daily_digest_grounding.sql")
_CT = ZoneInfo("America/Chicago")


def _proc(text: str, name: str) -> str:
    s = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    o = text.index("$$", s)
    return text[s:text.index("$$;", o + 2) + 3]


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


_M = _proc(_MIG, "SP_LOAD_MARTS_V27(")
_R = _proc(_MIG, "SP_NIGHTLY_RECONCILE(")
_P = _proc(_MIG, "SP_LOAD_PATTERN_COST(")
_M159 = _proc(_V159, "SP_LOAD_MARTS_V27(")
_R064 = _proc(_V064, "SP_NIGHTLY_RECONCILE(")
_P120 = _proc(_V120, "SP_LOAD_PATTERN_COST(")
_DAILY_SPLIT = "    IF (UPPER(:SCOPE) = 'DAILY') THEN\n"

# ---------------------------------------------------------------------------------------------------
# Test-side copies (independent of outputs/gen_v167.py).
# ---------------------------------------------------------------------------------------------------
_MARKERS = {
    "SP_LOAD_MARTS_V27": ("-- >>> derived:SP_LOAD_MARTS_V27  (from V159; + R2-015 arm [1] span source padded a day, "
                          "R2-014 arm [6] lead-in day + DAY filter, R2-052 arm [9] Central day key, R1-016 AI "
                          "COVERAGE_FROM in the DAILY freshness MERGE, V167)\n"),
    "SP_NIGHTLY_RECONCILE": ("-- >>> derived:SP_NIGHTLY_RECONCILE  (from V064; R2-018 token-gated mark-and-sweep of "
                             "the four wide-edge tables after the marts reload instead of DELETE-first, V167)\n"),
    "SP_LOAD_PATTERN_COST": ("-- >>> derived:SP_LOAD_PATTERN_COST  (from V120; R2-010 atomic DELETE + INSERT of the "
                             "window instead of the COMPANY-keyed MERGE, + the PATTERN-RESTAMP COVERAGE_FROM stamp, "
                             "V167)\n"),
}
_BASES = {"SP_LOAD_MARTS_V27": 159, "SP_NIGHTLY_RECONCILE": 64, "SP_LOAD_PATTERN_COST": 120}
_ALTER = "ALTER TABLE DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE ADD COLUMN IF NOT EXISTS COVERAGE_FROM DATE;\n"

# SP_LOAD_MARTS_V27 -- V159's old text and the V167 replacement block boundaries
_ARM1 = ("        -- [1] warehouse efficiency", "loaded := loaded || 'wh_eff ';")
_ARM6 = ("        -- [6] task graphs", "loaded := loaded || 'graphs ';")
_ARM9 = ("        -- [9] AI usage", "loaded := loaded || 'ai_code ';")
_OLD_QH = "                        WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())\n"
_NEW_QH_PRED = ("                        WHERE START_TIME >= DATEADD('day', -:d - 1, CURRENT_DATE())\n"
                "                          AND COALESCE(END_TIME, START_TIME) >= DATEADD('day', -:d, CURRENT_DATE())\n")
_OLD_ATT = "                    WHERE h.QUERY_START_TIME >= DATEADD('day', -:d, CURRENT_DATE())\n"
_NEW_ATT = "                    WHERE h.QUERY_START_TIME >= DATEADD('day', -:d - 1, CURRENT_DATE())\n"
_OLD_RUNS = "                FROM runs GROUP BY 1, 2, 3, 4\n"
_NEW_RUNS = ("                FROM runs\n"
             "                WHERE DAY >= DATEADD('day', -:d, CURRENT_DATE())\n"
             "                GROUP BY 1, 2, 3, 4\n")
_OLD_DAY9 = "                SELECT c.USAGE_TIME::DATE AS DAY,\n"
_NEW_DAY9 = "                SELECT CONVERT_TIMEZONE('America/Chicago', c.USAGE_TIME)::DATE AS DAY,\n"
_OLD_TS9 = ("                       MIN(c.USAGE_TIME)::TIMESTAMP_NTZ AS FIRST_TS,\n"
            "                       MAX(c.USAGE_TIME)::TIMESTAMP_NTZ AS LAST_TS,\n")
_NEW_TS9 = ("                       CONVERT_TIMEZONE('America/Chicago', MIN(c.USAGE_TIME))::TIMESTAMP_NTZ AS FIRST_TS,\n"
            "                       CONVERT_TIMEZONE('America/Chicago', MAX(c.USAGE_TIME))::TIMESTAMP_NTZ AS LAST_TS,\n")
_STAMP = "DATEADD('day', -:d + 1, CURRENT_DATE())"
_OLD_MERGE_TAIL = (
    "            STATUS = s.STATUS\n"
    "        WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)\n"
    "        VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, s.STATUS);\n")
_NEW_MERGE_SET = (
    f"            COVERAGE_FROM = IFF(s.SOURCE_NAME = 'FACT_AI_USAGE_DAILY',\n"
    f"                                LEAST(COALESCE(t.COVERAGE_FROM, {_STAMP}),\n"
    f"                                      {_STAMP}),\n"
    "                                t.COVERAGE_FROM)\n")
_NEW_MERGE_INSERT = (
    "        WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS, COVERAGE_FROM)\n"
    "        VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, s.STATUS,\n"
    f"                IFF(s.SOURCE_NAME = 'FACT_AI_USAGE_DAILY', {_STAMP}, NULL));\n")

# SP_NIGHTLY_RECONCILE -- V064's four DELETEs and its old TODO sentence
_OLD_DELETES = {
    "MART_WAREHOUSE_EFFICIENCY_DAILY": ("    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY\n"
                                        "     WHERE DAY >= DATEADD('day', -3, CURRENT_DATE());\n"),
    "FACT_QUERY_ROLE_HOURLY": ("    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_ROLE_HOURLY\n"
                               "     WHERE HOUR_TS >= DATEADD('day', -3, CURRENT_TIMESTAMP());\n"),
    "FACT_QUERY_SCHEMA_HOURLY": ("    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_SCHEMA_HOURLY\n"
                                 "     WHERE HOUR_TS >= DATEADD('day', -3, CURRENT_TIMESTAMP());\n"),
    "MART_TASK_GRAPH_DAILY": ("    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY\n"
                              "     WHERE DAY >= DATEADD('day', -3, CURRENT_DATE());\n"),
}
_SWEPT = {"MART_WAREHOUSE_EFFICIENCY_DAILY": "wh_eff", "MART_TASK_GRAPH_DAILY": "graphs",
          "FACT_QUERY_ROLE_HOURLY": "role_hr", "FACT_QUERY_SCHEMA_HOURLY": "schema_hr"}
_OLD_TODO = ("    -- failure between the DELETEs (above) and these CALLs leaves a transient gap until the\n"
             "    -- next nightly run re-covers. The robust fix (build-into-staging + atomic SWAP, or one\n")
_NEW_TODO_LAST = "    -- The robust fix (build-into-staging + atomic SWAP, or one\n"
_MARTS_VERDICT = (
    "    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('HOURLY', 3);\n"
    "    SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));\n"
    "    IF (rv IS NOT NULL AND (rv ILIKE '%WITH ERRORS%' OR rv ILIKE '%FAIL%')) THEN fails := fails + 1; END IF;\n")
_DIAG_CALL = "    CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_OPS_DIAG(3);\n"
_EXT_LO_HOUR = ("DATEADD('hour', IFF(MIN(START_TIME) = DATE_TRUNC('hour', MIN(START_TIME)), 0, 1), "
                "DATE_TRUNC('hour', MIN(START_TIME)))")

# SP_LOAD_PATTERN_COST -- V120's MERGE head / tail, its two source filters and the freshness MERGE arms
_OLD_P_HEAD = "$$\nBEGIN\n    MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY t\n    USING (\n"
_NEW_P_INSERT = ("    INSERT INTO DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY\n"
                 "        (DAY, QUERY_HASH, COMPANY, DATABASE_NAME, RUNS, CREDITS_ATTRIBUTED, USERS_HLL)\n")
_OLD_P_TAIL = (
    "        GROUP BY 1, 2, 3, 4\n"
    "    ) s\n"
    "    ON t.DAY = s.DAY AND t.QUERY_HASH = s.QUERY_HASH AND t.COMPANY = s.COMPANY\n"
    "       AND t.DATABASE_NAME = s.DATABASE_NAME\n"
    "    WHEN MATCHED THEN UPDATE SET\n"
    "        t.RUNS = s.RUNS, t.CREDITS_ATTRIBUTED = s.CREDITS_ATTRIBUTED,\n"
    "        t.USERS_HLL = s.USERS_HLL, t.LOAD_TS = CURRENT_TIMESTAMP()\n"
    "    WHEN NOT MATCHED THEN INSERT (DAY, QUERY_HASH, COMPANY, DATABASE_NAME, RUNS, CREDITS_ATTRIBUTED, USERS_HLL)\n"
    "    VALUES (s.DAY, s.QUERY_HASH, s.COMPANY, s.DATABASE_NAME, s.RUNS, s.CREDITS_ATTRIBUTED, s.USERS_HLL);\n")
_NEW_P_TAIL = "        GROUP BY 1, 2, 3, 4;\n    COMMIT;\n"
_OLD_LO = "DATEADD('day', -1 * :DAYS_BACK, CURRENT_DATE())"
_OLD_P_ROWCOUNT = ("               (SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY) AS ROW_COUNT\n"
                   "    ) s\n")
_OLD_P_FRESH_TAIL = ("        STATUS = 'loader'\n"
                     "    WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)\n"
                     "    VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, 'loader');\n")
_NEW_P_SET = ("        COVERAGE_FROM = LEAST(COALESCE(t.COVERAGE_FROM, s.COVERAGE_FROM), "
              "COALESCE(s.COVERAGE_FROM, t.COVERAGE_FROM))\n")
_NEW_P_HANDLER = ("EXCEPTION   -- V167 (R2-010 D5): never leave the window deleted; re-raise so the task still "
                  "fails visibly\n    WHEN OTHER THEN\n        ROLLBACK;\n        RAISE;\n")


def _cut(text: str, start: str, end: str, keep_end: bool = True) -> str:
    """Remove text[start-anchor : end-anchor) (each anchor unique)."""
    assert text.count(start) == 1 and text.count(end) >= 1, (start[:60], end[:60])
    i = text.index(start)
    j = text.index(end, i)
    return text[:i] + (text[j:] if keep_end else text[j + len(end):])


def _swap1(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, (old[:80], text.count(old))
    return text.replace(old, new)


def _reverse_marts(p: str) -> str:
    """M1-M4 out, V159's text back in."""
    # M1: the five comment lines + the padded predicate pair -> V159's single predicate
    i = p.index("                        -- V167 (R2-015)")
    j = p.index(_NEW_QH_PRED, i) + len(_NEW_QH_PRED)
    p = p[:i] + _OLD_QH + p[j:]
    # M2
    i = p.index("                    -- V167 (R2-014): one lead-in day")
    j = p.index(_NEW_ATT, i) + len(_NEW_ATT)
    p = p[:i] + _OLD_ATT + p[j:]
    p = _cut(p, "                -- V167 (R2-014): only runs", "                WHERE DAY >= DATEADD('day', -:d, "
             "CURRENT_DATE())\n")
    p = _swap1(p, _NEW_RUNS, _OLD_RUNS)
    # M3
    p = _cut(p, "                -- V167 (R2-052)", _NEW_DAY9)
    p = _swap1(p, _NEW_DAY9, _OLD_DAY9)
    p = _swap1(p, _NEW_TS9, _OLD_TS9)
    # M4 (DAILY half only)
    h, d = p.split(_DAILY_SPLIT, 1)
    i = d.index("            STATUS = s.STATUS,\n")
    j = d.index(_NEW_MERGE_INSERT, i) + len(_NEW_MERGE_INSERT)
    block = d[i:j]
    assert block.count(_NEW_MERGE_SET) == 1 and block.count("            -- V167 (R1-016)") == 1
    d = d[:i] + _OLD_MERGE_TAIL + d[j:]
    return h + _DAILY_SPLIT + d


def _reverse_recon(p: str) -> str:
    """N1-N3 out, V064's text back in (the four DELETEs at their original places)."""
    i = p.index("    -- V167 (R2-018) MARK-AND-SWEEP.")
    j = p.index(_DIAG_CALL, i)
    p = p[:i] + p[j:]
    p = _swap1(p, "    -- V167 (R2-018): MART_WAREHOUSE_EFFICIENCY_DAILY, FACT_QUERY_ROLE_HOURLY, FACT_QUERY_SCHEMA_HOURLY "
               "and\n    -- MART_TASK_GRAPH_DAILY are no longer DELETEd here: they are mark-and-swept after the "
               "marts CALL below.\n", _OLD_DELETES["MART_WAREHOUSE_EFFICIENCY_DAILY"])
    after = {   # V064's order: each restored DELETE goes back right after the statement that preceded it
        "FACT_QUERY_ROLE_HOURLY": ("    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY\n"
                                   "     WHERE DAY >= DATEADD('day', -2, CURRENT_DATE());\n"),
        "MART_TASK_GRAPH_DAILY": ("    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_COST_ALLOC_XDIM_DAILY\n"
                                  "     WHERE DAY >= DATEADD('day', -2, CURRENT_DATE());\n"),
    }
    p = _swap1(p, after["FACT_QUERY_ROLE_HOURLY"], after["FACT_QUERY_ROLE_HOURLY"]
               + _OLD_DELETES["FACT_QUERY_ROLE_HOURLY"] + _OLD_DELETES["FACT_QUERY_SCHEMA_HOURLY"])
    p = _swap1(p, after["MART_TASK_GRAPH_DAILY"], after["MART_TASK_GRAPH_DAILY"] + _OLD_DELETES["MART_TASK_GRAPH_DAILY"])
    i = p.index("    -- failure between the DELETEs (above) and these CALLs leaves a gap that the next hourly run\n")
    j = p.index(_NEW_TODO_LAST, i) + len(_NEW_TODO_LAST)
    return p[:i] + _OLD_TODO + p[j:]


def _reverse_pattern(p: str) -> str:
    """P1-P5 out, V120's text back in."""
    i = p.index("$$\nDECLARE\n    lo DATE;")
    j = p.index(_NEW_P_INSERT, i) + len(_NEW_P_INSERT)
    p = p[:i] + _OLD_P_HEAD + p[j:]
    p = _swap1(p, _NEW_P_TAIL, _OLD_P_TAIL)
    assert p.count(">= :lo\n") == 2
    p = p.replace(">= :lo\n", f">= {_OLD_LO}\n")
    i = p.index("               (SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY) AS ROW_COUNT,\n")
    j = p.index("    ) s\n", i) + len("    ) s\n")
    p = p[:i] + _OLD_P_ROWCOUNT + p[j:]
    i = p.index("        STATUS = 'loader',\n")
    j = p.index("    VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, 'loader', s.COVERAGE_FROM);\n", i)
    j += len("    VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, 'loader', s.COVERAGE_FROM);\n")
    assert p[i:j].count(_NEW_P_SET) == 1
    p = p[:i] + _OLD_P_FRESH_TAIL + p[j:]
    return _swap1(p, _NEW_P_HANDLER, "")


# -- generation ----------------------------------------------------------------------------------------------

def _gen_env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.upper().endswith("_OUT")}
    env.update(extra)
    return env


def _run_gen(tmp_path: Path, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "outputs" / "gen_v167.py")], env=_gen_env(**extra),
                          cwd=tmp_path, capture_output=True, text=True)


@pytest.fixture(scope="module")
def extras(tmp_path_factory) -> dict[str, str]:
    tmp = tmp_path_factory.mktemp("v167_extras")
    paths = {k: tmp / f"{k}.sql" for k in ("pre", "part_b", "repair", "mig")}
    res = _run_gen(tmp, V167_OUT=str(paths["mig"]), PREFLIGHT_OUT=str(paths["pre"]),
                   PART_B_OUT=str(paths["part_b"]), REPAIR_OUT=str(paths["repair"]))
    assert res.returncode == 0, res.stderr
    assert paths["mig"].read_text(encoding="utf-8") == _MIG          # the extras never change the migration
    return {k: p.read_text(encoding="utf-8") for k, p in paths.items()}


def test_v167_regenerates_byte_identical_and_writes_nothing_else(tmp_path):
    out = tmp_path / "regen.sql"
    result = _run_gen(tmp_path, V167_OUT=str(out))
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == (ROOT / "snowflake" / "migrations" / _NAME).read_bytes(), (
        "V167 drifted from its forward-generation -- edit outputs/gen_v167.py, not the .sql.")
    assert b"\r" not in out.read_bytes()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["regen.sql"]     # extras only on request
    assert "PREFLIGHT" not in result.stdout and "PART B" not in result.stdout and "OWNER_REPAIRS" not in result.stdout


def test_v167_generator_reads_only_its_bases():
    gen = read("outputs/gen_v167.py")
    assert re.findall(r'MIG / "(V\d+__\w+\.sql)"', gen) == [
        "V159__loader_compile_diet.sql", "V064__webhook_drain_watermarks_alert_burn_telemetry.sql",
        "V120__pattern_cost_runs_fanout_fix.sql"]
    assert "import app" not in gen and "from app" not in gen


@pytest.mark.parametrize("which", ["pre", "part_b"])
def test_v167_preflight_and_part_b_are_read_only_and_parse(extras, which):
    sql = extras[which]
    assert sql.split("\n", 2)[1] == "ALTER SESSION SET TIMEZONE = 'America/Chicago';"    # Central first
    code = _strip_noise(sql.replace("ALTER SESSION SET TIMEZONE = 'America/Chicago';", "", 1))
    for banned in ("INSERT", "UPDATE", "DELETE", "MERGE", "CALL", "CREATE", "ALTER", "DROP", "TRUNCATE",
                   "GRANT", "REVOKE", "EXECUTE"):
        assert not re.search(rf"\b{banned}\b", code, re.I), (which, banned)
    assert not re.search(r"(?<![:\w]):[A-Za-z_]\w*", code), "a scripting :bind survived"
    assert "$$" not in sql and "@" not in sql
    sqlglot = pytest.importorskip("sqlglot")
    from sqlglot import exp
    parsed = [p for p in sqlglot.parse(sql, dialect="snowflake") if p is not None]
    assert len(parsed) == 5
    writes = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop)
    for tree in parsed[1:]:
        assert tree.key == "select" or isinstance(tree, exp.Union), tree.key
        assert not [type(n).__name__ for n in tree.walk() if isinstance(n, writes)]


def test_v167_preflight_previews_the_migrations_own_twin_delete(extras):
    """P167.1 is the in-migration DELETE's USING / WHERE as a SELECT; PART B V167.3 re-runs it after the apply."""
    using = _between(_MIG, "DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY t\nUSING (\n", ") g\nWHERE ")
    using = using[len("DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY t\nUSING (\n"):]
    where = _between(_MIG, ") g\nWHERE t.DAY = g.DAY", ";\n")[len(") g\nWHERE "):]
    for text in (extras["pre"], extras["part_b"]):
        assert f"JOIN (\n{using}) g\n  ON {where};" in text
    assert extras["pre"].count("-- P167.") == 4 and extras["part_b"].count("-- V167.") == 4


def test_v167_owner_repairs_pin_central_and_rebuild_from_the_arm_text(extras):
    rep = extras["repair"]
    first = next(ln for ln in rep.splitlines() if ln.strip() and not ln.lstrip().startswith("--"))
    assert first == "ALTER SESSION SET TIMEZONE = 'America/Chicago';"       # correction 5: before any repair
    order = [rep.index(s) for s in (
        "-- ---- step 0", "-- ---- step 1", "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('DAILY', 365);",
        "-- ---- step 2", "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_PATTERN_COST(364);", "-- ---- step 3",
        "ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY SUSPEND;",
        "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(90);",
        "CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('HOURLY', 90);",
        "ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY RESUME;",
        "SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY');", "-- ---- step 4")]
    assert order == sorted(order)
    # step 1: reload THEN prune, gated on the ai_code token and on touched rows (never DELETE first)
    s1 = _between(rep, "-- ---- step 1", "-- ---- step 2")
    assert s1.index("CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('DAILY', 365);") < s1.index(
        "DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_AI_USAGE_DAILY")
    assert "NOT ARRAY_CONTAINS('ai_code'::VARIANT, SPLIT(:rv, ' '))" in s1 and "IF (touched > 0) THEN" in s1
    assert "WHERE SOURCE IN ('Snowsight', 'CLI') AND DAY > :lo AND LOAD_TS < :t0;" in s1
    assert "t0 := CURRENT_TIMESTAMP()::TIMESTAMP_NTZ;" in s1
    # step 4: an anonymous block; its INSERT source is V167's arm [6] USING text with :d -> 364
    s4 = rep[rep.index("-- ---- step 4"):]
    arm = _between(_M, _ARM6[0], _ARM6[1])
    src = _between(arm, "            USING (\n", "\n            ) s\n")[len("            USING (\n"):]
    assert src.count(":d") == 4 and src.replace(":d", "364") in s4
    assert s4.count("EXECUTE IMMEDIATE $$") == 1 and s4.rstrip().endswith("$$;")
    blk = _between(s4, "EXECUTE IMMEDIATE $$\nBEGIN\n", "$$;")
    order = [blk.index(s) for s in ("    BEGIN TRANSACTION;\n",
                                    "    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY\n"
                                    "     WHERE DAY >= DATEADD('day', -364, CURRENT_DATE());\n",
                                    "    INSERT INTO DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY\n",
                                    "    COMMIT;\n", "EXCEPTION\n    WHEN OTHER THEN\n        ROLLBACK;\n        RAISE;\n")]
    assert order == sorted(order)
    # every repair block is one EXECUTE IMMEDIATE per CALL (one timeout never spans two loaders)
    for blk_text in re.findall(r"EXECUTE IMMEDIATE \$\$\n(.*?)\$\$;", rep, re.S):
        assert len(re.findall(r"^\s*CALL ", blk_text, re.M)) <= 1


@pytest.mark.parametrize(("call", "label"), [
    ("DBA_MAINT_DB.OVERWATCH.SP_LOAD_PATTERN_COST(364)", "SP_LOAD_PATTERN_COST(364)"),
    ("DBA_MAINT_DB.OVERWATCH.SP_LOAD_QH_EXTRACT(90)", "SP_LOAD_QH_EXTRACT(90)"),
    ("DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('HOURLY', 90)", "SP_LOAD_MARTS_V27(HOURLY, 90)"),
])
def test_v167_owner_repair_call_blocks_carry_the_backfill_365_handler(extras, call, label):
    """review: the step-2/3 CALL blocks follow backfill_365's R1-231 idiom IN FULL -- a raised error (not only a
    failure verdict) is caught, logged to APP_ERROR_LOG and returned as a 'FAILED: ...' pane, so a Snowsight Run
    All still reaches step 3's RESUME + SYSTEM$TASK_DEPENDENTS_ENABLE instead of stranding TASK_LOAD_HOURLY
    (hourly loads, alert scans and delivery) suspended. Only a timeout or a Stop escapes (the !! note says so)."""
    rep = extras["repair"]
    blocks = [b for b in re.findall(r"EXECUTE IMMEDIATE \$\$\n(.*?)\$\$;", rep, re.S) if f"CALL {call};" in b]
    assert len(blocks) == 1, call
    blk = blocks[0]
    assert "    rv VARCHAR;\n    emsg VARCHAR;\n" in blk
    verdict, handler = blk.split("\nEXCEPTION\n", 1)
    assert ("INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)\n"
            f"        SELECT 'OwnerRepairV167', 'owner_repair_verdict_failed', LEFT(COALESCE(:rv, 'no verdict "
            f"returned'), 2000), '{label}', CURRENT_ROLE();\n"
            f"        RETURN 'FAILED: {label} -> ' || COALESCE(rv, 'no verdict returned');") in verdict
    assert handler == (
        "    WHEN OTHER THEN\n"
        "        emsg := SQLERRM;\n"
        "        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)\n"
        f"        SELECT 'OwnerRepairV167', 'owner_repair_call_failed', LEFT(:emsg, 2000), '{label}', CURRENT_ROLE();\n"
        f"        RETURN 'FAILED: {label} - ' || emsg;\n"
        "END;\n")
    # the step-3 CALLs sit inside the suspend window, so a caught failure still runs the RESUME after them
    if "PATTERN" not in call:
        s3 = _between(rep, "-- ---- step 3", "-- ---- step 4")
        assert (s3.index("TASK_LOAD_HOURLY SUSPEND;") < s3.index(f"CALL {call};")
                < s3.index("TASK_LOAD_HOURLY RESUME;"))
    # the docstring's claim is now true: the generator's block shape is backfill_365's handler shape
    bf = read("snowflake/backfill_365.sql")
    assert "EXCEPTION\n    WHEN OTHER THEN\n        emsg := SQLERRM;\n        INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG" in bf


def test_v167_owner_repair_sql_parses(extras):
    """The plain read-only probes parse; the scripting blocks are Snowsight-only (dialect gap)."""
    sqlglot = pytest.importorskip("sqlglot")
    outside = "".join(p for i, p in enumerate(extras["repair"].split("$$")) if i % 2 == 0)
    from tests.test_migrations_parse import _plain_statements
    stmts = list(_plain_statements(outside.replace("EXECUTE IMMEDIATE ", "-- ")))
    assert len([s for s in stmts if s.upper().startswith("SELECT")]) == 5      # 4 probes + DEPENDENTS_ENABLE
    for s in stmts:
        sqlglot.parse_one(s, read="snowflake")


# -- guard, order, shape -------------------------------------------------------------------------------------

def test_v167_first_line_guard_and_version():
    assert _MIG.startswith(f"-- {_NAME}\n")
    guard165 = _between(_V165, "EXECUTE IMMEDIATE\n$$\n", "$$;\n")          # the house guard, numbers moved
    want = (guard165.replace("-20165", "-20167").replace("'V165 requires V164 first", "'V167 requires V166 first")
            .replace("IF (v < 164)", "IF (v < 166)"))
    assert _between(_MIG, "EXECUTE IMMEDIATE\n$$\n", "$$;\n") == want
    assert "not_ready EXCEPTION (-20167, 'V167 requires V166 first - apply migrations in order.');" in _MIG
    assert _MIG.count("EXECUTE IMMEDIATE") == 1
    assert _MIG.rstrip().endswith("WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION "
                                  "WHERE VERSION = 167);")
    assert "SELECT 167 AS VERSION" in _MIG
    assert migration_tip() >= 167
    header = _MIG[:_MIG.index("EXECUTE IMMEDIATE")]
    for word in ("WHY:", "COST:", "FIRST RUN:", "ROLLBACK:", "Apply AFTER V166. Idempotent; safe to re-run."):
        assert word in header, word


def test_v167_file_order_and_statements():
    from tests.test_migrations_parse import _plain_statements
    pos = [_MIG.index(s) for s in (
        "EXCEPTION (-20167", _ALTER,
        _MARKERS["SP_LOAD_MARTS_V27"], _MARKERS["SP_NIGHTLY_RECONCILE"], _MARKERS["SP_LOAD_PATTERN_COST"],
        "DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY t\nUSING (",
        "INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION")]
    assert pos == sorted(pos)
    for proc, marker in _MARKERS.items():
        assert _MIG.count(marker) == 1
        assert _MIG[_MIG.index(marker) + len(marker):].startswith(
            f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{proc}(")
    kinds = [re.sub(r"^(?:--[^\n]*\n)+", "", s.strip()).split(None, 3)[:3] for s in _plain_statements(_MIG)]
    assert kinds == [["DELETE", "FROM", "DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY"],
                     ["INSERT", "INTO", "DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION"]], kinds
    assert _MIG.count("CREATE OR REPLACE PROCEDURE") == 3 and _MIG.count("$$") == 8
    assert _MIG.count(_ALTER) == 1
    top = _strip_noise("".join(p for i, p in enumerate(_MIG.split("$$")) if i % 2 == 0))
    for banned in ("CALL", "DROP", "TASK", "GRANT", "REVOKE", "TRUNCATE", "UPDATE", "MERGE", "VIEW", "FUNCTION"):
        assert not re.search(rf"\b{banned}\b", top), banned
    assert len(re.findall(r"\bALTER\b", top)) == 1 and len(re.findall(r"\bDELETE\b", top)) == 1
    assert not re.search(r"\$[A-Za-z_][A-Za-z0-9_]*\$", _MIG) and "\r" not in _MIG and "@" not in _MIG


def test_v167_description_fits_and_has_no_apostrophe():
    desc = re.search(r"SELECT 167 AS VERSION,\n       '(.*)' AS DESCRIPTION\n", _MIG, re.S).group(1)
    assert "\n" not in desc and "'" not in desc and len(desc) <= 4000
    for frag in ("re-derived from V159", "re-derived from V064", "re-derived from V120", "COVERAGE_FROM",
                 "nothing is CALLed at apply time"):
        assert frag in desc, frag


def test_v167_never_updates_source_freshness_state():
    """correction 1: COVERAGE_FROM rides the EXISTING freshness MERGEs (tests/test_freshness_coverage.py allows a
    point UPDATE of SOURCE_FRESHNESS_STATE only in the two alert scans)."""
    assert not re.search(r"UPDATE\s+DBA_MAINT_DB\.OVERWATCH\.SOURCE_FRESHNESS_STATE", _MIG)
    for body in (_M, _P):
        assert re.search(r"MERGE INTO DBA_MAINT_DB\.OVERWATCH\.SOURCE_FRESHNESS_STATE t.*COVERAGE_FROM", body, re.S)


# -- lineage + round 13 ------------------------------------------------------------------------------------

def test_v167_markers_name_the_current_definers():
    from tests.test_proc_lineage import _HISTORICAL_WAIVERS, _definers, _lineage, _migrations, _violations
    texts = _migrations()
    rows = {(r["v"], r["proc"]): r for r in _lineage(texts)}
    defs = _definers(texts)
    for proc, base in _BASES.items():
        r = rows[(167, proc)]
        assert r["src"] == "marker" and r["claims"] == [base] and r["prev"] == base and not r["waived"], r
        assert [v for v in defs[proc] if base < v <= 167] == [167], (proc, defs[proc])
    assert not [v for v in _violations(texts, _HISTORICAL_WAIVERS) if v.startswith("V167 ")]
    assert "LINEAGE-WAIVER" not in _MIG and _MIG.count("-- >>> derived:") == 3


_NEW = {"SP_LOAD_MARTS_V27": _M, "SP_NIGHTLY_RECONCILE": _R, "SP_LOAD_PATTERN_COST": _P}
_OLD = {"SP_LOAD_MARTS_V27": _M159, "SP_NIGHTLY_RECONCILE": _R064, "SP_LOAD_PATTERN_COST": _P120}
_REVERSE = {"SP_LOAD_MARTS_V27": _reverse_marts, "SP_NIGHTLY_RECONCILE": _reverse_recon,
            "SP_LOAD_PATTERN_COST": _reverse_pattern}


@pytest.mark.parametrize("proc", sorted(_NEW))
def test_v167_procs_normalize_back_to_their_bases_byte_for_byte(proc):
    assert _REVERSE[proc](_NEW[proc]) == _OLD[proc]


@pytest.mark.parametrize("proc,victim", [
    ("SP_LOAD_MARTS_V27", "AS IDLE_CREDITS"),
    ("SP_LOAD_MARTS_V27", "'MART_TASK_NODE_DAILY - other marts unaffected'"),
    ("SP_LOAD_MARTS_V27", "f.START_TIME::DATE AS DAY"),
    ("SP_NIGHTLY_RECONCILE", "WHERE DAY >= DATEADD('day', -2, CURRENT_DATE());"),
    ("SP_NIGHTLY_RECONCILE", "'RECONCILE OK - nightly reconcile complete"),
    ("SP_LOAD_PATTERN_COST", "HLL_ACCUMULATE(q.USER_NAME)"),
    ("SP_LOAD_PATTERN_COST", "GENERATION = COALESCE(t.GENERATION, 0) + 1"),
])
def test_v167_normalize_check_has_teeth(proc, victim):
    """Dropping one character anywhere outside the declared deltas breaks the byte compare."""
    new = _NEW[proc]
    assert new.count(victim) >= 1
    assert _REVERSE[proc](new.replace(victim, victim[:-1], 1)) != _OLD[proc]


# -- SP_LOAD_MARTS_V27 deltas ------------------------------------------------------------------------------

def test_v167_arm1_pads_only_the_span_source():
    arm = _between(_M, *_ARM1)
    qh = _between(arm, "                qh AS (", "                q_active AS (")
    assert _NEW_QH_PRED in qh and qh.count("START_TIME >= DATEADD") == 1
    assert arm.count("WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())") == 2          # m and q
    assert arm.count("WHERE mh.START_TIME >= DATEADD('day', -:d, CURRENT_DATE())") == 1       # m_idle
    assert arm.count("GENERATOR(ROWCOUNT => 25)") == 1
    assert _M.count("IF (d > 2 OR MOD(ct_hour, 4) = 0) THEN   -- V159 (D5) gate [1]") == 1
    assert "q_active AS (\n                    SELECT WAREHOUSE_NAME, DAY, COUNT(DISTINCT HOUR_TS) AS ACTIVE_HOURS\n" \
           "                    FROM qh\n                    GROUP BY 1, 2\n" in arm         # the prune is not taken


def test_v167_arm6_lead_in_day_and_run_filter_only():
    arm = _between(_M, *_ARM6)
    assert arm.count(_NEW_ATT) == 1 and _OLD_ATT not in arm
    assert arm.count("WHERE START_TIME >= DATEADD('day', -:d - 1, CURRENT_DATE())") == 1      # QAH unchanged
    assert arm.count("WHERE QUERY_START_TIME >= DATEADD('day', -:d, CURRENT_DATE())") == 1    # IN-list unchanged
    runs = _between(arm, "                FROM runs\n", "            ) s\n")
    assert runs.index("WHERE DAY >= DATEADD('day', -:d, CURRENT_DATE())") < runs.index("GROUP BY 1, 2, 3, 4")
    assert "DATE(MIN(QUERY_START_TIME)) AS DAY" in arm                                      # DAY = root's start day
    # arm [6b] is V159's, byte for byte
    node = ("        -- [6b] per-node task timing", "loaded := loaded || 'task_node ';")
    assert _between(_M, *node) == _between(_M159, *node)


def test_v167_arm9_keys_central_days_and_leaves_functions_alone():
    arm = _between(_M, *_ARM9)
    assert _NEW_DAY9 in arm and _NEW_TS9 in arm
    assert not re.search(r"(?<!CONVERT_TIMEZONE\('America/Chicago', )c\.USAGE_TIME::", arm)
    assert "MIN(c.USAGE_TIME)::" not in arm and "MAX(c.USAGE_TIME)::" not in arm
    assert "GROUP BY 1, 2, 3\n" in arm
    assert arm.count("WHERE USAGE_TIME >= DATEADD('day', -:d, CURRENT_DATE())") == 2         # R2-017 hunk NOT taken
    fn = _between(_M, "                SELECT f.START_TIME::DATE AS DAY,", "loaded := loaded || 'ai_functions ';")
    assert fn == _between(_M159, "                SELECT f.START_TIME::DATE AS DAY,",
                          "loaded := loaded || 'ai_functions ';")


def test_v167_coverage_from_rides_only_the_daily_merge():
    hourly, daily = _M.split(_DAILY_SPLIT, 1)
    assert "COVERAGE_FROM" not in hourly
    assert hourly.count(_OLD_MERGE_TAIL) == 1                                            # HOURLY MERGE untouched
    merge = _between(daily, "        MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t", "    END IF;")
    assert _NEW_MERGE_SET in merge and _NEW_MERGE_INSERT in merge
    # the HAVING (V066 #23) is the both-AI-arms gate -- unchanged, exactly once in the proc
    assert _M.count("HAVING COUNT(*) = COUNT_IF(ARRAY_CONTAINS(m.TOKEN::VARIANT, SPLIT(:loaded, ' ')))") == 1
    assert ("                    ('FACT_AI_USAGE_DAILY', 'ai_code'),\n"
            "                    ('FACT_AI_USAGE_DAILY', 'ai_functions')\n") in merge
    # the srcmap VALUES of both scopes are V159's
    for half_new, half_old in zip(_M.split(_DAILY_SPLIT, 1), _M159.split(_DAILY_SPLIT, 1), strict=True):
        assert _between(half_new, "FROM VALUES", "AS srcmap") == _between(half_old, "FROM VALUES", "AS srcmap")


# -- SP_NIGHTLY_RECONCILE deltas ---------------------------------------------------------------------------

def test_v167_reconcile_sweeps_after_the_marts_verdict_and_before_ops_diag():
    call, diag = _R.index(_MARTS_VERDICT), _R.index(_DIAG_CALL)
    for table, token in _SWEPT.items():
        hits = [m.start() for m in re.finditer(rf"DELETE FROM DBA_MAINT_DB\.OVERWATCH\.{table}\n", _R)]
        assert len(hits) == 1 and call < hits[0] < diag, table
        stmt = _R[hits[0]:_R.index(";", hits[0])]
        assert "AND LOAD_TS < :recon_start" in stmt
        assert f"AND ARRAY_CONTAINS('{token}'::VARIANT, SPLIT(:rv, ' '))" in stmt
    first_call = _R.index("CALL DBA_MAINT_DB.OVERWATCH.")
    assert len(re.findall(r"DELETE FROM DBA_MAINT_DB\.OVERWATCH\.", _R[:first_call])) == 7   # the other seven
    for t in ("FACT_WAREHOUSE_DAILY", "FACT_METERING_DAILY", "MART_QUERY_FAMILY_DAILY", "FACT_QUERY_DAILY",
              "MART_TAG_COVERAGE_DAILY", "MART_COST_ALLOCATION_DAILY", "FACT_COST_ALLOC_XDIM_DAILY"):
        assert f"DELETE FROM DBA_MAINT_DB.OVERWATCH.{t}\n" in _R[:first_call], t
    assert "transient gap" not in _R
    assert _between(_R, "    RETURN 'RECONCILE WITH ERRORS", "END;") == _between(_R064, "    RETURN 'RECONCILE WITH ERRORS",
                                                                                     "END;")


def test_v167_sweep_tokens_are_the_loaders_own_and_unambiguous():
    """Each sweep's token is the :loaded token of the arm whose MERGE writes that table, in V167's own loader,
    and the srcmap maps it to the same table. SPLIT(:rv, ' ') + ARRAY_CONTAINS is exact element membership,
    and the verdict's other words ('MARTS', 'OK', '(HOURLY,', '3d):') are never a token; still, no swept token
    is a substring of any other token or verdict word, so a future ILIKE rewrite could not cross-match."""
    tokens = re.findall(r"loaded := loaded \|\| '(\w+) ';", _M)
    assert len(tokens) == len(set(tokens)) == 13
    for table, token in _SWEPT.items():
        assert f"('{table}', '{token}')" in _M
        merge = _M.index(f"MERGE INTO DBA_MAINT_DB.OVERWATCH.{table} t")
        nxt = _M.index("loaded := loaded || '", merge)
        assert _M[nxt:].startswith(f"loaded := loaded || '{token} ';"), table
        verdict_words = ("MARTS", "OK", "WITH", "ERRORS:", "required,", "optional", "(HOURLY,", "3d):")
        assert not [b for b in tokens + list(verdict_words) if b != token and token in b], token


def test_v167_load_ts_invariant_the_sweep_and_twin_delete_stand_on():
    """Every MERGE the sweep trusts stamps LOAD_TS = CURRENT_TIMESTAMP() on UPDATE and leaves it to the column
    DEFAULT on INSERT; each table's DDL defaults it; no later migration ALTERs it."""
    for table in _SWEPT:
        merge = _between(_M, f"MERGE INTO DBA_MAINT_DB.OVERWATCH.{table} t", "loaded := loaded ||")
        matched = _between(merge, "WHEN MATCHED THEN UPDATE SET", "WHEN NOT MATCHED")
        assert "LOAD_TS = CURRENT_TIMESTAMP()" in matched, table
        insert_cols = _between(merge, "WHEN NOT MATCHED THEN INSERT", "VALUES")
        assert "LOAD_TS" not in insert_cols, table
    ddl = {"MART_WAREHOUSE_EFFICIENCY_DAILY": "snowflake/migrations/V027__mart_family.sql",
           "FACT_QUERY_ROLE_HOURLY": "snowflake/migrations/V027__mart_family.sql",
           "FACT_QUERY_SCHEMA_HOURLY": "snowflake/migrations/V027__mart_family.sql",
           "MART_TASK_GRAPH_DAILY": "snowflake/migrations/V045__task_monitoring_restored.sql",
           "MART_PATTERN_COST_DAILY": "snowflake/migrations/V037__pattern_env_grain.sql"}
    for table, rel in ddl.items():
        body = _between(read(rel), f"DBA_MAINT_DB.OVERWATCH.{table} (", ");")
        assert "LOAD_TS TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()" in body, table
    for p in (ROOT / "snowflake" / "migrations").glob("V*.sql"):
        assert not re.search(r"ALTER\s+TABLE\s+[\w.]*(?:" + "|".join(ddl) + r")\b[^;]*\bLOAD_TS\b",
                             p.read_text(encoding="utf-8")), p.name
    # V120's MERGE re-stamped LOAD_TS on MATCHED (the twin DELETE's premise)
    assert "t.USERS_HLL = s.USERS_HLL, t.LOAD_TS = CURRENT_TIMESTAMP()" in _P120


def test_v167_hour_sweep_bound_is_ext_lo_hour_without_its_fallback():
    assert _M.count(_EXT_LO_HOUR) == 1                                       # the arm's own expression
    for table in ("FACT_QUERY_ROLE_HOURLY", "FACT_QUERY_SCHEMA_HOURLY"):
        stmt = _between(_R, f"DELETE FROM DBA_MAINT_DB.OVERWATCH.{table}\n", ";")
        assert (f"WHERE HOUR_TS >= (SELECT GREATEST(DATEADD('day', -3, CURRENT_DATE()),\n"
                f"                              {_EXT_LO_HOUR})\n"
                "                       FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT)") in stmt
        assert "COALESCE" not in stmt
    for table in ("MART_WAREHOUSE_EFFICIENCY_DAILY", "MART_TASK_GRAPH_DAILY"):
        stmt = _between(_R, f"DELETE FROM DBA_MAINT_DB.OVERWATCH.{table}\n", ";")
        assert "WHERE DAY >= DATEADD('day', -3, CURRENT_DATE())" in stmt     # the reload's d=3 lower edge


# -- SP_LOAD_PATTERN_COST deltas ---------------------------------------------------------------------------

def test_v167_pattern_loader_is_one_atomic_window_replace():
    assert "MERGE INTO DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY" not in _P
    assert _P.count(_OLD_LO) == 1 and "    lo := DATEADD('day', -1 * :DAYS_BACK, CURRENT_DATE());\n" in _P
    assert _P.count(">= :lo") == 3
    order = [_P.index(s) for s in (
        "    BEGIN TRANSACTION;\n",
        "    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY\n     WHERE DAY >= :lo;\n",
        _NEW_P_INSERT, "        GROUP BY 1, 2, 3, 4;\n    COMMIT;\n",
        "    MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t\n", "    RETURN 'OK';\n", _NEW_P_HANDLER)]
    assert order == sorted(order)
    txn = _between(_P, "BEGIN TRANSACTION;", "COMMIT;")
    assert not re.search(r"\b(CREATE|ALTER|DROP|TRUNCATE)\b", _strip_noise(txn))
    assert "COMPANY_FOR_WAREHOUSE(g.WAREHOUSE_NAME) AS COMPANY" in _P                  # V030 shape kept
    assert ":lo AS COVERAGE_FROM" in _P and _NEW_P_SET in _P
    assert "VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, 'loader', s.COVERAGE_FROM);" in _P


def test_v167_twin_delete_groups_without_company_and_keeps_single_rows():
    stmt = _between(_MIG, "DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_PATTERN_COST_DAILY t\nUSING (", ";\n")
    assert "GROUP BY DAY, QUERY_HASH, DATABASE_NAME\n" in stmt and "COMPANY" not in stmt
    assert "HAVING COUNT(*) > 1" in stmt and "AND t.LOAD_TS < DATEADD('minute', -10, g.NEWEST_TS)" in stmt
    assert "ACCOUNT_USAGE" not in stmt                                           # scan-free


def test_v167_plain_statements_parse():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    stmts = list(_plain_statements(_MIG))
    assert len(stmts) == 2
    for s in stmts:
        sqlglot.parse_one(s, read="snowflake")
    with pytest.raises(sqlglot.errors.ParseError):                              # the parse has teeth
        sqlglot.parse_one(stmts[0].replace("HAVING COUNT(*) > 1", "HAVING COUNT(*) > (1"), read="snowflake")


# -- behaviour models (pure Python on the predicates the arms carry) ----------------------------------------

def _left_edge(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=_CT)


def _span_hours(start: datetime, end: datetime) -> list[datetime]:
    """Arm [1]'s qh expansion: hours H0 .. H0 + 24 (the 25-row GENERATOR), kept while <= H1. Hours are taken on
    the UTC instant line (DATEADD('hour', n, ts) adds real hours) and keyed by their Central wall clock."""
    h0 = start.astimezone(_CT).replace(minute=0, second=0, microsecond=0)
    h1 = end.astimezone(_CT).replace(minute=0, second=0, microsecond=0)
    out = []
    for n in range(25):
        h = (h0.astimezone(ZoneInfo("UTC")) + timedelta(hours=n)).astimezone(_CT)
        if h <= h1:
            out.append(h)
    return out


def _active_hours_on(day: date, queries: list[tuple[datetime, datetime]], pad: bool) -> set[datetime]:
    lo = _left_edge(day)
    src_lo = _left_edge(day - timedelta(days=1)) if pad else lo
    kept = [(s, e) for s, e in queries if s >= src_lo and (not pad or e >= lo)]
    return {h for s, e in kept for h in _span_hours(s, e) if h.date() == day}


def test_v167_r2_015_model_counts_the_carry_over_hours():
    # the arm carries exactly these predicates (V159 vs V167)
    assert _OLD_QH in _between(_M159, *_ARM1) and _NEW_QH_PRED in _between(_M, *_ARM1)
    day = date(2026, 9, 20)
    nightly = [(datetime(2026, 9, 19, 23, 0, tzinfo=_CT), datetime(2026, 9, 20, 2, 30, tzinfo=_CT))]
    billed = {_left_edge(day) + timedelta(hours=h) for h in range(3)}                  # 00, 01, 02 metered
    idle_v159 = billed - _active_hours_on(day, nightly, pad=False)
    idle_v167 = billed - _active_hours_on(day, nightly, pad=True)
    assert len(idle_v159) == 3 and idle_v167 == set()
    # a prior-day query that ends before the left edge is dropped by the END_TIME floor (no new output row)
    early = [(datetime(2026, 9, 19, 9, 0, tzinfo=_CT), datetime(2026, 9, 19, 10, 0, tzinfo=_CT))]
    assert _active_hours_on(day, early, pad=True) == set()


def test_v167_r2_015_one_day_pad_reaches_every_left_edge_hour_but_the_spring_forward_case():
    """A year of left edges: any query that reaches day D-d within the 25-hour span cap started on D-d-1 or
    later -- except a 24h+ query starting in hour 23 two days before a left edge that follows the 23-hour
    spring-forward day."""
    misses = []
    for k in range(365):
        day = date(2026, 1, 1) + timedelta(days=k)
        lo = _left_edge(day)
        for back in (2, 3):
            for hour in range(24):
                start = datetime.combine(day - timedelta(days=back), datetime.min.time()).replace(hour=hour,
                                                                                                    tzinfo=_CT)
                reach = [h for h in _span_hours(start, start + timedelta(hours=30)) if h >= lo and h.date() == day]
                if reach:
                    misses.append((day, back, hour))
    assert misses == [(date(2026, 3, 9), 2, 23)], misses


def _graph_attempts() -> list[dict]:
    """TASK_HISTORY rows: a graph whose root starts 23:50 on Y-1 with children after midnight (one failing),
    the same graph's ordinary same-day run, and a root that fails and auto-retries after midnight."""
    y = date(2026, 9, 20)

    def at(d: date, h: int, m: int) -> datetime:
        return datetime(d.year, d.month, d.day, h, m)

    prev = y - timedelta(days=1)
    rows = [
        {"run": "G1", "name": "ROOT", "start": at(prev, 23, 50), "state": "SUCCEEDED", "credits": 1.0},
        {"run": "G1", "name": "CHILD_A", "start": at(y, 0, 5), "state": "SUCCEEDED", "credits": 2.0},
        {"run": "G1", "name": "CHILD_B", "start": at(y, 1, 30), "state": "FAILED", "credits": 4.0},
        {"run": "G2", "name": "ROOT", "start": at(y, 9, 0), "state": "SUCCEEDED", "credits": 8.0},
        {"run": "G2", "name": "CHILD_A", "start": at(y, 9, 10), "state": "SUCCEEDED", "credits": 16.0},
        {"run": "G3", "name": "ROOT", "start": at(prev, 23, 55), "state": "FAILED", "credits": 32.0},
        {"run": "G3", "name": "ROOT", "start": at(y, 0, 5), "state": "SUCCEEDED", "credits": 64.0},
    ]
    return rows


def _arm6(rows: list[dict], today: date, d: int, fixed: bool) -> dict[tuple[date, str], dict]:
    """Arm [6] with the predicates the arm text carries: attempts from today-d (V159) or today-d-1 (V167);
    runs keyed by DAY = DATE(MIN(start)) and PIPELINE = MIN_BY(name, start); V167 keeps DAY >= today-d."""
    lo = today - timedelta(days=d + (1 if fixed else 0))
    att = [r for r in rows if r["start"].date() >= lo]
    runs: dict[str, list[dict]] = {}
    for r in att:
        runs.setdefault(r["run"], []).append(r)
    out: dict[tuple[date, str], dict] = {}
    for rs in runs.values():
        first = min(rs, key=lambda r: r["start"])
        day = first["start"].date()
        if fixed and day < today - timedelta(days=d):
            continue
        # terminal attempt per (task) -- the G3 root retry collapses to one TASK_RUN
        terminal = {r["name"]: r for r in sorted(rs, key=lambda r: r["start"])}
        row = out.setdefault((day, first["name"]), {"GRAPH_RUNS": 0, "FAILED_RUNS": 0, "TASK_RUNS": 0, "CR": 0.0})
        row["GRAPH_RUNS"] += 1
        row["FAILED_RUNS"] += int(any(r["state"] == "FAILED" for r in terminal.values()))
        row["TASK_RUNS"] += len(terminal)
        row["CR"] += sum(r["credits"] for r in rs)
    return out


def _replay(rows: list[dict], fixed: bool) -> dict[tuple[date, str], dict]:
    """6 days of the real write schedule: hourly d=2 MERGEs (the gated cycles), then each 06:45 reconcile
    DELETEs (V064 shape; under V167 the sweep removes exactly the keys the reload did not produce) DAY >= D-3
    and MERGEs d=3. MERGE = upsert by (DAY, PIPELINE)."""
    mart: dict[tuple[date, str], dict] = {}
    start = date(2026, 9, 18)
    for k in range(7):
        today = start + timedelta(days=k)
        visible = [r for r in rows if r["start"].date() < today or r["start"].hour < 6]
        mart.update(_arm6(visible, today, 2, fixed))
        lo = today - timedelta(days=3)
        for key in [key for key in mart if key[0] >= lo]:
            del mart[key]
        mart.update(_arm6(visible, today, 3, fixed))
    return mart


def test_v167_r2_014_model_drops_the_phantom_and_keeps_every_credit():
    att, runs = _between(_M, *_ARM6), _between(_M159, *_ARM6)
    assert _NEW_ATT in att and "WHERE DAY >= DATEADD('day', -:d, CURRENT_DATE())" in att and _OLD_ATT in runs
    rows = _graph_attempts()
    total = sum(r["credits"] for r in rows)
    y, prev = date(2026, 9, 20), date(2026, 9, 19)
    v167 = _replay(rows, fixed=True)
    v159 = _replay(rows, fixed=False)
    # V167: no child-named row, the straddling run sits whole on its root day, nothing double counts
    assert {k[1] for k in v167} == {"ROOT"}
    assert v167[(prev, "ROOT")]["GRAPH_RUNS"] == 2 and v167[(prev, "ROOT")]["CR"] == 1.0 + 2.0 + 4.0 + 32.0 + 64.0
    assert v167[(prev, "ROOT")]["FAILED_RUNS"] == 1                       # CHILD_B once; G3's root retried OK
    assert v167[(y, "ROOT")]["CR"] == 8.0 + 16.0
    assert sum(r["CR"] for r in v167.values()) == total
    # V159 reproduces the defect: the left-edge reload re-keys G1's children as a phantom (Y, CHILD_A) row
    # and G3's retry as a second (Y, ROOT) run -- credits counted twice
    assert (y, "CHILD_A") in v159 and sum(r["CR"] for r in v159.values()) > total
    # runs fully inside the window come out identical with and without the fix
    inside = [r for r in rows if r["run"] == "G2"]
    assert _replay(inside, fixed=True) == _replay(inside, fixed=False)
