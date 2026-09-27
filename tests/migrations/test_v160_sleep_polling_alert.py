"""Locks for V160 -- COST_SLEEP_POLLING, the chronic SYSTEM$WAIT sleep-polling alert (v4.596.0).

STRUCTURE + GENERATION + LOCKS only; the sqlite harness that EXECUTES the census / raise / hold / supersede /
clear text lives in its own module. What this file proves, from the shipped text:
  * generation -- outputs/gen_v160.py regenerates the migration byte-for-byte; the read-only PREFLIGHT is written
    only on request, parses, writes nothing and carries the proc's census chain verbatim;
  * shape -- first line, guard (-20160, v < 159), TRANSIENT SLEEP_POLLING_WEEKLY (26 columns), the WHEN NOT
    MATCHED seed, the new SP_SCAN_SLEEP_POLLING(BOOLEAN), the marker + SP_ALERT_SCAN_DAILY, the version row;
    nothing runs at apply time;
  * lineage + round 13 -- SP_ALERT_SCAN_DAILY is re-derived from V157 (its immediately previous definer) and
    cutting arm [25] and reversing the four declared literal deltas gives V157's body back byte-for-byte;
  * the new proc -- no EXCEPTION handler, mart-only, Central-pinned, statement order gate -> ready -> defer ->
    DELETE -> census -> raise -> supersede -> clear -> receipt (the receipt the LAST DML), every SQL statement
    parses (sqlglot, snowflake) once the :binds are literals;
  * the DEDUPE_KEY suffix parsing (SUBSTR -16/6, -15/5, LENGTH - 15/14) holds on MED and HIGH keys built in
    Python, TITLE carries exactly one '$', DETAIL / TITLE are LEFT-bounded to the ALERT_EVENTS widths;
  * the RUN_NEXT PART B GET_DDL fragments (spec 3.7 V160.2 / V160.3);
  * the v4.596.0 app lockstep (playbook, navigation, Admin, validate floor, docs, teardown, RUNBOOK).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_migration_proc_syntax import _strip_noise

_ROOT = Path(__file__).resolve().parents[2]
_MIGDIR = _ROOT / "snowflake" / "migrations"
_NAME = "V160__sleep_polling_alert.sql"
_MIG = (_MIGDIR / _NAME).read_text(encoding="utf-8")
_V157 = (_MIGDIR / "V157__alert_scan_self_watch_idle_push.sql").read_text(encoding="utf-8")
_RULE = "COST_SLEEP_POLLING"


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _proc(text: str, name: str) -> str:
    s = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    o = text.index("$$", s)
    return text[s:text.index("$$;", o + 2) + 3]


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


def _norm(text: str) -> str:
    return " ".join(text.split())


_NEW = _proc(_MIG, "SP_SCAN_SLEEP_POLLING(")
_BODY = _NEW[_NEW.index("$$") + 2:_NEW.rindex("$$")]
_D = _proc(_MIG, "SP_ALERT_SCAN_DAILY()")
_D157 = _proc(_V157, "SP_ALERT_SCAN_DAILY()")
_H157 = _proc(_V157, "SP_ALERT_SCAN()")

# ---------------------------------------------------------------------------------------------------
# Test-side copies (independent of outputs/gen_v160.py).
# ---------------------------------------------------------------------------------------------------
_MARKER = ("-- >>> derived:SP_ALERT_SCAN_DAILY  (from V157; + [25] COST_SLEEP_POLLING counting CALL arm, "
           "tally 11 -> 12, V160)\n")
_ARM25_HEAD = ("    -- [25] COST_SLEEP_POLLING (V160: chronic SYSTEM$WAIT sleep polling, the DB-side push of Cost > "
               "Spend > Which\n")
_ARM25_CODE = (
    "    BEGIN\n"
    "        CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_SLEEP_POLLING(FALSE);\n"
    "    EXCEPTION\n"
    "        WHEN OTHER THEN\n"
    "            emsg := SQLERRM;\n"
    "            fails := fails + 1;\n"
    "            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG\n"
    "                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)\n"
    "            SELECT 'AlertScan', 'rule_block_failed', :emsg,\n"
    "                   'rule COST_SLEEP_POLLING - other rules unaffected', CURRENT_ROLE();\n"
    "    END;\n"
)
_ANCHOR_17 = "    -- [17] PIPE_REF_GAP"
_ANCHOR_24 = "    -- [24] COST_IDLE_OPPORTUNITY"
_ANCHOR_25 = "    -- [25] COST_SLEEP_POLLING"
_SELF_11 = "' of 11 daily alert rule block(s) failed this run'"
_SELF_12 = "' of 12 daily alert rule block(s) failed this run'"
_RET_157 = ("'alert scan daily v3 (V157: + OPS_PIPELINE_DEGRADED self-watch + COST_IDLE_OPPORTUNITY + "
            "heartbeat): '")
_RET_160 = "'alert scan daily v4 (V160: + COST_SLEEP_POLLING weekly sleep-polling push): '"
_FAILS_INC = "fails := fails + 1"

_SEED_TUPLE = (
    "        ('COST_SLEEP_POLLING', 'COST', 'Chronic sleep polling: one poller (warehouse + user, or task owner "
    "role) slept on 5+ of the 7 newest complete days and billed at least the threshold in USD per week of "
    "cloud services', TRUE, 'MEDIUM', 25, 168, TRUE)\n")
_SEED_COLS = "(RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS, AUTO_CLEAR_ENABLED)"

_TABLE_COLS = ("WEEK_START", "ROW_KIND", "POLLER_KEY", "WAREHOUSE_NAME", "USER_NAME", "TASK_ROLE", "USER_TOP_APP",
               "OWNER_HINT", "CALLS", "FAMILIES", "POLLERS", "RUNS", "ACTIVE_DAYS", "LAST_ACTIVE_DAY", "CS_CREDITS",
               "BILLED_CS_CREDITS", "USD_WEEK", "ABOVE_ALLOWANCE_DAYS", "WIN_START", "WIN_END", "CREDIT_PRICE_USD",
               "RAISED", "SUPERSEDED", "CLEARED", "EVALUATED_AT", "COMPLETED_AT")
_CENSUS_COLS = _TABLE_COLS[:21]          # the [snapshot] INSERT fills every column but the receipt's five

# the sleep signature: app/logic/system_wait.SLEEP_SQL_PATTERN with each LF as the two-character escape \n
# (tests/test_sleep_polling_parity.py locks it to the app module; here it is a literal copy)
_SLEEP_RE_LITERAL = (r"^[[:space:]]*((/[*]([^*]|[*]+[^*/])*[*]+/|--[^\n]*\n|//[^\n]*\n)[[:space:]]*)*"
                     r"(SELECT|CALL)[[:space:]]+SYSTEM[$]WAIT[[:space:]]*[(]")

_RETURNS = (
    "RETURN 'sleep polling scan skipped (COST_SLEEP_POLLING disabled or missing)'",
    "RETURN 'sleep polling scan skipped (week of ' || TO_VARCHAR(:week_start) || ' already evaluated)'",
    "RETURN 'sleep polling scan deferred (data not complete)'",
    "RETURN 'sleep polling scan week of ' || TO_VARCHAR(:week_start) || ': ' || :n_pollers || ' poller(s) over ' "
    "|| TO_VARCHAR(DATEADD('day', -7, :newest)) || '..' || TO_VARCHAR(DATEADD('day', -1, :newest)) "
    "|| '; raised ' || :n_raised || ', superseded ' || :n_superseded || ', cleared ' || :n_cleared",
)


# ---------------------------------------------------------------------------------------------------
# Scanners. The sleep pattern holds '--' and '//' INSIDE a string literal, so every comment strip here is
# string-aware (the test_migration_proc_syntax._strip_noise shape); _code keeps the strings, _strip_noise
# drops them too.
# ---------------------------------------------------------------------------------------------------
def _code(sql: str) -> str:
    """Comments (--, // and /* */) out, single-quoted literals ('' escapes) KEPT verbatim."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        two = sql[i:i + 2]
        if two in ("--", "//"):
            j = sql.find("\n", i)
            i = n if j < 0 else j
        elif two == "/*":
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif sql[i] == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(sql[i:j])
            i = j
        else:
            out.append(sql[i])
            i += 1
    return "".join(out)


def _split(code: str) -> list[str]:
    """Split comment-free code on ';' outside string literals (a ';' sits inside the final RETURN string)."""
    parts, buf, in_str = [], [], False
    i, n = 0, len(code)
    while i < n:
        ch = code[i]
        buf.append(ch)
        if in_str:
            if ch == "'":
                if i + 1 < n and code[i + 1] == "'":
                    buf.append("'")
                    i += 1
                else:
                    in_str = False
        elif ch == "'":
            in_str = True
        elif ch == ";":
            parts.append("".join(buf[:-1]).strip())
            buf = []
        i += 1
    tail = "".join(buf).strip()
    return [p for p in [*parts, tail] if p]


_CHUNKS = _split(_code(_BODY))
_SQL_START_RE = re.compile(r"^[ \t]*(SELECT|INSERT|UPDATE|DELETE|MERGE)\b", re.M)
_INTO_LINE_RE = re.compile(r"\n[ \t]*INTO :[^\n]*")
_STR_RE = re.compile(r"'(?:[^']|'')*'")
_BIND_RE = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")
_BINDS = {"today_ct": "TO_DATE('2026-09-28')", "week_start": "TO_DATE('2026-09-28')",
          "newest": "TO_DATE('2026-09-28')", "credit_price": "3.68", "FORCE_RUN": "FALSE", "n_days": "7",
          "n_pollers": "2", "n_raised": "1", "n_superseded": "0", "n_cleared": "0"}


def _sql_statements(chunks: list[str]) -> list[str]:
    """The SQL statements of the body, scripting prefix (BEGIN / IF (...) THEN) dropped, SELECT ... INTO's
    INTO line removed (a scripting clause, not SQL)."""
    out = []
    for chunk in chunks:
        m = _SQL_START_RE.search(chunk)
        if m:
            out.append(_INTO_LINE_RE.sub("", chunk[m.start():]).strip())
    return out


def _bind(sql: str) -> str:
    """Replace every :bind OUTSIDE string literals ('[[:space:]]' is not a bind) with a literal; an unknown
    bind raises (fail closed)."""
    out, last = [], 0
    for m in _STR_RE.finditer(sql):
        out.append(_BIND_RE.sub(lambda b: _BINDS[b.group(1)], sql[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(_BIND_RE.sub(lambda b: _BINDS[b.group(1)], sql[last:]))
    return "".join(out)


def _target(stmt: str) -> tuple[str, str]:
    m = re.match(r"(SELECT|INSERT INTO|UPDATE|DELETE FROM|MERGE INTO)\s+(?:DBA_MAINT_DB\.OVERWATCH\.(\w+))?", stmt)
    assert m, stmt[:80]
    return m.group(1), m.group(2) or ""


def _gen_env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("PREFLIGHT_OUT", "V160_OUT")}
    env.update(extra)
    return env


def _run_gen(tmp_path: Path, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v160.py")], env=_gen_env(**extra),
                          cwd=tmp_path, capture_output=True, text=True)


# -- generation --------------------------------------------------------------------------------------------

def test_v160_regenerates_byte_identical_and_writes_nothing_else(tmp_path):
    out = tmp_path / "regen.sql"
    result = _run_gen(tmp_path, V160_OUT=str(out))
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == (_MIGDIR / _NAME).read_bytes(), (
        "V160 drifted from its forward-generation -- edit outputs/gen_v160.py, not the .sql.")
    assert b"\r\n" not in out.read_bytes()                       # LF only: the pattern literal is CRLF-proof
    # the PREFLIGHT is written only on request: nothing but the migration lands anywhere we can see
    assert sorted(p.name for p in tmp_path.iterdir()) == ["regen.sql"]
    assert "PREFLIGHT" not in result.stdout


def _preflight(tmp_path: Path) -> str:
    pre = tmp_path / "PREFLIGHT_SLEEP_POLLING.sql"
    result = _run_gen(tmp_path, V160_OUT=str(tmp_path / "m.sql"), PREFLIGHT_OUT=str(pre))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "m.sql").read_text(encoding="utf-8") == _MIG     # PREFLIGHT_OUT never changes the migration
    return pre.read_text(encoding="utf-8")


def test_v160_preflight_is_one_read_only_select(tmp_path):
    sql = _preflight(tmp_path)
    assert sql.startswith("-- PREFLIGHT_SLEEP_POLLING.sql -- READ-ONLY preview")
    code = _strip_noise(sql)                       # comments + strings out ('CREATE%' is a literal, not DDL)
    for banned in ("INSERT", "UPDATE", "DELETE", "MERGE", "CALL", "CREATE", "ALTER", "DROP", "TRUNCATE",
                   "GRANT", "EXECUTE"):
        assert not re.search(rf"\b{banned}\b", code, re.I), banned
    assert not _BIND_RE.search(code), "a scripting :bind survived into the PREFLIGHT"
    assert code.count(";") == 1 and code.rstrip().endswith(";")
    assert "$$" not in sql and "SYSTEM$WAIT$" not in sql
    sqlglot = pytest.importorskip("sqlglot")
    from sqlglot import exp
    parsed = sqlglot.parse(sql, dialect="snowflake")
    assert len(parsed) == 1 and parsed[0].key == "select"
    writes = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Command)
    assert not [type(n).__name__ for n in parsed[0].walk() if isinstance(n, writes)]
    ctes = [c.alias for c in parsed[0].find(exp.With).expressions]
    assert ctes[:4] == ["clk", "mx", "px", "thr"] and ctes[-1] == "census"
    assert {"bill", "win", "fd", "fam", "sleepfam", "sd", "pd", "pb", "pc", "app", "acct", "nf", "np"} <= set(ctes)


def test_v160_preflight_carries_the_proc_census_verbatim(tmp_path):
    """The PREFLIGHT is generated from the SAME census text as the [snapshot] INSERT, binds swapped for CTE
    reads -- so what the owner previews is what the proc will write."""
    sql = _preflight(tmp_path)
    chain = _BODY[_BODY.index("    WITH bill AS ("):_BODY.index("\n    SELECT w2.* FROM (\n")]
    chain = chain.replace("    WITH bill AS (", "    bill AS (", 1).replace(":newest", "(SELECT NEWEST FROM mx)")
    assert chain in sql
    sel = _between(_BODY, "    SELECT :week_start, 'POLLER'", "\n    ) w2;")
    sel = sel.replace(":week_start", "(SELECT WEEK_START FROM clk)").replace(":credit_price", "(SELECT PRICE FROM px)")
    assert sel in sql
    assert "census (" + ", ".join(_CENSUS_COLS) + ") AS (" in sql
    # the clock and price are the proc's own (Central week; FLOAT price, V153)
    assert ("DATEADD('day', 1 - DAYOFWEEKISO(CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE),"
            in sql)
    assert "COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68) AS PRICE" in sql
    assert "COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68) AS PRICE" in _BODY
    # WOULD_RAISE / BAND / key preview are the proc's raise rule (5+ of 7 days, >= THR, HIGH at 5x, 25 default)
    assert "c.ACTIVE_DAYS >= 5 AND c.USD_WEEK >= t.THR" in sql and "IFF(c.USD_WEEK >= t.THR * 5, 'HIGH', 'MED')" in sql
    assert "GREATEST(COALESCE(c.THRESHOLD_NUM, 25), 1)" in sql
    assert "s.ACTIVE_DAYS >= 5" in _BODY and "JOIN cfg c ON s.USD_WEEK >= c.THR" in _BODY
    assert "IFF(s.USD_WEEK >= c.THR * 5, 2, 1) AS BAND_RANK" in _BODY
    assert "GREATEST(COALESCE(THRESHOLD_NUM, 25), 1) AS THR" in _BODY
    assert ("c.POLLER_KEY || IFF(c.USD_WEEK >= t.THR * 5, 'HIGH', 'MED') || '|'\n"
            "                                  || TO_VARCHAR(c.WEEK_START)") in sql


# -- guard, order, shape -------------------------------------------------------------------------------------

def test_v160_first_line_guard_and_version():
    assert _MIG.startswith(f"-- {_NAME}\n")
    assert "not_ready EXCEPTION (-20160, 'V160 requires V159 first - apply migrations in order.');" in _MIG
    assert "IF (v < 159) THEN" in _MIG
    assert "SELECT 160 AS VERSION" in _MIG and "WHERE VERSION = 160);" in _MIG
    # the guard is V157's shape (V157:94-105) with only the numbers moved
    guard157 = _between(_V157, "EXECUTE IMMEDIATE\n$$\n", "$$;\n")
    want = (guard157.replace("-20157", "-20160").replace("'V157 requires V156 first", "'V160 requires V159 first")
            .replace("IF (v < 156)", "IF (v < 159)"))
    assert _between(_MIG, "EXECUTE IMMEDIATE\n$$\n", "$$;\n") == want
    assert _MIG.count("EXECUTE IMMEDIATE") == 1
    assert _MIG.rstrip().endswith("WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION "
                                  "WHERE VERSION = 160);")


def test_v160_file_order():
    guard = _MIG.index("EXCEPTION (-20160")
    table = _MIG.index("CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.SLEEP_POLLING_WEEKLY (")
    seed = _MIG.index("MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t")
    new = _MIG.index("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_SCAN_SLEEP_POLLING(FORCE_RUN BOOLEAN)")
    mark = _MIG.index(_MARKER)
    daily = _MIG.index("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()")
    version = _MIG.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION")
    assert guard < table < seed < new < mark < daily < version
    assert _MIG[mark + len(_MARKER):].startswith(
        "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()")     # marker heads its proc


def test_v160_two_procs_one_table_one_seed_nothing_runs_at_apply():
    from tests.test_migrations_parse import _plain_statements
    assert _MIG.count("CREATE OR REPLACE PROCEDURE") == 2
    assert _MIG.count("$$") == 6                                   # guard + two proc bodies, nothing else
    assert "$$" not in _MIG[:_MIG.index("EXECUTE IMMEDIATE")]       # never in the header comments
    assert "SYSTEM$WAIT$" not in _MIG
    top = _strip_noise("".join(p for i, p in enumerate(_MIG.split("$$")) if i % 2 == 0))
    for banned in ("CALL", "ALTER", "DROP", "TASK", "GRANT", "REVOKE", "TRUNCATE", "UPDATE", "DELETE"):
        assert not re.search(rf"\b{banned}\b", top), banned
    kinds = [re.sub(r"^(?:--[^\n]*\n)+", "", s.strip()).split(None, 3)[:3] for s in _plain_statements(_MIG)]
    assert kinds == [["CREATE", "TRANSIENT", "TABLE"], ["MERGE", "INTO", "DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG"],
                     ["INSERT", "INTO", "DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION"]], kinds
    # the only CALL of the new proc is the counting arm inside the daily body (never TRUE, never at apply time)
    assert _MIG.count("CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_SLEEP_POLLING(FALSE);") == 1
    assert _D.count("CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_SLEEP_POLLING(FALSE);") == 1
    assert "SP_SCAN_SLEEP_POLLING(TRUE)" not in _MIG
    code = _strip_noise(_MIG)                      # [22]'s DETAIL says 'ALTER TASK ... RESUME' inside a string
    assert not re.search(r"(?:CREATE(?:\s+OR\s+REPLACE)?\s+TASK|ALTER\s+TASK|EXECUTE\s+TASK)", code)
    assert "SETTINGS (KEY" not in _MIG and "REQUIRED_SCHEMA_FLOOR" not in _MIG     # no SETTINGS key


def test_v160_no_tagged_dollar_quote(tmp_path):
    tagged = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$")          # test_migrations_parse's Snowsight rule
    assert not tagged.findall(_MIG)
    assert not tagged.findall(_preflight(tmp_path))


def test_v160_table_is_the_26_column_transient_census():
    ddl = _between(_MIG, "CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.SLEEP_POLLING_WEEKLY (", "\n);")
    cols = re.findall(r"^    (\w+)\s+(\w+(?:\(\d+(?:,\d+)?\))?)([^\n]*)$", ddl, re.M)
    assert tuple(c for c, _t, _r in cols) == _TABLE_COLS and len(cols) == 26          # PART B V160.4
    not_null = {c for c, _t, rest in cols if "NOT NULL" in rest.split("--")[0]}
    assert not_null == {"WEEK_START", "ROW_KIND", "POLLER_KEY", "EVALUATED_AT"}
    assert "EVALUATED_AT         TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()," in ddl
    types = {c: t for c, t, _r in cols}
    assert types["CREDIT_PRICE_USD"] == "FLOAT" and types["USD_WEEK"] == "NUMBER(18,2)"
    assert types["WEEK_START"] == "DATE" and types["COMPLETED_AT"] == "TIMESTAMP_NTZ"
    # the census INSERT names exactly the first 21 columns, every NOT NULL column without a default included
    ins = _between(_BODY, "    INSERT INTO DBA_MAINT_DB.OVERWATCH.SLEEP_POLLING_WEEKLY\n", ")\n    WITH bill")
    assert tuple(re.findall(r"\w+", ins.split("(", 1)[1])) == _CENSUS_COLS
    assert {"WEEK_START", "ROW_KIND", "POLLER_KEY"} <= set(_CENSUS_COLS)
    # not a FACT_/MART_ table: tests/test_freshness_coverage owes it no freshness stamp
    assert not re.search(r"\b(?:FACT|MART)_\w*SLEEP", _MIG)


def _width(ddl_cols: str, col: str) -> int:
    m = re.search(rf"^\s*{col}\s+VARCHAR\((\d+)\)", ddl_cols, re.M)
    assert m, col
    return int(m.group(1))


def test_v160_census_strings_fit_their_columns():
    """Snowflake rejects (never truncates) an INSERT of a longer string: every census string is bounded by the
    source widths (MART_CLOUD_SVC_DAILY V055, FACT_APP_COST_DAILY V077) or a LEFT(...)."""
    ddl = _between(_MIG, "SLEEP_POLLING_WEEKLY (", "\n);")
    v055 = (_MIGDIR / "V055__cloud_services_breakdown.sql").read_text(encoding="utf-8")
    v077 = (_MIGDIR / "V077__app_cost_ledger.sql").read_text(encoding="utf-8")
    mart = _between(v055, "CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY (", ");")
    app = _between(v077, "CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.FACT_APP_COST_DAILY (", ");")
    wh, user, role = (_width(mart, c) for c in ("WAREHOUSE_NAME", "USER_NAME", "ROLE_NAME"))
    top_app = _width(app, "APPLICATION")
    # the key's identity columns are NOT NULL at the source, so the concatenated POLLER_KEY is never NULL
    for c in ("WAREHOUSE_NAME", "USER_NAME", "ROLE_NAME", "QUERY_TYPE", "QUERY_PARAMETERIZED_HASH"):
        assert re.search(rf"^\s*{c}\s+VARCHAR\(\d+\)\s+NOT NULL", mart, re.M), c
    poller_key_max = len("COST_SLEEP_POLLING|") + 100 + 1 + 120 + 1
    assert poller_key_max == 241 <= _width(ddl, "POLLER_KEY")
    assert wh <= _width(ddl, "WAREHOUSE_NAME") and user <= _width(ddl, "USER_NAME")
    assert role <= _width(ddl, "TASK_ROLE") and top_app <= _width(ddl, "USER_TOP_APP")
    owner_hint_max = max(len("Task owner (role ") + role + len(")"), top_app + len(" · ") + user, len("User ") + user)
    assert owner_hint_max <= _width(ddl, "OWNER_HINT")
    assert "WITHIN GROUP (ORDER BY c.CS DESC, c.CALL_TEXT), 400) AS CALLS" in _BODY and _width(ddl, "CALLS") == 400
    assert _width(ddl, "ROW_KIND") >= len("ACCOUNT")


def test_v160_seed_is_exact_and_when_not_matched_only():
    seed = _between(_MIG, "MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t", ";\n")
    assert seed.count(_SEED_TUPLE) == 1 and seed.count("\n        ('") == 1          # one rule, this tuple
    assert f"    AS s{_SEED_COLS}\n" in seed
    assert f"WHEN NOT MATCHED THEN INSERT {_SEED_COLS}" in seed and "WHEN MATCHED" not in seed
    assert "METRIC_NAME" not in seed and "CLEAR_THRESHOLD_NUM" not in seed     # [21] never picks it up
    name = re.search(r"\('COST_SLEEP_POLLING', 'COST', '([^']*)'", seed).group(1)
    assert len(name) <= 200 and "$" not in name                                  # ALERT_CONFIG.NAME VARCHAR(200)
    # no other ALERT_CONFIG write anywhere in the file (an operator's edits are never clobbered)
    assert len(re.findall(r"(?:MERGE INTO|UPDATE|DELETE FROM|INSERT INTO) DBA_MAINT_DB\.OVERWATCH\.ALERT_CONFIG",
                          _MIG)) == 1
    # the house Guard-C replay sees the rule enabled, and a raiser body raises it
    from tests.test_alert_rule_consistency import _config_enabled, _raised_rule_ids
    assert _RULE in _config_enabled() and _RULE in _raised_rule_ids()


def test_v160_description_fits_and_doubles_apostrophes():
    desc = re.search(r"SELECT 160 AS VERSION,\n       '(.*)' AS DESCRIPTION\n", _MIG, re.S).group(1)
    assert "\n" not in desc
    assert "'" not in desc.replace("''", "")
    assert len(desc.replace("''", "'")) <= 4000
    assert desc.startswith(f"{_RULE}: ")
    assert desc.endswith("No task change, no SETTINGS key, no procedure run at apply time.")
    assert "re-derived from V157" in desc and "tally 11 -> 12" in desc


# -- lineage ------------------------------------------------------------------------------------------------

def test_v160_marker_names_the_current_definer_and_the_new_proc_has_none():
    from tests.test_proc_lineage import _HISTORICAL_WAIVERS, _definers, _lineage, _migrations, _violations
    texts = _migrations()
    rows = {(r["v"], r["proc"]): r for r in _lineage(texts)}
    r = rows[(160, "SP_ALERT_SCAN_DAILY")]
    assert r["src"] == "marker" and r["claims"] == [157] and r["prev"] == 157 and not r["waived"], r
    assert (160, "SP_SCAN_SLEEP_POLLING") not in rows                  # a first definition: nothing to derive from
    defs = _definers(texts)
    assert defs["SP_SCAN_SLEEP_POLLING"] == [160]
    assert [v for v in defs["SP_ALERT_SCAN_DAILY"] if v > 157] == [160]  # V158/V159 never touched it
    assert not [v for v in _violations(texts, _HISTORICAL_WAIVERS) if v.startswith("V160 ")]
    assert "derived:SP_SCAN_SLEEP_POLLING" not in _MIG and "LINEAGE-WAIVER" not in _MIG
    assert _MIG.count("-- >>> derived:") == 1


# -- round 13: normalize-and-compare -------------------------------------------------------------------------

def test_v160_daily_normalizes_back_to_v157_byte_for_byte():
    d = _D
    i, j = d.index(_ANCHOR_25), d.index(_ANCHOR_17)
    d = d[:i] + d[j:]                                                            # D1: arm [25]
    assert d.count(_SELF_12) == 1
    assert d.count("(12 - :fails)") == 5
    assert d.count("'/12 rule blocks ok (daily)'") == 3
    assert d.count(_RET_160) == 1
    d = d.replace(_SELF_12, _SELF_11)                                             # D2
    d = d.replace("(12 - :fails)", "(11 - :fails)").replace("'/12 rule blocks ok (daily)'",
                                                             "'/11 rule blocks ok (daily)'")   # D3
    d = d.replace(_RET_160, _RET_157)                                             # D4
    assert d == _D157


def test_v160_arm25_is_one_counting_block_between_24_and_17():
    arm = _between(_D, _ANCHOR_25, _ANCHOR_17)
    assert arm.startswith(_ARM25_HEAD) and arm.endswith(_ARM25_CODE)
    # review r1: nothing but comment lines between the head and the code (an injected statement fails here)
    between = arm[len(_ARM25_HEAD):len(arm) - len(_ARM25_CODE)]
    assert all(ln.startswith("    --") for ln in between.splitlines()), between
    assert _D.count(_ARM25_CODE) == 1 and _D.count(_ANCHOR_25) == 1
    assert arm.count(_FAILS_INC) == 1 and arm.count("    BEGIN\n") == 1 and arm.count("    EXCEPTION\n") == 1
    # after [24]'s handler, before [17]
    assert _D[:_D.index(_ANCHOR_25)].endswith(
        "                   'rule COST_IDLE_OPPORTUNITY - other rules unaffected', CURRENT_ROLE();\n    END;\n")
    assert _D.index(_ANCHOR_24) < _D.index(_ANCHOR_25) < _D.index(_ANCHOR_17)
    # the handler is [24]'s handler with only the rule id swapped (a counting arm, not an add-on)
    arm24 = _between(_D, _ANCHOR_24, _ANCHOR_25)
    h24 = arm24[arm24.index("    EXCEPTION\n"):]
    assert arm[arm.index("    EXCEPTION\n"):] == h24.replace("COST_IDLE_OPPORTUNITY", _RULE)


def test_v160_tallies_equal_the_counting_arms():
    assert _D.count(_FAILS_INC) == 12 and _D157.count(_FAILS_INC) == 11
    assert _SELF_12 in _D and _SELF_11 not in _D
    assert "(11 - :fails)" not in _D and "/11 rule blocks ok" not in _D and "/11 " not in _D
    assert _D.count(_RET_160) == 1 and "alert scan daily v3" not in _D
    # the two [hb] ROW_COUNT sites the global denominator guard does not read
    assert _D.count("               ROW_COUNT = (12 - :fails),\n") == 1
    assert _D.count("                   (12 - :fails), 1, 'alert scan daily ' || (12 - :fails) || "
                    "'/12 rule blocks ok (daily)';\n") == 1
    from tests.test_alert_rule_consistency import _FAILS_INC_RE, _SCAN_DENOMINATORS
    self_alert_re, return_re = _SCAN_DENOMINATORS["SP_ALERT_SCAN_DAILY"]
    assert len(_FAILS_INC_RE.findall(_D)) == 12
    assert {int(x) for x in re.findall(self_alert_re, _D)} == {12}
    assert re.findall(return_re, _D) == [("12", "12")] * 3                  # [hb] STATUS x2 + RETURN


def test_v160_arms_22_and_24_are_untouched():
    arm22 = _between(_D, "    -- [22] OPS_PIPELINE_DEGRADED", _ANCHOR_24)
    assert arm22 == _between(_H157, "    -- [22] OPS_PIPELINE_DEGRADED", "    END IF;   -- /V157 cadence gate: [22]")
    assert arm22 == _between(_D157, "    -- [22] OPS_PIPELINE_DEGRADED", _ANCHOR_24)
    assert _between(_D, _ANCHOR_24, _ANCHOR_25) == _between(_D157, _ANCHOR_24, _ANCHOR_17)


def test_v160_keeps_the_intervening_carry_overs():
    for rule in ("COST_STORAGE_SURGE", "COST_SERVERLESS_CREEP", "COST_EGRESS_SPIKE", "OPS_PIPELINE_DEGRADED",
                 "COST_IDLE_OPPORTUNITY", "PIPE_REF_GAP", "DQ_RECON_ERROR"):
        assert rule in _D, rule
    assert "AS DAILY_BURN" in _D and "/ NULLIF(COUNT(DISTINCT DAY), 0)" in _D        # V064 burn
    assert "ct_hour" not in _D and "cadence gate" not in _D                          # the daily scan is ungated
    assert "ALERT_SCAN_DAILY heartbeat stamp - alerts unaffected" in _D
    assert "6-core-rule scan-health tally" in _D                                     # stale prose kept byte-identical


# -- the new proc ---------------------------------------------------------------------------------------------

def test_v160_new_proc_signature_and_declarations():
    assert _NEW.startswith("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_SCAN_SLEEP_POLLING"
                           "(FORCE_RUN BOOLEAN)\nRETURNS VARCHAR\nLANGUAGE SQL\nEXECUTE AS OWNER\nAS\n$$\n")
    assert _NEW.endswith("\nEND;\n$$;")
    decl = _between(_BODY, "DECLARE\n", "BEGIN\n")
    assert "    credit_price FLOAT DEFAULT 3.68;" in decl                     # V153: a NUMBER priced 3.68 as 4
    assert "    forced BOOLEAN DEFAULT FALSE;" in decl and "    newest DATE;" in decl
    names = re.findall(r"^    (\w+) (?:DATE|INT|BOOLEAN|FLOAT)\b", decl, re.M)
    assert names == ["today_ct", "week_start", "n_enabled", "n_done", "forced", "credit_price", "newest", "n_days",
                     "n_pollers", "n_raised", "n_superseded", "n_cleared"]
    # a NULL FORCE_RUN reads as FALSE
    assert "    IF (COALESCE(:FORCE_RUN, FALSE)) THEN\n        forced := TRUE;\n    END IF;\n" in _BODY


def test_v160_new_proc_is_mart_only_with_no_handler():
    raw, masked = _BODY, _strip_noise(_BODY)
    assert "EXCEPTION" not in masked and "WHEN OTHER" not in masked     # a failure reaches arm [25]
    assert "$$" not in raw
    for banned in ("ACCOUNT_USAGE", "TRY_TO_NUMBER", "SOURCE_FRESHNESS_STATE", "SNOWFLAKE.", "INFORMATION_SCHEMA"):
        assert banned not in raw, banned
    assert "TRY_TO_DOUBLE(" in raw and "credit_price FLOAT" in raw
    assert not re.search(r"\bCALL\b", masked) and not re.search(r"\bCURRENT_DATE\b", masked)   # Central-pinned
    assert masked.count("CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())") == 0     # (strings stripped)
    code = _code(raw)
    assert code.count("CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())") == 2       # gate + receipt
    objects = set(re.findall(r"DBA_MAINT_DB\.OVERWATCH\.(\w+)", code))
    assert objects == {"ALERT_CONFIG", "SLEEP_POLLING_WEEKLY", "SETTINGS", "FACT_METERING_DAILY",
                       "MART_CLOUD_SVC_DAILY", "FACT_APP_COST_DAILY", "APP_ERROR_LOG", "ALERT_EVENTS",
                       "COMPANY_FOR_WAREHOUSE"}, objects
    writes = set(re.findall(r"(?:INSERT INTO|UPDATE|DELETE FROM|MERGE INTO) DBA_MAINT_DB\.OVERWATCH\.(\w+)", code))
    assert writes == {"APP_ERROR_LOG", "SLEEP_POLLING_WEEKLY", "ALERT_EVENTS"}, writes
    assert "MERGE" not in code


def test_v160_new_proc_statement_order_and_labels():
    labels = [_BODY.index(f"    -- [{lb}]") for lb in
              ("gate", "ready", "snapshot", "raise", "supersede", "clear", "receipt")]
    assert labels == sorted(labels)
    code = _code(_BODY)
    marks = ("SELECT MAX(k.TODAY)", _RETURNS[0], "RETURN 'sleep polling scan skipped (week of '", "SELECT MAX(px.PRICE)",
             "INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG", _RETURNS[2],
             "DELETE FROM DBA_MAINT_DB.OVERWATCH.SLEEP_POLLING_WEEKLY",
             "INSERT INTO DBA_MAINT_DB.OVERWATCH.SLEEP_POLLING_WEEKLY", "n_pollers := SQLROWCOUNT - 1;",
             "INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS", "n_raised := SQLROWCOUNT;",
             "RESOLUTION_KIND = 'SUPERSEDED'", "n_superseded := SQLROWCOUNT;",
             "RESOLUTION_KIND = 'CONDITION_ENDED'", "n_cleared := SQLROWCOUNT;",
             "UPDATE DBA_MAINT_DB.OVERWATCH.SLEEP_POLLING_WEEKLY", "RETURN 'sleep polling scan week of '")
    for mark in marks:
        assert code.count(mark) == 1, mark
    idx = [code.index(m) for m in marks]
    assert idx == sorted(idx), [m for m, i in zip(marks, idx, strict=True) if i != sorted(idx)[idx.index(i)]]
    # the SQL statements, in order: the receipt UPDATE is the LAST DML
    stmts = [_target(s) for s in _sql_statements(_CHUNKS)]
    assert stmts == [("SELECT", ""), ("SELECT", ""), ("INSERT INTO", "APP_ERROR_LOG"),
                     ("DELETE FROM", "SLEEP_POLLING_WEEKLY"), ("INSERT INTO", "SLEEP_POLLING_WEEKLY"),
                     ("INSERT INTO", "ALERT_EVENTS"), ("UPDATE", "ALERT_EVENTS"), ("UPDATE", "ALERT_EVENTS"),
                     ("UPDATE", "SLEEP_POLLING_WEEKLY")], stmts


def test_v160_gate_is_the_only_statement_on_a_not_due_day():
    """A not-due day (receipt present, or the rule disabled) costs ONE small read: nothing but the gate
    SELECT precedes the two skip RETURNs, and the deferral writes only its log row before returning."""
    due = next(i for i, c in enumerate(_CHUNKS) if "already evaluated)'" in c)
    assert len(_sql_statements(_CHUNKS[:due + 1])) == 1
    gate = _sql_statements(_CHUNKS)[0]
    assert "FROM DBA_MAINT_DB.OVERWATCH.SLEEP_POLLING_WEEKLY r" not in gate          # a LEFT JOIN, not a FROM
    assert "LEFT JOIN DBA_MAINT_DB.OVERWATCH.SLEEP_POLLING_WEEKLY r\n" in gate
    assert "ON r.WEEK_START = k.WEEK_START AND r.ROW_KIND = 'ACCOUNT' AND r.COMPLETED_AT IS NOT NULL" in gate
    assert "ON c.RULE_ID = 'COST_SLEEP_POLLING' AND c.ENABLED" in gate
    assert ("DATEADD('day', 1 - DAYOFWEEKISO(d.TODAY), d.TODAY) AS WEEK_START" in gate
            and "(SELECT CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE AS TODAY) d" in gate)
    assert "    IF (n_enabled = 0) THEN\n        RETURN 'sleep polling scan skipped" in _BODY
    assert "    IF (n_done > 0 AND NOT forced) THEN\n" in _BODY
    # readiness: newest >= today-2 and all 7 window days carry metering AND statement rows, else defer
    assert "    IF (newest IS NULL OR newest < DATEADD('day', -2, today_ct) OR n_days < 7) THEN\n" in _BODY
    defer = _between(_code(_BODY), "IF (newest IS NULL", "END IF;")
    assert _sql_statements(_split(defer)) and "DELETE" not in defer and "ALERT_EVENTS" not in defer
    assert "'AlertScan', 'sleep_polling_scan_deferred'" in defer
    assert "'rule COST_SLEEP_POLLING - week of '" in defer
    assert "ON xd.DAY >= DATEADD('day', -7, mx.NEWEST) AND xd.DAY < mx.NEWEST" in _BODY
    assert "LEFT JOIN (SELECT m.DAY FROM DBA_MAINT_DB.OVERWATCH.MART_CLOUD_SVC_DAILY m" in _BODY


def test_v160_supersede_and_clear_each_sit_inside_their_exists_probe():
    i_sup = next(i for i, c in enumerate(_CHUNKS) if "RESOLUTION_KIND = 'SUPERSEDED'" in c)
    i_clr = next(i for i, c in enumerate(_CHUNKS) if "RESOLUTION_KIND = 'CONDITION_ENDED'" in c)
    for i, counter in ((i_sup, "n_superseded"), (i_clr, "n_cleared")):
        chunk = _CHUNKS[i]
        assert chunk.startswith("IF (EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS")
        assert re.search(r"\)\) THEN\n\s+UPDATE DBA_MAINT_DB\.OVERWATCH\.ALERT_EVENTS \w+\n", chunk)
        assert _CHUNKS[i + 1] == f"{counter} := SQLROWCOUNT" and _CHUNKS[i + 2] == "END IF"
    sup, clr = _CHUNKS[i_sup], _CHUNKS[i_clr]
    probe_sup, upd_sup = sup.split(" THEN\n", 1)
    assert "STATUS IN ('OPEN', 'ACK', 'SNOOZED')" in probe_sup and "SUBSTR(DEDUPE_KEY, -16, 6) = '|HIGH|'" in probe_sup
    # the probe is implied by the UPDATE's own filter: a skipped probe never skips a row the UPDATE would touch
    assert ("WHERE hi.RULE_ID = 'COST_SLEEP_POLLING' AND hi.STATUS IN ('OPEN', 'ACK', 'SNOOZED')" in upd_sup
            and "SUBSTR(hi.DEDUPE_KEY, -16, 6) = '|HIGH|'" in upd_sup)
    assert "lo.STATUS IN ('OPEN', 'ACK', 'SNOOZED')" in upd_sup and "SUBSTR(lo.DEDUPE_KEY, -15, 5) = '|MED|'" in upd_sup
    probe_clr, upd_clr = clr.split(" THEN\n", 1)
    assert "e.STATUS = 'OPEN' AND c.ENABLED AND c.AUTO_CLEAR_ENABLED" in probe_clr
    assert "ev.STATUS = 'OPEN'" in upd_clr and "WHERE ENABLED AND AUTO_CLEAR_ENABLED" in upd_clr
    assert "ev.RAISED_AT <= DATEADD('hour', -1, CURRENT_TIMESTAMP())" in upd_clr            # 1h dwell
    assert "'ACK'" not in upd_clr and "'SNOOZED'" not in upd_clr                             # OPEN only
    # the clear keeps a poller only when this week's census shows it at/above the clear level AND active in
    # the window's last 4 days; the clear level is clamped to the threshold
    assert _norm("s.USD_WEEK >= LEAST(COALESCE(c.CLEAR_THRESHOLD_NUM, GREATEST(COALESCE(c.THRESHOLD_NUM, 25), 1) "
                 "* 0.5), GREATEST(COALESCE(c.THRESHOLD_NUM, 25), 1))") in _norm(upd_clr)
    assert "s.LAST_ACTIVE_DAY >= DATEADD('day', -3, s.WIN_END)" in upd_clr
    assert "WHERE s.WEEK_START = :week_start AND s.ROW_KIND = 'POLLER'" in upd_clr


def test_v160_raise_hold_and_severity_text():
    ins = next(s for s in _sql_statements(_CHUNKS) if s.startswith("INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS"))
    n = _norm(ins)
    # hold: live OPEN/ACK/SNOOZED or a NOISE/EXPECTED resolve within 28 days holds its band; ACTIONED (and a
    # NULL kind) re-raises only on polling after the resolve day; machine closes never hold
    assert "COALESCE(e.RESOLUTION_KIND, 'ACTIONED') AS KIND" in n
    assert _norm("MAX(IFF(v.STATUS IN ('OPEN', 'ACK', 'SNOOZED') OR (v.STATUS = 'RESOLVED' AND v.KIND IN "
                 "('NOISE', 'EXPECTED') AND v.RESOLVED_AT >= DATEADD('day', -28, CURRENT_TIMESTAMP())), "
                 "v.BAND_RANK, 0)) AS HOLD_RANK") in n
    assert _norm("v.KIND NOT IN ('NOISE', 'EXPECTED', 'SUPERSEDED', 'AUTO_CLEARED', 'SNOOZE_SUPPRESSED', "
                 "'CONDITION_ENDED')") in n
    assert _norm("WHERE COALESCE(h.HOLD_RANK, 0) < o.BAND_RANK AND (h.LAST_ACTIONED_DAY IS NULL OR "
                 "o.LAST_ACTIVE_DAY > h.LAST_ACTIONED_DAY)") in n
    assert "LEFT JOIN hold h ON h.POLLER_KEY = o.POLLER_KEY" in n                      # uncorrelated
    assert _norm("WHERE NOT EXISTS ( SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e "
                 "WHERE e.DEDUPE_KEY = b.DEDUPE_KEY )") in n                           # exact-key, any status
    # severity never downgrades an operator-set base; COMPANY per warehouse
    assert "IFF(o.BAND_RANK = 2 AND o.BASE_SEVERITY IN ('LOW', 'MEDIUM'), 'HIGH', o.BASE_SEVERITY)" in n
    assert "COALESCE(DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(o.WAREHOUSE_NAME), 'ALL')" in n
    assert "IFF(o.WAREHOUSE_NAME = 'NONE', 'ALL'," in n                                  # no warehouse = account
    assert "WHERE RULE_ID = 'COST_SLEEP_POLLING' AND ENABLED" in n                      # a disabled rule raises nothing


def test_v160_new_proc_touches_only_its_own_rule_events():
    """Every ALERT_EVENTS read / write in the proc is pinned to this rule (or to the exact new key), so the
    hold, supersede and clear can never move another rule's event."""
    code = _code(_BODY)
    hits = list(re.finditer(r"(?:FROM|UPDATE) DBA_MAINT_DB\.OVERWATCH\.ALERT_EVENTS\b", code))
    assert len(hits) == 7                   # ev CTE, NOT EXISTS, supersede probe + UPDATE + IN, clear probe + UPDATE
    for m in hits:
        seg = code[m.end():]
        where = seg[seg.index("WHERE"):seg.index("WHERE") + 90]
        assert "RULE_ID = 'COST_SLEEP_POLLING'" in where or "e.DEDUPE_KEY = b.DEDUPE_KEY" in where, where


def test_v160_auto_clear_flag_is_inert_outside_its_own_clear():
    """The seed turns AUTO_CLEAR_ENABLED on, which is safe only because every OTHER sweep that reads the flag
    names its own rules (V157 scoped the V091 sweep to its 3 PERF rules): no latest proc body but this one can
    auto-resolve a COST_SLEEP_POLLING event."""
    from tests.test_alert_rule_consistency import _latest_proc_bodies
    bodies = _latest_proc_bodies()
    assert bodies["SP_SCAN_SLEEP_POLLING"] == _NEW and bodies["SP_ALERT_SCAN_DAILY"] == _D
    readers = 0
    for name, body in bodies.items():
        if name == "SP_SCAN_SLEEP_POLLING":
            continue
        for stmt in _split(_code(body)):
            if "AUTO_CLEAR_ENABLED" not in stmt or not re.search(r"UPDATE DBA_MAINT_DB\.OVERWATCH\.ALERT_EVENTS", stmt):
                continue
            readers += 1
            scoped = re.findall(r"\bev\.RULE_ID (?:= '(\w+)'|IN \(('[^)]*)\))", stmt)
            assert scoped, (name, stmt[:160])
            assert _RULE not in str(scoped), (name, scoped)
    assert readers >= 3                     # the V091 PERF sweep + the two V157 security condition-ended clears


def test_v160_new_proc_returns():
    rets = [re.split(r"(?m)^[ \t]*RETURN ", c, maxsplit=1)[1] for c in _CHUNKS if re.search(r"(?m)^[ \t]*RETURN ", c)]
    assert [_norm("RETURN " + r) for r in rets] == [_norm(r) for r in _RETURNS]
    assert _CHUNKS[-1] == "END"


def test_v160_every_new_proc_statement_parses():
    """Every SQL statement of the body parses as Snowflake SQL with the :binds as literals, and so does every
    scripting condition and RETURN expression (as SELECT <expr>)."""
    sqlglot = pytest.importorskip("sqlglot")
    from sqlglot import exp
    stmts = _sql_statements(_CHUNKS)
    assert len(stmts) == 9
    for stmt in stmts:
        parsed = sqlglot.parse(_bind(stmt), dialect="snowflake")
        assert len(parsed) == 1 and parsed[0] is not None, stmt[:80]
    conds = re.findall(r"IF \((.*?)\) THEN", _code(_BODY), re.S)
    assert len(conds) == 6
    for cond in conds:
        sqlglot.parse_one("SELECT " + _bind(cond), dialect="snowflake")
    for chunk in _CHUNKS:
        m = re.search(r"(?m)^[ \t]*RETURN (.*)", chunk, re.S)
        if m:
            sqlglot.parse_one("SELECT " + _bind(m.group(1)), dialect="snowflake")
    # column arity: both census branches project the 21 INSERT columns; the raise projects the 7 event columns
    snap = sqlglot.parse_one(_bind(stmts[4]), dialect="snowflake")
    assert [c.name for c in snap.this.expressions] == list(_CENSUS_COLS)
    (union,) = list(snap.find_all(exp.Union))
    assert len(union.left.expressions) == len(union.right.expressions) == 21
    raise_ = sqlglot.parse_one(_bind(stmts[5]), dialect="snowflake")
    cols = [c.name for c in raise_.this.expressions]
    assert cols == ["RULE_ID", "COMPANY", "SEVERITY", "TITLE", "DETAIL", "METRIC_VALUE", "DEDUPE_KEY"]
    (b,) = [s for s in raise_.find_all(exp.Subquery) if s.alias == "b"]
    assert [c.name for c in b.args["alias"].columns] == cols and len(b.this.expressions) == 7


def test_v160_parse_check_has_teeth():
    sqlglot = pytest.importorskip("sqlglot")
    raise_ = _bind(_sql_statements(_CHUNKS)[5])
    broken = raise_.replace("LEFT JOIN hold h ON", "LEFT JOIN hold h ON (", 1)
    with pytest.raises(sqlglot.errors.ParseError):
        sqlglot.parse(broken, dialect="snowflake")
    with pytest.raises(KeyError):
        _bind("SELECT :not_a_declared_variable")
    assert _bind("SELECT '[[:space:]]', :newest") == "SELECT '[[:space:]]', TO_DATE('2026-09-28')"
    # the scanner keeps the pattern string whole (it holds '--' and '//')
    assert _code(_BODY).count(_SLEEP_RE_LITERAL) == 1


def test_v160_sleep_family_filter_is_the_panel_shape():
    code = _code(_BODY)
    assert code.count(f"REGEXP_INSTR(UPPER(f.SAMPLE_TEXT), '{_SLEEP_RE_LITERAL}') > 0") == 1
    assert "\n" not in _SLEEP_RE_LITERAL and "'" not in _SLEEP_RE_LITERAL
    assert ("f.QUERY_TYPE NOT LIKE 'CREATE%' AND f.QUERY_TYPE NOT LIKE 'ALTER%' AND f.QUERY_TYPE NOT LIKE "
            "'MULTI_STATEMENT%'") in code
    assert "WHERE f.QUERY_PARAMETERIZED_HASH <> 'n/a'" in code
    assert "MIN(fd.SAMPLE_TEXT) AS SAMPLE_TEXT" in code                           # window-MIN sample
    assert "GROUP BY fd.QUERY_PARAMETERIZED_HASH, fd.QUERY_TYPE, fd.WAREHOUSE_NAME, fd.USER_NAME" in code
    # complete days only, grouped-cap per poller per day, and the ACCOUNT row is the panel group total
    assert "WHERE x.DAY >= DATEADD('day', -7, :newest) AND x.DAY < :newest" in code
    assert code.count("SUM(LEAST(p.CS_DAY, GREATEST(0, b.CS_BILLED_DAY))) AS BILLED_CS_CREDITS") == 1
    assert code.count("SUM(LEAST(d.CS_DAY, GREATEST(0, b.CS_BILLED_DAY))) AS BILLED_CS_CREDITS") == 1
    assert "SUM(COALESCE(x.CREDITS_CLOUD_SVCS, 0) + COALESCE(x.CREDITS_ADJUSTMENT, 0)) AS CS_BILLED_DAY" in code
    assert "n_pollers := SQLROWCOUNT - 1;" in code and "'ACCOUNT', 'COST_SLEEP_POLLING|*|*|'" in code


# -- DEDUPE_KEY, TITLE, DETAIL -----------------------------------------------------------------------------------

def _poller_key(wh: str, user: str, role: str = "") -> str:
    """Python twin of the sd CTE's POLLER_KEY (REPLACE '|' -> '/', UPPER, LEFT 100 / 120)."""
    who = f"SYSTEM:{role}" if user.upper() == "SYSTEM" else user
    return f"{_RULE}|{wh.replace('|', '/').upper()[:100]}|{who.replace('|', '/').upper()[:120]}|"


def _sf_substr(s: str, start: int, length: int) -> str:
    """Snowflake SUBSTR: 1-based; a negative start counts from the end (-1 = the last character)."""
    i = start - 1 if start > 0 else len(s) + start
    return s[max(i, 0):max(i, 0) + length]


def _sf_left(s: str, n: int) -> str:
    return s[:max(n, 0)]


_KEY_RE = re.compile(r"^COST_SLEEP_POLLING\|[^|]+\|[^|]+\|(MED|HIGH)\|\d{4}-\d{2}-\d{2}$")
_POLLERS = [("WH_ALFA_TRANSFORM_PRD", "CTM_USER", ""), ("WH_TRXS_TRANSFORM", "SYSTEM", "TRXS_ETL_ROLE"),
            ("WH_X", "HIGH", ""), ("WH_X", "MED", ""), ("WH|PIPE", "a|b", ""), ("W" * 300, "U" * 300, ""),
            ("WH_T", "system", "R" * 300), ("NONE", "SYSTEM", "")]


def test_v160_key_expressions():
    code = _norm(_code(_BODY))
    assert _norm("'COST_SLEEP_POLLING|' || LEFT(UPPER(REPLACE(d.WAREHOUSE_NAME, '|', '/')), 100) || '|' || "
                 "LEFT(UPPER(REPLACE(IFF(UPPER(d.USER_NAME) = 'SYSTEM', 'SYSTEM:' || d.ROLE_NAME, d.USER_NAME), "
                 "'|', '/')), 120) || '|' AS POLLER_KEY") in code
    assert "IFF(UPPER(d.USER_NAME) = 'SYSTEM', d.ROLE_NAME, '') AS TASK_ROLE" in code
    assert code.count("o.POLLER_KEY || IFF(o.BAND_RANK = 2, 'HIGH', 'MED') || '|' || TO_VARCHAR(:week_start)") == 1
    assert "IFF(SUBSTR(e.DEDUPE_KEY, -16, 6) = '|HIGH|', 2, 1) AS BAND_RANK" in code


def test_v160_key_suffix_parsing_holds_on_med_and_high_keys():
    """Every SUBSTR / LEFT(LENGTH - n) constant the proc uses on DEDUPE_KEY, applied (Snowflake semantics) to
    MED and HIGH keys built in Python: the band is read right and the POLLER_KEY prefix comes back exactly,
    even for a user named HIGH or MED, an escaped '|', and names at the truncation limits."""
    code = _code(_BODY)
    substrs = set(re.findall(r"SUBSTR\((?:\w+\.)?DEDUPE_KEY, (-\d+), (\d+)\) = '([^']+)'", code))
    assert substrs == {("-16", "6", "|HIGH|"), ("-15", "5", "|MED|")}, substrs
    lefts = re.findall(r"LEFT\((?:\w+\.)?DEDUPE_KEY, LENGTH\((?:\w+\.)?DEDUPE_KEY\) - (\d+)\)", code)
    assert sorted(lefts) == ["14", "15"]                                              # the supersede pair
    tails = re.findall(r"LENGTH\((\w+)\.DEDUPE_KEY\) - IFF\(SUBSTR\(\1\.DEDUPE_KEY, -16, 6\) = '\|HIGH\|', "
                       r"(\d+), (\d+)\)\)", code)
    assert tails == [("e", "15", "14"), ("ev", "15", "14")]                          # ev (hold) + clear
    for wh, user, role in _POLLERS:
        pk = _poller_key(wh, user, role)
        assert len(pk) <= 241 and pk.count("|") == 3
        for week in ("2026-09-28", "2026-10-05"):
            med, high = f"{pk}MED|{week}", f"{pk}HIGH|{week}"
            for key, band in ((med, "MED"), (high, "HIGH")):
                assert _KEY_RE.match(key) and _KEY_RE.match(key).group(1) == band, key
                assert len(key) <= 256 <= 300                                        # ALERT_EVENTS.DEDUPE_KEY
                is_high = _sf_substr(key, -16, 6) == "|HIGH|"
                assert is_high == (band == "HIGH")
                assert (_sf_substr(key, -15, 5) == "|MED|") == (band == "MED")
                assert _sf_left(key, len(key) - (15 if is_high else 14)) == pk           # hold + clear tail
                # V117 carry-forward: a trailing |date, and MED / HIGH never share an identity
                assert _sf_substr(key, -11, 1) == "|" and key[-10:] == week
            assert _sf_left(med, len(med) - 14) == _sf_left(high, len(high) - 15) == pk   # the supersede pair
            assert _sf_left(med, len(med) - 11) != _sf_left(high, len(high) - 11)
    # cross-week escalation is the proc's own supersede: V067's same-date token swap never pairs them
    pk = _poller_key("WH_A", "U")
    assert f"{pk}MED|2026-09-28".replace("|MED|", "|HIGH|") != f"{pk}HIGH|2026-10-05"


def _expr(start: str, end: str) -> str:
    i = _BODY.index(start)
    return _BODY[i:_BODY.index(end, i) + len(end)]


_LIT_RE = re.compile(r"'((?:[^']|'')*)'")


def test_v160_title_shape():
    from app.logic.navigate import _DB_RE, _WH_RE
    title = _expr("LEFT(IFF(o.WAREHOUSE_NAME = 'NONE'", ", 300)")
    assert _norm(title) == ("LEFT(IFF(o.WAREHOUSE_NAME = 'NONE', 'No warehouse', o.WAREHOUSE_NAME) || ' sleep "
                            "polling ~$' || ROUND(o.USD_WEEK)::INT || '/week: ' || o.OWNER_HINT, 300)")
    lits = _LIT_RE.findall(title)
    assert lits[0] == "NONE"                                                           # a comparator, not output
    shown = "".join(lits[1:])
    assert shown.count("$") == 1                                                       # one USD figure
    assert not _WH_RE.search(shown.upper()) and not _DB_RE.search(shown.upper())
    # the warehouse leads the title, so the drawer's first WH_ match is the poller's warehouse
    for hint in ("Task owner (role TRXS_ETL_ROLE)", "Control-M · CTM_USER", "User CTM_USER"):
        rendered = "WH_TRXS_TRANSFORM" + lits[2] + "105" + lits[3] + hint
        assert len(rendered) <= 300 and rendered.count("$") == 1
        assert _WH_RE.search(rendered.upper()).group(0) == "WH_TRXS_TRANSFORM"
    # the owner-hint text carries no entity either
    hint_expr = _between(_BODY, "IFF(UPPER(a.USER_NAME) = 'SYSTEM',", "a.CALLS, a.FAMILIES")
    hint_lits = "".join(_LIT_RE.findall(hint_expr)).upper()
    assert not _WH_RE.search(hint_lits) and not _DB_RE.search(hint_lits)


def test_v160_detail_shape():
    from app.logic.navigate import _DB_RE, _WH_RE
    detail = _expr("LEFT('Sleep polling on '", ", 2000)")
    lits = [x.replace("''", "'") for x in _LIT_RE.findall(detail)]
    text = "".join(lits).upper()
    assert not _WH_RE.search(text) and not _DB_RE.search(text) and "$$" not in text
    # both next steps, chosen by the task user; apostrophes doubled in the SQL
    task, client = (x for x in lits if x.startswith(("Stop sleeping", "Move the wait")))
    assert task.startswith("Stop sleeping inside the task") and client.startswith("Move the wait out of Snowflake")
    assert task.endswith("A resize won't help.") and client.endswith("A resize won't help.")
    assert detail.count("won''t help.") == 2
    assert _norm("IFF(UPPER(o.USER_NAME) = 'SYSTEM', 'Stop sleeping inside the task") in _norm(detail)
    assert "Cost Intelligence > Spend & Attribution > Cloud-services health" in "".join(lits)
    tail = "".join(lits[-3:])
    assert "idle on the last " in tail and "4 complete days;" in tail
    # review r1: the self-clear is OPEN-only; an acknowledged event is the human's to close
    assert "while OPEN, this event resolves itself" in "".join(lits)
    assert "".join(lits).endswith("an acknowledged event stays yours to close (resolve it as actioned once fixed).")
    # owner + next step come before the explanation (the AI prompt keeps DETAIL[:500])
    joined = "".join(lits)
    assert joined.index("Owner: ") < joined.index("A sleep compiles in well under 0.1 s")
    # fixed text (the longer next step), the 400-char CALLS cap, two 150-char owner hints and ~100 characters
    # of numbers and dates still fit 2000, so the verify / self-clear tail survives the LEFT on real data
    fixed = sum(len(x) for x in lits) - min(len(task), len(client))
    assert fixed + 400 + 2 * 150 + 100 <= 2000, fixed


# -- RUN_NEXT PART B -------------------------------------------------------------------------------------------
# The PART B grid checks these with CONTAINS(GET_DDL('PROCEDURE', ...), '<frag>'): each must literally be in the
# body it is checked against (GET_DDL returns the stored body, comments included) and stay free of quotes,
# backslashes and newlines so it pastes into a SQL literal unchanged. _PART_B_ABSENT are the expected-FALSE probes.
_PART_B_FRAGMENTS = {
    "SP_ALERT_SCAN_DAILY()": ("SP_SCAN_SLEEP_POLLING(FALSE)", "rule COST_SLEEP_POLLING - other rules unaffected",
                              "/12 rule blocks ok (daily)", "alert scan daily v4 (V160:", "COST_IDLE_OPPORTUNITY",
                              "OPS_PIPELINE_DEGRADED", "AS DAILY_BURN"),
    "SP_SCAN_SLEEP_POLLING(BOOLEAN)": ("SYSTEM[$]WAIT[[:space:]]*[(]", "MULTI_STATEMENT",
                                       "LEAST(p.CS_DAY, GREATEST(0, b.CS_BILLED_DAY))", "x.DAY < :newest",
                                       "s.ACTIVE_DAYS >= 5", "COALESCE(h.HOLD_RANK, 0) < o.BAND_RANK",
                                       "COMPLETED_AT IS NOT NULL", "sleep_polling_scan_deferred", "CONDITION_ENDED",
                                       "SUPERSEDED", "credit_price FLOAT", "TRY_TO_DOUBLE"),
}
_PART_B_ABSENT = {
    "SP_ALERT_SCAN_DAILY()": ("/11 rule blocks ok", "ct_hour"),
    "SP_SCAN_SLEEP_POLLING(BOOLEAN)": ("ACCOUNT_USAGE", "TRY_TO_NUMBER", "SOURCE_FRESHNESS_STATE"),
}


def _stored_body(proc: str) -> str:
    text = {"SP_ALERT_SCAN_DAILY()": _D, "SP_SCAN_SLEEP_POLLING(BOOLEAN)": _NEW}[proc]
    return text[text.index("$$") + 2:text.rindex("$$")]


def test_v160_part_b_get_ddl_fragments():
    assert set(_PART_B_FRAGMENTS) == set(_PART_B_ABSENT)
    for proc in _PART_B_FRAGMENTS:
        body = _stored_body(proc)
        for frag in (*_PART_B_FRAGMENTS[proc], *_PART_B_ABSENT[proc]):
            assert not set(frag) & {"'", "\\", "\n", "\r"}, (proc, frag)
        for frag in _PART_B_FRAGMENTS[proc]:
            assert frag in body, (proc, frag)
        for frag in _PART_B_ABSENT[proc]:
            assert frag not in body, (proc, frag)
    # the expected-FALSE daily probes were TRUE on V157 (so the grid tells the two versions apart)
    body157 = _D157[_D157.index("$$") + 2:_D157.rindex("$$")]
    assert "/11 rule blocks ok" in body157 and "SP_SCAN_SLEEP_POLLING" not in body157


# -- app lockstep (v4.596.0). These complete when the lead's lockstep lands: playbooks, navigate, Admin,
#    snowflake/validate.sql, snowflake/teardown.sql, DEPLOYMENT.md, README.md, RUNBOOK.md.

def test_v160_playbook():
    from app.logic.playbooks import PLAYBOOKS, playbook_for
    from tests.test_alert_rule_consistency import _RETIRED_RE
    pb = playbook_for(_RULE)
    assert _RULE in PLAYBOOKS and pb == PLAYBOOKS[_RULE]                  # its own entry, not the COST fallback
    assert pb.startswith("**Means:**")
    assert "$" not in re.sub(r"`[^`]*`", "", pb), "a bare '$' outside a code span garbles the drawer (write USD)"
    assert "USD" in pb
    assert not _RETIRED_RE.search(pb)                                     # Guard B: nothing reads as not-firing
    # the neighbours name the new push (COST_CLOUD_SVC_ANOMALY step 4, OPS_SCAN_DEGRADED's weekly-arm note)
    assert _RULE in PLAYBOOKS["COST_CLOUD_SVC_ANOMALY"] and _RULE in PLAYBOOKS["OPS_SCAN_DEGRADED"]


def test_v160_navigation():
    from app.logic import navigate
    assert navigate._RULE_TARGETS[_RULE] == ("Cost Intelligence", "Spend & Attribution")
    assert _RULE not in navigate.FIX_TARGETS and _RULE not in navigate.INLINE_FIX_RULES
    assert _RULE not in navigate._NO_ENTITY_FILTER_RULES
    title = "WH_TRXS_TRANSFORM sleep polling ~$105/week: Task owner (role TRXS_ETL_ROLE)"
    assert navigate.investigation_target(_RULE, title) == {
        "page": "Cost Intelligence", "section": "Spend & Attribution",
        "filters": {"warehouse_contains": "WH_TRXS_TRANSFORM"}}
    assert navigate.fix_target(_RULE, title) is None and navigate.inline_fix_warehouse(_RULE, title) == ""
    # the DETAIL's Verify path is the page / section Investigate opens
    assert "Verify: Cost Intelligence > Spend & Attribution > Cloud-services health" in _BODY


def test_v160_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 160 in _EXPECTED_MIGRATIONS
    text = str(_EXPECTED_MIGRATIONS[160])
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_" not in text and "$" not in text


def test_v160_validate_and_docs():
    val = _read("snowflake/validate.sql")
    assert "'V001..V160 applied'" in val and "VERSION BETWEEN 1 AND 160) = 160" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert f"snowflake/migrations/{_NAME}" in _read(rel), rel


def test_v160_teardown_drops_the_new_proc():
    td = _read("snowflake/teardown.sql")
    assert "DROP PROCEDURE IF EXISTS DBA_MAINT_DB.OVERWATCH.SP_SCAN_SLEEP_POLLING(BOOLEAN);" in td
    assert "SLEEP_POLLING_WEEKLY" in td


def test_v160_runbook_row():
    rb = _read("RUNBOOK.md")
    rows = [ln for ln in rb.splitlines() if ln.startswith(f"| {_RULE} | COST |")]
    assert len(rows) == 1, rows
    assert "V160" in rows[0] and "SP_SCAN_SLEEP_POLLING" in rows[0]
