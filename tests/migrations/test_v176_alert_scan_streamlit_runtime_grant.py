"""Locks for V176 -- hourly arm [27] SEC_ADMIN_GRANT skips Snowflake's own Streamlit runtime grant (owner 2026-10-12:
"Whenever I deploy the app, this keeps popping up in Teams. I want to turn this off.").

STRUCTURE + GENERATION + LOCKS; tests/migrations/test_v176_harness.py EXECUTES the arm and the runbox grids. What this
file proves:
  * generation -- outputs/gen_v176.py regenerates the migration byte-for-byte and reads only its base (V174); the
    read-only PREFLIGHT / PART B are written only on request, open with the Central pin and parse;
  * shape -- first line, guard (-20176, v < 175), one marker + proc, the version row, and nothing else at apply time;
  * lineage + round 13 -- removing the three declared insertions (the test's OWN copies) gives V174's SP_ALERT_SCAN
    back byte-for-byte, and a stray edit anywhere else breaks the compare;
  * the delta -- [27] gains one uncorrelated rt CTE, one LEFT JOIN and one NOT COALESCE(...) filter (the four decided
    conditions, any NULL keeps the row); no fourth ROLE IN list, no clock literal, [18] / [26] untouched; tallies,
    footprint and labels unchanged; the proc passes the two Snowflake-only locks;
  * the app slice -- Admin, the run docs, RUNBOOK, CLAUDE.md, the playbook and the gated panel help.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._source import ROOT, changelog_entry, read
from tests.test_migration_proc_syntax import _strip_noise

_NAME = "V176__alert_scan_streamlit_runtime_grant.sql"
_MIG = read(f"snowflake/migrations/{_NAME}")
_V174 = read("snowflake/migrations/V174__alert_scan_dsa_admin_role.sql")
_PATTERN = "STPLATSTREAMLIT[0-9]+"
_MANAGED = "SYSTEM$MANAGED"


def _proc(text: str, name: str) -> str:
    s = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    o = text.index("$$", s)
    return text[s:text.index("$$;", o + 2) + 3]


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


def _body(proc: str) -> str:
    return proc[proc.index("$$") + 2:proc.rindex("$$")]


def _in_list(arm: str) -> list[str]:
    (inlist,) = re.findall(r"\bROLE IN \(([^)]*)\)", arm)
    return re.findall(r"'(\w+)'", inlist)


_H, _H174 = _proc(_MIG, "SP_ALERT_SCAN()"), _proc(_V174, "SP_ALERT_SCAN()")
_A18 = ("    -- [18] SEC_NEW_ADMIN_NETWORK", "    IF (MOD(ct_hour, 4) = 1) THEN   -- V157 cadence gate: [20]")
_A26 = ("    -- [26] SEC_LOGIN_TAKEOVER", "    -- [27] SEC_ADMIN_GRANT")
_A27 = ("    -- [27] SEC_ADMIN_GRANT", "    IF (MOD(ct_hour, 3) = 2) THEN   -- V157 cadence gate: [22]")
_MARK = ("-- >>> derived:SP_ALERT_SCAN  (from V174; [27] SEC_ADMIN_GRANT skips a grant with no grantor of "
         "SNOW_ACCOUNTADMINS or SNOW_SYSADMINS to a Snowflake Streamlit runtime user STPLATSTREAMLIT<digits> that also "
         f"got a no-grantor {_MANAGED} grant within 60 s of it -- owner 2026-10-12, V176)\n")
_PREDICATE = ("NOT COALESCE(g.GRANTED_BY IS NULL AND REGEXP_LIKE(g.GRANTEE_NAME, 'STPLATSTREAMLIT[0-9]+') "
              "AND (g.ROLE = 'SNOW_ACCOUNTADMINS' OR g.ROLE = 'SNOW_SYSADMINS') AND rt.GRANTEE_NAME IS NOT NULL, FALSE)")
_RT = ("        rt AS (\n"
       "            SELECT GRANTEE_NAME, MIN(CREATED_ON) AS MARK_FROM, MAX(CREATED_ON) AS MARK_TO\n"
       "            FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS\n"
       f"            WHERE ROLE = '{_MANAGED}'\n"
       "              AND GRANTED_BY IS NULL\n"
       "              AND REGEXP_LIKE(GRANTEE_NAME, 'STPLATSTREAMLIT[0-9]+')\n"
       "            GROUP BY GRANTEE_NAME\n"
       "        )\n")
_JOIN = ("        LEFT JOIN rt\n"
         "          ON rt.GRANTEE_NAME = g.GRANTEE_NAME\n"
         "         AND g.CREATED_ON BETWEEN DATEADD('second', -60, rt.MARK_FROM) AND DATEADD('second', 60, rt.MARK_TO)\n")

# ---------------------------------------------------------------------------------------------------------------
# Test-side copies of every declared insertion (independent of outputs/gen_v176.py): (V174 text, V176 text, slice)
# ---------------------------------------------------------------------------------------------------------------
_DELTAS: list[tuple[str, str, tuple[str, str]]] = [
    ("                           'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS', 'SNOW_PRI_GFR_PRD_ALFA_DSA')\n        )\n",
     "                           'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS', 'SNOW_PRI_GFR_PRD_ALFA_DSA')\n        ),\n" + _RT,
     _A27),
    ("         AND g.CREATED_ON >= DATEADD('hour', -26, CURRENT_TIMESTAMP())\n",
     "         AND g.CREATED_ON >= DATEADD('hour', -26, CURRENT_TIMESTAMP())\n" + _JOIN +
     f"        WHERE {_PREDICATE}\n", _A27),
    ("    --      makes the user an OVERWATCH admin (in-app writes and the account levers), so it raises too.\n"
     "    BEGIN\n",
     "    --      makes the user an OVERWATCH admin (in-app writes and the account levers), so it raises too.\n"
     "    --      V176 (owner 2026-10-12): skips only a no-grantor SNOW_ACCOUNTADMINS / SNOW_SYSADMINS grant to a "
     "Streamlit\n"
     f"    --      runtime user (STPLATSTREAMLIT<digits>, a new one per deploy, also system-granted {_MANAGED}).\n"
     "    BEGIN\n", _A27),
]


def _reverse(body: str, deltas: list[tuple[str, str, tuple[str, str]]]) -> str:
    """Reverse each declared insertion inside its arm slice (asserting the new text is there, once)."""
    for old, new, (start, end) in deltas:
        i = body.index(start)
        j = body.index(end, i)
        seg = body[i:j]
        assert seg.count(new) == 1, new[:90]
        body = body[:i] + seg.replace(new, old) + body[j:]
    return body


def _gen_env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("V176_OUT", "PREFLIGHT_OUT", "PART_B_OUT")}
    env.update(extra)
    return env


def _run_gen(tmp_path: Path, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "outputs" / "gen_v176.py")], env=_gen_env(**extra),
                          cwd=tmp_path, capture_output=True, text=True)


def _extras(tmp_path: Path) -> dict[str, str]:
    pf, pb = tmp_path / "PF.sql", tmp_path / "PB.sql"
    result = _run_gen(tmp_path, V176_OUT=str(tmp_path / "m.sql"), PREFLIGHT_OUT=str(pf), PART_B_OUT=str(pb))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "m.sql").read_text(encoding="utf-8") == _MIG       # the extras never change the migration
    return {"preflight": pf.read_text(encoding="utf-8"), "part_b": pb.read_text(encoding="utf-8")}


# -- generation ----------------------------------------------------------------------------------------------
def test_v176_regenerates_byte_identical_and_writes_nothing_else(tmp_path):
    out = tmp_path / "regen.sql"
    result = _run_gen(tmp_path, V176_OUT=str(out))
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == (ROOT / "snowflake" / "migrations" / _NAME).read_bytes(), (
        "V176 drifted from its forward-generation -- edit outputs/gen_v176.py, not the .sql.")
    assert b"\r\n" not in out.read_bytes()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["regen.sql"]
    assert "PREFLIGHT" not in result.stdout and "PART B" not in result.stdout


def test_v176_generator_reads_only_its_base():
    gen = read("outputs/gen_v176.py")
    assert re.findall(r'MIG / "(V\d+__\w+\.sql)"', gen) == ["V174__alert_scan_dsa_admin_role.sql"]
    assert "import app" not in gen and "from app" not in gen
    assert gen.count(".read_text(") == 1


@pytest.mark.parametrize("which", ["preflight", "part_b"])
def test_v176_preflight_and_part_b_are_read_only_and_parse(tmp_path, which):
    sql = _extras(tmp_path)[which]
    code = _strip_noise(sql)
    stmts = [s.strip() for s in code.split(";") if s.strip()]
    assert stmts[0] == "ALTER SESSION SET TIMEZONE =" and sql.count("ALTER SESSION SET TIMEZONE") == 1
    assert "ALTER SESSION SET TIMEZONE = 'America/Chicago';" in sql
    code = code.replace("ALTER SESSION SET TIMEZONE =", "", 1)
    for banned in ("INSERT", "UPDATE", "DELETE", "MERGE", "CALL", "CREATE", "ALTER", "DROP", "TRUNCATE", "GRANT",
                   "REVOKE", "EXECUTE", "USE"):
        assert not re.search(rf"\b{banned}\b", code, re.I), (which, banned)
    assert not re.search(r"(?<![:\w]):[A-Za-z_]\w*", code), "a scripting :bind survived"
    assert "$$" not in sql and "@" not in sql and "\r" not in sql and sql.isascii()
    assert "SNOWFLAKE_ADMINS" not in sql and "<digits>" not in _strip_noise(sql)
    sqlglot = pytest.importorskip("sqlglot")
    from sqlglot import exp
    body = sql.replace("ALTER SESSION SET TIMEZONE = 'America/Chicago';", "")
    parsed = [p for p in sqlglot.parse(body, read="snowflake") if p is not None]
    assert len(parsed) == 3
    writes = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Command)
    for tree in parsed:
        assert tree.key == "select" or isinstance(tree, exp.Union), tree.key
        assert not [type(n).__name__ for n in tree.walk() if isinstance(n, writes)]


def test_v176_preflight_reuses_the_arms_own_text(tmp_path):
    pre = _extras(tmp_path)["preflight"]
    for grid in ("-- P176.1a ", "-- P176.1b ", "-- P176.2 "):
        assert pre.count(grid) == 1, grid
    arm = _between(_H, *_A27)

    def dedent(block: str) -> str:
        return "".join(ln[8:] + "\n" for ln in block.splitlines())

    ag = dedent(_between(arm, "        ag AS (\n", "        ),\n        rt AS (") + "        )\n")
    rt = dedent(_RT)
    p1a, p1b = _between(pre, "-- P176.1a ", "-- P176.1b "), _between(pre, "-- P176.1b ", "-- P176.2 ")
    p2 = pre[pre.index("-- P176.2 "):]
    for grid in (p1a, p1b):
        assert f"WITH {ag.rstrip(chr(10))},\n{rt.rstrip(chr(10))}" in grid and grid.count(_PREDICATE) == 1
    assert f"WITH {rt.rstrip(chr(10))}\n" in p2 and _PREDICATE not in p2
    assert "'last 26h'" in p1a and "DATEADD('hour', -26, CURRENT_TIMESTAMP())" in p1a
    assert "'last 30 days'" in p1a and p1a.count("DATEADD('day', -30, CURRENT_TIMESTAMP())") == 2
    assert "IFF(" + _PREDICATE + ", 'still raises', 'skipped by V176') AS V176_VERDICT" in p1b
    # P176.2: the open / ack / snoozed system grants of the owner pair to runtime names, the arm's 'by ?' detail
    for frag in ("e.RULE_ID = 'SEC_ADMIN_GRANT'", "e.STATUS IN ('OPEN', 'ACK', 'SNOOZED')",
                 f"REGEXP_LIKE(SPLIT_PART(e.DEDUPE_KEY, '|', 2), '{_PATTERN}')",
                 "SPLIT_PART(e.DEDUPE_KEY, '|', 3) IN ('SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')",
                 "e.DETAIL LIKE 'Granted % Central by ?. %'", "EXPECTED, not NOISE"):
        assert frag in p2, frag
    assert "COALESCE(g.GRANTED_BY, '?')" in arm and "' Central by ' ||" in arm   # the 'by ?' the LIKE reads


def test_v176_part_b_checks_the_v176_body_with_quote_free_fragments(tmp_path):
    part_b = _extras(tmp_path)["part_b"]
    ddl = "GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()')"
    searched = re.findall(rf"(?:CONTAINS|REGEXP_COUNT)\({re.escape(ddl)}, '([^']*)'\)", part_b)
    assert len(searched) == 9 and set(searched) == {
        "STPLATSTREAMLIT", _PATTERN, "g.GRANTED_BY IS NULL AND REGEXP_LIKE(g.GRANTEE_NAME", "rt.GRANTEE_NAME IS NOT NULL",
        _MANAGED, "V176 (owner 2026-10-12)", "alert scan v14 (V168:", "V174 (owner 2026-10-05)"}
    for frag in searched:                         # GET_DDL quotes the body: a fragment carries no quote / backslash
        assert not set(frag) & {"'", "\\", "\n"} and frag in _H, frag
    assert _H.count("STPLATSTREAMLIT") == 3 and "STPLATSTREAMLIT" not in _H174
    assert f"REGEXP_COUNT({ddl}, 'STPLATSTREAMLIT') = 3" in part_b
    a176 = "(SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 176)"
    v2 = _between(part_b, "-- V176.2 ", "-- V176.3 ")
    assert v2.count(f"MAX(LAST_LOAD_TS) >= DATEADD('minute', 55, {a176})") == 2
    assert v2.count(f"LOGGED_AT >= DATEADD('minute', 30, {a176})") == v2.count("LOGGED_AT >= ") == 2
    assert "CONTEXT LIKE 'rule SEC_ADMIN_GRANT %'" in v2 and "'alert scan 14/14 rule blocks ok'" in v2
    v3 = part_b[part_b.index("-- V176.3 "):]
    assert v3.count(_PREDICATE) == 1 and f"RAISED_AT >= DATEADD('minute', 30, {a176})" in v3
    assert "AND g.CREATED_ON <= DATEADD('hour', -4, CURRENT_TIMESTAMP())" in v3
    assert v3.count("WHEN (SELECT COUNT(*) FROM dep) = 0") == 3            # the deploy rows WAIT until one is on record
    for check in ("'V176.1 SCHEMA_VERSION has 176'", "'V176.1 SEC_ADMIN_GRANT is enabled'", "WHERE VERSION = 176"):
        assert check in part_b, check
    assert part_b.count("'V176.2 ") == 4 and part_b.count("'V176.3 ") == 4


# -- guard, order, shape -------------------------------------------------------------------------------------
def test_v176_first_line_guard_and_version():
    assert _MIG.startswith(f"-- {_NAME}\n")
    guard174 = _between(_V174, "EXECUTE IMMEDIATE\n$$\n", "$$;\n")
    want = (guard174.replace("-20174", "-20176").replace("'V174 requires V173 first", "'V176 requires V175 first")
            .replace("IF (v < 173)", "IF (v < 175)"))
    assert _between(_MIG, "EXECUTE IMMEDIATE\n$$\n", "$$;\n") == want
    assert "not_ready EXCEPTION (-20176, 'V176 requires V175 first - apply migrations in order.');" in _MIG
    assert _MIG.count("EXECUTE IMMEDIATE") == 1 and "SELECT 176 AS VERSION" in _MIG
    assert _MIG.rstrip().endswith("WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION "
                                  "WHERE VERSION = 176);")
    header = _MIG[:_MIG.index("EXECUTE IMMEDIATE")]
    for word in ("WHY:", "COST:", "LATENCY:", "FIRST RUN:", "ROLLBACK:",
                 "Apply AFTER V175, any time; no repairs; no app redeploy needed. Idempotent; safe to re-run."):
        assert word in header, word
    flat = " ".join(ln.lstrip("- ") for ln in header.splitlines())
    for phrase in ("Whenever I deploy the app, this keeps popping up in Teams. I want to turn this off.",
                   "re-derived from V174", "(a) GRANTED_BY IS NULL", "(b) the grantee name is exactly STPLATSTREAMLIT",
                   "(c) the role is SNOW_ACCOUNTADMINS or SNOW_SYSADMINS", f"(d) the same user also got a no-grantor "
                   f"{_MANAGED} grant within 60 s of the admin grant", "does not inherit that deploy's marker", "it fails open", "The ag CTE is untouched",
                   "[26] SEC_LOGIN_TAKEOVER included", "PREFLIGHT P176.2", "PART B V176.1-V176.3",
                   "Rolling back V176", "resolve them as EXPECTED"):
        assert phrase in flat, phrase
    assert header.isascii()


def test_v176_file_order_and_nothing_runs_at_apply():
    from tests.test_migrations_parse import _plain_statements
    order = [_MIG.index("EXCEPTION (-20176"), _MIG.index(_MARK),
             _MIG.index("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()"),
             _MIG.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION")]
    assert order == sorted(order)
    assert _MIG[_MIG.index(_MARK) + len(_MARK):].startswith("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH."
                                                             "SP_ALERT_SCAN()")
    kinds = [re.sub(r"^(?:--[^\n]*\n)+", "", s.strip()).split(None, 2)[:2] for s in _plain_statements(_MIG)]
    assert kinds == [["INSERT", "INTO"]], kinds
    assert _MIG.count("CREATE OR REPLACE PROCEDURE") == 1 and _MIG.count("$$") == 4
    top = _strip_noise("".join(p for i, p in enumerate(_MIG.split("$$")) if i % 2 == 0))
    for banned in ("CALL", "ALTER", "DROP", "TASK", "GRANT", "REVOKE", "TRUNCATE", "DELETE", "MERGE", "VIEW",
                   "FUNCTION", "TABLE", "UPDATE"):
        assert not re.search(rf"\b{banned}\b", top), banned
    assert len(re.findall(r"\bCREATE\b", top)) == 1
    assert "ALERT_EVENTS" not in top and "ALERT_CONFIG" not in top
    assert "PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY" not in _MIG
    assert not re.search(r"\$[A-Za-z_][A-Za-z0-9_]*\$", _MIG) and "\r" not in _MIG


def test_v176_description_fits_and_has_no_apostrophe_or_dollar():
    desc = re.search(r"SELECT 176 AS VERSION,\n       '(.*)' AS DESCRIPTION\n", _MIG, re.S).group(1)
    assert "\n" not in desc and "'" not in desc and "$" not in desc and len(desc) <= 4000
    for item in ("re-derived from V174", "SEC_ADMIN_GRANT", "STPLATSTREAMLIT", "GRANTED_BY", "SNOW_ACCOUNTADMINS",
                 "SNOW_SYSADMINS", "2026-10-12", "any NULL keeps the row", "No task change"):
        assert item in desc, item


# -- lineage + round 13 ------------------------------------------------------------------------------------
def _assert_v176_lineage(texts: dict[int, str]) -> None:
    """Bounded at 176: a later re-derivation must not turn it red."""
    from tests.test_proc_lineage import _HISTORICAL_WAIVERS, _definers, _lineage, _violations
    rows = {(r["v"], r["proc"]): r for r in _lineage(texts)}
    r = rows[(176, "SP_ALERT_SCAN")]
    assert r["src"] == "marker" and r["claims"] == [174] and r["prev"] == 174 and not r["waived"], r
    defs = _definers(texts)
    assert [v for v in defs["SP_ALERT_SCAN"] if 174 <= v <= 176] == [174, 176]
    assert 176 not in defs["SP_ALERT_SCAN_DAILY"]
    assert not [v for v in _violations(texts, _HISTORICAL_WAIVERS) if v.startswith("V176 ")]


def test_v176_marker_names_the_current_definer():
    from tests.test_proc_lineage import _migrations
    _assert_v176_lineage(_migrations())
    assert "LINEAGE-WAIVER" not in _MIG and _MIG.count("-- >>> derived:") == 1


def test_v176_lineage_lock_survives_a_later_re_derivation():
    from tests.test_proc_lineage import _migrations
    texts = _migrations()
    assert max(texts) < 999
    texts[999] = ("-- >>> derived:SP_ALERT_SCAN  (from V176)\nCREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH."
                  "SP_ALERT_SCAN()\nRETURNS VARCHAR LANGUAGE SQL AS\n$$\nBEGIN\n    RETURN 'x';\nEND;\n$$;\n")
    _assert_v176_lineage(texts)


def test_v176_normalizes_back_to_v174_byte_for_byte():
    assert _H != _H174 and _reverse(_H, _DELTAS) == _H174


@pytest.mark.parametrize("victim", [
    "'rule SEC_ADMIN_GRANT - other rules unaffected'", "'rule SEC_LOGIN_TAKEOVER - other rules unaffected'",
    "ON c.RULE_ID = 'SEC_ADMIN_GRANT'", "nn.LOGINS >= c.THRESHOLD_NUM",
    "FROM recent r", "alert scan v14 (V168:", "(HOUR(a.TS_CT) >= 20 OR HOUR(a.TS_CT) < 6",
    "COALESCE(g.GRANTED_BY, '?')", "ROW_NUMBER() OVER (PARTITION BY GRANTEE_NAME, ROLE ORDER BY CREATED_ON) - 1",
    "'SNOW_SYSADMINS', 'SNOW_PRI_GFR_PRD_ALFA_DSA')\n", "AND hi.RAISED_AT <= DATEADD('hour', 48, lo.RAISED_AT)",
])
def test_v176_normalize_check_has_teeth(victim):
    """Deleting one character anywhere outside the declared insertions breaks the byte compare."""
    assert _H.count(victim) >= 1, victim
    assert _reverse(_H.replace(victim, victim[:-1], 1), _DELTAS) != _H174


@pytest.mark.parametrize("i", range(3))
def test_v176_every_declared_insertion_is_real(i):
    """Skipping any one insertion's reversal leaves a body that is NOT V174's."""
    assert _reverse(_H, [d for k, d in enumerate(_DELTAS) if k != i]) != _H174


# -- the delta -----------------------------------------------------------------------------------------------
def test_v176_changes_only_arm_27_and_adds_one_predicate():
    arm = _between(_H, *_A27)
    assert arm.count(_PREDICATE) == 1 and _H.count(_PREDICATE) == 1 and arm.count(_RT) == 1
    assert arm.count(_JOIN) == 1
    assert _H.count(f"'{_PATTERN}'") == 2 and _H.count(f"'{_MANAGED}'") == 1
    # [18] and [26] are V174's byte for byte (probe S4: the runtime users have no LOGIN_HISTORY)
    for span in (_A18, _A26):
        assert _between(_H, *span) == _between(_H174, *span) and "STPLAT" not in _between(_H, *span)
    assert _H.replace(arm, "") == _H174.replace(_between(_H174, *_A27), "")
    # the three ROLE IN lists are unchanged; the pair renders as = OR =, never a fourth list
    assert len(re.findall(r"\bROLE IN \(", _H)) == 3
    for span in (_A18, _A26, _A27):
        assert _in_list(_between(_H, *span)) == _in_list(_between(_H174, *span))
    # the filter sits after the JOIN, never inside the ag CTE (PRIOR_GRANTS reads the full history)
    ag = _between(arm, "        ag AS (\n", "        ),\n        rt AS (")
    assert ag == _between(_between(_H174, *_A27), "        ag AS (\n", "        )\n") and "STPLAT" not in ag
    assert arm.index(_RT) < arm.index("        FROM cfg c\n") < arm.index(_PREDICATE) < arm.index(") b (RULE_ID")
    # an uncorrelated CTE: rt names only GRANTS_TO_USERS' own columns, no outer alias and no time bound; CREATED_ON
    # only feeds the MARK_FROM / MARK_TO the join's 60 s window reads (review r1)
    assert not re.search(r"\b(?:g|c|b|e)\.", _RT) and "DATEADD" not in _RT and "CURRENT_TIMESTAMP" not in _RT
    assert _RT.count("CREATED_ON") == 2 and "MIN(CREATED_ON) AS MARK_FROM, MAX(CREATED_ON) AS MARK_TO" in _RT
    # no new clock literal, no THRESHOLD_NUM read, no EXISTS / IN subquery added
    clocks = re.compile(r"(?:HOUR|DAYOFWEEKISO)\([\w.]+\) (?:>=|<|>|<=|=) \d+")
    assert sorted(clocks.findall(_H)) == sorted(clocks.findall(_H174))
    assert arm.count("THRESHOLD_NUM") == _between(_H174, *_A27).count("THRESHOLD_NUM") == 0
    assert arm.count("EXISTS") == _between(_H174, *_A27).count("EXISTS") == 1


def test_v176_fails_open_and_needs_all_four_conditions():
    """The predicate's shape: one COALESCE over four AND-ed conditions (a NULL in any of them -> FALSE -> kept)."""
    inner = _PREDICATE[len("NOT COALESCE("):-len(", FALSE)")]
    assert inner.split(" AND ") == ["g.GRANTED_BY IS NULL", f"REGEXP_LIKE(g.GRANTEE_NAME, '{_PATTERN}')",
                                    "(g.ROLE = 'SNOW_ACCOUNTADMINS' OR g.ROLE = 'SNOW_SYSADMINS')",
                                    "rt.GRANTEE_NAME IS NOT NULL"]
    assert "\\" not in _PREDICATE and "'STPLATSTREAMLIT[0-9]+'" in _PREDICATE    # [0-9], never \d
    assert f"WHERE ROLE = '{_MANAGED}'" in _RT and "AND GRANTED_BY IS NULL" in _RT


def test_v176_tallies_footprint_and_labels_unchanged():
    from tests.test_alert_rule_consistency import _FAILS_INC_RE, _SCAN_DENOMINATORS
    assert len(_FAILS_INC_RE.findall(_H)) == len(_FAILS_INC_RE.findall(_H174)) == 14
    self_alert_re, return_re = _SCAN_DENOMINATORS["SP_ALERT_SCAN"]
    assert re.findall(self_alert_re, _H) == re.findall(self_alert_re, _H174)
    assert re.findall(return_re, _H) == re.findall(return_re, _H174)
    assert set(re.findall(r"SNOWFLAKE\.ACCOUNT_USAGE\.(\w+)", _body(_H))) == set(
        re.findall(r"SNOWFLAKE\.ACCOUNT_USAGE\.(\w+)", _body(_H174)))
    assert re.findall(r"RETURN '[^']*'", _H) == re.findall(r"RETURN '[^']*'", _H174)
    assert "$$" not in _body(_H) and "\\" not in _body(_H)


def test_v176_passes_the_two_snowflake_only_locks():
    from tests.test_snowflake_supported_subqueries import violations as subquery_violations
    from tests.test_sql_division_guards import latest_definers
    from tests.test_sql_division_guards import violations as division_violations
    assert not subquery_violations({"SP_ALERT_SCAN()": _H})
    assert not division_violations({"SP_ALERT_SCAN()": _H})
    assert latest_definers()["SP_ALERT_SCAN()"][0] >= 176


def test_v176_changed_arm_parses_with_one_role_list():
    sqlglot = pytest.importorskip("sqlglot")
    from sqlglot import exp
    arm = _between(_H, *_A27)
    stmt = arm[arm.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS"):arm.index(";\n    EXCEPTION")]
    (parsed,) = sqlglot.parse(stmt, read="snowflake")
    assert [c.name for c in parsed.this.expressions] == ["RULE_ID", "COMPANY", "SEVERITY", "TITLE", "DETAIL",
                                                         "METRIC_VALUE", "DEDUPE_KEY"]
    assert len([n for n in parsed.find_all(exp.In) if n.this.name == "ROLE"]) == 1
    ctes = {c.alias for c in parsed.find_all(exp.CTE)}
    assert {"cfg", "ag", "rt"} <= ctes
    joins = [j for j in parsed.find_all(exp.Join) if j.this.name == "rt"]
    assert len(joins) == 1 and joins[0].side == "LEFT"
    assert any(isinstance(n.this, exp.Coalesce) for n in parsed.find_all(exp.Not))


# -- the app slice (Admin, docs, playbook, the gated panel help) ---------------------------------------------
def test_v176_in_expected_migrations():
    """Admin lists V176 with house-rule text (no $, no hand CALL, no trailing '.')."""
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    text = str(_EXPECTED_MIGRATIONS[176])
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_" not in text and "$" not in text and not text.endswith(".")
    for phrase in ("SEC_ADMIN_GRANT", "STPLATSTREAMLIT", "re-derived from V174", "every other admin grant still raises",
                   "No task change, no apply-time run"):
        assert phrase in text, phrase


def test_v176_run_docs_list_it_with_a_short_apply_note():
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert f"snowflake/migrations/{_NAME}" in read(rel), rel
    dep = read("DEPLOYMENT.md")
    block = dep[dep.index("> **V176 ("):]
    block = block[:block.index("\n\n")]
    note = " ".join(" ".join(ln.lstrip("> ") for ln in block.splitlines()).split())
    for phrase in ("after V175", "error -20176", "no app redeploy needed", "PREFLIGHT P176.1a / P176.1b", "P176.2",
                   "PART B V176.1", "V176.2", "V176.3", "resolve those as EXPECTED", "V174 -> V175 -> V176",
                   "Rolling back V176"):
        assert phrase in note, phrase
    rb = read("RUNBOOK.md")
    assert "**Rolling back V176.**" in rb
    para = rb[rb.index("**Rolling back V176.**"):].split("\n\n", 1)[0]
    assert "V174__alert_scan_dsa_admin_role.sql" in para and "never disable SEC_ADMIN_GRANT" in para
    (row,) = [ln for ln in rb.splitlines() if ln.startswith("| SEC_ADMIN_GRANT | SECURITY |")]
    assert "since V176 except" in row and "STPLATSTREAMLIT" in row and "hourly [27], V162" in row


def test_v176_claude_md_and_changelog_name_it():
    claude = " ".join(read("CLAUDE.md").split())
    assert "SP_ALERT_SCAN = V176 (V176 changed only hourly [27] SEC_ADMIN_GRANT" in claude
    assert "security_sql._not_streamlit_runtime_grant" in claude
    entry = changelog_entry("4.613.0")
    assert entry.startswith("## 4.613.0 - Deploys no longer page Teams with a Streamlit runtime admin grant")
    for phrase in ("Whenever I deploy the app, this keeps popping up in Teams.", "V174 -> V175 -> V176",
                   "No redeploy is needed for V176", "**EXPECTED**", "Use EXPECTED, not NOISE", "SEC_BREAK_GLASS_USE",
                   "What still alerts, exactly as before"):
        assert phrase in entry, phrase


def test_v176_playbook_names_the_skip_at_the_end_of_means():
    from app.logic.playbooks import PLAYBOOKS
    pb = PLAYBOOKS["SEC_ADMIN_GRANT"]
    means = pb[:pb.index("\n\n")]
    assert "since V174 also SNOW_PRI_GFR_PRD_ALFA_DSA" in means                      # V174's lock still holds
    assert means.index("never auto-declares an incident") < means.index("Since V176")
    for frag in ("Since V176 one shape is skipped", "no grantor", "exactly STPLATSTREAMLIT plus digits",
                 f"`{_MANAGED}`", "still raises"):
        assert frag in means, frag
    assert "$" not in re.sub(r"`[^`]*`", "", pb)


@pytest.mark.parametrize("applied", [False, True])
def test_v176_panel_help_claims_the_alert_skip_only_once_applied(monkeypatch, applied):
    """Law 12: the timing-check read leaves the runtime grants out on its own (ungated); the clause that the
    SEC_ADMIN_GRANT alert skips them too waits for the V176 apply."""
    import app.ui.security_center as sc
    helps: list[str] = []
    asked: list[tuple] = []
    monkeypatch.setattr(sc, "panel_help", helps.append)
    monkeypatch.setattr(sc, "section_header", lambda *_a, **_k: None)
    monkeypatch.setattr(sc, "has_migration", lambda n, page=None: asked.append((n, page)) or applied)
    monkeypatch.setattr(sc, "st", SimpleNamespace(toggle=lambda *_a, **_k: False))
    sc.render_admin_grant_anomalies("ALL")
    (text,) = helps
    assert asked == [(176, "Security")]
    assert "STPLATSTREAMLIT + digits" in text and "Recent grant changes as (system)" in text
    assert "SNOW_ACCOUNTADMINS or SNOW_SYSADMINS" in text and _MANAGED in text
    assert ("The SEC_ADMIN_GRANT alert skips the same grants (V176)." in text) is applied
