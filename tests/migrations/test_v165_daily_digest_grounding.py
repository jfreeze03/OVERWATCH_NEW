"""Locks for V165 -- the morning digest is checked, not assumed (Next-Fifty #24, v4.602.0).

STRUCTURE + GENERATION + LOCKS; the Python mirror of the figure check and its sqlite parity run live in
tests/test_digest_grounding.py and tests/test_digest_grounding_parity.py. What this file proves, from the
shipped text:
  * generation -- outputs/gen_v165.py regenerates the migration byte-for-byte (LF only) and writes nothing
    else; the PREFLIGHT (P165.1-P165.3) and PART B are written only on request, are read-only, parse, and the
    PREFLIGHT carries the proc's own D2 / D3 / D5 text with the binds swapped for CTE columns;
  * shape -- first line, guard (-20165, v < 164, V160's guard with the numbers moved), the six nullable
    DAILY_DIGEST columns, the marker + SP_DAILY_DIGEST, the version row; nothing runs at apply time;
  * lineage + round 13 -- SP_DAILY_DIGEST is re-derived from V112 (its immediately previous definer) and
    reversing D1-D9 with this file's OWN copies of the old text gives V112's proc back byte-for-byte;
  * the send -- TEXT_PLAIN(:msg) only, the draft never sent, V064's five JSON-escape lines verbatim then the
    trailing-backslash RTRIM, and (executed in Python) every escaped message is valid JSON inside the Teams
    body template;
  * content -- FACTS keys (SPEND_USD and CREDITS separate), COUNT_IF alert counts, the Cortex-failure path, the
    template's digits, the column widths, the V112 carry-overs, statement order, the RETURN; every SQL
    statement parses (sqlglot, snowflake) once the :binds are literals.
"""

from __future__ import annotations

import json
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
_NAME = "V165__daily_digest_grounding.sql"
_MIG = read(f"snowflake/migrations/{_NAME}")
_V112 = read("snowflake/migrations/V112__daily_digest_skips_paging_routes.sql")
_V064 = read("snowflake/migrations/V064__webhook_drain_watermarks_alert_burn_telemetry.sql")
_V160 = read("snowflake/migrations/V160__sleep_polling_alert.sql")


def _proc(text: str, name: str) -> str:
    s = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    o = text.index("$$", s)
    return text[s:text.index("$$;", o + 2) + 3]


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


_P = _proc(_MIG, "SP_DAILY_DIGEST()")
_BODY = _P[_P.index("$$") + 2:_P.rindex("$$")]
_P112 = _proc(_V112, "SP_DAILY_DIGEST()")
_BODY112 = _P112[_P112.index("$$") + 2:_P112.rindex("$$")]

# ---------------------------------------------------------------------------------------------------
# Test-side copies (independent of outputs/gen_v165.py).
# ---------------------------------------------------------------------------------------------------
_MARKER = ("-- >>> derived:SP_DAILY_DIGEST  (from V112; FACTS + measured GROUNDING_OK + templated digest on "
           "mismatch + JSON-safe send, V165)\n")
_COLUMNS = (("FACTS", "VARCHAR(4000)"), ("GROUNDING_OK", "BOOLEAN"), ("FIGURES_CHECKED", "NUMBER(6,0)"),
            ("UNGROUNDED", "VARCHAR(1000)"), ("BODY_SOURCE", "VARCHAR(20)"), ("AI_BODY", "VARCHAR(8000)"))
_ALTER = "ALTER TABLE DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST ADD COLUMN IF NOT EXISTS "

# V112's old text (the D2 / D3 / D4 / D6 / D8 / D9 left-hand sides), copied here, not from the generator
_OLD_D2 = """\
    SELECT COALESCE(LISTAGG(METRIC || '=' || COALESCE(VALUE_USD, VALUE)::VARCHAR, '; ')
           WITHIN GROUP (ORDER BY SORT_ORDER), 'no board rows')
      INTO :facts
    FROM DBA_MAINT_DB.OVERWATCH.MART_EXEC_BOARD
    WHERE COMPANY = 'ALL' AND WINDOW_DAYS = 7 AND PANEL = 'KPI';

    SELECT 'open_critical=' || SUM(IFF(SEVERITY = 'CRITICAL' AND STATUS IN ('OPEN','ACK'), 1, 0))
           || '; open_high=' || SUM(IFF(SEVERITY = 'HIGH' AND STATUS IN ('OPEN','ACK'), 1, 0))
           || '; raised_24h=' || SUM(IFF(RAISED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP()), 1, 0))
      INTO :alerts
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS;
"""
_OLD_D3 = """\
    prompt := LEFT(
        'You are a senior Snowflake DBA writing the morning digest for ALFA/Trexis leadership. '
        || 'Use ONLY these 7-day platform facts and alert counts - never invent numbers. '
        || 'Write 3 short paragraphs: (1) platform health and spend in plain language, '
        || '(2) what needs attention today and why, (3) one recommended focus. No preamble. '
        || 'FACTS: ' || COALESCE(:facts, 'none') || '. ALERTS: ' || COALESCE(:alerts, 'none') || '.',
        6000);
"""
_OLD_D4 = """\
            body := 'Digest unavailable: Cortex COMPLETE failed for model ' || :model
                    || '. Check SNOWFLAKE.CORTEX_USER grant and regional model availability.';
"""
_NEW_D4 = """\
            -- D4: V165 #24 - no error text as the digest; the template goes out and the failure is ledgered below
            body := NULL;
            ai_err := SQLERRM;
"""
_AI_BODY_LINE = "    ai_body := LEFT(:body, 8000);   -- D4: the draft, kept for audit whichever version is sent\n"
_OLD_D6 = """\
        INSERT INTO DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST (DIGEST_DATE, COMPANY, MODEL, BODY)
        VALUES (CURRENT_DATE(), 'ALL', :model, LEFT(:body, 8000));
"""
_OLD_D8 = """\
                SNOWFLAKE.NOTIFICATION.TEXT_PLAIN(
                    'OVERWATCH morning digest \u2014 ' || TO_VARCHAR(CURRENT_DATE()) || CHR(10) ||
                    LEFT(:body, 3000)),
"""
_NEW_D8 = "                SNOWFLAKE.NOTIFICATION.TEXT_PLAIN(:msg),   -- D8: V165 #24\n"
_OLD_D9 = """\
    RETURN 'digest written; sent ' || :routes_sent || '/' || :routes_total || ' routes'
           || IFF(:routes_total > 0 AND :routes_sent = 0, ' [UNDELIVERED]', '');
"""
# V064:203-207, the notifier's v3 JSON escape, re-targeted from message to msg
_ESCAPE = (
    "    msg := REPLACE(:msg, CHR(92), CHR(92) || CHR(92));\n"
    "    msg := REPLACE(:msg, CHR(34), CHR(92) || CHR(34));\n"
    "    msg := REPLACE(:msg, CHR(10), CHR(92) || 'n');\n"
    "    msg := REPLACE(:msg, CHR(13), '');\n"
    "    msg := REPLACE(:msg, CHR(9),  CHR(92) || 't');\n"
    "    msg := RTRIM(LEFT(:msg, 3000), CHR(92));"
)
_FACT_KEYS = ("WINDOW_DAYS", "SPEND_USD", "CREDITS", "QUERIES", "FAILED_QUERIES", "FAILED_QUERY_PCT",
              "QUERY_SUCCESS_PCT", "QUEUED_MINUTES", "SPILL_GB", "TASK_RUNS", "TASK_FAILURES", "TASK_FAILURE_PCT",
              "TASK_SUCCESS_PCT", "ALERT_WINDOW_HOURS", "OPEN_CRITICAL_ALERTS", "OPEN_HIGH_ALERTS",
              "ALERTS_RAISED_24H")


def _reverse(p: str) -> str:
    """The round-13 reversal: every new block out, V112's old text back in."""
    for start, end, old in (
            ("    -- D1 >>>", "    -- <<< D1\n", ""),
            ("    -- D2 >>>", "    -- <<< D2\n", _OLD_D2),
            ("    -- D3 >>>", "    -- <<< D3\n", _OLD_D3),
            ("    -- D5 >>>", "    -- <<< D5\n\n", ""),
            ("        -- D6: V165", ":body_source, :ai_body);\n", _OLD_D6),
            ("    -- D7 >>>", "    -- <<< D7\n", ""),
            ("    -- D9: V165", "' [UNDELIVERED]', '');\n", _OLD_D9)):
        assert p.count(start) == 1, start
        i = p.index(start)
        j = p.index(end, i) + len(end)
        p = p[:i] + old + p[j:]
    for new, old in ((_NEW_D4, _OLD_D4), (_AI_BODY_LINE, ""), (_NEW_D8, _OLD_D8)):
        assert p.count(new) == 1, new
        p = p.replace(new, old)
    return p


# ---------------------------------------------------------------------------------------------------
# Scanners (string-aware): _code drops comments but keeps literals; _split cuts on ';' outside literals.
# ---------------------------------------------------------------------------------------------------
def _code(sql: str) -> str:
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        two = sql[i:i + 2]
        if two == "--":
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
_STR_RE = re.compile(r"'(?:[^']|'')*'")
_BIND_RE = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")
_BINDS = {"model": "'llama3.1-8b'", "facts": "'WINDOW_DAYS=7'", "alerts": "'x'", "prompt": "'p'",
          "body": "'b'", "clean": "'c'", "ungrounded": "'2 critical'", "ai_err": "'e'", "ai_body": "'d'",
          "body_source": "'AI'", "msg": "'m'", "emsg": "'e'", "r_route_id": "'R1'", "r_integration": "'I'",
          "grounding_ok": "TRUE", "n_checked": "3", "n_bad": "0", "routes_total": "1", "routes_sent": "1",
          "f_spend_usd": "12345.67", "f_credits": "3354.80", "f_queries": "1234567", "f_failed_q": "321",
          "f_queued_min": "12.3", "f_spill_gb": "4.56", "f_task_runs": "900", "f_task_fail": "3",
          "f_failed_q_pct": "0.03", "f_task_fail_pct": "0.33", "a_open_crit": "0", "a_open_high": "2",
          "a_raised_24h": "5"}


def _bind(sql: str) -> str:
    out, last = [], 0
    for m in _STR_RE.finditer(sql):
        out.append(_BIND_RE.sub(lambda b: _BINDS[b.group(1)], sql[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(_BIND_RE.sub(lambda b: _BINDS[b.group(1)], sql[last:]))
    return "".join(out)


_SQL_START_RE = re.compile(r"^[ \t]*(SELECT|INSERT|UPDATE|DELETE|MERGE)\b", re.M)
_INTO_RE = re.compile(r"\bINTO :\w+(?:,\s*:\w+)*")      # SELECT ... INTO :binds is scripting, not SQL


def _sql_statements() -> list[str]:
    out = []
    for chunk in _CHUNKS:
        m = _SQL_START_RE.search(chunk)
        if m:
            out.append(_INTO_RE.sub("", chunk[m.start():]).strip())
    return out


def _assignments() -> list[tuple[str, str]]:
    """(variable, right-hand side) of every scripting assignment in the body."""
    out = []
    for chunk in _CHUNKS:
        m = re.search(r"(?m)^[ \t]*(\w+) := (.*)", chunk, re.S)
        if m:
            out.append((m.group(1), m.group(2).strip()))
    return out


def _rhs(var: str, prefix: str = "") -> str:
    """The right-hand side of the one assignment to ``var`` that starts with ``prefix``."""
    return next(rhs for v, rhs in _assignments() if v == var and rhs.startswith(prefix))


def _gen_env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("V165_OUT", "PREFLIGHT_OUT", "PART_B_OUT")}
    env.update(extra)
    return env


def _run_gen(tmp_path: Path, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v165.py")], env=_gen_env(**extra),
                          cwd=tmp_path, capture_output=True, text=True)


def _extras(tmp_path: Path) -> tuple[str, str]:
    pre, part_b = tmp_path / "PREFLIGHT_V165.sql", tmp_path / "PART_B_V165.sql"
    result = _run_gen(tmp_path, V165_OUT=str(tmp_path / "m.sql"), PREFLIGHT_OUT=str(pre), PART_B_OUT=str(part_b))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "m.sql").read_text(encoding="utf-8") == _MIG     # the extras never change the migration
    return pre.read_text(encoding="utf-8"), part_b.read_text(encoding="utf-8")


# -- generation ------------------------------------------------------------------------------------------------

def test_v165_regenerates_byte_identical_and_writes_nothing_else(tmp_path):
    out = tmp_path / "regen.sql"
    result = _run_gen(tmp_path, V165_OUT=str(out))
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == (_MIGDIR / _NAME).read_bytes(), (
        "V165 drifted from its forward-generation -- edit outputs/gen_v165.py, not the .sql.")
    assert b"\r\n" not in out.read_bytes()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["regen.sql"]     # PREFLIGHT / PART B only on request
    assert "PREFLIGHT" not in result.stdout and "PART B" not in result.stdout


def test_v165_generator_reads_only_v112_and_never_imports_app():
    gen = read("outputs/gen_v165.py")
    assert 'BASE = MIG / "V112__daily_digest_skips_paging_routes.sql"' in gen
    assert gen.count(".read_text(") == 1                       # V112, nothing else
    assert not re.search(r"^\s*(?:from|import)\s+app\b", gen, re.M)
    assert "_swap(" in gen and "def _swap(text: str, old: str, new: str, label: str, n: int = 1)" in gen


@pytest.mark.parametrize("which", ["preflight", "part_b"])
def test_v165_preflight_and_part_b_are_read_only_and_parse(tmp_path, which):
    pre, part_b = _extras(tmp_path)
    sql = pre if which == "preflight" else part_b
    code = _strip_noise(sql)                       # comments + string literals out
    for banned in ("INSERT", "UPDATE", "DELETE", "MERGE", "CALL", "CREATE", "ALTER", "DROP", "TRUNCATE",
                   "GRANT", "REVOKE", "EXECUTE", "UNDROP", "RENAME", "USE", "SET"):
        assert not re.search(rf"\b{banned}\b", code, re.I), (which, banned)
    assert not _BIND_RE.search(code), "a scripting :bind survived"
    assert "$$" not in sql and "@" not in sql
    sqlglot = pytest.importorskip("sqlglot")
    from sqlglot import exp
    parsed = sqlglot.parse(sql, dialect="snowflake")
    assert parsed and all(p is not None and p.key in ("select", "union") for p in parsed), which
    writes = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Command)
    for p in parsed:
        assert not [type(n).__name__ for n in p.walk() if isinstance(n, writes)], which


def test_v165_preflight_grids_and_the_optional_cortex_grid_last(tmp_path):
    pre, _ = _extras(tmp_path)
    assert pre.startswith("-- PREFLIGHT_WAVE4 -- V165 section (READ-ONLY).")
    heads = re.findall(r"^-- (P165\.\w+) ", pre, re.M)
    assert heads == ["P165.1a", "P165.1b", "P165.1c", "P165.1d", "P165.2", "P165.1e", "P165.3"], heads
    p3 = pre[pre.index("-- P165.3 "):]
    assert "OPTIONAL" in p3.splitlines()[0]
    assert pre.count("SNOWFLAKE.CORTEX.COMPLETE(") == 1 and "SNOWFLAKE.CORTEX.COMPLETE(" in p3   # only the opt-in grid
    assert "TABLE(GENERATOR(ROWCOUNT => 5))" in p3
    # NOTIFICATION_HISTORY takes START_TIME (never START_TIME_RANGE_START) inside the 336h cap
    assert "START_TIME_RANGE_START" not in pre and "DATEADD('day', -13, CURRENT_TIMESTAMP())" in pre
    # the negative control is a figure no fact can license
    assert "There are 999999 critical alerts open." in pre


def test_v165_preflight_carries_the_proc_text(tmp_path):
    """P165.2 / P165.3 are generated from the SAME D2 / D3 / D5 text as the proc, binds swapped for CTE
    columns -- what the owner previews is what the 07:20 run will do."""
    pre, _ = _extras(tmp_path)
    # the grounding core: every line from the u-level SELECT to the ON clause, verbatim, bar the RUN_ID carry
    core = _between(_BODY, "                       u.NUM_VAL * u.SCALE AS VAL,", "            GROUP BY t.TOK")
    core = (core.replace("SELECT w.POS, w.TOK,", "SELECT w.RUN_ID, w.POS, w.TOK,")
            .replace("SELECT x.INDEX AS POS,", "SELECT d.RUN_ID, x.INDEX AS POS,")
            .replace("FROM TABLE(FLATTEN(INPUT => REGEXP_SUBSTR_ALL(:clean,",
                     "FROM drafts d, TABLE(FLATTEN(INPUT => REGEXP_SUBSTR_ALL(d.CLEAN,")
            .replace("FROM TABLE(FLATTEN(INPUT => REGEXP_SUBSTR_ALL(:facts,",
                     "FROM k, TABLE(FLATTEN(INPUT => REGEXP_SUBSTR_ALL(k.FACTS,"))
    assert pre.count(core) == 2                                # P165.2 and P165.3
    # the template: the proc's right-hand side with the fact binds as k.<COLUMN>
    tpl = dict(_assignments())
    body_rhs = _rhs("body", "'Templated digest")
    want = re.sub(r":(f_\w+|a_\w+)", lambda m: "k." + m.group(1).upper(), body_rhs).replace(":grounding_ok", "FALSE")
    assert want in pre
    # the FACTS string and the prompt, rebound
    facts_rhs = _rhs("facts", "'WINDOW_DAYS=7")
    assert re.sub(r":(f_\w+)", lambda m: "kr." + m.group(1).upper(), facts_rhs) in pre
    assert re.sub(r":(\w+)", lambda m: "k." + m.group(1).upper(), tpl["prompt"]) in pre
    # the five strips, same patterns, same order, the last one multi-line
    strips = re.findall(r"clean := REGEXP_REPLACE\(:(?:body|clean), '([^']*)', ' '(?:, 1, 0, '(\w)')?\)", _BODY)
    assert len(strips) == 5 and strips[-1][1] == "m"
    nested = _between(pre, "SELECT 1 AS RUN_ID, 'template' AS DRAFT_KIND, t.BODY, ", " AS CLEAN FROM tpl t")
    assert re.findall(r"'([^']*)', ' '(?:, 1, 0, '(\w)')?\)", nested) == strips
    # the KPI read is the proc's, typed like the proc's variables
    for metric, col, typ in (("CREDITS", "VALUE_USD", "NUMBER(38,2)"), ("CREDITS", "VALUE", "NUMBER(38,2)"),
                             ("QUERIES", "VALUE", "NUMBER(38,0)"), ("QUEUED_MINUTES", "VALUE", "NUMBER(38,1)")):
        assert f"(MAX(IFF(METRIC = '{metric}', {col}, NULL)))::{typ}" in pre
        assert f"MAX(IFF(METRIC = '{metric}', {col}, NULL))" in _BODY


# -- guard, order, shape ---------------------------------------------------------------------------------------

def test_v165_first_line_guard_and_version():
    assert _MIG.startswith(f"-- {_NAME}\n")
    assert "not_ready EXCEPTION (-20165, 'V165 requires V164 first - apply migrations in order.');" in _MIG
    assert "IF (v < 164) THEN" in _MIG
    assert "SELECT 165 AS VERSION" in _MIG and "WHERE VERSION = 165);" in _MIG
    # the guard is V160's (V160:44-55) with only the numbers moved
    guard160 = _between(_V160, "EXECUTE IMMEDIATE\n$$\n", "$$;\n")
    want = (guard160.replace("-20160", "-20165").replace("'V160 requires V159 first", "'V165 requires V164 first")
            .replace("IF (v < 159)", "IF (v < 164)"))
    assert _between(_MIG, "EXECUTE IMMEDIATE\n$$\n", "$$;\n") == want
    assert _MIG.count("EXECUTE IMMEDIATE") == 1
    assert _MIG.rstrip().endswith("WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION "
                                  "WHERE VERSION = 165);")


def test_v165_header_names_the_house_sections():
    header = _MIG[:_MIG.index("EXECUTE IMMEDIATE")]
    for section in ("-- WHY:", "-- COST:", "-- LATENCY:", "-- FIRST RUN:", "-- ROLLBACK:"):
        assert section in header, section
    assert header.rstrip().endswith("-- Apply AFTER V164. Idempotent; safe to re-run.")
    assert "re-derived from V112" in header and "$$" not in header


def test_v165_file_order_and_columns():
    guard = _MIG.index("EXCEPTION (-20165")
    alters = [_MIG.index(f"{_ALTER}{c} {t};\n") for c, t in _COLUMNS]
    mark = _MIG.index(_MARKER)
    proc = _MIG.index("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST()")
    version = _MIG.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION")
    assert guard < alters[0] and alters == sorted(alters) and alters[-1] < mark < proc < version
    assert _MIG[mark + len(_MARKER):].startswith("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST()")
    # exactly the six nullable columns, one statement each, no default, no NOT NULL
    stmts = re.findall(rf"^{re.escape(_ALTER)}(\w+) ([^;]+);$", _MIG, re.M)
    assert tuple(stmts) == _COLUMNS
    assert "NOT NULL" not in _MIG[alters[0]:mark] and "DEFAULT" not in _MIG[alters[0]:mark]


def test_v165_nothing_runs_at_apply_and_no_new_object():
    from tests.test_migrations_parse import _plain_statements
    assert _MIG.count("CREATE OR REPLACE PROCEDURE") == 1
    assert _MIG.count("$$") == 4                                   # guard + one proc body, nothing else
    top = _strip_noise("".join(p for i, p in enumerate(_MIG.split("$$")) if i % 2 == 0))
    for banned in ("CALL", "DROP", "TASK", "GRANT", "REVOKE", "TRUNCATE", "UPDATE", "DELETE", "MERGE"):
        assert not re.search(rf"\b{banned}\b", top), banned
    assert re.findall(r"\bALTER TABLE\b", top) == ["ALTER TABLE"] * 6
    kinds = [re.sub(r"^(?:--[^\n]*\n)+", "", s.strip()).split(None, 3)[:3] for s in _plain_statements(_MIG)]
    assert kinds == [["INSERT", "INTO", "DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION"]], kinds
    assert not re.search(r"(?:CREATE(?:\s+OR\s+REPLACE)?\s+(?:TASK|TABLE|VIEW|FUNCTION)|ALTER\s+TASK|EXECUTE\s+TASK)",
                         _strip_noise(_MIG))
    assert "SETTINGS (KEY" not in _MIG and "MERGE INTO" not in _MIG     # no SETTINGS key, no seed
    assert "SP_DAILY_DIGEST()" not in top.replace("PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_DAILY_DIGEST()", "")
    # no address of any kind, and no tagged dollar quote (Snowsight rule)
    assert "@" not in _MIG and not re.findall(r"\$[A-Za-z_][A-Za-z0-9_]*\$", _MIG.replace("SYSTEM$SEND_SNOWFLAKE_NOTIFICATION", ""))


def test_v165_description_fits_and_doubles_apostrophes():
    desc = re.search(r"SELECT 165 AS VERSION,\n       '(.*)' AS DESCRIPTION\n", _MIG, re.S).group(1)
    assert "\n" not in desc
    assert "'" not in desc.replace("''", "")
    assert len(desc.replace("''", "'")) <= 4000
    assert "re-derived from V112" in desc and "No task change, no SETTINGS key, no procedure run at apply time." in desc


# -- lineage + round 13 ----------------------------------------------------------------------------------------

def test_v165_marker_names_the_current_definer():
    from tests.test_proc_lineage import _HISTORICAL_WAIVERS, _definers, _lineage, _migrations, _violations
    texts = _migrations()
    rows = {(r["v"], r["proc"]): r for r in _lineage(texts)}
    r = rows[(165, "SP_DAILY_DIGEST")]
    assert r["src"] == "marker" and r["claims"] == [112] and r["prev"] == 112 and not r["waived"], r
    assert [v for v in _definers(texts)["SP_DAILY_DIGEST"] if v >= 112] == [112, 165]
    assert not [v for v in _violations(texts, _HISTORICAL_WAIVERS) if v.startswith("V165 ")]
    assert "LINEAGE-WAIVER" not in _MIG and _MIG.count("-- >>> derived:") == 1


def test_v165_proc_normalizes_back_to_v112_byte_for_byte():
    assert _reverse(_P) == _P112


@pytest.mark.parametrize("mutation", [
    ("          AND UPPER(COALESCE(r.MIN_SEVERITY, '')) <> 'CRITICAL'", "          AND TRUE"),
    ("WHERE r.ENABLED AND r.DELIVER_DIGEST", "WHERE r.ENABLED"),
    ("            ROLLBACK;\n            RAISE;", "            ROLLBACK;"),
    ("SELECT 'DailyDigest', 'digest_send_failed', :emsg,", "SELECT 'DailyDigest', 'digest_failed', :emsg,"),
])
def test_v165_normalize_has_teeth(mutation):
    old, new = mutation
    assert _P.count(old) == 1
    assert _reverse(_P.replace(old, new)) != _P112


# -- the send ----------------------------------------------------------------------------------------------------

def test_v165_send_is_msg_only_and_never_the_draft():
    send = _between(_BODY, "CALL SYSTEM$SEND_SNOWFLAKE_NOTIFICATION(", "routes_sent := routes_sent + 1;")
    assert "SNOWFLAKE.NOTIFICATION.TEXT_PLAIN(:msg)," in send
    assert _BODY.count("TEXT_PLAIN(") == 1 and _BODY.count("SYSTEM$SEND_SNOWFLAKE_NOTIFICATION(") == 1
    assert "ai_body" not in send.lower() and "CHR(10)" not in send
    assert "SYSTEM$SEND_EMAIL" not in _BODY
    # msg is built after the template decision and the transaction, before the route walk
    i_tpl = _BODY.index("IF (grounding_ok IS NULL OR NOT grounding_ok) THEN")
    i_commit = _BODY.index("        COMMIT;")
    i_msg = _BODY.index("    msg := 'OVERWATCH morning digest")
    i_loop = _BODY.index("    FOR rec IN c_routes DO")
    assert i_tpl < i_commit < i_msg < i_loop
    # the msg header is V112's text (em dash kept), then the chosen body; an AI body carries the measured footer
    assert "msg := 'OVERWATCH morning digest \u2014 ' || TO_VARCHAR(CURRENT_DATE()) || CHR(10) || LEFT(:body, 3000)" in _BODY
    assert "IFF(:body_source = 'AI'," in _BODY and "' of its figures match the exec-board facts.'" in _BODY


def test_v165_escape_is_v064_verbatim_then_rtrim():
    v064 = "".join(ln.replace("message", "msg") + "\n" for ln in
                   _between(_V064, "            message := REPLACE(:message, CHR(92)", "\n\n").splitlines())
    assert [ln.strip() for ln in v064.splitlines()] == [ln.strip() for ln in _ESCAPE.splitlines()[:5]]
    assert _ESCAPE in _BODY
    # the five REPLACEs run on the final msg, before the send
    assert _BODY.index(_ESCAPE) < _BODY.index("    FOR rec IN c_routes DO")
    assert _BODY.count("msg := REPLACE(") == 5


def _escape_like_the_proc(msg: str) -> str:
    """Python mirror of the proc's five REPLACEs + RTRIM(LEFT(msg, 3000), CHR(92))."""
    msg = msg.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "").replace("\t", "\\t")
    return msg[:3000].rstrip("\\")


@pytest.mark.parametrize("body", [
    'Spend was "high" at $12,345.67;\ta tab, a backslash \\ and C:\\path\\n literal.\r\nNext line.',
    "x" * 2990 + "\\\\\\\\\\\\\\",          # a cut escape pair at the 3000 boundary
    "x" * 2996 + '"""""',                 # a cut \" pair at the boundary
    "Templated digest (not AI-written): the AI draft stated figures ...\n\nLast 7 days: spend $1.00.",
    "",
])
def test_v165_escaped_message_is_valid_json_in_the_teams_template(body):
    """The Teams integration splices the message INSIDE a JSON string (snowflake/webhook_delivery.sql); after
    the proc's escape + trailing-backslash trim the card body always parses, and a newline survives as a line
    break (V070/V112 sent a raw CHR(10), which Teams Workflows rejects)."""
    template = re.search(r"WEBHOOK_BODY_TEMPLATE = '(.*)'\n", read("snowflake/webhook_delivery.sql")).group(1)
    assert "SNOWFLAKE_WEBHOOK_MESSAGE" in template
    msg = "OVERWATCH morning digest \u2014 2026-09-30\n" + body[:3000]
    esc = _escape_like_the_proc(msg)
    card = json.loads(template.replace("SNOWFLAKE_WEBHOOK_MESSAGE", esc))
    text = card["attachments"][0]["content"]["body"][0]["text"]
    assert text.startswith("OVERWATCH morning digest \u2014 2026-09-30\n")
    assert len(esc) <= 3000
    with pytest.raises(json.JSONDecodeError):          # teeth: the unescaped V112 message breaks the card
        json.loads(template.replace("SNOWFLAKE_WEBHOOK_MESSAGE", msg))


# -- content -----------------------------------------------------------------------------------------------------

def test_v165_body_is_backslash_free_with_no_inner_dollar_quote():
    assert "\\" not in _BODY and "$$" not in _BODY
    assert _BODY.count("CHR(92)") == 7                            # 3 + 1 + 1 + 0 + 1 escape, 1 RTRIM
    assert "\\" not in _MIG


def test_v165_facts_are_named_with_one_unit_per_key():
    facts_rhs = _rhs("facts", "'WINDOW_DAYS=7")
    alerts_rhs = _rhs("alerts")
    keys = re.findall(r"([A-Z][A-Z0-9_]*)=", " ".join(re.findall(r"'([^']*)'", facts_rhs + " " + alerts_rhs)))
    assert tuple(keys) == _FACT_KEYS
    # SPEND_USD is the dollar value, CREDITS the credit count (V007-V112 sent dollars under CREDITS)
    assert "SELECT MAX(IFF(METRIC = 'CREDITS', VALUE_USD, NULL)), MAX(IFF(METRIC = 'CREDITS', VALUE, NULL))," in _BODY
    assert "INTO :f_spend_usd, :f_credits, :f_queries, :f_failed_q, :f_queued_min, :f_spill_gb, :f_task_runs, :f_task_fail" in _BODY
    assert "'; CREDITS=' || COALESCE(TO_VARCHAR(:f_credits), 'n/a')" in facts_rhs
    assert "COALESCE(VALUE_USD, VALUE)" not in _code(_BODY) and "COALESCE(VALUE_USD, VALUE)" in _BODY112
    # alert counts: COUNT_IF (an empty table reads 0), not V112's SUM(IFF(...))
    assert _BODY.count("COUNT_IF(SEVERITY = ") == 2 and "COUNT_IF(RAISED_AT >= DATEADD('hour', -24" in _BODY
    assert "SUM(IFF(" not in _code(_BODY) and "SUM(IFF(" in _BODY112
    # the same board rows as V112 (company ALL, 7-day window, KPI panel)
    assert "WHERE COMPANY = 'ALL' AND WINDOW_DAYS = 7 AND PANEL = 'KPI';" in _BODY
    assert _BODY.index("facts := :facts || '; ' || :alerts;") < _BODY.index("    prompt := LEFT(")


def test_v165_prompt_allows_only_fact_values():
    prompt = dict(_assignments())["prompt"]
    for phrase in ("Every number you write must be a FACT value, copied or rounded",
                   "never calculate", "never write dates or times", "Dollar amounts come only from",
                   "*_USD facts and percentages only from *_PCT facts; CREDITS are Snowflake credits, not dollars.",
                   "Write three short unnumbered paragraphs", "'FACTS: ' || COALESCE(:facts, 'none')"):
        assert phrase in prompt, phrase
    assert "(1)" not in prompt and "ALERTS:" not in prompt and prompt.endswith("6000)")


def test_v165_cortex_failure_path():
    assert "Digest unavailable" not in _MIG
    handler = _between(_BODY, "        body := SNOWFLAKE.CORTEX.COMPLETE(:model, :prompt);", "    END;\n")
    assert "            body := NULL;\n            ai_err := SQLERRM;\n" in handler
    ledger = _between(_BODY, "    IF (ai_err IS NOT NULL) THEN", "    END IF;")
    assert "'DailyDigest', 'digest_ai_failed', :ai_err," in ledger
    assert "(PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)" in ledger
    # an empty draft is never measured (GROUNDING_OK stays NULL -> the Cortex wording of the template)
    assert "IF (:body IS NOT NULL AND TRIM(:body) <> '') THEN" in _BODY
    assert "        grounding_ok := (n_bad = 0);\n    END IF;" in _BODY
    # the draft is kept for audit before the template may replace body
    assert _BODY.index(_AI_BODY_LINE) < _BODY.index("IF (grounding_ok IS NULL OR NOT grounding_ok) THEN")


def test_v165_template_is_built_only_from_the_facts():
    rhs = _rhs("body", "'Templated digest")
    assert rhs.startswith("'Templated digest (not AI-written): '")
    lits = _STR_RE.findall(rhs)
    fmt = re.compile(r"'[09,.]+'")                                # TO_VARCHAR format models
    digits = {d for lit in lits if not fmt.fullmatch(lit) for d in re.findall(r"[0-9]+", lit)}
    assert digits == {"7", "24"}, digits
    # every bind is a fact / alert variable (or the reason switch): never the draft or the model
    assert set(re.findall(r":(\w+)", _code(rhs))) <= {v for v in _BINDS if v.startswith(("f_", "a_"))} | {"grounding_ok"}
    assert ":ai_body" not in rhs and ":body" not in rhs.replace(":body_source", "")
    # every format model leaves room for 12 integer digits and is TRIMmed (a fixed format pads the sign)
    assert rhs.count("TRIM(TO_VARCHAR(") == len([lit for lit in lits if fmt.fullmatch(lit)]) == 8
    assert "body_source := 'TEMPLATE';" in _BODY


def test_v165_insert_stores_the_columns_within_their_widths():
    ins = _between(_BODY, "        INSERT INTO DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST", ":ai_body);")
    assert "(DIGEST_DATE, COMPANY, MODEL, BODY, FACTS, GROUNDING_OK, FIGURES_CHECKED, UNGROUNDED, BODY_SOURCE, AI_BODY)" in ins
    assert "LEFT(:body, 8000), LEFT(:facts, 4000), :grounding_ok," in ins
    assert "NULLIF(LEFT(LISTAGG(IFF(g.MATCHED, NULL, g.TOK), ', ') WITHIN GROUP (ORDER BY g.POS), 1000), '')" in _BODY
    assert _AI_BODY_LINE.strip().startswith("ai_body := LEFT(:body, 8000);")
    assert {len("TEMPLATE"), len("AI")} <= set(range(21))                           # BODY_SOURCE VARCHAR(20)
    assert "    body_source VARCHAR DEFAULT 'AI';\n" in _BODY
    # inside the unchanged V070 transaction: one DELETE, one INSERT, COMMIT, ROLLBACK + RAISE
    txn = _between(_BODY, "    BEGIN TRANSACTION;", "    -- D7 >>>")
    assert txn.count("DELETE FROM DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST WHERE DIGEST_DATE = CURRENT_DATE();") == 1
    assert txn.count("INSERT INTO") == 1 and txn.count("COMMIT;") == 1
    assert "            ROLLBACK;\n            RAISE;\n" in txn


def test_v165_keeps_the_v112_carry_overs():
    for line in ("        WHERE r.ENABLED AND r.DELIVER_DIGEST   -- V070 #11: only digest-eligible routes\n",
                 "          AND UPPER(COALESCE(r.MIN_SEVERITY, '')) <> 'CRITICAL'",
                 "SELECT 'DailyDigest', 'digest_send_failed', :emsg,",
                 "SELECT 'DailyDigest', 'digest_undelivered',",
                 "    IF (routes_total > 0 AND routes_sent = 0) THEN",
                 "    SELECT COALESCE(MAX(IFF(KEY = 'CORTEX_MODEL', VALUE, NULL)), 'llama3.1-8b')"):
        assert _BODY.count(line) == 1 and _BODY112.count(line) == 1, line


def test_v165_returns_the_version_written_and_why():
    ret = _between(_BODY, "    RETURN 'digest written (' || :body_source", "END;")
    assert "IFF(:grounding_ok = FALSE, '; the AI draft had ' || :n_bad || ' unmatched figure(s)', '')" in ret
    assert "IFF(:ai_err IS NOT NULL, '; Cortex failed', '')" in ret
    assert "|| '); sent ' || :routes_sent || '/' || :routes_total || ' routes'" in ret
    assert "' [UNDELIVERED]'" in ret
    assert _BODY.count("RETURN ") == 1


def test_v165_every_statement_parses():
    """Every SQL statement of the body parses as Snowflake SQL with the :binds as literals, and so does every
    scripting assignment's right-hand side and IF condition (as SELECT <expr>)."""
    sqlglot = pytest.importorskip("sqlglot")
    stmts = _sql_statements()
    targets = [re.match(r"(SELECT|INSERT INTO|DELETE FROM)\s+(\S+)", s).group(0) for s in stmts]
    assert len(stmts) == 10, targets
    for stmt in stmts:
        parsed = sqlglot.parse(_bind(stmt), dialect="snowflake")
        assert len(parsed) == 1 and parsed[0] is not None, stmt[:80]
    assigns = _assignments()
    assert len(assigns) >= 20
    for _var, rhs in assigns:
        if rhs.startswith("SNOWFLAKE.CORTEX.COMPLETE("):
            continue
        sqlglot.parse_one("SELECT " + _bind(rhs), dialect="snowflake")
    for cond in re.findall(r"IF \((.*?)\) THEN", _code(_BODY), re.S):
        sqlglot.parse_one("SELECT " + _bind(cond), dialect="snowflake")


def test_v165_parse_check_has_teeth():
    sqlglot = pytest.importorskip("sqlglot")
    ground = next(s for s in _sql_statements() if "REGEXP_SUBSTR_ALL" in s)
    with pytest.raises(sqlglot.errors.ParseError):
        sqlglot.parse(_bind(ground.replace("LEFT JOIN (", "LEFT JOIN ((", 1)), dialect="snowflake")
    with pytest.raises(KeyError):
        _bind("SELECT :not_a_declared_variable")
    assert _bind("SELECT '[$]?x', :n_bad") == "SELECT '[$]?x', 0"


def test_v165_declares_every_bind_it_uses():
    declared = set(re.findall(r"^    (\w+) (?:VARCHAR|INT|NUMBER|BOOLEAN|CURSOR)", _BODY[:_BODY.index("\nBEGIN\n")], re.M))
    used = set(_BIND_RE.findall(_strip_noise(_BODY)))
    assert used <= declared, used - declared
    assert used <= set(_BINDS), used - set(_BINDS)
    assert {"facts", "grounding_ok", "n_checked", "ungrounded", "body_source", "ai_body", "msg"} <= used

def test_v165_in_expected_migrations():
    """Integrator lockstep: Admin lists V165 with house-rule text (no $, no hand CALL, no trailing '.')."""
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    text = str(_EXPECTED_MIGRATIONS[165])
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_" not in text and "$" not in text and not text.endswith(".")
    assert "GROUNDING_OK" in text and "not AI-written" in text
