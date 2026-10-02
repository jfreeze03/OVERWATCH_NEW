"""Locks for V173 -- the hotfix for the two production failures of the V162-V172 apply: SP_ALERT_SCAN re-derived from
V168 (arm [18] SEC_NEW_ADMIN_NETWORK's dedupe guard, 'Unsupported subquery type cannot be evaluated' every hourly run
from 2026-10-02 07:08) and SP_ALERT_SCAN_DAILY re-derived from V169 (arm [24] COST_IDLE_OPPORTUNITY, 'Division by
zero' 2026-10-01 06:49).

STRUCTURE + GENERATION + LOCKS; tests/migrations/test_v173_harness.py EXECUTES both arms. What this file proves:
  * generation -- outputs/gen_v173.py regenerates the migration byte-for-byte and reads only its two bases; the
    read-only PREFLIGHT / PART B are written only on request, open with the Central pin and parse;
  * shape -- first line, guard (-20173, v < 172), two markers + procs, the version row; nothing runs at apply time;
  * lineage + round 13 -- reversing every declared delta (the test's OWN copies) gives V168's and V169's bodies back
    byte-for-byte, and a stray edit anywhere else breaks the compare;
  * the deltas -- arm [18]'s guard is three AND-ed NOT EXISTS with plain-equality keys over the recent CTE, arm [24]
    divides by NULLIF only; tallies, footprints and RETURN labels unchanged; both procs pass the two new locks
    (tests/test_snowflake_supported_subqueries.py, tests/test_sql_division_guards.py).
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

_NAME = "V173__alert_scan_supported_subquery_and_div0.sql"
_MIG = read(f"snowflake/migrations/{_NAME}")
_V168 = read("snowflake/migrations/V168__alert_scan_hourly_keys_and_sweeps.sql")
_V169 = read("snowflake/migrations/V169__alert_scan_daily_windows_and_keys.sql")


def _proc(text: str, name: str) -> str:
    s = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    o = text.index("$$", s)
    return text[s:text.index("$$;", o + 2) + 3]


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


def _body(proc: str) -> str:
    return proc[proc.index("$$") + 2:proc.rindex("$$")]


_H, _H168 = _proc(_MIG, "SP_ALERT_SCAN()"), _proc(_V168, "SP_ALERT_SCAN()")
_D, _D169 = _proc(_MIG, "SP_ALERT_SCAN_DAILY()"), _proc(_V169, "SP_ALERT_SCAN_DAILY()")
_A18 = ("    -- [18] SEC_NEW_ADMIN_NETWORK", "    IF (MOD(ct_hour, 4) = 1) THEN   -- V157 cadence gate: [20]")
_A24 = ("    -- [24] COST_IDLE_OPPORTUNITY", "    -- [25] COST_SLEEP_POLLING")
_MARK_H = ("-- >>> derived:SP_ALERT_SCAN  (from V168; arm [18] SEC_NEW_ADMIN_NETWORK dedupe guard as three AND-ed "
           "NOT EXISTS with plain-equality correlations over a 48h recent CTE -- Snowflake rejected V168's "
           "OR-correlated subquery, V173)\n")
_MARK_D = ("-- >>> derived:SP_ALERT_SCAN_DAILY  (from V169; arm [24] COST_IDLE_OPPORTUNITY divisions NULLIF-guarded -- "
           "prod Division by zero, V173)\n")

# ---------------------------------------------------------------------------------------------------------------
# Test-side copies of every declared delta (independent of outputs/gen_v173.py): (base text, V173 text, arm slice)
# ---------------------------------------------------------------------------------------------------------------
_H_DELTAS: list[tuple[str, str, tuple[str, str]]] = [
    ("        WITH cfg AS (\n            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED\n        )\n",
     "        WITH cfg AS (\n            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED\n        ),\n"
     "        recent AS (\n"
     "            -- V173: this rule's events raised in the last 48h, the date-stripped head precomputed. It\n"
     "            -- carries the RULE_ID = b.RULE_ID (every b row is this rule) and 48h legs of the V168 guard,\n"
     "            -- uncorrelated; a NULL key can never satisfy the two guards that read it.\n"
     "            SELECT DEDUPE_KEY,\n"
     "                   LENGTH(DEDUPE_KEY) AS KEY_LEN,\n"
     "                   LEFT(DEDUPE_KEY, LENGTH(DEDUPE_KEY) - 10) AS KEY_HEAD\n"
     "            FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS\n"
     "            WHERE RULE_ID = 'SEC_NEW_ADMIN_NETWORK'\n"
     "              AND RAISED_AT >= DATEADD('hour', -48, CURRENT_TIMESTAMP())\n"
     "              AND DEDUPE_KEY IS NOT NULL\n"
     "        )\n", _A18),
    ("        WHERE NOT EXISTS (\n"
     "            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e\n"
     "            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY\n"
     "               OR (e.RULE_ID = b.RULE_ID\n"
     "                   AND e.RAISED_AT >= DATEADD('hour', -48, CURRENT_TIMESTAMP())\n"
     "                   AND (e.DEDUPE_KEY = REPLACE(LEFT(b.DEDUPE_KEY, LENGTH(b.DEDUPE_KEY) - 11), '|FAILED', '')\n"
     "                        OR (LENGTH(e.DEDUPE_KEY) = LENGTH(b.DEDUPE_KEY)\n"
     "                            AND LEFT(e.DEDUPE_KEY, LENGTH(e.DEDUPE_KEY) - 10)\n"
     "                                = LEFT(b.DEDUPE_KEY, LENGTH(b.DEDUPE_KEY) - 10))))\n"
     "        );\n",
     "        -- V173: the V168 guard as three AND-ed NOT EXISTS, each correlated by plain equalities only. V168 had one\n"
     "        -- NOT EXISTS whose every outer reference sat under an OR, which Snowflake cannot decorrelate ('Unsupported\n"
     "        -- subquery type cannot be evaluated', every hourly run from 2026-10-02 07:08). NOT EXISTS (A OR B OR C) =\n"
     "        -- NOT EXISTS (A) AND NOT EXISTS (B) AND NOT EXISTS (C): the same rows are kept.\n"
     "        WHERE NOT EXISTS (   -- (1) the exact key (user|IP[|FAILED]|first-seen day), any age: R2-036\n"
     "            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e\n"
     "            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY\n"
     "        )\n"
     "          AND NOT EXISTS (   -- (2) this pair's V162 undated key (date and '|FAILED' stripped), last 48h\n"
     "            SELECT 1 FROM recent r\n"
     "            WHERE r.DEDUPE_KEY = REPLACE(LEFT(b.DEDUPE_KEY, LENGTH(b.DEDUPE_KEY) - 11), '|FAILED', '')\n"
     "        )\n"
     "          AND NOT EXISTS (   -- (3) the same exact base and outcome on another first-seen day, last 48h: "
     "R2-039\n"
     "            SELECT 1 FROM recent r\n"
     "            WHERE r.KEY_LEN = LENGTH(b.DEDUPE_KEY)\n"
     "              AND r.KEY_HEAD = LEFT(b.DEDUPE_KEY, LENGTH(b.DEDUPE_KEY) - 10)\n"
     "        );\n", _A18),
]
_D_DELTAS: list[tuple[str, str, tuple[str, str]]] = [
    ("like insights.show_auto_suspend and the mart loader.\n    BEGIN\n",
     "like insights.show_auto_suspend and the mart loader.\n"
     "    --      V173: both divisions guard their own divisor (NULLIF). Snowflake may compute a projection\n"
     "    --      before the HAVING / WHERE that drops a zero ('Division by zero', 2026-10-01 06:49, a\n"
     "    --      zero-credit warehouse); a NULL IDLE_PCT fails 'IDLE_PCT >= 20', so the events are the same.\n"
     "    BEGIN\n", _A24),
    ("ROUND(i.IDLE_CREDITS / i.TOTAL_CREDITS * 100, 1) AS IDLE_PCT,",
     "ROUND(i.IDLE_CREDITS / NULLIF(i.TOTAL_CREDITS, 0) * 100, 1) AS IDLE_PCT,", _A24),
    ("ROUND(s.RECOVERABLE_CREDITS * :credit_price / s.COVERED_DAYS * 30, 2) AS MONTHLY_USD,",
     "ROUND(s.RECOVERABLE_CREDITS * :credit_price / NULLIF(s.COVERED_DAYS, 0) * 30, 2) AS MONTHLY_USD,", _A24),
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
    env = {k: v for k, v in os.environ.items() if k not in ("V173_OUT", "PREFLIGHT_OUT", "PART_B_OUT", "REPAIR_OUT")}
    env.update(extra)
    return env


def _run_gen(tmp_path: Path, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "outputs" / "gen_v173.py")], env=_gen_env(**extra),
                          cwd=tmp_path, capture_output=True, text=True)


def _extras(tmp_path: Path) -> dict[str, str]:
    pf, pb = tmp_path / "PF.sql", tmp_path / "PB.sql"
    result = _run_gen(tmp_path, V173_OUT=str(tmp_path / "m.sql"), PREFLIGHT_OUT=str(pf), PART_B_OUT=str(pb))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "m.sql").read_text(encoding="utf-8") == _MIG       # the extras never change the migration
    return {"preflight": pf.read_text(encoding="utf-8"), "part_b": pb.read_text(encoding="utf-8")}


# -- generation ----------------------------------------------------------------------------------------------
def test_v173_regenerates_byte_identical_and_writes_nothing_else(tmp_path):
    out = tmp_path / "regen.sql"
    result = _run_gen(tmp_path, V173_OUT=str(out))
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == (ROOT / "snowflake" / "migrations" / _NAME).read_bytes(), (
        "V173 drifted from its forward-generation -- edit outputs/gen_v173.py, not the .sql.")
    assert b"\r\n" not in out.read_bytes()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["regen.sql"]
    assert "PREFLIGHT" not in result.stdout and "PART B" not in result.stdout


def test_v173_generator_reads_only_its_bases():
    gen = read("outputs/gen_v173.py")
    assert re.findall(r'MIG / "(V\d+__\w+\.sql)"', gen) == ["V168__alert_scan_hourly_keys_and_sweeps.sql",
                                                             "V169__alert_scan_daily_windows_and_keys.sql"]
    assert "import app" not in gen and "from app" not in gen
    assert gen.count(".read_text(") == 2


@pytest.mark.parametrize("which", ["preflight", "part_b"])
def test_v173_preflight_and_part_b_are_read_only_and_parse(tmp_path, which):
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
    assert len(parsed) == {"preflight": 5, "part_b": 4}[which]
    writes = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Command)
    for tree in parsed:
        assert tree.key == "select" or isinstance(tree, exp.Union), tree.key
        assert not [type(n).__name__ for n in tree.walk() if isinstance(n, writes)]


def test_v173_preflight_carries_the_arms_own_text(tmp_path):
    pre = _extras(tmp_path)["preflight"]
    for grid in ("-- P173.1 ", "-- P173.2 ", "-- P173.3 ", "-- P173.4 ", "-- P173.5 "):
        assert pre.count(grid) == 1, grid

    def dedent(arm: str) -> str:
        stmt = arm[arm.index("        WITH cfg AS (\n"):arm.index(";\n    EXCEPTION")]
        return "".join(ln[8:] + "\n" for ln in stmt.splitlines()).rstrip("\n")

    assert dedent(_between(_H, *_A18)) in pre
    price = ("(SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68) "
             "FROM DBA_MAINT_DB.OVERWATCH.SETTINGS)")
    assert dedent(_between(_D, *_A24)).replace(":credit_price", price) in pre
    # P173.1 reads the evidence since V168's apply: the two failing arms and the three possible-risk watch items
    p1 = _between(pre, "-- P173.1 ", "-- P173.2 ")
    for frag in ("'rule SEC_NEW_ADMIN_NETWORK %'", "'rule COST_IDLE_OPPORTUNITY %'", "'rule SEC_CRED_EXPIRY %'",
                 "'supersede_sweep_failed'", "'ref_gap_scan_failed'", "'ref_gap_check_failed'",
                 "WHERE VERSION = 168"):
        assert frag in p1, frag
    # P173.3 starts 24h before V168's apply: V162's last good run (06:07) could not see a pair LOGIN_HISTORY had not
    # landed yet (up to 2h), so the pairs first seen just before the apply are the first to age out unraised. Its
    # window column is "now", the PREFLIGHT's clock; the exact list is PART B V173.4, after the first good scan.
    p3 = _between(pre, "-- P173.3 ", "-- P173.4 ")
    assert ("HAVING MIN(L.EVENT_TIMESTAMP) >= DATEADD('hour', -24, (SELECT MAX(APPLIED_AT) FROM "
            "DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 168))") in p3
    assert "IN_ARM_WINDOW_NOW" in p3 and "RAISED_BY_NEXT_SCAN" not in pre and "PART B V173.4" in p3


def test_v173_part_b_fragments_are_v173_only(tmp_path):
    part_b = _extras(tmp_path)["part_b"]
    procs = {"SP_ALERT_SCAN": (_H, _H168), "SP_ALERT_SCAN_DAILY": (_D, _D169)}
    has = re.findall(r"'V173\.1 (\w+) DDL has: ([^']*)'", part_b)
    lacks = re.findall(r"'V173\.1 (\w+) DDL lacks: ([^']*)'", part_b)
    assert {p for p, _ in has} == set(procs) == {p for p, _ in lacks}
    for proc, frag in has:
        assert frag in procs[proc][0], (proc, frag)
    for proc, frag in lacks:
        assert frag not in procs[proc][0] and frag in procs[proc][1], (proc, frag)
    # each proc has at least one fragment the base lacks (the V173 text) and one that names the carried base label
    assert {p for p, f in has if f not in procs[p][1]} == set(procs)
    for check in ("'V173.1 SCHEMA_VERSION has 173'", "'alert scan 14/14 rule blocks ok'",
                  "'alert scan daily 14/14 rule blocks ok (daily)'", "CONTEXT LIKE 'rule SEC_NEW_ADMIN_NETWORK %'",
                  "CONTEXT LIKE 'rule COST_IDLE_OPPORTUNITY %'", "WHERE VERSION = 173"):
        assert check in part_b, check
    assert part_b.count("'V173.2 ") == 5 and part_b.count("'V173.3 ") == 4
    # the V168 supersede sweep's OR shape (tests/test_snowflake_supported_subqueries.py _PROVEN, since V168) logs
    # supersede_sweep_failed, which the 14/14 heartbeat does not count: V173.2 reads it
    v2 = _between(part_b, "-- V173.2 ", "-- V173.3 ")
    assert "'V173.2 no supersede_sweep_failed since the apply'" in v2 and "ERROR_TYPE = 'supersede_sweep_failed'" in v2
    # a scan already running at the apply finishes on the OLD body (CREATE OR REPLACE PROCEDURE leaves a CALL in
    # flight alone): V173.2 / V173.3 read only a heartbeat 55+ minutes after the apply and failures logged from
    # 30 minutes after it -- never the bare APPLIED_AT
    a173 = "(SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 173)"
    for grid in (v2, _between(part_b, "-- V173.3 ", "-- V173.4 ")):
        assert grid.count(f"MAX(LAST_LOAD_TS) >= DATEADD('minute', 55, {a173})") == 2, grid
        assert grid.count(f"LOGGED_AT >= DATEADD('minute', 30, {a173})") == grid.count("LOGGED_AT >= ") >= 2
        assert not re.search(r"(LAST_LOAD_TS\)|LOGGED_AT) >= \(SELECT MAX\(APPLIED_AT\)", grid)
    # V173.4: the exact never-raised list, once V173.2 reads OK -- the arm's own pairs (threshold, enabled rule)
    # first seen from 24h before V168's apply, no event, now past the 24h window
    v4 = part_b[part_b.index("-- V173.4 "):]
    for frag in ("DATEADD('hour', -24, (SELECT MAX(APPLIED_AT) FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION "
                 "WHERE VERSION = 168))", "nn.LOGINS >= c.THRESHOLD_NUM", "AND c.ENABLED",
                 "WHERE ev.USER_PART IS NULL", "nn.FIRST_SEEN < DATEADD('hour', -24, CURRENT_TIMESTAMP())"):
        assert frag in v4, frag


# -- guard, order, shape -------------------------------------------------------------------------------------
def test_v173_first_line_guard_and_version():
    assert _MIG.startswith(f"-- {_NAME}\n")
    guard172 = _between(read("snowflake/migrations/V172__detection_scans_company_and_accuracy.sql"),
                        "EXECUTE IMMEDIATE\n$$\n", "$$;\n")
    want = (guard172.replace("-20172", "-20173").replace("'V172 requires V171 first", "'V173 requires V172 first")
            .replace("IF (v < 171)", "IF (v < 172)"))
    assert _between(_MIG, "EXECUTE IMMEDIATE\n$$\n", "$$;\n") == want
    assert "not_ready EXCEPTION (-20173, 'V173 requires V172 first - apply migrations in order.');" in _MIG
    assert _MIG.count("EXECUTE IMMEDIATE") == 1 and "SELECT 173 AS VERSION" in _MIG
    assert _MIG.rstrip().endswith("WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION "
                                  "WHERE VERSION = 173);")
    header = _MIG[:_MIG.index("EXECUTE IMMEDIATE")]
    for word in ("WHY:", "COST:", "LATENCY:", "FIRST RUN:", "ROLLBACK:",
                 "Apply AFTER V172 (alone, any time; no repairs). Idempotent; safe to re-run."):
        assert word in header, word
    flat = " ".join(ln.lstrip("- ") for ln in header.splitlines())
    assert "24h before V168's apply" in flat and "PART B V173.4" in flat and "since V168's apply" not in flat


def test_v173_file_order_and_nothing_runs_at_apply():
    from tests.test_migrations_parse import _plain_statements
    order = [_MIG.index("EXCEPTION (-20173"), _MIG.index(_MARK_H),
             _MIG.index("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()"), _MIG.index(_MARK_D),
             _MIG.index("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()"),
             _MIG.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION")]
    assert order == sorted(order)
    for mark, name in ((_MARK_H, "SP_ALERT_SCAN()"), (_MARK_D, "SP_ALERT_SCAN_DAILY()")):
        assert _MIG[_MIG.index(mark) + len(mark):].startswith(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    kinds = [re.sub(r"^(?:--[^\n]*\n)+", "", s.strip()).split(None, 2)[:2] for s in _plain_statements(_MIG)]
    assert kinds == [["INSERT", "INTO"]], kinds
    assert _MIG.count("CREATE OR REPLACE PROCEDURE") == 2 and _MIG.count("$$") == 6
    top = _strip_noise("".join(p for i, p in enumerate(_MIG.split("$$")) if i % 2 == 0))
    for banned in ("CALL", "ALTER", "DROP", "TASK", "GRANT", "REVOKE", "TRUNCATE", "DELETE", "MERGE", "UPDATE",
                   "VIEW", "FUNCTION", "TABLE"):
        assert not re.search(rf"\b{banned}\b", top), banned
    assert len(re.findall(r"\bCREATE\b", top)) == 2 and "ALERT_EVENTS" not in top
    assert not re.search(r"\$[A-Za-z_][A-Za-z0-9_]*\$", _MIG) and "\r" not in _MIG


def test_v173_description_fits_and_has_no_apostrophe():
    desc = re.search(r"SELECT 173 AS VERSION,\n       '(.*)' AS DESCRIPTION\n", _MIG, re.S).group(1)
    assert "\n" not in desc and "'" not in desc and "$" not in desc and len(desc) <= 4000
    for item in ("re-derived from V168", "re-derived from V169", "SEC_NEW_ADMIN_NETWORK", "COST_IDLE_OPPORTUNITY",
                 "NULLIF", "No task change"):
        assert item in desc, item


def test_v173_in_expected_migrations():
    """Admin lists V173 with house-rule text (no $, no hand CALL, no trailing '.')."""
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    text = str(_EXPECTED_MIGRATIONS[173])
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_" not in text and "$" not in text and not text.endswith(".")
    for phrase in ("SEC_NEW_ADMIN_NETWORK", "COST_IDLE_OPPORTUNITY", "Division by zero", "re-derived from V168",
                   "re-derived from V169", "No task change, no apply-time run"):
        assert phrase in text, phrase


def test_v173_run_docs_list_it_with_a_short_apply_note():
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert f"snowflake/migrations/{_NAME}" in read(rel), rel
    dep = read("DEPLOYMENT.md")
    block = dep[dep.index("> **V173 (hotfix"):]
    block = block[:block.index("\n\n")]
    note = " ".join(" ".join(ln.lstrip("> ") for ln in block.splitlines()).split())
    for phrase in ("V173 alone, any time", "no repairs", "PREFLIGHT P173.3", "PART B V173.1", "V173.2", "V173.3",
                   "V173.4", "24h before V168's apply", "started after the apply"):
        assert phrase in note, phrase


# -- lineage + round 13 ------------------------------------------------------------------------------------
def _assert_v173_lineage(texts: dict[int, str]) -> None:
    """Bounded at 173: a later re-derivation of either scan must not turn it red."""
    from tests.test_proc_lineage import _HISTORICAL_WAIVERS, _definers, _lineage, _violations
    rows = {(r["v"], r["proc"]): r for r in _lineage(texts)}
    defs = _definers(texts)
    for name, base in (("SP_ALERT_SCAN", 168), ("SP_ALERT_SCAN_DAILY", 169)):
        r = rows[(173, name)]
        assert r["src"] == "marker" and r["claims"] == [base] and r["prev"] == base and not r["waived"], r
        assert [v for v in defs[name] if base <= v <= 173] == [base, 173], name
    assert not [v for v in _violations(texts, _HISTORICAL_WAIVERS) if v.startswith("V173 ")]


def test_v173_markers_name_the_current_definers():
    from tests.test_proc_lineage import _migrations
    _assert_v173_lineage(_migrations())
    assert "LINEAGE-WAIVER" not in _MIG and _MIG.count("-- >>> derived:") == 2


def test_v173_lineage_lock_survives_a_later_re_derivation():
    from tests.test_proc_lineage import _migrations
    texts = _migrations()
    assert max(texts) < 999
    texts[999] = "".join(f"-- >>> derived:{n}  (from V173)\nCREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{n}()\n"
                         "RETURNS VARCHAR LANGUAGE SQL AS\n$$\nBEGIN\n    RETURN 'x';\nEND;\n$$;\n"
                         for n in ("SP_ALERT_SCAN", "SP_ALERT_SCAN_DAILY"))
    _assert_v173_lineage(texts)


def test_v173_hourly_normalizes_back_to_v168_byte_for_byte():
    assert _H != _H168 and _reverse(_H, _H_DELTAS) == _H168


def test_v173_daily_normalizes_back_to_v169_byte_for_byte():
    assert _D != _D169 and _reverse(_D, _D_DELTAS) == _D169


@pytest.mark.parametrize(("which", "victim"), [
    ("H", "'rule SEC_NEW_ADMIN_NETWORK - other rules unaffected'"), ("H", "nn.LOGINS >= c.THRESHOLD_NUM"),
    ("H", "AND hi.RAISED_AT <= DATEADD('hour', 48, lo.RAISED_AT)"), ("H", "alert scan v14 (V168:"),
    ("D", "HAVING SUM(CREDITS_TOTAL) > 0"), ("D", "AND s.IDLE_PCT >= 20 AND s.IDLE_CREDITS >= 1"),
    ("D", "alert scan daily v6 (V169:"), ("D", "MTD_COMPLETE_USD"),
])
def test_v173_normalize_check_has_teeth(which, victim):
    """Deleting one character anywhere outside the declared deltas breaks the byte compare."""
    body, deltas, base = (_H, _H_DELTAS, _H168) if which == "H" else (_D, _D_DELTAS, _D169)
    assert body.count(victim) >= 1
    assert _reverse(body.replace(victim, victim[:-1], 1), deltas) != base


@pytest.mark.parametrize(("which", "i"), [("H", 0), ("H", 1), ("D", 0), ("D", 1), ("D", 2)])
def test_v173_every_declared_delta_is_real(which, i):
    """Skipping any one delta's reversal leaves a body that is NOT the base's."""
    body, deltas, base = (_H, _H_DELTAS, _H168) if which == "H" else (_D, _D_DELTAS, _D169)
    assert _reverse(body, [d for k, d in enumerate(deltas) if k != i]) != base


# -- the deltas ----------------------------------------------------------------------------------------------
def test_v173_arm18_guard_is_three_and_ed_not_exists_with_plain_keys():
    a = _between(_H, *_A18)
    code = _strip_noise(a)
    guard = code[code.index("WHERE NOT EXISTS ("):]
    assert guard.count("NOT EXISTS (") == 3 and " OR " not in guard and "LIKE" not in guard
    assert code.count("FROM recent r") == 2 and "recent AS (" in code
    # the candidates, key, title and the 90d / 24h / three-role windows are V168's: only the guard moved
    cand = ("        SELECT b.RULE_ID", "        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)\n")
    assert _between(a, *cand) == _between(_between(_H168, *_A18), *cand)
    # every other arm, sweep and gate is untouched
    assert _H.replace(a, "") == _H168.replace(_between(_H168, *_A18), "")


def test_v173_arm24_divides_by_nullif_only():
    from tests.test_sql_division_guards import unguarded
    a = _between(_D, *_A24)
    assert unguarded(a) == [] and unguarded(_between(_D169, *_A24)) == ["i.TOTAL_CREDITS", "s.COVERED_DAYS"]
    assert "HAVING SUM(CREDITS_TOTAL) > 0" in a and "WHERE s.COVERED_DAYS >= 7" in a      # the filters stay
    assert _D.replace(a, "") == _D169.replace(_between(_D169, *_A24), "")


def test_v173_both_procs_pass_the_two_new_locks():
    from tests.test_snowflake_supported_subqueries import violations as subquery_violations
    from tests.test_sql_division_guards import violations as division_violations
    assert not subquery_violations({"SP_ALERT_SCAN()": _H, "SP_ALERT_SCAN_DAILY()": _D})
    assert subquery_violations({"SP_ALERT_SCAN()": _H168})                       # V168: arm [18] fails R1
    assert not division_violations({"SP_ALERT_SCAN()": _H, "SP_ALERT_SCAN_DAILY()": _D})
    assert division_violations({"SP_ALERT_SCAN_DAILY()": _D169})


def test_v173_tallies_footprints_and_labels_unchanged():
    from tests.test_alert_rule_consistency import _FAILS_INC_RE, _SCAN_DENOMINATORS
    for name, new, old in (("SP_ALERT_SCAN", _H, _H168), ("SP_ALERT_SCAN_DAILY", _D, _D169)):
        assert len(_FAILS_INC_RE.findall(new)) == len(_FAILS_INC_RE.findall(old)) == 14, name
        self_alert_re, return_re = _SCAN_DENOMINATORS[name]
        assert re.findall(self_alert_re, new) == re.findall(self_alert_re, old), name
        assert re.findall(return_re, new) == re.findall(return_re, old), name
        assert set(re.findall(r"SNOWFLAKE\.ACCOUNT_USAGE\.(\w+)", _body(new))) == set(
            re.findall(r"SNOWFLAKE\.ACCOUNT_USAGE\.(\w+)", _body(old))), name
        assert re.findall(r"RETURN '[^']*'", new) == re.findall(r"RETURN '[^']*'", old), name
        assert "$$" not in _body(new) and "\\" not in _body(new)


def test_v173_changed_arm_statements_parse():
    sqlglot = pytest.importorskip("sqlglot")
    from sqlglot import exp
    for body, span in ((_H, _A18), (_D, _A24)):
        arm = _between(body, *span)
        stmt = arm[arm.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS"):arm.index(";\n    EXCEPTION")]
        (parsed,) = sqlglot.parse(stmt.replace(":credit_price", "3.68"), dialect="snowflake")
        assert [c.name for c in parsed.this.expressions] == ["RULE_ID", "COMPANY", "SEVERITY", "TITLE", "DETAIL",
                                                             "METRIC_VALUE", "DEDUPE_KEY"]
        (b,) = [s for s in parsed.find_all(exp.Subquery) if s.alias == "b"]
        assert len(b.this.selects) == 7
    arm18 = _between(_H, *_A18)
    (parsed,) = sqlglot.parse(arm18[arm18.index("INSERT INTO"):arm18.index(";\n    EXCEPTION")], dialect="snowflake")
    assert [c.alias for c in parsed.find_all(exp.CTE)] == ["cfg", "recent"]
    assert len(list(parsed.find_all(exp.Exists))) == 3
