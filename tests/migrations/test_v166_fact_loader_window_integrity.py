"""Locks for V166 -- four fact loaders stop losing or understating history (V166-V172 round, cluster loaders).

STRUCTURE + GENERATION + LOCKS; the executed models (sqlite + Python replays) live in tests/migrations/test_v166_harness.py.
What this file proves, from the shipped text:
  * generation -- outputs/gen_v166.py regenerates the migration byte-for-byte (LF only), reads only its four bases and
    writes nothing else; the PREFLIGHT / PART B are read-only and parse; the owner REPAIR section pins the session to
    Central first and every heal in it is a guarded block that reads the loader's own verdict;
  * shape -- first line, guard (-20166, v < 165: V165's guard with the numbers moved), the four marker + CREATE pairs in
    order, the one bounded repair MERGE after them, the version row; nothing CALLs, no task / table / DDL change;
  * lineage + round 13 -- each proc names its immediately previous definer (V105 / V101 / V077 / V046), and reversing
    every declared delta with THIS file's own copies of the old text gives the base proc back byte-for-byte (with
    teeth: a stray edit anywhere else fails the reversal);
  * the deltas -- R2-007 one shared bound in the d<=3 arm only; R2-009 SUM per name-day (V101's task_attempts CTE
    survives); R2-011 DELETE + INSERT in one DML-only transaction, ROLLBACK, a fact_load_failed row the alert legs
    already read, re-RAISE, freshness after the COMMIT; C10 the 30-day SESSIONS pad equals the app's SESSION_PAD_DAYS
    and the '(unknown)' label (two consumers filter on it) is unchanged; the repair rewrites only two byte columns.
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
_NAME = "V166__fact_loader_window_integrity.sql"
_MIG = read(f"snowflake/migrations/{_NAME}")
_V105 = read("snowflake/migrations/V105__change_risk_create_or_replace_destructive.sql")
_V101 = read("snowflake/migrations/V101__fact_task_daily_retry_collapse.sql")
_V077 = read("snowflake/migrations/V077__app_cost_ledger.sql")
_V046 = read("snowflake/migrations/V046__storage_truth.sql")
_V165 = read("snowflake/migrations/V165__daily_digest_grounding.sql")


def _proc(text: str, name: str) -> str:
    s = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    o = text.index("$$", s)
    return text[s:text.index("$$;", o + 2) + 3]


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


_SIG = {"SP_LOAD_SECURITY_FACTS": "SP_LOAD_SECURITY_FACTS(DAYS_BACK FLOAT)",
        "SP_LOAD_DAILY_FACTS": "SP_LOAD_DAILY_FACTS()",
        "SP_LOAD_APP_COST": "SP_LOAD_APP_COST(DAYS_BACK FLOAT)",
        "SP_LOAD_STORAGE_TRUTH": "SP_LOAD_STORAGE_TRUTH(DAYS_BACK FLOAT)"}
_BASE = {"SP_LOAD_SECURITY_FACTS": (105, _V105), "SP_LOAD_DAILY_FACTS": (101, _V101),
         "SP_LOAD_APP_COST": (77, _V077), "SP_LOAD_STORAGE_TRUTH": (46, _V046)}
_P = {name: _proc(_MIG, sig) for name, sig in _SIG.items()}
_PB = {name: _proc(_BASE[name][1], sig) for name, sig in _SIG.items()}

# ---------------------------------------------------------------------------------------------------
# Test-side copies of every declared delta (independent of outputs/gen_v166.py): (new text, old text).
# ---------------------------------------------------------------------------------------------------
_MARKERS = {
    "SP_LOAD_SECURITY_FACTS": ("-- >>> derived:SP_LOAD_SECURITY_FACTS  (from V105; d<=3 change reload DELETE + INSERT "
                               "share one bound GREATEST(MIN(extract.START_TIME), midnight(today-d)), V166)\n"),
    "SP_LOAD_DAILY_FACTS": ("-- >>> derived:SP_LOAD_DAILY_FACTS  (from V101; FACT_STORAGE_DAILY arm AVG -> SUM per "
                            "name-day, V166)\n"),
    "SP_LOAD_APP_COST": ("-- >>> derived:SP_LOAD_APP_COST  (from V077; DELETE+INSERT in one transaction, ROLLBACK + "
                         "APP_ERROR_LOG + re-RAISE; SESSIONS lookback 7 -> 30 days, V166)\n"),
    "SP_LOAD_STORAGE_TRUTH": ("-- >>> derived:SP_LOAD_STORAGE_TRUTH  (from V046; DELETE+INSERT in one transaction, "
                              "ROLLBACK + APP_ERROR_LOG + re-RAISE, V166)\n"),
}
_R2_007 = (
    ("    lo_ts TIMESTAMP_LTZ;        -- V166 (R2-007): the d<=3 change reload lower bound, DELETE and INSERT\n", ""),
    ("        -- V166 (R2-007): the DELETE and the INSERT share ONE lower bound, the later of the extract\n"
     "        -- first row and midnight(today - d). V100 deleted from MIN(START_TIME) alone while the INSERT\n"
     "        -- re-read only START_TIME >= today - d, so whenever the extract reached further back (a\n"
     "        -- swallowed extract failure across midnight keeps the old untrimmed fill; a backfill-wide\n"
     "        -- extract; a manual d of 1 or 2, DAYS_BACK 0 maps to 1) the rows in between were deleted,\n"
     "        -- never re-read, then trimmed out of the extract for good. GREATEST() propagates NULL: an\n"
     "        -- empty extract leaves lo_ts NULL and both statements match nothing (the V100 no-op is kept).\n"
     "        lo_ts := (SELECT GREATEST(MIN(START_TIME), DATEADD('day', -:d, CURRENT_DATE())::TIMESTAMP_LTZ)\n"
     "                    FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT);\n", ""),
    ("         WHERE EVENT_TS >= :lo_ts;\n",
     "         WHERE EVENT_TS >= (SELECT MIN(START_TIME) FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT);\n"),
    ("            FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT\n            WHERE START_TIME >= :lo_ts\n",
     "            FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT\n"
     "            WHERE START_TIME >= DATEADD('day', -:d, CURRENT_DATE())\n"),
)
_R2_009 = (
    ("        -- V166 (R2-009): SUM, not AVG. The view has one row per DATABASE_ID per day, and a dropped\n"
     "        -- (re-created / clone-refreshed) database keeps its own rows under the same name while it\n"
     "        -- holds Time Travel / fail-safe bytes. Each row is already the daily average of its own\n"
     "        -- DATABASE_ID, so the name-day billed bytes are the SUM (= storage_by_database_calendar_live).\n", ""),
    ("        SUM(COALESCE(AVERAGE_DATABASE_BYTES, 0)),\n        SUM(COALESCE(AVERAGE_FAILSAFE_BYTES, 0))\n",
     "        AVG(COALESCE(AVERAGE_DATABASE_BYTES, 0)),\n        AVG(COALESCE(AVERAGE_FAILSAFE_BYTES, 0))\n"),
)
_EMSG = "    emsg VARCHAR;               -- V166 (R2-011): the rolled-back load error, logged then re-raised\n"
_WRAP_OPEN = "    BEGIN\n    BEGIN TRANSACTION;\n"


def _handler(page: str, fact: str) -> str:
    return ("    COMMIT;\n"
            "    EXCEPTION\n"
            "        WHEN OTHER THEN\n"
            "            ROLLBACK;\n"
            "            emsg := SQLERRM;\n"
            "            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, "
            "ROLE_NAME)\n"
            f"            SELECT '{page}', 'fact_load_failed', LEFT(:emsg, 2000), '{fact} - previous fill retained on "
            "rollback, error re-raised', CURRENT_ROLE();\n"
            "            RAISE;\n"
            "    END;\n")


_R2_011_APP = (
    (_EMSG, ""),
    ("    -- V166 (R2-011): ONE transaction. Under autocommit a failed INSERT left the DELETE committed,\n"
     "    -- and the next run starts a day later, so the oldest deleted day was never reloaded (a\n"
     "    -- permanent hole per failed run). A failure now rolls back to the previous fill, is logged as\n"
     "    -- fact_load_failed (the self-watch ERR leg keys on the first CONTEXT word) and is re-raised,\n"
     "    -- so the task still reads FAILED.\n" + _WRAP_OPEN, ""),
    (_handler("AppCost", "FACT_APP_COST_DAILY"), ""),
)
_C10 = (
    ("        -- V166 (C10): 30 days before lo, not 7. The task LAST reload of a day (lo = that day)\n"
     "        -- narrowed the lookback to 7 days and relabelled '(unknown)' a keep-alive / pooled session\n"
     "        -- the first load had resolved. Same pad as app_cost_sql.SESSION_PAD_DAYS (the live twin).\n", ""),
    ("        WHERE CREATED_ON >= DATEADD('day', -30, :lo)\n", "        WHERE CREATED_ON >= DATEADD('day', -7, :lo)\n"),
)
_R2_011_STORAGE = (
    (_EMSG, ""),
    ("    -- V166 (R2-011): one transaction, so a failed INSERT rolls the DELETE back (see SP_LOAD_APP_COST).\n"
     + _WRAP_OPEN, ""),
    (_handler("StorageTruth", "FACT_STORAGE_ACCOUNT_DAILY"), ""),
)
_DELTAS = {"SP_LOAD_SECURITY_FACTS": _R2_007, "SP_LOAD_DAILY_FACTS": _R2_009,
           "SP_LOAD_APP_COST": _R2_011_APP + _C10, "SP_LOAD_STORAGE_TRUTH": _R2_011_STORAGE}


def _reverse(name: str, p: str) -> str:
    """The round-13 reversal: every declared new block out (or back to its old text), each exactly once."""
    for new, old in _DELTAS[name]:
        assert p.count(new) == 1, (name, new[:70])
        p = p.replace(new, old)
    return p


# -- generation ------------------------------------------------------------------------------------------------

def _gen_env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("V166_OUT", "PREFLIGHT_OUT", "PART_B_OUT", "REPAIR_OUT")}
    env.update(extra)
    return env


def _run_gen(tmp_path: Path, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v166.py")], env=_gen_env(**extra),
                          cwd=tmp_path, capture_output=True, text=True)


@pytest.fixture(scope="module")
def extras(tmp_path_factory) -> dict[str, str]:
    tmp = tmp_path_factory.mktemp("v166_extras")
    paths = {k: tmp / f"{k}.sql" for k in ("PREFLIGHT_OUT", "PART_B_OUT", "REPAIR_OUT")}
    result = _run_gen(tmp, V166_OUT=str(tmp / "m.sql"), **{k: str(v) for k, v in paths.items()})
    assert result.returncode == 0, result.stderr
    assert (tmp / "m.sql").read_text(encoding="utf-8") == _MIG          # the extras never change the migration
    return {k: v.read_text(encoding="utf-8") for k, v in paths.items()}


def test_v166_regenerates_byte_identical_and_writes_nothing_else(tmp_path):
    out = tmp_path / "regen.sql"
    result = _run_gen(tmp_path, V166_OUT=str(out))
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == (_MIGDIR / _NAME).read_bytes(), (
        "V166 drifted from its forward-generation -- edit outputs/gen_v166.py, not the .sql.")
    assert b"\r\n" not in out.read_bytes()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["regen.sql"]     # the extras only on request
    assert "wrote " not in result.stdout


def test_v166_generator_reads_only_its_four_bases_and_never_imports_app():
    gen = read("outputs/gen_v166.py")
    assert gen.count(".read_text(") == 4
    for base in ("V105__change_risk_create_or_replace_destructive.sql", "V101__fact_task_daily_retry_collapse.sql",
                 "V077__app_cost_ledger.sql", "V046__storage_truth.sql"):
        assert f'(MIG / "{base}").read_text(encoding="utf-8")' in gen, base
    assert not re.search(r"^\s*(?:from|import)\s+app\b", gen, re.M)
    assert "def _swap(text: str, old: str, new: str, label: str, n: int = 1)" in gen


@pytest.mark.parametrize("which", ["PREFLIGHT_OUT", "PART_B_OUT"])
def test_v166_preflight_and_part_b_are_read_only_and_parse(extras, which):
    sql = extras[which]
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


def test_v166_preflight_previews_the_repair_with_its_own_text(extras):
    pre = extras["PREFLIGHT_OUT"]
    assert pre.startswith("-- PREFLIGHT -- V166 section (READ-ONLY).")
    assert re.findall(r"^-- (P166\.\d) ", pre, re.M) == ["P166.1", "P166.2", "P166.3", "P166.4"]
    merge = _MIG[_MIG.index("MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY t"):]
    using = _between(merge, "USING (\n", "\n) s\n")[len("USING (\n"):]
    assert pre.count(using) == 1                                   # P166.1 runs the MERGE's own source SELECT
    assert "JOIN s ON t.DAY = s.DAY AND t.DATABASE_NAME = s.DATABASE_NAME" in pre      # ... on its own ON clause
    assert "OPTIONAL" in pre[pre.index("-- P166.4 "):].splitlines()[0]
    assert "TABLE(GENERATOR(ROWCOUNT => 401))" in pre               # the R2-011 calendar-gap grid
    # P166.4 (review r1): the session-age buckets need SESSIONS' full retention (the 'older than 30 days' bucket is
    # the C10-width owner question), so the label says so instead of '7 days of ... SESSIONS', and only the week's
    # sessions are aggregated
    s_cte = _between(pre[pre.index("-- P166.4 "):], "s AS (\n", "\n)\n")
    assert "WHERE SESSION_ID IN (SELECT SESSION_ID FROM q)" in s_cte and "GROUP BY 1" in s_cte
    flat = " ".join(pre.replace("--", " ").split())
    assert "SESSIONS full retention" in flat and "x SESSIONS)" not in flat


def test_v166_part_b_checks_each_proc_and_the_repair(extras):
    pb = extras["PART_B_OUT"]
    for sig in ("SP_LOAD_SECURITY_FACTS(FLOAT)", "SP_LOAD_DAILY_FACTS()", "SP_LOAD_APP_COST(FLOAT)",
                "SP_LOAD_STORAGE_TRUTH(FLOAT)"):
        assert f"GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.{sig}')" in pb, sig
    # every fragment PART B looks for is in the shipped proc, and every one it rules out is absent
    rows = re.findall(r"SELECT 'V166\.1 (\w+) is the V166 proc'(?: AS CHECK_NAME)?,\n\s+IFF\((.*?),\n", pb, re.S)
    assert [r[0] for r in rows] == list(_SIG)
    for name, cond in rows:
        present = re.findall(r"(?<!NOT )CONTAINS\(GET_DDL\('PROCEDURE', '[^']*'\), '([^']*)'\)", cond)
        absent = re.findall(r"NOT CONTAINS\(GET_DDL\('PROCEDURE', '[^']*'\), '([^']*)'\)", cond)
        assert present, name
        for frag in present:
            assert frag in _P[name], (name, frag)
        for frag in absent:
            assert frag not in _P[name], (name, frag)
            assert frag in _PB[name] or frag == "GREATEST_IGNORE_NULLS", (name, frag)   # teeth: the base had it
    assert pb.count("'V166.1 ") == 4 and "'V166.2 FACT_STORAGE_DAILY multi-ID name-days hold the SUM'" in pb
    assert "ALTER SESSION SET TIMEZONE" in pb.splitlines()[1]      # the combined PART B opens with the pin


def test_v166_repair_pins_central_first_and_guards_every_heal(extras):
    from tests.test_backfill_suspend_window import _statements
    rep = extras["REPAIR_OUT"]
    stmts = _statements(rep)
    assert stmts[0] == "ALTER SESSION SET TIMEZONE = 'America/Chicago'"
    blocks = [s for s in stmts if s.startswith("EXECUTE IMMEDIATE $$")]
    calls = [re.search(r"CALL DBA_MAINT_DB\.OVERWATCH\.(\w+)\(", b).group(1) for b in blocks]
    assert calls == ["SP_LOAD_SECURITY_FACTS", "SP_LOAD_STORAGE_TRUTH", "SP_LOAD_APP_COST"]
    assert not [s for s in stmts if s.upper().startswith("CALL ")]
    for b in blocks:
        body, _, handler = b.partition("\nEXCEPTION\n")
        assert len(re.findall(r"^\s*CALL DBA_MAINT_DB\.OVERWATCH\.", body, re.M)) == 1
        at = [ln.strip() for ln in body.splitlines()]
        call_at = next(i for i, ln in enumerate(at) if ln.startswith("CALL "))
        assert at[call_at + 1] == "SELECT $1 INTO :rv FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));"
        assert "WHEN OTHER THEN" in handler and "RETURN 'FAILED: '" in handler and "RAISE" not in handler
        assert "'OwnerRepair'" in handler and "'OwnerRepair'" in body
        assert re.search(r"'V166 ' \|\| [^,]*:n[^,]*, CURRENT_ROLE\(\)", b) or ":n" not in b
    # the security heal holds the hourly graph (test_v166_repair_security_heal_holds_the_hourly_graph); the verdicts
    # are the latest procs' own RETURNs
    sec, stor, app = blocks
    assert _HOURLY_SUSPEND in sec and "RETURN 'WAIT: " in sec
    assert not [b for b in (stor, app) if "ALTER TASK" in b or "called" in b]
    assert "rv = 'security facts loaded 180d'" in sec
    assert "RETURN 'security facts loaded ' || d || 'd';" in _P["SP_LOAD_SECURITY_FACTS"]
    for b, name in ((stor, "SP_LOAD_STORAGE_TRUTH"), (app, "SP_LOAD_APP_COST")):
        assert "rv = 'OK'" in b and _P[name].count("RETURN 'OK';") == 1
        assert "LEAST(" in b and ", 360)" in b                     # never past the 365-day sources
    assert "RETURN 'SKIP: FACT_STORAGE_ACCOUNT_DAILY has no calendar gap';" in stor
    assert "n := LEAST(GREATEST(n, 30), 360);" in app              # >= SESSION_PAD_DAYS: relabels C10 sessions
    # R166.1 admits exactly what the V166 d<=3 arm admits
    probe = _between(rep, "WITH qh AS (", "     GROUP BY 1\n")
    arm = _between(_P["SP_LOAD_SECURITY_FACTS"], "              AND EXECUTION_STATUS = 'SUCCESS'\n",
                   "        )\n        SELECT QUERY_ID")
    assert [ln.strip() for ln in arm.splitlines()] == [ln.strip() for ln in probe.splitlines()[5:]]
    sqlglot = pytest.importorskip("sqlglot")
    for s in stmts:
        if not s.startswith("EXECUTE IMMEDIATE"):
            assert sqlglot.parse_one(s, dialect="snowflake") is not None, s[:60]


_HOURLY = "DBA_MAINT_DB.OVERWATCH.TASK_LOAD_HOURLY"
_HOURLY_SUSPEND = f"ALTER TASK IF EXISTS {_HOURLY} SUSPEND;"
_HOURLY_RESUME = f"ALTER TASK IF EXISTS {_HOURLY} RESUME;"
_HOURLY_ENABLE = f"SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('{_HOURLY}');"


def test_v166_repair_security_heal_holds_the_hourly_graph(extras):
    """R166.2 (review r1). The d>3 arm commits each DELETE before its INSERT and FACT_SECURITY_CHANGE's key is not
    enforced. The old wall-clock guard (:30-:50 Central) checked only the start: a long scan, or a slow graph whose
    TASK_LOAD_SECURITY_FACTS ran at :30 or later, still overlapped the hourly insert (doubled rows), and a failed CALL
    left ~177 days deleted under a pane that did not say so. Now (the backfill_365 B12 pattern, which runs this same
    CALL inside its suspend window): SUSPEND the hourly root first, refuse (WAIT) while a graph run is still in flight
    (a suspended root finishes its current run), RESUME on every exit the block can catch, a standalone RESUME pair
    right after it for the exits it cannot (timeout / Stop), and the risk named in the header and both FAILED panes.
    The executed walk of every path is test_v166_harness.py::test_security_heal_paths."""
    from tests.test_backfill_suspend_window import _statements
    rep = extras["REPAIR_OUT"]
    stmts = _statements(rep)
    at = next(i for i, s in enumerate(stmts) if "SP_LOAD_SECURITY_FACTS(180);" in s)
    sec = stmts[at]
    assert [s.strip() + ";" for s in stmts[at + 1:at + 3]] == [_HOURLY_RESUME, _HOURLY_ENABLE]   # the safety net
    assert "MINUTE(" not in sec and "NOT BETWEEN" not in sec                                    # no wall-clock guess
    code = [ln.strip() for ln in sec.splitlines() if ln.strip() and not ln.strip().startswith("--")]
    begin = code.index("BEGIN")
    assert code[begin + 1] == _HOURLY_SUSPEND                     # before anything else the block does
    probe = " ".join(code[begin + 2:begin + 5])
    assert probe == ("SELECT COUNT(*) INTO :running FROM TABLE(DBA_MAINT_DB.INFORMATION_SCHEMA.CURRENT_TASK_GRAPHS("
                     "ROOT_TASK_NAME => 'TASK_LOAD_HOURLY')) WHERE DATABASE_NAME = 'DBA_MAINT_DB' AND SCHEMA_NAME = "
                     "'OVERWATCH' AND STATE = 'EXECUTING';")
    assert code[begin + 5] == "IF (running > 0) THEN"
    call = code.index("CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_SECURITY_FACTS(180);")
    assert code[call - 1] == "called := TRUE;"                    # the note is only for a CALL that started
    # every RETURN after the SUSPEND has a RESUME (+ dependents) as the nearest task statement above it
    for i, ln in enumerate(code):
        if ln.startswith("RETURN "):
            above = [x for x in code[:i] if x.startswith("ALTER TASK")]
            assert above[-1] == _HOURLY_RESUME, ln[:50]
    for i, ln in enumerate(code):
        if ln == _HOURLY_RESUME:
            assert code[i + 1] == _HOURLY_ENABLE
    # the risk: in the header and in both FAILED panes (the handler's only once the CALL started)
    head = _between(rep, "-- R166.2 ", "EXECUTE IMMEDIATE $$")
    flat = " ".join(head.replace("--", " ").split())
    for frag in ("FACT_SECURITY_LOGIN_DAILY", "commits each DELETE before its INSERT", "STATEMENT_TIMEOUT_IN_SECONDS",
                 "SUSPEND", "WAIT", "RESUME"):
        assert frag in flat, frag
    noted = [ln for ln in code if ln.startswith("RETURN 'FAILED: ") and "FACT_SECURITY_LOGIN_DAILY" in ln]
    assert len(noted) == 2 and all("re-run this block" in ln and "FACT_SECURITY_CHANGE" in ln for ln in noted)
    handler = code[code.index("EXCEPTION"):]
    assert handler[handler.index(noted[1]) - 1] == "IF (called) THEN"
    assert len([ln for ln in handler if ln.startswith("RETURN 'FAILED: ")]) == 2


# -- guard, order, shape ---------------------------------------------------------------------------------------

def test_v166_first_line_guard_and_version():
    assert _MIG.startswith(f"-- {_NAME}\n")
    assert "not_ready EXCEPTION (-20166, 'V166 requires V165 first - apply migrations in order.');" in _MIG
    assert "IF (v < 165) THEN" in _MIG and "RAISE not_ready;" in _MIG and "RAISE EXCEPTION (" not in _MIG
    assert "SELECT 166 AS VERSION" in _MIG and "WHERE VERSION = 166);" in _MIG
    guard165 = _between(_V165, "EXECUTE IMMEDIATE\n$$\n", "$$;\n")
    want = (guard165.replace("-20165", "-20166").replace("'V165 requires V164 first", "'V166 requires V165 first")
            .replace("IF (v < 164)", "IF (v < 165)"))
    assert _between(_MIG, "EXECUTE IMMEDIATE\n$$\n", "$$;\n") == want
    assert _MIG.count("EXECUTE IMMEDIATE") == 1
    assert _MIG.rstrip().endswith("WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION "
                                  "WHERE VERSION = 166);")


def test_v166_header_names_the_house_sections():
    header = _MIG[:_MIG.index("EXECUTE IMMEDIATE")]
    for section in ("-- WHY:", "-- COST:", "-- LATENCY:", "-- FIRST RUN:", "-- ROLLBACK:"):
        assert section in header, section
    assert header.rstrip().endswith("-- Apply AFTER V165. Idempotent; safe to re-run.")
    for base in ("re-derived from V105", "re-derived from V101", "re-derived from V077", "re-derived from V046"):
        assert base in header, base
    flat = " ".join(header.replace("--", " ").split())
    assert "now scans 33 days of SESSIONS instead of 10" in flat          # C10's cost, stated
    assert "$$" not in header and "'" not in header


def test_v166_file_order():
    guard = _MIG.index("EXCEPTION (-20166")
    marks = [_MIG.index(m) for m in _MARKERS.values()]
    repair = _MIG.index("MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY t")
    version = _MIG.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION")
    assert guard < marks[0] and marks == sorted(marks) and marks[-1] < repair < version
    for name, mark in _MARKERS.items():
        at = _MIG.index(mark) + len(mark)
        assert _MIG[at:].startswith(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{_SIG[name]}"), name
    assert repair > _MIG.rindex("$$;")                             # the repair runs after every proc swapped


def test_v166_nothing_runs_at_apply_and_no_new_object():
    from tests.test_migrations_parse import _plain_statements
    assert _MIG.count("CREATE OR REPLACE PROCEDURE") == 4 and _MIG.count("$$") == 10
    top = _strip_noise("".join(p for i, p in enumerate(_MIG.split("$$")) if i % 2 == 0))
    for banned in ("CALL", "DROP", "TASK", "GRANT", "REVOKE", "TRUNCATE", "UPDATE", "DELETE", "ALTER"):
        assert not re.search(rf"\b{banned}\b", top.replace("WHEN MATCHED THEN UPDATE SET", "")), banned
    kinds = [re.sub(r"^(?:--[^\n]*\n)+", "", s.strip()).split(None, 3)[:3] for s in _plain_statements(_MIG)]
    assert kinds == [["MERGE", "INTO", "DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY"],
                     ["INSERT", "INTO", "DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION"]], kinds
    assert not re.search(r"(?:CREATE(?:\s+OR\s+REPLACE)?\s+(?:TASK|TABLE|VIEW|FUNCTION)|ALTER\s+(?:TASK|TABLE)"
                         r"|EXECUTE\s+TASK)", _strip_noise(_MIG))
    assert not re.search(r"UPDATE\s+DBA_MAINT_DB\.OVERWATCH\.SOURCE_FRESHNESS_STATE", _MIG)
    assert "@" not in _MIG and not re.findall(r"\$[A-Za-z_][A-Za-z0-9_]*\$", _MIG)


def test_v166_description_fits_and_is_apostrophe_free():
    desc = re.search(r"SELECT 166 AS VERSION,\n       '(.*)' AS DESCRIPTION\n", _MIG, re.S).group(1)
    assert "\n" not in desc and "'" not in desc and len(desc) <= 4000
    for frag in ("re-derived from V105", "re-derived from V101", "SP_LOAD_APP_COST (V077)",
                 "SP_LOAD_STORAGE_TRUTH (V046)", "30 days before its reload start", "no procedure run at apply time"):
        assert frag in desc, frag


def test_v166_bodies_are_backslash_free_with_no_inner_dollar_quote():
    for name, p in _P.items():
        body = p[p.index("$$") + 2:p.rindex("$$")]
        assert "\\" not in body and "$$" not in body, name
    assert "\\" not in _MIG and "\r" not in _MIG


# -- lineage + round 13 ----------------------------------------------------------------------------------------

def _assert_v166_lineage(texts: dict[int, str]) -> None:
    """V166's own lineage, bounded at 166 like its siblings: a later migration that legitimately re-derives one
    of these procs must not turn this lock red."""
    from tests.test_proc_lineage import _HISTORICAL_WAIVERS, _definers, _lineage, _violations
    rows = {(r["v"], r["proc"]): r for r in _lineage(texts)}
    defs = _definers(texts)
    for name, (base, _) in _BASE.items():
        r = rows[(166, name)]
        assert r["src"] == "marker" and r["claims"] == [base] and r["prev"] == base and not r["waived"], r
        assert [v for v in defs[name] if base <= v <= 166] == [base, 166], name
    assert not [v for v in _violations(texts, _HISTORICAL_WAIVERS) if v.startswith("V166 ")]


def test_v166_markers_name_the_current_definers():
    from tests.test_proc_lineage import _migrations
    _assert_v166_lineage(_migrations())
    assert "LINEAGE-WAIVER" not in _MIG and _MIG.count("-- >>> derived:") == 4


def test_v166_lineage_lock_survives_a_later_re_derivation():
    from tests.test_proc_lineage import _migrations
    texts = _migrations()
    assert max(texts) < 999
    texts[999] = ("-- >>> derived:SP_LOAD_APP_COST  (from V166)\n"
                  "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_APP_COST(DAYS_BACK FLOAT)\n"
                  "RETURNS VARCHAR LANGUAGE SQL AS\n$$\nBEGIN\n    RETURN 'OK';\nEND;\n$$;\n")
    _assert_v166_lineage(texts)


@pytest.mark.parametrize("name", list(_SIG))
def test_v166_proc_normalizes_back_to_its_base_byte_for_byte(name):
    assert _reverse(name, _P[name]) == _PB[name]


@pytest.mark.parametrize(("name", "old", "new"), [
    ("SP_LOAD_SECURITY_FACTS", "        DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE\n"
                               "         WHERE DAY >= DATEADD('day', -:d, CURRENT_DATE());",
     "        DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE\n"
     "         WHERE DAY >= DATEADD('day', -:d - 1, CURRENT_DATE());"),
    ("SP_LOAD_SECURITY_FACTS", "lo_ts := (SELECT GREATEST(MIN(START_TIME)",
     "lo_ts := (SELECT GREATEST_IGNORE_NULLS(MIN(START_TIME)"),
    ("SP_LOAD_DAILY_FACTS", "WITH task_attempts AS (", "WITH task_attempts_x AS ("),
    ("SP_LOAD_APP_COST", "            RAISE;\n", "            NULL;\n"),
    ("SP_LOAD_APP_COST", "COALESCE(s.APPLICATION, '(unknown)') AS APPLICATION",
     "COALESCE(s.APPLICATION, '(no session record)') AS APPLICATION"),
    ("SP_LOAD_STORAGE_TRUTH", "    WHERE USAGE_DATE >= :lo\n", "    WHERE USAGE_DATE > :lo\n"),
])
def test_v166_normalize_has_teeth(name, old, new):
    assert _P[name].count(old) == 1, old
    mutated = _P[name].replace(old, new)
    try:
        reversed_ = _reverse(name, mutated)
    except AssertionError:            # the mutation broke a declared delta's anchor: caught all the same
        return
    assert reversed_ != _PB[name]


# -- R2-007: one bound, d<=3 arm only --------------------------------------------------------------------------

def _arms(p: str) -> tuple[str, str]:
    d3 = _between(p, "    IF (d <= 3) THEN\n", "\n    ELSE\n")
    other = _between(p, "\n    ELSE\n", "    END IF;\n")
    return d3, other


def test_v166_r2_007_delete_and_insert_share_one_bound():
    d3, _ = _arms(_P["SP_LOAD_SECURITY_FACTS"])
    assign = ("lo_ts := (SELECT GREATEST(MIN(START_TIME), DATEADD('day', -:d, CURRENT_DATE())::TIMESTAMP_LTZ)\n"
              "                    FROM DBA_MAINT_DB.OVERWATCH.OW_QH_EXTRACT);")
    assert d3.count(assign) == 1
    assert d3.count("WHERE EVENT_TS >= :lo_ts;") == 1 and d3.count("WHERE START_TIME >= :lo_ts") == 1
    assert (d3.index(assign) < d3.index("DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE")
            < d3.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE"))
    assert "(SELECT MIN(START_TIME)" not in d3                       # V100's DELETE subquery is gone
    assert "START_TIME >= DATEADD('day', -:d" not in d3              # ... and so is the INSERT's own bound
    assert "GREATEST_IGNORE_NULLS" not in _P["SP_LOAD_SECURITY_FACTS"]   # NULL keeps the empty extract a no-op
    assert "    lo_ts TIMESTAMP_LTZ;" in _P["SP_LOAD_SECURITY_FACTS"][:_P["SP_LOAD_SECURITY_FACTS"].index("\nBEGIN\n")]
    # START_TIME (OW_QH_EXTRACT) and EVENT_TS (FACT_SECURITY_CHANGE) are both TIMESTAMP_LTZ, like lo_ts
    assert re.search(r"START_TIME\s+TIMESTAMP_LTZ", read("snowflake/migrations/V041__loader_efficiency.sql"))
    assert re.search(r"EVENT_TS\s+TIMESTAMP_LTZ", read("snowflake/migrations/V075__security_operating_model.sql"))


def test_v166_r2_007_d_gt_3_arm_and_clamp_are_byte_identical():
    _, other = _arms(_P["SP_LOAD_SECURITY_FACTS"])
    _, other_base = _arms(_PB["SP_LOAD_SECURITY_FACTS"])
    assert other == other_base
    assert "FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY" in other and "lo_ts" not in other
    clamp = "    d := GREATEST(1, LEAST(COALESCE(DAYS_BACK, 3), 180))::INT;\n"
    assert _P["SP_LOAD_SECURITY_FACTS"].count(clamp) == 1 and _PB["SP_LOAD_SECURITY_FACTS"].count(clamp) == 1


# -- R2-009: SUM per name-day + the bounded repair -------------------------------------------------------------

def test_v166_r2_009_storage_arm_sums_and_v101_survives():
    daily = _P["SP_LOAD_DAILY_FACTS"]
    arm = _between(daily, "    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY", "GROUP BY 1, 2, 3;")
    assert "SUM(COALESCE(AVERAGE_DATABASE_BYTES, 0))" in arm and "SUM(COALESCE(AVERAGE_FAILSAFE_BYTES, 0))" in arm
    assert "AVG(COALESCE(AVERAGE_" not in daily
    assert daily.count("WITH task_attempts AS (") == 1                # V101's retry collapse (round 13)
    assert daily.count("GROUP BY 1, 2, 3;") == 2                      # login + storage arms, both kept
    assert "COMPANY_FOR_DATABASE(DATABASE_NAME)," in arm              # V030: the UDF on a plain column


def test_v166_r2_009_backfill_365_mirrors_the_loader_sum():
    """backfill_365.sql's storage arm is a copy of the loader's (its header says keep them in sync): it SUMs too, so
    a backfilled year and the loader's trailing days share one basis."""
    bf = read("snowflake/backfill_365.sql")
    arm = _between(bf, "INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY", "GROUP BY 1, 2, 3;")
    loader = _between(_P["SP_LOAD_DAILY_FACTS"], "    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY",
                      "GROUP BY 1, 2, 3;")
    for expr in ("SUM(COALESCE(AVERAGE_DATABASE_BYTES, 0))", "SUM(COALESCE(AVERAGE_FAILSAFE_BYTES, 0))",
                 "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME)"):
        assert expr in arm and expr in loader, expr
    assert "AVG(COALESCE(AVERAGE_" not in bf


def test_v166_r2_009_repair_is_bounded_idempotent_and_matches_the_loader():
    repair = _between(_MIG, "MERGE INTO DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY t", ";\n")
    assert "HAVING COUNT(*) > 1" in repair                            # multi-ID name-days only
    assert "WHERE USAGE_DATE >= DATEADD('day', -365, CURRENT_DATE())" in repair
    assert "ON t.DAY = s.DAY AND t.DATABASE_NAME = s.DATABASE_NAME" in repair
    assert repair.rstrip().endswith("WHEN MATCHED THEN UPDATE SET DB_BYTES = s.DB_BYTES, "
                                    "FAILSAFE_BYTES = s.FAILSAFE_BYTES")
    assert "WHEN NOT MATCHED" not in repair and "LOAD_TS" not in repair and "DELETE" not in repair
    # the repair's byte expressions are the loader's own
    for expr in ("SUM(COALESCE(AVERAGE_DATABASE_BYTES, 0))", "SUM(COALESCE(AVERAGE_FAILSAFE_BYTES, 0))"):
        assert expr in repair and expr in _P["SP_LOAD_DAILY_FACTS"]
    outside = "".join(p for i, p in enumerate(_MIG.split("$$")) if i % 2 == 0)
    assert "DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY" not in outside


# -- R2-011: one transaction, logged, re-raised ----------------------------------------------------------------

@pytest.mark.parametrize(("name", "fact", "page"), [("SP_LOAD_APP_COST", "FACT_APP_COST_DAILY", "AppCost"),
                                                    ("SP_LOAD_STORAGE_TRUTH", "FACT_STORAGE_ACCOUNT_DAILY",
                                                     "StorageTruth")])
def test_v166_r2_011_statement_order_and_dml_only_transaction(name, fact, page):
    p = _P[name]
    order = [p.index("    BEGIN TRANSACTION;\n"), p.index(f"    DELETE FROM DBA_MAINT_DB.OVERWATCH.{fact}"),
             p.index(f"    INSERT INTO DBA_MAINT_DB.OVERWATCH.{fact}"), p.index("    COMMIT;\n"),
             p.index("    EXCEPTION\n"), p.index("            ROLLBACK;\n"),
             p.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG"), p.index("            RAISE;\n"),
             p.index("MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE")]
    assert order == sorted(order), order
    assert p.count("BEGIN TRANSACTION;") == 1 and p.count("COMMIT;") == 1 and p.count("RAISE;") == 1
    txn = _strip_noise(_between(p, "    BEGIN TRANSACTION;\n", "    COMMIT;\n"))
    assert not re.search(r"\b(?:CREATE|ALTER|DROP|TRUNCATE|GRANT|REVOKE|MERGE)\b", txn)   # B34: no implicit commit
    assert txn.count("DELETE FROM") == 1 and txn.count("INSERT INTO") == 1
    handler = _between(p, "    EXCEPTION\n", "    END;\n")
    assert f"SELECT '{page}', 'fact_load_failed', LEFT(:emsg, 2000), '{fact} - " in handler
    assert "    emsg VARCHAR;" in p[:p.index("\nBEGIN\n")]
    # the declared base had no wrap at all (the defect)
    assert "BEGIN TRANSACTION" not in _PB[name] and "EXCEPTION" not in _PB[name]


def test_v166_r2_011_logged_error_fits_the_app_error_log_column():
    """APP_ERROR_LOG.ERROR_MESSAGE is VARCHAR(2000) (V001; no later migration alters the table). Snowflake rejects an
    over-long string instead of truncating it, so a raw SQLERRM past 2000 characters would fail the handler's INSERT,
    raise THAT error in place of the load's, and leave no fact_load_failed row for the self-watch ERR leg,
    SP_ALERT_SCAN_DAILY or NATIVE_ALERT_STALE_FACTS. The handler binds the message cut to the column width."""
    from tests.test_proc_lineage import _migrations
    v001 = _between(read("snowflake/migrations/V001__core.sql"), "TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (",
                    ");")
    width = int(re.search(r"\n\s+ERROR_MESSAGE\s+VARCHAR\((\d+)\),", v001).group(1))
    assert width == 2000
    later = [v for v, t in _migrations().items() if v > 1 and re.search(r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?"
                                                                       r"DBA_MAINT_DB\.OVERWATCH\.APP_ERROR_LOG\b", t)]
    assert later == [], later
    for name in ("SP_LOAD_APP_COST", "SP_LOAD_STORAGE_TRUTH"):
        handler = _between(_P[name], "    EXCEPTION\n", "    END;\n")
        assert handler.count(f"'fact_load_failed', LEFT(:emsg, {width}), '") == 1, name
        assert not re.search(r"'fact_load_failed', :emsg\b", handler), name


def test_v166_r2_011_failure_rows_reach_the_three_alert_legs():
    """fact_load_failed + a CONTEXT whose first word is the fact name is what SP_ALERT_SCAN's self-watch ERR leg,
    SP_ALERT_SCAN_DAILY and NATIVE_ALERT_STALE_FACTS already read (latest definers, so a re-derivation of either
    scan is checked too)."""
    from tests.test_alert_rule_consistency import _latest_proc_bodies
    bodies = _latest_proc_bodies()
    native = read("snowflake/native_alert_templates.sql")
    for leg in (bodies["SP_ALERT_SCAN"], bodies["SP_ALERT_SCAN_DAILY"], native):
        assert re.search(r"ERROR_TYPE IN \([^)]*'fact_load_failed'", leg)
    excluded = re.search(r"SPLIT_PART\(COALESCE\(CONTEXT, ''\), ' ', 1\) NOT IN \(([^)]*)\)", bodies["SP_ALERT_SCAN"])
    skip = set(re.findall(r"'(\w+)'", excluded.group(1))) if excluded else set()
    for name, fact in (("SP_LOAD_APP_COST", "FACT_APP_COST_DAILY"),
                       ("SP_LOAD_STORAGE_TRUTH", "FACT_STORAGE_ACCOUNT_DAILY")):
        ctx = re.search(r"'fact_load_failed', LEFT\(:emsg, 2000\), '([^']*)'", _P[name]).group(1)
        assert ctx.split(" ", 1)[0] == fact and fact not in skip, (name, ctx)


# -- C10: the session pad -------------------------------------------------------------------------------------

def _latest_app_cost() -> str:
    from tests.test_alert_rule_consistency import _latest_proc_bodies
    return _latest_proc_bodies()["SP_LOAD_APP_COST"]


def test_v166_c10_session_pad_is_the_apps_and_the_label_is_unchanged():
    from app.data import app_cost_sql, mart_sql
    p = _P["SP_LOAD_APP_COST"]
    assert p.count("CREATED_ON >= DATEADD('day', -30, :lo)") == 1 and "DATEADD('day', -7, :lo)" not in p
    # parity: the LATEST definer's pad (not just V166's) equals the live twin's
    pads = re.findall(r"CREATED_ON >= DATEADD\('day', -(\d+), :lo\)", _latest_app_cost())
    assert pads == [str(app_cost_sql.SESSION_PAD_DAYS)]
    # the '(unknown)' bucket is unchanged: both consumers filter exactly that label
    assert p.count("COALESCE(s.APPLICATION, '(unknown)')") == 1 and "(no session record)" not in p
    from tests.test_alert_rule_consistency import _latest_proc_bodies
    assert "a.APPLICATION <> '(unknown)'" in _latest_proc_bodies()["SP_SCAN_SLEEP_POLLING"]
    assert "c.APPLICATION <> '(unknown)'" in mart_sql.cloud_svc_billed_families(7)
    # the cred / q scans are unchanged (QUERY_ATTRIBUTION_HISTORY.START_TIME is the query's own start)
    assert p.count("WHERE START_TIME >= :lo\n") == 2


# -- parse ----------------------------------------------------------------------------------------------------

def test_v166_plain_sql_parses():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    stmts = list(_plain_statements(_MIG))
    assert len(stmts) == 2
    for stmt in stmts:
        assert sqlglot.parse_one(stmt, dialect="snowflake") is not None


_BINDS = {"d": "3", "lo_ts": "'2026-09-28 00:00:00'", "lo": "'2026-09-28'", "DAYS_BACK": "3", "emsg": "'e'",
          "trust_ok": "TRUE", "lo_storage": "'2026-09-28 00:00:00'"}


def test_v166_changed_statements_parse():
    """The SQL statements V166 touched (or wrapped) parse as Snowflake SQL once their :binds are literals."""
    sqlglot = pytest.importorskip("sqlglot")
    sec = _P["SP_LOAD_SECURITY_FACTS"]
    picked = [
        _between(sec, "lo_ts := (", ");\n")[len("lo_ts := ("):],
        _between(sec, "        DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE\n         WHERE EVENT_TS",
                 ";\n"),
        _between(sec, "        INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_SECURITY_CHANGE", ";\n"),
        _between(_P["SP_LOAD_DAILY_FACTS"], "    INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_STORAGE_DAILY", ";\n"),
    ]
    for name, fact in (("SP_LOAD_APP_COST", "FACT_APP_COST_DAILY"),
                       ("SP_LOAD_STORAGE_TRUTH", "FACT_STORAGE_ACCOUNT_DAILY")):
        p = _P[name]
        picked += [_between(p, f"    DELETE FROM DBA_MAINT_DB.OVERWATCH.{fact}", ";\n"),
                   _between(p, f"    INSERT INTO DBA_MAINT_DB.OVERWATCH.{fact}", ";\n"),
                   _between(p, "INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG", ";\n")]
    assert len(picked) == 10
    assert picked[2].rstrip().endswith("FROM raw") and picked[3].rstrip().endswith("GROUP BY 1, 2, 3")
    for stmt in picked:
        bound = re.sub(r"(?<![:\w]):([A-Za-z_]\w*)", lambda m: _BINDS[m.group(1)], stmt)
        assert bound.lstrip().startswith(("DELETE", "INSERT", "SELECT")), stmt[:70]
        assert sqlglot.parse_one(bound, dialect="snowflake") is not None, stmt[:70]


def test_v166_in_expected_migrations():
    """Integrator lockstep: Admin lists V166 with house-rule text (no $, no hand CALL, no trailing '.')."""
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    text = str(_EXPECTED_MIGRATIONS[166])
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_" not in text and "$" not in text and not text.endswith(".")
    for frag in ("SP_LOAD_SECURITY_FACTS", "FACT_STORAGE_DAILY", "SP_LOAD_APP_COST", "SP_LOAD_STORAGE_TRUTH"):
        assert frag in text, frag
