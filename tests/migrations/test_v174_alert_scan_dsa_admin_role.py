"""Locks for V174 -- SNOW_PRI_GFR_PRD_ALFA_DSA in the admin-role lists of SP_ALERT_SCAN's hourly security arms
(owner access decision 2026-10-05: a direct DSA holder is an OVERWATCH admin, so a DSA grant is an admin grant).

STRUCTURE + GENERATION + LOCKS; tests/migrations/test_v174_harness.py EXECUTES the three arms. What this file proves:
  * generation -- outputs/gen_v174.py regenerates the migration byte-for-byte and reads only its base (V173); the
    read-only PREFLIGHT / PART B are written only on request, open with the Central pin and parse;
  * shape -- first line, guard (-20174, v < 173), one marker + proc, one guarded NAME refresh, the version row;
  * lineage + round 13 -- reversing every declared delta (the test's OWN copies) gives V173's SP_ALERT_SCAN back
    byte-for-byte, and a stray edit anywhere else breaks the compare;
  * the deltas -- the role is appended to exactly the three admin lists (no fourth list exists in either scan), [26] and
    [27] still carry one list (the app's ALERT_ADMIN_ROLES); tallies, footprint and labels unchanged; the proc passes
    the two Snowflake-only locks (tests/test_snowflake_supported_subqueries.py, tests/test_sql_division_guards.py).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests._source import ROOT, read
from tests.test_migration_proc_syntax import _strip_noise

_NAME = "V174__alert_scan_dsa_admin_role.sql"
_MIG = read(f"snowflake/migrations/{_NAME}")
_V173 = read("snowflake/migrations/V173__alert_scan_supported_subquery_and_div0.sql")
_V162 = read("snowflake/migrations/V162__security_takeover_admin_grant.sql")
_DSA = "SNOW_PRI_GFR_PRD_ALFA_DSA"


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


_H, _H173 = _proc(_MIG, "SP_ALERT_SCAN()"), _proc(_V173, "SP_ALERT_SCAN()")
_A18 = ("    -- [18] SEC_NEW_ADMIN_NETWORK", "    IF (MOD(ct_hour, 4) = 1) THEN   -- V157 cadence gate: [20]")
_A26 = ("    -- [26] SEC_LOGIN_TAKEOVER", "    -- [27] SEC_ADMIN_GRANT")
_A27 = ("    -- [27] SEC_ADMIN_GRANT", "    IF (MOD(ct_hour, 3) = 2) THEN   -- V157 cadence gate: [22]")
_MARK = (f"-- >>> derived:SP_ALERT_SCAN  (from V173; + {_DSA} at the end of the admin-role lists of arms [18] "
         "SEC_NEW_ADMIN_NETWORK, [26] SEC_LOGIN_TAKEOVER and [27] SEC_ADMIN_GRANT -- owner access decision "
         "2026-10-05, V174)\n")
_SEVEN = ("ACCOUNTADMIN", "SECURITYADMIN", "SYSADMIN", "USERADMIN", "ORGADMIN", "SNOW_ACCOUNTADMINS", "SNOW_SYSADMINS")

# ---------------------------------------------------------------------------------------------------------------
# Test-side copies of every declared delta (independent of outputs/gen_v174.py): (V173 text, V174 text, arm slice)
# ---------------------------------------------------------------------------------------------------------------
_DELTAS: list[tuple[str, str, tuple[str, str]]] = [
    ("                  AND ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')\n",
     f"                  AND ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS', '{_DSA}')\n", _A18),
    ("    --      key, never raises one episode twice, and a failures-only key never swallows the success.\n"
     "    BEGIN\n",
     "    --      key, never raises one episode twice, and a failures-only key never swallows the success.\n"
     f"    --      V174 (owner 2026-10-05): + {_DSA}. Its direct holders are OVERWATCH admins (in-app\n"
     "    --      writes and the account levers), so the arm watches their logins like the three it watched.\n"
     "    BEGIN\n", _A18),
    ("                            'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')\n",
     f"                            'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS', '{_DSA}')\n", _A26),
    ("    --      it; a CRIT/WARN band crossing re-fires and the V067 sweep supersedes the WARN row.\n    BEGIN\n",
     "    --      it; a CRIT/WARN band crossing re-fires and the V067 sweep supersedes the WARN row.\n"
     f"    --      V174 (owner 2026-10-05): the admin-tier list adds {_DSA} (its direct\n"
     "    --      holders are OVERWATCH admins), so a takeover of one is CRITICAL at any hour.\n"
     "    BEGIN\n", _A26),
    ("                           'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')\n",
     f"                           'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS', '{_DSA}')\n", _A27),
    ("    --      ends in the grant's Central CREATED_ON to the millisecond with an explicit format.\n    BEGIN\n",
     "    --      ends in the grant's Central CREATED_ON to the millisecond with an explicit format.\n"
     f"    --      V174 (owner 2026-10-05): the owner list adds {_DSA}; a direct grant of it\n"
     "    --      makes the user an OVERWATCH admin (in-app writes and the account levers), so it raises too.\n"
     "    BEGIN\n", _A27),
]


def _reverse(body: str, deltas: list[tuple[str, str, tuple[str, str]]]) -> str:
    """Reverse each declared delta inside its arm slice (asserting the new text is there, once)."""
    for old, new, (start, end) in deltas:
        i = body.index(start)
        j = body.index(end, i)
        seg = body[i:j]
        assert seg.count(new) == 1, new[:90]
        body = body[:i] + seg.replace(new, old) + body[j:]
    return body


def _gen_env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("V174_OUT", "PREFLIGHT_OUT", "PART_B_OUT")}
    env.update(extra)
    return env


def _run_gen(tmp_path: Path, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "outputs" / "gen_v174.py")], env=_gen_env(**extra),
                          cwd=tmp_path, capture_output=True, text=True)


def _extras(tmp_path: Path) -> dict[str, str]:
    pf, pb = tmp_path / "PF.sql", tmp_path / "PB.sql"
    result = _run_gen(tmp_path, V174_OUT=str(tmp_path / "m.sql"), PREFLIGHT_OUT=str(pf), PART_B_OUT=str(pb))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "m.sql").read_text(encoding="utf-8") == _MIG       # the extras never change the migration
    return {"preflight": pf.read_text(encoding="utf-8"), "part_b": pb.read_text(encoding="utf-8")}


# -- generation ----------------------------------------------------------------------------------------------
def test_v174_regenerates_byte_identical_and_writes_nothing_else(tmp_path):
    out = tmp_path / "regen.sql"
    result = _run_gen(tmp_path, V174_OUT=str(out))
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == (ROOT / "snowflake" / "migrations" / _NAME).read_bytes(), (
        "V174 drifted from its forward-generation -- edit outputs/gen_v174.py, not the .sql.")
    assert b"\r\n" not in out.read_bytes()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["regen.sql"]
    assert "PREFLIGHT" not in result.stdout and "PART B" not in result.stdout


def test_v174_generator_reads_only_its_base():
    gen = read("outputs/gen_v174.py")
    assert re.findall(r'MIG / "(V\d+__\w+\.sql)"', gen) == ["V173__alert_scan_supported_subquery_and_div0.sql"]
    assert "import app" not in gen and "from app" not in gen
    assert gen.count(".read_text(") == 1


@pytest.mark.parametrize("which", ["preflight", "part_b"])
def test_v174_preflight_and_part_b_are_read_only_and_parse(tmp_path, which):
    sql = _extras(tmp_path)[which]
    code = _strip_noise(sql)
    stmts = [s.strip() for s in code.split(";") if s.strip()]
    assert stmts[0] == "ALTER SESSION SET TIMEZONE =" and sql.count("ALTER SESSION SET TIMEZONE") == 1
    assert "ALTER SESSION SET TIMEZONE = 'America/Chicago';" in sql
    code = code.replace("ALTER SESSION SET TIMEZONE =", "", 1)
    for banned in ("INSERT", "UPDATE", "DELETE", "MERGE", "CALL", "CREATE", "ALTER", "DROP", "TRUNCATE", "GRANT",
                   "REVOKE", "EXECUTE"):
        assert not re.search(rf"\b{banned}\b", code, re.I), (which, banned)
    assert not re.search(r"(?<![:\w]):[A-Za-z_]\w*", code), "a scripting :bind survived"
    assert "$$" not in sql and "@" not in sql and "\r" not in sql and sql.isascii()
    sqlglot = pytest.importorskip("sqlglot")
    from sqlglot import exp
    body = sql.replace("ALTER SESSION SET TIMEZONE = 'America/Chicago';", "")
    parsed = [p for p in sqlglot.parse(body, dialect="snowflake") if p is not None]
    assert len(parsed) == {"preflight": 4, "part_b": 3}[which]
    writes = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Command)
    for tree in parsed:
        assert tree.key == "select" or isinstance(tree, exp.Union), tree.key
        assert not [type(n).__name__ for n in tree.walk() if isinstance(n, writes)]


def test_v174_preflight_carries_the_arms_own_text(tmp_path):
    pre = _extras(tmp_path)["preflight"]
    for grid in ("-- P174.1 ", "-- P174.2 ", "-- P174.3 ", "-- P174.4 "):
        assert pre.count(grid) == 1, grid

    def dedent(arm: str) -> str:
        stmt = arm[arm.index("        WITH cfg AS (\n"):arm.index(";\n    EXCEPTION")]
        return "".join(ln[8:] + "\n" for ln in stmt.splitlines()).rstrip("\n")

    for head, nxt, span in (("-- P174.2 ", "-- P174.3 ", _A27), ("-- P174.3 ", "-- P174.4 ", _A26),
                            ("-- P174.4 ", None, _A18)):
        grid = pre[pre.index(head):pre.index(nxt) if nxt else len(pre)]
        assert dedent(_between(_H, *span)) in grid, head
    # P174.1 reads only live DIRECT grants and the V173 lists as "already watched"
    p1 = _between(pre, "-- P174.1 ", "-- P174.2 ")
    assert f"ROLE = '{_DSA}'" in p1 and p1.count("DELETED_ON IS NULL") == 2
    assert "ROLE IN (" + ", ".join(f"'{r}'" for r in _SEVEN) + ")" in p1
    assert "COUNT_IF(ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS'))" in p1
    assert "GRANTS_TO_ROLES" not in pre                                   # direct grants only, like the app


def test_v174_part_b_checks_the_v174_body_with_quote_free_fragments(tmp_path):
    part_b = _extras(tmp_path)["part_b"]
    ddl = "GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()')"
    searched = re.findall(rf"(?:CONTAINS|REGEXP_COUNT)\({re.escape(ddl)}, '([^']*)'\)", part_b)
    assert len(searched) == 5 and set(searched) == {_DSA, "alert scan v14 (V168:", "FROM recent r",
                                                    "V174 (owner 2026-10-05)"}
    for frag in searched:                         # GET_DDL quotes the body: a fragment carries no quote / backslash
        assert not set(frag) & {"'", "\\", "\n"} and frag in _H, frag
    assert _H.count(_DSA) == 6 and _H173.count(_DSA) == 0 and "V174 (owner 2026-10-05)" not in _H173
    assert f"REGEXP_COUNT({ddl}, '{_DSA}') = 6" in part_b
    for check in ("'V174.1 SCHEMA_VERSION has 174'", f"'V174.1 SEC_ADMIN_GRANT rule name lists {_DSA}'",
                  "'alert scan 14/14 rule blocks ok'", "CONTEXT LIKE 'rule SEC_NEW_ADMIN_NETWORK %'",
                  "CONTEXT LIKE 'rule SEC_LOGIN_TAKEOVER %'", "CONTEXT LIKE 'rule SEC_ADMIN_GRANT %'",
                  "WHERE VERSION = 174"):
        assert check in part_b, check
    a174 = "(SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 174)"
    v2 = _between(part_b, "-- V174.2 ", "-- V174.3 ")
    assert v2.count(f"MAX(LAST_LOAD_TS) >= DATEADD('minute', 55, {a174})") == 2
    assert v2.count(f"LOGGED_AT >= DATEADD('minute', 30, {a174})") == v2.count("LOGGED_AT >= ") == 2
    assert part_b.count("'V174.2 ") == 4


# -- guard, order, shape -------------------------------------------------------------------------------------
def test_v174_first_line_guard_and_version():
    assert _MIG.startswith(f"-- {_NAME}\n")
    guard173 = _between(_V173, "EXECUTE IMMEDIATE\n$$\n", "$$;\n")
    want = (guard173.replace("-20173", "-20174").replace("'V173 requires V172 first", "'V174 requires V173 first")
            .replace("IF (v < 172)", "IF (v < 173)"))
    assert _between(_MIG, "EXECUTE IMMEDIATE\n$$\n", "$$;\n") == want
    assert "not_ready EXCEPTION (-20174, 'V174 requires V173 first - apply migrations in order.');" in _MIG
    assert _MIG.count("EXECUTE IMMEDIATE") == 1 and "SELECT 174 AS VERSION" in _MIG
    assert _MIG.rstrip().endswith("WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION "
                                  "WHERE VERSION = 174);")
    header = _MIG[:_MIG.index("EXECUTE IMMEDIATE")]
    for word in ("WHY:", "COST:", "LATENCY:", "FIRST RUN:", "ROLLBACK:",
                 "Apply AFTER V173 (alone, any time; no repairs). Idempotent; safe to re-run."):
        assert word in header, word
    flat = " ".join(ln.lstrip("- ") for ln in header.splitlines())
    for phrase in ("2026-10-05", "last 26h", "PREFLIGHT P174.1", "PART B V174.3", "never auto-declare"):
        assert phrase in flat, phrase


def test_v174_file_order_and_only_the_name_refresh_runs_at_apply():
    from tests.test_migrations_parse import _plain_statements
    order = [_MIG.index("EXCEPTION (-20174"), _MIG.index(_MARK),
             _MIG.index("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()"),
             _MIG.index("UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG"),
             _MIG.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION")]
    assert order == sorted(order)
    assert _MIG[_MIG.index(_MARK) + len(_MARK):].startswith("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH."
                                                             "SP_ALERT_SCAN()")
    kinds = [re.sub(r"^(?:--[^\n]*\n)+", "", s.strip()).split(None, 2)[:2] for s in _plain_statements(_MIG)]
    assert kinds == [["UPDATE", "DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG"], ["INSERT", "INTO"]], kinds
    assert _MIG.count("CREATE OR REPLACE PROCEDURE") == 1 and _MIG.count("$$") == 4
    top = _strip_noise("".join(p for i, p in enumerate(_MIG.split("$$")) if i % 2 == 0))
    for banned in ("CALL", "ALTER", "DROP", "TASK", "GRANT", "REVOKE", "TRUNCATE", "DELETE", "MERGE", "VIEW",
                   "FUNCTION", "TABLE"):
        assert not re.search(rf"\b{banned}\b", top), banned
    assert len(re.findall(r"\bUPDATE\b", top)) == 1 and len(re.findall(r"\bCREATE\b", top)) == 1
    assert "ALERT_EVENTS" not in top and "PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY" not in _MIG
    assert not re.search(r"\$[A-Za-z_][A-Za-z0-9_]*\$", _MIG) and "\r" not in _MIG


def test_v174_name_refresh_is_guarded_on_the_v162_seed():
    (new, old) = re.findall(r"UPDATE DBA_MAINT_DB\.OVERWATCH\.ALERT_CONFIG\n   SET NAME = '([^']*)'\n"
                            r" WHERE RULE_ID = 'SEC_ADMIN_GRANT'\n   AND NAME = '([^']*)';\n", _MIG)[0]
    assert f"('SEC_ADMIN_GRANT', 'SECURITY', '{old}', TRUE, 'HIGH', 0, 24)" in _V162       # the V162 seed, verbatim
    assert new == old.replace("SNOW_SYSADMINS)", f"SNOW_SYSADMINS, {_DSA})") != old
    assert f"({', '.join(_in_list(_between(_H, *_A27)))})" in new                         # the arm's list, in order
    assert len(new) <= 200 and "'" not in new                                             # V004 NAME VARCHAR(200)
    # the config replay (Guards A-D) reads the UPDATE as text-only: the rule stays enabled and raised
    from tests.test_alert_rule_consistency import _config_enabled, _raised_rule_ids
    assert {"SEC_ADMIN_GRANT", "SEC_LOGIN_TAKEOVER", "SEC_NEW_ADMIN_NETWORK"} <= _config_enabled() & _raised_rule_ids()


def test_v174_description_fits_and_has_no_apostrophe():
    desc = re.search(r"SELECT 174 AS VERSION,\n       '(.*)' AS DESCRIPTION\n", _MIG, re.S).group(1)
    assert "\n" not in desc and "'" not in desc and "$" not in desc and len(desc) <= 4000
    for item in ("re-derived from V173", _DSA, "SEC_NEW_ADMIN_NETWORK", "SEC_LOGIN_TAKEOVER", "SEC_ADMIN_GRANT",
                 "2026-10-05", "No task change"):
        assert item in desc, item


def test_v174_in_expected_migrations():
    """Admin lists V174 with house-rule text (no $, no hand CALL, no trailing '.')."""
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    text = str(_EXPECTED_MIGRATIONS[174])
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_" not in text and "$" not in text and not text.endswith(".")
    for phrase in (_DSA, "SEC_ADMIN_GRANT", "SEC_LOGIN_TAKEOVER", "SEC_NEW_ADMIN_NETWORK", "re-derived from V173",
                   "No task change, no apply-time run"):
        assert phrase in text, phrase


# -- lineage + round 13 ------------------------------------------------------------------------------------
def _assert_v174_lineage(texts: dict[int, str]) -> None:
    """Bounded at 174: a later re-derivation must not turn it red."""
    from tests.test_proc_lineage import _HISTORICAL_WAIVERS, _definers, _lineage, _violations
    rows = {(r["v"], r["proc"]): r for r in _lineage(texts)}
    r = rows[(174, "SP_ALERT_SCAN")]
    assert r["src"] == "marker" and r["claims"] == [173] and r["prev"] == 173 and not r["waived"], r
    defs = _definers(texts)
    assert [v for v in defs["SP_ALERT_SCAN"] if 173 <= v <= 174] == [173, 174]
    assert 174 not in defs["SP_ALERT_SCAN_DAILY"]
    assert not [v for v in _violations(texts, _HISTORICAL_WAIVERS) if v.startswith("V174 ")]


def test_v174_marker_names_the_current_definer():
    from tests.test_proc_lineage import _migrations
    _assert_v174_lineage(_migrations())
    assert "LINEAGE-WAIVER" not in _MIG and _MIG.count("-- >>> derived:") == 1


def test_v174_lineage_lock_survives_a_later_re_derivation():
    from tests.test_proc_lineage import _migrations
    texts = _migrations()
    assert max(texts) < 999
    texts[999] = ("-- >>> derived:SP_ALERT_SCAN  (from V174)\nCREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH."
                  "SP_ALERT_SCAN()\nRETURNS VARCHAR LANGUAGE SQL AS\n$$\nBEGIN\n    RETURN 'x';\nEND;\n$$;\n")
    _assert_v174_lineage(texts)


def test_v174_normalizes_back_to_v173_byte_for_byte():
    assert _H != _H173 and _reverse(_H, _DELTAS) == _H173


@pytest.mark.parametrize("victim", [
    "'rule SEC_ADMIN_GRANT - other rules unaffected'", "'rule SEC_LOGIN_TAKEOVER - other rules unaffected'",
    "AND g.CREATED_ON >= DATEADD('hour', -26, CURRENT_TIMESTAMP())", "nn.LOGINS >= c.THRESHOLD_NUM",
    "FROM recent r", "alert scan v14 (V168:", "(HOUR(a.TS_CT) >= 20 OR HOUR(a.TS_CT) < 6",
    "AND (g.DELETED_ON IS NULL OR g.DELETED_ON > a.TS)", "AND hi.RAISED_AT <= DATEADD('hour', 48, lo.RAISED_AT)",
])
def test_v174_normalize_check_has_teeth(victim):
    """Deleting one character anywhere outside the declared deltas breaks the byte compare."""
    assert _H.count(victim) >= 1, victim
    assert _reverse(_H.replace(victim, victim[:-1], 1), _DELTAS) != _H173


@pytest.mark.parametrize("i", range(6))
def test_v174_every_declared_delta_is_real(i):
    """Skipping any one delta's reversal leaves a body that is NOT V173's."""
    assert _reverse(_H, [d for k, d in enumerate(_DELTAS) if k != i]) != _H173


# -- the deltas ----------------------------------------------------------------------------------------------
def test_v174_appends_the_role_to_exactly_the_three_admin_lists():
    for span, base in ((_A18, ["ACCOUNTADMIN", "SNOW_ACCOUNTADMINS", "SNOW_SYSADMINS"]), (_A26, list(_SEVEN)),
                       (_A27, list(_SEVEN))):
        assert _in_list(_between(_H173, *span)) == base
        assert _in_list(_between(_H, *span)) == [*base, _DSA]
    # find them all: the three arms hold the only ROLE IN lists (and the only SNOW_* role literals) of either scan
    assert len(re.findall(r"\bROLE IN \(", _H)) == 3 and _H.count(f"'{_DSA}'") == 3
    rest = _H
    for span in (_A18, _A26, _A27):
        rest = rest.replace(_between(_H, *span), "")
    assert not re.search(r"'SNOW_\w+'|'ACCOUNTADMIN'", rest)
    daily = _proc(_V173, "SP_ALERT_SCAN_DAILY()")
    assert not re.search(r"\bROLE IN \(|'SNOW_\w+'|'ACCOUNTADMIN'", daily)


def test_v174_alert_list_stays_the_apps_one_list():
    from app.data import security_sql
    assert tuple(_in_list(_between(_H, *_A26))) == tuple(_in_list(_between(_H, *_A27))) == (*_SEVEN, _DSA)
    assert (*_SEVEN, _DSA) == security_sql.ALERT_ADMIN_ROLES


def test_v174_tallies_footprint_and_labels_unchanged():
    from tests.test_alert_rule_consistency import _FAILS_INC_RE, _SCAN_DENOMINATORS
    assert len(_FAILS_INC_RE.findall(_H)) == len(_FAILS_INC_RE.findall(_H173)) == 14
    self_alert_re, return_re = _SCAN_DENOMINATORS["SP_ALERT_SCAN"]
    assert re.findall(self_alert_re, _H) == re.findall(self_alert_re, _H173)
    assert re.findall(return_re, _H) == re.findall(return_re, _H173)
    assert set(re.findall(r"SNOWFLAKE\.ACCOUNT_USAGE\.(\w+)", _body(_H))) == set(
        re.findall(r"SNOWFLAKE\.ACCOUNT_USAGE\.(\w+)", _body(_H173)))
    assert re.findall(r"RETURN '[^']*'", _H) == re.findall(r"RETURN '[^']*'", _H173)
    assert "$$" not in _body(_H) and "\\" not in _body(_H)


def test_v174_passes_the_two_snowflake_only_locks():
    from tests.test_snowflake_supported_subqueries import violations as subquery_violations
    from tests.test_sql_division_guards import latest_definers
    from tests.test_sql_division_guards import violations as division_violations
    assert not subquery_violations({"SP_ALERT_SCAN()": _H})
    assert not division_violations({"SP_ALERT_SCAN()": _H})
    # and the locks scan this body (or a later re-derivation of it) on every CI pass
    assert latest_definers()["SP_ALERT_SCAN()"][0] >= 174


def test_v174_changed_arm_statements_parse():
    sqlglot = pytest.importorskip("sqlglot")
    from sqlglot import exp
    for span in (_A18, _A26, _A27):
        arm = _between(_H, *span)
        stmt = arm[arm.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS"):arm.index(";\n    EXCEPTION")]
        (parsed,) = sqlglot.parse(stmt, dialect="snowflake")
        assert [c.name for c in parsed.this.expressions] == ["RULE_ID", "COMPANY", "SEVERITY", "TITLE", "DETAIL",
                                                             "METRIC_VALUE", "DEDUPE_KEY"]
        (inl,) = [n for n in parsed.find_all(exp.In) if n.this.name == "ROLE"]
        assert [e.name for e in inl.expressions][-1] == _DSA


def test_v174_playbooks_name_the_role_for_the_three_rules():
    from app.logic.playbooks import PLAYBOOKS
    for rule in ("SEC_NEW_ADMIN_NETWORK", "SEC_LOGIN_TAKEOVER", "SEC_ADMIN_GRANT"):
        pb = PLAYBOOKS[rule]
        means = pb[:pb.index("\n\n")]
        assert f"since V174 also {_DSA}" in means, rule
    # the seven stay named where they were
    for rule in ("SEC_LOGIN_TAKEOVER", "SEC_ADMIN_GRANT"):
        assert all(r in PLAYBOOKS[rule] for r in _SEVEN), rule
