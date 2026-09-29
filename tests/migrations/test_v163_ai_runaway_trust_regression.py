"""Locks for V163 -- COST_AI_USER_RUNAWAY (#37a), SEC_TRUST_REGRESSION (#44b) and the nightly SEC_FAILED_LOGINS
wording (#39), all in one SP_ALERT_SCAN_DAILY re-derivation from V160 (Next Fifty wave 4, app v4.602.0).

STRUCTURE + GENERATION + LOCKS; tests/migrations/test_v163_harness.py EXECUTES the arms. What this file proves:
  * generation -- outputs/gen_v163.py regenerates the migration byte-for-byte; the read-only PREFLIGHT and PART B
    are written only on request, parse, write nothing, and the PREFLIGHT carries the arms' CTE text verbatim;
  * shape -- first line, guard (-20163, v < 162), the WHEN NOT MATCHED seeds (2 rules, 2 settings), the marker +
    SP_ALERT_SCAN_DAILY, the version row; nothing runs at apply time;
  * lineage + round 13 -- re-derived from V160 (its immediately previous definer); cutting arms [28]-[29] and
    reversing the declared literal deltas gives V160's body back byte-for-byte;
  * the arms -- placement, handlers, the 14-block tally, the owner-decision literals, the keys, sqlglot parses;
  * the v4.602.0 app lockstep of this slice (playbooks, navigation, evidence, settings + editors, RUNBOOK rows).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from tests._source import ROOT, migration_tip, page_source, read
from tests.test_migration_proc_syntax import _strip_noise

_NAME = "V163__ai_runaway_trust_regression.sql"
_MIG = read(f"snowflake/migrations/{_NAME}")
_V160 = read("snowflake/migrations/V160__sleep_polling_alert.sql")
_AI, _TRUST = "COST_AI_USER_RUNAWAY", "SEC_TRUST_REGRESSION"


def _proc(text: str, name: str) -> str:
    s = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    o = text.index("$$", s)
    return text[s:text.index("$$;", o + 2) + 3]


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


def _norm(text: str) -> str:
    return " ".join(text.split())


_D = _proc(_MIG, "SP_ALERT_SCAN_DAILY()")
_D160 = _proc(_V160, "SP_ALERT_SCAN_DAILY()")
_BODY = _D[_D.index("$$") + 2:_D.rindex("$$")]

# ---------------------------------------------------------------------------------------------------
# Test-side copies (independent of outputs/gen_v163.py).
# ---------------------------------------------------------------------------------------------------
_MARKER = ("-- >>> derived:SP_ALERT_SCAN_DAILY  (from V160; + [28] COST_AI_USER_RUNAWAY + [29] SEC_TRUST_REGRESSION "
           "counting arms, [07] burst-vs-lockout wording, tally 12 -> 14, V163)\n")
_ANCHOR_07 = "    -- [07] SEC_FAILED_LOGINS"
_ANCHOR_08 = "    -- [08] COST_BUDGET_PACE"
_ANCHOR_22 = "    -- [22] OPS_PIPELINE_DEGRADED"
_ANCHOR_24 = "    -- [24] COST_IDLE_OPPORTUNITY"
_ANCHOR_25 = "    -- [25] COST_SLEEP_POLLING"
_ANCHOR_28 = "    -- [28] COST_AI_USER_RUNAWAY"
_ANCHOR_29 = "    -- [29] SEC_TRUST_REGRESSION"
_ANCHOR_17 = "    -- [17] PIPE_REF_GAP"
_OLD07_T = "               lg.USER_NAME || ' had ' || lg.FAILED_LOGINS || ' failed logins on ' || lg.DAY,\n"
_OLD07_D = "               'Investigate credential stuffing / lockouts.',\n"
_NEW07_T = (
    "               lg.USER_NAME || ' had ' || lg.FAILED_LOGINS || ' failed logins on ' || lg.DAY\n"
    "                   || IFF(COALESCE(lg.LOGINS, 0) - lg.FAILED_LOGINS > 0,\n"
    "                          ', ' || (lg.LOGINS - lg.FAILED_LOGINS) || ' successful'\n"
    "                              || IFF(lg.DAY >= CURRENT_DATE(), ' so far', ''),\n"
    "                          IFF(lg.DAY >= CURRENT_DATE(), ' and no successful login so far today',\n"
    "                              ' and no successful login')),\n")
_NEW07_D = (
    "               IFF(lg.DAY >= CURRENT_DATE(),\n"
    "                   'Partial day: today counts only what the ~06:45 Central daily load saw (LOGIN_HISTORY lags up '\n"
    "                   || 'to 2 h), and this event is not updated when the rest of the day loads. ',\n"
    "                   '')\n"
    "               || IFF(COALESCE(lg.LOGINS, 0) - lg.FAILED_LOGINS > 0,\n"
    "                   'The same day also had successful logins. While the hourly SEC_LOGIN_TAKEOVER rule is enabled '\n"
    "                   || '(Alerts > Rules), a failed burst followed within 60 minutes by a success raises it (CRITICAL '\n"
    "                   || 'off-hours or for an admin role); either way, check Security > Access > Authentication > '\n"
    "                   || 'Account-takeover candidates. ',\n"
    "                   'No successful login ' || IFF(lg.DAY >= CURRENT_DATE(), 'so far today', 'that day')\n"
    "                   || ': most likely a lockout or a job still sending an old secret '\n"
    "                   || '(a guessing attempt that never got in looks the same). ')\n"
    "                   || 'Review Security > Access > Authentication: failed-login reasons and client IPs.',\n")
_SELF_12 = "' of 12 daily alert rule block(s) failed this run'"
_SELF_14 = "' of 14 daily alert rule block(s) failed this run'"
_RET_160 = "'alert scan daily v4 (V160: + COST_SLEEP_POLLING weekly sleep-polling push): '"
_RET_163 = ("'alert scan daily v5 (V163: + COST_AI_USER_RUNAWAY + SEC_TRUST_REGRESSION, [07] burst-vs-lockout "
            "wording): '")
_FAILS_INC = "fails := fails + 1"

_SEED_AI = ("        ('COST_AI_USER_RUNAWAY', 'COST', 'Per-user AI runaway: one user''s AI credits on a day above "
            "THRESHOLD_NUM x the daily cap (COCO_DAILY_CAP_CREDITS) and a robust-z outlier against their own prior "
            "90 days', TRUE, 'HIGH', 2, 24),\n")
_SEED_TRUST = ("        ('SEC_TRUST_REGRESSION', 'SECURITY', 'Trust Center regression: a CRITICAL or HIGH scanner''s "
               "at-risk entity count rose by at least THRESHOLD_NUM since its previous snapshot day', TRUE, 'HIGH', "
               "1, 24)\n")
_SEED_COLS = "(RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)"
_SETTINGS_ROWS = ("        ('AI_RUNAWAY_ROBUST_Z', '3.5'),\n"
                  "        ('AI_RUNAWAY_INCLUDE_FUNCTIONS', 'FALSE')\n")

_ARM28 = _between(_D, _ANCHOR_28, _ANCHOR_29)
_ARM29 = _between(_D, _ANCHOR_29, _ANCHOR_17)
_ARM07 = _between(_D, _ANCHOR_07, _ANCHOR_08)


def _insert(arm: str) -> str:
    return arm[arm.index("        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS"):arm.index(";\n    EXCEPTION")]


def _code(sql: str) -> str:
    """-- comments out, single-quoted literals ('' escapes) KEPT."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
        elif sql[i] == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if sql[j + 1:j + 2] == "'":
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


def _cte(stmt: str, name: str) -> str:
    """``<name> AS ( ... )`` of a WITH list, through its matching close paren (string-aware)."""
    i = stmt.index(f"{name} AS (")
    j, depth, in_str = stmt.index("(", i), 0, False
    while True:
        ch = stmt[j]
        if in_str:
            if ch == "'":
                if stmt[j + 1:j + 2] == "'":
                    j += 1
                else:
                    in_str = False
        elif ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return stmt[i:j + 1]
        j += 1


def _gen_env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("V163_OUT", "PREFLIGHT_OUT", "PART_B_OUT")}
    env.update(extra)
    return env


def _run_gen(tmp_path: Path, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "outputs" / "gen_v163.py")], env=_gen_env(**extra),
                          cwd=tmp_path, capture_output=True, text=True)


def _extras(tmp_path: Path) -> tuple[str, str]:
    pre, part_b = tmp_path / "PREFLIGHT_V163.sql", tmp_path / "PART_B_V163.sql"
    result = _run_gen(tmp_path, V163_OUT=str(tmp_path / "m.sql"), PREFLIGHT_OUT=str(pre), PART_B_OUT=str(part_b))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "m.sql").read_text(encoding="utf-8") == _MIG      # the extras never change the migration
    return pre.read_text(encoding="utf-8"), part_b.read_text(encoding="utf-8")


# -- generation ----------------------------------------------------------------------------------------------

def test_v163_regenerates_byte_identical_and_writes_nothing_else(tmp_path):
    out = tmp_path / "regen.sql"
    result = _run_gen(tmp_path, V163_OUT=str(out))
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == (ROOT / "snowflake" / "migrations" / _NAME).read_bytes(), (
        "V163 drifted from its forward-generation -- edit outputs/gen_v163.py, not the .sql.")
    assert b"\r\n" not in out.read_bytes()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["regen.sql"]     # PREFLIGHT / PART B only on request
    assert "PREFLIGHT" not in result.stdout and "PART B" not in result.stdout


def test_v163_generator_reads_only_its_base():
    gen = read("outputs/gen_v163.py")
    assert re.findall(r'MIG / "(V\d+__\w+\.sql)"', gen) == ["V160__sleep_polling_alert.sql"]
    assert "import app" not in gen and "from app" not in gen


@pytest.mark.parametrize("which", ["preflight", "part_b"])
def test_v163_preflight_and_part_b_are_read_only_and_parse(tmp_path, which):
    pre, part_b = _extras(tmp_path)
    sql = pre if which == "preflight" else part_b
    code = _strip_noise(sql)                       # comments + strings out
    for banned in ("INSERT", "UPDATE", "DELETE", "MERGE", "CALL", "CREATE", "ALTER", "DROP", "TRUNCATE",
                   "GRANT", "REVOKE", "EXECUTE"):
        assert not re.search(rf"\b{banned}\b", code, re.I), (which, banned)
    assert not re.search(r"(?<![:\w]):[A-Za-z_]\w*", code), "a scripting :bind survived"
    assert "$$" not in sql and "@" not in sql      # no scripting block, no address of any kind
    sqlglot = pytest.importorskip("sqlglot")
    from sqlglot import exp
    parsed = [p for p in sqlglot.parse(sql, dialect="snowflake") if p is not None]
    assert len(parsed) == (3 if which == "preflight" else 2)
    writes = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Command)
    for tree in parsed:
        assert tree.key == "select" or isinstance(tree, exp.Union), tree.key
        assert not [type(n).__name__ for n in tree.walk() if isinstance(n, writes)]


def test_v163_preflight_carries_the_arm_text_verbatim(tmp_path):
    """What the owner previews is what the scan raises: every CTE the arms share with the PREFLIGHT is the arm's
    text, only the named read windows moved (the replay covers every complete day of the last 90)."""
    pre, _ = _extras(tmp_path)
    s28, s29 = _insert(_ARM28), _insert(_ARM29)
    for name in ("clk", "knob", "hist", "med", "disp", "scored"):
        assert _cte(s28, name) in pre, name
    ud = _cte(s28, "ud")
    assert ud.count("DATEADD('day', -93, k.TODAY)") == 1
    assert ud.replace("-93", "-183") in pre and ud not in pre
    snap = _cte(s29, "snap")
    assert snap.count("DATEADD('day', -30, k.TODAY)") == 1
    assert snap.replace("-30", "-60") in pre and snap not in pre
    # the raise rules are the arms' own predicates
    assert "COUNT_IF(s.CR > n.CAP_CR * COALESCE(c.THRESHOLD_NUM, 2) AND (s.Z IS NULL OR s.Z >= n.Z_MIN)) AS WOULD_RAISE" in pre
    assert "u.CR > n.CAP_CR * COALESCE(c.THRESHOLD_NUM, 2)" in s28 and "WHERE s.Z IS NULL OR s.Z >= n.Z_MIN" in s28
    assert ("(r.PRIOR_N IS NOT NULL AND r.CUR_N > 0 AND r.CUR_N - r.PRIOR_N >= COALESCE(t.THRESHOLD_NUM, 1) AND "
            "r.SEV IN ('CRITICAL', 'HIGH')) AS WOULD_RAISE") in pre
    for term in ("s.PRIOR_N IS NOT NULL", "s.CUR_N > 0", "s.CUR_N - s.PRIOR_N >= COALESCE(c.THRESHOLD_NUM, 1)",
                 "s.SEV IN ('CRITICAL', 'HIGH')"):
        assert f"         AND {term}\n" in s29, term
    # the rule rows fall back to their seeds before the apply; the event company is the arm's expression
    assert "COALESCE(MAX(IFF(c.ENABLED, c.THRESHOLD_NUM, NULL)), 2) AS THRESHOLD_NUM" in pre
    assert "COALESCE(MAX(IFF(c.ENABLED, c.THRESHOLD_NUM, NULL)), 1) AS THRESHOLD_NUM" in pre
    assert "COALESCE(NULLIF(DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(a.USER_NAME), 'UNKNOWN'), 'ALL') AS EVENT_COMPANY" in pre
    assert "COALESCE(NULLIF(DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(s.USER_NAME), 'UNKNOWN'), 'ALL')," in s28
    # the scan's own AI price read
    assert "COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'AI_CREDIT_PRICE_USD', VALUE, NULL))), 2.20)" in pre
    assert "COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'AI_CREDIT_PRICE_USD', VALUE, NULL))), 2.20)" in _BODY
    for grid in ("-- P163.1 ", "-- P163.2 ", "-- P163.3 "):
        assert pre.count(grid) == 1, grid
    assert "USER_NAME IN ('ACCOUNT', 'UNKNOWN')" in pre                 # P163.2: who Functions is booked to


# -- guard, order, shape -------------------------------------------------------------------------------------

def test_v163_first_line_guard_and_version():
    assert _MIG.startswith(f"-- {_NAME}\n")
    assert "not_ready EXCEPTION (-20163, 'V163 requires V162 first - apply migrations in order.');" in _MIG
    assert "IF (v < 162) THEN" in _MIG
    assert "SELECT 163 AS VERSION" in _MIG and "WHERE VERSION = 163);" in _MIG
    guard160 = _between(_V160, "EXECUTE IMMEDIATE\n$$\n", "$$;\n")          # V160's guard, numbers moved
    want = (guard160.replace("-20160", "-20163").replace("'V160 requires V159 first", "'V163 requires V162 first")
            .replace("IF (v < 159)", "IF (v < 162)"))
    assert _between(_MIG, "EXECUTE IMMEDIATE\n$$\n", "$$;\n") == want
    assert _MIG.count("EXECUTE IMMEDIATE") == 1
    assert _MIG.rstrip().endswith("WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION "
                                  "WHERE VERSION = 163);")
    assert migration_tip() >= 163
    header = _MIG[:_MIG.index("EXECUTE IMMEDIATE")]
    for word in ("WHY:", "COST:", "LATENCY:", "FIRST RUN:", "ROLLBACK:",
                 "Apply AFTER V162 (the [07] text names the hourly SEC_LOGIN_TAKEOVER). Idempotent; safe to re-run."):
        assert word in header, word


def test_v163_file_order_and_statements():
    from tests.test_migrations_parse import _plain_statements
    guard = _MIG.index("EXCEPTION (-20163")
    rules = _MIG.index("MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t")
    settings = _MIG.index("MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS t")
    mark = _MIG.index(_MARKER)
    daily = _MIG.index("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()")
    version = _MIG.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION")
    assert guard < rules < settings < mark < daily < version
    assert _MIG[mark + len(_MARKER):].startswith(
        "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()")
    kinds = [re.sub(r"^(?:--[^\n]*\n)+", "", s.strip()).split(None, 3)[:3] for s in _plain_statements(_MIG)]
    assert kinds == [["MERGE", "INTO", "DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG"],
                     ["MERGE", "INTO", "DBA_MAINT_DB.OVERWATCH.SETTINGS"],
                     ["INSERT", "INTO", "DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION"]], kinds
    assert _MIG.count("CREATE OR REPLACE PROCEDURE") == 1
    assert _MIG.count("$$") == 4                                     # guard + one proc body
    assert "$$" not in _MIG[:_MIG.index("EXECUTE IMMEDIATE")]
    top = _strip_noise("".join(p for i, p in enumerate(_MIG.split("$$")) if i % 2 == 0))
    for banned in ("CALL", "ALTER", "DROP", "TASK", "GRANT", "REVOKE", "TRUNCATE", "UPDATE", "DELETE", "VIEW",
                   "FUNCTION", "TABLE"):
        assert not re.search(rf"\b{banned}\b", top), banned
    assert not re.search(r"\$[A-Za-z_][A-Za-z0-9_]*\$", _MIG)          # no tagged dollar quote
    assert "\r" not in _MIG


def test_v163_seeds_are_exact_and_when_not_matched_only():
    seed = _between(_MIG, "MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t", ";\n")
    assert seed.count(_SEED_AI) == 1 and seed.count(_SEED_TRUST) == 1 and seed.count("\n        ('") == 2
    assert f"    AS s{_SEED_COLS}\n" in seed
    assert f"WHEN NOT MATCHED THEN INSERT {_SEED_COLS}" in seed and "WHEN MATCHED" not in seed
    assert "FALSE" not in seed and "AUTO_CLEAR_ENABLED" not in seed          # Guard-C trap; the default stays
    for rule in (_AI, _TRUST):
        name = re.search(rf"\('{rule}', '\w+', '((?:[^']|'')*)'", seed).group(1).replace("''", "'")
        assert len(name) <= 200 and ";" not in name and "$" not in name, rule
    assert len(re.findall(r"(?:MERGE INTO|UPDATE|DELETE FROM|INSERT INTO) DBA_MAINT_DB\.OVERWATCH\.ALERT_CONFIG",
                          _MIG)) == 1
    from tests.test_alert_rule_consistency import _config_enabled, _raised_rule_ids
    assert {_AI, _TRUST} <= _config_enabled() and {_AI, _TRUST} <= _raised_rule_ids()


def test_v163_settings_seed_matches_the_app_defaults():
    from app.config import DEFAULT_SETTINGS
    seed = _between(_MIG, "MERGE INTO DBA_MAINT_DB.OVERWATCH.SETTINGS t", ";\n")
    assert _SETTINGS_ROWS in seed and seed.count("\n        ('") == 2
    assert "WHEN NOT MATCHED THEN INSERT (KEY, VALUE) VALUES (s.KEY, s.VALUE)" in seed and "WHEN MATCHED" not in seed
    assert str(DEFAULT_SETTINGS["AI_RUNAWAY_ROBUST_Z"]) == "3.5"
    assert str(DEFAULT_SETTINGS["AI_RUNAWAY_INCLUDE_FUNCTIONS"]) == "FALSE"
    for key in ("AI_RUNAWAY_ROBUST_Z", "AI_RUNAWAY_INCLUDE_FUNCTIONS"):
        assert f"('{key}', '{DEFAULT_SETTINGS[key]!s}')" in seed


def test_v163_description_fits_and_doubles_apostrophes():
    desc = re.search(r"SELECT 163 AS VERSION,\n       '(.*)' AS DESCRIPTION\n", _MIG, re.S).group(1)
    assert "\n" not in desc and "'" not in desc.replace("''", "")
    assert len(desc.replace("''", "'")) <= 4000
    assert "re-derived from V160" in desc and "Tally 12 -> 14" in desc and "WHEN NOT MATCHED only" in desc


# -- lineage + round 13 ------------------------------------------------------------------------------------

def test_v163_marker_names_the_current_definer():
    from tests.test_proc_lineage import _HISTORICAL_WAIVERS, _definers, _lineage, _migrations, _violations
    texts = _migrations()
    rows = {(r["v"], r["proc"]): r for r in _lineage(texts)}
    r = rows[(163, "SP_ALERT_SCAN_DAILY")]
    assert r["src"] == "marker" and r["claims"] == [160] and r["prev"] == 160 and not r["waived"], r
    assert [v for v in _definers(texts)["SP_ALERT_SCAN_DAILY"] if 160 < v <= 163] == [163]
    assert not [v for v in _violations(texts, _HISTORICAL_WAIVERS) if v.startswith("V163 ")]
    assert "LINEAGE-WAIVER" not in _MIG and _MIG.count("-- >>> derived:") == 1


def test_v163_daily_normalizes_back_to_v160_byte_for_byte():
    d = _D
    i, j = d.index(_ANCHOR_28), d.index(_ANCHOR_17)
    d = d[:i] + d[j:]                                                                 # D2: arms [28] + [29]
    assert d.count(_NEW07_T) == 1 and d.count(_NEW07_D) == 1
    d = d.replace(_NEW07_T, _OLD07_T).replace(_NEW07_D, _OLD07_D)                    # D1a / D1b
    assert d.count(_SELF_14) == 1
    d = d.replace(_SELF_14, _SELF_12)                                                 # D3
    assert d.count("(14 - :fails)") == 5 and d.count("'/14 rule blocks ok (daily)'") == 3
    d = d.replace("(14 - :fails)", "(12 - :fails)").replace("'/14 rule blocks ok (daily)'",
                                                             "'/12 rule blocks ok (daily)'")   # D4
    assert d.count(_RET_163) == 1
    d = d.replace(_RET_163, _RET_160)                                                 # D5
    assert d == _D160


def test_v163_normalize_check_has_teeth():
    """Deleting one character anywhere outside the declared deltas breaks the byte compare."""
    for victim in ("AS DAILY_BURN", "'rule COST_SLEEP_POLLING - other rules unaffected'", "lg.FAILED_LOGINS >= "):
        assert _D.count(victim) >= 1
        mutated = _D.replace(victim, victim[:-1], 1)
        d = mutated[:mutated.index(_ANCHOR_28)] + mutated[mutated.index(_ANCHOR_17):]
        d = d.replace(_NEW07_T, _OLD07_T).replace(_NEW07_D, _OLD07_D).replace(_SELF_14, _SELF_12)
        d = d.replace("(14 - :fails)", "(12 - :fails)").replace("'/14 rule blocks ok (daily)'",
                                                                 "'/12 rule blocks ok (daily)'").replace(_RET_163, _RET_160)
        assert d != _D160, victim


# -- the arms ------------------------------------------------------------------------------------------------

def test_v163_arms_28_and_29_are_counting_blocks_between_25_and_17():
    assert _D.index(_ANCHOR_24) < _D.index(_ANCHOR_25) < _D.index(_ANCHOR_28) < _D.index(_ANCHOR_29) \
        < _D.index(_ANCHOR_17) < _D.index("    IF (fails > 0) THEN")
    assert _D[:_D.index(_ANCHOR_28)].endswith(
        "                   'rule COST_SLEEP_POLLING - other rules unaffected', CURRENT_ROLE();\n    END;\n")
    arm24 = _between(_D, _ANCHOR_24, _ANCHOR_25)
    h24 = arm24[arm24.index("    EXCEPTION\n"):]
    for arm, rule in ((_ARM28, _AI), (_ARM29, _TRUST)):
        assert _D.count(arm) == 1
        head = arm[:arm.index("    BEGIN\n")]
        assert all(ln.startswith("    --") for ln in head.splitlines()), head       # nothing hides in the head
        assert "(V163, Next-Fifty #" in head.splitlines()[0]
        assert arm.count(_FAILS_INC) == 1 and arm.count("    BEGIN\n") == 1 and arm.count("    EXCEPTION\n") == 1
        assert arm[arm.index("    EXCEPTION\n"):] == h24.replace("COST_IDLE_OPPORTUNITY", rule)
        between = arm[arm.index("    BEGIN\n") + len("    BEGIN\n"):arm.index("        INSERT INTO")]
        assert between == ""                                                         # BEGIN, then the one INSERT
        assert arm.count("INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS") == 1


def test_v163_tallies_equal_the_counting_arms():
    assert _D.count(_FAILS_INC) == 14 and _D160.count(_FAILS_INC) == 12
    assert _SELF_14 in _D and _SELF_12 not in _D
    assert "(12 - :fails)" not in _D and "/12 rule blocks ok" not in _D and "alert scan daily v4" not in _D
    assert _D.count("               ROW_COUNT = (14 - :fails),\n") == 1              # [hb], outside the global guard
    assert _D.count("                   (14 - :fails), 1, 'alert scan daily ' || (14 - :fails) || "
                    "'/14 rule blocks ok (daily)';\n") == 1
    from tests.test_alert_rule_consistency import _FAILS_INC_RE, _SCAN_DENOMINATORS
    self_alert_re, return_re = _SCAN_DENOMINATORS["SP_ALERT_SCAN_DAILY"]
    assert len(_FAILS_INC_RE.findall(_D)) == 14
    assert {int(x) for x in re.findall(self_alert_re, _D)} == {14}
    assert re.findall(return_re, _D) == [("14", "14")] * 3                      # [hb] STATUS x2 + RETURN


def test_v163_leaves_the_other_arms_and_the_ungated_shape_alone():
    assert _between(_D, _ANCHOR_22, _ANCHOR_28) == _between(_D160, _ANCHOR_22, _ANCHOR_17)   # [22] [24] [25]
    assert _between(_D, "BEGIN\n", _ANCHOR_07) == _between(_D160, "BEGIN\n", _ANCHOR_07)     # settings read, [06]
    assert "ct_hour" not in _D and "cadence gate" not in _D                         # the daily scan stays ungated
    assert "6-core-rule scan-health tally" in _D and "AS DAILY_BURN" in _D


def test_v163_arm07_changes_only_the_title_and_detail():
    arm = _ARM07
    old = arm.replace(_NEW07_T, _OLD07_T).replace(_NEW07_D, _OLD07_D)
    assert old == _between(_D160, _ANCHOR_07, _ANCHOR_08)
    s = _insert(arm)
    assert "c.RULE_ID || '|' || lg.USER_NAME || '|' || lg.DAY\n" in s                 # key: no past user-day re-fires
    assert "AND lg.FAILED_LOGINS >= c.THRESHOLD_NUM" in s and "c.RULE_ID, lg.COMPANY, c.SEVERITY," in s
    assert "credential stuffing" not in _D
    # review W5: today's row is a partial day (the ~06:45 load) that the RULE|USER|DAY key never re-raises, so
    # its text says 'so far' and never claims 'no successful login that day'; the predicate still reads today
    shown = "".join(x.replace("''", "'") for x in re.findall(r"'((?:[^']|'')*)'", s))
    assert s.count("lg.DAY >= CURRENT_DATE()") == 4 and "AND lg.DAY >= DATEADD('day', -1, CURRENT_DATE())" in s
    for frag in (" so far", " and no successful login so far today", "Partial day: ", "so far today",
                 "this event is not updated when the rest of the day loads"):
        assert frag in shown, frag
    assert "this nightly count covers the whole day" not in shown
    # review W18: the takeover pointer is conditional (the rule can be disabled, or V162 rolled back alone) and
    # the Account-takeover candidates lens is the check either way
    assert "While the hourly SEC_LOGIN_TAKEOVER rule is enabled (Alerts > Rules)" in shown
    assert "either way, check Security > Access > Authentication > Account-takeover candidates" in shown
    assert "success raises SEC_LOGIN_TAKEOVER from the hourly scan" not in shown
    # the hourly rule is named only inside a string -- never a quoted rule literal a guard or arm scan would read
    assert "'SEC_LOGIN_TAKEOVER'" not in _MIG and "SEC_LOGIN_TAKEOVER" not in _strip_noise(_D)
    from tests.test_alert_rule_consistency import _ARM_REF_RE, _RULE_RE
    assert "SEC_LOGIN_TAKEOVER" not in _RULE_RE.findall(_D) and "SEC_LOGIN_TAKEOVER" not in _ARM_REF_RE.findall(_D)
    assert _NEW07_T.isascii() and _NEW07_D.isascii()


def test_v163_arm28_owner_decisions_and_reads():
    s = _insert(_ARM28)
    code = _code(s)
    objects = set(re.findall(r"DBA_MAINT_DB\.OVERWATCH\.(\w+)", code))
    assert objects == {"ALERT_EVENTS", "ALERT_CONFIG", "SETTINGS", "FACT_AI_USAGE_DAILY", "COMPANY_FOR_USER"}, objects
    for banned in ("ACCOUNT_USAGE", "CURRENT_DATE", "'CRITICAL'", "V_SECURITY", "TRY_TO_NUMBER"):
        assert banned not in code, banned
    assert code.count("CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE AS TODAY") == 1
    for frag in ("WHERE f.DAY >= DATEADD('day', -93, k.TODAY) AND f.DAY < k.TODAY",
                 "WHERE u.DAY >= DATEADD('day', -3, k.TODAY)",
                 "h.DAY < c.DAY AND h.DAY >= DATEADD('day', -90, c.DAY)",
                 "CASE WHEN COALESCE(d.N_HIST, 0) < 5 THEN NULL",
                 "WHEN d.MAD > 0 THEN 0.6745 * (c.CR - d.MED) / d.MAD",
                 "WHEN d.MEAN_AD > 0 THEN 0.7979 * (c.CR - d.MED) / d.MEAN_AD",
                 "ELSE IFF(c.CR > d.MED, 999, 0) END AS Z",
                 "AND f.USER_NAME NOT IN ('ACCOUNT', 'UNKNOWN')",
                 "AND (f.SOURCE <> 'Functions' OR n.INCL_FN)",
                 "AND u.CR > n.CAP_CR * COALESCE(c.THRESHOLD_NUM, 2)",
                 "WHERE s.Z IS NULL OR s.Z >= n.Z_MIN",
                 "COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'AI_RUNAWAY_ROBUST_Z', VALUE, NULL))), 3.5) AS Z_MIN",
                 "COALESCE(TRY_TO_BOOLEAN(MAX(IFF(KEY = 'AI_RUNAWAY_INCLUDE_FUNCTIONS', VALUE, NULL))), FALSE) AS INCL_FN",
                 "COALESCE(NULLIF(GREATEST(COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'COCO_DAILY_CAP_CREDITS', VALUE, NULL)))"
                 ", 15), 0), 0), 15) AS CAP_CR",
                 "COALESCE(NULLIF(DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(s.USER_NAME), 'UNKNOWN'), 'ALL'),",
                 "LISTAGG(DISTINCT f.SOURCE, '+') WITHIN GROUP (ORDER BY f.SOURCE) AS SOURCES",
                 "c.RULE_ID || '|' || s.USER_NAME || '|' || TO_VARCHAR(s.DAY)"):
        assert code.count(frag) == 1, frag
    assert code.count("0.6745") == 1 and code.count("0.7979") == 1
    # the one price is the scan's own AI rate (V160 settings read), never a literal
    assert set(re.findall(r"(?<![:\w]):([A-Za-z_]\w*)", _strip_noise(s))) == {"ai_credit_price"}


def _projection(stmt: str) -> list[str]:
    """The seven inner SELECT expressions of the ``b`` derived table, as Snowflake SQL."""
    sqlglot = pytest.importorskip("sqlglot")
    from sqlglot import exp
    tree = sqlglot.parse_one(stmt.replace(":ai_credit_price", "2.2"), dialect="snowflake")
    (b,) = [q for q in tree.find_all(exp.Subquery) if q.alias == "b"]
    assert [c.name for c in b.args["alias"].columns] == ["RULE_ID", "COMPANY", "SEVERITY", "TITLE", "DETAIL",
                                                         "METRIC_VALUE", "DEDUPE_KEY"]
    return [e.sql(dialect="snowflake") for e in b.this.expressions]


def test_v163_every_new_or_changed_insert_parses_with_seven_columns():
    sqlglot = pytest.importorskip("sqlglot")
    for arm in (_ARM07, _ARM28, _ARM29):
        stmt = _insert(arm).replace(":ai_credit_price", "2.2")
        parsed = sqlglot.parse(stmt, dialect="snowflake")
        assert len(parsed) == 1 and parsed[0] is not None
        assert [c.name for c in parsed[0].this.expressions] == ["RULE_ID", "COMPANY", "SEVERITY", "TITLE", "DETAIL",
                                                                 "METRIC_VALUE", "DEDUPE_KEY"]
        assert len(_projection(_insert(arm))) == 7
    p28, p29 = _projection(_insert(_ARM28)), _projection(_insert(_ARM29))
    assert p28[0] == "c.RULE_ID" and p28[2] == "c.SEVERITY" and p28[5] == "ROUND(s.CR / n.CAP_CR, 4)"
    assert p29[:3] == ["c.RULE_ID", "'ALL'", "c.SEVERITY"] and p29[5] == "s.CUR_N - s.PRIOR_N"
    for p in (p28, p29):
        assert p[3].startswith("LEFT(") and p[3].endswith(", 300)")                    # TITLE <= 300
        assert p[4].startswith("LEFT(") and p[4].endswith(", 2000)")                   # DETAIL <= 2000
    with pytest.raises(sqlglot.errors.ParseError):                                    # the parse has teeth
        sqlglot.parse(_insert(_ARM28).replace("LEFT JOIN disp d ON", "LEFT JOIN disp d ON (", 1), dialect="snowflake")


def test_v163_arm29_reads_the_snapshot_not_the_delta_view():
    s = _insert(_ARM29)
    code = _code(s)
    objects = set(re.findall(r"DBA_MAINT_DB\.OVERWATCH\.(\w+)", code))
    assert objects == {"ALERT_EVENTS", "ALERT_CONFIG", "SECURITY_TRUST_SNAPSHOT"}, objects
    assert "V_SECURITY_TRUST_DELTA" not in code and "ACCOUNT_USAGE" not in code and "CURRENT_DATE" not in code
    for frag in ("LAG(t.TOTAL_AT_RISK_COUNT) OVER (PARTITION BY t.SCANNER_ID ORDER BY t.DAY) AS PRIOR_N",
                 "LAG(t.DAY) OVER (PARTITION BY t.SCANNER_ID ORDER BY t.DAY) AS PRIOR_DAY",
                 "UPPER(t.SEVERITY) AS SEV",
                 "WHERE t.DAY >= DATEADD('day', -30, k.TODAY)",
                 "AND s.PRIOR_N IS NOT NULL", "AND s.CUR_N > 0",
                 "AND s.CUR_N - s.PRIOR_N >= COALESCE(c.THRESHOLD_NUM, 1)",
                 "AND s.SEV IN ('CRITICAL', 'HIGH')",
                 "WHERE s.DAY >= DATEADD('day', -1, k.TODAY)",
                 "c.RULE_ID || '|' || s.SCANNER_ID || '|' || TO_VARCHAR(s.DAY)"):
        assert code.count(frag) == 1, frag


def test_v163_arm29_claims_only_what_its_key_delivers():
    """Review W6: the key is RULE|SCANNER|DAY, so a scanner-day raises once, with the counts of the scan that raised
    it. A rise after ~07:00 lands the next morning only when that morning had not already raised for the
    scanner-day; a further rise the same day is not pushed again. No text may promise more (header, arm comment,
    DESCRIPTION, playbook)."""
    from app.logic.playbooks import PLAYBOOKS
    head = _ARM29[:_ARM29.index("    BEGIN\n")]
    header = _MIG[:_MIG.index("EXECUTE IMMEDIATE")]
    desc = re.search(r"SELECT 163 AS VERSION,\n       '(.*)' AS DESCRIPTION\n", _MIG, re.S).group(1)
    pb = PLAYBOOKS[_TRUST]
    for text in (head, header, desc, pb):
        flat = _norm(text.replace("--", " "))
        assert "not pushed again" in flat.lower(), text[:120]
        # the old unconditional promise is gone everywhere
        assert "so a rise after the morning scan lands the next morning. " not in flat
        assert "shows up the next morning;" not in flat
    assert "only when that scanner-day had not raised yet" in _norm(head.replace("--", " "))
    assert "unless that morning already raised for the scanner-day" in _norm(header.replace("--", " "))
    assert "(unless that scanner-day already raised)" in header
    assert "unless that morning already raised for the same scanner and day" in _norm(pb)
    assert "c.RULE_ID || '|' || s.SCANNER_ID || '|' || TO_VARCHAR(s.DAY)" in _ARM29      # the key is unchanged


_BAND_TOKENS = ("|WARN|", "|MED|", "|HIGH|", "|CRIT|", "|EXH|", "|EXPIRING", "|EXPIRED")


def _v117_identity(key: str) -> str | None:
    """Python mirror of the V117 snooze carry-forward (V157): a key ending '|YYYY-MM-DD' carries a snooze to the
    same identity (the key minus its date)."""
    if len(key) >= 11 and key[-11] == "|":
        try:
            date.fromisoformat(key[-10:])
        except ValueError:
            return None
        return key[:-10]
    return None


def test_v163_keys_fit_carry_snoozes_per_entity_and_hold_no_band_token():
    code28, code29 = _code(_insert(_ARM28)), _code(_insert(_ARM29))
    for tok in _BAND_TOKENS:
        assert tok not in code28 and tok not in code29, tok
    days = [date(2026, 3, 8) + timedelta(days=k) for k in range(3)]               # across a DST change
    for n in (1, 7, 64, 199, 200):
        for user in ("A" * n, ("first.last" * 20)[:n], ("svc_etl_" * 30)[:n]):
            keys = [f"{_AI}|{user}|{d.isoformat()}" for d in days]
            for k in keys:
                assert len(k) <= 300 and not any(t in k for t in _BAND_TOKENS)
            assert {_v117_identity(k) for k in keys} == {f"{_AI}|{user}|"}          # one identity per user
    scanner = "S" * 200                                                           # SECURITY_TRUST_SNAPSHOT.SCANNER_ID
    assert len(f"{_TRUST}|{scanner}|2026-09-29") == 232 <= 300
    assert _v117_identity(f"{_TRUST}|X|2026-09-29") == f"{_TRUST}|X|"
    # the mirror has teeth: a key without a trailing date carries nothing
    assert _v117_identity(f"{_AI}|U|20260929") is None and _v117_identity("RULE|U|2026-13-40") is None


# -- RUN_NEXT PART B -------------------------------------------------------------------------------------------
# CONTAINS(GET_DDL('PROCEDURE', ...), '<frag>'): each fragment is literally in the V163 body, free of quotes,
# backslashes and newlines; the ABSENT ones are in V160's body and not in V163's, so the grid tells them apart.
_PART_B_FRAGMENTS = ("/14 rule blocks ok (daily)", "COST_AI_USER_RUNAWAY", "SEC_TRUST_REGRESSION",
                     "and no successful login", "AI_RUNAWAY_INCLUDE_FUNCTIONS", "alert scan daily v5 (V163:",
                     "SP_SCAN_SLEEP_POLLING(FALSE)")
_PART_B_ABSENT = ("/12 rule blocks ok (daily)", "credential stuffing")


def test_v163_part_b_get_ddl_fragments(tmp_path):
    body160 = _D160[_D160.index("$$") + 2:_D160.rindex("$$")]
    for frag in (*_PART_B_FRAGMENTS, *_PART_B_ABSENT):
        assert not set(frag) & {"'", "\\", "\n", "\r"}, frag
    for frag in _PART_B_FRAGMENTS:
        assert frag in _BODY, frag
    assert not all(frag in body160 for frag in _PART_B_FRAGMENTS)               # the present set tells V163 apart
    for frag in _PART_B_ABSENT:
        assert frag not in _BODY and frag in body160, frag
    _, part_b = _extras(tmp_path)
    for frag in _PART_B_FRAGMENTS:
        assert f"IFF(CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), '{frag}'), 'OK'" in part_b
    for frag in _PART_B_ABSENT:
        assert f"IFF(NOT CONTAINS(GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()'), '{frag}'), 'OK'" in part_b
    for check in ("'V163.1 SCHEMA_VERSION has 163'", "WHERE RULE_ID = 'COST_AI_USER_RUNAWAY' AND ENABLED AND "
                  "SEVERITY = 'HIGH' AND THRESHOLD_NUM = 2", "WHERE RULE_ID = 'SEC_TRUST_REGRESSION' AND ENABLED AND "
                  "SEVERITY = 'HIGH' AND THRESHOLD_NUM = 1", "(KEY = 'AI_RUNAWAY_ROBUST_Z' AND VALUE = '3.5')",
                  "'alert scan daily 14/14 rule blocks ok (daily)'", "ERROR_TYPE = 'rule_block_failed'"):
        assert check in part_b, check
    assert "'alert scan daily ' || (14 - :fails) || '/14 rule blocks ok (daily)'" in _BODY   # the stamp PART B reads


# -- app lockstep (v4.602.0, this slice) -----------------------------------------------------------------------

def test_v163_playbooks():
    from app.logic.playbooks import PLAYBOOKS, playbook_for
    from tests.test_alert_rule_consistency import _LIVE_RE, _RETIRED_RE
    for rule in (_AI, _TRUST):
        pb = playbook_for(rule)
        assert rule in PLAYBOOKS and pb == PLAYBOOKS[rule]                          # its own entry, not the fallback
        assert pb.startswith("**Means:**")
        assert "$" not in re.sub(r"`[^`]*`", "", pb), rule                          # write USD, never a bare dollar
        assert not _RETIRED_RE.search(pb), rule
    ai = PLAYBOOKS[_AI]
    for frag in ("COCO_DAILY_CAP_CREDITS", "AI_RUNAWAY_ROBUST_Z", "No baseline yet", "fewer than 5",
                 "AI_RUNAWAY_INCLUDE_FUNCTIONS", "last 3 complete days", "Chargeback & AI", "AI users",
                 "CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY", "USER_ID", "per-user AI quota", "EXPECTED", "snooze",
                 "USD", "partly loaded"):
        assert frag in ai, frag
    trust = PLAYBOOKS[_TRUST]
    for frag in ("CRITICAL or HIGH", "previous snapshot day", "first snapshot never raises", "TRUST_CENTER_VIEWER",
                 "Security > Trust Center", "Snowsight > Monitoring > Trust Center > Findings", "re-run the scanner"):
        assert frag in trust, frag
    assert "AT_RISK_ENTITIES" not in trust                                         # only after probe S3c
    failed = PLAYBOOKS["SEC_FAILED_LOGINS"]
    assert "SEC_LOGIN_TAKEOVER" in failed and "lockout" in failed and "60 minutes" in failed
    # SEC_LOGIN_TAKEOVER is raised by V162's hourly scan (another slice): nothing near its name here may read as a
    # live-claim or a retired-claim to Guards A/B, whichever order the slices land in
    text = read("app/logic/playbooks.py")
    entry = _between(text, '    "SEC_FAILED_LOGINS": (', '    "SEC_NEW_ADMIN_NETWORK": (')
    start = text.index(entry)
    mentions = [m for m in re.finditer(r"\bSEC_LOGIN_TAKEOVER\b", text) if start <= m.start() < start + len(entry)]
    assert mentions
    for m in mentions:
        window = text[max(0, m.start() - 260):m.end() + 260]
        assert not _LIVE_RE.search(window) and not _RETIRED_RE.search(window), window
    # placement: after COST_SLEEP_POLLING and after SEC_NEW_EXPOSURE
    keys = list(PLAYBOOKS)
    assert keys.index(_AI) == keys.index("COST_SLEEP_POLLING") + 1
    assert keys.index(_TRUST) == keys.index("SEC_NEW_EXPOSURE") + 1


def test_v163_navigation_and_evidence():
    from app.logic import navigate
    from app.logic.alert_evidence import plan_for_alert
    assert navigate._RULE_TARGETS[_AI] == ("Cost Intelligence", "Chargeback & AI")
    assert navigate._RULE_TARGETS[_TRUST] == ("Security", "Trust Center")
    assert "Chargeback & AI" in page_source("cost") and '"Trust Center"' in page_source("security")
    title_ai = "first.last.name used 44.0 AI credits on 2026-09-28 (~$97): 2.9x the 15-credit daily cap"
    title_tr = "Trust Center HIGH scanner regressed: ALFA_DB.PUBLIC.X on WH_ALFA_ADMIN at-risk entities 3 -> 5"
    for rule, title in ((_AI, title_ai), (_TRUST, title_tr)):
        assert rule in navigate._NO_ENTITY_FILTER_RULES
        assert navigate.investigation_target(rule, f"{title} dotted.user.name. text WH_X")["filters"] == {}
        assert navigate.fix_target(rule, title) is None and rule not in navigate.INLINE_FIX_RULES
        assert navigate.inline_fix_warehouse(rule, title) == ""
        assert plan_for_alert(rule, title, "detail WH_X") is None                  # no off-topic evidence pack
    # each arm's DETAIL points at the page / section Investigate opens
    for arm, path in ((_ARM28, "Cost Intelligence > Chargeback & AI > AI users"),
                      (_ARM29, "Security > Trust Center for the scanner delta")):
        shown = "".join(x.replace("''", "'") for x in re.findall(r"'((?:[^']|'')*)'", _insert(arm)))
        assert path in shown, path


def test_v163_settings_editors():
    from app.config import DEFAULT_SETTINGS
    from app.ui.pages import admin
    assert DEFAULT_SETTINGS["AI_RUNAWAY_ROBUST_Z"] == 3.5
    assert DEFAULT_SETTINGS["AI_RUNAWAY_INCLUDE_FUNCTIONS"] == "FALSE"
    assert admin._SETTING_EDITORS["AI_RUNAWAY_ROBUST_Z"] == (admin._NUM, {"min_value": 1.0, "step": 0.5})
    assert admin._SETTING_EDITORS["AI_RUNAWAY_INCLUDE_FUNCTIONS"] == ("enum", ["FALSE", "TRUE"])
    keys = list(DEFAULT_SETTINGS)
    assert keys.index("AI_RUNAWAY_ROBUST_Z") == keys.index("COCO_DAILY_CAP_CREDITS") + 1
    assert keys.index("AI_RUNAWAY_INCLUDE_FUNCTIONS") == keys.index("AI_RUNAWAY_ROBUST_Z") + 1


def test_v163_runbook_rows():
    rb = read("RUNBOOK.md")
    for rule, family, arm in ((_AI, "COST", "[28]"), (_TRUST, "SECURITY", "[29]")):
        rows = [ln for ln in rb.splitlines() if ln.startswith(f"| {rule} | {family} |")]
        assert len(rows) == 1, (rule, rows)
        assert "V163" in rows[0] and f"daily {arm}" in rows[0], rows[0]
    (failed,) = [ln for ln in rb.splitlines() if ln.startswith("| SEC_FAILED_LOGINS | SECURITY |")]
    assert "V163" in failed and "SEC_LOGIN_TAKEOVER" in failed

def test_v163_in_expected_migrations():
    """Integrator lockstep: Admin lists V163 with house-rule text (no $, no hand CALL, no trailing '.')."""
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    text = str(_EXPECTED_MIGRATIONS[163])
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_" not in text and "$" not in text and not text.endswith(".")
    assert "COST_AI_USER_RUNAWAY" in text and "SEC_TRUST_REGRESSION" in text and "14" in text
