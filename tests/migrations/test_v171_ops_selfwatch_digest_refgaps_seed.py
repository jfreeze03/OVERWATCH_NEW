"""Locks for V171 -- ops self-watch honesty, the complete-day warehouse digest, isolated ref-gap checks and the
CREDIT_PRICE_OVERRIDE seed (round 2: R2-026, R1-228, CORTEX-NULLIF digest half, R2-019 + R2-104, CREDIT-PRICE-SEED).

STRUCTURE + GENERATION + LOCKS; the permanent forward checks (they read the LATEST bodies) live in
tests/test_digest_grounding_parity.py (the facts window run in sqlite, the warehouse keys binding) and
tests/test_etl_control_sql.py (the alert's per-check SQL equals the panel's). What this file proves, from the
shipped text:
  * generation -- outputs/gen_v171.py regenerates the migration byte-for-byte (LF only), reads only its three
    bases and writes nothing else; the PREFLIGHT (P171.1-P171.4) and PART B are written only on request, are
    read-only, parse, and carry the proc's own E1 / C1 text;
  * shape -- first line, guard (-20171, v < 170: V165's guard with the numbers moved), the SETTINGS seed, three
    markers + procs, the version row; nothing runs at apply time;
  * lineage + round 13 -- each proc is re-derived from its immediately previous definer (V017, V165, V129) and
    reversing every delta with this file's OWN copies of the old and new text gives the base back
    byte-for-byte; a mutation inside or outside a delta breaks it;
  * content -- the canary text and handler, the digest window / keys / prompt / template and the CORTEX_MODEL
    read (parity with app.core.ai.normalize_model), the per-check ref-gap loop and both casts, the seed.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests._source import read
from tests.test_migration_proc_syntax import _strip_noise

_ROOT = Path(__file__).resolve().parents[2]
_MIGDIR = _ROOT / "snowflake" / "migrations"
_NAME = "V171__ops_selfwatch_digest_refgaps_seed.sql"
_MIG = read(f"snowflake/migrations/{_NAME}")
_V017 = read("snowflake/migrations/V017__hardening_v7.sql")
_V165 = read("snowflake/migrations/V165__daily_digest_grounding.sql")
_V129 = read("snowflake/migrations/V129__pipe_ref_gap_alert.sql")


def _proc(text: str, name: str) -> str:
    s = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    o = text.index("$$", s)
    return text[s:text.index("$$;", o + 2) + 3]


def _body(proc: str) -> str:
    return proc[proc.index("$$") + 2:proc.rindex("$$")]


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


_CAN, _CAN017 = _proc(_MIG, "SP_CANARY_SENTINEL()"), _proc(_V017, "SP_CANARY_SENTINEL()")
_DIG, _DIG165 = _proc(_MIG, "SP_DAILY_DIGEST()"), _proc(_V165, "SP_DAILY_DIGEST()")
_REF, _REF129 = _proc(_MIG, "SP_SCAN_REF_GAPS()"), _proc(_V129, "SP_SCAN_REF_GAPS()")
_CB, _DB, _RB = _body(_CAN), _body(_DIG), _body(_REF)

_MARKERS = {
    "SP_CANARY_SENTINEL": ("-- >>> derived:SP_CANARY_SENTINEL  (from V017; OPS_CANARY_FAIL detail no longer blames "
                           "column drift + render-SLA handler logs SQLERRM, V171)\n", 17),
    "SP_DAILY_DIGEST": ("-- >>> derived:SP_DAILY_DIGEST  (from V165; 7 complete days to yesterday + warehouse compute "
                        "spend keys and wording + CORTEX_MODEL normalized like the app, V171)\n", 165),
    "SP_SCAN_REF_GAPS": ("-- >>> derived:SP_SCAN_REF_GAPS  (from V129; both MINUS operands TO_VARCHAR + one EXECUTE "
                         "IMMEDIATE per check in its own EXCEPTION block, R2-019/R2-104, V171)\n", 129),
}

# ---------------------------------------------------------------------------------------------------------------
# Test-side copies of every delta (independent of outputs/gen_v171.py): (new text, old text).
# ---------------------------------------------------------------------------------------------------------------
_CANARY_DELTAS = (
    ("""\
               'CANARY_RESULTS.ERROR holds the error for each failing object. This probe (SELECT 1) ' ||
                   'only sees a missing or renamed object or lost access (a revoked grant, or ' ||
                   'IMPORTED PRIVILEGES on SNOWFLAKE); it cannot see column drift. Run the Admin ' ||
                   'canary for the per-builder picture.',
""", """\
               'CANARY_RESULTS has the errors. Likely ACCOUNT_USAGE column drift after a ' ||
                   'Snowflake release, or a revoked grant. Run the Admin canary for the ' ||
                   'per-builder picture.',
"""),
    ("            emsg := SQLERRM;   -- K2: V171 R2-026 - log the real cause (the probe loop is done, emsg is free)\n",
     ""),
    ("                   'render SLA check failed: ' || LEFT(:emsg, 500), 'source probes unaffected', CURRENT_ROLE();\n",
     "                   'APP_USAGE.RENDER_MS not readable', 'source probes unaffected', CURRENT_ROLE();\n"),
)

_NEW_E1 = """\
    -- E1 >>> V171 R1-228: the 7 COMPLETE days ending yesterday (the still-filling partial day of today left
    -- out, the Overview Spend, last N days convention). The board 7-day KPI rows are today-INCLUSIVE (DAY >=
    -- today-7: seven full days plus today so far), so they are no longer read. Spend is the board ALL / 7-day
    -- DAILY_SPEND rows for those days (warehouse metering at CREDIT_PRICE_USD; one row per day once COMPANY and
    -- WINDOW_DAYS are pinned); queries and task runs come from the facts the board aggregates, same days.
    SELECT SUM(VALUE_USD), SUM(VALUE)
      INTO :f_spend_usd, :f_credits
    FROM DBA_MAINT_DB.OVERWATCH.MART_EXEC_BOARD
    WHERE PANEL = 'DAILY_SPEND' AND METRIC = 'CREDITS' AND COMPANY = 'ALL' AND WINDOW_DAYS = 7
      AND PERIOD_START >= DATEADD('day', -7, CURRENT_DATE()) AND PERIOD_START < CURRENT_DATE();
    SELECT SUM(QUERY_COUNT), SUM(FAILED_COUNT), ROUND(SUM(QUEUED_SEC_SUM) / 60, 1), ROUND(SUM(SPILL_REMOTE_GB), 2)
      INTO :f_queries, :f_failed_q, :f_queued_min, :f_spill_gb
    FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY
    WHERE DAY >= DATEADD('day', -7, CURRENT_DATE()) AND DAY < CURRENT_DATE();
    SELECT SUM(RUNS), SUM(FAILED)
      INTO :f_task_runs, :f_task_fail
    FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
    WHERE DAY >= DATEADD('day', -7, CURRENT_DATE()) AND DAY < CURRENT_DATE();
    -- <<< E1
"""
_OLD_E1 = """\
    SELECT MAX(IFF(METRIC = 'CREDITS', VALUE_USD, NULL)), MAX(IFF(METRIC = 'CREDITS', VALUE, NULL)),
           MAX(IFF(METRIC = 'QUERIES', VALUE, NULL)), MAX(IFF(METRIC = 'FAILED_QUERIES', VALUE, NULL)),
           MAX(IFF(METRIC = 'QUEUED_MINUTES', VALUE, NULL)), MAX(IFF(METRIC = 'SPILL_GB', VALUE, NULL)),
           MAX(IFF(METRIC = 'TASK_RUNS', VALUE, NULL)), MAX(IFF(METRIC = 'TASK_FAILURES', VALUE, NULL))
      INTO :f_spend_usd, :f_credits, :f_queries, :f_failed_q, :f_queued_min, :f_spill_gb, :f_task_runs, :f_task_fail
    FROM DBA_MAINT_DB.OVERWATCH.MART_EXEC_BOARD
    WHERE COMPANY = 'ALL' AND WINDOW_DAYS = 7 AND PANEL = 'KPI';
"""
_NEW_C1 = """\
    -- C1: V171 CORTEX-NULLIF - CORTEX_MODEL read like app.core.ai.normalize_model (trimmed, lower-case, a valid
    -- name else the default): a blank, padded, mixed-case or invalid stored value no longer reaches COMPLETE
    SELECT IFF(RLIKE(cm, '[a-z0-9][a-z0-9.-]{1,60}'), cm, 'llama3.1-8b')
      INTO :model
    FROM (SELECT LOWER(TRIM(MAX(IFF(KEY = 'CORTEX_MODEL', VALUE, NULL)))) AS cm
          FROM DBA_MAINT_DB.OVERWATCH.SETTINGS);
"""
_OLD_C1 = """\
    SELECT COALESCE(MAX(IFF(KEY = 'CORTEX_MODEL', VALUE, NULL)), 'llama3.1-8b')
      INTO :model FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;
"""
_DIGEST_DELTAS = (
    (_NEW_C1, _OLD_C1),
    (_NEW_E1, _OLD_E1),
    ("""\
    -- E2: V171 R1-228 - the keys say warehouse compute (serverless, AI and storage are not in them)
    facts := 'WINDOW_DAYS=7; WAREHOUSE_SPEND_USD=' || COALESCE(TO_VARCHAR(:f_spend_usd), 'n/a')
          || '; WAREHOUSE_CREDITS=' || COALESCE(TO_VARCHAR(:f_credits), 'n/a')
""", """\
    facts := 'WINDOW_DAYS=7; SPEND_USD=' || COALESCE(TO_VARCHAR(:f_spend_usd), 'n/a')
          || '; CREDITS=' || COALESCE(TO_VARCHAR(:f_credits), 'n/a')
"""),
    ("        || 'Use ONLY the FACTS below (the 7 complete days ending yesterday, all companies; alert counts are "
     "open now or raised in the last 24 hours). '\n",
     "        || 'Use ONLY the FACTS below (the last 7 days, all companies; alert counts are open now or raised in "
     "the last 24 hours). '\n"),
    ("""\
        || '*_USD facts and percentages only from *_PCT facts; WAREHOUSE_CREDITS are Snowflake credits, not dollars. '
        || 'WAREHOUSE_SPEND_USD and WAREHOUSE_CREDITS cover warehouse compute only (serverless, AI and storage are not '
        || 'included): call it warehouse compute spend, never total spend. Write units in full '
        || '(credits, minutes, GB). Write three short unnumbered paragraphs: platform health and warehouse compute spend in plain language; '
""", """\
        || '*_USD facts and percentages only from *_PCT facts; CREDITS are Snowflake credits, not dollars. Write units in full '
        || '(credits, minutes, GB). Write three short unnumbered paragraphs: platform health and spend in plain language; '
"""),
    ("""\
             || 'The 7 complete days to yesterday, all companies: warehouse compute spend '
             || COALESCE('$' || TRIM(TO_VARCHAR(:f_spend_usd, '999,999,999,990.00')), 'n/a')
             || ' (' || COALESCE(TRIM(TO_VARCHAR(:f_credits, '999,999,999,990.00')), 'n/a')
             || ' credits; serverless, AI and storage not included); '
""", """\
             || 'Last 7 days, all companies: spend ' || COALESCE('$' || TRIM(TO_VARCHAR(:f_spend_usd, '999,999,999,990.00')), 'n/a')
             || ' (' || COALESCE(TRIM(TO_VARCHAR(:f_credits, '999,999,999,990.00')), 'n/a') || ' credits); '
"""),
)

_REFGAP_DELTAS = (
    ("""\
    enabled_cnt INT;
    ins_sql STRING;
    -- R1 >>> V171 R2-019: one statement per check, each in its own EXCEPTION block
    res RESULTSET;
    nm STRING;
    emsg STRING;
    n_ok INT DEFAULT 0;
    n_failed INT DEFAULT 0;
    all_failed EXCEPTION (-20662, 'ref-gap scan: every configured check failed - see APP_ERROR_LOG ref_gap_check_failed');
    -- <<< R1
""", """\
    enabled_cnt INT;
    scan_sql STRING;
    ins_sql STRING;
"""),
    ("""\
    -- family NAME + staging FQN + code column, and build ONE MINUS statement per check (R2 >>> V171: each runs
    -- alone below, R2-019; R2-104: BOTH operands TO_VARCHAR, the shape of the app twin
    -- etl_control_sql._check_sql since v4.527, so a NUMBER or DATE code column compares as text and never
    -- coerces the VARCHAR SRC_IDNTFTN_VAL).
""", """\
    -- family NAME + staging FQN + code column, and LISTAGG a UNION-ALL of per-check MINUS subqueries.
"""),
    ("""\
    res := (
    SELECT nm_clean AS CHECK_NAME,
             'SELECT ' || q_name || ' AS CHECK_NAME, TO_VARCHAR(g.NEW_CODE) AS NEW_CODE FROM ( '
             || 'SELECT TO_VARCHAR(s.' || col || ') AS NEW_CODE FROM ' || fqn || ' s WHERE s.' || col
             || ' IS NOT NULL MINUS SELECT TO_VARCHAR(x.SRC_IDNTFTN_VAL) FROM ' || :xlat
             || ' x WHERE x.SRC_IDNTFTN_NM = ' || q_name || ' ) g' AS CHECK_SQL
    FROM (
        SELECT nm_clean, '''' || REPLACE(nm_clean, '''', '''''') || '''' AS q_name, fqn, col
""", """\
    SELECT LISTAGG(
             'SELECT ' || q_name || ' AS CHECK_NAME, TO_VARCHAR(g.NEW_CODE) AS NEW_CODE FROM ( '
             || 'SELECT s.' || col || ' AS NEW_CODE FROM ' || fqn || ' s WHERE s.' || col
             || ' IS NOT NULL MINUS SELECT x.SRC_IDNTFTN_VAL FROM ' || :xlat
             || ' x WHERE x.SRC_IDNTFTN_NM = ' || q_name || ' ) g',
             ' UNION ALL ')
      INTO :scan_sql
    FROM (
        SELECT '''' || REPLACE(nm_clean, '''', '''''') || '''' AS q_name, fqn, col
"""),
    ("""\
    )
    ORDER BY CHECK_NAME
    );

    -- R3 >>> V171 R2-019: one INSERT per check, each in its own EXCEPTION block. A check that throws (a missing
    -- SELECT grant on one staging table, a renamed table, a code longer than NEW_CODE) logs one
    -- ref_gap_check_failed row naming it, and the other checks still write their gaps. V129 ran every check in
    -- ONE statement after the DELETE, so one bad check blanked the PIPE_REF_GAP page for all of them.
    LET c_checks CURSOR FOR res;
    FOR r IN c_checks DO
        nm := r.CHECK_NAME;
        BEGIN
            ins_sql := 'INSERT INTO DBA_MAINT_DB.OVERWATCH.ETL_REF_GAP_RESULTS (CHECK_NAME, NEW_CODE) '
                       || r.CHECK_SQL;
            EXECUTE IMMEDIATE :ins_sql;
            n_ok := n_ok + 1;
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                n_failed := n_failed + 1;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                    (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'AlertScan', 'ref_gap_check_failed', LEFT(:emsg, 2000),
                       'PIPE_REF_GAP check ' || LEFT(:nm, 200) || ' - other ref-gap checks unaffected', CURRENT_ROLE();
        END;
    END FOR;

    IF (:n_ok + :n_failed = 0) THEN
        RETURN 'ref-gap scan: no valid checks configured';
    END IF;
    IF (:n_ok = 0) THEN
        RAISE all_failed;   -- every check failed (e.g. no XLAT grant): arm [17] still logs ref_gap_scan_failed
    END IF;

    RETURN 'ref-gap scan complete (' || :n_ok || ' ok, ' || :n_failed || ' failed)';
    -- <<< R3
""", """\
    );

    IF (:scan_sql IS NULL OR TRIM(:scan_sql) = '') THEN
        RETURN 'ref-gap scan: no valid checks configured';
    END IF;

    ins_sql := 'INSERT INTO DBA_MAINT_DB.OVERWATCH.ETL_REF_GAP_RESULTS (CHECK_NAME, NEW_CODE) '
               || :scan_sql;
    EXECUTE IMMEDIATE :ins_sql;

    RETURN 'ref-gap scan complete';
"""),
)

_CASES = {"SP_CANARY_SENTINEL": (_CAN, _CAN017, _CANARY_DELTAS), "SP_DAILY_DIGEST": (_DIG, _DIG165, _DIGEST_DELTAS),
          "SP_SCAN_REF_GAPS": (_REF, _REF129, _REFGAP_DELTAS)}


def _reverse(proc: str, deltas) -> str | None:
    """The round-13 reversal: every new block out, the base's old text back in (None when a new block is not
    there exactly once -- a mutation inside a delta)."""
    for new, old in deltas:
        if proc.count(new) != 1:
            return None
        proc = proc.replace(new, old)
    return proc


# -- generation --------------------------------------------------------------------------------------------------

def _gen_env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("V171_OUT", "PREFLIGHT_OUT", "PART_B_OUT")}
    env.update(extra)
    return env


def _run_gen(tmp_path: Path, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v171.py")], env=_gen_env(**extra),
                          cwd=tmp_path, capture_output=True, text=True)


def _extras(tmp_path: Path) -> tuple[str, str]:
    pre, part_b = tmp_path / "PREFLIGHT_V171.sql", tmp_path / "PART_B_V171.sql"
    result = _run_gen(tmp_path, V171_OUT=str(tmp_path / "m.sql"), PREFLIGHT_OUT=str(pre), PART_B_OUT=str(part_b))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "m.sql").read_text(encoding="utf-8") == _MIG     # the extras never change the migration
    return pre.read_text(encoding="utf-8"), part_b.read_text(encoding="utf-8")


def test_v171_regenerates_byte_identical_and_writes_nothing_else(tmp_path):
    out = tmp_path / "regen.sql"
    result = _run_gen(tmp_path, V171_OUT=str(out))
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == (_MIGDIR / _NAME).read_bytes(), (
        "V171 drifted from its forward-generation -- edit outputs/gen_v171.py, not the .sql.")
    assert b"\r" not in out.read_bytes()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["regen.sql"]     # PREFLIGHT / PART B only on request
    assert "PREFLIGHT" not in result.stdout and "PART B" not in result.stdout


def test_v171_generator_reads_only_its_three_bases_and_never_imports_app():
    gen = read("outputs/gen_v171.py")
    for base in ("V017__hardening_v7.sql", "V165__daily_digest_grounding.sql", "V129__pipe_ref_gap_alert.sql"):
        assert f'MIG / "{base}"' in gen, base
    assert gen.count(".read_text(") == 1 and gen.count('MIG / "V') == 3   # the three bases, read in one place
    assert not re.search(r"^\s*(?:from|import)\s+app\b", gen, re.M)
    assert "def _swap(text: str, old: str, new: str, label: str, n: int = 1)" in gen
    assert 'ROOT = Path(__file__).resolve().parents[1]' in gen


@pytest.mark.parametrize("which", ["preflight", "part_b"])
def test_v171_preflight_and_part_b_are_read_only_and_parse(tmp_path, which):
    pre, part_b = _extras(tmp_path)
    sql = pre if which == "preflight" else part_b
    code = _strip_noise(sql)
    for banned in ("INSERT", "UPDATE", "DELETE", "MERGE", "CALL", "CREATE", "ALTER", "DROP", "TRUNCATE",
                   "GRANT", "REVOKE", "EXECUTE", "UNDROP", "RENAME", "USE", "SET"):
        assert not re.search(rf"\b{banned}\b", code, re.I), (which, banned)
    assert not re.search(r"(?<![:\w]):[A-Za-z_]\w*", code), "a scripting :bind survived"
    assert "$$" not in sql and "@" not in sql and "\\" not in sql
    sqlglot = pytest.importorskip("sqlglot")
    from sqlglot import exp
    parsed = sqlglot.parse(sql, dialect="snowflake")
    assert parsed and all(p is not None and p.key in ("select", "union") for p in parsed), which
    writes = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Command)
    for p in parsed:
        assert not [type(n).__name__ for n in p.walk() if isinstance(n, writes)], which


def test_v171_preflight_previews_the_procs_own_reads(tmp_path):
    pre, part_b = _extras(tmp_path)
    assert re.findall(r"^-- (P171\.\d) ", pre, re.M) == ["P171.1", "P171.2", "P171.3", "P171.4"]
    assert re.findall(r"^-- (V171\.\d) ", part_b, re.M) == ["V171.1", "V171.5", "V171.6"]
    for label, proc in (("V171.1", "SP_CANARY_SENTINEL"), ("V171.2", "SP_DAILY_DIGEST"), ("V171.3", "SP_SCAN_REF_GAPS")):
        assert part_b.count(f"SELECT '{label} {proc} is the V171 proc' AS CHECK_NAME,") == 1, label
        assert part_b.count(f"GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.{proc}()')") >= 4, label
    assert "SELECT 'V171.4 exactly one CREDIT_PRICE_OVERRIDE row'," in part_b
    # each E1 read's FROM / WHERE, Central-pinned (a UTC worksheet previews the same days as the task)
    central = "CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE"
    for tbl in ("MART_EXEC_BOARD", "FACT_QUERY_DAILY", "FACT_TASK_DAILY"):
        m = re.search(rf"    FROM DBA_MAINT_DB\.OVERWATCH\.{tbl}\n(    WHERE [^;]*);", _DB)
        assert m, tbl
        assert m.group(1).replace("CURRENT_DATE()", central) in pre, tbl
    assert "CURRENT_DATE()" not in pre
    # the V165 side is V165's own KPI read, and the model preview is C1's expression
    assert "WHERE COMPANY = 'ALL' AND WINDOW_DAYS = 7 AND PANEL = 'KPI'" in pre
    assert "IFF(RLIKE(cm, '[a-z0-9][a-z0-9.-]{1,60}'), cm, 'llama3.1-8b')" in pre


# -- guard, order, shape -------------------------------------------------------------------------------------------

def test_v171_first_line_guard_and_version():
    assert _MIG.startswith(f"-- {_NAME}\n")
    assert "not_ready EXCEPTION (-20171, 'V171 requires V170 first - apply migrations in order.');" in _MIG
    assert "IF (v < 170) THEN" in _MIG
    assert "SELECT 171 AS VERSION" in _MIG and "WHERE VERSION = 171);" in _MIG
    guard165 = _between(_V165, "EXECUTE IMMEDIATE\n$$\n", "$$;\n")
    want = (guard165.replace("-20165", "-20171").replace("'V165 requires V164 first", "'V171 requires V170 first")
            .replace("IF (v < 164)", "IF (v < 170)"))
    assert _between(_MIG, "EXECUTE IMMEDIATE\n$$\n", "$$;\n") == want
    assert _MIG.count("EXECUTE IMMEDIATE\n$$") == 1
    assert _MIG.rstrip().endswith("WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION "
                                  "WHERE VERSION = 171);")


def test_v171_header_names_the_house_sections():
    header = _MIG[:_MIG.index("EXECUTE IMMEDIATE\n$$")]
    for section in ("-- WHY:", "-- COST:", "-- LATENCY:", "-- FIRST RUN:", "-- ROLLBACK:"):
        assert section in header, section
    assert header.rstrip().endswith("-- Apply AFTER V170. Idempotent; safe to re-run.")
    for base in ("V017__hardening_v7.sql", "V165__daily_digest_grounding.sql", "V129__pipe_ref_gap_alert.sql"):
        assert base in header, base
    assert "$$" not in header and "RLIKE(" not in header


def test_v171_file_order():
    guard = _MIG.index("EXCEPTION (-20171")
    seed = _MIG.index("MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS t")
    pos = []
    for proc, (marker, _base) in _MARKERS.items():
        m = _MIG.index(marker)
        assert _MIG[m + len(marker):].startswith(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{proc}()")
        pos.append(m)
    version = _MIG.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION")
    assert guard < seed < pos[0] < pos[1] < pos[2] < version


def test_v171_nothing_runs_at_apply_and_no_new_object():
    from tests.test_migrations_parse import _plain_statements
    assert _MIG.count("CREATE OR REPLACE PROCEDURE") == 3 and _MIG.count("$$") == 8
    top = _strip_noise("".join(p for i, p in enumerate(_MIG.split("$$")) if i % 2 == 0))
    for banned in ("CALL", "DROP", "TASK", "GRANT", "REVOKE", "TRUNCATE", "UPDATE", "DELETE", "ALTER"):
        assert not re.search(rf"\b{banned}\b", top), banned
    kinds = [re.sub(r"^(?:--[^\n]*\n)+", "", s.strip()).split(None, 3)[:3] for s in _plain_statements(_MIG)]
    assert kinds == [["MERGE", "INTO", "DBA_MAINT_DB.OVERWATCH.SETTINGS"],
                     ["INSERT", "INTO", "DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION"]], kinds
    assert not re.search(r"(?:CREATE(?:\s+OR\s+REPLACE)?\s+(?:TASK|TABLE|VIEW|FUNCTION)|ALTER\s+TASK|EXECUTE\s+TASK)",
                         _strip_noise(_MIG))
    # no proc is CALLed outside the digest's own route send; SP_ALERT_SCAN_DAILY (the ref-gap caller) is untouched
    assert re.findall(r"\bCALL\s+\S+", _strip_noise(_MIG)) == ["CALL SYSTEM$SEND_SNOWFLAKE_NOTIFICATION("]
    assert "PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY" not in _MIG
    assert "SOURCE_FRESHNESS_STATE" not in _MIG
    assert "@" not in _MIG and "\\" not in _MIG and "\r" not in _MIG
    assert not re.findall(r"\$[A-Za-z_][A-Za-z0-9_]*\$", _MIG.replace("SYSTEM$SEND_SNOWFLAKE_NOTIFICATION", ""))


def test_v171_plain_statements_parse():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_MIG):
        assert sqlglot.parse(statement, dialect="snowflake"), statement[:80]


def test_v171_description_fits():
    desc = re.search(r"SELECT 171 AS VERSION,\n       '(.*)' AS DESCRIPTION\n", _MIG, re.S).group(1)
    assert "\n" not in desc and "'" not in desc and "$" not in desc and len(desc) <= 4000
    for proc, (_m, base) in _MARKERS.items():
        assert f"{proc} re-derived from V{base:03d}" in desc, proc
    assert desc.endswith("No task change, no new object, no procedure run at apply time.")


# -- lineage + round 13 ----------------------------------------------------------------------------------------------

def _assert_v171_lineage(texts: dict[int, str]) -> None:
    """V171's own lineage, bounded at 171: a later migration that re-derives one of these procs must not turn it red."""
    from tests.test_proc_lineage import _HISTORICAL_WAIVERS, _definers, _lineage, _violations
    rows = {(r["v"], r["proc"]): r for r in _lineage(texts)}
    defs = _definers(texts)
    for proc, (_marker, base) in _MARKERS.items():
        r = rows[(171, proc)]
        assert r["src"] == "marker" and r["claims"] == [base] and r["prev"] == base and not r["waived"], r
        assert [v for v in defs[proc] if base <= v <= 171] == [base, 171], proc
    assert not [v for v in _violations(texts, _HISTORICAL_WAIVERS) if v.startswith("V171 ")]


def test_v171_markers_name_the_current_definers():
    from tests.test_proc_lineage import _migrations
    _assert_v171_lineage(_migrations())
    assert "LINEAGE-WAIVER" not in _MIG and _MIG.count("-- >>> derived:") == 3
    for marker, _base in _MARKERS.values():
        assert _MIG.count(marker) == 1, marker


def test_v171_lineage_lock_survives_a_later_re_derivation():
    from tests.test_proc_lineage import _migrations
    texts = _migrations()
    assert max(texts) < 999
    texts[999] = "".join(
        f"-- >>> derived:{proc}  (from V171)\nCREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{proc}()\n"
        "RETURNS VARCHAR LANGUAGE SQL AS\n$$\nBEGIN\n    RETURN 'x';\nEND;\n$$;\n" for proc in _MARKERS)
    _assert_v171_lineage(texts)


@pytest.mark.parametrize("proc", list(_CASES))
def test_v171_proc_normalizes_back_to_its_base_byte_for_byte(proc):
    new, base, deltas = _CASES[proc]
    assert _reverse(new, deltas) == base


@pytest.mark.parametrize("proc, old, new", [
    # outside every delta
    ("SP_CANARY_SENTINEL", "                fails := fails + 1;", "                fails := fails + 2;"),
    ("SP_CANARY_SENTINEL", "        'SNOWFLAKE.ACCOUNT_USAGE.LOCK_WAIT_HISTORY',\n", ""),
    ("SP_CANARY_SENTINEL", "RETURN 'sentinel v2: '", "RETURN 'sentinel v3: '"),
    ("SP_DAILY_DIGEST", "          AND UPPER(COALESCE(r.MIN_SEVERITY, '')) <> 'CRITICAL'", "          AND TRUE"),
    ("SP_DAILY_DIGEST", "        grounding_ok := (n_bad = 0);", "        grounding_ok := TRUE;"),
    ("SP_SCAN_REF_GAPS", "AND RLIKE(nm_clean, '^[-A-Za-z0-9_.:/ ]+$')", "AND RLIKE(nm_clean, '^.+$')"),
    ("SP_SCAN_REF_GAPS", "    DELETE FROM DBA_MAINT_DB.OVERWATCH.ETL_REF_GAP_RESULTS;\n", ""),
    # inside a delta (the plan's teeth)
    ("SP_DAILY_DIGEST", "PERIOD_START < CURRENT_DATE()", "PERIOD_START <= CURRENT_DATE()"),
    ("SP_DAILY_DIGEST", "FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY\n    WHERE DAY >= DATEADD('day', -7, "
                        "CURRENT_DATE()) AND DAY < CURRENT_DATE();",
     "FROM DBA_MAINT_DB.OVERWATCH.FACT_QUERY_DAILY\n    WHERE DAY >= DATEADD('day', -7, CURRENT_DATE());"),
    ("SP_DAILY_DIGEST", "PANEL = 'DAILY_SPEND'", "PANEL = 'KPI'"),
    ("SP_DAILY_DIGEST", "'WINDOW_DAYS=7; WAREHOUSE_SPEND_USD='", "'WINDOW_DAYS=7; SPEND_USD='"),
    ("SP_DAILY_DIGEST", "RLIKE(cm, '[a-z0-9][a-z0-9.-]{1,60}')", "RLIKE(cm, '.*')"),
    ("SP_SCAN_REF_GAPS", "MINUS SELECT TO_VARCHAR(x.SRC_IDNTFTN_VAL) FROM", "MINUS SELECT x.SRC_IDNTFTN_VAL FROM"),
    ("SP_SCAN_REF_GAPS", "        RAISE all_failed;", "        RETURN 'x';"),
    ("SP_CANARY_SENTINEL", "'render SLA check failed: ' || LEFT(:emsg, 500)",
     "'render SLA check failed: ' || LEFT(:emsg, 50)"),
])
def test_v171_normalize_has_teeth(proc, old, new):
    p, base, deltas = _CASES[proc]
    assert p.count(old) == 1, old
    assert _reverse(p.replace(old, new), deltas) != base


# -- SP_CANARY_SENTINEL (R2-026) -------------------------------------------------------------------------------------

def test_v171_canary_detail_and_render_handler():
    assert "column drift after" not in _CB and "cannot see column drift" in _CB
    detail = _between(_CB, "'CANARY_RESULTS.ERROR holds", "               :fails,\n")
    text = "".join(re.findall(r"'([^']*)'", detail))
    assert len(text) < 2000 and detail.count("'") == 8                         # ALERT_EVENTS.DETAIL VARCHAR(2000)
    handler = _between(_CB, "    -- Render-time SLA (guarded)", "    DELETE FROM DBA_MAINT_DB.OVERWATCH.CANARY_RESULTS")
    assert "            emsg := SQLERRM;" in handler.split("WHEN OTHER THEN", 1)[1]
    assert "'render SLA check failed: ' || LEFT(:emsg, 500), 'source probes unaffected', CURRENT_ROLE();" in handler
    assert "'CanarySentinel', 'render_check_unavailable'," in handler        # ERROR_TYPE kept (history lock)
    # the source-probe count is unchanged: render failures still do not count
    assert _CB.count("fails := fails + 1;") == 1 and "RETURN 'sentinel v2: ' || :fails || ' failure(s)';" in _CB
    assert _CB.index("    END FOR;") < _CB.index("            emsg := SQLERRM;   -- K2")    # the probe loop is done
    assert "EXECUTE AS OWNER" in _CAN


def test_v171_canary_checks_array_is_v017s():
    def checks(body: str) -> list[str]:
        return re.findall(r"'([A-Z_.]+)'", _between(body, "checks ARRAY DEFAULT [", "];"))
    assert checks(_CB) == checks(_body(_CAN017)) and len(checks(_CB)) == 24


# -- SP_DAILY_DIGEST (R1-228 + CORTEX-NULLIF) ------------------------------------------------------------------------

def test_v171_digest_reads_the_seven_complete_days():
    assert "PANEL = 'KPI'" not in _DB
    reads = re.findall(r"^    FROM DBA_MAINT_DB\.OVERWATCH\.(\w+)\n    WHERE ([^;]*);", _DB, re.M)
    fact_reads = [(t, w) for t, w in reads if t in ("MART_EXEC_BOARD", "FACT_QUERY_DAILY", "FACT_TASK_DAILY")]
    assert [t for t, _ in fact_reads] == ["MART_EXEC_BOARD", "FACT_QUERY_DAILY", "FACT_TASK_DAILY"]
    for tbl, where in fact_reads:
        col = "PERIOD_START" if tbl == "MART_EXEC_BOARD" else "DAY"
        assert f"{col} >= DATEADD('day', -7, CURRENT_DATE())" in where, tbl
        assert f"{col} < CURRENT_DATE()" in where and f"{col} <= " not in where, tbl
    board = fact_reads[0][1]
    for pin in ("PANEL = 'DAILY_SPEND'", "METRIC = 'CREDITS'", "COMPANY = 'ALL'", "WINDOW_DAYS = 7"):
        assert pin in board, pin
    # the INTO order is V165's (the typed variables and FACTS string downstream are unchanged)
    into = re.findall(r"^      INTO (:[^\n]+)$", _between(_DB, "    -- E1 >>>", "    -- <<< E1"), re.M)
    assert ", ".join(into) == ":f_spend_usd, :f_credits, :f_queries, :f_failed_q, :f_queued_min, :f_spill_gb, " \
                              ":f_task_runs, :f_task_fail"


def test_v171_digest_facts_window_on_its_own_body():
    """The same sqlite harness as the permanent parity test, on V171's own body (it stays V171's once later
    migrations re-derive the digest)."""
    from tests.test_digest_grounding_parity import _close, _want, run_fact_reads
    got, want = run_fact_reads(_DB), _want()
    for tbl in want:
        assert _close(got[tbl], want[tbl]), (tbl, got[tbl], want[tbl])
    got_165 = run_fact_reads(_DB.replace(" AND COMPANY = 'ALL'", ""))
    assert not _close(got_165["MART_EXEC_BOARD"], want["MART_EXEC_BOARD"])


def test_v171_digest_keys_prompt_and_template_say_warehouse_compute():
    facts = _between(_DB, "    facts := 'WINDOW_DAYS=7", "    alerts := ")
    keys = re.findall(r"([A-Z][A-Z0-9_]*)=", " ".join(re.findall(r"'([^']*)'", facts)))
    assert keys[:3] == ["WINDOW_DAYS", "WAREHOUSE_SPEND_USD", "WAREHOUSE_CREDITS"] and len(keys) == 13
    assert "SPEND_USD" not in keys and "CREDITS" not in keys
    prompt = _between(_DB, "    prompt := LEFT(", "6000);")
    for phrase in ("the 7 complete days ending yesterday, all companies", "warehouse compute only",
                   "never total spend", "WAREHOUSE_CREDITS are Snowflake credits, not dollars",
                   "platform health and warehouse compute spend in plain language"):
        assert phrase in prompt, phrase
    assert "the last 7 days" not in prompt
    tpl = _between(_DB, "        body := 'Templated digest (not AI-written): '", " ELSE 'nothing is open")
    assert "'The 7 complete days to yesterday, all companies: warehouse compute spend '" in tpl
    assert "' credits; serverless, AI and storage not included); '" in tpl and "Last 7 days" not in tpl
    lits = re.findall(r"'(?:[^']|'')*'", tpl)
    digits = {d for lit in lits if not re.fullmatch(r"'[09,.]+'", lit) for d in re.findall(r"[0-9]+", lit)}
    assert digits == {"7", "24"}, digits                                  # the template states only fact values
    assert "\\" not in _DB and "$$" not in _DB


def test_v171_digest_grounding_is_v165s():
    """The measured grounding (D5) and the send (D7-D9) are byte-identical to V165."""
    for start, end in (("    -- D5 >>>", "    -- V165 #24: the templated digest"),
                       ("    -- D7 >>>", "    -- <<< D7"), ("    FOR rec IN c_routes DO", "END;")):
        assert _between(_DB, start, end) == _between(_body(_DIG165), start, end), start


_MODEL_SAMPLES = (None, "", "   ", " Llama3.1-70B ", "llama3.1 8b", "llama3_1", "mistral-large2", "claude-sonnet-4-5",
                  "a" * 61, "a" * 62, "-llama", "LLAMA3.1-8B", "x")


def _sql_model(stored):
    """Python emulation of C1: LOWER(TRIM(v)) (TRIM strips spaces), then RLIKE (whole-string), else the default."""
    if stored is None:
        return "llama3.1-8b"
    cm = stored.strip(" ").lower()
    return cm if re.fullmatch("[a-z0-9][a-z0-9.-]{1,60}", cm) else "llama3.1-8b"


def test_v171_cortex_model_read_matches_the_app():
    from app.config import DEFAULT_SETTINGS
    from app.core import ai
    m = re.search(r"SELECT IFF\(RLIKE\(cm, '([^']*)'\), cm, '([^']*)'\)\n      INTO :model\n"
                  r"    FROM \(SELECT LOWER\(TRIM\(MAX\(IFF\(KEY = 'CORTEX_MODEL', VALUE, NULL\)\)\)\) AS cm\n"
                  r"          FROM DBA_MAINT_DB\.OVERWATCH\.SETTINGS\);", _DB)
    assert m, "the C1 read"
    literal, default = m.groups()
    assert "^" + literal.replace(".-]", ".\\-]") + "$" == ai._MODEL_RE.pattern
    assert default == ai._DEFAULT_MODEL == DEFAULT_SETTINGS["CORTEX_MODEL"]
    assert "\\" not in literal and _DB.count("RLIKE(cm, ") == 1
    assert "COALESCE(MAX(IFF(KEY = 'CORTEX_MODEL'" not in _DB
    for stored in _MODEL_SAMPLES:
        assert _sql_model(stored) == ai.normalize_model(stored), stored


def test_v171_digest_statements_parse_and_binds_are_declared():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.migrations.test_v165_daily_digest_grounding import _BINDS, _bind, _code, _split
    declared = set(re.findall(r"^    (\w+) (?:VARCHAR|INT|NUMBER|BOOLEAN|CURSOR)", _DB[:_DB.index("\nBEGIN\n")], re.M))
    used = set(re.findall(r"(?<![:\w]):([A-Za-z_]\w*)", _strip_noise(_DB)))
    assert used <= declared and used <= set(_BINDS), used - declared
    into = re.compile(r"\bINTO :\w+(?:,\s*:\w+)*")
    stmts = []
    for chunk in _split(_code(_DB)):
        m = re.search(r"^[ \t]*(SELECT|INSERT|UPDATE|DELETE|MERGE)\b", chunk, re.M)
        if m:
            stmts.append(into.sub("", chunk[m.start():]).strip())
    assert len(stmts) == 12, [s[:40] for s in stmts]                    # V165's 10, the KPI read now three reads
    for stmt in stmts:
        parsed = sqlglot.parse(_bind(stmt), dialect="snowflake")
        assert len(parsed) == 1 and parsed[0] is not None, stmt[:80]


# -- SP_SCAN_REF_GAPS (R2-019 + R2-104) ------------------------------------------------------------------------------

def test_v171_ref_gap_casts_both_operands_and_runs_each_check_alone():
    assert _RB.count("'SELECT TO_VARCHAR(s.' || col || ') AS NEW_CODE FROM '") == 1
    assert _RB.count("MINUS SELECT TO_VARCHAR(x.SRC_IDNTFTN_VAL) FROM") == 1
    for gone in ("LISTAGG(", "UNION ALL", "scan_sql"):
        assert gone not in _RB, gone
    loop = _between(_RB, "    FOR r IN c_checks DO", "    END FOR;")
    assert _RB.count("EXECUTE IMMEDIATE :ins_sql;") == 1 and "EXECUTE IMMEDIATE :ins_sql;" in loop
    handler = loop.split("        EXCEPTION\n", 1)[1]
    assert "'AlertScan', 'ref_gap_check_failed', LEFT(:emsg, 2000)," in handler
    assert "'PIPE_REF_GAP check ' || LEFT(:nm, 200) ||" in handler and "n_failed := n_failed + 1;" in handler
    order = [_RB.index(s) for s in ("    DELETE FROM DBA_MAINT_DB.OVERWATCH.ETL_REF_GAP_RESULTS;",
                                    "    res := (", "    LET c_checks CURSOR FOR res;", "    FOR r IN c_checks DO",
                                    "    END FOR;", "    IF (:n_ok + :n_failed = 0) THEN", "        RAISE all_failed;",
                                    "    RETURN 'ref-gap scan complete ('")]
    assert order == sorted(order)
    # V129's gate, skips and empty-config return are kept
    for kept in ("RETURN 'ref-gap scan skipped (rule disabled)';",
                 "RETURN 'ref-gap scan skipped (unconfigured or invalid XLAT)';",
                 "RETURN 'ref-gap scan: no valid checks configured';"):
        assert _RB.count(kept) == 1 and _body(_REF129).count(kept) == 1, kept


def test_v171_ref_gap_allowlist_line_is_the_only_one_in_the_file():
    """tests/test_p608_ops_etl.py pins etl_control_sql.ALERT_NAME_PATTERN to the ONE RLIKE(nm_clean, ...) of the
    latest-definer FILE: header prose must never quote it."""
    line = "          AND RLIKE(nm_clean, '^[-A-Za-z0-9_.:/ ]+$')\n"
    assert _MIG.count("RLIKE(nm_clean, ") == 1 and _RB.count(line) == 1 and _body(_REF129).count(line) == 1


def test_v171_all_failed_code_is_unique():
    hits = [p.name for p in _MIGDIR.glob("V*.sql") if "-20662" in p.read_text(encoding="utf-8")]
    assert hits == [_NAME], hits
    assert "all_failed EXCEPTION (-20662, " in _RB and _RB.count("RAISE all_failed;") == 1


def test_v171_ref_gap_statements_parse():
    sqlglot = pytest.importorskip("sqlglot")
    query = _between(_RB, "    res := (\n", "\n    );\n")[len("    res := (\n"):]
    bound = query.replace(":xlat", "'DB.SCH.XLAT'").replace(":checks", "'a.code | DB.S.T | CODE'")
    assert ":" not in re.sub(r"'(?:[^']|'')*'", "''", bound)
    sqlglot.parse_one(bound, dialect="snowflake")
    log = _between(_RB, "                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG", "CURRENT_ROLE();")
    sqlglot.parse_one((log + "CURRENT_ROLE()").replace(":emsg", "'e'").replace(":nm", "'n'"), dialect="snowflake")


# -- CREDIT-PRICE-SEED ------------------------------------------------------------------------------------------------

def test_v171_seed_is_when_not_matched_only_and_never_satisfies_validate():
    seed = _between(_MIG, "MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS t", "\n\n")
    assert "        ('CREDIT_PRICE_OVERRIDE', 'FALSE')\n" in seed
    assert seed.rstrip().endswith("WHEN NOT MATCHED THEN INSERT (KEY, VALUE) VALUES (s.KEY, s.VALUE);")
    assert "WHEN MATCHED" not in seed.replace("WHEN NOT MATCHED", "") and seed.count("VALUES") == 2
    # validate.sql reads the override as UPPER(COALESCE(VALUE, '')) IN (...): FALSE is never in it
    body = read("snowflake/validate.sql")
    lists = re.findall(r"KEY = 'CREDIT_PRICE_OVERRIDE'\s+AND UPPER\(COALESCE\(VALUE, ''\)\) IN \(([^)]*)\)", body)
    assert len(lists) == 2
    for lst in lists:
        assert "'FALSE'" not in lst and "FALSE" not in re.findall(r"'([^']*)'", lst)
